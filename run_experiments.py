"""
Automated rate-sweep experiment runner.

Phase 1 - Go app coarse sweep:  50, 100, 200, 400 rps x 90s
Phase 2 - Go app fine sweep:   150, 175, 200, 225, 250 rps x 90s (around the knee)
Phase 3 - Apache sweep:  10, 25, 50 rps x 90s (Apache saturates at ~30 rps)

For each Go rate, runs DES in three modes (bootstrap, replay, parametric) with
the 0.006 ms goroutine-scheduling floor subtracted from observed queue_ms before
the KS comparison.

Outputs:
  logs and des/experiments/go_<rate>rps.csv                 per-rate trace slices
  logs and des/experiments/go_<rate>rps_des_<mode>.csv      DES output per mode
  logs and des/experiments/experiment_results.json           full consolidated JSON
"""
import csv
import json
import math
import subprocess
import sys
import time
from pathlib import Path

COARSE_RATES        = [50, 100, 200, 400]
FINE_RATES          = [150, 175, 200, 225, 250]
# Apache saturates at ~30 rps due to file-lock contention in mpm_prefork.
# Sweep only below the knee; 50 rps is already above saturation so we capture
# the transition from stable to saturated in the 10-25 rps range.
APACHE_RATES        = [10, 25, 50]
DURATION          = 90        # seconds per step
QUEUE_OFFSET      = 0.006     # ms goroutine-scheduling floor to subtract before KS
GO_QUEUE_CAPACITY = 1024      # matches JOB_QUEUE channel buffer in server_single/main.go
DES_MODES    = ["bootstrap", "replay", "parametric"]
DES_SEED     = 42

CSV_PATH        = Path("logs and des/requests.csv")
APACHE_CSV_PATH = Path("logs and des/apache_requests.csv")
RESULTS_DIR     = Path("logs and des/experiments")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        "n":    len(vals),
        "mean": sum(vals) / len(vals),
        "p50":  pct(vals, 50),
        "p95":  pct(vals, 95),
        "p99":  pct(vals, 99),
        "max":  max(vals),
    }


# ---------------------------------------------------------------------------
# CSV slicing
# ---------------------------------------------------------------------------

def csv_row_count():
    if not CSV_PATH.exists():
        return 0
    with CSV_PATH.open() as f:
        return sum(1 for _ in f)


def extract_csv_slice(start_line, out_path):
    """Copy rows from start_line onward (1-indexed; row 1 = header)."""
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
            if str(r.get("status_code", "")).startswith("2"):
                rows.append(r)
    if not rows:
        return {}
    resp = [float(r["response_ms"]) for r in rows]
    svc  = [float(r["service_ms"])  for r in rows]
    que  = [float(r["queue_ms"])    for r in rows]
    arrivals = [int(r["arrival_unix_ns"]) for r in rows]
    span_s = (max(arrivals) - min(arrivals)) / 1e9 if len(arrivals) > 1 else DURATION
    return {
        "n":          len(rows),
        "throughput": len(rows) / span_s,
        "response":   summarise(resp),
        "service":    summarise(svc),
        "queue":      summarise(que),
    }


# ---------------------------------------------------------------------------
# DES runner
# ---------------------------------------------------------------------------

def run_des(input_csv, output_csv, mode, queue_capacity=None):
    cmd = [sys.executable, "logs and des/single_server_des.py",
           "--input",        str(input_csv),
           "--output",       str(output_csv),
           "--mode",         mode,
           "--seed",         str(DES_SEED),
           "--queue-offset", str(QUEUE_OFFSET)]
    if queue_capacity is not None:
        cmd += ["--queue-capacity", str(queue_capacity)]
    result = run(cmd, capture_output=True, text=True)
    ks_resp = ks_q_raw = ks_q_corr = None
    n_dropped = 0
    drop_rate = 0.0
    for line in result.stdout.splitlines():
        if line.startswith("ks_like_response="):
            ks_resp = float(line.split("=")[1])
        elif line.startswith("ks_like_queue_corrected="):
            ks_q_corr = float(line.split("=")[1])
        elif line.startswith("ks_like_queue="):
            ks_q_raw = float(line.split("=")[1])
        elif line.startswith("n_total="):
            parts = dict(p.split("=") for p in line.split())
            n_dropped = int(parts.get("n_dropped", 0))
            drop_rate = float(parts.get("drop_rate", 0.0))
    return ks_resp, ks_q_raw, ks_q_corr, n_dropped, drop_rate, result.stdout


