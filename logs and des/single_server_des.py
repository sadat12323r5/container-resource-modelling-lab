import argparse
import csv
import math
import random
import statistics
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Single-server FCFS DES from request trace and service-time samples."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input CSV trace path (required)",
    )
    parser.add_argument(
        "--output",
        default="logs and des/des_simulated.csv",
        help="Output simulated CSV path (default: logs and des/des_simulated.csv)",
    )
    parser.add_argument(
        "--mode",
        choices=["replay", "bootstrap", "parametric"],
        default="bootstrap",
        help=(
            "Service-time source: "
            "'replay' uses observed times in arrival order; "
            "'bootstrap' samples with replacement from observed times; "
            "'parametric' fits a lognormal to observed times and samples i.i.d."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for bootstrap/parametric modes (default: 42)",
    )
    parser.add_argument(
        "--queue-offset",
        type=float,
        default=0.0,
        metavar="MS",
        help=(
            "Subtract this constant (ms) from observed queue_ms before KS comparison "
            "to remove the goroutine-scheduling floor (default: 0.0). "
            "Typical value: 0.006 ms."
        ),
    )
    parser.add_argument(
        "--dist",
        choices=["lognormal", "gamma", "weibull"],
        default="lognormal",
        help=(
            "Distribution used by parametric mode to sample service times. "
            "'lognormal' (default): fits mu/sigma via log-space MLE. "
            "'gamma': fits shape k and scale theta via method of moments. "
            "Has no effect on replay or bootstrap modes."
        ),
    )
    parser.add_argument(
        "--queue-capacity",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum number of jobs that may wait in the simulated queue. "
            "Arrivals that find the queue full are dropped (sim_status=503) and "
            "excluded from KS comparison. "
            "Set to match the server's JOB_QUEUE setting (default: unlimited). "
            "Go server default: 1024."
        ),
    )
    return parser.parse_args()


def quantile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def summarize(name, vals):
    s = sorted(vals)
    return {
        "name": name,
        "count": len(vals),
        "mean": statistics.fmean(vals),
        "p50": quantile(s, 0.50),
        "p90": quantile(s, 0.90),
        "p95": quantile(s, 0.95),
        "p99": quantile(s, 0.99),
        "max": s[-1],
    }


def print_summary(label, summary):
    print(
        f"{label}: count={summary['count']} mean={summary['mean']:.6f} "
        f"p50={summary['p50']:.6f} p90={summary['p90']:.6f} "
        f"p95={summary['p95']:.6f} p99={summary['p99']:.6f} max={summary['max']:.6f}"
    )


def ks_like_distance(obs, sim):
    """Kolmogorov-Smirnov-like CDF supremum distance (no SciPy dependency)."""
    obs_sorted = sorted(obs)
    sim_sorted = sorted(sim)
    points = sorted(set(obs_sorted + sim_sorted))
    i = j = 0
    n, m = len(obs_sorted), len(sim_sorted)
    d = 0.0
    for x in points:
        while i < n and obs_sorted[i] <= x:
            i += 1
        while j < m and sim_sorted[j] <= x:
            j += 1
        d = max(d, abs(i / n - j / m))
    return d


def fit_gamma(service_ms):
    """Return (shape k, scale theta) of gamma fit via method of moments.

    k = mean² / variance  (shape, dimensionless)
    theta = variance / mean  (scale, same units as service_ms)

    Sampling: random.gammavariate(k, theta)
    Mean = k * theta, Variance = k * theta², CV = 1/sqrt(k).
    """
    mean = statistics.fmean(service_ms)
    var = statistics.variance(service_ms) if len(service_ms) > 1 else mean
    var = max(var, 1e-12)  # guard against zero variance
    k = (mean ** 2) / var
    theta = var / mean
    return k, theta


