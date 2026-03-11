Docker Container Modelling
============
Currently working on Single-core, single-thread queueing testbed for validating trace-driven DES
and capacity planning models under controlled load. All found in server_single.

Overview
--------
- Go HTTP service with a single worker (FCFS FIFO).
- Optional real Apache service (php-apache) with configurable service-time distribution.
- Docker constraints to approximate a single core.
- CSV logging of arrival/start/end times and queue/service/response latencies.
- Load generation via Vegeta or a Poisson (open-loop) generator.
- Simple plotting helpers for histograms and CDFs.

Repository Layout
-----------------
- server_single/
  - main.go: single-threaded HTTP service + CSV logging
  - Dockerfile: container build for the service
- docker-compose.yml: app + optional apache + metrics stack
- apache/
  - index.php: Apache-served messaging endpoints with configurable synthetic service demand
- logs and des/
  - requests.csv: CSV logs (bind-mounted from container)
  - plot_metrics.py: plotting helper
  - single_server_des.py: single-server DES and comparison script
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
# apache variant
python .\poisson_load.py --url http://host.docker.internal:8082/ --rate 200 --duration 60
```

3) Or use Vegeta (Dockerized):
```powershell
$TARGET = "http://host.docker.internal:8080/"
"GET $TARGET" | docker run --rm -i peterevans/vegeta sh -c "vegeta attack -duration=60s -rate=800 | vegeta report"
```

CSV Logs
--------
Logs are written to `./logs and des/requests.csv` (bind-mounted):

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
Apache Service Variant
----------------------
A real Apache service is available at `http://localhost:8082/` via `php:8.3-apache`.
It now behaves like a minimal messaging backend with three routes:

- `GET /health`
- `POST /send` with JSON body: `{"room":"general","user":"alice","text":"hello"}`
- `GET /messages?room=general&since_id=0&limit=50`

Message persistence is append-only JSONL on container filesystem (default `/tmp/apache_messages.jsonl`).

Service-time and app controls:
- APACHE_SERVICE_DIST=fixed|exponential|lognormal|uniform
- APACHE_SERVICE_MEAN_US=200
- APACHE_SERVICE_LOGN_SIGMA=0.5
- APACHE_SERVICE_MIN_US / APACHE_SERVICE_MAX_US (uniform)
- APACHE_MESSAGES_FILE=/tmp/apache_messages.jsonl
- APACHE_MAX_TEXT_BYTES=1024

Response headers include:
- `X-Service-Target-Us`: sampled target demand
- `X-Service-Actual-Ms`: measured in-handler service time

Example requests:
```powershell
curl -X POST http://localhost:8082/send -H "Content-Type: application/json" -d '{"room":"general","user":"alice","text":"hello"}'
curl "http://localhost:8082/messages?room=general&since_id=0&limit=50"
```


Plotting
--------
Plot histograms and CDFs from `requests.csv` in `logs and des/`:
```powershell
python ".\logs and des\plot_metrics.py"
```

Optional flags:
```powershell
python ".\logs and des\plot_metrics.py" --all
python ".\logs and des\plot_metrics.py" --logy
python ".\logs and des\plot_metrics.py" --csv requests_2rps.csv --metric response_ms
```

DES (Single Server FCFS)
------------------------
Run DES against a trace:
```powershell
python ".\logs and des\single_server_des.py" --input ".\logs and des\requests_2rps.csv" --output ".\logs and des\des_simulated_2rps_replay.csv" --mode replay
```

Bootstrap service-time sampling:
```powershell
python ".\logs and des\single_server_des.py" --input ".\logs and des\requests_2rps.csv" --output ".\logs and des\des_simulated_2rps.csv" --mode bootstrap --seed 42
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
