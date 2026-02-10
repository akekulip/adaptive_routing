#!/usr/bin/env python3
"""
6-switch Mininet topology with BMv2 simple_switch for adaptive routing.

Topology:
    H1 -- S1 ========= S2 -- H2
           |  \\     /   |
           |   S3-S4    |
           |  /     \\   |
    H3 -- S5 ========= S6 -- H4

Paths between S1-S2:
  - Direct: S1-S2
  - Via middle: S1-S3-S4-S2
  - Via bottom: S1-S5-S6-S2
"""

import os
import sys
import argparse
import json

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

# BMv2 switch integration — add known locations to path
# IMPORTANT: BMv2 native version first (Thrift-based), tutorials version uses gRPC
_p4_mininet_paths = [
    '/home/philip/src/behavioral-model/mininet',
]
for _p in _p4_mininet_paths:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from p4_mininet import P4Switch, P4Host


# Default link parameters
DEFAULT_BW = 10      # Mbps
DEFAULT_DELAY = '1ms'
DEFAULT_LOSS = 0      # percent


class AdaptiveRoutingTopo(Topo):
    """6-switch topology with 3 parallel paths."""

    def __init__(self, p4_json, bw=DEFAULT_BW, host_bw=None, **kwargs):
        Topo.__init__(self, **kwargs)

        self.bw = bw
        self.host_bw = host_bw or bw * 10   # host links faster than core
        self.p4_json = p4_json

        # --- Switches ---
        s1 = self.addSwitch('s1', cls=P4Switch, sw_path='simple_switch',
                            json_path=p4_json, thrift_port=9090,
                            device_id=1, log_console=False)
        s2 = self.addSwitch('s2', cls=P4Switch, sw_path='simple_switch',
                            json_path=p4_json, thrift_port=9091,
                            device_id=2, log_console=False)
        s3 = self.addSwitch('s3', cls=P4Switch, sw_path='simple_switch',
                            json_path=p4_json, thrift_port=9092,
                            device_id=3, log_console=False)
        s4 = self.addSwitch('s4', cls=P4Switch, sw_path='simple_switch',
                            json_path=p4_json, thrift_port=9093,
                            device_id=4, log_console=False)
        s5 = self.addSwitch('s5', cls=P4Switch, sw_path='simple_switch',
                            json_path=p4_json, thrift_port=9094,
                            device_id=5, log_console=False)
        s6 = self.addSwitch('s6', cls=P4Switch, sw_path='simple_switch',
                            json_path=p4_json, thrift_port=9095,
                            device_id=6, log_console=False)

        # --- Hosts ---
        h1 = self.addHost('h1', cls=P4Host, ip='10.0.1.1/24',
                          mac='00:00:00:00:01:01',
                          defaultRoute='via 10.0.1.254')
        h2 = self.addHost('h2', cls=P4Host, ip='10.0.2.1/24',
                          mac='00:00:00:00:02:01',
                          defaultRoute='via 10.0.2.254')
        h3 = self.addHost('h3', cls=P4Host, ip='10.0.5.1/24',
                          mac='00:00:00:00:05:01',
                          defaultRoute='via 10.0.5.254')
        h4 = self.addHost('h4', cls=P4Host, ip='10.0.6.1/24',
                          mac='00:00:00:00:06:01',
                          defaultRoute='via 10.0.6.254')

        sw_opts = dict(bw=bw, delay=DEFAULT_DELAY, loss=DEFAULT_LOSS,
                       cls=TCLink)
        host_opts = dict(bw=self.host_bw, delay=DEFAULT_DELAY,
                         loss=DEFAULT_LOSS, cls=TCLink)

        # --- Host links (high bandwidth so bottleneck is in the core) ---
        self.addLink(h1, s1, **host_opts)  # s1-eth1
        self.addLink(h2, s2, **host_opts)  # s2-eth1
        self.addLink(h3, s5, **host_opts)  # s5-eth1
        self.addLink(h4, s6, **host_opts)  # s6-eth1

        # --- Switch-to-switch links (10 Mbps core) ---
        # S1-S2 direct (path 1)
        self.addLink(s1, s2, **sw_opts)  # s1-eth2, s2-eth2

        # S1-S3, S3-S4, S4-S2 (path 2 via middle)
        self.addLink(s1, s3, **sw_opts)  # s1-eth3, s3-eth1
        self.addLink(s3, s4, **sw_opts)  # s3-eth2, s4-eth1
        self.addLink(s4, s2, **sw_opts)  # s4-eth2, s2-eth3

        # S1-S5, S5-S6, S6-S2 (path 3 via bottom)
        self.addLink(s1, s5, **sw_opts)  # s1-eth4, s5-eth2
        self.addLink(s5, s6, **sw_opts)  # s5-eth3, s6-eth2
        self.addLink(s6, s2, **sw_opts)  # s6-eth3, s2-eth4

        # S3-S5, S4-S6 cross links (additional connectivity)
        self.addLink(s3, s5, **sw_opts)  # s3-eth3, s5-eth4
        self.addLink(s4, s6, **sw_opts)  # s4-eth3, s6-eth4


