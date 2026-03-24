"""
Poisson load generator for Apache endpoints.
Supports GET /messages and POST /send with a configurable mix.
"""
import argparse
import concurrent.futures
import json
import random
import threading
import time
import urllib.error
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser(
        description="Poisson load generator for Apache (GET /messages + POST /send)."
    )
    parser.add_argument("--base-url", default="http://localhost:8082",
                        help="Base URL (default: http://localhost:8082)")
    parser.add_argument("--rate", type=float, default=100.0,
                        help="Mean arrival rate rps (default: 100)")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="Duration in seconds (default: 60)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Per-request timeout in seconds (default: 5)")
    parser.add_argument("--post-ratio", type=float, default=0.3,
                        help="Fraction of requests that are POST /send (default: 0.3)")
    parser.add_argument("--room", default="general",
                        help="Chat room name (default: general)")
    parser.add_argument("--workers", type=int, default=0,
                        help="Thread pool size (0 = auto)")
    parser.add_argument("--seed", type=int, default=0,
                        help="RNG seed (0 = time-based)")
    return parser.parse_args()


def do_get(url, timeout):
    req = urllib.request.Request(url)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            ok = 200 <= r.status < 300
            svc_target = r.headers.get("X-Service-Target-Us", "")
            svc_actual = r.headers.get("X-Service-Actual-Ms", "")
    except Exception:
        ok = False
        svc_target = svc_actual = ""
    latency = time.perf_counter() - t0
    return ok, latency, svc_target, svc_actual


def do_post(url, body_bytes, timeout):
    req = urllib.request.Request(url, data=body_bytes,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            ok = 200 <= r.status < 300
            svc_target = r.headers.get("X-Service-Target-Us", "")
            svc_actual = r.headers.get("X-Service-Actual-Ms", "")
    except Exception:
        ok = False
        svc_target = svc_actual = ""
    latency = time.perf_counter() - t0
    return ok, latency, svc_target, svc_actual


def main():
    args = parse_args()
    rng = random.Random(args.seed if args.seed else None)

    workers = args.workers if args.workers > 0 else int(max(50, min(args.rate * 2, 2000)))

    get_url = f"{args.base_url}/messages?room={args.room}"
    post_url = f"{args.base_url}/send"

    lock = threading.Lock()
    latencies = {"GET": [], "POST": []}
    counts = {"sent": 0, "ok": 0, "err": 0}

    def on_done(fut, method):
        try:
            ok, lat, _, _ = fut.result()
        except Exception:
            ok, lat = False, 0.0
        with lock:
            counts["ok" if ok else "err"] += 1
            latencies[method].append(lat)

    start = time.perf_counter()
    end = start + args.duration
    next_time = start
    seq = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            if time.perf_counter() >= end:
                break
            seq += 1
            if rng.random() < args.post_ratio:
                body = json.dumps({
                    "user": f"u{seq % 20}",
                    "room": args.room,
                    "text": f"msg {seq}"
                }).encode()
                fut = pool.submit(do_post, post_url, body, args.timeout)
                method = "POST"
            else:
                fut = pool.submit(do_get, get_url, args.timeout)
                method = "GET"

            fut.add_done_callback(lambda f, m=method: on_done(f, m))
            with lock:
                counts["sent"] += 1

            inter = rng.expovariate(args.rate)
            next_time += inter
            sleep_for = next_time - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

    elapsed = time.perf_counter() - start

    def pct(vals, p):
        if not vals:
            return float("nan")
        s = sorted(vals)
        idx = int(p / 100 * (len(s) - 1))
        return s[idx]

    with lock:
        all_lats = latencies["GET"] + latencies["POST"]
        get_lats = latencies["GET"]
        post_lats = latencies["POST"]
        sent = counts["sent"]
        ok = counts["ok"]
        err = counts["err"]

    print(f"rate={args.rate} duration={args.duration}s elapsed={elapsed:.2f}s")
    print(f"sent={sent} ok={ok} err={err} achieved_rps={sent/elapsed:.1f}")
    print(f"ALL   p50={pct(all_lats,50)*1000:.1f}ms p95={pct(all_lats,95)*1000:.1f}ms p99={pct(all_lats,99)*1000:.1f}ms")
    print(f"GET   p50={pct(get_lats,50)*1000:.1f}ms p95={pct(get_lats,95)*1000:.1f}ms p99={pct(get_lats,99)*1000:.1f}ms n={len(get_lats)}")
    print(f"POST  p50={pct(post_lats,50)*1000:.1f}ms p95={pct(post_lats,95)*1000:.1f}ms p99={pct(post_lats,99)*1000:.1f}ms n={len(post_lats)}")


if __name__ == "__main__":
    main()
