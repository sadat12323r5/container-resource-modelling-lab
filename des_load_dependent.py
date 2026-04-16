#!/usr/bin/env python3
"""
des_load_dependent.py
=====================
Compares two DES models against every observed trace:

  Standard M/G/c   — service time ~ Lognormal(S0, cv)          [baseline]
  Load-dependent   — service time ~ Lognormal(S0/(1-β·ρ), cv)  [new model]

The load-dependent model captures the empirical finding that E[S] grows with
utilisation ρ (OS scheduler preemption / processor sharing effect).  β is the
hyperbolic shrinkage coefficient fitted by fit_service_time.py.

Method
------
For each server × rate:
  1. Read observed trace → arrivals (ns) + response_ms
  2. Estimate CV from service_ms column (low-load trace per server)
  3. Use observed ρ from summary CSV to fix E[S]_eff = S0/(1-β·ρ)
     (avoids solving the implicit ρ=λ·E[S]/c equation)
  4. Run both DES variants (3 independent runs each, merged)
  5. Compute KS(observed, sim) for both; record the delta

Output
------
  des_ld_results.csv  — per-rate KS comparison table
  des_ld_cdf.png      — CDF overlay plots for every server × rate
"""
import csv
import math
import os
import random
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from des_utils import ks_distance, pct

# ── Config ───────────────────────────────────────────────────────────────────
BASE     = os.path.dirname(os.path.abspath(__file__))
EXP_BASE = os.path.join(BASE, "experiments")
N_RUNS   = 5      # independent DES runs merged per variant
RHO_MAX  = 0.92   # skip saturated trace files

SERVER_SPECS = {
    # tag           folder           c   trace_prefix
    "apache_dsp":  ("apache_dsp_1c",  1, "apache_dsp_"),
    "apache_msg":  ("apache_msg_1c",  1, "apache_msg_"),
    "go":          ("go_1c",          1, "go_"),
    "go_sqlite":   ("go_sqlite_1c",   1, "go_sqlite_"),
    "java_dsp":    ("java_dsp_1c",    4, "java_dsp_"),
    "node_dsp":    ("node_dsp_1c",    1, "node_dsp_"),
    "python_dsp":  ("python_dsp_1c",  1, "python_dsp_"),
    "python_dsp3": ("python_dsp_3c",  3, "python_dsp_mc_"),
    "node_dsp3":   ("node_dsp_3c",    3, "node_dsp_mc_"),
}

# Map experiment folder → fit-params row label
FOLDER_TO_LABEL = {
    "apache_dsp_1c":  "Apache DSP (1c)",
    "apache_msg_1c":  "Apache MSG (1c)",
    "go_1c":          "Go lognormal (1c)",
    "go_sqlite_1c":   "Go SQLite (1c)",
    "java_dsp_1c":    "Java DSP (1c)",
    "node_dsp_1c":    "Node DSP (1c)",
    "python_dsp_1c":  "Python DSP (1c)",
    "python_dsp_3c":  "Python DSP (3c)",
    "node_dsp_3c":    "Node DSP (3c)",
}

# ── Lognormal helpers ────────────────────────────────────────────────────────
def ln_params(mean_s, cv):
    """Return (mu, sigma) of the underlying normal for Lognormal(mean, cv)."""
    if cv < 1e-4:
        return math.log(max(mean_s, 1e-12)), 1e-4
    sigma2 = math.log(1.0 + cv * cv)
    mu     = math.log(max(mean_s, 1e-12)) - sigma2 / 2.0
    return mu, math.sqrt(sigma2)

def sample_ln(rng, mean_s, cv):
    mu, sig = ln_params(mean_s, cv)
    return math.exp(mu + sig * rng.gauss(0.0, 1.0))

# ── Core DES engine ──────────────────────────────────────────────────────────
def run_des(arrivals_ms, mean_svc_ms, cv, c, beta=0.0, rho_obs=0.0, seed=0):
    """
    Batch M/G/c DES using replay arrivals.

    beta=0     → standard M/G/c (constant service time)
    beta>0     → load-dependent: E[S]_eff = mean_svc_ms / (1 - beta * rho_obs)
                 rho_obs is the *observed* utilisation from the summary CSV,
                 used as a fixed calibration point for the whole trace.

    Returns sorted list of simulated response times (ms).
    """
    rng      = random.Random(seed)
    free_at  = [0.0] * c          # when each worker becomes free (ms)

    # Effective mean service time — fixed per run
    if beta > 0 and rho_obs > 0:
        s_eff = mean_svc_ms / max(0.01, 1.0 - beta * min(rho_obs, 0.95))
    else:
        s_eff = mean_svc_ms

    resp_times = []
    for arr_t in arrivals_ms:
        # Assign to the worker that will be free soonest
        sid     = min(range(c), key=lambda s: free_at[s])
        start_t = max(arr_t, free_at[sid])
        svc     = sample_ln(rng, s_eff, cv)
        free_at[sid] = start_t + svc
        resp_times.append(free_at[sid] - arr_t)

    return sorted(resp_times)