def get_topology_graph():
    """
    Return the topology as an adjacency dict for path computation.
    Keys are switch names, values are dicts of {neighbor: (local_port, remote_port, cost)}.

    Port numbering follows Mininet addLink order:
      - Port 1 on edge switches = host port
      - Subsequent ports = switch-switch links
    """
    graph = {
        's1': {
            's2': (2, 2, 1),   # s1-eth2 <-> s2-eth2
            's3': (3, 1, 1),   # s1-eth3 <-> s3-eth1
            's5': (4, 2, 1),   # s1-eth4 <-> s5-eth2
        },
        's2': {
            's1': (2, 2, 1),   # s2-eth2 <-> s1-eth2
            's4': (3, 2, 1),   # s2-eth3 <-> s4-eth2
            's6': (4, 3, 1),   # s2-eth4 <-> s6-eth3
        },
        's3': {
            's1': (1, 3, 1),   # s3-eth1 <-> s1-eth3
            's4': (2, 1, 1),   # s3-eth2 <-> s4-eth1
            's5': (3, 4, 1),   # s3-eth3 <-> s5-eth4
        },
        's4': {
            's3': (1, 2, 1),   # s4-eth1 <-> s3-eth2
            's2': (2, 3, 1),   # s4-eth2 <-> s2-eth3
            's6': (3, 4, 1),   # s4-eth3 <-> s6-eth4
        },
        's5': {
            's1': (2, 4, 1),   # s5-eth2 <-> s1-eth4
            's6': (3, 2, 1),   # s5-eth3 <-> s6-eth2
            's3': (4, 3, 1),   # s5-eth4 <-> s3-eth3
        },
        's6': {
            's5': (2, 3, 1),   # s6-eth2 <-> s5-eth3
            's2': (3, 4, 1),   # s6-eth3 <-> s2-eth4
            's4': (4, 3, 1),   # s6-eth4 <-> s4-eth3
        },
    }
    return graph


def get_host_info():
    """
    Return host connection info: {host_name: (switch, port, ip, mac, gateway)}.
    """
    return {
        'h1': ('s1', 1, '10.0.1.1', '00:00:00:00:01:01', '10.0.1.254'),
        'h2': ('s2', 1, '10.0.2.1', '00:00:00:00:02:01', '10.0.2.254'),
        'h3': ('s5', 1, '10.0.5.1', '00:00:00:00:05:01', '10.0.5.254'),
        'h4': ('s6', 1, '10.0.6.1', '00:00:00:00:06:01', '10.0.6.254'),
    }


def get_switch_mac(switch_name, port):
    """Generate a deterministic MAC for a switch port."""
    sw_num = int(switch_name[1:])
    return f'00:00:0{sw_num}:00:00:{port:02x}'


def get_thrift_port(switch_name):
    """Return the Thrift port for a given switch."""
    sw_num = int(switch_name[1:])
    return 9089 + sw_num  # s1=9090, s2=9091, ...


def main():
    parser = argparse.ArgumentParser(description='Adaptive Routing Topology')
    parser.add_argument('--p4-json', type=str,
                        default='build/adaptive_routing.json',
                        help='Path to compiled P4 JSON')
    parser.add_argument('--bw', type=int, default=DEFAULT_BW,
                        help='Link bandwidth in Mbps')
    parser.add_argument('--no-cli', action='store_true', default=False,
                        help='Skip Mininet CLI (for scripted use)')
    args = parser.parse_args()

    p4_json = os.path.abspath(args.p4_json)
    if not os.path.exists(p4_json):
        print(f'Error: P4 JSON not found at {p4_json}')
        print('Run "make compile" first to build the P4 program.')
        sys.exit(1)

    setLogLevel('info')

    info('*** Creating topology\n')
    topo = AdaptiveRoutingTopo(p4_json=p4_json, bw=args.bw)

    info('*** Starting network\n')
    net = Mininet(topo=topo, controller=None)
    net.start()

    # Disable IPv6 on all interfaces to prevent interference
    for host in net.hosts:
        host.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        host.cmd('sysctl -w net.ipv6.conf.default.disable_ipv6=1')

    # P4Host.config() calls super(Host, self).config() which skips
    # Host.config(), so defaultRoute is never applied. Add them manually.
    host_info = get_host_info()
    for h in net.hosts:
        sw_name, port, ip, mac, gw = host_info[h.name]
        h.cmd(f'ip route add default via {gw} dev eth0')
        info(f'  {h.name}: default route via {gw}\n')

    # Set ARP entries statically (since P4 switches don't do ARP)
    for h in net.hosts:
        for other_name, (sw, port, ip, mac, gw) in host_info.items():
            if other_name != h.name:
                h.cmd(f'arp -s {ip} {mac}')
        # Also set gateway MAC (switch port MAC)
        sw_name = host_info[h.name][0]
        gw_ip = host_info[h.name][4]
        gw_mac = get_switch_mac(sw_name, host_info[h.name][1])
        h.cmd(f'arp -s {gw_ip} {gw_mac}')

    info('\n*** Network is ready. Run the controller to populate tables.\n')
    info('*** Use: python controller/controller.py\n\n')

    if not args.no_cli:
        CLI(net)

    net.stop()


if __name__ == '__main__':
    main()
