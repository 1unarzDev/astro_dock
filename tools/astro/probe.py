"""Message rates of some topics, measured where they are published. Standalone (no astro imports): `astro up`
and `astro status` send this file as the stdin of `python3 - TIMEOUT WINDOW PID TOPIC...` on a stack's host.

Waits (at most TIMEOUT s, or until process group PID is gone; 0 = none) until every topic has published,
then counts messages for WINDOW s, and prints one line: `astro-rates {"topic": hz, ..., "_dead": bool, "_pubs":
{"topic": [node, ...]}}`. `python3 - pubs SETTLE TOPIC...` only lists each topic's publishing nodes (the most
seen within SETTLE s of discovery, sooner once every topic has one): `astro-pubs {"topic": [node, ...]}`."""
import json
import os
import sys
import time


def alive(pid):
    if not pid:
        return True
    try:
        os.killpg(pid, 0)
        return True
    except OSError:
        return False


def names(node, topic):
    return sorted(('/' + i.node_namespace.strip('/') + '/' + i.node_name).replace('//', '/')
                  for i in node.get_publishers_info_by_topic(topic))


def publishers(topics, settle):
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('astro_probe')
    seen, end = {t: [] for t in topics}, time.monotonic() + settle
    try:
        while time.monotonic() < end:
            for topic in topics:
                found = names(node, topic)
                if len(found) > len(seen[topic]):
                    seen[topic] = found
            if all(seen.values()):        # every topic has one: enough to tell it runs
                break
            rclpy.spin_once(node, timeout_sec=0.1)
        return seen
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def rates(topics, timeout, window, pid=0):
    import rclpy
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    from rosidl_runtime_py.utilities import get_message
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('astro_probe')
    counts = dict.fromkeys(topics, 0)
    # Volatile matches every publisher; reliable when every publisher is (a large message such as a costmap
    # arrives in fragments that best effort drops), else best effort, which matches any.
    def qos_for(topic):
        infos = node.get_publishers_info_by_topic(topic)
        reliable = bool(infos) and all(i.qos_profile.reliability == ReliabilityPolicy.RELIABLE for i in infos)
        return QoSProfile(depth=10, durability=DurabilityPolicy.VOLATILE, reliability=ReliabilityPolicy.RELIABLE
                          if reliable else ReliabilityPolicy.BEST_EFFORT)
    subscribed, begin, dead = set(), time.monotonic(), False

    def spin_until(done, limit):
        while time.monotonic() < limit and not done():
            types = dict(node.get_topic_names_and_types())
            for topic in topics:
                if topic in types and topic not in subscribed:
                    subscribed.add(topic)
                    node.create_subscription(get_message(types[topic][0]), topic,
                                             lambda _, t=topic: counts.__setitem__(t, counts[t] + 1), qos_for(topic))
            rclpy.spin_once(node, timeout_sec=0.1)
    try:
        def ready():
            nonlocal dead
            dead = not alive(pid)
            return dead or all(counts.values())
        spin_until(ready, begin + timeout)
        if not dead:
            counts.update(dict.fromkeys(topics, 0))
            start = time.monotonic()
            spin_until(lambda: False, start + window)
            span = time.monotonic() - start
            out = {t: round(c / span, 1) for t, c in counts.items()}
        else:
            out = dict.fromkeys(topics, 0.0)
        out['_dead'] = dead or not alive(pid)
        out['_pubs'] = {t: names(node, t) for t in topics}
        return out
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    if sys.argv[1] == 'pubs':
        print('astro-pubs ' + json.dumps(publishers(sys.argv[3:], float(sys.argv[2]))), flush=True)
        sys.exit(0)
    timeout, window, pid = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
    print('astro-rates ' + json.dumps(rates(sys.argv[4:], timeout, window, pid)), flush=True)
