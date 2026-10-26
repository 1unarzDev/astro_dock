#!/usr/bin/env python3
"""airMAX link measurement and tuning proposals for devices/radio/airmax.sh.

  udp-rate RAW LABEL                 Mbit/s for a UDP test: 70% of LABEL's TCP rate, at least 1.
  summary RAW OUT NAME=ROLE...       parse the raw measure output, write OUT (JSON), print a table.
  propose FILE RANGE_M FREQ WIDTH_MAX TXPOWER_MAX REVERT_S COMMAND
                                     print setting changes per radio and the plan/apply commands.
Nothing here contacts a device.
"""
import json
import math
import re
import sys

LABELS = ('tcp to-target', 'tcp from-target', 'udp to-target', 'udp from-target')


def sections(raw):
    out = {}
    for part in open(raw).read().split('\n==== ')[1:]:
        name, _, body = part.partition('\n')
        out[name] = body
    return out


def get(data, path):
    for key in path.split('.'):
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def first(*values):
    return next((v for v in values if v not in (None, '')), None)


def jsonish(text):
    start = min([i for i in (text.find('{'), text.find('[')) if i >= 0] or [-1])
    try:
        return json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        return None


def num(v):
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def udp_rate(raw, label):
    try:
        bps = json.loads(sections(raw)['iperf3 ' + label])['end']['sum_received']['bits_per_second']
    except Exception:
        bps = 0
    print(max(1, round(bps * 0.7 / 1e6)))


def radio(body, role):
    parts = {}
    for chunk in ('\n' + body).split('\n### ')[1:]:
        name, _, text = chunk.partition('\n')
        parts[name.split()[-1].rsplit('/', 1)[-1]] = text
    mca = {}
    for line in parts.get('mca-status', '').splitlines():
        for item in line.split(','):
            if '=' in item:
                k, v = item.split('=', 1)
                mca[k.strip()] = v.strip()
    cfg = dict(line.split('=', 1) for line in parts.get('cfg', '').splitlines() if '=' in line)
    status = jsonish(parts.get('status.cgi', '')) or {}
    sta = jsonish(parts.get('wstalist', '')) or get(status, 'wireless.sta') or []
    sta = sta[0] if isinstance(sta, list) and sta else {}
    return {
        'role': role,
        'signal': num(first(sta.get('signal'), get(status, 'wireless.signal'), mca.get('signal'))),
        'noise': num(first(sta.get('noisefloor'), get(status, 'wireless.noisef'), mca.get('noise'))),
        'cinr_rx': num(get(sta, 'airmax.rx.cinr')), 'cinr_tx': num(get(sta, 'airmax.tx.cinr')),
        'tx_rate': first(sta.get('tx_rate'), mca.get('wlanTxRate')),
        'rx_rate': first(sta.get('rx_rate'), mca.get('wlanRxRate')),
        'tx_mcs': first(sta.get('tx_idx')), 'tx_nss': first(sta.get('tx_nss')),
        'rx_mcs': first(sta.get('rx_idx')), 'rx_nss': first(sta.get('rx_nss')),
        'capacity_dl': num(first(get(sta, 'airmax.dl_capacity'), get(status, 'wireless.polling.dl_capacity'))),
        'capacity_ul': num(first(get(sta, 'airmax.ul_capacity'), get(status, 'wireless.polling.ul_capacity'))),
        'capacity': num(mca.get('wlanPollingCapacity')),
        'quality': num(first(mca.get('wlanPollingQuality'), get(sta, 'airmax.quality'), sta.get('ccq'), mca.get('ccq'))),
        'retries': num(first(sta.get('tx_lretries'), get(sta, 'stats.tx_retries'), get(status, 'wireless.stats.tx_retries'))),
        'latency_ms': num(first(sta.get('tx_latency'), mca.get('wlanTxLatency'))),
        'freq': num(first(get(status, 'wireless.frequency'), mca.get('wlanFrequency'), mca.get('freq'), cfg.get('radio.1.freq'))),
        'center': num(first(get(status, 'wireless.center1_freq'), cfg.get('radio.1.center.1.freq'))),
        'width': num(first(get(status, 'wireless.chanbw'), cfg.get('radio.1.chanbw'), mca.get('chwidth'))),
        'distance_m': num(first(sta.get('distance'), get(status, 'wireless.distance'), mca.get('distance'))),
        'txpower': num(first(get(status, 'wireless.txpower'), cfg.get('radio.1.txpower'), mca.get('wlanTxPower'))),
        'ack': 'auto' if cfg.get('radio.1.ack.auto') == 'enabled' else cfg.get('radio.1.ackdistance'),
        'uptime_s': num(parts.get('uptime', '')),
        'cfg': cfg,
    }


