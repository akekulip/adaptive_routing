#!/usr/bin/env python3
"""
Control plane for adaptive routing on BMv2.

Responsibilities:
  1. Compute shortest paths (Dijkstra) on the topology graph
  2. Identify ECMP groups for multi-path destinations
  3. Populate ipv4_lpm, ecmp_group, ecmp_nhop, alt_nhop tables via Thrift
  4. Set load_threshold register
  5. Periodic monitoring: read byte_counter registers, log utilization
"""

import sys
import os
import time
import argparse
import heapq
import subprocess
import signal
from collections import defaultdict

# Add topology module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'topology'))
from topo import get_topology_graph, get_host_info, get_switch_mac, get_thrift_port


# ---------- Path Computation (Dijkstra + ECMP) ----------

def dijkstra_all_paths(graph, source):
    """
    Compute shortest paths from source to all other nodes.
    Returns dict of {dest: [list of equal-cost paths]}.
    Each path is a list of switch names.
    """
    dist = {source: 0}
    paths = {source: [[source]]}
    pq = [(0, source)]
    visited = set()

    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)

        for neighbor, (_, _, cost) in graph[u].items():
            new_dist = d + cost
            if neighbor not in dist or new_dist < dist[neighbor]:
                dist[neighbor] = new_dist
                paths[neighbor] = [p + [neighbor] for p in paths[u]]
                heapq.heappush(pq, (new_dist, neighbor))
            elif new_dist == dist[neighbor]:
                paths[neighbor].extend([p + [neighbor] for p in paths[u]])

    return paths


def compute_next_hops(graph, source, paths):
    """
    For each destination, determine the set of (next_hop_switch, egress_port, next_hop_port).
    Returns {dest_switch: [(egress_port, next_hop_mac)]}.
    """
    next_hops = {}
    for dest, path_list in paths.items():
        if dest == source:
            continue
        hops = set()
        for path in path_list:
            next_sw = path[1]  # first hop after source
            local_port, remote_port, _ = graph[source][next_sw]
            # MAC of the next switch's receiving port
            next_mac = get_switch_mac(next_sw, remote_port)
            hops.add((local_port, next_mac))
        next_hops[dest] = list(hops)
    return next_hops


# ---------- Thrift CLI Interface ----------

class SwitchController:
    """Interface to BMv2 simple_switch via simple_switch_CLI (Thrift)."""

    def __init__(self, thrift_port):
        self.thrift_port = thrift_port

    def _run_cmd(self, command):
        """Send a command to simple_switch_CLI."""
        cmd = f'echo "{command}" | simple_switch_CLI --thrift-port {self.thrift_port}'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                timeout=10)
        return result.stdout

    def table_add(self, table, action, match, params):
        """Add a table entry."""
        match_str = ' '.join(str(m) for m in match)
        params_str = ' '.join(str(p) for p in params)
        cmd = f'table_add {table} {action} {match_str} => {params_str}'
        return self._run_cmd(cmd)

    def table_clear(self, table):
        """Clear all entries from a table."""
        return self._run_cmd(f'table_clear {table}')

    def register_write(self, register, index, value):
        """Write a register value."""
        return self._run_cmd(f'register_write {register} {index} {value}')

    def register_read(self, register, index):
        """Read a register value."""
        output = self._run_cmd(f'register_read {register} {index}')
        # Parse output: "register_name[index] = value"
        for line in output.split('\n'):
            if '=' in line and register in line:
                try:
                    return int(line.split('=')[-1].strip())
                except ValueError:
                    pass
        return 0

    def register_reset(self, register):
        """Reset all entries in a register to 0."""
        return self._run_cmd(f'register_reset {register}')


# ---------- Table Population ----------

# Subnet assignments: each host gets /24 subnet on its edge switch
HOST_SUBNETS = {
    'h1': ('10.0.1.0/24', '10.0.1.1'),
    'h2': ('10.0.2.0/24', '10.0.2.1'),
    'h3': ('10.0.5.0/24', '10.0.5.1'),
    'h4': ('10.0.6.0/24', '10.0.6.1'),
}

# Map host subnet to edge switch
SUBNET_TO_SWITCH = {
    '10.0.1.0/24': 's1',
    '10.0.2.0/24': 's2',
    '10.0.5.0/24': 's5',
    '10.0.6.0/24': 's6',
}


