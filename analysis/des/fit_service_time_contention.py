#!/usr/bin/env python3
"""
Compare service-time load models, including a contention-penalty alternative.

The existing M2 model is:

    E[S](rho0) = S0 / (1 - beta*rho0)

This script adds M3:

    E[S](rho0) = S0 * (1 + theta*rho0/(1-rho0))

M3 keeps the same FCFS queueing structure but interprets the extra wall-clock
service time as a CPU-contention penalty. theta=0 recovers constant service
time, and theta=1 recovers the full processor-sharing slowdown shape.

Outputs:
  results/tables/service_time_contention_fit_params.csv
  results/figures/service_time_contention_fit.png
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
RHO0_CLIP = 0.95


def rho0_from_rate(rate_rps, s0_ms, workers):
    return rate_rps * s0_ms / (workers * 1000.0)


def m0_rate(rate_rps, s0_ms, workers):
    return np.full_like(rate_rps, s0_ms, dtype=float)


def m1_rate(rate_rps, s0_ms, alpha, workers):
    rho0 = rho0_from_rate(rate_rps, s0_ms, workers)
    return s0_ms + alpha * rho0


def m2_rate(rate_rps, s0_ms, beta, workers):
    rho0 = np.minimum(rho0_from_rate(rate_rps, s0_ms, workers), RHO0_CLIP)
    return s0_ms / np.maximum(1.0 - beta * rho0, 0.01)


def m3_rate(rate_rps, s0_ms, theta, workers):
    rho0 = np.minimum(rho0_from_rate(rate_rps, s0_ms, workers), RHO0_CLIP)
    return s0_ms * (1.0 + theta * rho0 / np.maximum(1.0 - rho0, 0.01))


def m0_rho0(rho0, s0_ms):
    return np.full_like(rho0, s0_ms, dtype=float)


def m1_rho0(rho0, s0_ms, alpha):
    return s0_ms + alpha * rho0


def m2_rho0(rho0, s0_ms, beta):
    rho0 = np.minimum(rho0, RHO0_CLIP)
    return s0_ms / np.maximum(1.0 - beta * rho0, 0.01)


def m3_rho0(rho0, s0_ms, theta):
    rho0 = np.minimum(rho0, RHO0_CLIP)
    return s0_ms * (1.0 + theta * rho0 / np.maximum(1.0 - rho0, 0.01))


MODELS = {
    "M0 constant":    (m0_rate, m0_rho0, 1, ["S0"]),
    "M1 linear":      (m1_rate, m1_rho0, 2, ["S0", "alpha"]),
    "M2 beta-denom":  (m2_rate, m2_rho0, 2, ["S0", "beta"]),
    "M3 contention":  (m3_rate, m3_rho0, 2, ["S0", "theta"]),
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


def initial_and_bounds(key, svc_ms):
    max_s = float(svc_ms.max()) * 10.0
    if key == "M0 constant":
        return [float(svc_ms.mean())], ([0.0], [max_s])
    if key == "M1 linear":
        return [float(svc_ms[0]), float(np.ptp(svc_ms))], ([0.0, 0.0], [max_s, max_s * 10.0])
    if key == "M2 beta-denom":
        return [float(svc_ms[0]), 0.5], ([0.0, 0.0], [max_s, 0.999])
    if key == "M3 contention":
        return [float(svc_ms[0]), 0.5], ([0.0, 0.0], [max_s, 1.0])
    raise ValueError(key)


def fit_server(rate_rps, svc_ms, workers):
    results = {}
    best_key = None
    best_aic = np.inf

    for key, (fn_rate, fn_rho0, k, param_names) in MODELS.items():
        try:
            p0, bounds = initial_and_bounds(key, svc_ms)

            def wrapped(rate_x, *params):
                return fn_rate(rate_x, *params, workers)

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                popt, _ = curve_fit(
                    wrapped,
                    rate_rps,
                    svc_ms,
                    p0=p0,
                    bounds=bounds,
                    maxfev=20000,
                )

            fitted = fn_rate(rate_rps, *popt, workers)
            residuals = svc_ms - fitted
            score = aic(len(svc_ms), k, residuals)
            results[key] = {
                "params": popt,
                "aic": score,
                "rmse": float(np.sqrt(np.mean(residuals ** 2))),
                "r2": r2_score(svc_ms, fitted),
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
    os.makedirs(FIG_DIR, exist_ok=True)
    os.makedirs(TABLE_DIR, exist_ok=True)

    rows = []
    ncols = 3
    nrows = -(-len(SERVERS) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(15, nrows * 3.8))
    axes = axes.flatten()
    fig.suptitle("Service Time vs rho0 - Constant, Linear, Beta-Denominator, and Contention Fits",
                 fontsize=13, fontweight="bold", y=1.01)

    rho_dense = np.linspace(0.0, RHO_OBS_MAX, 240)
    colors = {
        "M0 constant": "#888888",
        "M1 linear": "#4daf4a",
        "M2 beta-denom": "#377eb8",
        "M3 contention": "#984ea3",
    }
    styles = {
        "M0 constant": "--",
        "M1 linear": ":",
        "M2 beta-denom": "-.",
        "M3 contention": "-",
    }

    for idx, (label, (rel_path, workers)) in enumerate(SERVERS.items()):
        ax = axes[idx]
        csv_path = os.path.join(EXP_BASE, rel_path)
        if not os.path.exists(csv_path):
            ax.set_visible(False)
            continue

        df = pd.read_csv(csv_path)
        df = df[df["rho"] <= RHO_OBS_MAX].copy()
        if len(df) < 2:
            ax.set_visible(False)
            continue

        rate = df["rate_rps"].to_numpy(dtype=float)
        svc = df["mean_svc_ms"].to_numpy(dtype=float)
        fit_results, best_key = fit_server(rate, svc, workers)
        if not best_key:
            ax.set_visible(False)
            continue

        preferred = fit_results.get("M3 contention")
        export = preferred if preferred and "params" in preferred else fit_results[best_key]
        s0_for_x = float(export["params"][0])
        rho0_obs = rho0_from_rate(rate, s0_for_x, workers)

        ax.scatter(rho0_obs, svc, color="#e41a1c", s=38, zorder=5, label="Observed E[S]")
        for key, res in fit_results.items():
            if "params" not in res:
                continue
            y_hat = res["fn_rho0"](rho_dense, *res["params"])
            ax.plot(rho_dense, y_hat, color=colors[key], ls=styles[key], lw=2.0,
                    label=f"{key} AIC={res['aic']:.0f}")

        m2 = fit_results.get("M2 beta-denom", {})
        m3 = fit_results.get("M3 contention", {})
        beta = float(m2["params"][1]) if "params" in m2 else float("nan")
        theta = float(m3["params"][1]) if "params" in m3 else float("nan")
        ax.text(
            0.97,
            0.05,
            f"best: {best_key}\nbeta={beta:.3f}\ntheta={theta:.3f}",
            transform=ax.transAxes,
            fontsize=7,
            ha="right",
            va="bottom",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.85),
        )
        ax.set_title(label, fontsize=9, fontweight="bold")
        ax.set_xlabel("rho0 = lambda*S0/c", fontsize=8)
        ax.set_ylabel("E[S] (ms)", fontsize=8)
        ax.set_xlim(0, RHO_OBS_MAX)
        ax.set_ylim(bottom=0)
        ax.legend(fontsize=6.0, loc="upper left")
        ax.tick_params(labelsize=7)

        row = {
            "Server": label,
            "c": workers,
            "best_model": best_key,
        }
        for key, res in fit_results.items():
            short = key.split()[0]
            if "params" in res:
                row[f"{short}_S0"] = round(float(res["params"][0]), 4)
                if len(res["params"]) > 1:
                    row[f"{short}_param"] = round(float(res["params"][1]), 4)
                row[f"{short}_aic"] = round(float(res["aic"]), 4)
                row[f"{short}_rmse"] = round(float(res["rmse"]), 4)
                row[f"{short}_r2"] = round(float(res["r2"]), 4)
        if "params" in m2 and "params" in m3:
            row["delta_aic_M2_minus_M3"] = round(float(m2["aic"] - m3["aic"]), 4)
        rows.append(row)

    for ax in axes[len(SERVERS):]:
        ax.set_visible(False)

    plt.tight_layout()
    out_png = os.path.join(FIG_DIR, "service_time_contention_fit.png")
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"Plot saved -> {out_png}")

    out_csv = os.path.join(TABLE_DIR, "service_time_contention_fit_params.csv")
    df_out = pd.DataFrame(rows)
    df_out.to_csv(out_csv, index=False)
    print(f"Parameters saved -> {out_csv}\n")

    cols = ["Server", "best_model", "M2_param", "M3_param", "M2_aic", "M3_aic", "delta_aic_M2_minus_M3"]
    cols = [c for c in cols if c in df_out.columns]
    print(df_out[cols].to_string(index=False))


if __name__ == "__main__":
    main()
