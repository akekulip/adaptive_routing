#!/usr/bin/env python3
"""
Benchmark: Static ECMP vs Adaptive Routing.

Runs inside Mininet. Creates targeted traffic to overload one ECMP path
while leaving the alternative uncongested. Measures how adaptive rerouting
improves total throughput and path utilization balance.

Traffic pattern (one-sided to isolate the effect):
  - H1 -> H2  UDP 9 Mbps   (non-ECMP, always S1->S2 direct)
  - H1 -> H4  UDP 3 Mbps x4 (ECMP on S1: port 2 via S2, port 4 via S5)

Baseline:  hash distributes ECMP flows; some land on congested S1->S2 link.
Adaptive:  S1 port 2 exceeds threshold -> ECMP flows reroute to port 4.

Must be run as root:  sudo python3 tests/benchmark.py
"""

import os
import sys
import time
import json
import threading
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'topology'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))

from mininet.net import Mininet
from mininet.link import TCLink
from mininet.log import setLogLevel, info

from topo import AdaptiveRoutingTopo, get_host_info, get_switch_mac, get_thrift_port
from controller import (populate_switch, set_threshold, get_topology_graph,
                         SwitchController)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# (src, dst, iperf_port, udp_bw_mbps)
FLOW_SPECS = [
    ('h1', 'h2', 5201, 9),   # heavy flow, always S1 port 2 (direct)
    ('h1', 'h4', 5202, 3),   # ECMP on S1
    ('h1', 'h4', 5203, 3),   # ECMP on S1 (different 5-tuple)
    ('h1', 'h4', 5204, 3),   # ECMP on S1
    ('h1', 'h4', 5205, 3),   # ECMP on S1
]

HOST_IPS = {
    'h1': '10.0.1.1',  'h2': '10.0.2.1',
    'h3': '10.0.5.1',  'h4': '10.0.6.1',
}

BASELINE_THRESHOLD = 2**31 - 1
ADAPTIVE_THRESHOLD = 2000000      # 2 MB — triggers at ~8+ Mbps in 2s window
COUNTER_RESET_SEC  = 2
TEST_DURATION      = 20

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def jains_fairness(vals):
    if not vals or all(v == 0 for v in vals):
        return 0.0
    n, s, s2 = len(vals), sum(vals), sum(v*v for v in vals)
    return (s*s) / (n*s2) if s2 else 0.0


def coeff_var(vals):
    if not vals or all(v == 0 for v in vals):
        return 0.0
    n = len(vals)
    mean = sum(vals) / n
    if mean == 0:
        return 0.0
    return (sum((v-mean)**2 for v in vals) / n) ** 0.5 / mean


def set_all_thresholds(threshold):
    for i in range(1, 7):
        SwitchController(get_thrift_port(f's{i}')).register_write(
            'load_threshold', 0, threshold)


def reset_all_counters():
    for i in range(1, 7):
        SwitchController(get_thrift_port(f's{i}')).register_reset(
            'byte_counter')


def read_s1_port_counters():
    """Read S1 port 2 (direct) and port 4 (via S5) counters."""
    ctrl = SwitchController(get_thrift_port('s1'))
    return {
        'port2_direct': ctrl.register_read('byte_counter', 2),
        'port4_via_s5': ctrl.register_read('byte_counter', 4),
    }


class PeriodicReset:
    def __init__(self, interval):
        self.interval = interval
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        self._t.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self.interval):
            reset_all_counters()


def parse_iperf_json(raw):
    try:
        d = json.loads(raw)
        return d['end']['sum']['bits_per_second'] / 1e6
    except Exception:
        pass
    try:
        d = json.loads(raw)
        return d['end']['sum_received']['bits_per_second'] / 1e6
    except Exception:
        return 0.0

# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

def run_flows(net, flows, duration):
    # Start servers
    for src, dst, port, bw in flows:
        net.get(dst).cmd(f'iperf3 -s -p {port} -D --one-off')
    time.sleep(1)

    # Start clients (UDP)
    procs = []
    for src, dst, port, bw in flows:
        h = net.get(src)
        dst_ip = HOST_IPS[dst]
        p = h.popen(f'iperf3 -c {dst_ip} -p {port} -u -b {bw}M '
                     f'-t {duration} -J'.split())
        procs.append((src, dst, port, bw, p))

    # Collect
    results = []
    for src, dst, port, bw, p in procs:
        out, _ = p.communicate(timeout=duration + 30)
        tp = parse_iperf_json(out.decode())
        results.append((src, dst, port, bw, tp))

    # Cleanup servers
    for src, dst, port, bw in flows:
        net.get(dst).cmd('kill %% 2>/dev/null; true')

    return results


