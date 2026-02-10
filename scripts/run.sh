#!/bin/bash
# Build P4, launch Mininet topology, start controller
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

P4_JSON="build/adaptive_routing.json"

echo "============================================"
echo "  Adaptive Routing - BMv2 Demo"
echo "============================================"

# Step 1: Compile P4
echo ""
echo "[1/3] Compiling P4 program..."
make compile
echo "  Done."

# Step 2: Start Mininet in background
echo ""
echo "[2/3] Starting Mininet topology..."
echo "  (Topology will run in background)"
sudo python3 topology/topo.py --p4-json "$P4_JSON" --cli &
TOPO_PID=$!

# Wait for switches to come up
echo "  Waiting for switches to initialize..."
sleep 5

# Step 3: Run controller
echo ""
echo "[3/3] Running controller to populate tables..."
python3 controller/controller.py --threshold 500000

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Mininet CLI is available."
echo "  To run benchmarks: make test"
echo "  To monitor: make monitor"
echo "============================================"

# Wait for Mininet to finish
wait $TOPO_PID 2>/dev/null
