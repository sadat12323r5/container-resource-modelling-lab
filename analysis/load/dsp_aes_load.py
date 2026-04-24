"""
dsp_aes_load.py — Poisson load generator for the secure DSP server.

Simulates a client that:
  1. Holds a pool of pre-generated float32 signal packets (e.g. audio frames).
  2. AES-256-CBC encrypts each packet with a fresh random IV before sending.
  3. POSTs the encrypted payload to POST /process.
  4. Optionally decrypts + verifies the filtered response.

This reflects a realistic scenario: a client streaming audio or sensor data to a
remote noise-reduction / signal-processing service, with end-to-end encryption for
content privacy.

Usage:
  python dsp_aes_load.py --rate 25 --duration 90 --seed 42
  python dsp_aes_load.py --rate 50 --duration 90 --aes-key 0102...1f20 --verify

Dependencies:
  pip install requests cryptography
"""

import argparse
import base64
import json
import math
import os
import random
import struct
import threading
import time

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as crypto_padding


# ---------------------------------------------------------------------------
# AES helpers
# ---------------------------------------------------------------------------

def aes_encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """AES-256-CBC encrypt. Returns (iv, ciphertext) with PKCS7 padding."""
    iv = os.urandom(16)
    padder = crypto_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return iv, enc.update(padded) + enc.finalize()


def aes_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-256-CBC decrypt + remove PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = crypto_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ---------------------------------------------------------------------------
# Signal pool — pre-generated at startup, reused across requests
# ---------------------------------------------------------------------------

def build_signal_pool(n_signals: int, signal_length: int, seed: int,
                      aes_key: bytes) -> list[dict]:
    """
    Generate `n_signals` random float32 arrays (simulating audio frames),
    AES encrypt each with a fresh IV, and return a list of ready-to-send
    request bodies.

    Using a fixed seed makes the pool reproducible across runs (supervisor
    reproducibility requirement). Each packet still gets a unique random IV
    (from os.urandom) so every request is cryptographically distinct.
    """
    rng = random.Random(seed)
    pool = []
    for _ in range(n_signals):
        # Simulate one frame of audio: uniform float32 samples in [-1, 1].
        # At 44.1 kHz, signal_length=1024 => ~23 ms of audio per packet.
        samples = [rng.uniform(-1.0, 1.0) for _ in range(signal_length)]
        raw = struct.pack(f'{signal_length}f', *samples)
        iv, ciphertext = aes_encrypt(aes_key, raw)
        pool.append({
            'iv':      base64.b64encode(iv).decode(),
            'payload': base64.b64encode(ciphertext).decode(),
        })
    return pool


# ---------------------------------------------------------------------------
# Per-request worker
# ---------------------------------------------------------------------------

def send_request(url: str, body: dict, aes_key: bytes, verify: bool,
                 timeout: float, results: list, lock: threading.Lock) -> None:
    t0 = time.perf_counter()
    ok = False
    err_msg = ''
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            ok = True
            if verify:
                data = resp.json()
                resp_iv = base64.b64decode(data['iv'])
                resp_ct = base64.b64decode(data['payload'])
                raw_out = aes_decrypt(aes_key, resp_iv, resp_ct)
                n_out = len(raw_out) // 4
                expected = data.get('samples', -1)
                if n_out != expected:
                    ok = False
                    err_msg = f'sample count mismatch: got {n_out}, expected {expected}'
        else:
            elapsed = time.perf_counter() - t0
            err_msg = f'http {resp.status_code}'
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        err_msg = str(exc)

    with lock:
        results.append((ok, elapsed, err_msg))


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------