# ── Trace reading ────────────────────────────────────────────────────────────
def read_trace(path):
    """Return (arrivals_ms, response_ms, service_ms) from a trace CSV."""
    arrivals_ns, resp_ms, svc_ms = [], [], []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                status = row.get("status_code", "200").strip()
                if not status.startswith("2"):
                    continue
                a = float(row["arrival_unix_ns"])
                r = float(row["response_ms"])
                s = float(row.get("service_ms", "nan"))
                if math.isfinite(r) and r > 0:
                    arrivals_ns.append(a)
                    resp_ms.append(r)
                    svc_ms.append(s)
            except (ValueError, KeyError):
                continue

    if not arrivals_ns:
        return [], [], []

    # Convert to relative ms, preserving arrival order
    pairs = sorted(zip(arrivals_ns, resp_ms, svc_ms))
    t0 = pairs[0][0]
    arr  = [(a - t0) / 1e6 for a, _, _ in pairs]
    resp = [r for _, r, _ in pairs]
    svc  = [s for _, _, s in pairs]
    return arr, resp, svc

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Load fit parameters
    fit_csv = os.path.join(BASE, "service_time_fit_params.csv")
    if not os.path.exists(fit_csv):
        print(f"ERROR: {fit_csv} not found — run fit_service_time.py first")
        return

    fit_df = pd.read_csv(fit_csv)
    fit_map = {row["Server"]: row for _, row in fit_df.iterrows()}

    results = []

    # One figure: for each server show CDF at low + high load
    n_servers = len(SERVER_SPECS)
    fig, axes = plt.subplots(n_servers, 2, figsize=(14, n_servers * 3.2))
    fig.suptitle(
        "CDF: Observed vs Standard M/G/c vs Load-Dependent DES",
        fontsize=13, fontweight="bold"
    )

    for row_idx, (tag, (folder, c, prefix)) in enumerate(SERVER_SPECS.items()):
        exp_dir = os.path.join(EXP_BASE, folder)
        sum_csv = os.path.join(exp_dir, f"{tag.rstrip('3')}_summary.csv")

        # Try common summary file naming
        for candidate in os.listdir(exp_dir):
            if candidate.endswith("_summary.csv"):
                sum_csv = os.path.join(exp_dir, candidate)
                break

        if not os.path.exists(sum_csv):
            print(f"  [skip] no summary CSV in {exp_dir}")
            continue

        sum_df = pd.read_csv(sum_csv)

        # Fit params for this server
        label = FOLDER_TO_LABEL.get(folder, folder)
        if label not in fit_map:
            print(f"  [skip] {label} not in fit params")
            continue

        fp       = fit_map[label]
        s0_ms    = float(fp["S0"])
        beta     = float(fp["beta"])

        # Estimate CV from the lowest-rate trace (service_ms column)
        trace_files = sorted([
            f for f in os.listdir(exp_dir)
            if f.startswith(prefix) and f.endswith("rps.csv") and "_des_" not in f
        ])
        cv_est = 0.5   # default
        for tf in trace_files:
            _, _, svc = read_trace(os.path.join(exp_dir, tf))
            svc_valid = [s for s in svc if math.isfinite(s) and s > 0]
            if len(svc_valid) > 30:
                m = np.mean(svc_valid)
                cv_est = np.std(svc_valid) / m if m > 0 else 0.5
                break

        print(f"\n{'='*60}")
        print(f"{label}  c={c}  S0={s0_ms:.3f}ms  beta={beta:.3f}  CV={cv_est:.3f}")
        print(f"{'='*60}")

        server_rows = []
        for tf in trace_files:
            rate_str = tf.replace(prefix, "").replace("rps.csv", "")
            try:
                rate = int(rate_str)
            except ValueError:
                continue

            # Get observed rho from summary
            rate_row = sum_df[sum_df["rate_rps"] == rate]
            if rate_row.empty:
                continue
            rho_obs = float(rate_row["rho"].iloc[0])
            if rho_obs > RHO_MAX:
                print(f"  rate={rate:4d} rps  rho={rho_obs:.3f}  [SATURATED — skip]")
                continue

            arrivals, obs_resp, _ = read_trace(os.path.join(exp_dir, tf))
            if len(arrivals) < 30:
                continue
            obs_sorted = sorted(obs_resp)

            # ── Run both DES variants ──────────────────────────────────────
            std_resp, ld_resp = [], []
            for seed in range(N_RUNS):
                std_resp.extend(run_des(arrivals, s0_ms, cv_est, c,
                                        beta=0.0, rho_obs=rho_obs, seed=seed))
                ld_resp.extend( run_des(arrivals, s0_ms, cv_est, c,
                                        beta=beta,  rho_obs=rho_obs, seed=seed))
            std_sorted = sorted(std_resp)
            ld_sorted  = sorted(ld_resp)

            # ── KS distances ──────────────────────────────────────────────
            ks_std, _ = ks_distance(obs_sorted, std_sorted)
            ks_ld,  _ = ks_distance(obs_sorted, ld_sorted)
            delta     = ks_std - ks_ld
            improved  = delta > 0.005

            s_eff = s0_ms / max(0.01, 1.0 - beta * min(rho_obs, 0.95))
            print(f"  rate={rate:4d} rps  rho={rho_obs:.3f}  "
                  f"S_eff={s_eff:.2f}ms  "
                  f"KS_std={ks_std:.3f}  KS_ld={ks_ld:.3f}  "
                  f"delta={delta:+.3f}  {'IMPROVED' if improved else ''}")

            server_rows.append({
                "server":  label, "c": c, "rate_rps": rate,
                "rho":     round(rho_obs, 3),
                "s_eff_ms":round(s_eff, 3),
                "ks_std":  round(ks_std, 4),
                "ks_ld":   round(ks_ld,  4),
                "delta":   round(delta,  4),
                "improved":improved,
            })
            results.append(server_rows[-1])

        # ── CDF plots: pick lowest & highest stable rate ──────────────────
        if not server_rows:
            continue
        plot_rates = [server_rows[0]["rate_rps"],
                      server_rows[-1]["rate_rps"]]
        if plot_rates[0] == plot_rates[-1]:
            plot_rates = [plot_rates[0], plot_rates[0]]

        for col, prate in enumerate(plot_rates):
            ax = axes[row_idx][col]
            tf = f"{prefix}{prate}rps.csv"
            tpath = os.path.join(exp_dir, tf)
            if not os.path.exists(tpath):
                ax.set_visible(False)
                continue

            rate_row = sum_df[sum_df["rate_rps"] == prate]
            if rate_row.empty:
                ax.set_visible(False)
                continue
            rho_obs = float(rate_row["rho"].iloc[0])

            arrivals, obs_resp, _ = read_trace(tpath)
            obs_sorted = sorted(obs_resp)

            std_resp, ld_resp = [], []
            for seed in range(N_RUNS):
                std_resp.extend(run_des(arrivals, s0_ms, cv_est, c,
                                        beta=0.0, rho_obs=rho_obs, seed=seed))
                ld_resp.extend( run_des(arrivals, s0_ms, cv_est, c,
                                        beta=beta, rho_obs=rho_obs, seed=seed))
            std_sorted = sorted(std_resp)
            ld_sorted  = sorted(ld_resp)

            def cdf_xy(data):
                n = len(data)
                return data, [i/n for i in range(1, n+1)]

            ax.step(*cdf_xy(obs_sorted), color="#1f77b4", lw=2,
                    label=f"Observed (n={len(obs_sorted)})", where="post")
            ax.step(*cdf_xy(std_sorted), color="#d62728", lw=1.5, ls="--",
                    label=f"Standard M/G/c", where="post")
            ax.step(*cdf_xy(ld_sorted),  color="#2ca02c", lw=1.5, ls="-.",
                    label=f"Load-dep (beta={beta:.2f})", where="post")

            ks_s, _ = ks_distance(obs_sorted, std_sorted)
            ks_l, _ = ks_distance(obs_sorted, ld_sorted)
            ax.set_title(
                f"{label}  {prate} rps  (rho={rho_obs:.2f})\n"
                f"KS_std={ks_s:.3f}  KS_ld={ks_l:.3f}  delta={ks_s-ks_l:+.3f}",
                fontsize=8
            )
            ax.set_xlabel("Response time (ms)", fontsize=8)
            ax.set_ylabel("CDF", fontsize=8)
            ax.legend(fontsize=6.5)
            ax.set_xlim(left=0)
            ax.tick_params(labelsize=7)

    plt.tight_layout()
    plot_out = os.path.join(BASE, "des_ld_cdf.png")
    plt.savefig(plot_out, dpi=140, bbox_inches="tight")
    print(f"\nPlot saved  -> {plot_out}")

    # ── Summary table ─────────────────────────────────────────────────────
    if results:
        df_res = pd.DataFrame(results)
        print("\n" + "="*80)
        print("COMPARISON SUMMARY  (KS: lower is better)")
        print("="*80)
        print(df_res.to_string(index=False))

        # Per-server aggregate
        agg = df_res.groupby("server").agg(
            mean_ks_std=("ks_std", "mean"),
            mean_ks_ld= ("ks_ld",  "mean"),
            mean_delta= ("delta",  "mean"),
            pct_improved=("improved", lambda x: f"{100*x.mean():.0f}%"),
        ).round(4)
        print("\nPer-server average:")
        print(agg.to_string())

        csv_out = os.path.join(BASE, "des_ld_results.csv")
        df_res.to_csv(csv_out, index=False)
        print(f"\nResults saved -> {csv_out}")

    print("\nDone.")


if __name__ == "__main__":
    main()
