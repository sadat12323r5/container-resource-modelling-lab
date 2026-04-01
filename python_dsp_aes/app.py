"""
python-dsp — End-to-end encrypted signal processing server.
Architecture: Gunicorn WSGI with --workers 1 (single process, single worker).
This is a true M/G/1 server: one request executes at a time, others wait in the
OS-level socket accept queue.

Pipeline: AES-256-CBC decrypt → FIR low-pass filter → AES-256-CBC encrypt
Protocol: POST /process {iv, payload} → {ok, iv, payload, samples, service_ms}
"""
import base64
import fcntl
import json
import math
import os
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as crypto_padding
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AES_KEY     = bytes.fromhex(os.environ.get('DSP_AES_KEY',
              '0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20'))
SIG_LEN     = int(os.environ.get('DSP_SIGNAL_LENGTH', '1024'))
FILTER_TAPS = int(os.environ.get('DSP_FILTER_TAPS',   '64'))
CUTOFF      = float(os.environ.get('DSP_CUTOFF_FREQ',  '0.25'))
CSV_FILE    = os.environ.get('DSP_TRACE_CSV', '')

# ---------------------------------------------------------------------------
# FIR kernel — built once at import time, reused every request
# ---------------------------------------------------------------------------
def _build_hamming_fir(taps: int, cutoff: float) -> list[float]:
    half = taps // 2
    kernel = []
    for i in range(taps):
        n    = i - half
        sinc = (2.0 * cutoff) if n == 0 else (math.sin(2*math.pi*cutoff*n) / (math.pi*n))
        win  = 0.54 - 0.46 * math.cos(2 * math.pi * i / max(1, taps - 1))
        kernel.append(sinc * win)
    return kernel

KERNEL: list[float] = _build_hamming_fir(FILTER_TAPS, CUTOFF)

# ---------------------------------------------------------------------------
# DSP helpers
# ---------------------------------------------------------------------------
def fir_convolve(signal: list[float], kernel: list[float]) -> list[float]:
    N, M = len(signal), len(kernel)
    out = []
    for i in range(N):
        acc = 0.0
        for j in range(min(M, i + 1)):
            acc += signal[i - j] * kernel[j]
        out.append(acc)
    return out

def aes_decrypt(iv: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv))
    dec    = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpad  = crypto_padding.PKCS7(128).unpadder()
    return unpad.update(padded) + unpad.finalize()

def aes_encrypt(plaintext: bytes) -> tuple[bytes, bytes]:
    import os as _os
    iv     = _os.urandom(16)
    padder = crypto_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv))
    enc    = cipher.encryptor()
    return iv, enc.update(padded) + enc.finalize()

# ---------------------------------------------------------------------------
# CSV logging (single worker → no concurrent writes, lock is just safety)
# ---------------------------------------------------------------------------
_csv_header_written = False

def log_csv(arrival_ns: int, service_ms: float, queue_ms: float,
            response_ms: float, status: int) -> None:
    global _csv_header_written
    if not CSV_FILE:
        return
    try:
        d = os.path.dirname(CSV_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(CSV_FILE, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            if not _csv_header_written and f.tell() == 0:
                f.write('arrival_unix_ns,service_ms,queue_ms,response_ms,status_code\n')
            _csv_header_written = True
            f.write(f'{arrival_ns},{service_ms:.6f},{queue_ms:.6f},{response_ms:.6f},{status}\n')
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get('/health')
def health():
    return jsonify({'ok': True})

@app.get('/')
def info():
    return jsonify({
        'service':      'python-dsp',
        'runtime':      'Python + Gunicorn (--workers 1)',
        'pipeline':     'AES-256-CBC decrypt → FIR low-pass → AES-256-CBC encrypt',
        'signal_length': SIG_LEN,
        'filter_taps':  FILTER_TAPS,
        'cutoff':       CUTOFF,
    })

@app.post('/process')
def process():
    arrival_ns = time.time_ns()
    req_start  = time.perf_counter()

    body = request.get_json(silent=True)
    if not body or 'iv' not in body or 'payload' not in body:
        return jsonify({'error': 'missing iv or payload'}), 400

    # ---- SERVICE WORK ----
    svc_start = time.perf_counter()

    try:
        iv_bytes = base64.b64decode(body['iv'])
        ct_bytes = base64.b64decode(body['payload'])
        raw      = aes_decrypt(iv_bytes, ct_bytes)
    except Exception:
        svc_ms  = (time.perf_counter() - svc_start)  * 1000
        resp_ms = (time.perf_counter() - req_start)   * 1000
        log_csv(arrival_ns, svc_ms, max(0, resp_ms - svc_ms), resp_ms, 422)
        return jsonify({'error': 'decrypt_failed'}), 422

    # Unpack IEEE-754 float32 little-endian
    n_samples = len(raw) // 4
    signal    = list(struct.unpack(f'{n_samples}f', raw[:n_samples * 4]))

    # FIR convolution (blocks this worker — M/G/1 behaviour)
    filtered = fir_convolve(signal, KERNEL)

    # Pack back to float32
    packed = struct.pack(f'{n_samples}f', *filtered)

    # Re-encrypt
    resp_iv, ciphertext = aes_encrypt(packed)

    svc_end  = time.perf_counter()
    svc_ms   = (svc_end - svc_start) * 1000
    resp_ms  = (svc_end - req_start)  * 1000
    queue_ms = max(0.0, resp_ms - svc_ms)
    # ---- END SERVICE WORK ----

    log_csv(arrival_ns, svc_ms, queue_ms, resp_ms, 200)

    resp = jsonify({
        'ok':         True,
        'iv':         base64.b64encode(resp_iv).decode(),
        'payload':    base64.b64encode(ciphertext).decode(),
        'samples':    n_samples,
        'service_ms': round(svc_ms, 3),
    })
    resp.headers['X-Service-Actual-Ms'] = f'{svc_ms:.3f}'
    return resp


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
