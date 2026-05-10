#!/usr/bin/env python3
"""
DES comparison for M0/M1/M2 plus the M3 contention-penalty model.

This is an exploratory companion to des_m0_m1_m2_compare.py. It asks whether
the more defensible M3 equation improves response-time CDF fit:

  M0: E[S] = S0
  M1: E[S] = S0 + alpha*rho0
  M2: E[S] = S0 / (1 - beta*rho0)
  M3: E[S] = S0 * (1 + theta*rho0/(1-rho0))

Outputs:
  results/tables/des_m0_m1_m2_m3_results.csv
  results/figures/des_m0_m1_m2_m3_cdf.png
"""
import math
import os
import random

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from des_m0_m1_m2_compare import (
    EXP_BASE,
    FIG_DIR,
    FOLDER_TO_LABEL,
    N_RUNS,
    RHO_MAX,
    SERVER_SPECS,
    TABLE_DIR,
    cdf_xy,
    estimate_cv,
    read_trace,
    run_des,
)
from des_utils import ks_distance


RHO0_CLIP = 0.95


def mean_service(model, s0_ms, alpha, beta, theta, rho0):
    rho = min(rho0, RHO0_CLIP)
    if model == "M0":
        return s0_ms
    if model == "M1":
        return s0_ms + alpha * rho
    if model == "M2":
        return s0_ms / max(0.01, 1.0 - beta * rho)
    if model == "M3":
        return s0_ms * (1.0 + theta * rho / max(0.01, 1.0 - rho))
    raise ValueError(model)


def safe_float(row, name, default=0.0):
    try:
        value = float(row[name])
        return value if math.isfinite(value) else default
    except Exception:
        return default


