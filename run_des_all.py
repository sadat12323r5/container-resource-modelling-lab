"""
run_des_all.py — Runs M/G/1 or M/G/c DES on every trace in experiments/<folder>/
and writes DES output CSVs + summary CSV back into the same folder.

Reads traces directly from experiments/<folder>/ (no intermediate staging directory).
Running this script is idempotent: it skips DES files that already exist and
overwrites the summary CSV.

Usage:
  python run_des_all.py                       # all server folders
  python run_des_all.py --servers go node_dsp # specific servers
"""
import argparse
import csv
import os
import subprocess
import sys

from des_utils import pct, ks_distance, read_response_ms

BASE     = os.path.dirname(os.path.abspath(__file__))
EXP_BASE = os.path.join(BASE, "experiments")
DES1     = os.path.join(BASE, "logs and des", "single_server_des.py")
DESC     = os.path.join(BASE, "logs and des", "multi_server_des.py")

# ---------------------------------------------------------------------------
# Server specs: (folder_name, workers_for_DES, file_tag_prefix)
#   folder_name  — subdirectory under experiments/
#   workers      — c value for M/G/c DES (1 = M/G/1)
#   prefix       — filename prefix for trace CSVs in that folder
# ---------------------------------------------------------------------------
SERVER_SPECS = {
    # Single-worker servers — M/G/1 DES
    "go":           ("go_1c",          1, "go_"),
    "apache_msg":   ("apache_msg_1c",  1, "apache_msg_"),
    "apache_dsp":   ("apache_dsp_1c",  1, "apache_dsp_"),
    "node_dsp":     ("node_dsp_1c",    1, "node_dsp_"),
    "python_dsp":   ("python_dsp_1c",  1, "python_dsp_"),
    "java_dsp":     ("java_dsp_1c",    4, "java_dsp_"),   # JAVA_WORKERS=4
    "go_sqlite":    ("go_sqlite_1c",   1, "go_sqlite_"),

    # Multi-worker servers — M/G/c DES (c=3)
    "node_dsp_mc":   ("node_dsp_3c",   3, "node_dsp_mc_"),
    "python_dsp_mc": ("python_dsp_3c", 3, "python_dsp_mc_"),
    "java_dsp_mc":   ("java_dsp_3c",   3, "java_dsp_mc_"),
    "go_sqlite_mc":  ("go_sqlite_3c",  3, "go_sqlite_mc_"),
}



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
    exp_dir = os.path.join(EXP_BASE, folder_name)
    os.makedirs(exp_dir, exist_ok=True)

    # Find all rate CSVs for this server (already in experiments/<folder>/)
    traces = []
    for fn in sorted(os.listdir(exp_dir)):
        if fn.startswith(prefix) and fn.endswith("rps.csv") and "_des_" not in fn:
            rate_str = fn[len(prefix):-len("rps.csv")]
            try:
                rate = int(rate_str)
                traces.append((rate, os.path.join(exp_dir, fn)))
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

        # Run DES modes (traces already in experiments/<folder>/)
        out_prefix = os.path.join(exp_dir, f"{prefix}{rate}rps_des")

        replay_path = run_des(trace_path, out_prefix, workers, "replay")
        bootstrap_path = run_des(trace_path, out_prefix, workers, "bootstrap")
        parametric_path = run_des(trace_path, out_prefix, workers, "parametric")

        # Compute KS distances (ks_distance returns (d, best_x); we only need d)
        def ks(sim_path):
            if not sim_path:
                return float('nan')
            sim = read_response_ms(sim_path)
            return ks_distance(observed, sim)[0] if sim else float('nan')

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
        summary_path = os.path.join(exp_dir, f"{server_tag}_summary.csv")
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

    for server in servers:
        try:
            process_server(server)
        except Exception as e:
            print(f"  [ERR] {server}: {e}")

    print(f"\nDone. Results in: {EXP_BASE}")


if __name__ == '__main__':
    main()
