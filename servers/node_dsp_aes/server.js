'use strict';
/**
 * node-dsp — End-to-end encrypted signal processing server.
 *
 * Single-worker mode (WORKERS=1, default):
 *   Architecture: single-threaded Node.js event loop (no worker threads).
 *   CPU-bound FIR convolution blocks the loop → effective M/G/1.
 *
 * Multi-worker mode (WORKERS=N, N>1):
 *   Uses Node.js cluster module: N independent processes, each a copy of this
 *   event loop. OS round-robin distributes connections → M/G/N behaviour.
 *
 * Pipeline: AES-256-CBC decrypt → FIR low-pass filter → AES-256-CBC encrypt
 * Protocol: POST /process {iv, payload} → {ok, iv, payload, samples, service_ms}
 */
const cluster = require('cluster');
const http    = require('http');
const crypto  = require('crypto');
const fs      = require('fs');
const path    = require('path');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const PORT        = parseInt(process.env.PORT                || '3000');
const WORKERS     = parseInt(process.env.WORKERS             || '1');
const AES_KEY     = Buffer.from(process.env.DSP_AES_KEY      ||
                    '0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20', 'hex');
const SIG_LEN     = parseInt(process.env.DSP_SIGNAL_LENGTH   || '1024');
const FILTER_TAPS = parseInt(process.env.DSP_FILTER_TAPS     || '64');
const CUTOFF      = parseFloat(process.env.DSP_CUTOFF_FREQ   || '0.25');
const CSV_FILE    = process.env.DSP_TRACE_CSV                || '';

// ---------------------------------------------------------------------------
// Cluster primary — just forks workers and restarts on crash
// ---------------------------------------------------------------------------
if (WORKERS > 1 && cluster.isPrimary) {
    console.log(`node-dsp primary pid=${process.pid}  forking ${WORKERS} workers`);
    for (let i = 0; i < WORKERS; i++) cluster.fork();
    cluster.on('exit', (worker, code) => {
        console.log(`worker ${worker.process.pid} exited (${code}), restarting`);
        cluster.fork();
    });
} else {
    startWorker();
}

