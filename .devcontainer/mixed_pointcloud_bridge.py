#!/usr/bin/env python3
"""Bridge a Fast DDS LARGE_DATA PointCloud2 stream into the normal DDS graph.

Run one ingest process with LARGE_DATA and one publish process with the normal
Fast DDS transport.  The processes exchange serialized PointCloud2 messages
over localhost, so the ROS graph keeps one transport policy per participant.
"""

import argparse
import socket
import struct
import threading

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message, serialize_message
from sensor_msgs.msg import PointCloud2


class Ingest(Node):
    def __init__(self, bind, port, topic, max_bytes):
        super().__init__("mixed_pointcloud_ingest")
        self._client = None
        self._pending = None
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((bind, port))
        self._server.listen(1)
        self._server.settimeout(0.2)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

        qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.create_subscription(PointCloud2, topic, self._on_cloud, qos)
        self.get_logger().info(
            f"ingest {topic}; forwarding serialized messages on tcp://{bind}:{port}"
        )

    def _accept_loop(self):
        while rclpy.ok():
            try:
                client, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                old, self._client = self._client, client
                self._wake.notify_all()
            if old:
                old.close()
            self.get_logger().info(f"publisher connected from {address[0]}:{address[1]}")

    def _on_cloud(self, message):
        # A VLP16 PointCloud2 can be ~642 KiB.  That is small enough for the
        # LARGE_DATA participant but still exceeds the normal Fast DDS UDP
        # transport used by RViz/TF.  Keep the bridge publisher on the normal
        # transport by decimating whole points before serialization.  The
        # fields and frame remain unchanged, so costmaps and RViz consume the
        # same PointCloud2 contract with a bounded payload.
        if self._max_bytes and len(message.data) > self._max_bytes:
            point_step = message.point_step
            if point_step <= 0:
                self.get_logger().warning("dropping PointCloud2 with zero point_step")
                return
            keep = max(1, self._max_bytes // point_step)
            total = message.width * message.height
            stride = max(1, (total + keep - 1) // keep)
            data = b"".join(
                bytes(message.data[i * point_step : (i + 1) * point_step])
                for i in range(0, total, stride)
            )
            message = PointCloud2(
                header=message.header,
                height=1,
                width=len(data) // point_step,
                fields=message.fields,
                is_bigendian=message.is_bigendian,
                point_step=point_step,
                row_step=len(data),
                data=data,
                is_dense=message.is_dense,
            )
        payload = serialize_message(message)
        packet = struct.pack("!I", len(payload)) + payload
        # Keep only the newest complete packet.  A blocked TCP writer must not
        # block the ROS subscription callback or let stale clouds accumulate.
        with self._wake:
            self._pending = packet
            self._wake.notify()

    def _send_loop(self):
        while rclpy.ok():
            with self._wake:
                while rclpy.ok() and (self._client is None or self._pending is None):
                    self._wake.wait(timeout=0.2)
                if not rclpy.ok():
                    return
                client = self._client
                packet = self._pending
                self._pending = None
            try:
                client.sendall(packet)
            except OSError:
                with self._wake:
                    if self._client is client:
                        self._client = None
                client.close()

    def close(self):
        self._server.close()
        with self._wake:
            self._pending = None
            if self._client:
                self._client.close()
                self._client = None
            self._wake.notify_all()
        self.destroy_node()


class Publish(Node):
    def __init__(self, host, port, topic):
        super().__init__("mixed_pointcloud_publish")
        self._host, self._port = host, port
        self._socket = None
        self._buffer = bytearray()
        qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        # A reliable offered QoS is compatible with both RViz's reliable
        # reader and Nav2's best-effort SensorDataQoS reader.  Keep one writer:
        # two writers with different reliability policies make best-effort
        # subscribers receive every cloud twice and halve useful throughput.
        self._publisher = self.create_publisher(PointCloud2, topic, qos)
        self.create_timer(0.02, self._poll)
        self.get_logger().info(
            f"publishing {topic} through the default DDS transport from "
            f"tcp://{host}:{port}"
        )

    def _connect(self):
        if self._socket:
            return True
        try:
            self._socket = socket.create_connection((self._host, self._port), timeout=1)
            self._socket.setblocking(False)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.get_logger().info("connected to LARGE_DATA ingest")
            return True
        except OSError:
            return False

    def _poll(self):
        if not self._connect():
            return
        try:
            while True:
                try:
                    chunk = self._socket.recv(1024 * 1024)
                except BlockingIOError:
                    break
                if not chunk:
                    raise ConnectionError("ingest closed")
                self._buffer.extend(chunk)
                latest_payload = None
                while len(self._buffer) >= 4:
                    length = struct.unpack("!I", self._buffer[:4])[0]
                    if length > 32 * 1024 * 1024:
                        raise ValueError(f"cloud is too large: {length} bytes")
                    if len(self._buffer) < 4 + length:
                        break
                    latest_payload = bytes(self._buffer[4 : 4 + length])
                    del self._buffer[: 4 + length]
                if latest_payload is not None:
                    message = deserialize_message(latest_payload, PointCloud2)
                    self._publisher.publish(message)
        except (ConnectionError, OSError, ValueError) as error:
            self.get_logger().warning(f"resetting ingest connection: {error}")
            self._socket.close()
            self._socket = None
            self._buffer.clear()

    def close(self):
        if self._socket:
            self._socket.close()
        self.destroy_node()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("ingest", "publish"))
    parser.add_argument("--topic", default="/points")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47680)
    parser.add_argument(
        "--max-bytes", type=int, default=32 * 1024,
        help="maximum PointCloud2 data bytes sent to the normal DDS publisher",
    )
    args = parser.parse_args()

    rclpy.init()
    node = (
        Ingest(args.bind, args.port, args.topic, args.max_bytes)
        if args.role == "ingest"
        else Publish(args.host, args.port, args.topic)
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