def fit_weibull(service_ms):
    """Return (scale alpha, shape k) of Weibull fit via method of moments.

    Finds shape k by bisection on CV(k) = sqrt(Γ(1+2/k)/Γ(1+1/k)² - 1).
    Then scale alpha = mean / Γ(1+1/k).

    Sampling: random.weibullvariate(alpha, k)
    Mean = alpha * Γ(1+1/k), CV = sqrt(Γ(1+2/k)/Γ(1+1/k)² - 1).
    k > 1 → right-skewed, mode > 0 (like lognormal but with power-law tail cutoff).
    k → ∞ → deterministic. k = 1 → exponential (CV=1). k < 1 → heavy-tailed.
    """
    mean = statistics.fmean(service_ms)
    std = statistics.stdev(service_ms) if len(service_ms) > 1 else mean * 0.5
    cv_target = std / mean if mean > 0 else 1.0
    cv_target = max(cv_target, 1e-6)

    def cv_of_k(k):
        g1 = math.gamma(1.0 + 1.0 / k)
        g2 = math.gamma(1.0 + 2.0 / k)
        return math.sqrt(max(0.0, g2 / (g1 * g1) - 1.0))

    # Bisect for k in [0.05, 500] — covers CV from ~20 down to ~0.04.
    lo, hi = 0.05, 500.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if cv_of_k(mid) > cv_target:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    alpha = mean / math.gamma(1.0 + 1.0 / k)
    return alpha, k


def fit_lognormal(service_ms):
    """Return (mu, sigma) of the lognormal fit to service_ms via log-space MLE."""
    log_vals = [math.log(max(v, 1e-9)) for v in service_ms]
    mu = statistics.fmean(log_vals)
    sigma = statistics.stdev(log_vals) if len(log_vals) > 1 else 0.5
    return mu, sigma


def load_trace(path):
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    if not rows:
        raise ValueError("input CSV has no rows")
    return rows


def prepare_observed(rows):
    """Return arrival timestamps and per-request ms values for status=2xx rows."""
    ok = [r for r in rows if str(r.get("status_code", "")).startswith("2")]
    if not ok:
        raise ValueError("no successful (status_code=2xx) rows in input CSV")
    ok.sort(key=lambda r: int(r["arrival_unix_ns"]))

    arrivals_ns = [int(r["arrival_unix_ns"]) for r in ok]
    service_ms  = [float(r["service_ms"])    for r in ok]
    queue_ms    = [float(r["queue_ms"])      for r in ok]
    response_ms = [float(r["response_ms"])   for r in ok]
    return arrivals_ns, service_ms, queue_ms, response_ms


