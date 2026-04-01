"""
write_server_readmes.py — Generate README.md for each experiments/ subfolder.
Run once; safe to re-run (overwrites existing READMEs).
"""
import csv
import os

BASE = os.path.join(os.path.dirname(__file__), "experiments")

META = {
    "go_1c": {
        "title": "Go Single-Worker (Lognormal Synthetic Load)",
        "arch": "Single goroutine FCFS channel queue (M/G/1)",
        "service": "Calibrated busy-loop sampling lognormal service time (mean ~1.1ms, sigma=0.5)",
        "model": "M/G/1 — `logs and des/single_server_des.py`",
        "port": 8080, "cores": 1,
        "compose_svc": "app",
        "load_cmd": "python poisson_load_generator.py --url http://localhost:8080/ --rate 50 --duration 90",
        "des_script": "single_server_des.py",
        "notes": (
            "Baseline server — service distribution is exactly lognormal, matching the DES model "
            "assumption. Achieves near-perfect DES accuracy (KS < 0.03 across all rates). "
            "Capacity knee ~190 rps; queue p99 jumps 8x between 200 and 225 rps."
        ),
    },
    "apache_msg_1c": {
        "title": "Apache/PHP Messaging Backend (mpm_prefork, 1 Core)",
        "arch": "Apache mpm_prefork — pre-forked worker processes sharing one CPU, file-backed JSONL store",
        "service": "PHP synthetic lognormal busy-loop + file I/O for message append/read (~1.8ms)",
        "model": "M/G/1 — `logs and des/single_server_des.py`",
        "port": 8082, "cores": 1,
        "compose_svc": "apache",
        "load_cmd": "python apache_load.py --rate 25 --duration 90",
        "des_script": "single_server_des.py",
        "notes": (
            "File-lock contention between mpm_prefork workers (flock on the JSONL store) "
            "inflates response times far beyond DES prediction. KS floor ~0.35 at all tested rates. "
            "Demonstrates the scope condition under which M/G/1 DES fails: hidden shared-resource contention."
        ),
    },
    "apache_dsp_1c": {
        "title": "Apache/PHP DSP-AES Pipeline (mpm_prefork, 1 Core)",
        "arch": "Apache mpm_prefork — worker processes competing for one CPU core",
        "service": "AES-256-CBC decrypt -> 64-tap FIR low-pass -> AES-256-CBC encrypt on 1024 float32 samples (~2.3ms)",
        "model": "M/G/1 — `logs and des/single_server_des.py`",
        "port": 8083, "cores": 1,
        "compose_svc": "apache-dsp",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8083/process --rate 25 --duration 90",
        "des_script": "single_server_des.py",
        "notes": (
            "CPU contention between mpm_prefork workers on a single core causes "
            "load-dependent service time inflation. At 75 rps (rho=0.44) service time grows "
            "from 2.3ms to ~3ms; at 100 rps it explodes to ~9.5ms. "
            "DES KS is acceptable below rho=0.2 (KS~0.14) but collapses at saturation (KS=0.97). "
            "M/G/c c=2 reduces KS from 0.215 to 0.104 at 75 rps."
        ),
    },
    "node_dsp_1c": {
        "title": "Node.js DSP-AES Pipeline (Single Event Loop, 1 Core)",
        "arch": "Single-threaded Node.js event loop — CPU-bound FIR blocks the loop, equivalent to M/G/1",
        "service": "AES-256-CBC decrypt -> 64-tap FIR -> AES-256-CBC encrypt (~0.5ms per request)",
        "model": "M/G/1 — `logs and des/single_server_des.py`",
        "port": 8084, "cores": 1,
        "compose_svc": "node-dsp",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8084/process --rate 200 --duration 90",
        "des_script": "single_server_des.py",
        "notes": (
            "Async CSV logging (fs.createWriteStream — sync version caused saturation at ~48 rps). "
            "High KS at low load (0.29-0.42) due to occasional GC/event-loop stalls creating "
            "a bimodal service time distribution the lognormal model cannot represent. "
            "Capacity knee ~600-800 rps. Inversely, KS improves at saturation (0.14) "
            "as queueing delay dominates over service time shape."
        ),
    },
    "python_dsp_1c": {
        "title": "Python/Gunicorn DSP-AES Pipeline (1 Worker, 1 Core)",
        "arch": "Gunicorn --workers 1 single WSGI process (true M/G/1)",
        "service": "AES-256-CBC decrypt -> 64-tap FIR (pure Python) -> AES-256-CBC encrypt (~7.5ms)",
        "model": "M/G/1 — `logs and des/single_server_des.py`",
        "port": 8085, "cores": 1,
        "compose_svc": "python-dsp",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8085/process --rate 50 --duration 90",
        "des_script": "single_server_des.py",
        "notes": (
            "Near-deterministic service time (CV ~0.05) because pure Python math has no JIT variance. "
            "DES achieves KS=0.105 at steady load (50-75 rps). "
            "Warm-up transient at 10 rps inflates KS to 0.55 (first ~200 requests are slow while "
            "Python cold-starts). Capacity knee ~90-100 rps."
        ),
    },
    "java_dsp_1c": {
        "title": "Java DSP-AES Pipeline (ThreadPoolExecutor 4 Threads, 1 Core)",
        "arch": "Java HttpServer + ThreadPoolExecutor(4, 4, ...) on one CPU core (M/G/4)",
        "service": "AES-256-CBC (JCE HW acceleration) + 64-tap FIR + re-encrypt (~0.17-0.29ms)",
        "model": "M/G/4 — `logs and des/multi_server_des.py --workers 4`",
        "port": 8086, "cores": 1,
        "compose_svc": "java-dsp",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8086/process --rate 100 --duration 90",
        "des_script": "multi_server_des.py --workers 4",
        "notes": (
            "JVM JIT warm-up creates a bimodal service time distribution: "
            "first ~200 requests run 5-10x slower (interpreter mode). "
            "KS=0.748 at 25 rps drops to ~0.18 after warm-up. "
            "Hardware AES acceleration via JCE makes this the fastest pipeline per-thread (~0.2ms). "
            "Very high theoretical capacity (>2000 rps on 4 threads); practical limit set by "
            "client connection pool."
        ),
    },
    "go_sqlite_1c": {
        "title": "Go SQLite I/O Server (1 Worker, 1 Core)",
        "arch": "Single goroutine FCFS channel queue — I/O-bound service (M/G/1)",
        "service": "SQLite INSERT sensor reading + SELECT last 20 rows (~10ms per request)",
        "model": "M/G/1 — `logs and des/single_server_des.py`",
        "port": 8087, "cores": 1,
        "compose_svc": "go-sqlite",
        "load_cmd": "python sqlite_load.py --url http://localhost:8087/process --rate 25 --duration 90",
        "des_script": "single_server_des.py",
        "notes": (
            "Tests whether DES accuracy depends on service type (I/O vs CPU). "
            "Conclusion: no — the same failure pattern occurs as CPU-bound servers. "
            "DES is accurate at rho=0.27 (KS=0.15) but degrades above rho=0.5 as "
            "WAL write-lock stall variance grows load-dependently. "
            "Capacity knee ~75-90 rps; 10x slower than Go lognormal due to SQLite I/O."
        ),
    },
    "node_dsp_3c": {
        "title": "Node.js DSP-AES Pipeline (3-Worker Cluster, 3 Cores)",
        "arch": "Node.js cluster module — 3 independent event-loop processes on 3 CPU cores (M/G/3)",
        "service": "Same AES-FIR-AES as node_dsp_1c, ~0.75ms per worker process",
        "model": "M/G/3 — `logs and des/multi_server_des.py --workers 3`",
        "port": 8088, "cores": 3,
        "compose_svc": "node-dsp-mc",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8088/process --rate 400 --duration 90",
        "des_script": "multi_server_des.py --workers 3",
        "notes": (
            "Throughput ~2.3x single-worker due to OS connection-dispatch overhead on shared port. "
            "High KS (0.66-0.81) at all rates — OS process-scheduling latency when switching between "
            "cluster workers creates a bimodal service time distribution that the M/G/3 lognormal "
            "model cannot capture. Adding cores does not fix the root cause (distribution mismatch)."
        ),
    },
    "python_dsp_3c": {
        "title": "Python/Gunicorn DSP-AES Pipeline (3 Workers, 3 Cores)",
        "arch": "Gunicorn --workers 3 on 3 independent CPU cores (M/G/3)",
        "service": "Same AES-FIR-AES pipeline as python_dsp_1c, ~10ms per worker",
        "model": "M/G/3 — `logs and des/multi_server_des.py --workers 3`",
        "port": 8089, "cores": 3,
        "compose_svc": "python-dsp-mc",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8089/process --rate 100 --duration 90",
        "des_script": "multi_server_des.py --workers 3",
        "notes": (
            "Best M/G/c DES result in the entire study: KS=0.039-0.053 at 100-300 rps. "
            "Near-perfect accuracy achieved because: (a) near-constant service time (CV~0.05) "
            "matches lognormal model closely, (b) c=3 correctly accounts for 3 independent workers, "
            "(c) no shared state contention between Gunicorn processes. "
            "Throughput scales linearly at 3.0x. Capacity knee ~270 rps (vs ~90 for 1-worker)."
        ),
    },
    "java_dsp_3c": {
        "title": "Java DSP-AES Pipeline (3-Thread Pool, 3 Cores)",
        "arch": "Java ThreadPoolExecutor(3, 3, ...) on 3 CPU cores (M/G/3)",
        "service": "Same AES-FIR-AES pipeline as java_dsp_1c, ~0.2ms per thread",
        "model": "M/G/3 — `logs and des/multi_server_des.py --workers 3`",
        "port": 8090, "cores": 3,
        "compose_svc": "java-dsp-mc",
        "load_cmd": "python dsp_aes_load.py --url http://localhost:8090/process --rate 200 --duration 90",
        "des_script": "multi_server_des.py --workers 3",
        "notes": (
            "Comparable accuracy to java_dsp_1c (KS~0.16-0.34). "
            "JVM JIT warm-up bimodality remains the binding constraint — changing from 4 to 3 threads "
            "does not reduce the GC/JIT variance in service times. "
            "Client-limited at tested rates; server capacity is well above 2000 rps."
        ),
    },
    "go_sqlite_3c": {
        "title": "Go SQLite I/O Server (3 Workers, WAL Mode, 3 Cores)",
        "arch": "3 goroutine workers sharing SQLite with WAL journal mode (M/G/3)",
        "service": "Same INSERT+SELECT as go_sqlite_1c, WAL-serialised writes (~4.6ms per request)",
        "model": "M/G/3 — `logs and des/multi_server_des.py --workers 3`",
        "port": 8091, "cores": 3,
        "compose_svc": "go-sqlite-mc",
        "load_cmd": "python sqlite_load.py --url http://localhost:8091/process --rate 100 --duration 90",
        "des_script": "multi_server_des.py --workers 3",
        "notes": (
            "SQLite WAL mode allows concurrent reads but serialises writes. "
            "With 3 goroutines all doing INSERT, effective concurrency is limited by write serialisation. "
            "Service time halves to ~4.6ms (vs ~10ms single-worker) — not a speedup, but a reflection "
            "of shorter individual critical sections when writes are interleaved. "
            "Effective throughput ~2.2x (not 3x). KS improves at high rho as WAL queueing "
            "matches M/G/3 prediction."
        ),
    },
}


