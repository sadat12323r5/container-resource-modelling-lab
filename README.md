# Docker Container Capacity Modelling — Research Platform

A reproducible, personal-machine research platform for capacity planning of containerised
microservices. The platform progressively builds from foundational single-server queueing
experiments through multi-worker scaling, CPU-resource sweeps, and ML-assisted capacity
prediction — replicating and extending the MLASP (Vitui & Chen, 2021) pipeline in a
controlled Docker environment.

This is **Iteration 1** of a multi-iteration thesis project (COMP9334, UNSW). Each
iteration adds one dimension of complexity: server concurrency, resource configuration,
service topology, or modelling technique.

---

## Research Agenda

| Iteration | Focus | Status |
|---|---|---|
| 1 | Single-server, single-queue: DES validation, operational laws, ML baseline | **Complete** |
| 2 | Multi-worker scaling + CPU-limit sweep: how does capacity change with resource allocation? | Planned |
| 3 | Queueing networks: two-tier service, MVA comparison, bottleneck identification | Planned |
| 4 | MLASP pipeline: train `(cpu_limit, arrival_rate) → response_p99` model, SLA query | Planned |

---

## Iteration 1 — What Has Been Done

### Experimental scope
- Go single-worker FCFS server, rate sweep 50–400 rps (90 s per step), 8 rates
- Fine-grained sweep around the capacity knee: 150, 175, 200, 225, 250 rps
- Apache/PHP messaging backend, rate sweep 10–50 rps (90 s per step)

### DES validation (Go server)
Three simulation modes against observed traces, with Kolmogorov-Smirnov distance as
the fidelity metric and a 0.006 ms goroutine-scheduling floor correction applied.
The DES also includes a **queue-capacity drop model** (`--queue-capacity 1024`) that
mirrors the Go server's 1024-job channel limit: arrivals that find the simulated queue
full are dropped (status 503) and excluded from the KS comparison.

| Mode | Method | KS_response (200 rps) |
|---|---|---:|
| Replay | Observed service times in arrival order | **0.029** |
| Bootstrap | Resample with replacement | 0.132 |
| Parametric | Lognormal fit (MLE) + i.i.d. draws | 0.141 |

Capacity knee at ~190 rps (this run; varies ±20 rps with WSL2 calibration):
queue p99 jumps 8×, achieved throughput drops below offered load at 200+ rps.

### Operational laws validation
- **Utilisation law** (ρ = λ E[S]): validated across all 8 rates (ρ = 6.2%–55.2%)
- **Little's Law**: formula holds conceptually; direct P-K comparison is confounded
  by the 6 µs goroutine scheduling floor added to every queue_ms measurement

### ML baseline vs DES
Polynomial regression (degree 1 and 2) with leave-one-out cross-validation:

| Model | LOOCV MAE p99 | vs DES replay |
|---|---:|---|
| Linear regression | 8.9 ms | 6.3× more accurate than DES at saturation |
| DES replay | 79.5 ms | 30–50× more accurate than ML at low utilisation |

DES replay dominates at low load (ρ < 30%). ML dominates near/above saturation
because the DES has no request-drop model and simulates unbounded queue growth.
The ML advantage grows when WSL2 calibrates slower (longer actual service times →
earlier saturation → worse DES over-prediction).

Parametric DES cross-rate generalisation: 22 ms error within regime (100→200 rps),
198 ms error across the capacity boundary (100→400 rps).

### Apache comparison
Apache/PHP with file-backed message store (bounded ring buffer, 1000-message cap):

| rate | tput | resp_p99 | q_p99 | KS_replay |
|---:|---:|---:|---:|---:|
| 10 rps | 10.0 | 10.1 ms | 6.5 ms | 0.485 |
| 25 rps | 25.0 | 14.1 ms | 9.8 ms | 0.425 |
| 50 rps | 50.0 | 35.8 ms | 27.2 ms | 0.397 |

DES KS ≈ 0.44 for Apache vs ≈ 0.03 for Go. Apache's dominant latency source is
mpm_prefork file-lock contention — invisible to the M/G/1 model — demonstrating the
scope condition under which DES is accurate.

Full results and analysis: see [`EXPERIMENTS.md`](EXPERIMENTS.md).

---

## Architecture

### System topology

| Service | Port | Role |
|---|---|---|
| `app` | 8080 | Go single-worker FCFS queueing server — primary DES target |
| `apache` | 8082 | PHP/Apache messaging backend + browser chat UI |
| `cadvisor` | 8081 | Container-level CPU/memory metrics |
| `prometheus` | 9090 | Scrapes cAdvisor |
| `grafana` | 3000 | Visualises Prometheus metrics |

