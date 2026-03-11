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

Architecture
------------
The repository contains two different workload targets plus a small measurement and analysis pipeline around them.

System topology:
- `app` is the Go single-server queueing testbed exposed on `localhost:8080`.
- `apache` is the PHP/Apache messaging backend plus browser UI exposed on `localhost:8082`.
- `cadvisor` exposes container-level CPU and memory metrics on `localhost:8081`.
- `prometheus` scrapes cAdvisor on `localhost:9090` using `prometheus.yml`.
- `grafana` can visualize Prometheus data on `localhost:3000`.

Container and resource model:
- Both `app` and `apache` are constrained in `docker-compose.yml` with `cpuset: "0"` and `cpus: "1.0"` to keep experiments closer to a single-core service model.
- If you run both workload services at the same time, they contend for the same pinned host CPU. For cleaner experiments, drive one workload target at a time.

Go service architecture:
- The Go server in `server_single/main.go` is the clean queueing baseline: one HTTP ingress, one bounded FIFO queue, and one worker goroutine.
- Each request arrival is timestamped in the HTTP handler, assigned a trace ID, and placed onto `jobs`.
- If the queue is full, the handler returns `503 queue full` immediately instead of blocking indefinitely.
- The worker samples a service demand from the configured distribution, converts that demand into calibrated CPU work, and executes a busy loop to approximate deterministic CPU consumption.
- When the worker finishes, the handler writes the response and pushes a CSV record into an async logging channel.
- The logger intentionally applies backpressure rather than dropping records, because missing slow requests would distort the trace used for DES validation.
- On shutdown, the server stops accepting new requests, drains the worker queue, flushes the CSV logger, and then exits.

Apache messaging architecture:
- The Apache service in `apache/index.php` is a minimal messaging backend with a browser client.
- `GET /` serves `apache/chat.html`, which loads `apache/chat.js` and `apache/chat.css`.
- `POST /send` accepts JSON with `room`, `user`, and `text`, assigns a monotonically increasing message ID, and appends the message to a JSONL file.
- `GET /messages` filters stored messages by room and `since_id`, which lets the frontend fetch only newer messages.
- `GET /health` is the lightweight health/timing endpoint.
- The same synthetic service-demand model used for experiments is applied inside the PHP handler before route processing, and timing is surfaced through `X-Service-Target-Us` and `X-Service-Actual-Ms`.
- Message persistence defaults to `/tmp/apache_messages.jsonl` inside the container, so it survives requests but is reset if the container is recreated.
- The browser client is polling-based rather than websocket-based: it periodically calls `/messages` and updates the visible room transcript.

Measurement and analysis flow:
- Client-side load can be generated with `poisson_load_generator.py` for GET endpoints or with external tools such as Vegeta for more controlled experiments.
- The Go service writes request-level traces to `./logs and des/requests.csv`, including arrival, queue, service, and response timings.
- `logs and des/plot_metrics.py` reads a trace CSV and produces histograms, CDFs, and a small variability summary.
- `logs and des/single_server_des.py` reads an empirical trace, simulates a single-server FCFS system in replay or bootstrap mode, and writes a simulated CSV for comparison.
- `prometheus.yml` is intentionally minimal: it scrapes only cAdvisor, making Prometheus/Grafana a container-metrics side channel rather than the primary request-trace source.

Repository Layout
-----------------
- server_single/
  - main.go: single-threaded HTTP service + CSV logging
  - Dockerfile: container build for the service
- docker-compose.yml: app + optional apache + metrics stack
- apache/
  - index.php: Apache-served messaging endpoints with configurable synthetic service demand
  - chat.html / chat.css / chat.js: browser UI for room-based message send/receive testing
- logs and des/
  - requests.csv: CSV logs from the Go app (bind-mounted from container)
  - plot_metrics.py: histogram/CDF plotting helper for trace CSVs
  - single_server_des.py: single-server DES and comparison script
  - sample CSV/PNG files: example traces and generated analysis outputs
- poisson_load_generator.py: Poisson arrival load generator (host, GET requests)
- prometheus.yml: Prometheus scrape config for cAdvisor

Quick Start (Docker)
--------------------
1) Start the stack:
```powershell
docker compose up --build
```

2) Generate Poisson arrivals (open-loop) against the Go app:
```powershell
python .\poisson_load_generator.py --url http://localhost:8080/ --rate 200 --duration 60
```

3) Generate open-loop read traffic against the Apache messaging variant:
```powershell
python .\poisson_load_generator.py --url "http://localhost:8082/messages?room=general&since_id=0&limit=50" --rate 200 --duration 60
```

4) Or use Vegeta (Dockerized) for the Go app:
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
It now behaves like a minimal messaging backend with a small browser client at `/` and three API routes:

- `GET /` serves a chat-style test UI
- `GET /health`
- `POST /send` with JSON body: `{"room":"general","user":"alice","text":"hello"}`
- `GET /messages?room=general&since_id=0&limit=50`

