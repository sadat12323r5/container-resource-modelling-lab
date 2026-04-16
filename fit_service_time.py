#!/usr/bin/env python3
"""
fit_service_time.py
===================
Fits load-dependent service time models to each server's summary CSV.

The key empirical finding: for most servers E[S] grows with arrival rate λ,
violating the constant-service-time assumption of M/G/c. This script:
  1. Plots E[S] vs ρ for every server
  2. Fits three competing models and selects the best by AIC
  3. Extracts model parameters (S₀, inflation coefficient)
  4. Prints a summary table for the thesis

Models compared
---------------
  M0 — Constant:   E[S] = S₀
  M1 — Linear:     E[S] = S₀ + α·ρ
  M2 — Hyperbolic: E[S] = S₀ / (1 − β·ρ)   (Processor-Sharing analogue)

ρ is taken directly from the summary CSV (= rate·mean_svc_ms / (c·1000)),
so model fitting uses the *observed* ρ and avoids solving an implicit equation.
"""
import os
import warnings
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from scipy.optimize import curve_fit
from scipy.stats import pearsonr

# ── Server catalogue ────────────────────────────────────────────────────────
SERVERS = {
    "Apache DSP (1c)":    ("experiments/apache_dsp_1c/apache_dsp_summary.csv",     1),
    "Apache MSG (1c)":    ("experiments/apache_msg_1c/apache_msg_summary.csv",     1),
    "Go lognormal (1c)":  ("experiments/go_1c/go_summary.csv",                    1),
    "Go SQLite (1c)":     ("experiments/go_sqlite_1c/go_sqlite_summary.csv",       1),
    "Java DSP (1c)":      ("experiments/java_dsp_1c/java_dsp_summary.csv",         1),
    "Node DSP (1c)":      ("experiments/node_dsp_1c/node_dsp_summary.csv",         1),
    "Python DSP (1c)":    ("experiments/python_dsp_1c/python_dsp_summary.csv",     1),
    "Python DSP (3c)":    ("experiments/python_dsp_3c/python_dsp_mc_summary.csv",  3),
    "Node DSP (3c)":      ("experiments/node_dsp_3c/node_dsp_mc_summary.csv",      3),
    "Java DSP (3c)":      ("experiments/java_dsp_3c/java_dsp_mc_summary.csv",      4),
    "Go SQLite (3c)":     ("experiments/go_sqlite_3c/go_sqlite_mc_summary.csv",    3),
}

BASE = os.path.dirname(__file__)
RHO_MAX = 0.92   # exclude saturated points from fitting

# ── Model definitions ────────────────────────────────────────────────────────
def m0_constant(rho, s0):
    return np.full_like(rho, s0)

def m1_linear(rho, s0, alpha):
    return s0 + alpha * rho

def m2_hyperbolic(rho, s0, beta):
    denom = 1.0 - beta * rho
    # Guard: keep denominator positive during fitting
    denom = np.where(denom < 0.01, 0.01, denom)
    return s0 / denom

MODELS = {
    "M0 constant":   (m0_constant,   1, ["S₀"]),
    "M1 linear":     (m1_linear,     2, ["S₀", "α"]),
    "M2 hyperbolic": (m2_hyperbolic, 2, ["S₀", "β"]),
}

# ── Fit helpers ──────────────────────────────────────────────────────────────
def aic(n, k, residuals):
    """Akaike Information Criterion (Gaussian log-likelihood)."""
    sse = np.sum(residuals ** 2)
    if sse <= 0:
        return -np.inf
    return n * np.log(sse / n) + 2 * k

