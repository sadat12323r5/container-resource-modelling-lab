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
        default="logs and des/requests_2rps.csv",
        help="Input CSV path (default: logs and des/requests_2rps.csv)",
    )
    parser.add_argument(
        "--output",
        default="logs and des/des_simulated.csv",
        help="Output simulated CSV path (default: logs and des/des_simulated.csv)",
    )
    parser.add_argument(
        "--mode",
        choices=["replay", "bootstrap"],
        default="bootstrap",
        help=(
            "Service-time mode: replay uses observed service times in order; "
            "bootstrap samples with replacement from observed service times."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for bootstrap mode (default: 42)",
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
    # Simple CDF sup distance on pooled unique points (no SciPy dependency).
    obs_sorted = sorted(obs)
    sim_sorted = sorted(sim)
    points = sorted(set(obs_sorted + sim_sorted))
    i = 0
    j = 0
    n = len(obs_sorted)
    m = len(sim_sorted)
    d = 0.0
    for x in points:
        while i < n and obs_sorted[i] <= x:
            i += 1
        while j < m and sim_sorted[j] <= x:
            j += 1
        f_obs = i / n
        f_sim = j / m
        d = max(d, abs(f_obs - f_sim))
    return d


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
    # Keep successful requests for distribution comparison.
    ok = [r for r in rows if str(r.get("status_code", "")) == "200"]
    if not ok:
        raise ValueError("no successful (status_code=200) rows in input CSV")
    ok.sort(key=lambda r: int(r["arrival_unix_ns"]))

    arrivals_ns = [int(r["arrival_unix_ns"]) for r in ok]
    service_ms = [float(r["service_ms"]) for r in ok]
    queue_ms = [float(r["queue_ms"]) for r in ok]
    response_ms = [float(r["response_ms"]) for r in ok]
    return arrivals_ns, service_ms, queue_ms, response_ms


def simulate(arrivals_ns, observed_service_ms, mode, seed):
    rng = random.Random(seed)

    if mode == "replay":
        sampled_service_ms = list(observed_service_ms)
    else:
        sampled_service_ms = [
            observed_service_ms[rng.randrange(len(observed_service_ms))]
            for _ in observed_service_ms
        ]
    sim_queue_ms = []
    sim_response_ms = []
    sim_service_start_ns = []
    sim_service_end_ns = []
    prev_end_ns = arrivals_ns[0]
    for arrival_ns, svc_ms in zip(arrivals_ns, sampled_service_ms):
        service_ns = int(round(svc_ms * 1_000_000.0))
        start_ns = max(arrival_ns, prev_end_ns)
        end_ns = start_ns + service_ns
        q_ms = (start_ns - arrival_ns) / 1_000_000.0
        r_ms = (end_ns - arrival_ns) / 1_000_000.0

        sim_service_start_ns.append(start_ns)
        sim_service_end_ns.append(end_ns)
        sim_queue_ms.append(q_ms)
        sim_response_ms.append(r_ms)
        prev_end_ns = end_ns

    return {
        "sampled_service_ms": sampled_service_ms,
        "sim_queue_ms": sim_queue_ms,
        "sim_response_ms": sim_response_ms,
        "sim_service_start_ns": sim_service_start_ns,
        "sim_service_end_ns": sim_service_end_ns,
    }


def write_sim_csv(path, arrivals_ns, sim_data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "arrival_unix_ns",
                "sim_service_start_unix_ns",
                "sim_service_end_unix_ns",
                "sim_queue_ms",
                "sim_service_ms",
                "sim_response_ms",
            ]
        )
        for i, arrival_ns in enumerate(arrivals_ns):
            writer.writerow(
                [
                    arrival_ns,
                    sim_data["sim_service_start_ns"][i],
                    sim_data["sim_service_end_ns"][i],
                    f"{sim_data['sim_queue_ms'][i]:.6f}",
                    f"{sim_data['sampled_service_ms'][i]:.6f}",
                    f"{sim_data['sim_response_ms'][i]:.6f}",
                ]
            )


def main():
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise SystemExit(f"Input CSV not found: {input_path}")

    rows = load_trace(input_path)
    arrivals_ns, obs_service_ms, obs_queue_ms, obs_response_ms = prepare_observed(rows)
    sim_data = simulate(arrivals_ns, obs_service_ms, args.mode, args.seed)

    obs_resp_summary = summarize("observed_response_ms", obs_response_ms)
    sim_resp_summary = summarize("sim_response_ms", sim_data["sim_response_ms"])
    obs_q_summary = summarize("observed_queue_ms", obs_queue_ms)
    sim_q_summary = summarize("sim_queue_ms", sim_data["sim_queue_ms"])
    obs_s_summary = summarize("observed_service_ms", obs_service_ms)
    sim_s_summary = summarize("sim_service_ms", sim_data["sampled_service_ms"])
    print(f"input={input_path} mode={args.mode} seed={args.seed}")
    print_summary("observed_response_ms", obs_resp_summary)
    print_summary("sim_response_ms", sim_resp_summary)
    print_summary("observed_queue_ms", obs_q_summary)
    print_summary("sim_queue_ms", sim_q_summary)
    print_summary("observed_service_ms", obs_s_summary)
    print_summary("sim_service_ms", sim_s_summary)
    ks_resp = ks_like_distance(obs_response_ms, sim_data["sim_response_ms"])
    ks_queue = ks_like_distance(obs_queue_ms, sim_data["sim_queue_ms"])
    print(f"ks_like_response={ks_resp:.6f}")
    print(f"ks_like_queue={ks_queue:.6f}")
    write_sim_csv(output_path, arrivals_ns, sim_data)
    print(f"wrote_simulated_csv={output_path}")


if __name__ == "__main__":
    main()