Both `app` and `apache` are pinned to `cpuset: "0"`, `cpus: "1.0"` in
`docker-compose.yml`. Run one workload at a time to avoid CPU contention between services.

### Go service (`server_single/main.go`)

- One HTTP ingress goroutine timestamps each arriving request and enqueues a job
- One worker goroutine dequeues jobs, samples a service demand from the configured
  distribution, and runs a calibrated busy-loop to approximate CPU work
- Job queue depth 1024; full queue returns `503 queue full` immediately
- Async logger goroutine writes CSV records with backpressure (never drops records)
- Shutdown: stops accepting, drains queue, flushes CSV

### Apache service (`apache/index.php`)

- `GET /` — serves `chat.html` (browser chat UI)
- `GET /health` — lightweight timing endpoint
- `POST /send` — accepts `{"room","user","text"}`, appends to bounded JSONL store
- `GET /messages?room=&since_id=&limit=` — returns recent messages for a room

Each request samples a synthetic service demand and busy-loops before routing.
Response headers expose `X-Service-Target-Us` and `X-Service-Actual-Ms`.

Message store: append-only JSONL capped at `APACHE_MAX_MESSAGES` (default 1000) with
O(1) fast-path append and O(limit) read. Resets on container recreation.

CSV trace logging: every request is logged to `APACHE_TRACE_CSV` with the same schema
as the Go server — enabling DES comparison on Apache traces.

### Measurement pipeline

```
poisson_load_generator.py  -->  Go app  -->  logs and des/requests.csv
apache_load.py             -->  Apache  -->  logs and des/apache_requests.csv

run_experiments.py  (phases 1-3)
  Phase 1: Go coarse sweep   [50, 100, 200, 400 rps]
  Phase 2: Go fine sweep     [150, 175, 225, 250 rps]  around capacity knee
  Phase 3: Apache sweep      [10, 25, 50 rps]

  per-rate CSV slice  -->  single_server_des.py  -->  DES output CSV + KS distances
                       -->  ml_baseline.py        -->  LOOCV MAE, operational laws
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
│   ├── index.php            # PHP routing, synthetic load, bounded JSONL store, CSV log
│   ├── .htaccess            # FallbackResource /index.php
│   ├── 000-default.conf     # Apache VirtualHost with AllowOverride All
│   ├── chat.html            # Browser chat UI
│   ├── chat.js
│   └── chat.css
├── logs and des/
│   ├── requests.csv             # Go app trace log (bind-mounted from container)
│   ├── apache_requests.csv      # Apache trace log (bind-mounted from container)
│   ├── single_server_des.py     # FCFS DES: replay, bootstrap, parametric modes
│   ├── plot_metrics.py          # Histogram/CDF plotting helper
│   └── experiments/             # Per-rate CSV slices + DES outputs (git-ignored)
│       ├── go_<rate>rps.csv
│       ├── go_<rate>rps_des_<mode>.csv
│       ├── apache_<rate>rps.csv
│       ├── apache_<rate>rps_des_<mode>.csv
│       └── experiment_results.json
├── poisson_load_generator.py    # Open-loop Poisson generator (Go app, GET only)
├── apache_load.py               # Open-loop Poisson generator (Apache, GET + POST)
├── run_experiments.py           # Automated three-phase experiment runner
├── ml_baseline.py               # Polynomial LOOCV, operational laws, DES vs ML
├── environment.yml              # Conda environment for CI
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
curl http://localhost:8080/                        # Go app (returns JSON)
curl http://localhost:8082/health                  # Apache health
curl -X POST http://localhost:8082/send \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","room":"general","text":"hello"}'
curl "http://localhost:8082/messages?room=general"
```

### 3. Run the full experiment suite

Runs all three phases (Go coarse + fine sweep, Apache sweep), DES in all three modes,
and saves per-rate CSVs + a consolidated JSON to `logs and des/experiments/`.

```bash
python run_experiments.py
```

### 4. Run the ML baseline and operational laws validation

```bash
python ml_baseline.py
```

Prints: utilisation law table, Little's Law check, LOOCV MAE for linear/quadratic
regression, per-rate DES vs ML comparison, and cross-rate generalisation results.

### 5. Run a single load step

```bash
# Go app
python poisson_load_generator.py --url http://localhost:8080/ --rate 200 --duration 60

# Apache (70% GET /messages + 30% POST /send)
python apache_load.py --rate 25 --duration 60 --post-ratio 0.3
```

