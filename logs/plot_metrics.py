import argparse
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot histogram and CDF from logs/requests.csv"
    )
    parser.add_argument(
        "--metric",
        default="response_ms",
        help="Metric column to plot (default: response_ms)",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=100,
        help="Histogram bins (default: 100)",
    )
    parser.add_argument(
        "--logy",
        action="store_true",
        help="Use log scale for histogram y-axis",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Plot queue_ms, service_ms, and response_ms",
    )
    return parser.parse_args()


def require_deps():
    try:
        import pandas as pd
        import matplotlib.pyplot as plt
    except ImportError as exc:
        print(
            "Missing dependency. Please install pandas and matplotlib:\n"
            "  python -m pip install pandas matplotlib",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    return pd, plt


def plot_metric(df, metric, out_dir, bins, logy, plt):
    if metric not in df.columns:
        print(f"Skipping {metric}: column not found.")
        return
    series = df[metric].dropna()
    if series.empty:
        print(f"Skipping {metric}: no data.")
        return

    # Histogram
    plt.figure(figsize=(7, 4))
    plt.hist(series, bins=bins, edgecolor="black")
    if logy:
        plt.yscale("log")
    plt.title(f"Histogram of {metric}")
    plt.xlabel("ms")
    plt.ylabel("count")
    plt.tight_layout()
    hist_path = out_dir / f"{metric}_hist.png"
    plt.savefig(hist_path, dpi=150)
    plt.close()

    # CDF
    sorted_vals = series.sort_values()
    cdf = (range(1, len(sorted_vals) + 1))
    cdf = [v / len(sorted_vals) for v in cdf]
    plt.figure(figsize=(7, 4))
    plt.plot(sorted_vals, cdf)
    plt.title(f"CDF of {metric}")
    plt.xlabel("ms")
    plt.ylabel("CDF")
    plt.tight_layout()
    cdf_path = out_dir / f"{metric}_cdf.png"
    plt.savefig(cdf_path, dpi=150)
    plt.close()

    # Percentiles
    p = series.quantile([0.5, 0.9, 0.95, 0.99, 0.999])
    print(
        f"{metric}: p50={p[0.5]:.3f} ms, p90={p[0.9]:.3f} ms, "
        f"p95={p[0.95]:.3f} ms, p99={p[0.99]:.3f} ms, p99.9={p[0.999]:.3f} ms"
    )
    print(f"Saved: {hist_path.name}, {cdf_path.name}")


def main() -> int:
    args = parse_args()
    pd, plt = require_deps()

    script_dir = Path(__file__).resolve().parent
    csv_path = script_dir / "requests.csv"
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(csv_path)
    out_dir = script_dir

    if args.all:
        for metric in ("queue_ms", "service_ms", "response_ms"):
            plot_metric(df, metric, out_dir, args.bins, args.logy, plt)
    else:
        plot_metric(df, args.metric, out_dir, args.bins, args.logy, plt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
