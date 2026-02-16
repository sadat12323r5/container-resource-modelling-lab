Capacity Lab
============
Single-core, single-thread queueing testbed for validating trace-driven DES
and capacity planning models under controlled load.

Overview
--------
- Go HTTP service with a single worker (FCFS FIFO).
- Docker constraints to approximate a single core.
- CSV logging of arrival/start/end times and queue/service/response latencies.
- Load generation via Vegeta or a Poisson (open-loop) generator.
- Simple plotting helpers for histograms and CDFs.

Repository Layout
-----------------
- server_single/
  - main.go: single-threaded HTTP service + CSV logging
  - Dockerfile: container build for the service
- docker-compose.yml: app + metrics stack
- logs/
  - requests.csv: CSV logs (bind-mounted from container)
  - plot_metrics.py: plotting helper
- poisson_load.py: Poisson arrival load generator (host)

Quick Start (Docker)
--------------------
1) Start the stack:
```powershell
docker compose up --build
```

2) Generate Poisson arrivals (open-loop):
```powershell
python .\poisson_load.py --url http://host.docker.internal:8080/ --rate 200 --duration 60
```

3) Or use Vegeta (Dockerized):
```powershell
$TARGET = "http://host.docker.internal:8080/"
"GET $TARGET" | docker run --rm -i peterevans/vegeta sh -c "vegeta attack -duration=60s -rate=800 | vegeta report"
```

CSV Logs
--------
Logs are written to `./logs/requests.csv` (bind-mounted):

CSV columns:
```
id,trace_id,arrival_unix_ns,service_start_unix_ns,service_end_unix_ns,response_end_unix_ns,queue_ms,service_ms,response_ms,status_code,bytes_written
```

Timestamp fields are Unix time in nanoseconds.

CSV logging controls (env):
- CSV_LOG_PATH: path inside container (default: requests.csv)
- CSV_LOG_APPEND: append to CSV (true/false)
- CSV_LOG_QUEUE: async log queue size (default: 10000)
- CSV_LOG_FLUSH_MS: async flush interval in ms (default: 1000)

Service-Time Distributions
--------------------------
Select service-time distribution via env vars on the app:

- SERVICE_DIST=fixed (default)
  - SERVICE_MEAN_US=200
- SERVICE_DIST=exponential
  - SERVICE_MEAN_US=200
- SERVICE_DIST=lognormal
  - SERVICE_MEAN_US=200
  - SERVICE_LOGN_SIGMA=0.5
- SERVICE_DIST=uniform
  - SERVICE_MIN_US=100
  - SERVICE_MAX_US=300

Example in `docker-compose.yml`:
```
environment:
  - SERVICE_DIST=lognormal
  - SERVICE_MEAN_US=200
  - SERVICE_LOGN_SIGMA=1.0
```

Plotting
--------
Plot histograms and CDFs from `logs/requests.csv`:
```powershell
python .\logs\plot_metrics.py
```

Optional flags:
```powershell
python .\logs\plot_metrics.py --all
python .\logs\plot_metrics.py --logy
```

Dependencies:
```powershell
python -m pip install pandas matplotlib
```

Iteration 1 Goals (Summary)
---------------------------
Establish a validated baseline modelling pipeline:
- Single-core, single-thread containerized service.
- Open-loop load and trace-based measurement.
- Parameterize a DES (single-server FIFO) from empirical traces.
- Verify that DES reproduces response-time distributions and utilization
  across arrival-rate sweeps.
- Compare empirical-sample DES vs parametric-fit DES.
- Baseline ML-only latency prediction vs DES prediction.

Notes
-----
- cAdvisor may log warnings about short-lived containers (benign).
- Rebuild after code changes: `docker compose up --build`.
