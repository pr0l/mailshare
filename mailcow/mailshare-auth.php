<?php
// Эталон для <MAILCOW_DIR>/data/web/mailshare-auth.php (устанавливает check-mailcow.sh)
// Проверка сессии Mailcow для nginx auth_request.
// Только читает сессию: ничего не создаёт, не продлевает, не пишет.

require_once __DIR__ . '/inc/vars.inc.php';
if (file_exists(__DIR__ . '/inc/vars.local.inc.php')) {
  include_once __DIR__ . '/inc/vars.local.inc.php';
}

function deny() { http_response_code(401); exit; }

// ID сессии берём из куки сами, чтобы анониму не создавалась новая сессия
$sid = $_COOKIE[$SESSION_NAME] ?? '';
if (!preg_match('/^[A-Za-z0-9,-]{22,256}$/', $sid)) deny();

ini_set('session.use_cookies', 0);
session_name($SESSION_NAME);
session_id($sid);
if (!@session_start(['read_and_close' => true])) deny();

$s  = $_SESSION;
$ua = $_SERVER['HTTP_USER_AGENT'] ?? '';

if (empty($s['mailcow_cc_username']))                 deny(); // не вошёл
if (($s['mailcow_cc_role'] ?? '') !== 'user')         deny(); // не почтовый ящик
if (isset($s['dual-login']))                          deny(); // админ под чужим ящиком
if (!empty($s['mailcow_cc_api']))                     deny(); // вход по API-ключу
if (($s['SESS_REMOTE_UA'] ?? null) !== $ua)           deny(); // другой браузер
if (!isset($s['LAST_ACTIVITY']) ||
    time() - $s['LAST_ACTIVITY'] > $SESSION_LIFETIME) deny(); // сессия истекла

header('X-Mailshare-User: ' . $s['mailcow_cc_username']);
http_response_code(204);
