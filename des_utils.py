"""
des_utils.py — Shared helpers for DES analysis and plotting scripts.

Imported by: run_des_all.py, plot_all_cdfs.py
"""
import csv
import math


def pct(arr, p):
    """Linear-interpolation percentile on a sorted list."""
    if not arr:
        return float('nan')
    idx = p / 100 * (len(arr) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return arr[lo] if lo == hi else arr[lo] * (1 - (idx - lo)) + arr[hi] * (idx - lo)


def ks_distance(obs, sim):
    """
    KS distance (supremum of CDF gap) between two sorted lists.
    Returns (d, best_x) where best_x is the x-value where the gap is largest.
    O(n log n) two-pointer implementation.
    """
    if not obs or not sim:
        return float('nan'), 0.0
    pts = sorted(set(obs) | set(sim))
    n, m = len(obs), len(sim)
    i = j = 0
    d, best_x = 0.0, 0.0
    for x in pts:
        while i < n and obs[i] <= x:
            i += 1
        while j < m and sim[j] <= x:
            j += 1
        gap = abs(i / n - j / m)
        if gap > d:
            d, best_x = gap, x
    return d, best_x


def make_cdf(sv):
    """Return (x, y) step-function arrays for a sorted list sv."""
    n = len(sv)
    if n == 0:
        return [], []
    x = [sv[0]] + sv
    y = [0.0] + [(i + 1) / n for i in range(n)]
    return x, y


def read_csv_col(path, col, status_col="status_code", ok_prefix="200"):
    """
    Read a single numeric column from a CSV, filtering to rows where
    status_col starts with ok_prefix. Returns a sorted list of floats.
    """
    vals = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sc = str(row.get(status_col, '')).strip()
                if sc.startswith(ok_prefix):
                    try:
                        v = float(row[col])
                        if math.isfinite(v):
                            vals.append(v)
                    except (ValueError, KeyError):
                        pass
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [WARN] could not read {path}: {e}")
    return sorted(vals)


def read_response_ms(path):
    """
    Read response times from a trace or DES-output CSV.
      - Observed trace:  response_ms / status_code
      - DES output:      sim_response_ms / sim_status
    Returns a sorted list of floats (200-status rows only).
    """
    vals = read_csv_col(path, 'response_ms', 'status_code', '200')
    if not vals:
        vals = read_csv_col(path, 'sim_response_ms', 'sim_status', '200')
    return vals
