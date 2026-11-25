"""`astro status [--watch]`: one plain-text page - the stacks (host, up/down, uptime, ready rates, log), the ROS nodes, the last message on each topic of the tests
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


def sample(topics, seconds=2.0):
    """{topic: summary} of the last message on each topic within ``seconds``, plus the node list."""
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from rosidl_runtime_py.utilities import get_message
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('astro_status')
    seen = {}
    try:
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        # Best effort and volatile match every publisher (reliable or not, latched or not).
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
        subscribed, begin = set(), time.monotonic()
        while time.monotonic() - begin < 6.0:        # discovery and messages: until each topic has spoken, at most 6 s
            types = dict(node.get_topic_names_and_types())
            for topic in topics:
                if topic in types and topic not in subscribed:
                    subscribed.add(topic)
                    node.create_subscription(get_message(types[topic][0]), topic,
                                             lambda m, t=topic: seen.__setitem__(t, summarize(m)), qos)
            rclpy.spin_once(node, timeout_sec=0.1)
            if all(t in seen for t in topics) or (time.monotonic() - begin > seconds
                                                  and all(t in seen for t in subscribed)):
                break
        nodes = sorted(f'{ns.rstrip("/")}/{name}' for name, ns in node.get_node_names_and_namespaces()
                       if name != 'astro_status')
        return nodes, {t: seen.get(t, '(no message)' if t in subscribed else '(not published)') for t in topics}
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def stacks_block(lines):
    """The stacks part of the page (stacks.table lines), or nothing when the vessel has no stacks."""
    return ['stacks ' + lines[0]] + ['       ' + line for line in lines[1:]] if lines else []


def page(vessel, spec, rows_next, lines=6, stack_lines=()):
    inst = f'  instance {vessel.instance}' if getattr(vessel, 'is_sim', False) else ''
    out = [f'vessel {vessel.name}{inst}  data {vessel.data_root()}  {time.strftime("%H:%M:%S")}']
    out += stacks_block(list(stack_lines))
    if config.have_ros() and spec:
        topics = [t for t in dict.fromkeys(spec.topics.values()) if isinstance(t, str)]
        nodes, seen = sample(topics)
        wanted = list(spec.nodes)
        shown = [f'{n} {"up" if n in nodes else "DOWN"}' for n in wanted]
        others = len([n for n in nodes if n not in wanted and not n.split('/')[-1].startswith('_')])
        out.append(f'nodes  {"  ".join(shown + [f"+{others} others"])}')
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
