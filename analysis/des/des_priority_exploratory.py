#!/usr/bin/env python3
"""
Exploratory validation of two explicit scheduling models.

These are not Thesis B production models because the traces do not label a
second traffic class or feedback events. The goal is narrower: test whether
either explicit scheduling structure can explain response-time CDF mismatches
better than the one-stage FCFS DES.

Models tested against observed DSP-class response times:

  FCFS_M0:
      observed arrivals -> one FCFS server with base service S0

  PRIO_PREEMPT:
      observed DSP arrivals are low priority (class 2). A synthetic short
      high-priority class (class 1) arrives as a Poisson stream. Class 1
      preempts class 2, and class 2 resumes from its remaining service.

  FEEDBACK_PRIO:
      observed DSP arrivals first receive a high-priority initial CPU quantum.
      Unfinished work feeds back into a low-priority queue. High priority is
      always served first; low-priority jobs resume later.

Outputs:
  results/tables/des_priority_exploratory_results.csv
  results/figures/des_priority_exploratory_cdf.png
"""
import heapq
import math
import os
import random
from collections import deque

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from des_m0_m1_m2_compare import (
    EXP_BASE,
    FIG_DIR,
    FOLDER_TO_LABEL,
    RHO_MAX,
    SERVER_SPECS,
    TABLE_DIR,
    cdf_xy,
    estimate_cv,
    read_trace,
    run_des,
    sample_lognormal,
)
from des_utils import ks_distance


N_RUNS = 1

# Keep this exploratory run intentionally small. These are the cases that were
# already useful in the M0/M1/M2 supervisor follow-up, plus Python 1c as a
# constant-service control.
SELECTED_RATES = {
    "apache_dsp_1c": {50, 75, 100},
    "go_1c": {100, 200, 400},
    "node_dsp_1c": {200, 400},
    "python_dsp_1c": {25, 50},
}

PREEMPT_LAMBDA1_RATIOS = [0.10, 0.30, 0.60]
PREEMPT_S1_FRACS = [0.05, 0.15]
FEEDBACK_QUANTILE_FRACS = [0.10, 0.25, 0.50]


def read_fit_params():
    fit_csv = os.path.join(TABLE_DIR, "service_time_fit_params.csv")
    if not os.path.exists(fit_csv):
        raise SystemExit(f"{fit_csv} not found. Run fit_service_time.py first.")
    fit_df = pd.read_csv(fit_csv)
    return {row["Server"]: row for _, row in fit_df.iterrows()}


def generate_poisson_arrivals(rate_per_ms, horizon_ms, rng):
    if rate_per_ms <= 0:
        return []
    t = 0.0
    arrivals = []
    while t < horizon_ms:
        t += rng.expovariate(rate_per_ms)
        if t <= horizon_ms:
            arrivals.append(t)
    return arrivals


def make_observed_jobs(arrivals_ms, rng, mean_ms, cv):
    return [
        {"cls": 2, "arrival": arr, "service": sample_lognormal(rng, mean_ms, cv), "idx": idx}
        for idx, arr in enumerate(arrivals_ms)
    ]


