"""
run_des_all.py — Runs M/G/1 or M/G/c DES on every collected trace and
organises results into per-server experiment folders.

Folder layout created:
  results/
    go_1c/          — original Go lognormal sleep server (1 worker)
    apache_msg_1c/  — Apache message-processing server
    apache_dsp_1c/  — Apache DSP-AES server
    node_dsp_1c/    — Node.js DSP-AES server (1 event loop)
    python_dsp_1c/  — Python/Gunicorn DSP-AES server (1 worker)
    java_dsp_1c/    — Java ThreadPoolExecutor (4 workers → M/G/4)
    go_sqlite_1c/   — Go SQLite server (1 worker)
    node_dsp_3c/    — Node.js cluster (3 workers → M/G/3)
    python_dsp_3c/  — Python Gunicorn (3 workers → M/G/3)
    java_dsp_3c/    — Java ThreadPoolExecutor (3 workers → M/G/3)
    go_sqlite_3c/   — Go SQLite (3 workers → M/G/3)

For each server/rate:
  - copies the raw trace CSV
  - runs DES (replay, bootstrap, parametric) and saves simulated CSVs
  - prints KS distances

Usage:
  python run_des_all.py [--servers go apache_dsp node_dsp ...]
  python run_des_all.py          # all servers
"""
import argparse
import os
import shutil
import subprocess
import sys
import csv
import math

BASE     = os.path.dirname(os.path.abspath(__file__))
EXP_DIR  = os.path.join(BASE, "logs and des", "experiments")
RES_DIR  = os.path.join(BASE, "results")
DES1     = os.path.join(BASE, "logs and des", "single_server_des.py")
DESC     = os.path.join(BASE, "logs and des", "multi_server_des.py")

# ---------------------------------------------------------------------------
# Server specs: (result_folder, workers_for_DES, source_csv_prefix)
# ---------------------------------------------------------------------------
SERVER_SPECS = {
    # Single-worker servers → M/G/1 DES
    "go":           ("go_1c",         1, "go_"),
    "apache_msg":   ("apache_msg_1c", 1, "apache_"),
    "apache_dsp":   ("apache_dsp_1c", 1, "dsp_aes_"),
    "node_dsp":     ("node_dsp_1c",   1, "node_dsp_"),
    "python_dsp":   ("python_dsp_1c", 1, "python_dsp_"),
    "java_dsp":     ("java_dsp_1c",   4, "java_dsp_"),    # JAVA_WORKERS=4
    "go_sqlite":    ("go_sqlite_1c",  1, "go_sqlite_"),

    # Multi-worker servers → M/G/c DES (c=3)
    "node_dsp_mc":  ("node_dsp_3c",   3, "node_dsp_mc_"),
    "python_dsp_mc":("python_dsp_3c", 3, "python_dsp_mc_"),
    "java_dsp_mc":  ("java_dsp_3c",   3, "java_dsp_mc_"),
    "go_sqlite_mc": ("go_sqlite_3c",  3, "go_sqlite_mc_"),
}