def run_go_rate(rate, label=None):
    """Run one Go rate step: load, slice CSV, run all DES modes. Return results dict."""
    tag = label or f"{rate}rps"
    print(f"\n-- Go {rate} rps ({tag}) x {DURATION}s --")

    before = csv_row_count()
    start_line = before + 1

    result = run(
        [sys.executable, "poisson_load_generator.py",
         "--url",      "http://localhost:8080/",
         "--rate",     str(rate),
         "--duration", str(DURATION)],
        capture_output=True, text=True
    )
    print(result.stdout.strip())

    time.sleep(2)  # wait for CSV flush
    after = csv_row_count()
    print(f"  CSV rows before={before} after={after} new={after - before}")

    slice_path = RESULTS_DIR / f"go_{tag}.csv"
    extract_csv_slice(start_line, slice_path)
    analysis = analyse_go_csv(slice_path)

    des = {}
    for mode in DES_MODES:
        des_out = RESULTS_DIR / f"go_{tag}_des_{mode}.csv"
        ks_r, ks_q_raw, ks_q_corr, n_dropped, drop_rate, stdout = run_des(
            slice_path, des_out, mode, queue_capacity=GO_QUEUE_CAPACITY
        )
        des[mode] = {
            "ks_resp":       ks_r,
            "ks_queue_raw":  ks_q_raw,
            "ks_queue_corr": ks_q_corr,
            "n_dropped":     n_dropped,
            "drop_rate":     drop_rate,
            "stdout":        stdout.strip(),
        }
        drop_str = f"  drop={n_dropped}({drop_rate:.1%})" if n_dropped else ""
        print(f"  [{mode:>11}] ks_resp={ks_r:.4f}  "
              f"ks_q_raw={ks_q_raw:.4f}  ks_q_corr={ks_q_corr:.4f}{drop_str}")

    return {"rate": rate, "analysis": analysis, "des": des, "load_out": result.stdout.strip()}


# ---------------------------------------------------------------------------
# Apache CSV helpers (mirrors Go CSV helpers but for apache_requests.csv)
# ---------------------------------------------------------------------------

def apache_csv_row_count():
    if not APACHE_CSV_PATH.exists():
        return 0
    with APACHE_CSV_PATH.open() as f:
        return sum(1 for _ in f)


def extract_apache_csv_slice(start_line, out_path):
    """Copy rows from start_line onward (1-indexed; row 1 = header)."""
    with APACHE_CSV_PATH.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        with out_path.open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(header)
            for i, row in enumerate(reader, start=2):
                if i >= start_line:
                    writer.writerow(row)


# ---------------------------------------------------------------------------
# Apache runner
# ---------------------------------------------------------------------------

def run_apache_rate(rate):
    """Run one Apache rate step: load, slice CSV, run all DES modes. Return results dict."""
    print(f"\n-- Apache {rate} rps x {DURATION}s --")

    before     = apache_csv_row_count()
    start_line = before + 1

    result = run(
        [sys.executable, "apache_load.py",
         "--rate",        str(rate),
         "--duration",    str(DURATION),
         "--post-ratio",  "0.3",
         "--seed",        "42"],
        capture_output=True, text=True
    )
    print(result.stdout.strip())

    time.sleep(2)
    after = apache_csv_row_count()
    print(f"  CSV rows before={before} after={after} new={after - before}")

    slice_path = RESULTS_DIR / f"apache_{rate}rps.csv"
    extract_apache_csv_slice(start_line, slice_path)
    analysis = analyse_go_csv(slice_path)   # same schema as Go

    # No goroutine scheduling floor and no queue-capacity model for Apache
    des = {}
    for mode in DES_MODES:
        des_out = RESULTS_DIR / f"apache_{rate}rps_des_{mode}.csv"
        ks_r, ks_q_raw, ks_q_corr, _, _, stdout = run_des(slice_path, des_out, mode)
        des[mode] = {
            "ks_resp":       ks_r,
            "ks_queue_raw":  ks_q_raw,
            "ks_queue_corr": ks_q_corr,
            "stdout":        stdout.strip(),
        }
        print(f"  [{mode:>11}] ks_resp={ks_r:.4f}  "
              f"ks_q_raw={ks_q_raw:.4f}  ks_q_corr={ks_q_corr:.4f}")

    # Parse client-side summary from apache_load.py stdout
    out   = result.stdout
    stats = {"rate": rate, "raw": out, "analysis": analysis, "des": des}
    for line in out.splitlines():
        if line.startswith("sent="):
            parts = dict(p.split("=") for p in line.split())
            stats["sent"]         = int(parts.get("sent", 0))
            stats["ok"]           = int(parts.get("ok", 0))
            stats["err"]          = int(parts.get("err", 0))
            stats["achieved_rps"] = float(parts.get("achieved_rps", 0))
        if line.startswith("ALL"):
            toks = line.split()
            def grab(key, _toks=toks):
                for t in _toks:
                    if t.startswith(key + "="):
                        return float(t.split("=")[1].rstrip("ms"))
                return float("nan")
            stats["p50_ms"] = grab("p50")
            stats["p95_ms"] = grab("p95")
            stats["p99_ms"] = grab("p99")
    return stats


