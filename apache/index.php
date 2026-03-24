<?php
// --- Record arrival time and request start immediately ---
$requestStartNs = hrtime(true);
$arrivalNs      = (int)($_SERVER['REQUEST_TIME_FLOAT'] * 1_000_000_000);

$dist        = strtolower(getenv('APACHE_SERVICE_DIST') ?: 'lognormal');
$meanUs      = (float)(getenv('APACHE_SERVICE_MEAN_US') ?: '200');
if ($meanUs <= 0) $meanUs = 1.0;

$messagesFile = getenv('APACHE_MESSAGES_FILE') ?: '/tmp/apache_messages.jsonl';
$maxTextBytes = (int)(getenv('APACHE_MAX_TEXT_BYTES') ?: '1024');
if ($maxTextBytes <= 0) $maxTextBytes = 1024;

$maxMessages  = (int)(getenv('APACHE_MAX_MESSAGES') ?: '1000');
if ($maxMessages <= 0) $maxMessages = 1000;

$csvFile = getenv('APACHE_TRACE_CSV') ?: '';

// ---------------------------------------------------------------------------
// Service-time sampling
// ---------------------------------------------------------------------------

function rand_uniform(): float {
    return mt_rand() / mt_getrandmax();
}

function rand_normal(): float {
    $u1 = max(rand_uniform(), 1e-12);
    $u2 = rand_uniform();
    return sqrt(-2.0 * log($u1)) * cos(2.0 * M_PI * $u2);
}

function sample_service_us(string $dist, float $meanUs): float {
    switch ($dist) {
        case 'exp':
        case 'exponential':
            $u = max(rand_uniform(), 1e-12);
            return -log($u) * $meanUs;
        case 'uniform':
            $minUs = (float)(getenv('APACHE_SERVICE_MIN_US') ?: (string)($meanUs * 0.5));
            $maxUs = (float)(getenv('APACHE_SERVICE_MAX_US') ?: (string)($meanUs * 1.5));
            if ($maxUs < $minUs) $maxUs = $minUs;
            return $minUs + rand_uniform() * ($maxUs - $minUs);
        case 'lognormal':
        case 'ln':
            $sigma = (float)(getenv('APACHE_SERVICE_LOGN_SIGMA') ?: '0.5');
            if ($sigma <= 0) $sigma = 0.5;
            $mu = log($meanUs) - 0.5 * $sigma * $sigma;
            return exp(rand_normal() * $sigma + $mu);
        case 'fixed':
        default:
            return $meanUs;
    }
}

function busy_for_us(float $targetUs): void {
    $targetNs = (int)max(1000.0, $targetUs * 1000.0);
    $startNs  = hrtime(true);
    $x = 0x9e3779b9;
    while ((hrtime(true) - $startNs) < $targetNs) {
        $x = ($x * 1664525 + 1013904223) & 0xffffffff;
    }
}

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

function send_json(int $status, array $payload): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($payload) . "\n";
}

function send_html_file(string $path): void {
    if (!file_exists($path)) {
        http_response_code(500);
        header('Content-Type: text/plain; charset=UTF-8');
        echo "missing frontend asset\n";
        return;
    }
    http_response_code(200);
    header('Content-Type: text/html; charset=UTF-8');
    readfile($path);
}

// ---------------------------------------------------------------------------
// Bounded message store (O(1) ID lookup; file capped at $maxMessages lines)
// ---------------------------------------------------------------------------

/**
 * Read the last line of the JSONL file to get the most recent ID.
 * Seeks to near the end of the file — O(1) regardless of file size.
 */
function next_message_id_fast(string $path): int {
    if (!file_exists($path) || filesize($path) === 0) {
        return 1;
    }
    $fp = fopen($path, 'r');
    if ($fp === false) return 1;
    if (!flock($fp, LOCK_SH)) { fclose($fp); return 1; }

    $size    = filesize($path);
    $seekPos = max(0, $size - 4096);
    fseek($fp, $seekPos);
    $chunk = stream_get_contents($fp);
    flock($fp, LOCK_UN);
    fclose($fp);

    $lines   = array_filter(array_map('trim', explode("\n", $chunk)));
    $lastRow = json_decode((string)end($lines), true);
    return (is_array($lastRow) && isset($lastRow['id'])) ? (int)$lastRow['id'] + 1 : 1;
}