def iperf(text, label):
    data = jsonish(text) or {}
    end = data.get('end', {})
    if label.startswith('tcp'):
        r = {'mbps': get(end, 'sum_received.bits_per_second'), 'retransmits': get(end, 'sum_sent.retransmits')}
    else:
        r = {'mbps': get(end, 'sum.bits_per_second'), 'jitter_ms': get(end, 'sum.jitter_ms'),
             'lost_percent': get(end, 'sum.lost_percent')}
    if r['mbps'] is not None:
        r['mbps'] = round(r['mbps'] / 1e6, 1)
    r['error'] = data.get('error') or (None if data else 'no result')
    return r


def ping(text):
    loss = re.search(r'([0-9.]+)% packet loss', text)
    rtt = re.search(r'= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms', text)
    r = {'loss_percent': float(loss.group(1)) if loss else None}
    if rtt:
        r.update(zip(('min_ms', 'avg_ms', 'max_ms', 'mdev_ms'), map(float, rtt.groups())))
    return r


def f(v, fmt='%g'):
    return '?' if v is None else fmt % v if isinstance(v, (int, float)) else str(v)


def summary(raw, out, *roles):
    s = sections(raw)
    result = {'raw': raw,
              'radios': {n: radio(s.get('radio ' + n, ''), role) for n, role in (r.split('=', 1) for r in roles)},
              'iperf': {label: iperf(s.get('iperf3 ' + label, ''), label) for label in LABELS},
              'ping': {label: ping(s.get('ping ' + label, '')) for label in ('idle',) + LABELS}}
    json.dump(result, open(out, 'w'), indent=1)
    row = '%-7s %6s %6s %9s %8s %15s %-11s %9s %6s %10s %6s %5s'
    print(row % ('radio', 'signal', 'noise', 'cinr rx/tx', 'quality', 'capacity dl/ul', 'mcs tx/rx', 'rate tx/rx',
                 'retry', 'freq/width', 'dist', 'txpwr'))
    for name, r in result['radios'].items():
        print(row % (
            name, f(r['signal']), f(r['noise']), '%s/%s' % (f(r['cinr_rx']), f(r['cinr_tx'])), f(r['quality']),
            '%s/%s' % (f(r['capacity_dl']), f(r['capacity_ul'])) if r['capacity_dl'] is not None else f(r['capacity']),
            '%s/%s' % (f(r['tx_mcs']), f(r['rx_mcs'])), '%s/%s' % (f(r['tx_rate']), f(r['rx_rate'])), f(r['retries']),
            '%s/%s' % (f(r['freq']), f(r['width'])), f(r['distance_m']), f(r['txpower'])))
        print('        ack %s, uptime %s s, tx latency %s ms' % (f(r['ack']), f(r['uptime_s'], '%d'), f(r['latency_ms'])))
    for label in LABELS:
        i, p = result['iperf'][label], result['ping'][label]
        extra = ('retrans %s' % f(i.get('retransmits'))) if label.startswith('tcp') else \
            'jitter %s ms, lost %s%%' % (f(i.get('jitter_ms'), '%.1f'), f(i.get('lost_percent'), '%.1f'))
        print('%-16s %7s Mbit/s  %-24s | ping avg %s max %s mdev %s ms, loss %s%%%s' % (
            label, f(i['mbps']), extra, f(p.get('avg_ms')), f(p.get('max_ms')), f(p.get('mdev_ms')),
            f(p.get('loss_percent')), '  ERROR: ' + i['error'] if i['error'] else ''))
    p = result['ping']['idle']
    print('%-16s %7s         %-24s | ping avg %s max %s mdev %s ms, loss %s%%' % (
        'idle', '', '', f(p.get('avg_ms')), f(p.get('max_ms')), f(p.get('mdev_ms')), f(p.get('loss_percent'))))


# 802.11 5 GHz grid: 20 MHz control channels and the 40/80 MHz blocks they belong to.
GRID = list(range(5180, 5321, 20)) + list(range(5500, 5721, 20)) + list(range(5745, 5826, 20))
BLOCKS = {40: [5190, 5230, 5270, 5310, 5510, 5550, 5590, 5630, 5670, 5710, 5755, 5795],
          80: [5210, 5290, 5530, 5610, 5690, 5775]}
