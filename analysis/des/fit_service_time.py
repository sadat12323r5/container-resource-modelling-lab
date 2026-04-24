#!/usr/bin/env python3
"""
fit_service_time.py
===================
Fits load-dependent service-time models to each server's summary CSV.

The important correction in this version is that the fitted hyperbolic model uses
nominal/base utilisation:

    rho0 = lambda * S0 / c
    E[S_eff] = S0 / (1 - beta * rho0)

The older version used observed rho from the summary CSV. Observed rho is computed
from the already-inflated service time, so using it in the denominator makes beta
harder to interpret. With rho0, beta has the clean processor-sharing meaning:
beta=0 recovers constant service time, beta=1 recovers full PS-style slowdown.
"""
import os
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit


SERVERS = {
    "Apache DSP (1c)":    ("apache_dsp_1c/apache_dsp_summary.csv",     1),
    "Apache MSG (1c)":    ("apache_msg_1c/apache_msg_summary.csv",     1),
    "Go lognormal (1c)":  ("go_1c/go_summary.csv",                    1),
    "Go SQLite (1c)":     ("go_sqlite_1c/go_sqlite_summary.csv",       1),
    "Java DSP (1c)":      ("java_dsp_1c/java_dsp_summary.csv",         1),
    "Node DSP (1c)":      ("node_dsp_1c/node_dsp_summary.csv",         1),
    "Python DSP (1c)":    ("python_dsp_1c/python_dsp_summary.csv",     1),
    "Python DSP (3c)":    ("python_dsp_3c/python_dsp_mc_summary.csv",  3),
    "Node DSP (3c)":      ("node_dsp_3c/node_dsp_mc_summary.csv",      3),
    "Java DSP (3c)":      ("java_dsp_3c/java_dsp_mc_summary.csv",      3),
    "Go SQLite (3c)":     ("go_sqlite_3c/go_sqlite_mc_summary.csv",    3),
}

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXP_BASE = os.path.join(ROOT, "data", "experiments")
FIG_DIR = os.path.join(ROOT, "results", "figures")
TABLE_DIR = os.path.join(ROOT, "results", "tables")
RHO_OBS_MAX = 0.92


def rho0_from_rate(rate_rps, s0_ms, workers):
    return rate_rps * s0_ms / (workers * 1000.0)


def m0_rate(rate_rps, s0_ms, workers):
    return np.full_like(rate_rps, s0_ms, dtype=float)


def m1_rate(rate_rps, s0_ms, alpha, workers):
    rho0 = rho0_from_rate(rate_rps, s0_ms, workers)
    return s0_ms + alpha * rho0


def m2_rate(rate_rps, s0_ms, beta, workers):
    rho0 = rho0_from_rate(rate_rps, s0_ms, workers)
    denom = np.maximum(1.0 - beta * rho0, 0.01)
    return s0_ms / denom


def m0_rho0(rho0, s0_ms):
    return np.full_like(rho0, s0_ms, dtype=float)


def m1_rho0(rho0, s0_ms, alpha):
    return s0_ms + alpha * rho0


def m2_rho0(rho0, s0_ms, beta):
    denom = np.maximum(1.0 - beta * rho0, 0.01)
    return s0_ms / denom


MODELS = {
    "M0 constant":   (m0_rate, m0_rho0, 1, ["S0"]),
    "M1 linear":     (m1_rate, m1_rho0, 2, ["S0", "alpha"]),
    "M2 hyperbolic": (m2_rate, m2_rho0, 2, ["S0", "beta"]),
}


def aic(n, k, residuals):
    sse = float(np.sum(residuals ** 2))
    if sse <= 0:
        return -np.inf
    return n * np.log(sse / n) + 2 * k


def r2_score(y, y_hat):
    denom = float(np.sum((y - y.mean()) ** 2))
    if denom <= 0:
        return 0.0
    return 1.0 - float(np.sum((y - y_hat) ** 2)) / denom


def fit_server(rate_rps, svc_ms, workers):
    results = {}
    best_key = None
    best_aic = np.inf

    for key, (fn_rate, fn_rho0, k, param_names) in MODELS.items():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if key == "M2 hyperbolic":
                    p0 = [float(svc_ms[0]), 0.5]
                    bounds = ([0.0, 0.0], [float(svc_ms.max()) * 10.0, 0.999])
                elif key == "M1 linear":
                    p0 = [float(svc_ms[0]), float(np.ptp(svc_ms))]
                    bounds = ([0.0, 0.0], [float(svc_ms.max()) * 10.0, float(svc_ms.max()) * 100.0])
                else:
                    p0 = [float(svc_ms.mean())]
                    bounds = ([0.0], [float(svc_ms.max()) * 10.0])

                def wrapped(rate_x, *params):
                    return fn_rate(rate_x, *params, workers)

                popt, _ = curve_fit(
                    wrapped,
                    rate_rps,
                    svc_ms,
                    p0=p0,
                    bounds=bounds,
                    maxfev=10000,
                )

            fitted = fn_rate(rate_rps, *popt, workers)
            residuals = svc_ms - fitted
            score = aic(len(svc_ms), k, residuals)
            rmse = float(np.sqrt(np.mean(residuals ** 2)))
            r2 = r2_score(svc_ms, fitted)

            results[key] = {
                "params": popt,
                "aic": score,
                "rmse": rmse,
                "r2": r2,
                "fn_rate": fn_rate,
                "fn_rho0": fn_rho0,
                "param_names": param_names,
            }
            if score < best_aic:
                best_aic = score
                best_key = key
        except Exception as exc:
            results[key] = {"error": str(exc)}

    return results, best_key