Message persistence is append-only JSONL on container filesystem (default `/tmp/apache_messages.jsonl`).
The browser client receives messages by polling `GET /messages`; this is sufficient for interactive testing, but it is not a websocket-based real-time transport.

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
curl.exe -X POST http://localhost:8082/send -H "Content-Type: application/json" -d "{\"room\":\"general\",\"user\":\"alice\",\"text\":\"hello\"}"
curl.exe "http://localhost:8082/messages?room=general&since_id=0&limit=50"
```

Testing the Messaging Backend
-----------------------------
Use the `apache` service when you want the messaging-style backend rather than the Go FIFO app.

1) Start only the messaging backend:
```powershell
docker compose up -d apache
docker compose ps apache
```

2) Open the browser client:
```text
http://localhost:8082/
```

The UI lets you:
- choose a room
- choose a user name
- send messages with `POST /send`
- receive new messages by polling `GET /messages`

3) Optionally watch logs while testing:
```powershell
docker compose logs -f apache
```

4) Smoke test the health route:
```powershell
Invoke-RestMethod http://localhost:8082/health
```

Expected result:
- `ok = True`

5) Send a message:
```powershell
$body = @{ room = "general"; user = "alice"; text = "hello" } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri http://localhost:8082/send -ContentType "application/json" -Body $body
```

Expected result:
- `ok = true`
- A `message` object containing `id`, `room`, `user`, `text`, and `ts_unix_ns`

6) Fetch messages from a room:
```powershell
Invoke-RestMethod "http://localhost:8082/messages?room=general&since_id=0&limit=50"
```

Expected result:
- `count` should be at least `1`
- The returned `messages` array should include the message you just posted

7) Inspect raw response headers if you want to record synthetic service timing:
```powershell
curl.exe -i http://localhost:8082/health
```

Relevant headers:
- `X-Service-Target-Us`
- `X-Service-Actual-Ms`

8) Negative tests:
```powershell
curl.exe -i -X POST http://localhost:8082/send -H "Content-Type: application/json" -d "{\"room\":\"general\",\"user\":\"alice\",\"text\":\"\"}"
curl.exe -i -X POST http://localhost:8082/send -H "Content-Type: application/json" -d "{bad json}"
```

Expected result:
- Empty `text` returns `400` with `text_required`
- Invalid JSON returns `400` with `invalid_json`

Load Testing the Messaging Backend
----------------------------------
The included `poisson_load_generator.py` only sends `GET` requests, so it is suitable for `GET /messages` or `GET /health`, but not `POST /send`.

For interactive manual testing, open the frontend at `http://localhost:8082/` in two browser tabs with different user names and the same room. Sending from one tab should appear in the other on the next polling cycle.

Read-path load test:
```powershell
python .\poisson_load_generator.py --url "http://localhost:8082/messages?room=general&since_id=0&limit=50" --rate 200 --duration 60 | Tee-Object ".\logs and des\apache_read_load_200rps.txt"
```

Health-route load test:
```powershell
python .\poisson_load_generator.py --url "http://localhost:8082/health" --rate 200 --duration 60 | Tee-Object ".\logs and des\apache_health_load_200rps.txt"
```

Simple write burst for the send path:
```powershell
1..100 | ForEach-Object {
  $body = @{ room = "general"; user = "load"; text = "message $_" } | ConvertTo-Json -Compress
  Invoke-RestMethod -Method Post -Uri http://localhost:8082/send -ContentType "application/json" -Body $body | Out-Null
}
```

Notes:
- The write burst above is useful for quickly exercising `POST /send`, but it is not a controlled open-loop load generator.
- If you need controlled POST load, use a tool that supports request bodies such as Vegeta, k6, or Locust.

Recording Results
-----------------
For the Apache messaging backend, record results with client-side output, HTTP headers, container logs, and the metrics stack.

Save a single request's timing headers:
```powershell
curl.exe -D ".\logs and des\apache_health_headers.txt" -o NUL http://localhost:8082/health
```

Save a message snapshot:
```powershell
Invoke-RestMethod "http://localhost:8082/messages?room=general&since_id=0&limit=50" | ConvertTo-Json -Depth 6 | Out-File ".\logs and des\apache_messages_snapshot.json"
```

Save Apache container logs:
```powershell
docker compose logs apache | Out-File ".\logs and des\apache_docker.log"
```

Save load-generator summaries:
```powershell
python .\poisson_load_generator.py --url "http://localhost:8082/health" --rate 200 --duration 60 | Tee-Object ".\logs and des\apache_health_load_200rps.txt"
```

Optional metrics stack for container-level CPU and memory:
```powershell
docker compose up -d apache cadvisor prometheus grafana
```

Endpoints:
- cAdvisor: `http://localhost:8081`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` (default password: `admin`)

Important:
- The Go app writes request-level traces to `./logs and des/requests.csv`.
- The Apache messaging backend does **not** currently write request-level CSV traces, so `requests.csv` will not contain Apache route timings unless you add separate logging for that service.
- Messages are stored in the Apache container at `/tmp/apache_messages.jsonl`. Recreating the container resets that message store.


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
