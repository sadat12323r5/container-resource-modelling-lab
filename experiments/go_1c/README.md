# Go Single-Worker (Lognormal Synthetic Load)

## Experimental Design

| Parameter | Value |
|---|---|
| Architecture | Single goroutine FCFS channel queue (M/G/1) |
| Service pipeline | Calibrated busy-loop sampling lognormal service time (mean ~1.1ms, sigma=0.5) |
| DES model | M/G/1 — `logs and des/single_server_des.py` |
| CPU cores | 1 (`cpuset=0`, `cpus=1.0`) |
| Memory limit | 256m |
| Port | 8080 |
| Sweep duration | 90 s per rate point |
| Load seed | 42 |

## Results

| Rate (rps) | n | rho | svc p50 (ms) | resp p50 (ms) | resp p99 (ms) | KS replay | KS bootstrap | KS parametric |
|---|---|---|---|---|---|---|---|---|
| 50 | 4,574 | 0.061 | 1.223 | 1.066 | 4.098 | 0.026 | 0.033 | 0.025 |
| 100 | 9,013 | 0.163 | 1.630 | 1.215 | 10.614 | 0.017 | 0.036 | 0.063 |
| 200 | 17,813 | 0.435 | 2.176 | 1.521 | 13.984 | 0.029 | 0.132 | 0.141 |
| 400 | 36,317 | 0.967 | 2.418 | 1.642 | 15.765 | 0.064 | 0.181 | 0.171 |

![Response-time CDFs: observed vs DES](cdf.png)

## Interpretation

Baseline server — service distribution is exactly lognormal, matching the DES model assumption. Achieves near-perfect DES accuracy (KS < 0.03 across all rates). Capacity knee ~190 rps; queue p99 jumps 8x between 200 and 225 rps.

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
docker compose up -d app

# 2. Run one load step (adjust --rate)
python poisson_load_generator.py --url http://localhost:8080/ --rate 50 --duration 90

# 3. Run DES on the collected trace
python "logs and des/single_server_des.py" \
  --input experiments/go_1c/<trace_file>.csv \
  --mode replay --output des_out.csv

# 4. Re-run all DES modes and regenerate summary + CDF
python run_des_all.py --servers go_1c
python plot_all_cdfs.py go_1c
```
