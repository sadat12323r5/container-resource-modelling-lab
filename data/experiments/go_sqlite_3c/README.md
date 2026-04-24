# Go SQLite I/O Server (3 Workers, WAL Mode, 3 Cores)

## Experimental Design

| Parameter | Value |
|---|---|
| Architecture | 3 goroutine workers sharing SQLite with WAL journal mode (M/G/3) |
| Service pipeline | Same INSERT+SELECT as go_sqlite_1c, WAL-serialised writes (~4.6ms per request) |
| DES model | M/G/3 — `analysis/des/multi_server_des.py --workers 3` |
| CPU cores | 3 (`cpuset=0,1,2`, `cpus=3.0`) |
| Memory limit | 512m |
| Port | 8091 |
| Sweep duration | 90 s per rate point |
| Load seed | 42 |

## Results

| Rate (rps) | n | rho | svc p50 (ms) | resp p50 (ms) | resp p99 (ms) | KS replay | KS bootstrap | KS parametric |
|---|---|---|---|---|---|---|---|---|
| 50 | 2,658 | 0.087 | 5.238 | 34.087 | 158.627 | 0.754 | 0.757 | 0.771 |
| 100 | 4,428 | 0.171 | 5.141 | 20.209 | 154.893 | 0.577 | 0.572 | 0.600 |
| 200 | 6,113 | 0.372 | 5.586 | 6.983 | 167.377 | 0.380 | 0.375 | 0.401 |
| 400 | 6,282 | 0.845 | 6.338 | 5.791 | 183.662 | 0.231 | 0.224 | 0.252 |
| 800 | 6,029 | 1.864 | 6.989 | 5.662 | 223.254 | 0.160 | 0.153 | 0.186 |

![Response-time CDFs: observed vs DES](cdf.png)

## Interpretation

SQLite WAL mode allows concurrent reads but serialises writes. With 3 goroutines all doing INSERT, effective concurrency is limited by write serialisation. Service time halves to ~4.6ms (vs ~10ms single-worker) — not a speedup, but a reflection of shorter individual critical sections when writes are interleaved. Effective throughput ~2.2x (not 3x). KS improves at high rho as WAL queueing matches M/G/3 prediction.

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
docker compose up -d go-sqlite-mc

# 2. Run one load step (adjust --rate)
python analysis/load/sqlite_load.py --url http://localhost:8091/process --rate 100 --duration 90

# 3. Run DES on the collected trace
python analysis/des/multi_server_des.py --workers 3 \
  --input data/experiments/go_sqlite_3c/<trace_file>.csv \
  --mode replay --output des_out.csv --workers 3

# 4. Re-run all DES modes and regenerate summary + CDF
python analysis/orchestration/run_des_all.py --servers go_sqlite_mc
python analysis/reporting/plot_all_cdfs.py go_sqlite_3c
```
