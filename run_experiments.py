"""
Automated rate-sweep experiment runner.
Runs Go app and Apache at 50/100/200/400 rps, saves per-rate CSVs,
runs DES on each Go trace, and prints a consolidated summary table.
"""
import csv
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

RATES = [50, 100, 200, 400]
DURATION = 90          # seconds per step
CSV_PATH = Path("logs and des/requests.csv")
RESULTS_DIR = Path("logs and des/experiments")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# -- helpers ------------------------------------------------------------------

def run(cmd, **kw):
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, **kw)


def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = p / 100 * (len(s) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    return s[lo] * (1 - (idx - lo)) + s[hi] * (idx - lo)


def summarise(vals):
    if not vals:
        return {}
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "p50": pct(vals, 50),
        "p95": pct(vals, 95),
        "p99": pct(vals, 99),
        "max": max(vals),
    }

# -- Go CSV slice extraction ---------------------------------------------------

def csv_row_count():
    if not CSV_PATH.exists():
        return 0
    with CSV_PATH.open() as f:
        return sum(1 for _ in f)


def extract_csv_slice(start_line, out_path):
    """Extract rows [start_line .. end] from requests.csv (1-indexed, header=1)."""
    with CSV_PATH.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        with out_path.open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(header)
            for i, row in enumerate(reader, start=2):
                if i >= start_line:
                    writer.writerow(row)


def analyse_go_csv(path):
    rows = []
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status_code") == "200":
                rows.append(r)
    if not rows:
        return {}
    resp = [float(r["response_ms"]) for r in rows]
    svc  = [float(r["service_ms"]) for r in rows]
    que  = [float(r["queue_ms"])   for r in rows]
    # throughput: requests / elapsed wall time
    arrivals = [int(r["arrival_unix_ns"]) for r in rows]
    span_s = (max(arrivals) - min(arrivals)) / 1e9 if len(arrivals) > 1 else DURATION
    return {
        "n": len(rows),
        "throughput": len(rows) / span_s,
        "response": summarise(resp),
        "service":  summarise(svc),
        "queue":    summarise(que),
    }

# -- DES run -------------------------------------------------------------------

def run_des(input_csv, output_csv, mode="bootstrap"):
    result = run(
        [sys.executable, "logs and des/single_server_des.py",
         "--input", str(input_csv),
         "--output", str(output_csv),
         "--mode", mode,
         "--seed", "42"],
        capture_output=True, text=True
    )
    ks_resp = ks_queue = None
    for line in result.stdout.splitlines():
        if line.startswith("ks_like_response="):
            ks_resp = float(line.split("=")[1])
        elif line.startswith("ks_like_queue="):
            ks_queue = float(line.split("=")[1])
    return ks_resp, ks_queue, result.stdout


# -- Apache load (inline Poisson POST+GET) ------------------------------------

def run_apache_rate(rate, duration):
    result = run(
        [sys.executable, "apache_load.py",
         "--rate", str(rate),
         "--duration", str(duration),
         "--post-ratio", "0.3",
         "--seed", "42"],
        capture_output=True, text=True
    )
    out = result.stdout
    stats = {"rate": rate, "raw": out}
    for line in out.splitlines():
        if line.startswith("sent="):
            parts = dict(p.split("=") for p in line.split())
            stats["sent"] = int(parts.get("sent", 0))
            stats["ok"]   = int(parts.get("ok", 0))
            stats["err"]  = int(parts.get("err", 0))
            stats["achieved_rps"] = float(parts.get("achieved_rps", 0))
        if line.startswith("ALL"):
            toks = line.split()
            def grab(key):
                for t in toks:
                    if t.startswith(key + "="):
                        return float(t.split("=")[1].rstrip("ms"))
                return float("nan")
            stats["p50_ms"] = grab("p50")
            stats["p95_ms"] = grab("p95")
            stats["p99_ms"] = grab("p99")
    return stats


# -- main ----------------------------------------------------------------------

