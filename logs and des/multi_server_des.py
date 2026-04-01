"""
multi_server_des.py — M/G/c trace-driven DES for multi-worker servers.

Models a server with c parallel workers (e.g. Apache mpm_prefork), each handling
one request at a time. All workers compete for arrivals from a shared queue.
This extends the M/G/1 single_server_des.py to M/G/c.

Key difference from M/G/1:
  - c servers run in parallel; a job starts as soon as ANY server is free.
  - At low load, jobs rarely wait (c idle workers available).
  - At high load, all c workers are busy and jobs queue behind the earliest-free one.

Usage:
  python "logs and des/multi_server_des.py" \\
      --input  "logs and des/experiments/dsp_aes_10rps.csv" \\
      --output "logs and des/experiments/dsp_aes_10rps_mgc_w4_replay.csv" \\
      --mode replay --workers 4

  python "logs and des/multi_server_des.py" \\
      --input  "logs and des/experiments/dsp_aes_50rps.csv" \\
      --output "logs and des/experiments/dsp_aes_50rps_mgc_w4_parametric.csv" \\
      --mode parametric --dist lognormal --workers 4
"""

import argparse
import csv
import heapq
import math
import random
import statistics
from pathlib import Path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="M/G/c trace-driven DES: c parallel workers, shared arrival queue."
    )
    parser.add_argument("--input",  required=True, help="Input CSV trace path (required)")
    parser.add_argument("--output", default="logs and des/mgc_simulated.csv")
    parser.add_argument(
        "--mode",
        choices=["replay", "bootstrap", "parametric"],
        default="replay",
        help=(
            "Service-time source: "
            "'replay' uses observed times in arrival order; "
            "'bootstrap' resamples with replacement; "
            "'parametric' fits a distribution and samples i.i.d."
        ),
    )
    parser.add_argument(
        "--dist",
        choices=["lognormal", "gamma", "weibull"],
        default="lognormal",
        help="Distribution for parametric mode (default: lognormal).",
    )
    parser.add_argument(
        "--workers", "-c",
        type=int,
        default=4,
        metavar="C",
        help="Number of parallel workers (default: 4, typical Apache mpm_prefork).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--queue-offset",
        type=float,
        default=0.0,
        metavar="MS",
        help="Subtract constant from observed queue_ms before KS (default: 0.0).",
    )
    parser.add_argument(
        "--queue-capacity",
        type=int,
        default=None,
        metavar="N",
        help="Max jobs waiting (not in service). Arrivals beyond this are dropped (503).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def quantile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (1.0 - (idx - lo)) + sorted_vals[hi] * (idx - lo)


def summarize(name, vals):
    s = sorted(vals)
    return {
        "name":  name,
        "count": len(vals),
        "mean":  statistics.fmean(vals),
        "p50":   quantile(s, 0.50),
        "p90":   quantile(s, 0.90),
        "p95":   quantile(s, 0.95),
        "p99":   quantile(s, 0.99),
        "max":   s[-1],
    }


def print_summary(label, d):
    print(
        f"{label}: count={d['count']} mean={d['mean']:.6f} "
        f"p50={d['p50']:.6f} p90={d['p90']:.6f} "
        f"p95={d['p95']:.6f} p99={d['p99']:.6f} max={d['max']:.6f}"
    )


def ks_like_distance(obs, sim):
    obs_s, sim_s = sorted(obs), sorted(sim)
    points = sorted(set(obs_s + sim_s))
    i = j = 0
    n, m = len(obs_s), len(sim_s)
    d = 0.0
    for x in points:
        while i < n and obs_s[i] <= x:
            i += 1
        while j < m and sim_s[j] <= x:
            j += 1
        d = max(d, abs(i / n - j / m))
    return d


# ---------------------------------------------------------------------------
# Distribution fitting
# ---------------------------------------------------------------------------

def fit_lognormal(svc):
    logs = [math.log(max(v, 1e-9)) for v in svc]
    mu = statistics.fmean(logs)
    sigma = statistics.stdev(logs) if len(logs) > 1 else 0.5
    return mu, sigma


def fit_gamma(svc):
    mean = statistics.fmean(svc)
    var  = max(statistics.variance(svc) if len(svc) > 1 else mean, 1e-12)
    k    = mean ** 2 / var
    theta = var / mean
    return k, theta


def fit_weibull(svc):
    mean = statistics.fmean(svc)
    std  = statistics.stdev(svc) if len(svc) > 1 else mean * 0.5
    cv   = max(std / mean if mean > 0 else 1.0, 1e-6)

    def cv_of_k(k):
        g1 = math.gamma(1.0 + 1.0 / k)
        g2 = math.gamma(1.0 + 2.0 / k)
        return math.sqrt(max(0.0, g2 / (g1 * g1) - 1.0))

    lo, hi = 0.05, 500.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if cv_of_k(mid) > cv:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    alpha = mean / math.gamma(1.0 + 1.0 / k)
    return alpha, k


def sample_service_times(observed, mode, dist, rng):
    """Return a list of service times of the same length as observed."""
    if mode == "replay":
        return list(observed)

    if mode == "bootstrap":
        return [observed[rng.randrange(len(observed))] for _ in observed]

    # parametric
    if dist == "gamma":
        k, theta = fit_gamma(observed)
        return [rng.gammavariate(k, theta) for _ in observed]

    if dist == "weibull":
        alpha, k = fit_weibull(observed)
        return [rng.weibullvariate(alpha, k) for _ in observed]

    # lognormal (default)
    mu, sigma = fit_lognormal(observed)
    return [math.exp(rng.gauss(mu, sigma)) for _ in observed]


# ---------------------------------------------------------------------------
# M/G/c simulation
# ---------------------------------------------------------------------------

def simulate_mgc(arrivals_ns, observed_service_ms, mode, dist, seed,
                 workers=4, queue_capacity=None):
    """
    M/G/c FCFS simulation driven by observed arrival timestamps.

    workers (c): number of parallel servers. Each server handles one job at a
        time. The job goes to whichever server finishes soonest (min-heap).

    queue_capacity: max jobs waiting (not in any server). Arrivals that find
        the wait queue full are dropped (status 503).

    Queue-depth tracking: same scheduled_waiting approach as single_server_des —
        a sorted list of scheduled start times for jobs that have arrived but
        not yet started service (start_ns > arrival_ns). Entries are pruned
        when start_ns <= current arrival_ns (those jobs have entered a server).
    """
    rng = random.Random(seed)
    sampled = sample_service_times(observed_service_ms, mode, dist, rng)

    # Min-heap of server completion times. Initially all servers are idle (t=0).
    server_ends = [arrivals_ns[0]] * workers
    heapq.heapify(server_ends)

    scheduled_waiting = []   # sorted start_ns for jobs waiting (not in service)
    sim_queue_ms     = []
    sim_response_ms  = []
    sim_svc_start_ns = []
    sim_svc_end_ns   = []
    sim_dropped      = []

    for arrival_ns, svc_ms in zip(arrivals_ns, sampled):
        service_ns = int(round(svc_ms * 1_000_000.0))

        # Prune waiting jobs that have started service by arrival time.
        prune = 0
        while prune < len(scheduled_waiting) and scheduled_waiting[prune] <= arrival_ns:
            prune += 1
        if prune:
            scheduled_waiting = scheduled_waiting[prune:]

        # Earliest-free server.
        earliest_end = server_ends[0]

        if earliest_end <= arrival_ns:
            # At least one server is idle — start immediately.
            start_ns = arrival_ns
        else:
            # All workers busy — job must wait.
            queue_depth = len(scheduled_waiting)
            if queue_capacity is not None and queue_depth >= queue_capacity:
                # Queue full: drop.
                sim_svc_start_ns.append(-1)
                sim_svc_end_ns.append(-1)
                sim_queue_ms.append(float("nan"))
                sim_response_ms.append(float("nan"))
                sim_dropped.append(True)
                continue

            start_ns = earliest_end
            scheduled_waiting.append(start_ns)
            scheduled_waiting.sort()  # keep ascending for pruning

        end_ns = start_ns + service_ns
        heapq.heapreplace(server_ends, end_ns)   # this server is now busy until end_ns

        q_ms = (start_ns  - arrival_ns) / 1_000_000.0
        r_ms = (end_ns    - arrival_ns) / 1_000_000.0

        sim_svc_start_ns.append(start_ns)
        sim_svc_end_ns.append(end_ns)
        sim_queue_ms.append(q_ms)
        sim_response_ms.append(r_ms)
        sim_dropped.append(False)

    return {
        "sampled_service_ms":   sampled,
        "sim_queue_ms":         sim_queue_ms,
        "sim_response_ms":      sim_response_ms,
        "sim_service_start_ns": sim_svc_start_ns,
        "sim_service_end_ns":   sim_svc_end_ns,
        "sim_dropped":          sim_dropped,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_trace(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def prepare_observed(rows):
    ok = [r for r in rows if str(r.get("status_code", "")).startswith("2")]
    if not ok:
        raise ValueError("no successful (2xx) rows in input CSV")
    ok.sort(key=lambda r: int(r["arrival_unix_ns"]))
    arrivals_ns  = [int(r["arrival_unix_ns"]) for r in ok]
    service_ms   = [float(r["service_ms"])    for r in ok]
    queue_ms     = [float(r["queue_ms"])      for r in ok]
    response_ms  = [float(r["response_ms"])   for r in ok]
    return arrivals_ns, service_ms, queue_ms, response_ms


def write_sim_csv(path, arrivals_ns, sim_data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "arrival_unix_ns",
            "sim_service_start_unix_ns", "sim_service_end_unix_ns",
            "sim_queue_ms", "sim_service_ms", "sim_response_ms", "sim_status",
        ])
        for i, arrival_ns in enumerate(arrivals_ns):
            if sim_data["sim_dropped"][i]:
                w.writerow([arrival_ns, "", "", "", "", "", 503])
            else:
                w.writerow([
                    arrival_ns,
                    sim_data["sim_service_start_ns"][i],
                    sim_data["sim_service_end_ns"][i],
                    f"{sim_data['sim_queue_ms'][i]:.6f}",
                    f"{sim_data['sampled_service_ms'][i]:.6f}",
                    f"{sim_data['sim_response_ms'][i]:.6f}",
                    200,
                ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    inp  = Path(args.input)
    out  = Path(args.output)
    if not inp.exists():
        raise SystemExit(f"Input CSV not found: {inp}")

    rows = load_trace(inp)
    arrivals_ns, obs_svc, obs_q, obs_resp = prepare_observed(rows)

    offset = args.queue_offset
    obs_q_corr = [max(0.0, q - offset) for q in obs_q]

    # Print fit info.
    mu_fit, sig_fit = fit_lognormal(obs_svc)
    k_g, th_g = fit_gamma(obs_svc)
    print(
        f"input={inp} mode={args.mode} dist={args.dist} "
        f"workers={args.workers} seed={args.seed} "
        f"queue_offset={offset} queue_capacity={args.queue_capacity}"
    )
    print(
        f"lognormal_fit: mu={mu_fit:.6f} sigma={sig_fit:.6f} "
        f"implied_mean_ms={math.exp(mu_fit + 0.5*sig_fit**2):.6f}"
    )
    cv_g = 1.0 / math.sqrt(k_g) if k_g > 0 else float("nan")
    print(
        f"gamma_fit: k={k_g:.6f} theta={th_g:.6f} "
        f"implied_mean_ms={k_g*th_g:.6f} implied_cv={cv_g:.6f}"
    )

    sim = simulate_mgc(
        arrivals_ns, obs_svc,
        args.mode, args.dist, args.seed,
        workers=args.workers,
        queue_capacity=args.queue_capacity,
    )

    admitted_idx  = [i for i, d in enumerate(sim["sim_dropped"]) if not d]
    n_total   = len(arrivals_ns)
    n_dropped = sum(sim["sim_dropped"])

    sim_resp = [sim["sim_response_ms"][i] for i in admitted_idx]
    sim_q    = [sim["sim_queue_ms"][i]    for i in admitted_idx]
    sim_svc  = [sim["sampled_service_ms"][i] for i in admitted_idx]

    print(
        f"n_total={n_total} n_admitted={len(admitted_idx)} "
        f"n_dropped={n_dropped} drop_rate={n_dropped/n_total:.4f}"
    )
    print_summary("observed_response_ms",        summarize("obs_resp", obs_resp))
    print_summary("sim_response_ms",             summarize("sim_resp", sim_resp))
    print_summary("observed_queue_ms",           summarize("obs_q",    obs_q))
    print_summary("observed_queue_corrected_ms", summarize("obs_qc",   obs_q_corr))
    print_summary("sim_queue_ms",                summarize("sim_q",    sim_q))
    print_summary("observed_service_ms",         summarize("obs_svc",  obs_svc))
    print_summary("sim_service_ms",              summarize("sim_svc",  sim_svc))

    ks_resp = ks_like_distance(obs_resp, sim_resp)
    ks_q    = ks_like_distance(obs_q,    sim_q)
    ks_qc   = ks_like_distance(obs_q_corr, sim_q)
    print(f"ks_like_response={ks_resp:.6f}")
    print(f"ks_like_queue={ks_q:.6f}")
    print(f"ks_like_queue_corrected={ks_qc:.6f}")

    write_sim_csv(out, arrivals_ns, sim)
    print(f"wrote_simulated_csv={out}")


if __name__ == "__main__":
    main()