### 6. Run DES against a trace

```bash
python "logs and des/single_server_des.py" \
  --input  "logs and des/requests.csv" \
  --output "logs and des/des_simulated.csv" \
  --mode   replay \
  --queue-offset 0.006
```

Available modes: `replay`, `bootstrap`, `parametric`.
`--queue-offset 0.006` subtracts the 6 µs goroutine scheduling floor from observed
queue times before the KS comparison (Go server only; omit for Apache).

### 7. Plot a trace

```bash
python "logs and des/plot_metrics.py"
python "logs and des/plot_metrics.py" --all --logy
```

---

## CSV Trace Format (Go server)

Written to `./logs and des/requests.csv` (bind-mounted from the Go container):

```
id, trace_id, arrival_unix_ns, service_start_unix_ns, service_end_unix_ns,
response_end_unix_ns, queue_ms, service_ms, response_ms, status_code, bytes_written
```

All timestamp fields are Unix nanoseconds. `queue_ms`, `service_ms`, and `response_ms`
are floating-point milliseconds derived from Go's monotonic clock.

## CSV Trace Format (Apache server)

Written to `./logs and des/apache_requests.csv` (bind-mounted from the Apache container):

```
arrival_unix_ns, service_ms, queue_ms, response_ms, status_code
```

`arrival_unix_ns` is derived from PHP's `$_SERVER['REQUEST_TIME_FLOAT']` (wall clock).
`service_ms` is the synthetic busy-loop time (i.i.d. lognormal draws).
`queue_ms` captures PHP overhead not modelled by DES (file I/O, lock wait).
`response_ms` is total PHP execution time.

---

## Service-Time Distributions

Both services sample synthetic service demand per request, controlled by environment
variables.

| `SERVICE_DIST` | Additional vars | Description |
|---|---|---|
| `lognormal` | `SERVICE_MEAN_US`, `SERVICE_LOGN_SIGMA` | Log-normal (default) |
| `exponential` | `SERVICE_MEAN_US` | Exponential |
| `uniform` | `SERVICE_MIN_US`, `SERVICE_MAX_US` | Uniform range |
| `fixed` | `SERVICE_MEAN_US` | Constant |

Apache uses the same options prefixed with `APACHE_` (e.g. `APACHE_SERVICE_DIST`).

Default: `lognormal`, mean **2000 µs**, σ = 0.5. At 200 rps this places the Go server
at ρ ≈ 30%, producing measurable M/G/1 queueing without saturation.

---

## Dependencies

```bash
pip install pandas matplotlib
```

Python 3.10+ required. Docker Desktop with Linux engine required for the stack.
No cloud infrastructure required — the platform is designed to run on a personal machine.

---

## Known Measurement Artefacts

### 6 µs goroutine-scheduling floor in `queue_ms` (Go server)
`queue_ms` is computed as `service_start - arrival`. Even when the server is idle, the
goroutine round-trip (HTTP handler → job channel → worker goroutine) adds a constant
~6–8 µs. Subtract 0.006 ms before KS comparisons (`--queue-offset 0.006`). This
correction is applied automatically by `run_experiments.py`.

### ms columns vs ns columns use different clocks (Go server)
`queue_ms`, `service_ms`, `response_ms` use Go's monotonic clock (`time.Sub()`).
The ns timestamp columns use the wall clock (`time.Now().UnixNano()`). WSL2 NTP
adjustments can cause ~0.37% of rows to diverge. Use ms columns for interval analysis;
use ns columns for ordering only.

### Work-based calibration scale under WSL2
`SERVICE_MEAN_US` declares the target service time, but the busy-loop calibration runs
once at startup. Under WSL2, actual service times are typically 40–65% of the declared
value. Distribution shape is preserved (lognormal σ confirmed); only scale differs.

### Apache client-side latency includes WSL2 network overhead
`apache_load.py` measures round-trip time from Python. Docker/WSL2 network adds ~35 ms
per request on top of PHP execution time. Use the server-side `apache_requests.csv`
for capacity analysis; treat client-side stats as indicative only.

---

## Notes

- Rebuild after Go code changes: `docker compose up --build`
- Apache messages reset on container recreation; Go CSV persists via bind-mount
- Filter on `status_code` starting with `2` (200/201) before DES or plotting
- Drive one workload target at a time — both services share `cpuset: "0"`
- cAdvisor may log benign warnings about short-lived containers
- Full experiment log, results tables, findings, and next steps: see [`EXPERIMENTS.md`](EXPERIMENTS.md)
