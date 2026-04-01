"""
sqlite_load.py — Poisson load generator for the go-sqlite server.
POSTs sensor readings to POST /process and measures latency.

Usage:
  python sqlite_load.py --rate 25 --duration 90 --seed 42
"""
import argparse, concurrent.futures, math, random, threading, time
import requests


def pct(sv, p):
    if not sv: return float('nan')
    idx = p/100*(len(sv)-1); lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return sv[lo] if lo==hi else sv[lo]*(1-(idx-lo))+sv[hi]*(idx-lo)


def send(url, body, timeout, results, lock):
    t0 = time.perf_counter()
    ok, err = False, ''
    try:
        r = requests.post(url, json=body, timeout=timeout)
        ok = r.status_code == 200
        if not ok: err = f'http {r.status_code}'
    except Exception as e:
        err = str(e)
    with lock:
        results.append((ok, (time.perf_counter()-t0)*1000, err))


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--url',      default='http://localhost:8087/process')
    p.add_argument('--rate',     type=float, default=10.0)
    p.add_argument('--duration', type=float, default=90.0)
    p.add_argument('--timeout',  type=float, default=15.0)
    p.add_argument('--seed',     type=int,   default=42)
    return p.parse_args()


def main():
    args = parse_args()
    rng  = random.Random(args.seed)

    print(f'Warmup (5 s) -> {args.url}')
    t_w = time.perf_counter() + 5.0
    while time.perf_counter() < t_w:
        try: requests.post(args.url, json={'sensor_id':0,'value':0.0}, timeout=args.timeout)
        except: pass
        time.sleep(1/max(args.rate,1))

    print(f'Load test: rate={args.rate} rps  duration={args.duration} s')
    results, lock = [], threading.Lock()
    workers = max(50, min(int(args.rate*3), 2000))
    sent = 0
    t_start = time.perf_counter()
    t_end   = t_start + args.duration

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        while time.perf_counter() < t_end:
            body = {'sensor_id': rng.randint(1,100), 'value': rng.uniform(-100,100)}
            ex.submit(send, args.url, body, args.timeout, results, lock)
            sent += 1
            target = time.perf_counter() + rng.expovariate(args.rate)
            while time.perf_counter() < target: pass

    elapsed  = time.perf_counter() - t_start
    ok_times = sorted(r[1] for r in results if r[0])
    errs     = [r for r in results if not r[0]]
    print(f'sent={sent} ok={len(ok_times)} err={len(errs)} '
          f'achieved_rps={sent/elapsed:.1f}')
    if ok_times:
        print(f'client latency(ms): p50={pct(ok_times,50):.1f} '
              f'p95={pct(ok_times,95):.1f} p99={pct(ok_times,99):.1f}')
    if errs:
        print(f'errors (up to 5): {[e[2] for e in errs[:5]]}')


if __name__ == '__main__':
    main()
