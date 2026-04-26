#!/usr/bin/env python3
"""
Compare measured response-time CDFs against DES driven by service-time models:

  M0: E[S] = S0
  M1: E[S] = S0 + alpha*rho0
  M2: E[S] = S0 / (1 - beta*rho0)

This script answers the supervisor's specific question: does the model that fits
service time better also improve the simulated response-time distribution?

Outputs:
  results/tables/des_m0_m1_m2_results.csv
  results/figures/des_m0_m1_m2_cdf.png
"""
import csv
import math
import os
import random

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from des_utils import ks_distance


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXP_BASE = os.path.join(ROOT, "data", "experiments")
FIG_DIR = os.path.join(ROOT, "results", "figures")
TABLE_DIR = os.path.join(ROOT, "results", "tables")
N_RUNS = 5
RHO_MAX = 0.92


SERVER_SPECS = {
    # tag            folder            c  trace_prefix
    "apache_dsp":   ("apache_dsp_1c",  1, "apache_dsp_"),
    "apache_msg":   ("apache_msg_1c",  1, "apache_msg_"),
    "go":           ("go_1c",          1, "go_"),
    "go_sqlite":    ("go_sqlite_1c",   1, "go_sqlite_"),
    "java_dsp":     ("java_dsp_1c",    4, "java_dsp_"),
    "node_dsp":     ("node_dsp_1c",    1, "node_dsp_"),
    "python_dsp":   ("python_dsp_1c",  1, "python_dsp_"),
    "python_dsp3":  ("python_dsp_3c",  3, "python_dsp_mc_"),
    "node_dsp3":    ("node_dsp_3c",    3, "node_dsp_mc_"),
    "java_dsp3":    ("java_dsp_3c",    3, "java_dsp_mc_"),
    "go_sqlite3":   ("go_sqlite_3c",   3, "go_sqlite_mc_"),
}


FOLDER_TO_LABEL = {
    "apache_dsp_1c": "Apache DSP (1c)",
    "apache_msg_1c": "Apache MSG (1c)",
    "go_1c": "Go lognormal (1c)",
    "go_sqlite_1c": "Go SQLite (1c)",
    "java_dsp_1c": "Java DSP (1c)",
    "node_dsp_1c": "Node DSP (1c)",
    "python_dsp_1c": "Python DSP (1c)",
    "python_dsp_3c": "Python DSP (3c)",
    "node_dsp_3c": "Node DSP (3c)",
    "java_dsp_3c": "Java DSP (3c)",
    "go_sqlite_3c": "Go SQLite (3c)",
}


def ln_params(mean_s, cv):
    if cv < 1e-4:
        return math.log(max(mean_s, 1e-12)), 1e-4
    sigma2 = math.log(1.0 + cv * cv)
    mu = math.log(max(mean_s, 1e-12)) - sigma2 / 2.0
    return mu, math.sqrt(sigma2)


def sample_lognormal(rng, mean_s, cv):
    mu, sig = ln_params(mean_s, cv)
    return math.exp(mu + sig * rng.gauss(0.0, 1.0))


def read_trace(path):
    arrivals_ns = []
    response_ms = []
    service_ms = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                status = row.get("status_code", "200").strip()
                if not status.startswith("2"):
                    continue
                arrival = float(row["arrival_unix_ns"])
                response = float(row["response_ms"])
                service = float(row.get("service_ms", "nan"))
            except (KeyError, ValueError):
                continue
            if math.isfinite(response) and response > 0:
                arrivals_ns.append(arrival)
                response_ms.append(response)
                service_ms.append(service)

    if not arrivals_ns:
        return [], [], []

    rows = sorted(zip(arrivals_ns, response_ms, service_ms))
    t0 = rows[0][0]
    arrivals_ms = [(a - t0) / 1e6 for a, _, _ in rows]
    responses = [r for _, r, _ in rows]
    services = [s for _, _, s in rows]
    return arrivals_ms, responses, services


def estimate_cv(exp_dir, prefix):
    trace_files = sorted(
        f for f in os.listdir(exp_dir)
        if f.startswith(prefix) and f.endswith("rps.csv") and "_des_" not in f
    )
    for trace_file in trace_files:
        _, _, svc = read_trace(os.path.join(exp_dir, trace_file))
        valid = np.array([s for s in svc if math.isfinite(s) and s > 0], dtype=float)
        if len(valid) > 30:
            mean = float(valid.mean())
            return float(valid.std() / mean) if mean > 0 else 0.5
    return 0.5


def mean_service(model, s0_ms, alpha, beta, rho0):
    if model == "M0":
        return s0_ms
    if model == "M1":
        return s0_ms + alpha * rho0
    if model == "M2":
        return s0_ms / max(0.01, 1.0 - beta * min(rho0, 0.95))
    raise ValueError(model)


def run_des(arrivals_ms, mean_svc_ms, cv, workers, seed):
    rng = random.Random(seed)
    free_at = [0.0] * workers
    responses = []

    for arrival in arrivals_ms:
        worker = min(range(workers), key=lambda idx: free_at[idx])
        start = max(arrival, free_at[worker])
        service = sample_lognormal(rng, mean_svc_ms, cv)
        finish = start + service
        free_at[worker] = finish
        responses.append(finish - arrival)

    return responses


def cdf_xy(values):
    values = sorted(values)
    n = len(values)
    return values, [i / n for i in range(1, n + 1)]


