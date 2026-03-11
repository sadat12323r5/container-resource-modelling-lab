import argparse
import concurrent.futures
import random
import threading
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open-loop Poisson load generator (exponential inter-arrivals)."
    )
    parser.add_argument(
        "--url",
        default="http://host.docker.internal:8080/",
        help="Target URL (default: http://host.docker.internal:8080/)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=200.0,
        help="Mean arrival rate (requests per second)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Duration in seconds",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Thread pool size (default: auto from rate)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed (default: time-based)",
    )
    parser.add_argument(
        "--id-header",
        default="X-Request-Id",
        help="Header name to carry request ID (default: X-Request-Id)",
    )
    return parser.parse_args()


def do_request(url: str, timeout: float, id_header: str, seq: int) -> tuple[bool, float]:
    req = urllib.request.Request(url, headers={id_header: str(seq)})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read()
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        ok = False
    except Exception:
        ok = False
    duration = time.perf_counter() - start
    return ok, duration


def main() -> int:
    args = parse_args()
    if args.rate <= 0:
        raise SystemExit("rate must be > 0")
    if args.duration <= 0:
        raise SystemExit("duration must be > 0")

    if args.seed:
        random.seed(args.seed)
    else:
        random.seed()

    if args.workers and args.workers > 0:
        workers = args.workers
    else:
        workers = int(max(50.0, min(args.rate * 2.0, 2000.0)))

    stats_lock = threading.Lock()
    stats = {
        "sent": 0,
        "completed": 0,
        "ok": 0,
        "err": 0,
        "lat_sum": 0.0,
    }

    def on_done(fut: concurrent.futures.Future):
        try:
            ok, duration = fut.result()
        except Exception:
            ok = False
            duration = 0.0
        with stats_lock:
            stats["completed"] += 1
            if ok:
                stats["ok"] += 1
            else:
                stats["err"] += 1
            stats["lat_sum"] += duration

    start = time.perf_counter()
    end = start + args.duration
    next_time = start

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        seq = 0
        while True:
            now = time.perf_counter()
            if now >= end:
                break

            seq += 1
            fut = executor.submit(
                do_request, args.url, args.timeout, args.id_header, seq
            )
            fut.add_done_callback(on_done)
            with stats_lock:
                stats["sent"] += 1

            inter = random.expovariate(args.rate)
            next_time += inter
            sleep_for = next_time - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

    elapsed = time.perf_counter() - start
    with stats_lock:
        sent = stats["sent"]
        completed = stats["completed"]
        ok = stats["ok"]
        err = stats["err"]
        lat_avg = (stats["lat_sum"] / completed) if completed else 0.0

    print(f"sent={sent} completed={completed} ok={ok} err={err}")
    print(f"elapsed_s={elapsed:.3f} achieved_rate={sent/elapsed:.2f} rps")
    print(f"avg_client_latency_s={lat_avg:.4f}")
    print(f"workers={workers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
