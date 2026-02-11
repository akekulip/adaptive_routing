# Adaptive Routing with P4 on BMv2

A P4-based adaptive load-balancing system that dynamically distributes traffic across multiple equal-cost paths based on real-time link utilization, improving on static ECMP behavior. Tested on a 6-switch Mininet topology with BMv2 `simple_switch` and OSPF-like shortest-path routing as the underlay.

## Results

Benchmark comparing static ECMP vs adaptive routing with 5 concurrent UDP flows (1 heavy direct flow + 4 ECMP cross-flows):

| Metric | Static ECMP | Adaptive | Change |
|--------|-------------|----------|--------|
| Total throughput | 9.71 Mbps | 16.22 Mbps | **+67.0%** |
| ECMP flow throughput | 6.18 Mbps | 9.97 Mbps | **+61.3%** |
| ECMP flow balance (CV) | 0.1613 | 0.0516 | **-68.0%** |
| ECMP Jain's fairness | 0.9747 | 0.9973 | +2.3% |

In the baseline, CRC16 hashing placed all 4 ECMP flows on the same port as a 9 Mbps direct flow, saturating the 10 Mbps link. Adaptive routing detected the overload via per-port byte counters and rerouted ECMP traffic to the alternative uncongested path.

## How It Works

Standard ECMP hashes flows onto fixed paths regardless of congestion. This project adds a data-plane feedback loop: each switch tracks per-port byte counts in P4 registers and, when a port's load exceeds a configurable threshold, reroutes traffic to an alternative equal-cost path -- all without controller involvement per packet.

```
Ingress Pipeline:

  IPv4 LPM --> ECMP Group --> 5-tuple Hash Path Selection
                                       |
                             +---------v----------+
                             | Read byte_counter  |
                             | on selected port   |
                             +---------+----------+
                                       |
                             +---------v----------+
                             | Load > threshold?  |
                             |  Yes -> alt_nhop   |
                             |  No  -> continue   |
                             +---------+----------+
                                       |
                             +---------v----------+
                             | Update byte_counter|
                             | Rewrite MACs, -TTL |
                             +--------------------+
```

## Topology

6-switch mesh with 3 parallel paths between edge pairs and 4 hosts:

```
    H1 -- S1 ----------- S2 -- H2
           |  \       /   |
           |   S3 - S4    |
           |  /       \   |
    H3 -- S5 ----------- S6 -- H4
```

| Path | Hops | Route |
|------|------|-------|
| 1 (direct) | 1 | S1 - S2 |
| 2 (middle) | 3 | S1 - S3 - S4 - S2 |
| 3 (bottom) | 3 | S1 - S5 - S6 - S2 |

- Core links: 10 Mbps, 1 ms delay
- Host links: 100 Mbps (so the bottleneck is inside the network, not at the edge)
- Cross-links S3-S5 and S4-S6 provide additional connectivity

## Project Structure

```
adaptive_routing/
├── p4/
│   ├── adaptive_routing.p4       # Core P4 program (v1model, P4_16)
│   └── includes/
│       ├── headers.p4            # Ethernet/IPv4/TCP/UDP headers, metadata
│       └── parsers.p4            # Parser, deparser, checksums
├── controller/
│   └── controller.py             # Dijkstra path computation, Thrift table population
├── topology/
│   └── topo.py                   # 6-switch Mininet topology with BMv2
├── tests/
│   ├── test_connectivity.py      # End-to-end ping verification
│   └── benchmark.py              # iperf throughput + fairness comparison
├── scripts/
│   ├── run.sh                    # Build + launch + configure
│   └── cleanup.sh                # Kill processes, clean state
├── Makefile
└── requirements.txt
```

## Prerequisites