def pct(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return float('nan')
    idx = p / 100.0 * (len(sorted_vals) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (1.0 - (idx - lo)) + sorted_vals[hi] * (idx - lo)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Poisson load generator for the secure DSP signal-processing server.'
    )
    parser.add_argument('--url', default='http://localhost:8083/index.php',
                        help='Base URL of the DSP server (default: http://localhost:8083/index.php)')
    parser.add_argument('--rate', type=float, default=10.0,
                        help='Target request rate in rps (default: 10)')
    parser.add_argument('--duration', type=float, default=90.0,
                        help='Test duration in seconds (default: 90)')
    parser.add_argument('--timeout', type=float, default=10.0,
                        help='Per-request timeout in seconds (default: 10)')
    parser.add_argument('--seed', type=int, default=42,
                        help='RNG seed for signal pool + arrival jitter (default: 42)')
    parser.add_argument('--aes-key',
                        default='0102030405060708090a0b0c0d0e0f10'
                                '1112131415161718191a1b1c1d1e1f20',
                        help='AES-256 key as 64 hex chars (must match DSP_AES_KEY in docker-compose)')
    parser.add_argument('--signal-length', type=int, default=1024,
                        help='Number of float32 samples per packet (default: 1024)')
    parser.add_argument('--pool-size', type=int, default=200,
                        help='Number of pre-generated signal packets in pool (default: 200)')
    parser.add_argument('--verify', action='store_true',
                        help='Decrypt and verify each response (adds client-side overhead)')
    return parser.parse_args()


def main():
    args = parse_args()

    aes_key = bytes.fromhex(args.aes_key)
    if len(aes_key) != 32:
        raise SystemExit(f'--aes-key must be exactly 64 hex chars (got {len(aes_key)*2})')

    process_url = args.url.rstrip('/') + '?route=/process'

    print(f'Building signal pool: {args.pool_size} packets x {args.signal_length} samples '
          f'(seed={args.seed}) ...')
    pool = build_signal_pool(args.pool_size, args.signal_length, args.seed, aes_key)
    payload_bytes = len(base64.b64decode(pool[0]['payload']))
    print(f'Pool ready. Ciphertext: {payload_bytes} B/packet, '
          f'JSON body: ~{len(json.dumps(pool[0]))} B/request')

    rng = random.Random(args.seed + 1)

    # Warmup
    print(f'Warmup (5 s) -> {process_url}')
    t_warmup_end = time.perf_counter() + 5.0
    while time.perf_counter() < t_warmup_end:
        body = pool[rng.randrange(len(pool))]
        try:
            requests.post(process_url, json=body, timeout=args.timeout)
        except Exception:
            pass
        time.sleep(1.0 / max(args.rate, 1.0))

    print(f'\nLoad test: rate={args.rate} rps, duration={args.duration} s, '
          f'verify={args.verify}')

    results = []
    lock    = threading.Lock()
    workers = max(50, min(int(args.rate * 3), 2000))
    pool_idx = 0

    t_start = time.perf_counter()
    t_end   = t_start + args.duration
    sent    = 0

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            now = time.perf_counter()
            if now >= t_end:
                break

            body = pool[pool_idx % len(pool)]
            pool_idx += 1
            executor.submit(send_request, process_url, body, aes_key,
                            args.verify, args.timeout, results, lock)
            sent += 1

            # Poisson inter-arrival: exponential with mean 1/rate
            sleep_s = rng.expovariate(args.rate)
            target  = now + sleep_s
            while time.perf_counter() < target:
                pass  # busy-wait for precise inter-arrival timing

    elapsed  = time.perf_counter() - t_start
    ok_times = sorted(r[1] * 1000 for r in results if r[0])
    err_list = [r for r in results if not r[0]]
    achieved = sent / elapsed

    print(f'\n--- Results ---')
    print(f'sent={sent}  ok={len(ok_times)}  err={len(err_list)}  '
          f'achieved_rps={achieved:.1f}  elapsed={elapsed:.1f}s')
    if ok_times:
        print(f'client latency (ms):  '
              f'p50={pct(ok_times, 50):.1f}  '
              f'p95={pct(ok_times, 95):.1f}  '
              f'p99={pct(ok_times, 99):.1f}  '
              f'max={ok_times[-1]:.1f}')
    if err_list:
        print(f'error sample (up to 5): {[e[2] for e in err_list[:5]]}')


if __name__ == '__main__':
    main()