def main():
    fit_csv = os.path.join(TABLE_DIR, "service_time_contention_fit_params.csv")
    if not os.path.exists(fit_csv):
        raise SystemExit(f"{fit_csv} not found. Run fit_service_time_contention.py first.")

    fit_df = pd.read_csv(fit_csv)
    fit_map = {row["Server"]: row for _, row in fit_df.iterrows()}
    rows = []
    plot_payloads = []

    for _, (folder, workers, prefix) in SERVER_SPECS.items():
        exp_dir = os.path.join(EXP_BASE, folder)
        label = FOLDER_TO_LABEL[folder]
        if label not in fit_map:
            print(f"[skip] no fit row for {label}")
            continue

        fit = fit_map[label]
        s0_ms = safe_float(fit, "M3_S0", safe_float(fit, "M2_S0", 1.0))
        alpha = safe_float(fit, "M1_param", 0.0)
        beta = safe_float(fit, "M2_param", 0.0)
        theta = safe_float(fit, "M3_param", 0.0)
        cv = estimate_cv(exp_dir, prefix)

        summary_files = [
            os.path.join(exp_dir, f)
            for f in os.listdir(exp_dir)
            if f.endswith("_summary.csv")
        ]
        if not summary_files:
            continue
        summary = pd.read_csv(summary_files[0])

        trace_files = sorted(
            f for f in os.listdir(exp_dir)
            if f.startswith(prefix) and f.endswith("rps.csv") and "_des_" not in f
        )

        for trace_file in trace_files:
            try:
                rate = int(trace_file.replace(prefix, "").replace("rps.csv", ""))
            except ValueError:
                continue

            summary_row = summary[summary["rate_rps"] == rate]
            if summary_row.empty:
                continue
            rho_obs = float(summary_row["rho"].iloc[0])
            if rho_obs > RHO_MAX:
                continue

            arrivals, observed, _ = read_trace(os.path.join(exp_dir, trace_file))
            if len(arrivals) < 30:
                continue

            rho0 = rate * s0_ms / (workers * 1000.0)
            observed_sorted = sorted(observed)
            sim_by_model = {}
            ks_by_model = {}
            mean_by_model = {}

            for model in ("M0", "M1", "M2", "M3"):
                model_mean = mean_service(model, s0_ms, alpha, beta, theta, rho0)
                sim = []
                for seed in range(N_RUNS):
                    sim.extend(run_des(arrivals, model_mean, cv, workers, seed))
                sim_sorted = sorted(sim)
                ks, _ = ks_distance(observed_sorted, sim_sorted)
                sim_by_model[model] = sim_sorted
                ks_by_model[model] = ks
                mean_by_model[model] = model_mean

            best_ks_model = min(ks_by_model, key=ks_by_model.get)
            row = {
                "server": label,
                "rate_rps": rate,
                "workers": workers,
                "rho_obs": round(rho_obs, 3),
                "rho0": round(rho0, 3),
                "S0_ms": round(s0_ms, 3),
                "alpha": round(alpha, 3),
                "beta": round(beta, 3),
                "theta": round(theta, 3),
                "M0_mean_ms": round(mean_by_model["M0"], 3),
                "M1_mean_ms": round(mean_by_model["M1"], 3),
                "M2_mean_ms": round(mean_by_model["M2"], 3),
                "M3_mean_ms": round(mean_by_model["M3"], 3),
                "ks_M0": round(ks_by_model["M0"], 4),
                "ks_M1": round(ks_by_model["M1"], 4),
                "ks_M2": round(ks_by_model["M2"], 4),
                "ks_M3": round(ks_by_model["M3"], 4),
                "delta_M2_minus_M3": round(ks_by_model["M2"] - ks_by_model["M3"], 4),
                "delta_M0_minus_M3": round(ks_by_model["M0"] - ks_by_model["M3"], 4),
                "best_ks_model": best_ks_model,
                "M3_beats_M2": ks_by_model["M3"] + 0.005 < ks_by_model["M2"],
                "M3_beats_M0": ks_by_model["M3"] + 0.005 < ks_by_model["M0"],
            }
            rows.append(row)
            plot_payloads.append((row["delta_M0_minus_M3"], row, observed_sorted, sim_by_model))

    if not rows:
        raise SystemExit("No comparison rows generated.")

    os.makedirs(TABLE_DIR, exist_ok=True)
    out_csv = os.path.join(TABLE_DIR, "des_m0_m1_m2_m3_results.csv")
    df = pd.DataFrame(rows)
    df.to_csv(out_csv, index=False)
    print(f"Saved {out_csv}\n")

    print("Average KS by service-time model:")
    print(df[["ks_M0", "ks_M1", "ks_M2", "ks_M3"]].mean().round(4).to_string())

    print("\nTop cases where M3 improves over M0:")
    wins = df[df["M3_beats_M0"]].sort_values("delta_M0_minus_M3", ascending=False)
    if wins.empty:
        print("  none")
    else:
        print(wins[[
            "server", "rate_rps", "rho0", "theta", "ks_M0", "ks_M2", "ks_M3",
            "delta_M0_minus_M3", "best_ks_model",
        ]].head(12).to_string(index=False))

    selected = sorted(plot_payloads, key=lambda item: item[0], reverse=True)[:6]
    ncols = 2
    nrows = math.ceil(len(selected) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4.2 * nrows))
    axes = np.array(axes).reshape(-1)
    fig.suptitle("CDF_REF vs DES using M0, M1, M2, and M3 service-time models",
                 fontsize=13, fontweight="bold")
    colors = {"M0": "#d62728", "M1": "#ff7f0e", "M2": "#2ca02c", "M3": "#984ea3"}
    styles = {"M0": "--", "M1": ":", "M2": "-.", "M3": "-"}

    for ax, (_, row, observed, sim_by_model) in zip(axes, selected):
        ax.step(*cdf_xy(observed), where="post", color="#1f77b4", lw=2.0, label="CDF_REF")
        for model in ("M0", "M1", "M2", "M3"):
            ax.step(*cdf_xy(sim_by_model[model]), where="post",
                    color=colors[model], ls=styles[model], lw=1.5,
                    label=f"CDF_{model} KS={row[f'ks_{model}']:.3f}")
        ax.set_title(
            f"{row['server']} @ {row['rate_rps']} rps "
            f"(rho0={row['rho0']:.2f}, theta={row['theta']:.2f})\n"
            f"M0-M3 KS delta={row['delta_M0_minus_M3']:+.3f}",
            fontsize=9,
        )
        ax.set_xlabel("Response time (ms)")
        ax.set_ylabel("CDF")
        ax.grid(True, alpha=0.25)
        ax.legend(fontsize=7, loc="lower right")

    for ax in axes[len(selected):]:
        ax.set_visible(False)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    os.makedirs(FIG_DIR, exist_ok=True)
    out_png = os.path.join(FIG_DIR, "des_m0_m1_m2_m3_cdf.png")
    plt.savefig(out_png, dpi=150)
    print(f"\nSaved {out_png}")


if __name__ == "__main__":
    random.seed(42)
    main()
