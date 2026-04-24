"""
plot_all_cdfs.py — Generate observed-vs-DES response-time CDF plots for every
server experiment folder under data/experiments/.

Each folder gets a cdf.png showing one panel per rate point:
  - Observed trace (solid)
  - Replay DES (dashed red)
  - Bootstrap DES (dashed orange)
  - Parametric DES (dotted purple)
  KS distance annotated on each panel.

Usage:
  python analysis/reporting/plot_all_cdfs.py
  python analysis/reporting/plot_all_cdfs.py go_1c
"""
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "analysis", "des"))

from des_utils import ks_distance, make_cdf, read_csv_col

EXPERIMENTS = os.path.join(ROOT, "data", "experiments")

SIM_MODES = [
    ("replay",     "Replay",     "#e41a1c", "-",  2.0),
    ("bootstrap",  "Bootstrap",  "#ff7f00", "--", 1.4),
    ("parametric", "Parametric", "#984ea3", ":",  1.4),
]

OBS_COLOR = "#4c72b0"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_observed(path):
    return read_csv_col(path, "response_ms", "status_code", "200")


def load_simulated(path):
    return read_csv_col(path, "sim_response_ms", "sim_status", "200")


# ---------------------------------------------------------------------------
# Per-folder plot
# ---------------------------------------------------------------------------

def find_rates(folder, server_tag):
    """Return sorted list of rate integers that have a raw trace CSV."""
    rates = []
    for fn in os.listdir(folder):
        if fn.startswith(f"{server_tag}_") and fn.endswith("rps.csv") and "_des_" not in fn:
            try:
                rate = int(fn[len(server_tag) + 1: -len("rps.csv")])
                rates.append(rate)
            except ValueError:
                pass
    return sorted(rates)


def plot_folder(folder_path, server_tag):
    rates = find_rates(folder_path, server_tag)
    if not rates:
        print(f"  [{server_tag}] no trace CSVs found, skipping")
        return

    ncols = min(len(rates), 4)
    nrows = math.ceil(len(rates) / ncols)
    fig, axes = plt.subplots(nrows, ncols,
                              figsize=(5 * ncols, 4 * nrows),
                              squeeze=False)
    fig.suptitle(f"Response-time CDFs — {server_tag}", fontsize=13, fontweight="bold")

    for idx, rate in enumerate(rates):
        ax = axes[idx // ncols][idx % ncols]
        obs_path = os.path.join(folder_path, f"{server_tag}_{rate}rps.csv")
        obs = load_observed(obs_path)

        if not obs:
            ax.set_title(f"{rate} rps — no data")
            ax.axis("off")
            continue

        ox, oy = make_cdf(obs)
        ax.step(ox, oy, where="post", color=OBS_COLOR,
                linewidth=2.2, label=f"Observed n={len(obs):,}", zorder=5)

        for mode, label, color, ls, lw in SIM_MODES:
            sim_path = os.path.join(folder_path, f"{server_tag}_{rate}rps_des_{mode}.csv")
            sim = load_simulated(sim_path)
            if not sim:
                continue
            ks, ks_x = ks_distance(obs, sim)
            sx, sy = make_cdf(sim)
            ax.step(sx, sy, where="post", color=color, linestyle=ls,
                    linewidth=lw, label=f"{label} KS={ks:.3f}", zorder=4)

            # Annotate KS arrow on replay only to avoid clutter
            if mode == "replay" and math.isfinite(ks) and ks_x > 0:
                obs_f = sum(1 for v in obs if v <= ks_x) / len(obs)
                sim_f = sum(1 for v in sim if v <= ks_x) / len(sim)
                ax.annotate("", xy=(ks_x, sim_f), xytext=(ks_x, obs_f),
                             arrowprops=dict(arrowstyle="<->", color=color, lw=1.2))

        ax.set_title(f"{rate} rps", fontsize=11)
        ax.set_xlabel("Response time (ms)", fontsize=9)
        ax.set_ylabel("CDF", fontsize=9)
        ax.set_ylim(0, 1.05)
        ax.set_xlim(left=0)
        ax.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f"))
        ax.legend(fontsize=7.5, loc="lower right")
        ax.grid(True, alpha=0.3)

    for idx in range(len(rates), nrows * ncols):
        axes[idx // ncols][idx % ncols].axis("off")

    plt.tight_layout()
    out = os.path.join(folder_path, "cdf.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved: {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else None

    for entry in sorted(os.listdir(EXPERIMENTS)):
        if targets and entry not in targets:
            continue
        folder = os.path.join(EXPERIMENTS, entry)
        if not os.path.isdir(folder):
            continue
        # Infer the server tag from the actual CSV filenames in the folder
        # (folder may be go_1c but files are go_*.csv)
        server_tag = _infer_tag(folder, entry)
        print(f"Plotting {entry} (tag={server_tag}) ...")
        plot_folder(folder, server_tag)


def _infer_tag(folder, fallback):
    """Find the longest prefix shared by all trace CSVs in the folder."""
    traces = [f for f in os.listdir(folder)
              if f.endswith("rps.csv") and "_des_" not in f and "_summary" not in f]
    if not traces:
        return fallback
    # strip trailing _NNNrps.csv to get the tag
    tags = set()
    for fn in traces:
        try:
            rate_part = fn[fn.rfind("_") + 1:]  # e.g. "100rps.csv"
            int(rate_part.replace("rps.csv", ""))
            tag = fn[:fn.rfind("_")]             # e.g. "go" or "node_dsp"
            tags.add(tag)
        except ValueError:
            pass
    return tags.pop() if len(tags) == 1 else fallback


if __name__ == "__main__":
    main()