LINK_KEYS = re.compile(r'^radio\.1\.(freq|center\.1\.freq|chanbw|ieee_mode)$')


def center(freq, width):
    if freq not in GRID:
        return None
    if width == 20:
        return freq
    return next((c for c in BLOCKS[width] if abs(freq - c) < width / 2), None)


def dfs(freq, width):
    c = center(freq, width) or freq
    return c + width / 2 > 5250 and c - width / 2 < 5730


def fail(message):
    sys.exit('link proposal: ' + message)


def integer(name, value, low, high):
    if not (re.fullmatch(r'[0-9]+', value) and low <= int(value) <= high):
        fail('%s must be an integer in %d..%d, not %r' % (name, low, high, value))
    return int(value)


def propose(path, range_m, want_freq, width_max, power_max, revert, command):
    range_m = integer('propose.range_m', range_m, 100, 10000)
    width_max = integer('propose.width_max', width_max, 20, 80)
    if width_max not in (20, 40, 80):
        fail('propose.width_max must be 20, 40 or 80')
    want_freq = integer('propose.freq', want_freq, 5150, 5895) if want_freq else None
    power_max = integer('propose.txpower_max', power_max, 1, 30) if power_max else None
    radios = json.load(open(path))['radios']
    changes = {name: [] for name in radios}
    notes = []

    def change(name, key, value, reason):
        cfg = radios[name]['cfg']
        if key not in cfg:
            notes.append('%s: %s is not in its config; skipped (%s)' % (name, key, reason))
        elif cfg[key] != str(value):
            changes[name].append((key, cfg[key], str(value), reason))

    ap = next((n for n, r in radios.items() if r['role'] == 'ap'), None)
    noise = [r['noise'] for r in radios.values() if r['noise'] is not None]
    snr = [r['signal'] - r['noise'] for r in radios.values() if r['signal'] is not None and r['noise'] is not None]
    worst_noise, worst_snr = (max(noise) if noise else None), (min(snr) if snr else None)

    # Width and channel: AP only. 40 MHz costs 3 dB of SNR; take it only on a quiet channel with margin.
    if ap is None:
        notes.append('No radio has role ap in the profile; width and channel left alone')
    elif worst_snr is None:
        notes.append('No signal/noise reading; width and channel left alone')
    else:
        cfg = radios[ap]['cfg']
        try:
            freq, width = int(cfg['radio.1.freq']), int(cfg['radio.1.chanbw'])
        except (KeyError, ValueError):
            freq = width = None
        if width is None:
            notes.append('%s: no radio.1.freq/chanbw in its config; width and channel left alone' % ap)
        else:
            new_width = width
            if worst_noise <= -88 and worst_snr >= 32 and width_max >= 80:
                new_width = 80
            elif worst_noise <= -88 and worst_snr >= 25:
                new_width = 40
            elif worst_noise > -85 or worst_snr < 18:
                new_width = 20
            new_width = min(new_width, width_max)
            new_freq = want_freq or freq
            reason = 'noise %g dBm, worst SNR %g dB' % (worst_noise, worst_snr)
            if (new_width, new_freq) != (width, freq):
                c = center(new_freq, new_width)
                if c is None:
                    notes.append('%s: %d MHz is off the 802.11 grid for %d MHz; pick the channel in the airOS UI'
                                 % (ap, new_freq, new_width))
                elif 'radio.1.center.1.freq' in cfg and str(center(freq, width)) != cfg['radio.1.center.1.freq']:
                    notes.append('%s: its freq/center pair (%s/%s) is not on the 802.11 grid; set width and channel '
                                 'in the airOS UI' % (ap, freq, cfg['radio.1.center.1.freq']))
                else:
                    if new_freq != freq:
                        change(ap, 'radio.1.freq', new_freq, 'propose.freq from the survey')
                    change(ap, 'radio.1.center.1.freq', c, 'center of the %d MHz block' % new_width)
                    change(ap, 'radio.1.chanbw', new_width, reason)
                    if re.fullmatch(r'11acvht(20|40|80)', cfg.get('radio.1.ieee_mode', '')):
                        change(ap, 'radio.1.ieee_mode', '11acvht%d' % new_width, 'matches the width')
            else:
                notes.append('%s: keep %d MHz at %d MHz (%s)' % (ap, width, freq, reason))
            if dfs(new_freq, new_width) and cfg.get('radio.1.dfs.status') == 'enabled':
                notes.append('%s: %d MHz/%d is a DFS channel: a radar hit or the 60 s channel check drops the link; '
                             'prefer 5180-5240 or 5745-5825 if the survey shows them quiet' % (ap, new_freq, new_width))
            if freq not in GRID:
                notes.append('%s: %d MHz is not a standard 20 MHz channel; confirm it is legal for the country code'
                             % (ap, freq))

    for name, r in radios.items():
        cfg, peer = r['cfg'], radios[[n for n in radios if n != name][0]]
        # TDMA for one station: airMAX polling on, AP in PtP mode.
        change(name, 'radio.1.polling', 'enabled', 'airMAX TDMA scheduling')
        if r['role'] == 'ap':
            change(name, 'radio.1.ptpmode', '1', 'one station: PtP framing')
        # Fixed distance for a moving robot: the farthest range plus margin, so ACKs never time out.
        dist = max(range_m, math.ceil((r['distance_m'] or 0) * 1.2 / 100) * 100)
        if 'radio.1.ack.auto' in cfg or 'radio.1.ackdistance' in cfg:
            change(name, 'radio.1.ack.auto', 'disabled', 'the distance changes as the robot moves')
            old = cfg.get('radio.1.ackdistance', '')
            change(name, 'radio.1.ackdistance', dist, 'propose.range_m %d m, measured %s m' % (range_m, r['distance_m']))
            if old.isdigit() and cfg.get('radio.1.acktimeout', '').isdigit():
                # Round trip at light speed: 2 * 1 km / 300 m/us = 6.7 us per km.
                timeout = int(cfg['radio.1.acktimeout']) + math.ceil(2 * (dist - int(old)) / 300)
                change(name, 'radio.1.acktimeout', max(timeout, 1), 'round-trip time for %d m' % dist)
        # Power: what the peer hears from this radio, now and (free-space loss) at range_m.
        # -65..-50 dBm is clean; hotter distorts, weaker loses the top modulations.
        current, heard, measured = r['txpower'], peer['signal'], peer['distance_m'] or r['distance_m']
        if current is not None and heard is not None and 'radio.1.txpower' in cfg:
            far = heard - 20 * math.log10(max(range_m, measured) / measured) if measured else heard
            if not measured:
                notes.append('%s: no distance reading; power judged from the signal here only' % name)
            if cfg.get('radio.1.atpc.sta.status') == 'enabled' and r['role'] != 'ap':
                notes.append('%s: ATPC sets its power; txpower left alone' % name)
            elif far < -65:
                ceiling = power_max if power_max is not None else int(current)
                raised = min(ceiling, int(current + math.ceil(-58 - far)))
                if raised > current:
                    change(name, 'radio.1.txpower', raised, 'peer would hear %.0f dBm at %d m' % (far, range_m))
                else:
                    notes.append('%s: peer would hear %.0f dBm at %d m; raise power only with propose.txpower_max '
                                 'after an EIRP check' % (name, far, range_m))
            elif heard > -50 and int(min(heard + 50, far + 65)) >= 1:
                change(name, 'radio.1.txpower', int(current - min(heard + 50, far + 65)),
                       'peer hears %g dBm here, %.0f dBm at %d m' % (heard, far, range_m))
        # Queueing on the radio adds latency; the robot's traffic is far below the link rate.
        change(name, 'tshaper.status', 'disabled', 'traffic shaping queues packets')

    for name, items in changes.items():
        print('%s (%s):' % (name, radios[name]['role'] or '?'))
        if not items:
            print('  no change')
        for key, old, new, reason in items:
            print('  %-24s %s -> %s  (%s)' % (key, old, new, reason))
    for note in notes:
        print('note: ' + note)
    print('\nEach trial reverts in %s s unless you type KEEP; re-run measure before KEEP. '
          'Channel/width first, one radio at a time:' % revert)
    for name, items in sorted(changes.items(), key=lambda kv: radios[kv[0]]['role'] != 'ap'):
        for group in ([i for i in items if LINK_KEYS.match(i[0])], [i for i in items if not LINK_KEYS.match(i[0])]):
            if group:
                pairs = ' '.join('%s=%s' % (k, n) for k, _, n, _ in group)
                print('  ./%s plan %s %s' % (command, name, pairs))
                print('  ./%s apply %s %s' % (command, name, pairs))


if __name__ == '__main__':
    verb, args = sys.argv[1], sys.argv[2:]
    {'udp-rate': udp_rate, 'summary': summary, 'propose': propose}[verb](*args)