# ---------------------------------------------------------------------------
# Summary printers
# ---------------------------------------------------------------------------

def print_go_summary(title, results_list):
    print(f"\n{title}")
    hdr = (f"{'rate':>6}  {'n':>6}  {'tput':>6}  "
           f"{'svc_p50':>7}  {'svc_p99':>7}  "
           f"{'resp_p50':>8}  {'resp_p99':>8}  {'q_p99':>6}  "
           f"{'boot_r':>6}  {'rply_r':>6}  {'para_r':>6}  "
           f"{'boot_qc':>7}  {'rply_qc':>7}  {'para_qc':>7}  "
           f"{'drop%':>6}")
    print(hdr)
    for r in results_list:
        a = r["analysis"]
        if not a:
            print(f"{r['rate']:>6}  NO DATA")
            continue
        d = r["des"]
        # Use replay drop_rate as the representative figure (all modes see same arrivals)
        drop_pct = 100.0 * d["replay"].get("drop_rate", 0.0)
        print(
            f"{r['rate']:>6}  {a['n']:>6}  {a['throughput']:>6.1f}  "
            f"{a['service']['p50']:>7.2f}  {a['service']['p99']:>7.2f}  "
            f"{a['response']['p50']:>8.2f}  {a['response']['p99']:>8.2f}  "
            f"{a['queue']['p99']:>6.2f}  "
            f"{d['bootstrap']['ks_resp']:>6.4f}  "
            f"{d['replay']['ks_resp']:>6.4f}  "
            f"{d['parametric']['ks_resp']:>6.4f}  "
            f"{d['bootstrap']['ks_queue_corr']:>7.4f}  "
            f"{d['replay']['ks_queue_corr']:>7.4f}  "
            f"{d['parametric']['ks_queue_corr']:>7.4f}  "
            f"{drop_pct:>5.1f}%"
        )


