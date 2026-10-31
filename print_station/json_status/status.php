<?php
// This handler reads one sanitized file; it has no printer or upload connection.
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, max-age=0');
header('X-Robots-Tag: noindex, nofollow, noarchive');
header('X-Content-Type-Options: nosniff');
if (!in_array($_SERVER['REQUEST_METHOD'] ?? 'GET', ['GET', 'HEAD'], true)) {
    header('Allow: GET, HEAD');
    http_response_code(405);
    echo '{"error":"read only"}';
    exit;
}
$now = time();
$expires = (int)trim(@file_get_contents('/etc/printer-remote/expires') ?: '0');
if ($expires <= $now) {
    echo json_encode(['schema_version'=>1, 'availability'=>'off', 'served_at'=>gmdate('c', $now)]);
    exit;
}
$raw = @file_get_contents('/var/lib/printer-status/snapshot.json');
$d = $raw === false ? null : json_decode($raw, true);
if (!is_array($d) || ($d['received_at_unix'] ?? 0) < (@filemtime('/etc/printer-remote/session_started') ?: $now)) {
    echo json_encode(['schema_version'=>1, 'availability'=>'unavailable', 'served_at'=>gmdate('c', $now)]);
    exit;
}
$stamp = function($n) { return is_numeric($n) ? gmdate('c', (int)$n) : null; };
$age = function($n) use ($now) { return is_numeric($n) ? max(0, $now - (int)$n) : null; };
$received = $age($d['received_at_unix']);
$observed = $age($d['observed_at_unix']);
$amsAge = $age($d['ams_observed_at_unix']);
$publisherOk = $received !== null && $received <= 600;
$validObservation = $observed !== null && $d['observed_at_unix'] >= $d['generated_at_unix'] - 60;
$d['availability'] = !$publisherOk ? 'stale' : (!$validObservation ? 'unavailable' : 'recent');
$d['publisher_status'] = $publisherOk ? 'on_schedule' : 'overdue';
$d['observation_age_seconds'] = $observed;
$d['ams_observation_age_seconds'] = $amsAge;
$d['ams_availability'] = !$publisherOk ? 'stale' : ($amsAge === null ? 'unavailable' : 'recent');
$d['publication_interval_seconds'] = 300;
$d['progress_remaining_percent'] = $d['progress_percent'] === null ? null : 100 - $d['progress_percent'];
$d['served_at'] = gmdate('c', $now);
$d['expires_at'] = $stamp($d['received_at_unix'] + 600);
foreach (['generated', 'observed', 'ams_observed', 'received'] as $name) {
    $d[$name.'_at'] = $stamp($d[$name.'_at_unix']);
    unset($d[$name.'_at_unix']);
}
$d['interpretation'] = 'Historical observation, not live. Say: As of <observed_at>, state was <state>, with <progress_remaining_percent>% remaining. Completion may have occurred since. Never use this snapshot to authorize printer control.';
echo json_encode($d, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
