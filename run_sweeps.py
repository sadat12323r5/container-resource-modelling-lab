"""
run_sweeps.py — Orchestrates all server sweeps for the capacity thesis.

Runs each (server, rate) pair sequentially:
  1. Clears the live CSV log for that server (written by the container).
  2. Runs the appropriate load generator for DURATION seconds.
  3. Copies the result directly into experiments/<server_folder>/.

Usage:
  python run_sweeps.py [--servers node_dsp go_sqlite ...]
  python run_sweeps.py          # runs all pending sweeps
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE     = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE, "logs and des")
EXP_BASE = os.path.join(BASE, "experiments")

DSP_LOAD   = os.path.join(BASE, "dsp_aes_load.py")
SQLITE_LOAD = os.path.join(BASE, "sqlite_load.py")

AES_KEY = "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
DURATION = 90    # seconds per rate
TIMEOUT  = DURATION + 60  # subprocess timeout

# ---------------------------------------------------------------------------
# Server definitions
# ---------------------------------------------------------------------------
# Each entry: (exp_folder, file_tag, live_csv, url, kind)
#   exp_folder  — subdirectory under experiments/
#   file_tag    — prefix for trace files (e.g. "node_dsp" -> node_dsp_200rps.csv)
#   live_csv    — path that the container writes to (bind-mounted from logs and des/)
#   url         — load generator target
#   kind        — "dsp" or "sqlite"
SERVERS = {
    # Single-core servers
    "node_dsp": (
        "node_dsp_1c", "node_dsp",
        os.path.join(LOGS_DIR, "node_dsp_requests.csv"),
        "http://localhost:8084/process", "dsp",
    ),
    "go_sqlite": (
        "go_sqlite_1c", "go_sqlite",
        os.path.join(LOGS_DIR, "go_sqlite_requests.csv"),
        "http://localhost:8087/process", "sqlite",
    ),
    "java_dsp": (
        "java_dsp_1c", "java_dsp",
        os.path.join(LOGS_DIR, "java_dsp_requests.csv"),
        "http://localhost:8086/process", "dsp",
    ),
    # Multi-core servers (c=3)
    "node_dsp_mc": (
        "node_dsp_3c", "node_dsp_mc",
        os.path.join(LOGS_DIR, "node_dsp_mc_requests.csv"),
        "http://localhost:8088/process", "dsp",
    ),
    "python_dsp_mc": (
        "python_dsp_3c", "python_dsp_mc",
        os.path.join(LOGS_DIR, "python_dsp_mc_requests.csv"),
        "http://localhost:8089/process", "dsp",
    ),
    "java_dsp_mc": (
        "java_dsp_3c", "java_dsp_mc",
        os.path.join(LOGS_DIR, "java_dsp_mc_requests.csv"),
        "http://localhost:8090/process", "dsp",
    ),
    "go_sqlite_mc": (
        "go_sqlite_3c", "go_sqlite_mc",
        os.path.join(LOGS_DIR, "go_sqlite_mc_requests.csv"),
        "http://localhost:8091/process", "sqlite",
    ),
}

# Sweep rates per server (rps)
SWEEP_RATES = {
    # Node.js single event loop — ~0.5ms service → capacity ~1500+ rps
    # Run high rates to see queueing + saturation
    "node_dsp":      [200, 400, 800, 1200, 1600],

    # Go SQLite single worker — ~1ms service → capacity ~800 rps
    "go_sqlite":     [100, 200, 400, 600],

    # Java DSP already has 25,50,100,200 — add high rates
    "java_dsp":      [400, 600],

    # Multi-core (c=3) DSP servers — python ~8ms × 3 → knee at ~250-300 rps
    "node_dsp_mc":   [200, 400, 800, 1600, 2400],
    "python_dsp_mc": [25, 50, 100, 150, 200, 250, 300],
    "java_dsp_mc":   [100, 200, 400, 600, 800],
    "go_sqlite_mc":  [50, 100, 200, 400, 800],
}


CSV_HEADER = 'arrival_unix_ns,service_ms,queue_ms,response_ms,status_code\n'

def clear_csv(path: str) -> None:
    """Truncate live CSV and write header so data is clean and parseable."""
    with open(path, 'w') as f:
        f.write(CSV_HEADER)


def run_sweep(server: str, rate: float) -> bool:
    exp_folder, file_tag, live_csv, url, kind = SERVERS[server]
    out_dir = os.path.join(EXP_BASE, exp_folder)
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"{file_tag}_{int(rate)}rps.csv")

    print(f"\n{'='*60}")
    print(f"  {server}  {rate} rps  ->  experiments/{exp_folder}/")
    print(f"{'='*60}")

    clear_csv(live_csv)
    time.sleep(0.5)

    if kind == "dsp":
        cmd = [
            sys.executable, DSP_LOAD,
            "--url", url,
            "--rate", str(rate),
            "--duration", str(DURATION),
            "--aes-key", AES_KEY,
            "--seed", "42",
        ]
    else:  # sqlite
        cmd = [
            sys.executable, SQLITE_LOAD,
            "--url", url,
            "--rate", str(rate),
            "--duration", str(DURATION),
            "--seed", "42",
        ]

    try:
        subprocess.run(cmd, timeout=TIMEOUT, capture_output=False)
    except subprocess.TimeoutExpired:
        print(f"  [WARN] load generator timed out after {TIMEOUT}s")

    time.sleep(2)

    if os.path.exists(live_csv) and os.path.getsize(live_csv) > 100:
        shutil.copy2(live_csv, dest)
        n = sum(1 for _ in open(dest)) - 1
        print(f"  -> saved {n} rows to {dest}")
        return True
    else:
        print(f"  [WARN] no data written to {live_csv}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--servers', nargs='*',
                    help='Servers to sweep (default: all)',
                    choices=list(SERVERS.keys()))
    args = ap.parse_args()

    servers_to_run = args.servers or list(SWEEP_RATES.keys())

    total = sum(len(SWEEP_RATES[s]) for s in servers_to_run)
    done = 0
    t_start = time.time()

    for server in servers_to_run:
        if server not in SERVERS:
            print(f"Unknown server: {server}")
            continue
        exp_folder, file_tag, _, _, _ = SERVERS[server]
        for rate in SWEEP_RATES[server]:
            dest = os.path.join(EXP_BASE, exp_folder, f"{file_tag}_{int(rate)}rps.csv")
            if os.path.exists(dest) and os.path.getsize(dest) > 200:
                print(f"  SKIP  {server} {rate} rps  (exists, {os.path.getsize(dest)//1024}KB)")
                done += 1
                continue
            success = run_sweep(server, rate)
            done += 1
            elapsed = time.time() - t_start
            remaining = (elapsed / done) * (total - done) if done else 0
            print(f"  Progress: {done}/{total}  elapsed={elapsed/60:.1f}min  "
                  f"remaining~{remaining/60:.1f}min")

    print(f"\nAll sweeps complete in {(time.time()-t_start)/60:.1f} min")


if __name__ == '__main__':
    main()