def print_apache_summary(apache_results):
    print("\n-- Apache (70% GET /messages + 30% POST /send) --")
    print("  Internal timings (from CSV, server-side); client p50/p99 include WSL2 network (~35ms).")
    # Client-side stats
    print(f"\n  {'rate':>5}  {'sent':>6}  {'ok':>5}  {'err':>4}  {'ach_rps':>7}  "
          f"{'cli_p50':>7}  {'cli_p99':>7}  {'err%':>5}")
    for s in apache_results:
        sent    = s.get("sent", 0)
        err     = s.get("err", 0)
        err_pct = 100 * err / sent if sent else 0
        print(f"  {s['rate']:>5}  {sent:>6}  {s.get('ok',0):>5}  {err:>4}  "
              f"{s.get('achieved_rps',0):>7.1f}  "
              f"{s.get('p50_ms', float('nan')):>7.1f}  "
              f"{s.get('p99_ms', float('nan')):>7.1f}  "
              f"{err_pct:>5.1f}%")
    # Internal (server-side) stats + KS
    print(f"\n  {'rate':>5}  {'n':>5}  {'tput':>6}  "
          f"{'svc_p50':>7}  {'svc_p99':>7}  "
          f"{'resp_p50':>8}  {'resp_p99':>8}  {'q_p99':>6}  "
          f"{'boot_r':>6}  {'rply_r':>6}  {'para_r':>6}")
    for s in apache_results:
        a = s.get("analysis")
        if not a:
            print(f"  {s['rate']:>5}  NO DATA")
            continue
        d = s["des"]
        print(
            f"  {s['rate']:>5}  {a['n']:>5}  {a['throughput']:>6.1f}  "
            f"{a['service']['p50']:>7.2f}  {a['service']['p99']:>7.2f}  "
            f"{a['response']['p50']:>8.2f}  {a['response']['p99']:>8.2f}  "
            f"{a['queue']['p99']:>6.2f}  "
            f"{d['bootstrap']['ks_resp']:>6.4f}  "
            f"{d['replay']['ks_resp']:>6.4f}  "
            f"{d['parametric']['ks_resp']:>6.4f}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    coarse_results = []
    fine_results   = []
    apache_results = []

    # ---- Phase 1: Go coarse sweep ------------------------------------------
    print("\n" + "=" * 65)
    print("PHASE 1: Go app coarse sweep (50/100/200/400 rps)")
    print("=" * 65)
    print("\n[warmup] 10 rps for 10s ...")
    run([sys.executable, "poisson_load_generator.py",
         "--url", "http://localhost:8080/", "--rate", "10", "--duration", "10"],
        capture_output=True)
    time.sleep(2)

    for rate in COARSE_RATES:
        coarse_results.append(run_go_rate(rate))

    # ---- Phase 2: Go fine sweep around capacity knee -----------------------
    print("\n" + "=" * 65)
    print("PHASE 2: Go app fine sweep around capacity knee (150-250 rps)")
    print("=" * 65)
    # 200 rps already run in coarse — skip to avoid duplicating load
    for rate in FINE_RATES:
        if rate == 200:
            # reuse coarse 200rps slice for DES; just re-tag it
            existing = next(r for r in coarse_results if r["rate"] == 200)
            fine_results.append(existing)
            print(f"\n-- Go 200 rps (reusing coarse result) --")
        else:
            fine_results.append(run_go_rate(rate, label=f"{rate}rps_fine"))

    # ---- Phase 3: Apache coarse sweep --------------------------------------
    print("\n" + "=" * 65)
    print("PHASE 3: Apache sweep (10/25/50 rps)")
    print("  Note: Apache saturates at ~30 rps due to mpm_prefork file-lock")
    print("  contention. Rates kept below/at the Apache capacity knee.")
    print("=" * 65)
    # Clear the Apache CSV so rows from different rates don't mix.
    if APACHE_CSV_PATH.exists():
        APACHE_CSV_PATH.unlink()
        print(f"  Cleared {APACHE_CSV_PATH}")

    print("\n[warmup] 5 rps for 10s ...")
    run([sys.executable, "apache_load.py",
         "--rate", "5", "--duration", "10", "--seed", "1"],
        capture_output=True)
    time.sleep(2)

    for rate in APACHE_RATES:
        apache_results.append(run_apache_rate(rate))

    # ---- Summary -----------------------------------------------------------
    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    print(f"\n(queue_offset={QUEUE_OFFSET} ms subtracted from obs queue before KS)")
    print("Columns: boot_r/rply_r/para_r = KS response for bootstrap/replay/parametric")
    print("         boot_qc/rply_qc/para_qc = KS queue (scheduling-floor corrected)")

    print_go_summary("-- Go coarse sweep --", coarse_results)
    print_go_summary("-- Go fine sweep (capacity knee) --", fine_results)
    print_apache_summary(apache_results)

    # ---- Save JSON ---------------------------------------------------------
    def serialise(obj):
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(type(obj))

    json_path = RESULTS_DIR / "experiment_results.json"
    with json_path.open("w") as f:
        json.dump({
            "queue_offset_ms":    QUEUE_OFFSET,
            "go_queue_capacity":  GO_QUEUE_CAPACITY,
            "coarse": coarse_results,
            "fine":   fine_results,
            "apache": apache_results,
        }, f, indent=2, default=serialise)
    print(f"\nFull results saved to {json_path}")


if __name__ == "__main__":
    main()