def simulate(arrivals_ns, observed_service_ms, mode, seed, queue_capacity=None, dist="lognormal"):
    """
    Simulate a single-server FCFS queue driven by observed arrival timestamps.

    queue_capacity: maximum number of jobs waiting (not in service) before new
    arrivals are dropped (sim_status=503). None means unlimited. Matches the Go
    server's JOB_QUEUE channel buffer (default 1024).

    Queue-depth tracking: maintain `scheduled_waiting`, a sorted list of the
    scheduled start times of jobs that have arrived but not yet started service.
    When a new job arrives at time t, any entry s <= t has already started service
    and is pruned. The remaining length is the current waiting count.
    """
    rng = random.Random(seed)

    if mode == "replay":
        sampled_service_ms = list(observed_service_ms)

    elif mode == "bootstrap":
        sampled_service_ms = [
            observed_service_ms[rng.randrange(len(observed_service_ms))]
            for _ in observed_service_ms
        ]

    else:  # parametric
        if dist == "gamma":
            k, theta = fit_gamma(observed_service_ms)
            sampled_service_ms = [
                rng.gammavariate(k, theta)
                for _ in observed_service_ms
            ]
        elif dist == "weibull":
            alpha, k = fit_weibull(observed_service_ms)
            sampled_service_ms = [
                rng.weibullvariate(alpha, k)
                for _ in observed_service_ms
            ]
        else:  # lognormal
            mu, sigma = fit_lognormal(observed_service_ms)
            sampled_service_ms = [
                math.exp(rng.gauss(mu, sigma))
                for _ in observed_service_ms
            ]

    sim_queue_ms = []
    sim_response_ms = []
    sim_service_start_ns = []
    sim_service_end_ns = []
    sim_dropped = []          # True when queue was full at arrival
    prev_end_ns = arrivals_ns[0]

    # Sorted list of scheduled start times for jobs waiting (not yet in service).
    # Start times are always > the arrival time of the job that added them (because
    # start_ns = prev_end_ns > arrival_ns for any job that had to wait). Pruning
    # entries <= current arrival_ns removes jobs that have since started service.
    scheduled_waiting = []

    for arrival_ns, svc_ms in zip(arrivals_ns, sampled_service_ms):
        service_ns = int(round(svc_ms * 1_000_000.0))

        # Prune jobs that have started service by the time this job arrives.
        # scheduled_waiting is ascending, so we can scan from the front.
        prune_idx = 0
        while prune_idx < len(scheduled_waiting) and scheduled_waiting[prune_idx] <= arrival_ns:
            prune_idx += 1
        if prune_idx:
            scheduled_waiting = scheduled_waiting[prune_idx:]

        queue_depth = len(scheduled_waiting)  # jobs waiting (not in service)

        if arrival_ns >= prev_end_ns:
            # Server is idle: job starts immediately, nothing waiting.
            start_ns = arrival_ns
            # scheduled_waiting is already empty or all entries <= arrival_ns (already pruned).
        else:
            # Server is busy.
            if queue_capacity is not None and queue_depth >= queue_capacity:
                # Queue full: drop this request (mirrors Go server 503 behaviour).
                sim_service_start_ns.append(-1)
                sim_service_end_ns.append(-1)
                sim_queue_ms.append(float("nan"))
                sim_response_ms.append(float("nan"))
                sim_dropped.append(True)
                continue  # prev_end_ns unchanged; dropped job never enters service

            # Job waits: it will start when the last scheduled job ends.
            start_ns = prev_end_ns
            scheduled_waiting.append(start_ns)  # start_ns > arrival_ns (always)

        end_ns = start_ns + service_ns
        q_ms   = (start_ns - arrival_ns) / 1_000_000.0
        r_ms   = (end_ns   - arrival_ns) / 1_000_000.0

        sim_service_start_ns.append(start_ns)
        sim_service_end_ns.append(end_ns)
        sim_queue_ms.append(q_ms)
        sim_response_ms.append(r_ms)
        sim_dropped.append(False)
        prev_end_ns = end_ns

    return {
        "sampled_service_ms":   sampled_service_ms,
        "sim_queue_ms":         sim_queue_ms,
        "sim_response_ms":      sim_response_ms,
        "sim_service_start_ns": sim_service_start_ns,
        "sim_service_end_ns":   sim_service_end_ns,
        "sim_dropped":          sim_dropped,
    }


def write_sim_csv(path, arrivals_ns, sim_data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "arrival_unix_ns",
            "sim_service_start_unix_ns",
            "sim_service_end_unix_ns",
            "sim_queue_ms",
            "sim_service_ms",
            "sim_response_ms",
            "sim_status",
        ])
        for i, arrival_ns in enumerate(arrivals_ns):
            dropped = sim_data["sim_dropped"][i]
            if dropped:
                writer.writerow([
                    arrival_ns, "", "", "", "", "", 503,
                ])
            else:
                writer.writerow([
                    arrival_ns,
                    sim_data["sim_service_start_ns"][i],
                    sim_data["sim_service_end_ns"][i],
                    f"{sim_data['sim_queue_ms'][i]:.6f}",
                    f"{sim_data['sampled_service_ms'][i]:.6f}",
                    f"{sim_data['sim_response_ms'][i]:.6f}",
                    200,
                ])