def simulate_preemptive_resume(arrivals_ms, mean2_ms, cv2, lambda1_ratio, s1_frac, seed):
    """
    Two-class M/G/1 preemptive-resume simulation.

    Class 2 jobs are observed DSP arrivals. Class 1 jobs are synthetic short
    high-priority arrivals. Only class 2 response times are returned.
    """
    rng = random.Random(seed)
    if not arrivals_ms:
        return []

    horizon = max(arrivals_ms)
    observed_rate_per_ms = len(arrivals_ms) / max(horizon, 1e-9)
    class1_arrivals = generate_poisson_arrivals(
        observed_rate_per_ms * lambda1_ratio,
        horizon,
        rng,
    )

    events = []
    for job in make_observed_jobs(arrivals_ms, rng, mean2_ms, cv2):
        heapq.heappush(events, (job["arrival"], "arrival", job))
    for idx, arr in enumerate(class1_arrivals):
        svc = sample_lognormal(rng, max(mean2_ms * s1_frac, 1e-6), cv2)
        heapq.heappush(events, (arr, "arrival", {"cls": 1, "arrival": arr, "service": svc, "idx": idx}))

    high_q = deque()
    low_q = deque()
    current = None
    current_start = 0.0
    t = 0.0
    class2_responses = []

    def dispatch(now):
        nonlocal current, current_start
        if current is not None:
            return
        if high_q:
            current = high_q.popleft()
        elif low_q:
            current = low_q.popleft()
        else:
            return
        current_start = now
        heapq.heappush(events, (now + current["remaining"], "complete", None))

    while events:
        event_t, kind, payload = heapq.heappop(events)
        if current is not None and event_t < current_start + current["remaining"] - 1e-9:
            elapsed = max(0.0, event_t - current_start)
            current["remaining"] -= elapsed
            current_start = event_t
        t = event_t

        if kind == "arrival":
            job = payload
            job["remaining"] = job["service"]
            if job["cls"] == 1:
                if current is not None and current["cls"] == 2:
                    low_q.appendleft(current)
                    current = None
                high_q.append(job)
            else:
                low_q.append(job)
            dispatch(t)
            continue

        if kind == "complete":
            if current is None:
                continue
            expected_finish = current_start + current["remaining"]
            if abs(t - expected_finish) > 1e-6:
                continue
            finished = current
            current = None
            if finished["cls"] == 2:
                class2_responses.append(t - finished["arrival"])
            dispatch(t)

    return class2_responses


def simulate_feedback_priority(arrivals_ms, mean_ms, cv, quantum_frac, seed):
    """
    Two-level feedback priority simulation for one class of DSP jobs.

    Each job first receives a high-priority CPU quantum. If service remains,
    it moves to a low-priority queue and resumes later. Only complete-job
    response times are returned.
    """
    rng = random.Random(seed)
    events = []
    quantum = max(mean_ms * quantum_frac, 1e-6)
    for job in make_observed_jobs(arrivals_ms, rng, mean_ms, cv):
        job["remaining"] = job["service"]
        heapq.heappush(events, (job["arrival"], "arrival", job))

    high_q = deque()
    low_q = deque()
    current = None
    current_start = 0.0
    responses = []

    def dispatch(now):
        nonlocal current, current_start
        if current is not None:
            return
        if high_q:
            current = high_q.popleft()
            current["phase"] = "high"
            run_for = min(current["remaining"], quantum)
        elif low_q:
            current = low_q.popleft()
            current["phase"] = "low"
            run_for = current["remaining"]
        else:
            return
        current["run_for"] = run_for
        current_start = now
        heapq.heappush(events, (now + run_for, "complete_or_feedback", None))

    while events:
        event_t, kind, payload = heapq.heappop(events)
        if current is not None and event_t < current_start + current["run_for"] - 1e-9:
            # New high-priority arrivals preempt low-priority work.
            if kind == "arrival" and current["phase"] == "low":
                elapsed = max(0.0, event_t - current_start)
                current["remaining"] -= elapsed
                low_q.appendleft(current)
                current = None
            else:
                # Stale or irrelevant event; let the scheduled completion handle it.
                pass
        t = event_t

        if kind == "arrival":
            high_q.append(payload)
            dispatch(t)
            continue

        if kind == "complete_or_feedback":
            if current is None:
                dispatch(t)
                continue
            expected_finish = current_start + current["run_for"]
            if abs(t - expected_finish) > 1e-6:
                continue
            current["remaining"] -= current["run_for"]
            finished = current
            current = None
            if finished["remaining"] <= 1e-9:
                responses.append(t - finished["arrival"])
            else:
                low_q.append(finished)
            dispatch(t)

    return responses


