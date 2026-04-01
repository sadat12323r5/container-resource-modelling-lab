<?php
// Capture arrival time immediately — before any work
$requestStartNs = hrtime(true);
$arrivalNs      = (int)($_SERVER['REQUEST_TIME_FLOAT'] * 1_000_000_000);

// ---------------------------------------------------------------------------
// Config (all tunable via docker-compose environment)
// ---------------------------------------------------------------------------
$signalLength = max(1,    (int)(getenv('DSP_SIGNAL_LENGTH') ?: '1024'));
$filterTaps   = max(2,    (int)(getenv('DSP_FILTER_TAPS')   ?: '64'));
$cutoff       = max(0.01, min(0.49, (float)(getenv('DSP_CUTOFF_FREQ') ?: '0.25')));
$csvFile      = getenv('DSP_TRACE_CSV') ?: '';

// AES-256-CBC key — 32 raw bytes, supplied as 64 hex chars in DSP_AES_KEY.
// Shared between client (load generator) and server.
$aesKeyHex = getenv('DSP_AES_KEY') ?: str_repeat('00', 32);
$aesKey    = @hex2bin($aesKeyHex);
if ($aesKey === false || strlen($aesKey) < 32) {
    $aesKey = str_repeat("\x00", 32);
} else {
    $aesKey = substr($aesKey, 0, 32);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function send_json(int $status, array $payload): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($payload) . "\n";
}

function aes_decrypt(string $key, string $iv, string $ciphertext): string|false {
    return openssl_decrypt($ciphertext, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv);
}

function aes_encrypt(string $key, string $plaintext): array {
    $iv        = random_bytes(16);
    $encrypted = openssl_encrypt($plaintext, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv);
    return ['iv' => $iv, 'data' => $encrypted ?: $plaintext];
}

function log_trace(string $path, int $arrivalNs, float $serviceMs,
                   float $queueMs, float $responseMs, int $statusCode): void {
    $dir = dirname($path);
    if (!is_dir($dir)) mkdir($dir, 0777, true);
    $fp = fopen($path, 'a');
    if ($fp === false) return;
    if (!flock($fp, LOCK_EX)) { fclose($fp); return; }
    $stat = fstat($fp);
    if ($stat !== false && $stat['size'] === 0) {
        fwrite($fp, "arrival_unix_ns,service_ms,queue_ms,response_ms,status_code\n");
    }
    fwrite($fp, sprintf("%d,%.6f,%.6f,%.6f,%d\n",
        $arrivalNs, $serviceMs, $queueMs, $responseMs, $statusCode));
    fflush($fp);
    flock($fp, LOCK_UN);
    fclose($fp);
}

// ---------------------------------------------------------------------------
// Routing — do this before service work so health checks are cheap
// ---------------------------------------------------------------------------
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$path   = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);

$routeOverride = trim((string)($_GET['route'] ?? ''));
if ($routeOverride !== '') {
    $path = '/' . ltrim($routeOverride, '/');
} elseif (str_starts_with($path, '/index.php/')) {
    $path = substr($path, strlen('/index.php'));
    if ($path === '') $path = '/';
}

if ($path === '/health') {
    send_json(200, ['ok' => true]);
    exit;
}

if ($path === '/' && $method === 'GET') {
    send_json(200, [
        'service'        => 'secure-dsp',
        'description'    => 'End-to-end encrypted signal processing: AES-decrypt → FIR filter → AES-encrypt',
        'use_case'       => 'Client sends AES-256-CBC encrypted float32 signal; server filters and returns encrypted result',
        'signal_length'  => $signalLength,
        'filter_taps'    => $filterTaps,
        'cutoff_freq'    => $cutoff,
        'aes_mode'       => 'AES-256-CBC',
        'request_format' => ['iv' => 'base64(16 bytes)', 'payload' => 'base64(AES-encrypted float32 array)'],
    ]);
    exit;
}

if (!($path === '/process' && $method === 'POST')) {
    send_json(404, ['error' => 'not_found', 'routes' => [
        ['method' => 'GET',  'path' => '/health'],
        ['method' => 'GET',  'path' => '/'],
        ['method' => 'POST', 'path' => '/process'],
    ]]);
    exit;
}

// ---------------------------------------------------------------------------
// POST /process — the full pipeline starts here.
//
// Expected request body (JSON):
//   {
//     "iv":      "<base64-encoded 16-byte AES IV>",
//     "payload": "<base64-encoded AES-256-CBC ciphertext of float32 array>"
//   }
//
// Pipeline:
//   1. Parse + validate request
//   2. AES-256-CBC decrypt  → raw float32 bytes
//   3. Unpack float32 array → signal samples
//   4. Build Hamming-windowed sinc FIR low-pass kernel
//   5. Convolve signal with kernel  (O(N × M) — the heavy CPU step)
//   6. Pack filtered samples → raw bytes
//   7. AES-256-CBC encrypt with fresh IV → response payload
//
// Response (JSON):
//   {
//     "ok":      true,
//     "iv":      "<base64 of response IV>",
//     "payload": "<base64 of encrypted filtered signal>",
//     "samples": <int>,
//     "service_ms": <float>
//   }
// ---------------------------------------------------------------------------