def run_scenario(net, name, threshold, flows, duration, reset_counters):
    hdr = f'  Scenario: {name}'
    print(f'\n{"=" * 64}')
    print(hdr)
    print(f'  Threshold: {threshold:,}B | Duration: {duration}s | '
          f'Counter reset: {"every "+str(COUNTER_RESET_SEC)+"s" if reset_counters else "off"}')
    print(f'{"=" * 64}')

    set_all_thresholds(threshold)
    reset_all_counters()
    time.sleep(1)

    resetter = None
    if reset_counters:
        resetter = PeriodicReset(COUNTER_RESET_SEC)
        resetter.start()

    results = run_flows(net, flows, duration)

    if resetter:
        resetter.stop()

    counters = read_s1_port_counters()

    # Separate heavy (non-ECMP) and ECMP flows
    heavy = [(s,d,p,bw,tp) for s,d,p,bw,tp in results if d == 'h2']
    ecmp  = [(s,d,p,bw,tp) for s,d,p,bw,tp in results if d == 'h4']

    heavy_tp = [tp for *_,tp in heavy]
    ecmp_tp  = [tp for *_,tp in ecmp]
    all_tp   = [tp for *_,tp in results]

    total = sum(all_tp)
    ecmp_total = sum(ecmp_tp)
    ecmp_jfi = jains_fairness(ecmp_tp)
    ecmp_cv  = coeff_var(ecmp_tp)
    all_jfi  = jains_fairness(all_tp)

    print(f'\n  Per-flow received throughput:')
    for s, d, p, bw, tp in results:
        tag = '(direct)' if d == 'h2' else '(ECMP)  '
        lost = max(0, bw - tp)
        print(f'    {s}->{d} :{p}  sent {bw}M  recv {tp:5.2f}M  '
              f'loss {lost:4.2f}M  {tag}')

    p2 = counters['port2_direct']
    p4 = counters['port4_via_s5']
    ptotal = p2 + p4 if (p2+p4) > 0 else 1
    print(f'\n  S1 path utilisation:')
    print(f'    port 2 (S1->S2 direct) : {p2:>12,} bytes  '
          f'({p2/ptotal*100:4.1f}%)')
    print(f'    port 4 (S1->S5 alt)    : {p4:>12,} bytes  '
          f'({p4/ptotal*100:4.1f}%)')

    print(f'\n  Aggregate metrics:')
    print(f'    Total throughput       : {total:.2f} Mbps')
    print(f'    ECMP throughput        : {ecmp_total:.2f} Mbps')
    print(f'    ECMP Jain\'s fairness   : {ecmp_jfi:.4f}')
    print(f'    ECMP coeff of var      : {ecmp_cv:.4f}')
    print(f'    Overall Jain\'s fairness: {all_jfi:.4f}')

    return dict(name=name, results=results, heavy_tp=heavy_tp,
                ecmp_tp=ecmp_tp, all_tp=all_tp, total=total,
                ecmp_total=ecmp_total, ecmp_jfi=ecmp_jfi,
                ecmp_cv=ecmp_cv, all_jfi=all_jfi, counters=counters)


# ---------------------------------------------------------------------------
# Network setup
# ---------------------------------------------------------------------------