def best_preemptive(arrivals, observed_sorted, mean_ms, cv):
    best = None
    for lambda1_ratio in PREEMPT_LAMBDA1_RATIOS:
        for s1_frac in PREEMPT_S1_FRACS:
            sim = []
            for run in range(N_RUNS):
                sim.extend(simulate_preemptive_resume(
                    arrivals,
                    mean_ms,
                    cv,
                    lambda1_ratio,
                    s1_frac,
                    seed=11000 + run,
                ))
            sim_sorted = sorted(sim)
            if len(sim_sorted) < 30:
                continue
            ks, _ = ks_distance(observed_sorted, sim_sorted)
            cand = (ks, lambda1_ratio, s1_frac, sim_sorted)
            if best is None or cand[0] < best[0]:
                best = cand
    return best


def best_feedback(arrivals, observed_sorted, mean_ms, cv):
    best = None
    for quantum_frac in FEEDBACK_QUANTILE_FRACS:
        sim = []
        for run in range(N_RUNS):
            sim.extend(simulate_feedback_priority(
                arrivals,
                mean_ms,
                cv,
                quantum_frac,
                seed=22000 + run,
            ))
        sim_sorted = sorted(sim)
        if len(sim_sorted) < 30:
            continue
        ks, _ = ks_distance(observed_sorted, sim_sorted)
        cand = (ks, quantum_frac, sim_sorted)
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def m2_mean_service(s0_ms, beta, rate, workers=1):
    rho0 = rate * s0_ms / (workers * 1000.0)
    return s0_ms / max(0.01, 1.0 - beta * min(rho0, 0.95))


