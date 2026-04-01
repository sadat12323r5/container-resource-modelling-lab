# Go SQLite I/O Server (1 Worker, 1 Core)

## Experimental Design

| Parameter | Value |
|---|---|
| Architecture | Single goroutine FCFS channel queue — I/O-bound service (M/G/1) |
| Service pipeline | SQLite INSERT sensor reading + SELECT last 20 rows (~10ms per request) |
| DES model | M/G/1 — `logs and des/single_server_des.py` |
| CPU cores | 1 (`cpuset=0`, `cpus=1.0`) |
| Memory limit | 256m |
| Port | 8087 |
| Sweep duration | 90 s per rate point |
| Load seed | 42 |

## Results

| Rate (rps) | n | rho | svc p50 (ms) | resp p50 (ms) | resp p99 (ms) | KS replay | KS bootstrap | KS parametric |
|---|---|---|---|---|---|---|---|---|
| 10 | 917 | 0.113 | 11.332 | 30.049 | 136.181 | 0.550 | 0.544 | 0.570 |
| 25 | 2,315 | 0.266 | 10.648 | 12.151 | 140.086 | 0.158 | 0.151 | 0.152 |
| 50 | 4,495 | 0.523 | 10.463 | 10.421 | 121.832 | 0.448 | 0.448 | 0.442 |
| 75 | 6,423 | 0.803 | 10.701 | 10.340 | 83.319 | 0.718 | 0.712 | 0.702 |
| 100 | 1,007 | 4.609 | 46.091 | 38.506 | 152.532 | 0.917 | 0.921 | 0.883 |
| 200 | 8,680 | 2.868 | 14.340 | 12.863 | 51.911 | 0.987 | 0.984 | 0.961 |
| 400 | 5,173 | 8.404 | 21.010 | 15.088 | 88.089 | 0.910 | 0.951 | 0.884 |
| 600 | 4,930 | 12.145 | 20.242 | 15.091 | 88.212 | 0.961 | 0.962 | 0.961 |

![Response-time CDFs: observed vs DES](cdf.png)

## Interpretation

Tests whether DES accuracy depends on service type (I/O vs CPU). Conclusion: no — the same failure pattern occurs as CPU-bound servers. DES is accurate at rho=0.27 (KS=0.15) but degrades above rho=0.5 as WAL write-lock stall variance grows load-dependently. Capacity knee ~75-90 rps; 10x slower than Go lognormal due to SQLite I/O.

## Files

| File | Description |
|---|---|
| `cdf.png` | Observed vs DES response-time CDFs for all tested rates |
| `*_summary.csv` | Per-rate summary: rho, percentiles, KS distances for all modes |
| `*_NNNrps.csv` | Raw request trace (arrival_unix_ns, service_ms, queue_ms, response_ms, status_code) |
| `*_NNNrps_des_replay.csv` | DES output — replay mode (observed service times in order) |
| `*_NNNrps_des_bootstrap.csv` | DES output — bootstrap mode (resample with replacement) |
| `*_NNNrps_des_parametric.csv` | DES output — parametric mode (fitted lognormal) |

## Reproducing

```bash
# 1. Start only this server
docker compose up -d go-sqlite

# 2. Run one load step (adjust --rate)
python sqlite_load.py --url http://localhost:8087/process --rate 25 --duration 90

# 3. Run DES on the collected trace
python "logs and des/single_server_des.py" \
  --input experiments/go_sqlite_1c/<trace_file>.csv \
  --mode replay --output des_out.csv

# 4. Re-run all DES modes and regenerate summary + CDF
python run_des_all.py --servers go_sqlite_1c
python plot_all_cdfs.py go_sqlite_1c
```
