<?php
$dist = strtolower(getenv('APACHE_SERVICE_DIST') ?: 'lognormal');
$meanUs = (float)(getenv('APACHE_SERVICE_MEAN_US') ?: '200');
if ($meanUs <= 0) {
    $meanUs = 1.0;
}

$messagesFile = getenv('APACHE_MESSAGES_FILE') ?: '/tmp/apache_messages.jsonl';
$maxTextBytes = (int)(getenv('APACHE_MAX_TEXT_BYTES') ?: '1024');
if ($maxTextBytes <= 0) {
    $maxTextBytes = 1024;
}

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
            if ($maxUs < $minUs) {
                $maxUs = $minUs;
            }
            return $minUs + rand_uniform() * ($maxUs - $minUs);
        case 'lognormal':
        case 'ln':
            $sigma = (float)(getenv('APACHE_SERVICE_LOGN_SIGMA') ?: '0.5');
            if ($sigma <= 0) {
                $sigma = 0.5;
            }
            $mu = log($meanUs) - 0.5 * $sigma * $sigma;
            return exp(rand_normal() * $sigma + $mu);
        case 'fixed':
        default:
            return $meanUs;
    }
}

function busy_for_us(float $targetUs): void {
    $targetNs = (int)max(1000.0, $targetUs * 1000.0);
    $startNs = hrtime(true);
    $x = 0x9e3779b9;
    while ((hrtime(true) - $startNs) < $targetNs) {
        $x = ($x * 1664525 + 1013904223) & 0xffffffff;
    }
}

function send_json(int $status, array $payload): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($payload) . "\n";
}

function append_message(string $path, array $msg): void {
    $dir = dirname($path);
    if (!is_dir($dir)) {
        mkdir($dir, 0777, true);
    }
    $fp = fopen($path, 'a+');
    if ($fp === false) {
        throw new RuntimeException('cannot open message file');
    }
    if (!flock($fp, LOCK_EX)) {
        fclose($fp);
        throw new RuntimeException('cannot lock message file');
    }
    fwrite($fp, json_encode($msg) . "\n");
    fflush($fp);
    flock($fp, LOCK_UN);
    fclose($fp);
}

function read_messages(string $path, string $room, int $sinceId, int $limit): array {
    if (!file_exists($path)) {
        return [];
    }
    $limit = max(1, min($limit, 200));
    $fp = fopen($path, 'r');
    if ($fp === false) {
        return [];
    }
    if (!flock($fp, LOCK_SH)) {
        fclose($fp);
        return [];
    }

    $out = [];
    while (($line = fgets($fp)) !== false) {
        $row = json_decode(trim($line), true);
        if (!is_array($row)) {
            continue;
        }
        if (($row['room'] ?? '') !== $room) {
            continue;
        }
        $id = (int)($row['id'] ?? 0);
        if ($id <= $sinceId) {
            continue;
        }
        $out[] = $row;
    }
    flock($fp, LOCK_UN);
    fclose($fp);

    if (count($out) > $limit) {
        $out = array_slice($out, -$limit);
    }
    return $out;
}

function next_message_id(string $path): int {
    if (!file_exists($path)) {
        return 1;
    }
    $lastId = 0;
    $fp = fopen($path, 'r');
    if ($fp === false) {
        return 1;
    }
    if (!flock($fp, LOCK_SH)) {
        fclose($fp);
        return 1;
    }
    while (($line = fgets($fp)) !== false) {
        $row = json_decode(trim($line), true);
        if (!is_array($row)) {
            continue;
        }
        $lastId = max($lastId, (int)($row['id'] ?? 0));
    }
    flock($fp, LOCK_UN);
    fclose($fp);
    return $lastId + 1;
}

$serviceUs = sample_service_us($dist, $meanUs);
$serviceStart = hrtime(true);
busy_for_us($serviceUs);
$serviceEnd = hrtime(true);
$actualServiceMs = ($serviceEnd - $serviceStart) / 1_000_000.0;

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
$query = $_GET;

header('X-Service-Target-Us: ' . number_format($serviceUs, 3, '.', ''));
header('X-Service-Actual-Ms: ' . number_format($actualServiceMs, 3, '.', ''));

if ($path === '/health') {
    send_json(200, ['ok' => true]);
    exit;
}

if ($path === '/send' && $method === 'POST') {
    $raw = file_get_contents('php://input') ?: '';
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

    $id = next_message_id($messagesFile);
    $message = [
        'id' => $id,
        'room' => $room,
        'user' => $user,
        'text' => $text,
        'ts_unix_ns' => (int)(microtime(true) * 1_000_000_000),
    ];
    append_message($messagesFile, $message);
    send_json(201, ['ok' => true, 'message' => $message]);
    exit;
}

if ($path === '/messages' && $method === 'GET') {
    $room = trim((string)($query['room'] ?? 'general'));
    $sinceId = (int)($query['since_id'] ?? 0);
    $limit = (int)($query['limit'] ?? 50);
    $messages = read_messages($messagesFile, $room, $sinceId, $limit);
    send_json(200, [
        'ok' => true,
        'room' => $room,
        'count' => count($messages),
        'messages' => $messages,
    ]);
    exit;
}

send_json(404, [
    'error' => 'not_found',
    'routes' => [
        ['method' => 'GET', 'path' => '/health'],
        ['method' => 'POST', 'path' => '/send'],
        ['method' => 'GET', 'path' => '/messages'],
    ],
]);