/**
 * Append a message and keep only the last $maxMessages entries.
 *
 * Fast path (file < maxMessages): plain append — O(1).
 * Slow path (file at capacity): read all lines, trim to maxMessages-1, rewrite.
 * This avoids a full read+rewrite on every POST.
 */
function append_message_bounded(string $path, array $msg, int $maxMessages): void {
    $dir = dirname($path);
    if (!is_dir($dir)) mkdir($dir, 0777, true);

    $fp = fopen($path, 'a+');
    if ($fp === false) throw new RuntimeException('cannot open message file');
    if (!flock($fp, LOCK_EX)) { fclose($fp); throw new RuntimeException('cannot lock message file'); }

    // Count existing lines without loading all content
    rewind($fp);
    $lineCount = 0;
    while (fgets($fp) !== false) {
        $lineCount++;
    }

    if ($lineCount < $maxMessages) {
        // Fast path: just append
        fwrite($fp, json_encode($msg) . "\n");
    } else {
        // Slow path: trim to maxMessages-1, then append new entry
        rewind($fp);
        $lines = [];
        while (($line = fgets($fp)) !== false) {
            $line = trim($line);
            if ($line !== '') $lines[] = $line;
        }
        $lines[] = json_encode($msg);
        $lines   = array_slice($lines, -$maxMessages);
        ftruncate($fp, 0);
        rewind($fp);
        fwrite($fp, implode("\n", $lines) . "\n");
    }

    fflush($fp);
    flock($fp, LOCK_UN);
    fclose($fp);
}

/**
 * Return recent messages for $room with id > $sinceId.
 *
 * Reads the (bounded) file but JSON-decodes only the last limit*3 lines,
 * avoiding decoding the full MAX_MESSAGES entries on every GET.
 */
function read_messages(string $path, string $room, int $sinceId, int $limit): array {
    if (!file_exists($path)) return [];
    $limit = max(1, min($limit, 200));

    $fp = fopen($path, 'r');
    if ($fp === false) return [];
    if (!flock($fp, LOCK_SH)) { fclose($fp); return []; }

    $rawLines = [];
    while (($line = fgets($fp)) !== false) {
        $line = trim($line);
        if ($line !== '') $rawLines[] = $line;
    }
    flock($fp, LOCK_UN);
    fclose($fp);

    // Decode only the most recent candidates — avoids O(MAX_MESSAGES) JSON decodes
    $candidates = array_slice($rawLines, -min(count($rawLines), $limit * 3));

    $out = [];
    foreach (array_reverse($candidates) as $line) {
        $row = json_decode($line, true);
        if (!is_array($row)) continue;
        if (($row['room'] ?? '') !== $room) continue;
        if ((int)($row['id'] ?? 0) <= $sinceId) continue;
        $out[] = $row;
        if (count($out) >= $limit) break;
    }
    return array_reverse($out);
}

// ---------------------------------------------------------------------------
// CSV trace logging (same schema as Go server)
// ---------------------------------------------------------------------------

