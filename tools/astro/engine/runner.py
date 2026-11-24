"""The one ROS node of a test run: publishes a geometry_msgs/Twist command at 20 Hz on the node clock (so
use_sim_time follows /clock), records nav_msgs/Odometry body twist and yaw, and on Ctrl-C (or any error)
publishes zeros several times before it lets go."""
import math
import time

RATE_HZ = 20.0


class Runner:
    def __init__(self, command_topic, odom_topic, battery_topic=None, sim_time=False):
        import rclpy
        from rclpy.parameter import Parameter
        from rclpy.signals import SignalHandlerOptions
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        self.rclpy, self.Twist = rclpy, Twist
        # Python's own SIGINT handler stays: Ctrl-C raises KeyboardInterrupt with the context still up,
        # so neutral can still be published.
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self.node = rclpy.create_node('astro_test', parameter_overrides=[
            Parameter('use_sim_time', Parameter.Type.BOOL, bool(sim_time))])
        self.pub = self.node.create_publisher(Twist, command_topic, 10)
        self.rows = []                    # (t, vx, vy, wz, yaw, x, y, header stamp)
        self.battery = None
        self.node.create_subscription(Odometry, odom_topic, self._odom, 20)
        if battery_topic:
            from sensor_msgs.msg import BatteryState
            self.node.create_subscription(BatteryState, battery_topic, self._battery, 5)
        self.command = [0.0, 0.0, 0.0]

    def now(self):
        return self.node.get_clock().now().nanoseconds * 1e-9

    def _odom(self, msg):
        q = msg.pose.pose.orientation
        norm = q.w * q.w + q.x * q.x + q.y * q.y + q.z * q.z
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) if norm > 0.5 else None
        t = msg.twist.twist
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        self.rows.append((self.now(), t.linear.x, t.linear.y, t.angular.z, yaw,
                          msg.pose.pose.position.x, msg.pose.pose.position.y, stamp))

    def _battery(self, msg):
        self.battery = msg.voltage

    def _send(self, command):
        msg = self.Twist()
        msg.linear.x, msg.linear.y, msg.angular.z = (float(v) for v in command)
        self.pub.publish(msg)

    def gate_state(self, gate, timeout=3.0):
        """A latched JSON state (gate: {state, held, cause}; dotted paths): (seen, the cause or held value, or None)."""
        import json
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        seen = {}

        def state(msg):
            try:
                seen['state'] = json.loads(msg.data)
            except ValueError:
                pass
        sub = self.node.create_subscription(String, gate['state'], state, qos)
        try:
            end = time.monotonic() + timeout
            while 'state' not in seen and time.monotonic() < end:
                self.rclpy.spin_once(self.node, timeout_sec=0.05)
            if not isinstance(seen.get('state'), dict):
                return False, None
            state = seen['state']

            def get(path):
                node = state
                for part in str(path).split('.'):
                    node = node.get(part) if isinstance(node, dict) else None
                return node
            held = get(gate.get('held', 'hold'))
            if held and gate.get('cause'):
                held = get(gate['cause']) or held
            return True, held
        finally:
            self.node.destroy_subscription(sub)

    def watch(self, lane):
        """Record the arbiter's latched JSON state during the run (lane: {state, field, cause}):
        self.lane_log gets (t, field value, cause value) on each message."""
        import json
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.lane_log = []

        def state(msg):
            try:
                data = json.loads(msg.data)
            except ValueError:
                return
            if isinstance(data, dict):
                self.lane_log.append((self.now(), data.get(lane.get('field', 'input')), data.get(lane.get('cause', 'cause'))))
        self.node.create_subscription(String, lane['state'], state, qos)

    def wait_ready(self, timeout=10.0):
        """Until the clock runs and odometry arrives; False after ``timeout`` wall seconds."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.rclpy.spin_once(self.node, timeout_sec=0.05)
            if self.now() > 0 and self.rows:
                return True
        return False

    def hold(self, command, seconds):
        """Publish ``command`` for ``seconds`` of node time; returns (t0, t1)."""
        self.command = list(command)
        t0 = self.now()
        next_send, period = t0, 1.0 / RATE_HZ
        while True:
            now = self.now()
            if now - t0 >= seconds:
                return t0, now
            if now >= next_send:
                self._send(self.command)
                next_send = max(next_send + period, now)
            self.rclpy.spin_once(self.node, timeout_sec=period / 4)

    def window(self, t0, t1):
        return [r for r in self.rows if t0 <= r[0] <= t1]

    def neutral(self, repeats=5):
        self.command = [0.0, 0.0, 0.0]
        for _ in range(repeats):
            self._send(self.command)
            time.sleep(0.02)

    def close(self):
        try:
            self.neutral()
        finally:
            self.node.destroy_node()
            self.rclpy.try_shutdown()
