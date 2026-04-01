# Java DSP-AES Pipeline (ThreadPoolExecutor 4 Threads, 1 Core)

## Experimental Design

| Parameter | Value |
|---|---|
| Architecture | Java HttpServer + ThreadPoolExecutor(4, 4, ...) on one CPU core (M/G/4) |
| Service pipeline | AES-256-CBC (JCE HW acceleration) + 64-tap FIR + re-encrypt (~0.17-0.29ms) |
| DES model | M/G/4 — `logs and des/multi_server_des.py --workers 4` |
| CPU cores | 1 (`cpuset=0`, `cpus=1.0`) |
| Memory limit | 256m |
| Port | 8086 |
| Sweep duration | 90 s per rate point |
| Load seed | 42 |

## Results

| Rate (rps) | n | rho | svc p50 (ms) | resp p50 (ms) | resp p99 (ms) | KS replay | KS bootstrap | KS parametric |
|---|---|---|---|---|---|---|---|---|
| 25 | 2,379 | 0.003 | 0.547 | 27.519 | 166.020 | 0.748 | 0.747 | 0.778 |
| 50 | 4,519 | 0.004 | 0.306 | 0.209 | 13.115 | 0.180 | 0.182 | 0.233 |
| 100 | 8,848 | 0.006 | 0.236 | 0.187 | 2.424 | 0.235 | 0.230 | 0.251 |
| 200 | 4,402 | 0.012 | 0.248 | 0.194 | 1.954 | 0.219 | 0.210 | 0.242 |
| 400 | 2,721 | 0.068 | 0.679 | 0.220 | 11.651 | 0.221 | 0.222 | 0.215 |
| 600 | 2,611 | 0.067 | 0.444 | 0.221 | 9.683 | 0.245 | 0.260 | 0.221 |

![Response-time CDFs: observed vs DES](cdf.png)

## Interpretation

JVM JIT warm-up creates a bimodal service time distribution: first ~200 requests run 5-10x slower (interpreter mode). KS=0.748 at 25 rps drops to ~0.18 after warm-up. Hardware AES acceleration via JCE makes this the fastest pipeline per-thread (~0.2ms). Very high theoretical capacity (>2000 rps on 4 threads); practical limit set by client connection pool.

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
docker compose up -d java-dsp

# 2. Run one load step (adjust --rate)
python dsp_aes_load.py --url http://localhost:8086/process --rate 100 --duration 90

# 3. Run DES on the collected trace
python "logs and des/multi_server_des.py --workers 4" \
  --input experiments/java_dsp_1c/<trace_file>.csv \
  --mode replay --output des_out.csv

# 4. Re-run all DES modes and regenerate summary + CDF
python run_des_all.py --servers java_dsp_1c
python plot_all_cdfs.py java_dsp_1c
```