def populate_switch(switch_name, graph, host_info):
    """Populate all tables for a single switch."""
    thrift_port = get_thrift_port(switch_name)
    ctrl = SwitchController(thrift_port)

    print(f'[{switch_name}] Clearing tables...')
    for table in ['ipv4_lpm', 'ecmp_group', 'ecmp_nhop', 'alt_nhop', 'smac_rewrite']:
        ctrl.table_clear(table)

    # Compute paths from this switch
    paths = dijkstra_all_paths(graph, switch_name)
    next_hops = compute_next_hops(graph, switch_name, paths)

    ecmp_group_id = 1  # start from 1

    print(f'[{switch_name}] Installing forwarding rules...')

    for subnet, dest_switch in SUBNET_TO_SWITCH.items():
        if dest_switch == switch_name:
            # Local subnet — forward directly to host port
            host_port = None
            host_mac = None
            for hname, (hsw, hport, hip, hmac, hgw) in host_info.items():
                if hsw == switch_name:
                    host_port = hport
                    host_mac = hmac
                    break

            if host_port is not None:
                ctrl.table_add('ipv4_lpm', 'set_nhop',
                               [subnet], [host_mac, host_port])
                print(f'  LPM: {subnet} -> port {host_port} (local host)')
        else:
            # Remote subnet — check for ECMP
            if dest_switch not in next_hops:
                print(f'  WARNING: no path from {switch_name} to {dest_switch}')
                continue

            hops = next_hops[dest_switch]

            if len(hops) == 1:
                # Single path — direct next hop
                port, mac = hops[0]
                ctrl.table_add('ipv4_lpm', 'set_nhop',
                               [subnet], [mac, port])
                print(f'  LPM: {subnet} -> port {port} (single path)')
            else:
                # Multiple equal-cost paths — ECMP
                gid = ecmp_group_id
                ecmp_group_id += 1
                count = len(hops)

                ctrl.table_add('ipv4_lpm', 'set_ecmp_group',
                               [subnet], [gid])
                ctrl.table_add('ecmp_group', 'set_ecmp_info',
                               [gid], [count, 0])

                print(f'  LPM: {subnet} -> ECMP group {gid} ({count} paths)')

                for idx, (port, mac) in enumerate(hops):
                    ctrl.table_add('ecmp_nhop', 'set_ecmp_nhop',
                                   [gid, idx], [mac, port])
                    print(f'    ECMP[{gid}][{idx}]: port {port}, mac {mac}')

                # Set up alternative next hops for adaptive rerouting
                # For each ECMP member, the alt is the next member (round-robin)
                for idx, (port, mac) in enumerate(hops):
                    alt_idx = (idx + 1) % count
                    alt_port, alt_mac = hops[alt_idx]
                    ctrl.table_add('alt_nhop', 'set_alt_nhop',
                                   [port], [alt_mac, alt_port])
                    print(f'    ALT: port {port} -> alt port {alt_port}')

    # Install smac_rewrite entries for all ports
    for neighbor, (local_port, _, _) in graph[switch_name].items():
        smac = get_switch_mac(switch_name, local_port)
        ctrl.table_add('smac_rewrite', 'set_smac',
                       [local_port], [smac])

    # Also install smac for host port (port 1 on edge switches)
    if switch_name in ['s1', 's2', 's5', 's6']:
        smac = get_switch_mac(switch_name, 1)
        ctrl.table_add('smac_rewrite', 'set_smac', [1], [smac])

    return ctrl


def set_threshold(controllers, threshold_bytes):
    """Set the load threshold on all switches."""
    for sw_name, ctrl in controllers.items():
        ctrl.register_write('load_threshold', 0, threshold_bytes)
        print(f'[{sw_name}] Load threshold set to {threshold_bytes} bytes')


def monitor_utilization(controllers, interval=5, reset=True):
    """Periodically read and display byte counters."""
    graph = get_topology_graph()

    print(f'\n--- Monitoring utilization (every {interval}s) ---')
    print('Press Ctrl+C to stop.\n')

    try:
        while True:
            print(f'\n[{time.strftime("%H:%M:%S")}] Port byte counters:')
            for sw_name, ctrl in sorted(controllers.items()):
                ports = [info[0] for info in graph.get(sw_name, {}).values()]
                # Include host port for edge switches
                if sw_name in ['s1', 's2', 's5', 's6']:
                    ports = [1] + ports
                counts = {}
                for port in sorted(set(ports)):
                    val = ctrl.register_read('byte_counter', port)
                    counts[port] = val
                port_str = ', '.join(f'p{p}={v}' for p, v in sorted(counts.items()))
                print(f'  {sw_name}: {port_str}')

            if reset:
                for ctrl in controllers.values():
                    ctrl.register_reset('byte_counter')

            time.sleep(interval)
    except KeyboardInterrupt:
        print('\nMonitoring stopped.')


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description='Adaptive Routing Controller')
    parser.add_argument('--threshold', type=int, default=500000,
                        help='Load threshold in bytes (default: 500000 = 500KB)')
    parser.add_argument('--monitor', action='store_true',
                        help='Enable periodic utilization monitoring')
    parser.add_argument('--monitor-interval', type=int, default=5,
                        help='Monitoring interval in seconds')
    parser.add_argument('--no-reset', action='store_true',
                        help='Do not reset counters after each monitoring read')
    args = parser.parse_args()

    graph = get_topology_graph()
    host_info = get_host_info()

    print('=' * 60)
    print('Adaptive Routing Controller')
    print('=' * 60)

    # Populate tables on all switches
    controllers = {}
    all_switches = ['s1', 's2', 's3', 's4', 's5', 's6']

    for sw in all_switches:
        ctrl = populate_switch(sw, graph, host_info)
        controllers[sw] = ctrl

    # Set load threshold
    set_threshold(controllers, args.threshold)

    print('\n' + '=' * 60)
    print('All tables populated. Network is ready.')
    print('=' * 60)

    # Start monitoring if requested
    if args.monitor:
        monitor_utilization(controllers, interval=args.monitor_interval,
                            reset=not args.no_reset)
    else:
        print('\nRun with --monitor to watch utilization.')
        print('Or start the benchmark: python tests/benchmark.py')


if __name__ == '__main__':
    main()