def main():
    go_results    = {}
    apache_results = {}

    # -- Phase 1: Go rate sweep ----------------------------------------------
    print("\n" + "="*60)
    print("PHASE 1: Go app rate sweep (port 8080)")
    print("="*60)

    # Warm-up: 5s at 10 rps to trigger calibration
    print("\n[warmup] 10 rps for 10s ...")
    run([sys.executable, "poisson_load_generator.py",
         "--url", "http://localhost:8080/",
         "--rate", "10", "--duration", "10"],
        capture_output=True)
    time.sleep(2)

    for rate in RATES:
        print(f"\n-- Go {rate} rps - {DURATION}s --")
        before = csv_row_count()  # row number of last existing line
        start_line = before + 1  # first new data row (header is line 1)

        result = run(
            [sys.executable, "poisson_load_generator.py",
             "--url", "http://localhost:8080/",
             "--rate", str(rate),
             "--duration", str(DURATION)],
            capture_output=True, text=True
        )
        print(result.stdout.strip())

        # Wait for CSV flush (flush interval = 1s)
        time.sleep(2)
        after = csv_row_count()
        print(f"  CSV rows before={before} after={after} new={after - before}")

        slice_path = RESULTS_DIR / f"go_{rate}rps.csv"
        extract_csv_slice(start_line, slice_path)
        analysis = analyse_go_csv(slice_path)

        # DES bootstrap
        des_out = RESULTS_DIR / f"go_{rate}rps_des_bootstrap.csv"
        ks_resp, ks_queue, des_stdout = run_des(slice_path, des_out, "bootstrap")
        print(f"  DES ks_response={ks_resp:.4f}  ks_queue={ks_queue:.4f}")

        go_results[rate] = {
            "analysis": analysis,
            "ks_resp":  ks_resp,
            "ks_queue": ks_queue,
            "load_out": result.stdout.strip(),
            "des_stdout": des_stdout.strip(),
        }

    # -- Phase 2: Apache rate sweep ------------------------------------------
    print("\n" + "="*60)
    print("PHASE 2: Apache rate sweep (port 8082)")
    print("="*60)

    # Warm-up
    print("\n[warmup] 10 rps for 10s ...")
    run([sys.executable, "apache_load.py",
         "--rate", "10", "--duration", "10", "--seed", "1"],
        capture_output=True)
    time.sleep(2)

    for rate in RATES:
        print(f"\n-- Apache {rate} rps - {DURATION}s --")
        stats = run_apache_rate(rate, DURATION)
        print(stats.get("raw", "").strip())
        apache_results[rate] = stats

    # -- Summary table -------------------------------------------------------
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    print("\n-- Go app (service_ms / response_ms / queue_ms) --")
    print(f"{'rate':>6}  {'n':>6}  {'tput':>7}  "
          f"{'svc_p50':>8}  {'svc_p99':>8}  "
          f"{'resp_p50':>9}  {'resp_p95':>9}  {'resp_p99':>9}  "
          f"{'q_p99':>8}  {'ks_resp':>8}  {'ks_q':>7}")
    for rate in RATES:
        r = go_results[rate]
        a = r["analysis"]
        if not a:
            print(f"{rate:>6}  NO DATA")
            continue
        print(f"{rate:>6}  {a['n']:>6}  {a['throughput']:>7.1f}  "
              f"{a['service']['p50']:>8.2f}  {a['service']['p99']:>8.2f}  "
              f"{a['response']['p50']:>9.2f}  {a['response']['p95']:>9.2f}  {a['response']['p99']:>9.2f}  "
              f"{a['queue']['p99']:>8.2f}  {r['ks_resp']:>8.4f}  {r['ks_queue']:>7.4f}")

    print("\n-- Apache (client-side latency, mixed 70% GET / 30% POST) --")
    print(f"{'rate':>6}  {'sent':>6}  {'ok':>6}  {'err':>5}  {'ach_rps':>8}  "
          f"{'p50_ms':>8}  {'p95_ms':>8}  {'p99_ms':>8}  {'err%':>6}")
    for rate in RATES:
        s = apache_results[rate]
        sent = s.get("sent", 0)
        err  = s.get("err", 0)
        err_pct = 100 * err / sent if sent else 0
        print(f"{rate:>6}  {sent:>6}  {s.get('ok',0):>6}  {err:>5}  "
              f"{s.get('achieved_rps', 0):>8.1f}  "
              f"{s.get('p50_ms', float('nan')):>8.1f}  "
              f"{s.get('p95_ms', float('nan')):>8.1f}  "
              f"{s.get('p99_ms', float('nan')):>8.1f}  "
              f"{err_pct:>6.1f}%")

    # Save full results as JSON
    out = {
        "go": {str(k): v for k, v in go_results.items()},
        "apache": {str(k): v for k, v in apache_results.items()},
    }
    # make json-serialisable (some values are Path objects etc)
    json_path = RESULTS_DIR / "experiment_results.json"
    with json_path.open("w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nFull results saved to {json_path}")


if __name__ == "__main__":
    main()
