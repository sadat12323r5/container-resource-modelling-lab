#!/usr/bin/env python3
"""
Generate focused follow-up plots requested by the supervisor:

1. CDF comparisons where M2 clearly improves over M0.
2. Service-time histograms by arrival rate for load-dependent workloads.

Outputs:
  results/figures/m2_beats_m0_cdf.png
  results/tables/m2_beats_m0_cases.csv
  results/figures/service_time_hist_<folder>.png
"""
import math
import os

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
    SERVER_SPECS,
    TABLE_DIR,
    cdf_xy,
    estimate_cv,
    mean_service,
    read_trace,
    run_des,
)
from des_utils import ks_distance


FOCUS_FOLDERS = [
    "python_dsp_3c",
    "apache_dsp_1c",
    "go_1c",
    "go_sqlite_3c",
]


def load_fit_map():
    fit_csv = os.path.join(TABLE_DIR, "service_time_fit_params.csv")
    fit_df = pd.read_csv(fit_csv)
    return {row["Server"]: row for _, row in fit_df.iterrows()}


def spec_by_folder():
    return {
        folder: (tag, workers, prefix)
        for tag, (folder, workers, prefix) in SERVER_SPECS.items()
    }


def regenerate_simulations(row, fit_map, specs):
    folder = next(
        folder for folder, label in FOLDER_TO_LABEL.items()
        if label == row["server"]
    )
    _, workers, prefix = specs[folder]
    exp_dir = os.path.join(EXP_BASE, folder)
    fit = fit_map[row["server"]]

    s0_ms = float(fit["S0"])
    alpha = float(fit["slope"])
    beta = float(fit["beta"])
    cv = estimate_cv(exp_dir, prefix)
    rate = int(row["rate_rps"])
    rho0 = float(row["rho0"])
    trace_path = os.path.join(exp_dir, f"{prefix}{rate}rps.csv")
    arrivals, observed, _ = read_trace(trace_path)
    observed = sorted(observed)

    sims = {}
    for model in ("M0", "M1", "M2"):
        svc_mean = mean_service(model, s0_ms, alpha, beta, rho0)
        sim = []
        for seed in range(N_RUNS):
            sim.extend(run_des(arrivals, svc_mean, cv, workers, seed))
        sims[model] = sorted(sim)

    return observed, sims


def plot_m2_beats_m0():
    table_path = os.path.join(TABLE_DIR, "des_m0_m1_m2_results.csv")
    df = pd.read_csv(table_path)
    df["delta_M0_minus_M2"] = df["ks_M0"] - df["ks_M2"]

    cases = (
        df[df["delta_M0_minus_M2"] > 0.03]
        .sort_values("delta_M0_minus_M2", ascending=False)
        .head(6)
        .copy()
    )

    os.makedirs(TABLE_DIR, exist_ok=True)
    out_cases = os.path.join(TABLE_DIR, "m2_beats_m0_cases.csv")
    cases.to_csv(out_cases, index=False)

    fit_map = load_fit_map()
    specs = spec_by_folder()

    ncols = 2
    nrows = math.ceil(len(cases) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4.2 * nrows))
    axes = np.array(axes).reshape(-1)
    fig.suptitle("Cases where CDF_M2 fits CDF_REF better than CDF_M0",
                 fontsize=13, fontweight="bold")

    for ax, (_, row) in zip(axes, cases.iterrows()):
        observed, sims = regenerate_simulations(row, fit_map, specs)
        ax.step(*cdf_xy(observed), where="post", color="#1f77b4",
                lw=2.2, label="CDF_REF")
        ax.step(*cdf_xy(sims["M0"]), where="post", color="#d62728",
                lw=1.5, ls="--", label=f"CDF_M0 KS={row['ks_M0']:.3f}")
        ax.step(*cdf_xy(sims["M1"]), where="post", color="#ff7f0e",
                lw=1.3, ls=":", label=f"CDF_M1 KS={row['ks_M1']:.3f}")
        ax.step(*cdf_xy(sims["M2"]), where="post", color="#2ca02c",
                lw=1.8, ls="-.", label=f"CDF_M2 KS={row['ks_M2']:.3f}")
        ax.set_title(
            f"{row['server']} {int(row['rate_rps'])} rps "
            f"(rho0={row['rho0']:.2f}, beta={row['beta']:.2f})\n"
            f"KS improvement M0-M2 = {row['delta_M0_minus_M2']:+.3f}",
            fontsize=9,
        )
        ax.set_xlabel("Response time (ms)")
        ax.set_ylabel("CDF")
        ax.set_xlim(left=0)
        ax.legend(fontsize=7)

    for ax in axes[len(cases):]:
        ax.set_visible(False)

    plt.tight_layout()
    os.makedirs(FIG_DIR, exist_ok=True)
    out_png = os.path.join(FIG_DIR, "m2_beats_m0_cdf.png")
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"Saved {out_png}")
    print(f"Saved {out_cases}")


def plot_service_histograms():
    specs = spec_by_folder()

    for folder in FOCUS_FOLDERS:
        if folder not in specs:
            continue
        _, _, prefix = specs[folder]
        exp_dir = os.path.join(EXP_BASE, folder)
        label = FOLDER_TO_LABEL[folder]

        trace_files = sorted(
            f for f in os.listdir(exp_dir)
            if f.startswith(prefix) and f.endswith("rps.csv") and "_des_" not in f
        )

        traces = []
        for trace_file in trace_files:
            try:
                rate = int(trace_file.replace(prefix, "").replace("rps.csv", ""))
            except ValueError:
                continue
            _, _, service = read_trace(os.path.join(exp_dir, trace_file))
            service = np.array(
                [s for s in service if math.isfinite(s) and s > 0],
                dtype=float,
            )
            if len(service) > 30:
                traces.append((rate, service))

        if not traces:
            continue

        traces.sort(key=lambda item: item[0])
        ncols = min(4, len(traces))
        nrows = math.ceil(len(traces) / ncols)
        fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.2 * nrows))
        axes = np.array(axes).reshape(-1)
        fig.suptitle(f"Service-time distributions by arrival rate: {label}",
                     fontsize=13, fontweight="bold")

        for ax, (rate, service) in zip(axes, traces):
            mean = float(service.mean())
            median = float(np.median(service))
            panel_xmax = float(np.percentile(service, 99.5))
            panel_xmax = max(panel_xmax, mean * 1.25, median * 1.5)
            clipped = service[service <= panel_xmax]
            ax.hist(clipped, bins=35, density=True, color="#7aa6c2",
                    edgecolor="white", alpha=0.9)
            ax.axvline(mean, color="#d62728", lw=1.6, label=f"mean={mean:.2f} ms")
            ax.axvline(median, color="#2ca02c", lw=1.2, ls="--",
                       label=f"median={median:.2f} ms")
            ax.set_title(f"{rate} rps  n={len(service)}", fontsize=9)
            ax.set_xlabel("service_ms")
            ax.set_ylabel("density")
            ax.set_xlim(left=0, right=panel_xmax)
            ax.legend(fontsize=7)

        for ax in axes[len(traces):]:
            ax.set_visible(False)

        plt.tight_layout()
        out_png = os.path.join(FIG_DIR, f"service_time_hist_{folder}.png")
        plt.savefig(out_png, dpi=150, bbox_inches="tight")
        print(f"Saved {out_png}")


def main():
    plot_m2_beats_m0()
    plot_service_histograms()


if __name__ == "__main__":
    main()