function log_trace(string $path, int $arrivalNs, float $serviceMs, float $queueMs,
                   float $responseMs, int $statusCode): void {
    $dir = dirname($path);
    if (!is_dir($dir)) mkdir($dir, 0777, true);

    // Open with 'a' so the position is always at EOF; check size inside the lock
    // to avoid a race where two processes both see size=0 and both write headers.
    $fp = fopen($path, 'a');
    if ($fp === false) return;
    if (!flock($fp, LOCK_EX)) { fclose($fp); return; }

    // fstat gives the true current inode size after the lock is held,
    // preventing two concurrent processes from both writing the header.
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
// Run synthetic service work (measured before any I/O so it's clean)
// ---------------------------------------------------------------------------

$serviceUs       = sample_service_us($dist, $meanUs);
$serviceStart    = hrtime(true);
busy_for_us($serviceUs);
$serviceEnd      = hrtime(true);
$actualServiceMs = ($serviceEnd - $serviceStart) / 1_000_000.0;

// ---------------------------------------------------------------------------
// Register shutdown hook to log trace after response is sent
// ---------------------------------------------------------------------------

if ($csvFile !== '') {
    register_shutdown_function(
        function() use ($requestStartNs, $arrivalNs, $csvFile, &$actualServiceMs) {
            // service_ms = synthetic busy-loop time (i.i.d. lognormal, comparable to Go).
            // queue_ms   = remaining PHP overhead (file I/O, lock wait, Apache internals).
            // response_ms = total PHP execution time (service_ms + queue_ms).
            $requestEndNs    = hrtime(true);
            $totalResponseMs = ($requestEndNs - $requestStartNs) / 1_000_000.0;
            $queueMs         = max(0.0, $totalResponseMs - $actualServiceMs);
            log_trace($csvFile, $arrivalNs, $actualServiceMs, $queueMs,
                      $totalResponseMs, http_response_code());
        }
    );
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

$method     = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$requestUri = $_SERVER['REQUEST_URI'] ?? '/';
$path       = parse_url($requestUri, PHP_URL_PATH);
$query      = $_GET;

$routeOverride = trim((string)($query['route'] ?? ''));
if ($routeOverride !== '') {
    $path = '/' . ltrim($routeOverride, '/');
} elseif (str_starts_with($path, '/index.php/')) {
    $path = substr($path, strlen('/index.php'));
    if ($path === '') $path = '/';
}

header('X-Service-Target-Us: ' . number_format($serviceUs, 3, '.', ''));
header('X-Service-Actual-Ms: ' . number_format($actualServiceMs, 3, '.', ''));

if (($path === '/' || $path === '/index.html') && $method === 'GET') {
    send_html_file(__DIR__ . '/chat.html');
    exit;
}

if ($path === '/health') {
    send_json(200, ['ok' => true]);
    exit;
}

if ($path === '/send' && $method === 'POST') {
    $raw  = file_get_contents('php://input') ?: '';
    $body = json_decode($raw, true);
    if (!is_array($body)) {
        send_json(400, ['error' => 'invalid_json']);
        exit;
    }

    $user = trim((string)($body['user'] ?? 'anon'));
    $room = trim((string)($body['room'] ?? 'general'));
    $text = trim((string)($body['text'] ?? ''));

    if ($text === '') {
        send_json(400, ['error' => 'text_required']);
        exit;
    }
    if (strlen($text) > $maxTextBytes) {
        send_json(400, ['error' => 'text_too_large']);
        exit;
    }

    $id      = next_message_id_fast($messagesFile);
    $message = [
        'id'          => $id,
        'room'        => $room,
        'user'        => $user,
        'text'        => $text,
        'ts_unix_ns'  => (int)(microtime(true) * 1_000_000_000),
    ];
    append_message_bounded($messagesFile, $message, $maxMessages);
    send_json(201, ['ok' => true, 'message' => $message]);
    exit;
}

if ($path === '/messages' && $method === 'GET') {
    $room     = trim((string)($query['room'] ?? 'general'));
    $sinceId  = (int)($query['since_id'] ?? 0);
    $limit    = (int)($query['limit'] ?? 50);
    $messages = read_messages($messagesFile, $room, $sinceId, $limit);
    send_json(200, [
        'ok'       => true,
        'room'     => $room,
        'count'    => count($messages),
        'messages' => $messages,
    ]);
    exit;
}

send_json(404, [
    'error'  => 'not_found',
    'routes' => [
        ['method' => 'GET',  'path' => '/health'],
        ['method' => 'POST', 'path' => '/send'],
        ['method' => 'GET',  'path' => '/messages'],
    ],
]);