// Register CSV logger before any early-exit so every request is recorded.
if ($csvFile !== '') {
    $actualServiceMsRef = 0.0;
    register_shutdown_function(
        function() use ($requestStartNs, $arrivalNs, $csvFile, &$actualServiceMsRef) {
            $requestEndNs    = hrtime(true);
            $totalResponseMs = ($requestEndNs - $requestStartNs) / 1_000_000.0;
            $queueMs         = max(0.0, $totalResponseMs - $actualServiceMsRef);
            log_trace($csvFile, $arrivalNs, $actualServiceMsRef, $queueMs,
                      $totalResponseMs, http_response_code());
        }
    );
}

// --- Step 1: Parse request ---
$body = file_get_contents('php://input');
$req  = json_decode($body, true);
if (!is_array($req) || empty($req['iv']) || empty($req['payload'])) {
    send_json(400, ['error' => 'bad_request',
                    'detail' => 'body must be JSON with "iv" and "payload" (both base64)']);
    exit;
}

$ivBytes  = base64_decode($req['iv'],      true);
$ctBytes  = base64_decode($req['payload'], true);
if ($ivBytes === false || strlen($ivBytes) !== 16 || $ctBytes === false || strlen($ctBytes) === 0) {
    send_json(400, ['error' => 'bad_request',
                    'detail' => '"iv" must be base64(16 bytes); "payload" must be non-empty base64']);
    exit;
}

// ---------------------------------------------------------------------------
// SERVICE WORK — timed from here
// ---------------------------------------------------------------------------
$serviceStartNs = hrtime(true);

// --- Step 2: AES-256-CBC decrypt ---
$rawBytes = aes_decrypt($aesKey, $ivBytes, $ctBytes);
if ($rawBytes === false || strlen($rawBytes) === 0) {
    $serviceEndNs = hrtime(true);
    send_json(422, ['error' => 'decrypt_failed', 'detail' => 'AES decryption error — wrong key or corrupt payload']);
    exit;
}

// --- Step 3: Unpack float32 samples ---
// PHP unpack('f*', ...) returns a 1-indexed array; reindex with array_values.
$signal  = array_values(unpack('f*', $rawBytes) ?: []);
$nSamples = count($signal);
if ($nSamples === 0) {
    $serviceEndNs = hrtime(true);
    send_json(422, ['error' => 'empty_signal', 'detail' => 'Decrypted payload contains no float32 samples']);
    exit;
}

// --- Step 4: Build Hamming-windowed sinc FIR low-pass kernel ---
// Cutoff is normalised frequency (0–0.5). At 44.1 kHz sampling, cutoff=0.25 → 11 kHz.
// This removes high-frequency noise while preserving speech and music content.
$kernel = [];
$half   = (int)($filterTaps / 2);
for ($i = 0; $i < $filterTaps; $i++) {
    $n    = $i - $half;
    $sinc = ($n === 0)
        ? 2.0 * $cutoff
        : sin(2.0 * M_PI * $cutoff * $n) / (M_PI * $n);
    $win      = 0.54 - 0.46 * cos(2.0 * M_PI * $i / max(1, $filterTaps - 1)); // Hamming
    $kernel[] = $sinc * $win;
}

// --- Step 5: FIR convolution — O(nSamples × filterTaps), the heavy CPU loop ---
// Each output sample is the dot-product of the signal window with the kernel.
// Causal: only uses past samples (j <= i), so no look-ahead required.
$filtered = [];
for ($i = 0; $i < $nSamples; $i++) {
    $acc = 0.0;
    for ($j = 0; $j < $filterTaps && $j <= $i; $j++) {
        $acc += $signal[$i - $j] * $kernel[$j];
    }
    $filtered[] = $acc;
}

// --- Step 6: Pack filtered samples back to IEEE-754 float32 ---
$packedOut = pack('f*', ...$filtered);

// --- Step 7: AES-256-CBC encrypt with a fresh IV ---
['iv' => $respIv, 'data' => $encryptedOut] = aes_encrypt($aesKey, $packedOut);

$serviceEndNs   = hrtime(true);
$actualServiceMs = ($serviceEndNs - $serviceStartNs) / 1_000_000.0;
if ($csvFile !== '') {
    $actualServiceMsRef = $actualServiceMs; // picked up by shutdown function
}
// ---------------------------------------------------------------------------
// End of service work
// ---------------------------------------------------------------------------

header('X-Service-Actual-Ms: ' . number_format($actualServiceMs, 3, '.', ''));
header('X-Samples-In: '  . $nSamples);
header('X-Samples-Out: ' . count($filtered));
header('X-Filter-Taps: ' . $filterTaps);

send_json(200, [
    'ok'         => true,
    'iv'         => base64_encode($respIv),
    'payload'    => base64_encode($encryptedOut),
    'samples'    => count($filtered),
    'service_ms' => round($actualServiceMs, 3),
]);