- **BMv2** (`simple_switch`, `simple_switch_CLI`) -- [behavioral-model](https://github.com/p4lang/behavioral-model)
- **p4c** (`p4c-bm2-ss`) -- [p4c compiler](https://github.com/p4lang/p4c)
- **Mininet** with `p4_mininet` module from BMv2
- **Python 3.10+** with `psutil`
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

### 2. Start the topology (Terminal 1)

```bash
sudo python3 topology/topo.py --p4-json build/adaptive_routing.json
```

Wait for the `mininet>` prompt.

### 3. Populate forwarding tables (Terminal 2)

```bash
python3 controller/controller.py --threshold 500000
```

The controller computes shortest paths via Dijkstra, identifies ECMP groups, and installs entries on all 6 switches via the Thrift runtime API.

### 4. Verify connectivity (Terminal 1)

```
mininet> pingall
```

Expected: `0% dropped (12/12 received)`.

### 5. Run the benchmark

```bash
sudo python3 tests/benchmark.py --duration 20
```

Runs UDP flows in two modes (static ECMP vs adaptive) and reports throughput and fairness metrics.

### 6. Monitor utilization (optional)

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

### Headers Parsed

Ethernet, IPv4, TCP, UDP -- the 5-tuple (src/dst IP, protocol, src/dst L4 port) drives ECMP hash computation so different flows between the same host pair can take different paths.

### Tables

| Table | Match | Action | Purpose |
|-------|-------|--------|---------|
| `ipv4_lpm` | `dstAddr` (LPM) | `set_nhop` / `set_ecmp_group` | Route to next hop or ECMP group |
| `ecmp_group` | `ecmp_group_id` (exact) | `set_ecmp_info` | Get group size, compute CRC16 hash |
| `ecmp_nhop` | `(group_id, hash)` (exact) | `set_ecmp_nhop` | Select egress port from ECMP members |
| `alt_nhop` | `selected_port` (exact) | `set_alt_nhop` | Reroute when port is overloaded |
| `smac_rewrite` | `egress_port` (exact) | `set_smac` | Rewrite source MAC per port |

### Registers

| Register | Size | Purpose |
|----------|------|---------|
| `byte_counter` | 256 x 32-bit | Per-port byte count (read/updated per ECMP packet) |
| `load_threshold` | 1 x 32-bit | Configurable threshold (written by controller) |

## Controller

The control plane uses BMv2's Thrift runtime API (`simple_switch_CLI`) to:

1. **Compute ECMP groups** -- Dijkstra on the topology graph identifies all equal-cost shortest paths (simulating OSPF SPF computation)
2. **Populate tables** -- Installs LPM, ECMP, next-hop, and alternative next-hop entries on all 6 switches
3. **Configure threshold** -- Writes the `load_threshold` register (default: 2 MB with 2s counter-reset window)
4. **Monitor** -- Periodically reads `byte_counter` registers to display per-port utilization

## Benchmark Design

The benchmark (`tests/benchmark.py`) creates a traffic pattern that exposes the weakness of static ECMP:

**Traffic pattern:**
- H1 -> H2 at 9 Mbps UDP (non-ECMP, always uses S1->S2 direct link)
- H1 -> H4 at 3 Mbps UDP x4 flows (ECMP: hashed across S1->S2 and S1->S5 paths)

**Why this works:** The 9 Mbps direct flow nearly saturates the S1-S2 link. Any ECMP flow that hashes onto the same link faces congestion, while the S1-S5 alternative path sits idle. Adaptive routing detects the overload and shifts ECMP flows to the uncongested path.

| Scenario | Threshold | Counter reset | Behavior |
|----------|-----------|---------------|----------|
| Static ECMP (baseline) | 2^31 (infinite) | Off | Pure hash-based distribution |
| Adaptive routing | 2 MB | Every 2s | Reroutes when port load exceeds threshold |

**Metrics:**
- Per-flow received throughput (UDP receiver-side, reflects actual delivery)
- Aggregate and ECMP-only throughput
- Jain's fairness index and coefficient of variation
- S1 port utilization balance

## Makefile Targets

| Target | Description |
|--------|-------------|
| `make compile` | Compile P4 to BMv2 JSON |
| `make run` | Start Mininet + BMv2 switches |
| `make controller` | Populate tables (run in separate terminal) |
| `make monitor` | Live utilization display |
| `make test` | Run benchmark suite |
| `make clean` | Remove build artifacts |

## Automated Test

Run the full integration test (starts topology, populates tables, pings, opens CLI):

```bash
sudo python3 tests/test_connectivity.py
```

## License

MIT