def main():
    args = parse_args()
    input_path  = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise SystemExit(f"Input CSV not found: {input_path}")

    rows = load_trace(input_path)
    arrivals_ns, obs_service_ms, obs_queue_ms, obs_response_ms = prepare_observed(rows)

    # Apply scheduling-floor correction to observed queue before KS comparison.
    offset = args.queue_offset
    obs_queue_corrected = [max(0.0, q - offset) for q in obs_queue_ms]

    # Parametric fit info (printed regardless of mode, useful for reference).
    mu_fit, sigma_fit = fit_lognormal(obs_service_ms)

    sim_data = simulate(
        arrivals_ns, obs_service_ms, args.mode, args.seed,
        queue_capacity=args.queue_capacity,
        dist=args.dist,
    )

    # Split sim results into admitted (served) and dropped.
    admitted_idx = [i for i, d in enumerate(sim_data["sim_dropped"]) if not d]
    n_total   = len(arrivals_ns)
    n_dropped = sum(sim_data["sim_dropped"])
    n_admitted = len(admitted_idx)

    sim_resp_admitted  = [sim_data["sim_response_ms"][i] for i in admitted_idx]
    sim_queue_admitted = [sim_data["sim_queue_ms"][i]    for i in admitted_idx]
    sim_svc_admitted   = [sim_data["sampled_service_ms"][i] for i in admitted_idx]

    obs_resp_summary = summarize("observed_response_ms", obs_response_ms)
    obs_q_summary    = summarize("observed_queue_ms",    obs_queue_ms)
    obs_qc_summary   = summarize("observed_queue_corrected_ms", obs_queue_corrected)
    obs_s_summary    = summarize("observed_service_ms",  obs_service_ms)

    sim_resp_summary = summarize("sim_response_ms",  sim_resp_admitted)
    sim_q_summary    = summarize("sim_queue_ms",     sim_queue_admitted)
    sim_s_summary    = summarize("sim_service_ms",   sim_svc_admitted)

    print(
        f"input={input_path} mode={args.mode} dist={args.dist} seed={args.seed} "
        f"queue_offset={offset} queue_capacity={args.queue_capacity}"
    )
    print(
        f"n_total={n_total} n_admitted={n_admitted} "
        f"n_dropped={n_dropped} drop_rate={n_dropped/n_total:.4f}"
    )
    print(f"lognormal_fit: mu={mu_fit:.6f} sigma={sigma_fit:.6f} "
          f"implied_mean_ms={math.exp(mu_fit + 0.5*sigma_fit**2):.6f}")
    k_fit, theta_fit = fit_gamma(obs_service_ms)
    cv_fit = 1.0 / math.sqrt(k_fit) if k_fit > 0 else float("nan")
    print(f"gamma_fit: k={k_fit:.6f} theta={theta_fit:.6f} "
          f"implied_mean_ms={k_fit*theta_fit:.6f} implied_cv={cv_fit:.6f}")
    alpha_fit, kw_fit = fit_weibull(obs_service_ms)
    mean_w = alpha_fit * math.gamma(1.0 + 1.0 / kw_fit)
    cv_w = math.sqrt(max(0.0, math.gamma(1.0 + 2.0/kw_fit) / math.gamma(1.0 + 1.0/kw_fit)**2 - 1.0))
    print(f"weibull_fit: alpha={alpha_fit:.6f} k={kw_fit:.6f} "
          f"implied_mean_ms={mean_w:.6f} implied_cv={cv_w:.6f}")
    print_summary("observed_response_ms",          obs_resp_summary)
    print_summary("sim_response_ms",               sim_resp_summary)
    print_summary("observed_queue_ms",             obs_q_summary)
    print_summary("observed_queue_corrected_ms",   obs_qc_summary)
    print_summary("sim_queue_ms",                  sim_q_summary)
    print_summary("observed_service_ms",           obs_s_summary)
    print_summary("sim_service_ms",                sim_s_summary)

    # KS distances: sim side uses admitted requests only (mirrors observed 2xx filter).
    ks_resp            = ks_like_distance(obs_response_ms,      sim_resp_admitted)
    ks_queue_raw       = ks_like_distance(obs_queue_ms,         sim_queue_admitted)
    ks_queue_corrected = ks_like_distance(obs_queue_corrected,  sim_queue_admitted)

    print(f"ks_like_response={ks_resp:.6f}")
    print(f"ks_like_queue={ks_queue_raw:.6f}")
    print(f"ks_like_queue_corrected={ks_queue_corrected:.6f}")

    write_sim_csv(output_path, arrivals_ns, sim_data)
    print(f"wrote_simulated_csv={output_path}")


if __name__ == "__main__":
    main()
