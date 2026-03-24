# Docker Container Capacity Modelling — Queueing Testbed

Single-core, single-thread queueing testbed for validating trace-driven Discrete Event
Simulation (DES) and capacity planning models under controlled load.

---

## Overview

- Go HTTP service with a single FCFS FIFO worker — the primary queueing baseline.
- Apache/PHP messaging backend with configurable synthetic service-time distributions.
- Docker resource constraints to approximate a single-core service model.
- CSV logging of arrival/queue/service/response timestamps at nanosecond resolution.
- Open-loop Poisson load generators for both GET-only and mixed GET+POST workloads.
- DES engine (bootstrap and replay modes) and plotting helpers for trace analysis.

---

## Architecture

### System topology

| Service | Port | Role |
|---|---|---|
| `app` | 8080 | Go single-server queueing testbed |
| `apache` | 8082 | PHP/Apache messaging backend + browser UI |
| `cadvisor` | 8081 | Container-level CPU/memory metrics |
| `prometheus` | 9090 | Scrapes cAdvisor |
| `grafana` | 3000 | Visualises Prometheus data |

Both `app` and `apache` are pinned with `cpuset: "0"` and `cpus: "1.0"` in
`docker-compose.yml`. Drive one workload target at a time to avoid CPU contention.

### Go service (`server_single/main.go`)

- One HTTP ingress goroutine timestamps each arriving request and enqueues a job.
- One worker goroutine dequeues jobs, samples a service demand from the configured
  distribution, and executes a calibrated busy-loop to approximate CPU work.
- If the job queue (default 1024 slots) is full, the handler returns `503 queue full`
  immediately rather than blocking.
- A separate async logger goroutine writes CSV records; it applies backpressure instead of
  dropping records to preserve all slow-request data for DES validation.
- On shutdown: stops accepting, drains the queue, flushes CSV, exits.

### Apache service (`apache/index.php`)

- `GET /` — serves `chat.html` (browser test UI)
- `GET /health` — lightweight timing endpoint
- `POST /send` — accepts `{"room","user","text"}`, appends to JSONL file
- `GET /messages?room=&since_id=&limit=` — filters stored messages by room

Each request samples a synthetic service demand and executes a busy-loop before routing,
surfaced through response headers:
- `X-Service-Target-Us` — sampled target in microseconds
- `X-Service-Actual-Ms` — measured in-handler time in milliseconds

Message persistence is append-only JSONL at `APACHE_MESSAGES_FILE` (default
`/tmp/apache_messages.jsonl`). The store resets if the container is recreated.

Apache routing is handled by `.htaccess` (`FallbackResource /index.php`) with
`AllowOverride All` enabled in `apache/000-default.conf`.

### Measurement pipeline

```
poisson_load_generator.py  ──►  Go app  ──►  requests.csv
apache_load.py             ──►  Apache  ──►  client-side latency stats

requests.csv  ──►  single_server_des.py  ──►  des_simulated.csv + KS distances
requests.csv  ──►  plot_metrics.py       ──►  histogram/CDF PNGs
```

---

## Repository Layout

```
Capacity_lab/
├── server_single/
│   ├── main.go              # Go single-worker HTTP service + CSV logger
│   ├── Dockerfile
│   └── go.mod
├── apache/
│   ├── index.php            # PHP routing, synthetic service demand, JSONL store
│   ├── .htaccess            # FallbackResource /index.php
│   ├── 000-default.conf     # Apache VirtualHost with AllowOverride All
│   ├── chat.html            # Browser test UI
│   ├── chat.js
│   └── chat.css
├── logs and des/
│   ├── requests.csv         # Go app trace log (bind-mounted from container)
│   ├── single_server_des.py # FCFS DES: replay and bootstrap modes
│   └── plot_metrics.py      # Histogram/CDF plotting helper
├── poisson_load_generator.py  # Open-loop Poisson generator (GET only)
├── apache_load.py             # Open-loop Poisson generator (GET + POST /send)
├── run_experiments.py         # Automated rate-sweep experiment runner
├── docker-compose.yml
└── prometheus.yml
```

---

## Quick Start

### 1. Start the stack

```bash
docker compose up --build
```

### 2. Verify endpoints

```bash
curl http://localhost:8080/                        # Go app
curl http://localhost:8082/health                  # Apache health
curl -X POST http://localhost:8082/send \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","room":"general","text":"hello"}'
curl "http://localhost:8082/messages?room=general"
```

### 3. Run the automated experiment suite

Runs Go + Apache rate sweeps (50/100/200/400 rps, 90 s each), runs DES on every Go
trace, and prints a consolidated results table.

```bash
python run_experiments.py
```