def main():
    fit_map = read_fit_params()
    rows = []
    plots = []

    for _, (folder, workers, prefix) in SERVER_SPECS.items():
        if folder not in SELECTED_RATES or workers != 1:
            continue
        exp_dir = os.path.join(EXP_BASE, folder)
        label = FOLDER_TO_LABEL[folder]
        fit = fit_map[label]
        s0_ms = float(fit["S0"])
        beta = float(fit["beta"])
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
            if rate not in SELECTED_RATES[folder]:
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
            observed_sorted = sorted(observed)

            fcfs_sim = []
            for run in range(N_RUNS):
                fcfs_sim.extend(run_des(arrivals, s0_ms, cv, 1, seed=33000 + run))
            fcfs_sorted = sorted(fcfs_sim)
            ks_fcfs, _ = ks_distance(observed_sorted, fcfs_sorted)

            m2_sim = []
            m2_mean = m2_mean_service(s0_ms, beta, rate, workers=1)
            for run in range(N_RUNS):
                m2_sim.extend(run_des(arrivals, m2_mean, cv, 1, seed=44000 + run))
            m2_sorted = sorted(m2_sim)
            ks_m2, _ = ks_distance(observed_sorted, m2_sorted)

            preempt = best_preemptive(arrivals, observed_sorted, s0_ms, cv)
            feedback = best_feedback(arrivals, observed_sorted, s0_ms, cv)
            if preempt is None or feedback is None:
                continue
            ks_preempt, lambda1_ratio, s1_frac, preempt_sorted = preempt
            ks_feedback, quantum_frac, feedback_sorted = feedback
            best_model = min(
                [
                    ("FCFS_M0", ks_fcfs),
                    ("M2_LOAD_DEP", ks_m2),
                    ("PRIO_PREEMPT", ks_preempt),
                    ("FEEDBACK_PRIO", ks_feedback),
                ],
                key=lambda item: item[1],
            )[0]

            row = {
                "server": label,
                "rate_rps": rate,
                "rho_obs": round(rho_obs, 3),
                "S0_ms": round(s0_ms, 3),
                "beta": round(beta, 3),
                "cv": round(cv, 3),
                "ks_FCFS_M0": round(ks_fcfs, 4),
                "ks_M2_LOAD_DEP": round(ks_m2, 4),
                "ks_PRIO_PREEMPT": round(ks_preempt, 4),
                "ks_FEEDBACK_PRIO": round(ks_feedback, 4),
                "preempt_lambda1_ratio": lambda1_ratio,
                "preempt_s1_frac": s1_frac,
                "feedback_quantum_frac": quantum_frac,
                "delta_FCFS_minus_PREEMPT": round(ks_fcfs - ks_preempt, 4),
                "delta_FCFS_minus_FEEDBACK": round(ks_fcfs - ks_feedback, 4),
                "delta_M2_minus_PREEMPT": round(ks_m2 - ks_preempt, 4),
                "delta_M2_minus_FEEDBACK": round(ks_m2 - ks_feedback, 4),
                "best_model": best_model,
            }
            rows.append(row)
            plots.append((
                max(row["delta_M2_minus_PREEMPT"], row["delta_M2_minus_FEEDBACK"]),
                row,
                observed_sorted,
                fcfs_sorted,
                m2_sorted,
                preempt_sorted,
                feedback_sorted,
            ))

    if not rows:
        raise SystemExit("No exploratory priority rows generated.")

    os.makedirs(TABLE_DIR, exist_ok=True)
    out_csv = os.path.join(TABLE_DIR, "des_priority_exploratory_results.csv")
    df = pd.DataFrame(rows)
    df.to_csv(out_csv, index=False)
    print(f"Saved {out_csv}\n")

    print("Average KS:")
    print(df[["ks_FCFS_M0", "ks_M2_LOAD_DEP", "ks_PRIO_PREEMPT", "ks_FEEDBACK_PRIO"]].mean().round(4).to_string())
    print("\nBest model counts:")
    print(df.groupby("best_model").size().to_string())
    print("\nTop improvements over FCFS_M0:")
    print(df.sort_values(
        ["delta_FCFS_minus_PREEMPT", "delta_FCFS_minus_FEEDBACK"],
        ascending=False,
    )[[
        "server", "rate_rps", "rho_obs", "ks_FCFS_M0", "ks_PRIO_PREEMPT",
        "ks_M2_LOAD_DEP", "ks_FEEDBACK_PRIO", "preempt_lambda1_ratio",
        "preempt_s1_frac", "feedback_quantum_frac", "best_model",
    ]].head(12).to_string(index=False))

    plots.sort(key=lambda item: item[0], reverse=True)
    selected = plots[:6]
    ncols = 2
    nrows = math.ceil(len(selected) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4.2 * nrows))
    axes = np.array(axes).reshape(-1)
    fig.suptitle("Exploratory priority / feedback DES against measured response CDFs",
                 fontsize=13, fontweight="bold")
    for ax, (_, row, observed, fcfs, m2, preempt, feedback) in zip(axes, selected):
        ax.step(*cdf_xy(observed), where="post", lw=2.0, color="#1f77b4", label="CDF_REF")
        ax.step(*cdf_xy(fcfs), where="post", lw=1.4, color="#d62728",
                ls="--", label=f"FCFS_M0 KS={row['ks_FCFS_M0']:.3f}")
        ax.step(*cdf_xy(m2), where="post", lw=1.4, color="#ff7f0e",
                ls="-", label=f"M2_LOAD_DEP KS={row['ks_M2_LOAD_DEP']:.3f}")
        ax.step(*cdf_xy(preempt), where="post", lw=1.4, color="#2ca02c",
                ls="-.", label=f"PRIO_PREEMPT KS={row['ks_PRIO_PREEMPT']:.3f}")
        ax.step(*cdf_xy(feedback), where="post", lw=1.4, color="#984ea3",
                ls=":", label=f"FEEDBACK_PRIO KS={row['ks_FEEDBACK_PRIO']:.3f}")
        ax.set_title(
            f"{row['server']} @ {row['rate_rps']} rps, best={row['best_model']}",
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
    out_png = os.path.join(FIG_DIR, "des_priority_exploratory_cdf.png")
    plt.savefig(out_png, dpi=150)
    print(f"\nSaved {out_png}")


if __name__ == "__main__":
    main()
