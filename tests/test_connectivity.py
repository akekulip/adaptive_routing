#!/usr/bin/env python3
"""
End-to-end connectivity test.
Starts topology, populates tables, runs ping tests, then opens CLI.
Must be run as root.
"""

import os
import sys
import time

# Add project paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'topology'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'controller'))

from mininet.net import Mininet
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

from topo import AdaptiveRoutingTopo, get_host_info, get_switch_mac
from controller import (populate_switch, set_threshold, get_topology_graph,
                         SwitchController, get_thrift_port)


def main():
    setLogLevel('info')

    p4_json = os.path.abspath('build/adaptive_routing.json')
    if not os.path.exists(p4_json):
        print(f'Error: {p4_json} not found. Run "make compile" first.')
        sys.exit(1)

    # --- Start topology ---
    info('*** Creating topology\n')
    topo = AdaptiveRoutingTopo(p4_json=p4_json, bw=10)
    net = Mininet(topo=topo, controller=None)
    net.start()

    # Disable IPv6
    for host in net.hosts:
        host.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        host.cmd('sysctl -w net.ipv6.conf.default.disable_ipv6=1')

    # P4Host skips Host.config(), so default routes aren't applied. Add manually.
    host_info = get_host_info()
    for h in net.hosts:
        sw_name, port, ip, mac, gw = host_info[h.name]
        h.cmd(f'ip route add default via {gw} dev eth0')
        info(f'  {h.name}: default route via {gw}\n')

    # Set static ARP entries
    for h in net.hosts:
        for other_name, (sw, port, ip, mac, gw) in host_info.items():
            if other_name != h.name:
                h.cmd(f'arp -s {ip} {mac}')
        sw_name = host_info[h.name][0]
        gw_ip = host_info[h.name][4]
        gw_mac = get_switch_mac(sw_name, host_info[h.name][1])
        h.cmd(f'arp -s {gw_ip} {gw_mac}')

    info('*** Waiting for switches to initialize...\n')
    time.sleep(2)

    # --- Populate tables ---
    info('\n*** Populating forwarding tables...\n')
    graph = get_topology_graph()
    controllers = {}
    for sw in ['s1', 's2', 's3', 's4', 's5', 's6']:
        ctrl = populate_switch(sw, graph, host_info)
        controllers[sw] = ctrl

    set_threshold(controllers, 500000)

    # --- Verify table entries on S1 ---
    info('\n*** Verifying S1 table entries...\n')
    ctrl_s1 = SwitchController(get_thrift_port('s1'))
    result = ctrl_s1._run_cmd('table_dump ipv4_lpm')
    info(f'S1 ipv4_lpm:\n{result}\n')

    # --- Debug: check host network config ---
    info('\n*** Host network configuration:\n')
    for h in net.hosts:
        info(f'\n--- {h.name} ---\n')
        info(h.cmd('ip addr show') + '\n')
        info(h.cmd('ip route show') + '\n')
        info(h.cmd('arp -n') + '\n')

    # --- Run ping tests ---
    info('\n*** Running ping tests...\n')
    time.sleep(1)

    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

    # Test H1 -> H2 with verbose output
    info('\n--- H1 -> H2 ping ---\n')
    result = h1.cmd('ping -c 3 -W 2 10.0.2.1')
    info(result + '\n')

    # Test H1 -> H3
    info('--- H1 -> H3 ping ---\n')
    result = h1.cmd('ping -c 3 -W 2 10.0.5.1')
    info(result + '\n')

    # Test H3 -> H4
    info('--- H3 -> H4 ping ---\n')
    result = h3.cmd('ping -c 3 -W 2 10.0.6.1')
    info(result + '\n')

    # Full pingall
    info('\n*** Full pingall:\n')
    net.pingAll()

    # --- Open CLI for interactive debugging ---
    info('\n*** Opening Mininet CLI (type "exit" to quit)\n')
    CLI(net)

    net.stop()


if __name__ == '__main__':
    main()