def main():
    fit_csv = os.path.join(TABLE_DIR, "service_time_fit_params.csv")
    if not os.path.exists(fit_csv):
        raise SystemExit(f"{fit_csv} not found. Run fit_service_time.py first.")

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
        s0_ms = float(fit["S0"])
        alpha = float(fit["slope"])
        beta = float(fit["beta"])
        best_model = str(fit["best_model"])
        cv = estimate_cv(exp_dir, prefix)

        summary_files = [
            os.path.join(exp_dir, f)
            for f in os.listdir(exp_dir)
            if f.endswith("_summary.csv")
        ]
        if not summary_files:
            print(f"[skip] no summary csv in {exp_dir}")
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
            for model in ("M0", "M1", "M2"):
                model_mean = mean_service(model, s0_ms, alpha, beta, rho0)
                sim = []
                for seed in range(N_RUNS):
                    sim.extend(run_des(arrivals, model_mean, cv, workers, seed))
                sim_sorted = sorted(sim)
                ks, _ = ks_distance(observed_sorted, sim_sorted)
                sim_by_model[model] = sim_sorted
                ks_by_model[model] = ks
                mean_by_model[model] = model_mean

            best_ks_model = min(ks_by_model, key=ks_by_model.get)
            delta_m1_m2 = ks_by_model["M1"] - ks_by_model["M2"]
            row = {
                "server": label,
                "rate_rps": rate,
                "workers": workers,
                "rho_obs": round(rho_obs, 3),
                "rho0": round(rho0, 3),
                "S0_ms": round(s0_ms, 3),
                "alpha": round(alpha, 3),
                "beta": round(beta, 3),
                "fit_best_model": best_model,
                "M0_mean_ms": round(mean_by_model["M0"], 3),
                "M1_mean_ms": round(mean_by_model["M1"], 3),
                "M2_mean_ms": round(mean_by_model["M2"], 3),
                "ks_M0": round(ks_by_model["M0"], 4),
                "ks_M1": round(ks_by_model["M1"], 4),
                "ks_M2": round(ks_by_model["M2"], 4),
                "delta_M1_minus_M2": round(delta_m1_m2, 4),
                "best_ks_model": best_ks_model,
                "M2_beats_M1": delta_m1_m2 > 0.005,
            }
            rows.append(row)
            plot_payloads.append((delta_m1_m2, row, observed_sorted, sim_by_model))

    if not rows:
        raise SystemExit("No comparison rows generated.")

    os.makedirs(TABLE_DIR, exist_ok=True)
    out_csv = os.path.join(TABLE_DIR, "des_m0_m1_m2_results.csv")
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"Saved {out_csv}")

    df = pd.DataFrame(rows)
    print("\nCases where M2 beats M1 by KS > 0.005:")
    wins = df[df["M2_beats_M1"]].sort_values("delta_M1_minus_M2", ascending=False)
    if wins.empty:
        print("  none")
    else:
        print(wins[[
            "server", "rate_rps", "rho0", "beta", "ks_M1", "ks_M2",
            "delta_M1_minus_M2", "best_ks_model",
        ]].to_string(index=False))

    print("\nAverage KS by service-time model:")
    print(df[["ks_M0", "ks_M1", "ks_M2"]].mean().round(4).to_string())

    # Plot top M2 wins first; if there are few wins, include biggest losses too.
    plot_payloads.sort(key=lambda item: item[0], reverse=True)
    selected = plot_payloads[:6]
    if len(selected) < 6:
        selected = plot_payloads[:]
    selected += plot_payloads[-max(0, 6 - len(selected)):]
    selected = selected[:6]

    ncols = 2
    nrows = math.ceil(len(selected) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4.2 * nrows))
    axes = np.array(axes).reshape(-1)
    fig.suptitle("CDF_REF vs DES using M0, M1, and M2 service-time models",
                 fontsize=13, fontweight="bold")

    colors = {"M0": "#d62728", "M1": "#ff7f0e", "M2": "#2ca02c"}
    styles = {"M0": "--", "M1": ":", "M2": "-."}
    for ax, (_, row, observed, sim_by_model) in zip(axes, selected):
        ax.step(*cdf_xy(observed), where="post", color="#1f77b4",
                lw=2.0, label="CDF_REF")
        for model in ("M0", "M1", "M2"):
            ax.step(*cdf_xy(sim_by_model[model]), where="post",
                    color=colors[model], ls=styles[model], lw=1.5,
                    label=f"CDF_{model} KS={row[f'ks_{model}']:.3f}")
        ax.set_title(
            f"{row['server']} {row['rate_rps']} rps "
            f"(rho0={row['rho0']:.2f}, beta={row['beta']:.2f})\n"
            f"M1-M2 KS delta={row['delta_M1_minus_M2']:+.3f}",
            fontsize=9,
        )
        ax.set_xlabel("Response time (ms)")
        ax.set_ylabel("CDF")
        ax.set_xlim(left=0)
        ax.legend(fontsize=7)

    for ax in axes[len(selected):]:
        ax.set_visible(False)

    plt.tight_layout()
    os.makedirs(FIG_DIR, exist_ok=True)
    out_png = os.path.join(FIG_DIR, "des_m0_m1_m2_cdf.png")
    plt.savefig(out_png, dpi=140, bbox_inches="tight")
    print(f"Saved {out_png}")


if __name__ == "__main__":
    main()
