#!/bin/bash
# Kill processes and clean up after adaptive routing demo
set -e

echo "Cleaning up adaptive routing environment..."

# Kill any running simple_switch instances
echo "  Stopping BMv2 switches..."
sudo pkill -f simple_switch 2>/dev/null || true

# Kill any iperf3 servers/clients
echo "  Stopping iperf3 processes..."
sudo pkill -f iperf3 2>/dev/null || true

# Stop Mininet and clean up
echo "  Cleaning Mininet..."
sudo mn -c 2>/dev/null || true

# Remove any leftover log files
echo "  Removing logs..."
rm -f /tmp/p4s.*.log 2>/dev/null || true

echo "Cleanup complete."