def pct(arr, p):
    if not arr:
        return float('nan')
    idx = p / 100 * (len(arr) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return arr[lo] if lo == hi else arr[lo] * (1 - (idx - lo)) + arr[hi] * (idx - lo)


def ks_distance(a, b):
    """KS distance between two sorted lists (CDFs)."""
    if not a or not b:
        return float('nan')
    all_vals = sorted(set(a) | set(b))
    fa = [sum(1 for x in a if x <= v) / len(a) for v in all_vals]
    fb = [sum(1 for x in b if x <= v) / len(b) for v in all_vals]
    return max(abs(x - y) for x, y in zip(fa, fb))


def read_response_ms(path):
    rows = []
    try:
        with open(path, newline='') as f:
            for row in csv.DictReader(f):
                if row.get('status_code', '200') == '200':
                    rows.append(float(row['response_ms']))
    except Exception as e:
        print(f"    [WARN] could not read {path}: {e}")
    return sorted(rows)


def run_des(trace_path, out_prefix, workers, mode, dist='lognormal'):
    """Run DES and return path of simulated CSV."""
    des_script = DESC if workers > 1 else DES1
    out_path = f"{out_prefix}_{mode}.csv"
    cmd = [
        sys.executable, des_script,
        "--input", trace_path,
        "--mode", mode,
        "--output", out_path,
        "--seed", "42",
    ]
    if workers > 1:
        cmd += ["--workers", str(workers)]
    if mode == "parametric":
        cmd += ["--dist", dist]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        print(f"    [ERR] DES failed: {e}")
        return None
    return out_path if os.path.exists(out_path) else None


def process_server(server_tag):
    folder_name, workers, prefix = SERVER_SPECS[server_tag]
    out_dir = os.path.join(RES_DIR, folder_name)
    os.makedirs(out_dir, exist_ok=True)

    # Find all rate CSVs for this server
    traces = []
    for fn in sorted(os.listdir(EXP_DIR)):
        if fn.startswith(prefix) and fn.endswith("rps.csv"):
            rate_str = fn[len(prefix):-len("rps.csv")]
            try:
                rate = int(rate_str)
                traces.append((rate, os.path.join(EXP_DIR, fn)))
            except ValueError:
                pass

    if not traces:
        print(f"  [{server_tag}] no trace files found (prefix={prefix})")
        return

    print(f"\n{'='*60}")
    print(f"  {server_tag}  ({folder_name})  workers={workers}")
    print(f"{'='*60}")

    summary_rows = []

    for rate, trace_path in sorted(traces):
        observed = read_response_ms(trace_path)
        if len(observed) < 50:
            print(f"  {rate:4d} rps: too few rows ({len(observed)}), skipping")
            continue

        # Copy raw trace to result folder
        dest_trace = os.path.join(out_dir, f"{server_tag}_{rate}rps.csv")
        shutil.copy2(trace_path, dest_trace)

        # Run DES modes
        out_prefix = os.path.join(out_dir, f"{server_tag}_{rate}rps_des")

        replay_path = run_des(trace_path, out_prefix, workers, "replay")
        bootstrap_path = run_des(trace_path, out_prefix, workers, "bootstrap")
        parametric_path = run_des(trace_path, out_prefix, workers, "parametric")

        # Compute KS distances
        def ks(sim_path):
            if not sim_path:
                return float('nan')
            sim = read_response_ms(sim_path)
            return ks_distance(observed, sim) if sim else float('nan')

        ks_r = ks(replay_path)
        ks_b = ks(bootstrap_path)
        ks_p = ks(parametric_path)

        with open(trace_path, newline='') as _f:
            svc = sorted(
                float(row['service_ms'])
                for row in csv.DictReader(_f)
                if row.get('status_code', '200') == '200'
            )
        mean_svc = sum(svc) / len(svc) if svc else float('nan')
        util = rate * mean_svc / 1000 / workers  # rho (fraction, per worker)

        print(f"  {rate:4d} rps  n={len(observed):6d}  rho={util:.3f}  "
              f"KS: replay={ks_r:.3f}  bootstrap={ks_b:.3f}  parametric={ks_p:.3f}  "
              f"svc_p50={pct(svc,50):.2f}ms")

        summary_rows.append({
            "rate_rps": rate, "n_obs": len(observed), "rho": util,
            "mean_svc_ms": mean_svc,
            "p50_resp_ms": pct(observed, 50), "p95_resp_ms": pct(observed, 95),
            "p99_resp_ms": pct(observed, 99),
            "ks_replay": ks_r, "ks_bootstrap": ks_b, "ks_parametric": ks_p,
        })

    # Write summary CSV
    if summary_rows:
        summary_path = os.path.join(out_dir, f"{server_tag}_summary.csv")
        with open(summary_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)
        print(f"  Summary -> {summary_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--servers', nargs='*', choices=list(SERVER_SPECS.keys()),
                    help='Servers to process (default: all)')
    args = ap.parse_args()

    servers = args.servers or list(SERVER_SPECS.keys())

    os.makedirs(RES_DIR, exist_ok=True)

    for server in servers:
        try:
            process_server(server)
        except Exception as e:
            print(f"  [ERR] {server}: {e}")

    print(f"\nDone. Results in: {RES_DIR}")


if __name__ == '__main__':
    main()
