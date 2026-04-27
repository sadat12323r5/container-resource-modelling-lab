#!/usr/bin/env python3
"""
ANOVA tests for whether service-time distributions depend on arrival rate.

For each experiment folder, service_ms samples are grouped by arrival rate.
We test:

  H0 / M0: service-time distribution is independent of arrival rate.
  H1:      service-time distribution depends on arrival rate.

Because many service-time samples are approximately log-normal, the main ANOVA
is run on log(service_ms). Raw-service ANOVA and Kruskal-Wallis are also reported
as robustness checks.

Output:
  results/tables/service_time_anova.csv
"""
import csv
import math
import os
import re

import numpy as np
import pandas as pd
from scipy import stats


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXP_BASE = os.path.join(ROOT, "data", "experiments")
TABLE_DIR = os.path.join(ROOT, "results", "tables")


TRACE_RE = re.compile(r"(.+?)_(\d+)rps\.csv$")


def read_services(path):
    services = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                status = row.get("status_code", "200").strip()
                if not status.startswith("2"):
                    continue
                service = float(row["service_ms"])
            except (KeyError, ValueError):
                continue
            if math.isfinite(service) and service > 0:
                services.append(service)
    return np.array(services, dtype=float)


def eta_squared(groups):
    values = np.concatenate(groups)
    grand_mean = float(values.mean())
    ss_between = sum(len(g) * (float(g.mean()) - grand_mean) ** 2 for g in groups)
    ss_total = float(np.sum((values - grand_mean) ** 2))
    return ss_between / ss_total if ss_total > 0 else float("nan")


def summarize_folder(folder):
    folder_path = os.path.join(EXP_BASE, folder)
    groups = []
    rate_rows = []

    for name in sorted(os.listdir(folder_path)):
        if "_des_" in name or not name.endswith("rps.csv"):
            continue
        match = TRACE_RE.match(name)
        if not match:
            continue
        rate = int(match.group(2))
        services = read_services(os.path.join(folder_path, name))
        if len(services) < 30:
            continue
        groups.append((rate, services))
        rate_rows.append(
            f"{rate}:{len(services)}:{services.mean():.3f}:{np.median(services):.3f}"
        )

    if len(groups) < 2:
        return None

    groups.sort(key=lambda item: item[0])
    rates = [rate for rate, _ in groups]
    raw_groups = [services for _, services in groups]
    log_groups = [np.log(services) for _, services in groups]

    raw_f, raw_p = stats.f_oneway(*raw_groups)
    log_f, log_p = stats.f_oneway(*log_groups)
    lev_f, lev_p = stats.levene(*log_groups, center="median")
    kw_h, kw_p = stats.kruskal(*raw_groups)

    first_mean = float(raw_groups[0].mean())
    last_mean = float(raw_groups[-1].mean())
    mean_ratio = last_mean / first_mean if first_mean > 0 else float("nan")

    return {
        "folder": folder,
        "n_rates": len(groups),
        "rates": ",".join(str(r) for r in rates),
        "n_total": int(sum(len(g) for g in raw_groups)),
        "mean_first_ms": round(first_mean, 4),
        "mean_last_ms": round(last_mean, 4),
        "mean_last_over_first": round(mean_ratio, 4),
        "anova_raw_F": round(float(raw_f), 4),
        "anova_raw_p": float(raw_p),
        "anova_log_F": round(float(log_f), 4),
        "anova_log_p": float(log_p),
        "eta2_log": round(float(eta_squared(log_groups)), 4),
        "levene_log_p": float(lev_p),
        "kruskal_H": round(float(kw_h), 4),
        "kruskal_p": float(kw_p),
        "reject_M0_log_anova_0.05": bool(log_p < 0.05),
        "rate_n_mean_median": ";".join(rate_rows),
    }


def main():
    rows = []
    for entry in sorted(os.listdir(EXP_BASE)):
        path = os.path.join(EXP_BASE, entry)
        if not os.path.isdir(path):
            continue
        row = summarize_folder(entry)
        if row:
            rows.append(row)

    if not rows:
        raise SystemExit("No ANOVA rows generated.")

    os.makedirs(TABLE_DIR, exist_ok=True)
    out_csv = os.path.join(TABLE_DIR, "service_time_anova.csv")
    df = pd.DataFrame(rows)
    df.to_csv(out_csv, index=False)

    print(f"Saved {out_csv}")
    print(
        df[
            [
                "folder",
                "n_rates",
                "mean_last_over_first",
                "anova_log_F",
                "anova_log_p",
                "eta2_log",
                "reject_M0_log_anova_0.05",
            ]
        ].sort_values("anova_log_p").to_string(index=False)
    )


if __name__ == "__main__":
    main()
