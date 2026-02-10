P4C = p4c-bm2-ss
P4_SRC = p4/adaptive_routing.p4
P4_JSON = build/adaptive_routing.json
BUILD_DIR = build

.PHONY: all compile run test clean controller monitor

all: compile

# Compile P4 program to BMv2 JSON
compile: $(P4_JSON)

$(P4_JSON): $(P4_SRC) p4/includes/headers.p4 p4/includes/parsers.p4
	@mkdir -p $(BUILD_DIR)
	$(P4C) --p4v 16 -o $(P4_JSON) $(P4_SRC)
	@echo "Compiled: $(P4_JSON)"

# Start Mininet topology with BMv2 switches
run: $(P4_JSON)
	sudo python3 topology/topo.py --p4-json $(P4_JSON)

# Run the controller to populate tables
controller:
	python3 controller/controller.py

# Run controller with monitoring enabled
monitor:
	python3 controller/controller.py --monitor --monitor-interval 5

# Run benchmark tests
test:
	sudo python3 tests/benchmark.py

# Full pipeline: build, run topology + controller + benchmark
demo: $(P4_JSON)
	bash scripts/run.sh

# Clean build artifacts and logs
clean:
	rm -rf $(BUILD_DIR)
	bash scripts/cleanup.sh 2>/dev/null || true
	@echo "Cleaned."