def fit_server(rho, svc, name):
    """Return dict of best-fit model parameters and diagnostics."""
    results = {}
    best_aic = np.inf
    best_key = None

    for key, (fn, k, param_names) in MODELS.items():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if key == "M2 hyperbolic":
                    p0    = [svc[0], 0.5]
                    bounds = ([0, 0], [svc.max() * 10, 0.999])
                elif key == "M1 linear":
                    p0    = [svc[0], svc.ptp()]
                    bounds = ([0, 0], [svc.max() * 10, svc.max() * 100])
                else:
                    p0    = [svc.mean()]
                    bounds = ([0], [svc.max() * 10])
                popt, _ = curve_fit(fn, rho, svc, p0=p0, bounds=bounds, maxfev=5000)
            residuals = svc - fn(rho, *popt)
            a = aic(len(svc), k, residuals)
            rmse = np.sqrt(np.mean(residuals ** 2))
            r2 = 1 - np.sum(residuals**2) / np.sum((svc - svc.mean())**2)
            results[key] = {"params": popt, "aic": a, "rmse": rmse, "r2": r2,
                            "fn": fn, "param_names": param_names}
            if a < best_aic:
                best_aic = a
                best_key = key
        except Exception:
            pass

    return results, best_key

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    summary_rows = []

    ncols = 3
    nrows = -(-len(SERVERS) // ncols)   # ceiling division
    fig, axes = plt.subplots(nrows, ncols, figsize=(15, nrows * 3.8))
    axes = axes.flatten()
    fig.suptitle("Service Time vs Utilisation — Load-Dependent Model Fits",
                 fontsize=13, fontweight="bold", y=1.01)

    rho_dense = np.linspace(0, RHO_MAX, 200)

    for idx, (label, (rel_path, c)) in enumerate(SERVERS.items()):
        ax = axes[idx]
        csv_path = os.path.join(BASE, rel_path)

        if not os.path.exists(csv_path):
            ax.set_visible(False)
            continue

        df = pd.read_csv(csv_path)
        df = df[df["rho"] <= RHO_MAX].copy()

        if len(df) < 2:
            ax.set_title(f"{label}\n(insufficient data)", fontsize=9)
            ax.set_visible(False)
            continue

        rho = df["rho"].values
        svc = df["mean_svc_ms"].values
        lam = df["rate_rps"].values

        # ── Fit ──
        fit_results, best_key = fit_server(rho, svc, label)

        # ── Plot observed ──
        ax.scatter(rho, svc, color="#e41a1c", zorder=5, s=40, label="Observed E[S]")

        colors = {"M0 constant": "#aaaaaa", "M1 linear": "#4daf4a", "M2 hyperbolic": "#377eb8"}
        for key, res in fit_results.items():
            y_hat = res["fn"](rho_dense, *res["params"])
            lw = 2.2 if key == best_key else 1.0
            ls = "-"  if key == best_key else "--"
            ax.plot(rho_dense, y_hat, color=colors[key], lw=lw, ls=ls,
                    label=f"{key} (AIC={res['aic']:.0f})")

        ax.set_xlabel("Utilisation ρ", fontsize=8)
        ax.set_ylabel("E[S] (ms)", fontsize=8)
        ax.set_title(label, fontsize=9, fontweight="bold")
        ax.legend(fontsize=6.5, loc="upper left")
        ax.tick_params(labelsize=7)
        ax.set_xlim(0, RHO_MAX)
        ax.set_ylim(bottom=0)

        # ── Annotate best fit ──
        if best_key and best_key in fit_results:
            res = fit_results[best_key]
            params = res["params"]
            param_strs = ", ".join(
                f"{n}={v:.3f}" for n, v in zip(res["param_names"], params)
            )
            ax.text(0.97, 0.05,
                    f"Best: {best_key}\n{param_strs}\nR²={res['r2']:.3f}",
                    transform=ax.transAxes, fontsize=6.5, ha="right",
                    va="bottom", color=colors[best_key],
                    bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8))

        # ── Collect summary row ──
        if best_key and best_key in fit_results:
            res = fit_results[best_key]
            params = res["params"]
            s0 = params[0]
            slope = params[1] if len(params) > 1 else 0.0

            # Service time variation ratio: E[S](ρ=0.8) / E[S](ρ=0.1)
            try:
                s_hi = res["fn"](np.array([0.8]), *params)[0]
                s_lo = res["fn"](np.array([0.1]), *params)[0]
                variation_ratio = s_hi / s_lo if s_lo > 0 else float("nan")
            except Exception:
                variation_ratio = float("nan")

            summary_rows.append({
                "Server":          label,
                "c":               c,
                "S₀ (ms)":         round(s0, 3),
                "Best model":      best_key,
                "Slope / β":       round(slope, 3),
                "R²":              round(res["r2"], 3),
                "RMSE (ms)":       round(res["rmse"], 3),
                "E[S]@ρ0.8 / @ρ0.1": round(variation_ratio, 2),
                "Assumption holds": "✓" if variation_ratio < 1.15 or np.isnan(variation_ratio) else "✗",
            })

    # Hide unused axes
    for ax in axes[len(SERVERS):]:
        ax.set_visible(False)

    plt.tight_layout()
    out_png = os.path.join(BASE, "service_time_fit.png")
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"Plot saved → {out_png}\n")
    plt.show()

    # ── Summary table ────────────────────────────────────────────────────────
    if summary_rows:
        df_sum = pd.DataFrame(summary_rows)
        print("=" * 90)
        print("SERVICE TIME MODEL FIT SUMMARY")
        print("=" * 90)
        print(df_sum.to_string(index=False))
        print()
        print("Interpretation")
        print("-" * 90)
        print("  S₀           — base service time extrapolated to zero load (ms)")
        print("  Slope / β    — M1: ms added per unit ρ;  M2: denominator shrinkage rate")
        print("  E[S]@ρ0.8/0.1 — how much service time inflates from light to heavy load")
        print("  Assumption   — ✓ if service time is roughly constant (ratio < 1.15)")
        print()

        # Save to CSV for later use in the 2-phase DES
        out_csv = os.path.join(BASE, "service_time_fit_params.csv")
        df_sum.to_csv(out_csv, index=False)
        print(f"Parameters saved → {out_csv}")


if __name__ == "__main__":
    main()
