"""`astro status [--watch]`: one plain-text page - the ROS nodes, the last message on each topic of the tests
file, the recording, the last log lines and the next test."""
import json
import math
import time

from . import config, rec
from .engine import log


def summarize(msg):
    """A short line for any message: Twist/Odometry/String/BatteryState get a shaped one."""
    kind = type(msg).__name__
    if kind == 'Twist':
        return f'x={msg.linear.x:+.2f} y={msg.linear.y:+.2f} wz={msg.angular.z:+.2f}'
    if kind == 'Odometry':
        t, q = msg.twist.twist, msg.pose.pose.orientation
        yaw = math.degrees(math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))
        return (f'vx={t.linear.x:+.2f} vy={t.linear.y:+.2f} wz={t.angular.z:+.2f} yaw={yaw:.0f}deg '
                f'xy=({msg.pose.pose.position.x:.1f},{msg.pose.pose.position.y:.1f})')
    if kind == 'String':
        try:
            data = json.loads(msg.data)
        except ValueError:
            data = None
        if isinstance(data, dict):       # a JSON state: its short top-level fields
            return ' '.join(f'{k}={json.dumps(v)}' for k, v in data.items()
                            if not k.endswith('_ns') and (not isinstance(v, (dict, list)) or len(json.dumps(v)) < 60))[:160]
        return msg.data.replace('\n', ' ')[:110]
    if kind == 'BatteryState':
        return f'{msg.voltage:.2f} V'
    try:
        from rosidl_runtime_py import message_to_yaml
        text = ' '.join(message_to_yaml(msg).split())
    except ImportError:
        text = str(msg)
    return text[:110]


def sample(topics, seconds=1.5):
    """{topic: summary} of the last message on each topic within ``seconds``, plus the node list."""
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from rosidl_runtime_py.utilities import get_message
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('astro_status')
    seen = {}
    try:
        end = time.monotonic() + 3.0                 # discovery: until every topic shows, at most 3 s
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
            types = dict(node.get_topic_names_and_types())
            if all(t in types for t in topics):
                break
        for topic in topics:
            if topic in types:
                cls = get_message(types[topic][0])
                node.create_subscription(cls, topic, lambda m, t=topic: seen.__setitem__(t, summarize(m)),
                                         10)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.05)
        nodes = sorted(f'{ns.rstrip("/")}/{name}' for name, ns in node.get_node_names_and_namespaces()
                       if name != 'astro_status')
        return nodes, {t: seen.get(t, '(no message)' if t in types else '(not published)') for t in topics}
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def page(vessel, spec, rows_next, lines=6):
    out = [f'vessel {vessel.name}  data {vessel.data_root()}  {time.strftime("%H:%M:%S")}']
    if config.have_ros() and spec:
        topics = [t for t in dict.fromkeys(spec.topics.values()) if isinstance(t, str)]
        nodes, seen = sample(topics)
        out.append(f'nodes  {" ".join(nodes) or "(none)"}')
        width = max(map(len, spec.topics)) if spec.topics else 0
        for name, topic in spec.topics.items():
            out.append(f'  {name:<{width}}  {topic}  {seen.get(topic, "")}')
    elif not config.have_ros():
        out.append('(no ROS here: run astro status in the dev container for the live part)')
    found = rec.running(vessel.data_root())
    out.append(f'rec    {found[1] if found else "off"}')
    tail = log.read(vessel.data_root())[-lines:]
    out.append('log    ' + ('\n       '.join(tail) if tail else '(empty today)'))
    out.append(f'next   {rows_next}')
    return '\n'.join(out)
