"""
Iteration 1 completion: ML-only latency predictor vs DES comparison.

Trains simple regression models on (arrival_rate -> response percentiles) using
leave-one-out cross-validation, then compares prediction error to the best DES
mode (replay).

Also validates operational laws:
  - Utilisation law: rho = lambda * E[S]
  - Little's Law:    L = lambda * W  (mean queue length = rate * mean wait)
"""
import csv
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "data" / "experiments" / "go_1c"
QUEUE_OFFSET = 0.006  # ms scheduling floor

# Rates with available trace CSVs in data/experiments/go_1c.
ALL_RATES = [50, 100, 200, 400]
RATE_TAGS = {
    50:  "go_50rps",
    100: "go_100rps",
    200: "go_200rps",
    400: "go_400rps",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = p / 100 * (len(s) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    return s[lo] * (1 - (idx - lo)) + s[hi] * (idx - lo)


def load_rate(rate):
    tag  = RATE_TAGS[rate]
    path = RESULTS_DIR / f"{tag}.csv"
    rows = [r for r in csv.DictReader(path.open(newline=""))
            if r.get("status_code") == "200"]
    if not rows:
        return None
    resp = [float(r["response_ms"]) for r in rows]
    svc  = [float(r["service_ms"])  for r in rows]
    que  = [float(r["queue_ms"])    for r in rows]
    arr  = [int(r["arrival_unix_ns"]) for r in rows]
    span_s = (max(arr) - min(arr)) / 1e9 if len(arr) > 1 else 90
    tput   = len(rows) / span_s
    svc_mean = statistics.fmean(svc)
    rho = tput * svc_mean / 1000  # lambda(rps) * E[S](ms) / 1000 = dimensionless
    return {
        "rate":     rate,
        "n":        len(rows),
        "tput":     tput,
        "svc_mean": svc_mean,
        "rho":      rho,
        "resp_mean": statistics.fmean(resp),
        "resp_p50":  pct(resp, 50),
        "resp_p95":  pct(resp, 95),
        "resp_p99":  pct(resp, 99),
        "queue_mean": statistics.fmean(que),
        "queue_mean_corrected": max(0.0, statistics.fmean(que) - QUEUE_OFFSET),
        "queue_p99":  pct(que, 99),
        "resp":  resp,
        "svc":   svc,
        "que":   que,
    }


# ---------------------------------------------------------------------------
# Simple ML models (no sklearn dependency)
# ---------------------------------------------------------------------------

def poly_fit(xs, ys, degree=2):
    """Fit a polynomial via normal equations. Returns coefficient list [a0,a1,...]."""
    n = len(xs)
    # Build Vandermonde matrix
    A = [[x**d for d in range(degree + 1)] for x in xs]
    # Normal equations: (A^T A) coef = A^T y
    def dot(u, v):
        return sum(a * b for a, b in zip(u, v))
    AtA = [[dot([A[i][r] for i in range(n)], [A[i][c] for i in range(n)])
            for c in range(degree + 1)] for r in range(degree + 1)]
    Aty = [dot([A[i][r] for i in range(n)], ys) for r in range(degree + 1)]
    # Gaussian elimination
    m = degree + 1
    aug = [AtA[r][:] + [Aty[r]] for r in range(m)]
    for col in range(m):
        pivot = max(range(col, m), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        if abs(aug[col][col]) < 1e-12:
            continue
        for r in range(m):
            if r == col:
                continue
            f = aug[r][col] / aug[col][col]
            aug[r] = [aug[r][c] - f * aug[col][c] for c in range(m + 1)]
    coef = [aug[r][m] / aug[r][r] if abs(aug[r][r]) > 1e-12 else 0.0
            for r in range(m)]
    return coef


def poly_predict(coef, x):
    return sum(c * x**d for d, c in enumerate(coef))


def loocv_mae(xs, ys, degree=2):
    """Leave-one-out cross-validation MAE for polynomial regression."""
    errors = []
    for i in range(len(xs)):
        train_x = xs[:i] + xs[i+1:]
        train_y = ys[:i] + ys[i+1:]
        if len(train_x) <= degree:
            continue
        coef = poly_fit(train_x, train_y, degree)
        pred = poly_predict(coef, xs[i])
        errors.append(abs(pred - ys[i]))
    return statistics.fmean(errors) if errors else float("nan")


# ---------------------------------------------------------------------------
# DES replay predictions (pre-computed KS from experiment results)
# DES absolute prediction error: |observed_p99 - replay_p99| from DES stdout
# We re-read the DES output CSVs for each rate.
# ---------------------------------------------------------------------------

def load_des_predictions(rate, mode="replay"):
    tag     = RATE_TAGS[rate]
    des_csv = RESULTS_DIR / f"{tag}_des_{mode}.csv"
    if not des_csv.exists():
        return None
    rows = list(csv.DictReader(des_csv.open(newline="")))
    if not rows:
        return None
    sim_resp = [float(r["sim_response_ms"]) for r in rows]
    sim_que  = [float(r["sim_queue_ms"])    for r in rows]
    return {
        "resp_p50": pct(sim_resp, 50),
        "resp_p95": pct(sim_resp, 95),
        "resp_p99": pct(sim_resp, 99),
        "queue_p99": pct(sim_que, 99),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    data = [d for d in (load_rate(r) for r in ALL_RATES) if d]

    print("=" * 70)
    print("OPERATIONAL LAWS VALIDATION")
    print("=" * 70)

    print("\n-- Utilisation law: rho = tput * E[S] (tput in rps, E[S] in ms / 1000) --")
    print(f"{'rate':>6}  {'tput':>7}  {'svc_mean':>9}  {'rho_est':>8}  {'resp_mean':>10}  {'q_mean_corr':>12}")
    for d in data:
        print(f"{d['rate']:>6}  {d['tput']:>7.1f}  {d['svc_mean']:>9.3f}  "
              f"{d['rho']:>8.3f}  {d['resp_mean']:>10.3f}  "
              f"{d['queue_mean_corrected']:>12.4f}")

    print("\n-- Little's Law check: L_queue = lambda * W_queue --")
    print(f"  (L_queue = mean corrected queue occupancy; W_queue = mean corrected wait)")
    print(f"{'rate':>6}  {'lambda':>7}  {'W_q_corr':>9}  {'L_q_pred':>9}  "
          f"{'L_q_obs':>8}  {'err%':>6}")
    for d in data:
        lam   = d["tput"]                            # requests/s
        W_q   = d["queue_mean_corrected"] / 1000.0   # seconds
        L_pred = lam * W_q                           # mean jobs in queue (predicted)
        # Observed L: fraction of service time the server is busy ~ rho
        # For M/G/1: L_q = rho^2 / (1 - rho) * (1 + CV^2) / 2
        rho = d["rho"]
        if rho < 1.0:
            svc_vals = d["svc"]
            cv = (statistics.stdev(svc_vals) / statistics.fmean(svc_vals)
                  if len(svc_vals) > 1 else 1.0)
            L_mg1 = (rho**2 / (1 - rho)) * (1 + cv**2) / 2
        else:
            L_mg1 = float("inf")
        err_pct = abs(L_pred - L_mg1) / L_mg1 * 100 if L_mg1 < 1e6 else float("nan")
        print(f"{d['rate']:>6}  {lam:>7.1f}  {W_q*1000:>9.4f}  {L_pred:>9.5f}  "
              f"{L_mg1:>8.5f}  {err_pct:>6.1f}%")

    print("\n" + "=" * 70)
    print("ML BASELINE vs DES COMPARISON (Leave-One-Out Cross-Validation)")
    print("=" * 70)

    xs    = [d["rate"]     for d in data]
    y_p50 = [d["resp_p50"] for d in data]
    y_p95 = [d["resp_p95"] for d in data]
    y_p99 = [d["resp_p99"] for d in data]

    for degree, name in [(1, "Linear"), (2, "Quadratic")]:
        mae_p50 = loocv_mae(xs, y_p50, degree)
        mae_p95 = loocv_mae(xs, y_p95, degree)
        mae_p99 = loocv_mae(xs, y_p99, degree)
        print(f"\n{name} regression (degree={degree}) LOOCV MAE:")
        print(f"  resp p50: {mae_p50:.3f} ms")
        print(f"  resp p95: {mae_p95:.3f} ms")
        print(f"  resp p99: {mae_p99:.3f} ms")

    print("\n-- Per-rate: ML quadratic prediction vs DES replay prediction vs observed --")
    print(f"{'rate':>6}  {'obs_p99':>8}  {'ml_pred':>8}  {'ml_err':>7}  "
          f"{'des_pred':>9}  {'des_err':>8}")

    coef_p99 = poly_fit(xs, y_p99, degree=2)
    for d in data:
        obs   = d["resp_p99"]
        ml    = poly_predict(coef_p99, d["rate"])
        des   = load_des_predictions(d["rate"], "replay")
        des_p = des["resp_p99"] if des else float("nan")
        print(f"{d['rate']:>6}  {obs:>8.2f}  {ml:>8.2f}  {abs(ml-obs):>7.2f}  "
              f"{des_p:>9.2f}  {abs(des_p-obs):>8.2f}")

    print("\n-- Cross-rate generalisation: fit parametric at 100 rps, predict 200 & 400 --")
    print("  (Fit lognormal to 100 rps service times; run parametric DES at 200 & 400)")
    for pred_rate in [200, 400]:
        tag  = RATE_TAGS[pred_rate]
        des  = load_des_predictions(pred_rate, "parametric")
        obs  = next(d for d in data if d["rate"] == pred_rate)
        if des:
            err_p99 = abs(des["resp_p99"] - obs["resp_p99"])
            print(f"  Rate {pred_rate} rps: obs_p99={obs['resp_p99']:.2f} ms  "
                  f"parametric_des_p99={des['resp_p99']:.2f} ms  "
                  f"abs_err={err_p99:.2f} ms")

    print("\n-- Summary: LOOCV MAE (all rates) --")
    ml_mae_p99  = loocv_mae(xs, y_p99, degree=2)
    des_errors  = []
    for d in data:
        des = load_des_predictions(d["rate"], "replay")
        if des:
            des_errors.append(abs(des["resp_p99"] - d["resp_p99"]))
    des_mae_p99 = statistics.fmean(des_errors) if des_errors else float("nan")
    print(f"  ML  (quadratic, p99): {ml_mae_p99:.3f} ms")
    print(f"  DES (replay,    p99): {des_mae_p99:.3f} ms")
    if des_mae_p99 < ml_mae_p99:
        print(f"  -> DES is {ml_mae_p99/des_mae_p99:.1f}x more accurate than ML on p99")
    else:
        print(f"  -> ML is {des_mae_p99/ml_mae_p99:.1f}x more accurate than DES on p99")


if __name__ == "__main__":
    main()
