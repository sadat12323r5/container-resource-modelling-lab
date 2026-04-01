# Apache/PHP Messaging Backend (mpm_prefork, 1 Core)

## Experimental Design

| Parameter | Value |
|---|---|
| Architecture | Apache mpm_prefork — pre-forked worker processes sharing one CPU, file-backed JSONL store |
| Service pipeline | PHP synthetic lognormal busy-loop + file I/O for message append/read (~1.8ms) |
| DES model | M/G/1 — `logs and des/single_server_des.py` |
| CPU cores | 1 (`cpuset=0`, `cpus=1.0`) |
| Memory limit | 256m |
| Port | 8082 |
| Sweep duration | 90 s per rate point |
| Load seed | 42 |

## Results

| Rate (rps) | n | rho | svc p50 (ms) | resp p50 (ms) | resp p99 (ms) | KS replay | KS bootstrap | KS parametric |
|---|---|---|---|---|---|---|---|---|
| 10 | 649 | 0.020 | 1.980 | 2.697 | 7.423 | 0.423 | 0.433 | 0.432 |
| 25 | 1,582 | 0.053 | 2.101 | 2.712 | 9.644 | 0.357 | 0.378 | 0.358 |
| 50 | 3,182 | 0.123 | 2.468 | 3.004 | 26.019 | 0.343 | 0.353 | 0.315 |

![Response-time CDFs: observed vs DES](cdf.png)

## Interpretation

File-lock contention between mpm_prefork workers (flock on the JSONL store) inflates response times far beyond DES prediction. KS floor ~0.35 at all tested rates. Demonstrates the scope condition under which M/G/1 DES fails: hidden shared-resource contention.

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
docker compose up -d apache

# 2. Run one load step (adjust --rate)
python apache_load.py --rate 25 --duration 90

# 3. Run DES on the collected trace
python "logs and des/single_server_des.py" \
  --input experiments/apache_msg_1c/<trace_file>.csv \
  --mode replay --output des_out.csv

# 4. Re-run all DES modes and regenerate summary + CDF
python run_des_all.py --servers apache_msg_1c
python plot_all_cdfs.py apache_msg_1c
```