def main():
    summary_rows = []
    ncols = 3
    nrows = -(-len(SERVERS) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(15, nrows * 3.8))
    axes = axes.flatten()
    fig.suptitle(
        "Service Time vs Nominal Utilisation rho0 - Load-Dependent Model Fits",
        fontsize=13,
        fontweight="bold",
        y=1.01,
    )
    rho_dense = np.linspace(0.0, RHO_OBS_MAX, 200)
    colors = {"M0 constant": "#999999", "M1 linear": "#4daf4a", "M2 hyperbolic": "#377eb8"}

    for idx, (label, (rel_path, workers)) in enumerate(SERVERS.items()):
        ax = axes[idx]
        csv_path = os.path.join(EXP_BASE, rel_path)
        if not os.path.exists(csv_path):
            ax.set_visible(False)
            continue

        df = pd.read_csv(csv_path)
        df = df[df["rho"] <= RHO_OBS_MAX].copy()
        if len(df) < 2:
            ax.set_title(f"{label}\n(insufficient data)", fontsize=9)
            ax.set_visible(False)
            continue

        rate = df["rate_rps"].to_numpy(dtype=float)
        svc = df["mean_svc_ms"].to_numpy(dtype=float)
        fit_results, best_key = fit_server(rate, svc, workers)

        if not best_key:
            ax.set_title(f"{label}\n(fit failed)", fontsize=9)
            ax.set_visible(False)
            continue

        hyper = fit_results.get("M2 hyperbolic")
        export = hyper if hyper and "params" in hyper else fit_results[best_key]
        best = fit_results[best_key]
        s0 = float(export["params"][0])
        rho0_obs = rho0_from_rate(rate, s0, workers)

        ax.scatter(rho0_obs, svc, color="#e41a1c", zorder=5, s=40, label="Observed E[S]")
        for key, res in fit_results.items():
            if "params" not in res:
                continue
            y_hat = res["fn_rho0"](rho_dense, *res["params"])
            lw = 2.2 if key == "M2 hyperbolic" else 1.0
            ls = "-" if key == "M2 hyperbolic" else "--"
            ax.plot(rho_dense, y_hat, color=colors[key], lw=lw, ls=ls,
                    label=f"{key} (AIC={res['aic']:.0f})")

        params = export["params"]
        param_str = ", ".join(
            f"{name}={value:.3f}"
            for name, value in zip(export["param_names"], params)
        )
        ax.text(
            0.97,
            0.05,
            f"Hyperbolic fit\n{param_str}\nR2={export['r2']:.3f}\nAIC best: {best_key}",
            transform=ax.transAxes,
            fontsize=6.5,
            ha="right",
            va="bottom",
            color=colors["M2 hyperbolic"],
            bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8),
        )
        ax.set_xlabel("rho0 = lambda*S0/c", fontsize=8)
        ax.set_ylabel("E[S] (ms)", fontsize=8)
        ax.set_title(label, fontsize=9, fontweight="bold")
        ax.legend(fontsize=6.5, loc="upper left")
        ax.tick_params(labelsize=7)
        ax.set_xlim(0, RHO_OBS_MAX)
        ax.set_ylim(bottom=0)

        beta = float(params[1]) if len(params) > 1 and hyper and "params" in hyper else 0.0
        try:
            s_hi = float(export["fn_rho0"](np.array([0.8]), *params)[0])
            s_lo = float(export["fn_rho0"](np.array([0.1]), *params)[0])
            ratio = s_hi / s_lo if s_lo > 0 else float("nan")
        except Exception:
            ratio = float("nan")

        summary_rows.append({
            "Server": label,
            "c": workers,
            "S0": round(s0, 3),
            "best_model": best_key,
            "beta": round(beta, 3),
            "slope": round(float(fit_results["M1 linear"]["params"][1])
                           if "params" in fit_results.get("M1 linear", {}) else 0.0, 3),
            "R2": round(float(export["r2"]), 3),
            "RMSE_ms": round(float(export["rmse"]), 3),
            "rho0_min": round(float(np.min(rho0_obs)), 3),
            "rho0_max": round(float(np.max(rho0_obs)), 3),
            "rho_obs_min": round(float(df["rho"].min()), 3),
            "rho_obs_max": round(float(df["rho"].max()), 3),
            "ratio": round(ratio, 2),
            "constant?": "YES" if ratio < 1.15 or np.isnan(ratio) else "NO",
        })

    for ax in axes[len(SERVERS):]:
        ax.set_visible(False)

    plt.tight_layout()
    os.makedirs(FIG_DIR, exist_ok=True)
    out_png = os.path.join(FIG_DIR, "service_time_fit.png")
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"Plot saved -> {out_png}\n")

    if summary_rows:
        df_sum = pd.DataFrame(summary_rows)
        print("=" * 90)
        print("SERVICE TIME MODEL FIT SUMMARY")
        print("=" * 90)
        print(df_sum.to_string(index=False))
        print()
        print("Interpretation")
        print("-" * 90)
        print("  S0       - base service time extrapolated to zero load (ms)")
        print("  rho0     - nominal utilisation lambda*S0/c, not observed inflated rho")
        print("  beta     - M2 denominator shrinkage rate under rho0")
        print("  ratio    - E[S] at rho0=0.8 divided by E[S] at rho0=0.1")
        print("  constant - YES if service time is roughly constant (ratio < 1.15)")
        print()

        os.makedirs(TABLE_DIR, exist_ok=True)
        out_csv = os.path.join(TABLE_DIR, "service_time_fit_params.csv")
        df_sum.to_csv(out_csv, index=False)
        print(f"Parameters saved -> {out_csv}")


if __name__ == "__main__":
    main()