def build_network():
    p4_json = os.path.abspath('build/adaptive_routing.json')
    if not os.path.exists(p4_json):
        print(f'Error: {p4_json} not found. Run "make compile" first.')
        sys.exit(1)

    setLogLevel('warning')
    topo = AdaptiveRoutingTopo(p4_json=p4_json, bw=10)
    net = Mininet(topo=topo, controller=None)
    net.start()

    host_info = get_host_info()
    for h in net.hosts:
        h.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        h.cmd('sysctl -w net.ipv6.conf.default.disable_ipv6=1')
    for h in net.hosts:
        _, _, _, _, gw = host_info[h.name]
        h.cmd(f'ip route add default via {gw} dev eth0')
    for h in net.hosts:
        for other, (sw, port, ip, mac, gw) in host_info.items():
            if other != h.name:
                h.cmd(f'arp -s {ip} {mac}')
        sw_name = host_info[h.name][0]
        gw_ip = host_info[h.name][4]
        gw_mac = get_switch_mac(sw_name, host_info[h.name][1])
        h.cmd(f'arp -s {gw_ip} {gw_mac}')

    time.sleep(2)
    graph = get_topology_graph()
    for sw in ['s1', 's2', 's3', 's4', 's5', 's6']:
        populate_switch(sw, graph, host_info)

    return net


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--duration', type=int, default=TEST_DURATION)
    ap.add_argument('--threshold', type=int, default=ADAPTIVE_THRESHOLD)
    ap.add_argument('--skip-baseline', action='store_true')
    ap.add_argument('--skip-adaptive', action='store_true')
    args = ap.parse_args()

    print('\n' + '=' * 64)
    print('  P4 Adaptive Routing — Throughput Balance Benchmark')
    print('  Static ECMP  vs  Adaptive Load-Balancing')
    print('=' * 64)

    net = build_network()

    print('\nVerifying connectivity … ', end='', flush=True)
    loss = net.pingAll(timeout='1')
    print(f'{"OK" if loss == 0 else f"{loss}% loss"}')

    runs = []
    try:
        if not args.skip_baseline:
            runs.append(run_scenario(
                net, 'Static ECMP (Baseline)', BASELINE_THRESHOLD,
                FLOW_SPECS, args.duration, reset_counters=False))
            time.sleep(3)

        if not args.skip_adaptive:
            runs.append(run_scenario(
                net, 'Adaptive Routing', args.threshold,
                FLOW_SPECS, args.duration, reset_counters=True))

        if len(runs) == 2:
            b, a = runs
            print(f'\n{"=" * 64}')
            print('  COMPARISON')
            print(f'{"=" * 64}')

            def pct(old, new):
                return ((new-old)/old*100) if old else 0

            print(f'\n  {"Metric":<34} {"Baseline":>9} {"Adaptive":>9} '
                  f'{"Change":>9}')
            print(f'  {"-"*61}')

            rows = [
                ('Total throughput (Mbps)',     b['total'],      a['total'],     '.2f'),
                ('ECMP throughput (Mbps)',       b['ecmp_total'], a['ecmp_total'],'.2f'),
                ('ECMP Jain\'s fairness',       b['ecmp_jfi'],   a['ecmp_jfi'], '.4f'),
                ('ECMP coeff of variation',     b['ecmp_cv'],    a['ecmp_cv'],  '.4f'),
            ]
            for label, bv, av, fmt in rows:
                ch = pct(bv, av)
                print(f'  {label:<34} {bv:>9{fmt}} {av:>9{fmt}} '
                      f'{ch:>+8.1f}%')

            # Path utilisation balance
            bp2 = b['counters']['port2_direct']
            bp4 = b['counters']['port4_via_s5']
            ap2 = a['counters']['port2_direct']
            ap4 = a['counters']['port4_via_s5']

            def path_balance(p2, p4):
                if max(p2,p4) == 0:
                    return 0
                return min(p2,p4) / max(p2,p4)

            bb = path_balance(bp2, bp4)
            ab = path_balance(ap2, ap4)
            print(f'\n  S1 path balance (min/max):')
            print(f'    Baseline : {bb:.3f}  '
                  f'(port2={bp2:,}, port4={bp4:,})')
            print(f'    Adaptive : {ab:.3f}  '
                  f'(port2={ap2:,}, port4={ap4:,})')
            if bb > 0:
                improvement = pct(bb, ab)
                print(f'    Improvement: {improvement:+.1f}%')

            # Throughput balance (CV-based)
            if b['ecmp_cv'] > 0:
                cv_improve = (b['ecmp_cv'] - a['ecmp_cv']) / b['ecmp_cv'] * 100
                print(f'\n  Throughput balance improvement: '
                      f'{cv_improve:.1f}% '
                      f'(ECMP CV: {b["ecmp_cv"]:.4f} -> {a["ecmp_cv"]:.4f})')
            else:
                # baseline already perfectly balanced
                print(f'\n  Baseline already balanced (CV=0); '
                      f'adaptive CV={a["ecmp_cv"]:.4f}')

            print()

    finally:
        net.stop()

    print('Benchmark complete.')


if __name__ == '__main__':
    main()