function startWorker() {
    // ---------------------------------------------------------------------------
    // Nanosecond epoch clock (anchored at startup)
    // ---------------------------------------------------------------------------
    const EPOCH_NS_ANCHOR = BigInt(Date.now()) * 1_000_000n;
    const HRTIME_ANCHOR   = process.hrtime.bigint();
    function nowNs() {
        return EPOCH_NS_ANCHOR + (process.hrtime.bigint() - HRTIME_ANCHOR);
    }

    // ---------------------------------------------------------------------------
    // FIR kernel — built once per worker process, reused for every request
    // ---------------------------------------------------------------------------
    function buildHammingFIR(taps, cutoff) {
        const half = Math.floor(taps / 2);
        const k = new Float64Array(taps);
        for (let i = 0; i < taps; i++) {
            const n   = i - half;
            const sinc = n === 0 ? 2 * cutoff
                                 : Math.sin(2 * Math.PI * cutoff * n) / (Math.PI * n);
            const win = 0.54 - 0.46 * Math.cos(2 * Math.PI * i / Math.max(1, taps - 1));
            k[i] = sinc * win;
        }
        return k;
    }
    const KERNEL = buildHammingFIR(FILTER_TAPS, CUTOFF);

    // ---------------------------------------------------------------------------
    // DSP helpers
    // ---------------------------------------------------------------------------
    function firConvolve(signal, kernel) {
        const N = signal.length, M = kernel.length;
        const out = new Float64Array(N);
        for (let i = 0; i < N; i++) {
            let acc = 0;
            for (let j = 0; j < M && j <= i; j++) acc += signal[i - j] * kernel[j];
            out[i] = acc;
        }
        return out;
    }

    function aesDecrypt(iv, ciphertext) {
        const d = crypto.createDecipheriv('aes-256-cbc', AES_KEY, iv);
        return Buffer.concat([d.update(ciphertext), d.final()]);
    }

    function aesEncrypt(plaintext) {
        const iv = crypto.randomBytes(16);
        const c  = crypto.createCipheriv('aes-256-cbc', AES_KEY, iv);
        return { iv, data: Buffer.concat([c.update(plaintext), c.final()]) };
    }

    // ---------------------------------------------------------------------------
    // CSV logging — async write stream (does NOT block event loop)
    // ---------------------------------------------------------------------------
    let csvStream = null;
    if (CSV_FILE) {
        const dir = path.dirname(CSV_FILE);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        const needsHeader = !fs.existsSync(CSV_FILE) || fs.statSync(CSV_FILE).size === 0;
        csvStream = fs.createWriteStream(CSV_FILE, { flags: 'a' });
        if (needsHeader) {
            csvStream.write('arrival_unix_ns,service_ms,queue_ms,response_ms,status_code\n');
        }
    }

    function logCsv(arrivalNs, serviceMs, queueMs, responseMs, status) {
        if (!csvStream) return;
        csvStream.write(
            `${arrivalNs},${serviceMs.toFixed(6)},${queueMs.toFixed(6)},` +
            `${responseMs.toFixed(6)},${status}\n`
        );
    }

    // ---------------------------------------------------------------------------
    // HTTP server
    // ---------------------------------------------------------------------------
    function sendJson(res, status, obj) {
        const body = JSON.stringify(obj);
        res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
        res.end(body);
    }

    const server = http.createServer((req, res) => {
        const arrivalNs = nowNs();
        const reqStart  = process.hrtime.bigint();

        if (req.method === 'GET' && req.url === '/health') {
            return sendJson(res, 200, { ok: true });
        }
        if (req.method === 'GET' && (req.url === '/' || req.url === '')) {
            return sendJson(res, 200, {
                service: 'node-dsp', runtime: `Node.js cluster (${WORKERS} workers)`,
                pipeline: 'AES-256-CBC decrypt → FIR low-pass → AES-256-CBC encrypt',
                signal_length: SIG_LEN, filter_taps: FILTER_TAPS, cutoff: CUTOFF,
                workers: WORKERS,
            });
        }
        if (!(req.method === 'POST' && req.url.startsWith('/process'))) {
            return sendJson(res, 404, { error: 'not_found' });
        }

        const chunks = [];
        req.on('data', c => chunks.push(c));
        req.on('end', () => {
            let parsed;
            try { parsed = JSON.parse(Buffer.concat(chunks)); }
            catch (_) { return sendJson(res, 400, { error: 'invalid_json' }); }

            if (!parsed.iv || !parsed.payload)
                return sendJson(res, 400, { error: 'missing iv or payload' });

            const svcStart = process.hrtime.bigint();

            let raw;
            try {
                const iv = Buffer.from(parsed.iv,      'base64');
                const ct = Buffer.from(parsed.payload, 'base64');
                raw = aesDecrypt(iv, ct);
            } catch (_) {
                const svcEnd = process.hrtime.bigint();
                const svcMs  = Number(svcEnd - svcStart) / 1e6;
                const respMs = Number(svcEnd - reqStart)  / 1e6;
                sendJson(res, 422, { error: 'decrypt_failed' });
                logCsv(arrivalNs, svcMs, Math.max(0, respMs - svcMs), respMs, 422);
                return;
            }

            const nSamples = Math.floor(raw.length / 4);
            const signal   = new Float64Array(nSamples);
            for (let i = 0; i < nSamples; i++) signal[i] = raw.readFloatLE(i * 4);

            const filtered = firConvolve(signal, KERNEL);

            const packed = Buffer.allocUnsafe(nSamples * 4);
            for (let i = 0; i < nSamples; i++) packed.writeFloatLE(filtered[i], i * 4);

            const enc = aesEncrypt(packed);

            const svcEnd  = process.hrtime.bigint();
            const svcMs   = Number(svcEnd - svcStart) / 1e6;
            const respMs  = Number(svcEnd - reqStart)  / 1e6;
            const queueMs = Math.max(0, respMs - svcMs);

            res.writeHead(200, {
                'Content-Type':        'application/json',
                'X-Service-Actual-Ms': svcMs.toFixed(3),
            });
            res.end(JSON.stringify({
                ok:         true,
                iv:         enc.iv.toString('base64'),
                payload:    enc.data.toString('base64'),
                samples:    nSamples,
                service_ms: Math.round(svcMs * 1000) / 1000,
            }));
            logCsv(arrivalNs, svcMs, queueMs, respMs, 200);
        });
    });

    server.listen(PORT, () =>
        console.log(`node-dsp worker pid=${process.pid} :${PORT}  taps=${FILTER_TAPS} cutoff=${CUTOFF} siglen=${SIG_LEN}`));
}