def fmt(s, decimals=3):
    try:
        return f"{float(s):.{decimals}f}"
    except Exception:
        return str(s)


for folder_name, meta in META.items():
    folder = os.path.join(BASE, folder_name)
    if not os.path.isdir(folder):
        continue

    # Load summary CSV
    summary_rows = []
    for fn in sorted(os.listdir(folder)):
        if fn.endswith("_summary.csv"):
            with open(os.path.join(folder, fn), newline="") as f:
                summary_rows = list(csv.DictReader(f))
            break

    # Build results table
    table_lines = [
        "| Rate (rps) | n | rho | svc p50 (ms) | resp p50 (ms) | resp p99 (ms) | KS replay | KS bootstrap | KS parametric |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in summary_rows:
        rate = int(float(r["rate_rps"]))
        n    = int(float(r["n_obs"]))
        rho  = fmt(r["rho"])
        msvc = fmt(r["mean_svc_ms"])
        p50  = fmt(r["p50_resp_ms"])
        p99  = fmt(r["p99_resp_ms"])
        ks_r = fmt(r["ks_replay"])
        ks_b = fmt(r["ks_bootstrap"])
        ks_p = fmt(r["ks_parametric"])
        table_lines.append(
            f"| {rate} | {n:,} | {rho} | {msvc} | {p50} | {p99} | {ks_r} | {ks_b} | {ks_p} |"
        )
    table = "\n".join(table_lines)

    cpuset = "0,1,2" if meta["cores"] == 3 else "0"
    mem    = "512m" if meta["cores"] == 3 else "256m"
    workers_flag = f" --workers {meta['cores']}" if meta["cores"] > 1 else ""

    readme = f"""# {meta["title"]}

## Experimental Design

| Parameter | Value |
|---|---|
| Architecture | {meta["arch"]} |
| Service pipeline | {meta["service"]} |
| DES model | {meta["model"]} |
| CPU cores | {meta["cores"]} (`cpuset={cpuset}`, `cpus={meta["cores"]}.0`) |
| Memory limit | {mem} |
| Port | {meta["port"]} |
| Sweep duration | 90 s per rate point |
| Load seed | 42 |

## Results

{table}

![Response-time CDFs: observed vs DES](cdf.png)

## Interpretation

{meta["notes"]}

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
docker compose up -d {meta["compose_svc"]}

# 2. Run one load step (adjust --rate)
{meta["load_cmd"]}

# 3. Run DES on the collected trace
python "logs and des/{meta["des_script"]}" \\
  --input experiments/{folder_name}/<trace_file>.csv \\
  --mode replay --output des_out.csv{workers_flag}

# 4. Re-run all DES modes and regenerate summary + CDF
python run_des_all.py --servers {folder_name}
python plot_all_cdfs.py {folder_name}
```
"""
    out_path = os.path.join(folder, "README.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"wrote {out_path}")
