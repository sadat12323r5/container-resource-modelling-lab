"""
plot_cdf.py — Plot observed vs DES-simulated response-time CDFs.

Usage:
  python plot_cdf.py                        # all servers, default rates
  python plot_cdf.py --server go            # Go only
  python plot_cdf.py --server dsp_aes       # Apache DSP-AES only
  python plot_cdf.py --server apache        # Apache messaging only
  python plot_cdf.py --server go dsp_aes    # multiple servers
  python plot_cdf.py --out cdf_plots.png    # custom output path
"""

import argparse
import csv
import math
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

EXP_DIR = Path("logs and des/experiments")

SERVERS = {
    "go": {
        "label": "Go server",
        "rates": [50, 100, 200, 400],
        "prefix": "go_{rate}rps",
        "obs_col": "response_ms",
        "color": "#4c72b0",
    },
    "apache": {
        "label": "Apache messaging",
        "rates": [10, 25, 50],
        "prefix": "apache_{rate}rps",
        "obs_col": "response_ms",
        "color": "#dd8452",
    },
    "dsp_aes": {
        "label": "Apache DSP-AES",
        "rates": [10, 25, 50, 75, 100],
        "prefix": "dsp_aes_{rate}rps",
        "obs_col": "response_ms",
        "color": "#55a868",
    },
}

SIM_MODES = [
    ("replay",     "Replay",     "#e41a1c", "-"),
    ("bootstrap",  "Bootstrap",  "#ff7f00", "--"),
    ("parametric", "Lognormal",  "#984ea3", ":"),
]


def load_observed(path, col="response_ms"):
    vals = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sc = str(row.get("status_code", "")).strip()
            if sc.startswith("2"):
                try:
                    vals.append(float(row[col]))
                except (ValueError, KeyError):
                    pass
    return sorted(vals)


def load_simulated(path, col="sim_response_ms"):
    vals = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sc = str(row.get("sim_status", "")).strip()
            if sc == "200":
                try:
                    v = float(row[col])
                    if math.isfinite(v):
                        vals.append(v)
                except (ValueError, KeyError):
                    pass
    return sorted(vals)


def make_cdf(sorted_vals):
    n = len(sorted_vals)
    x = [sorted_vals[0]] + sorted_vals
    y = [0.0] + [(i + 1) / n for i in range(n)]
    return x, y


def ks_distance(obs, sim):
    obs_s, sim_s = sorted(obs), sorted(sim)
    points = sorted(set(obs_s + sim_s))
    i = j = 0
    n, m = len(obs_s), len(sim_s)
    d = best_x = 0.0
    for x in points:
        while i < n and obs_s[i] <= x:
            i += 1
        while j < m and sim_s[j] <= x:
            j += 1
        gap = abs(i / n - j / m)
        if gap > d:
            d = gap
            best_x = x
    return d, best_x


def plot_server(server_key, cfg, out_path):
    rates = cfg["rates"]
    ncols = min(len(rates), 3)
    nrows = math.ceil(len(rates) / ncols)
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(5 * ncols, 4 * nrows),
                             squeeze=False)
    fig.suptitle(f"Response-time CDFs — {cfg['label']}", fontsize=13, fontweight="bold")

    for idx, rate in enumerate(rates):
        ax = axes[idx // ncols][idx % ncols]
        prefix = cfg["prefix"].format(rate=rate)
        obs_path = EXP_DIR / f"{prefix}.csv"

        if not obs_path.exists():
            ax.set_title(f"{rate} rps — no data")
            ax.axis("off")
            continue

        obs = load_observed(obs_path, cfg["obs_col"])
        if not obs:
            ax.set_title(f"{rate} rps — empty")
            ax.axis("off")
            continue

        ox, oy = make_cdf(obs)
        ax.step(ox, oy, where="post", color=cfg["color"],
                linewidth=2, label=f"Observed (n={len(obs)})", zorder=5)

        for mode, mode_label, color, ls in SIM_MODES:
            sim_path = EXP_DIR / f"{prefix}_des_{mode}.csv"
            if not sim_path.exists():
                continue
            sim = load_simulated(sim_path)
            if not sim:
                continue
            ks, ks_x = ks_distance(obs, sim)
            sx, sy = make_cdf(sim)
            ax.step(sx, sy, where="post", color=color, linestyle=ls,
                    linewidth=1.4, label=f"{mode_label} KS={ks:.3f}", zorder=4)

            # Draw KS arrow at worst-gap x
            obs_cdf_at_x = sum(1 for v in obs if v <= ks_x) / len(obs)
            sim_cdf_at_x = sum(1 for v in sim if v <= ks_x) / len(sim)
            if mode == "replay":  # only annotate replay to avoid clutter
                ax.annotate(
                    "", xy=(ks_x, sim_cdf_at_x), xytext=(ks_x, obs_cdf_at_x),
                    arrowprops=dict(arrowstyle="<->", color=color, lw=1.2),
                )

        ax.set_title(f"{rate} rps", fontsize=11)
        ax.set_xlabel("Response time (ms)", fontsize=9)
        ax.set_ylabel("CDF", fontsize=9)
        ax.set_ylim(0, 1.05)
        ax.set_xlim(left=0)
        ax.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f"))
        ax.legend(fontsize=7.5, loc="lower right")
        ax.grid(True, alpha=0.3)

    # Hide unused subplots
    for idx in range(len(rates), nrows * ncols):
        axes[idx // ncols][idx % ncols].axis("off")

    plt.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"saved: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", nargs="+",
                        choices=list(SERVERS.keys()),
                        default=list(SERVERS.keys()),
                        help="Which server(s) to plot (default: all)")
    parser.add_argument("--out", default=None,
                        help="Output PNG path (default: cdf_<server>.png per server)")
    args = parser.parse_args()

    for key in args.server:
        cfg = SERVERS[key]
        out = Path(args.out) if args.out and len(args.server) == 1 else Path(f"cdf_{key}.png")
        plot_server(key, cfg, out)


if __name__ == "__main__":
    main()