Results land in `logs and des/experiments/`.

### 4. Run a single load step (Go app)

```bash
python poisson_load_generator.py --url http://localhost:8080/ --rate 200 --duration 60
```

### 5. Run a mixed load step (Apache)

```bash
python apache_load.py --rate 100 --duration 60 --post-ratio 0.3
```

### 6. Run DES against a trace

```bash
# Bootstrap mode (samples with replacement)
python "logs and des/single_server_des.py" \
  --input  "logs and des/requests.csv" \
  --output "logs and des/des_simulated.csv" \
  --mode bootstrap --seed 42

# Replay mode (uses observed service times in order)
python "logs and des/single_server_des.py" \
  --input  "logs and des/requests.csv" \
  --output "logs and des/des_simulated_replay.csv" \
  --mode replay
```

### 7. Plot a trace

```bash
python "logs and des/plot_metrics.py"
python "logs and des/plot_metrics.py" --all --logy
python "logs and des/plot_metrics.py" --csv requests_2rps.csv --metric response_ms
```

---

## CSV Trace Format

Written to `./logs and des/requests.csv` (bind-mounted from the Go container):

```
id, trace_id, arrival_unix_ns, service_start_unix_ns, service_end_unix_ns,
response_end_unix_ns, queue_ms, service_ms, response_ms, status_code, bytes_written
```

All timestamp fields are Unix nanoseconds. `queue_ms`, `service_ms`, and `response_ms`
are floating-point milliseconds derived from those timestamps.

CSV logging environment variables:

| Variable | Default | Description |
|---|---|---|
| `CSV_LOG_PATH` | `requests.csv` | Path inside container |
| `CSV_LOG_APPEND` | `true` | Append vs overwrite on start |
| `CSV_LOG_QUEUE` | `10000` | Async logger channel depth |
| `CSV_LOG_FLUSH_MS` | `1000` | Flush interval (ms) |

---

## Service-Time Distributions

Configured via environment variables on the `app` or `apache` service.

| `SERVICE_DIST` | Additional vars | Description |
|---|---|---|
| `fixed` | `SERVICE_MEAN_US` | Constant demand |
| `exponential` | `SERVICE_MEAN_US` | Exp with given mean |
| `lognormal` | `SERVICE_MEAN_US`, `SERVICE_LOGN_SIGMA` | Log-normal |
| `uniform` | `SERVICE_MIN_US`, `SERVICE_MAX_US` | Uniform range |

Apache uses the same options prefixed with `APACHE_` (e.g. `APACHE_SERVICE_DIST`).

Default in `docker-compose.yml`: `lognormal`, mean **2000 µs**, σ = 0.5.
This places the 200 rps rate step at ρ ≈ 40%, producing measurable M/G/1 queueing.
See `EXPERIMENTS.md` for the rationale and full results.

---

## Dependencies

```bash
pip install pandas matplotlib
```

Python 3.10+ required. Docker Desktop with Linux engine required for the stack.

---

## Known Measurement Artefacts

### 8 µs goroutine-scheduling floor in `queue_ms`
`queue_ms` is computed as `service_start - arrival`. Even when the server is idle,
the goroutine scheduling round-trip (HTTP handler → job channel → worker goroutine)
adds a constant ~7–8 µs that appears as queue time. Subtract the p1 of `queue_ms`
(≈ 0.006 ms) before computing KS statistics or queue-time distributions.

### ms columns vs ns columns use different clocks
The stored ms values (`queue_ms`, `service_ms`, `response_ms`) are computed using
Go's monotonic clock (`time.Sub()`). The ns timestamp columns use the wall clock
(`time.Now().UnixNano()`). Under WSL2, NTP synchronisation can adjust the wall
clock mid-run, causing ~0.37% of rows to show divergence between the two
representations. **Use ms columns for all interval analysis; use ns columns for
ordering only.**

### Work-based calibration scale under WSL2
`SERVICE_MEAN_US` declares the target service time in µs, but the work-based busy-
loop is calibrated once at startup. Under WSL2 with variable CPU scheduling, actual
service times are typically 40–65% of the declared value. Distribution *shape* is
preserved (lognormal confirmed by p50/p99 ratio); only scale differs.

## Notes

- cAdvisor may log benign warnings about short-lived containers.
- Rebuild after Go code changes: `docker compose up --build`.
- Apache messages reset on container recreation; Go CSV persists via bind-mount.
- The Go app logs all requests including 503s. Filter on `status_code == 200`
  before feeding traces to the DES or plotting scripts.
- Drive one workload target at a time — both are pinned to `cpuset: "0"`.
- Full experiment log, results tables, and DES analysis: see `EXPERIMENTS.md`.
