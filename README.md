<<<<<<< HEAD
# Adaptive Routing with P4 on BMv2

A P4-based adaptive load-balancing system that dynamically distributes traffic across multiple equal-cost paths based on real-time link utilization, improving on static ECMP behavior. Tested on a 6-switch Mininet topology with BMv2 `simple_switch`.

## How It Works

Standard ECMP hashes flows onto fixed paths regardless of congestion. This project adds a data-plane feedback loop: each switch tracks per-port byte counts in P4 registers and, when a port's load exceeds a configurable threshold, reroutes new traffic to an alternative equal-cost path — all without controller involvement per packet.

```
Ingress Pipeline:
  IPv4 LPM → ECMP Group → Hash-based Path Selection
                                    │
                          ┌─────────▼──────────┐
                          │ Read byte_counter   │
                          │ on selected port    │
                          └─────────┬──────────┘
                                    │
                          ┌─────────▼──────────┐
                          │ Load > threshold?   │
                          │   Yes → alt_nhop    │
                          │   No  → continue    │
                          └─────────┬──────────┘
                                    │
                          ┌─────────▼──────────┐
                          │ Update byte_counter │
                          │ Rewrite MACs, ↓TTL  │
                          └────────────────────┘
```

## Topology

6-switch mesh with 3 parallel paths between edge pairs and 4 hosts:

```
    H1 -- S1 ─────────── S2 -- H2
           │ \         / │
           │  S3 ── S4   │
           │ /         \ │
    H3 -- S5 ─────────── S6 -- H4
```

| Path | Hops | Route |
|------|------|-------|
| 1 (direct) | 1 | S1 → S2 |
| 2 (middle) | 3 | S1 → S3 → S4 → S2 |
| 3 (bottom) | 3 | S1 → S5 → S6 → S2 |

All links: 10 Mbps, 1 ms delay. Cross-links S3-S5 and S4-S6 provide additional connectivity.

## Project Structure

```
adaptive_routing/
├── p4/
│   ├── adaptive_routing.p4       # Core P4 program (v1model, P4_16)
│   └── includes/
│       ├── headers.p4            # Ethernet/IPv4 headers, metadata
│       └── parsers.p4            # Parser, deparser, checksums
├── controller/
│   └── controller.py             # Dijkstra path computation, Thrift table population
├── topology/
│   └── topo.py                   # 6-switch Mininet topology with BMv2
├── tests/
│   ├── test_connectivity.py      # End-to-end ping verification
│   └── benchmark.py              # iperf throughput + Jain's fairness comparison
├── scripts/
│   ├── run.sh                    # Build + launch + configure
│   └── cleanup.sh                # Kill processes, clean state
├── Makefile
└── requirements.txt
```

## Prerequisites

- **BMv2** (`simple_switch`, `simple_switch_CLI`) — [behavioral-model](https://github.com/p4lang/behavioral-model)
- **p4c** (`p4c-bm2-ss`) — [p4c compiler](https://github.com/p4lang/p4c)
- **Mininet** with `p4_mininet` module from BMv2
- **Python 3.10+** with `scapy`, `psutil`
- **iperf3** (for benchmarks)

On Ubuntu:
```bash
sudo apt install mininet iperf3 python3-psutil
pip install scapy networkx
```

## Quick Start

### 1. Compile the P4 program

```bash
cd adaptive_routing
make compile
```

Produces `build/adaptive_routing.json`.

### 2. Start the topology (Terminal 1)

```bash
sudo python3 topology/topo.py --p4-json build/adaptive_routing.json
```

Wait for the `mininet>` prompt.

### 3. Populate forwarding tables (Terminal 2)

```bash
python3 controller/controller.py --threshold 500000
```

The controller computes shortest paths via Dijkstra, identifies ECMP groups, and installs entries on all 6 switches via the Thrift API.

### 4. Verify connectivity (Terminal 1)

```
mininet> pingall
```

Expected: `0% dropped (12/12 received)`.

### 5. Run the benchmark (Terminal 2)

```bash
sudo python3 tests/benchmark.py --duration 15
```

Runs 3 parallel iperf flows in two modes (static ECMP vs. adaptive) and reports throughput and Jain's fairness index.

### 6. Monitor utilization (optional, Terminal 2)

```bash
python3 controller/controller.py --monitor --monitor-interval 5
```

### 7. Cleanup

```
mininet> exit
```
```bash
bash scripts/cleanup.sh
```

## P4 Data Plane

### Tables

| Table | Match | Action | Purpose |
|-------|-------|--------|---------|
| `ipv4_lpm` | `dstAddr` (LPM) | `set_nhop` / `set_ecmp_group` | Route to next hop or ECMP group |
| `ecmp_group` | `ecmp_group_id` (exact) | `set_ecmp_info` | Get group size, compute hash |
| `ecmp_nhop` | `(group_id, hash)` (exact) | `set_ecmp_nhop` | Select egress port from ECMP members |
| `alt_nhop` | `selected_port` (exact) | `set_alt_nhop` | Reroute when port is overloaded |
| `smac_rewrite` | `egress_port` (exact) | `set_smac` | Rewrite source MAC per port |

### Registers

| Register | Size | Purpose |
|----------|------|---------|
| `byte_counter` | 256 entries, 32-bit | Per-port cumulative byte count |
| `load_threshold` | 1 entry, 32-bit | Configurable threshold (set by controller) |

## Controller

The control plane uses BMv2's Thrift runtime API (`simple_switch_CLI`) to:

1. **Compute ECMP groups** — Dijkstra on the topology graph identifies all equal-cost shortest paths between every switch pair
2. **Populate tables** — Installs LPM, ECMP, next-hop, and alternative next-hop entries on all 6 switches
3. **Configure threshold** — Writes the `load_threshold` register (default: 500 KB)
4. **Monitor** — Periodically reads `byte_counter` registers to display per-port utilization

## Benchmark

The benchmark (`tests/benchmark.py`) runs two scenarios:

| Scenario | Threshold | Behavior |
|----------|-----------|----------|
| Static ECMP (baseline) | 2^31 (effectively infinite) | Hash-based path selection only |
| Adaptive routing | 500 KB (configurable) | Reroutes when port load exceeds threshold |

**Flows**: H1→H2, H3→H4, H1→H4 (15 seconds each, parallel)

**Metrics**:
- Per-flow throughput (Mbps)
- Aggregate throughput
- [Jain's fairness index](https://en.wikipedia.org/wiki/Jain%27s_fairness_index): measures throughput balance across flows (1.0 = perfectly fair)

## Makefile Targets

| Target | Command | Description |
|--------|---------|-------------|
| `make compile` | `p4c-bm2-ss` | Compile P4 to BMv2 JSON |
| `make run` | `sudo python3 topology/topo.py` | Start Mininet + BMv2 switches |
| `make controller` | `python3 controller/controller.py` | Populate tables |
| `make monitor` | `python3 controller/controller.py --monitor` | Live utilization display |
| `make test` | `sudo python3 tests/benchmark.py` | Run benchmark suite |
| `make clean` | `rm -rf build/` | Remove build artifacts |

## Automated Test

Run the full integration test (starts topology, populates tables, pings, opens CLI):

```bash
sudo python3 tests/test_connectivity.py
```

## License
# adaptive_routing
>>>>>>> 16f2364eed566a324b5d3c3b2c7665f047055a3
