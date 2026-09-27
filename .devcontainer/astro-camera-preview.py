#!/usr/bin/env python3
"""LAN-only JPEG preview across incompatible ROS distributions, without DDS bridging."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

MAX_FRAME_BYTES = 8 * 1024 * 1024


class LatestFrame:
    """Thread-safe single-frame buffer; never replay an old camera as live."""

    def __init__(self):
        self.lock = threading.Lock()
        self.value = None

    def put(self, data, sec, nanosec, frame_id):
        data = bytes(data)
        if not data.startswith(b'\xff\xd8') or len(data) > MAX_FRAME_BYTES:
            return False
        if not 0 <= nanosec < 1_000_000_000:
            return False
        frame_id = frame_id.encode('ascii', errors='replace').decode('ascii')
        frame_id = frame_id.replace('\r', '').replace('\n', '')[:256]
        with self.lock:
            self.value = (data, int(sec), int(nanosec), frame_id, time.monotonic())
        return True

    def get(self, max_age=2.0):
        with self.lock:
            if self.value is None or time.monotonic() - self.value[-1] > max_age:
                return None
            return self.value[:4]


def preview_server(address, frames):
    """Serve a browser preview and one bounded JPEG per request."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/':
                payload = (b'<!doctype html><title>Astro camera preview</title>'
                           b'<h1>Camera preview (JPEG; not navigation data)</h1>'
                           b'<p id="status">Waiting for camera</p><img id="camera">'
                           b'<script>const i=document.getElementById("camera"),'
                           b's=document.getElementById("status");'
                           b'function next(){setTimeout(()=>i.src="/frame.jpg?t="+Date.now(),200)}'
                           b'i.onload=()=>{i.style.visibility="visible";s.textContent="Live";next()};'
                           b'i.onerror=()=>{i.style.visibility="hidden";s.textContent="No fresh frame";next()};'
                           b'next();</script>')
                content_type = 'text/html; charset=utf-8'
                metadata = None
            elif path == '/frame.jpg':
                metadata = frames.get()
                if metadata is None:
                    self.send_error(503, 'No fresh JPEG frame')
                    return
                payload = metadata[0]
                content_type = 'image/jpeg'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            if metadata:
                self.send_header('X-ROS-Stamp-Sec', str(metadata[1]))
                self.send_header('X-ROS-Stamp-Nanosec', str(metadata[2]))
                self.send_header('X-ROS-Frame-ID', metadata[3])
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server


def fetch_frame(url):
    with urlopen(url.rstrip('/') + '/frame.jpg', timeout=2) as response:
        if response.headers.get_content_type() != 'image/jpeg':
            raise ValueError('Expected image/jpeg')
        data = response.read(MAX_FRAME_BYTES + 1)
        sec = int(response.headers['X-ROS-Stamp-Sec'])
        nanosec = int(response.headers['X-ROS-Stamp-Nanosec'])
        frame_id = response.headers.get('X-ROS-Frame-ID', '')
    if len(data) > MAX_FRAME_BYTES or not data.startswith(b'\xff\xd8'):
        raise ValueError('Invalid or oversized JPEG')
    if not 0 <= nanosec < 1_000_000_000:
        raise ValueError('Invalid camera timestamp')
    return data, sec, nanosec, frame_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    serve = modes.add_parser('serve', help='Run in the camera ROS domain')
    serve.add_argument('--topic', default='/zed/zed_node/left/image_rect_color/compressed')
    serve.add_argument('--bind', default='127.0.0.1', help='Use boat LAN IP for remote viewers')
    serve.add_argument('--port', type=int, default=8088)
    receive = modes.add_parser('receive', help='Run in the laptop ROS domain')
    receive.add_argument('url', help='Example: http://squirtle-jetson.local:8088')
    receive.add_argument('--topic', default='/astro_camera/image/compressed')
    receive.add_argument('--fps', type=float, default=5.0)
    args = parser.parse_args()
    if args.mode == 'receive' and not 0 < args.fps <= 30:
        parser.error('--fps must be greater than 0 and at most 30')

    # Lazy imports keep HTTP transport tests independent of ROS releases.
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage

    rclpy.init(args=[])
    node = rclpy.create_node('astro_camera_preview_' + args.mode)
    server = None
    thread = None
    try:
        if args.mode == 'serve':
            frames = LatestFrame()

            def callback(message):
                frames.put(message.data, message.header.stamp.sec,
                           message.header.stamp.nanosec, message.header.frame_id)

            subscription = node.create_subscription(
                CompressedImage, args.topic, callback, qos_profile_sensor_data)
            server = preview_server((args.bind, args.port), frames)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print('LAN-only, unauthenticated preview: http://{}:{}; topic {}'.format(
                args.bind, args.port, args.topic), flush=True)
            rclpy.spin(node)
        else:
            publisher = node.create_publisher(CompressedImage, args.topic, 10)
            previous = None
            failed = False
            while rclpy.ok():
                started = time.monotonic()
                try:
                    data, sec, nanosec, frame_id = fetch_frame(args.url)
                    if (sec, nanosec) != previous:
                        message = CompressedImage()
                        message.header.stamp.sec = sec
                        message.header.stamp.nanosec = nanosec
                        message.header.frame_id = frame_id
                        message.format = 'jpeg'
                        message.data = data
                        publisher.publish(message)
                        previous = (sec, nanosec)
                        if failed:
                            print('Camera preview recovered', flush=True)
                        failed = False
                except (URLError, OSError, ValueError, TypeError) as error:
                    if not failed:
                        print('Waiting for fresh camera preview: {}'.format(error), flush=True)
                    failed = True
                rclpy.spin_once(node, timeout_sec=0)
                time.sleep(max(0, 1 / args.fps - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
