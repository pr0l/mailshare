"""Mailshare Gateway.

Принимает файлы от сотрудника, вошедшего в почту Mailcow, частями передаёт их
в Pingvin Share X под служебной учётной записью и возвращает одну публичную
ссылку на все файлы отправки.

Кто пользователь, Gateway узнаёт только из заголовка X-Mailshare-User, который
выставляет Nginx Mailcow после проверки сессии (auth_request). Поэтому Gateway
должен быть доступен только из внутренней Docker-сети, без опубликованных портов.
"""

import asyncio
import base64
import http.client
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import string
import threading
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

# --------------------------------------------------------------------------
# Настройки (из переменных окружения)
# --------------------------------------------------------------------------

PINGVIN_URL = os.environ.get("PINGVIN_URL", "http://mailshare-pingvin:3000").rstrip("/")
PINGVIN_USER = os.environ.get("PINGVIN_USER", "")
PINGVIN_PASSWORD = os.environ.get("PINGVIN_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
MAIL_ORIGIN = os.environ.get("MAIL_ORIGIN", "").rstrip("/")

GB = 1000 ** 3
# Лимит на одну отправку (все файлы вместе), байт. Тот же лимит получает Pingvin (share.maxSize).
# MAX_SHARE_GB и MAX_FILE_GB — старые имена той же настройки, в гигабайтах.
if os.environ.get("MAX_SHARE_BYTES"):
    MAX_SHARE_BYTES = int(os.environ["MAX_SHARE_BYTES"])
else:
    MAX_SHARE_BYTES = int(float(os.environ.get("MAX_SHARE_GB") or os.environ.get("MAX_FILE_GB") or "0.3") * GB)
MAX_FILES = int(os.environ.get("MAX_FILES_PER_SHARE", "100"))
DAILY_QUOTA_BYTES = int(float(os.environ.get("DAILY_QUOTA_GB", "10")) * GB)
MAX_ACTIVE_UPLOADS = int(os.environ.get("MAX_ACTIVE_UPLOADS", "3"))
EXPIRATION_DAYS = int(os.environ.get("SHARE_EXPIRATION_DAYS", "30"))
STALE_SECONDS = int(float(os.environ.get("STALE_UPLOAD_HOURS", "2")) * 3600)
CHUNK_SIZE_FALLBACK = int(os.environ.get("CHUNK_SIZE", "10000000"))
DB_PATH = os.environ.get("DB_PATH", "/data/gateway.db")
PREFIX = "/mailshare"
DISPLAY_TZ = timezone(timedelta(hours=float(os.environ.get("DISPLAY_TZ_OFFSET", "3"))))  # даты в карточке и PDF
PDF_NAME = os.environ.get("PDF_NAME", "download-files.pdf")
PDF_FONT = os.environ.get("PDF_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
PDF_FONT_BOLD = os.environ.get("PDF_FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mailshare")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
ID_ALPHABET = string.ascii_letters + string.digits


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------
# Журнал отправок (SQLite)
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    user TEXT NOT NULL,
                    share_id TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    chunk_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    client_ip TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS batch_files (
                    batch_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    next_chunk INTEGER NOT NULL DEFAULT 0,
                    inited INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (batch_id, idx)
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS batches_user ON batches(user, created_at)")
            self.db.execute("CREATE INDEX IF NOT EXISTS batches_status ON batches(status, updated_at)")

    def q(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None


store: Store | None = None


# --------------------------------------------------------------------------
# Клиент Pingvin
# --------------------------------------------------------------------------

class PingvinResponse:
    def __init__(self, status: int, body: bytes, set_cookies: list[str]):
        self.status = status
        self.body = body
        self.set_cookies = set_cookies

    def json(self):
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None

    def text(self, limit: int = 300) -> str:
        return self.body[:limit].decode("utf-8", "replace")


class Pingvin:
    def __init__(self, base_url: str):
        parts = urlsplit(base_url)
        self.host = parts.hostname
        self.port = parts.port or 80
        self.token: str | None = None
        self.token_exp = 0.0
        self.token_lock = asyncio.Lock()
        self._chunk_size: int | None = None
        self._chunk_size_at = 0.0

    def _do(self, method: str, path: str, body: bytes | None, headers: dict) -> PingvinResponse:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=300)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            cookies = resp.headers.get_all("Set-Cookie") or []
            return PingvinResponse(resp.status, data, cookies)
        finally:
            conn.close()

    async def raw(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        return await asyncio.to_thread(self._do, method, path, body, dict(headers or {}))

    async def login(self):
        field = "email" if "@" in PINGVIN_USER else "username"
        payload = json.dumps({field: PINGVIN_USER, "password": PINGVIN_PASSWORD}).encode()
        r = await self.raw("POST", "/api/auth/signIn", payload, {"Content-Type": "application/json"})
        if r.status >= 400:
            raise ApiError(502, f"Не удалось войти в Pingvin ({r.status})", detail=r.text())
        token = None
        for c in r.set_cookies:
            m = re.match(r"\s*access_token=([^;]+)", c)
            if m:
                token = m.group(1)
        if not token:
            data = r.json() or {}
            token = data.get("accessToken") if isinstance(data, dict) else None
        if not token:
            raise ApiError(502, "Pingvin не выдал токен доступа")
        self.token = token
        self.token_exp = self._token_expiry(token)
        log.info("pingvin: вход выполнен, токен действует %d с", self.token_exp - time.time())

    @staticmethod
    def _token_expiry(token: str) -> float:
        """Срок действия из JWT (поле exp). Если прочитать не удалось — считаем 10 минут."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except Exception:  # noqa: BLE001
            return time.time() + 600

    def _token_fresh(self) -> bool:
        return bool(self.token) and self.token_exp - time.time() > 60

    async def call(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
        """Запрос от имени служебной учётной записи.

        Токен Pingvin живёт 15 минут: обновляем его заранее, а если Pingvin всё же
        ответил 401 или 403 (просроченный токен он отклоняет и так, и так),
        входим заново и повторяем запрос один раз.
        """
        for attempt in range(2):
            if not self._token_fresh():
                async with self.token_lock:
                    if not self._token_fresh():
                        await self.login()
            token = self.token
            headers = {"Cookie": f"access_token={token}"}
            if content_type:
                headers["Content-Type"] = content_type
            r = await self.raw(method, path, body, headers)
            if r.status in (401, 403) and attempt == 0:
                log.info("pingvin: ответ %d, входим заново", r.status)
                async with self.token_lock:
                    if self.token == token:
                        self.token = None
                continue
            return r
        return r

    async def chunk_size(self) -> int:
        """Размер части берём из настроек Pingvin: он проверяет номер части по размеру."""
        if self._chunk_size and time.time() - self._chunk_size_at < 300:
            return self._chunk_size
        size = None
        try:
            r = await self.raw("GET", "/api/configs")
            for item in r.json() or []:
                if isinstance(item, dict) and item.get("key") == "share.chunkSize":
                    size = int(item.get("value"))
        except Exception as e:  # noqa: BLE001
            log.warning("pingvin: не удалось прочитать share.chunkSize: %s", e)
        if not size or size <= 0:
            size = CHUNK_SIZE_FALLBACK
        self._chunk_size, self._chunk_size_at = size, time.time()
        return size


pingvin = Pingvin(PINGVIN_URL)
batch_locks: dict[str, asyncio.Lock] = {}


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def now() -> int:
    return int(time.time())


def current_user(request: Request) -> str:
    user = request.headers.get("x-mailshare-user", "").strip().lower()
    if not user or not EMAIL_RE.match(user):
        raise ApiError(401, "Нет входа в почту. Откройте почту, войдите и вернитесь на эту страницу.")
    return user


def check_origin(request: Request):
    """Защита от подделки запросов: изменяющие запросы только со страницы почты."""
    if request.headers.get("origin", "").rstrip("/") != MAIL_ORIGIN:
        raise ApiError(403, "Запрос отклонён: он пришёл не со страницы почты.")


def clean_name(name) -> str:
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    if name in ("", ".", ".."):
        name = "file"
    if len(name) > 200:
        base, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 15:
            name = base[: 200 - len(ext) - 1] + "." + ext
        else:
            name = name[:200]
    return name


def unique_names(names: list[str]) -> list[str]:
    """Одинаковые имена в одной отправке: «отчёт.pdf», «отчёт (2).pdf», …"""
    seen: set[str] = set()
    out = []
    for name in names:
        candidate, n = name, 2
        base, dot, ext = name.rpartition(".")
        if not dot or not base:
            base, dot, ext = name, "", ""
        while candidate.lower() in seen:
            candidate = f"{base} ({n}){dot}{ext}"
            n += 1
        seen.add(candidate.lower())
        out.append(candidate)
    return out


def new_share_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(20))


def used_today(user: str) -> int:
    row = store.one(
        "SELECT COALESCE(SUM(size), 0) AS s FROM batches "
        "WHERE user = ? AND created_at > ? AND status IN ('uploading', 'done')",
        (user, now() - 86400),
    )
    return int(row["s"])


def get_batch(batch_id: str, user: str):
    row = store.one("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if not row or row["user"] != user:
        raise ApiError(404, "Загрузка не найдена.")
    return row


def get_file(batch_id: str, idx: int):
    row = store.one("SELECT * FROM batch_files WHERE batch_id = ? AND idx = ?", (batch_id, idx))
    if not row:
        raise ApiError(404, "Файл не найден в этой отправке.")
    return row


def expected_length(f, chunk_size: int, index: int) -> int:
    if index < f["total_chunks"] - 1:
        return chunk_size
    return f["size"] - chunk_size * (f["total_chunks"] - 1)


async def delete_share(share_id: str):
    try:
        r = await pingvin.call("DELETE", f"/api/shares/{share_id}")
        if r.status >= 400 and r.status != 404:
            log.warning("pingvin: не удалось удалить share %s: %s %s", share_id, r.status, r.text())
    except Exception as e:  # noqa: BLE001
        log.warning("pingvin: ошибка удаления share %s: %s", share_id, e)


def fmt_size(b: int) -> str:
    def n(v: float, d: int) -> str:
        t = f"{v:.{d}f}"
        if t.endswith(".0"):
            t = t[:-2]
        return t.replace(".", ",")
    if b >= 1e9:
        return n(b / 1e9, 0 if b >= 1e10 else 1) + " ГБ"
    if b >= 1e6:
        return n(b / 1e6, 0 if b >= 1e8 else 1) + " МБ"
    if b >= 1e3:
        return f"{round(b / 1e3)} КБ"
    return f"{b} Б"


def plural_files(n: int) -> str:
    a, b = n % 10, n % 100
    if a == 1 and b != 11:
        return "файл"
    if 2 <= a <= 4 and not 12 <= b <= 14:
        return "файла"
    return "файлов"


def expires_of(batch) -> datetime:
    return datetime.fromtimestamp(batch["created_at"], timezone.utc) + timedelta(days=EXPIRATION_DAYS)


_pdf_fonts_ready = False


def build_pdf(user: str, url: str, files, total: int, expires: datetime) -> bytes:
    """Короткий PDF со списком файлов и ссылкой — прикладывается к письму как вложение."""
    global _pdf_fonts_ready
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if not _pdf_fonts_ready:
        pdfmetrics.registerFont(TTFont("MS", PDF_FONT))
        pdfmetrics.registerFont(TTFont("MSB", PDF_FONT_BOLD))
        _pdf_fonts_ready = True

    dark, grey, line, blue = HexColor("#202124"), HexColor("#5f6368"), HexColor("#dadce0"), HexColor("#2f6fde")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Файлы для скачивания")
    c.setAuthor(user)
    W, H = A4
    x, right = 56, A4[0] - 56
    y = H - 72

    c.setFont("MSB", 18); c.setFillColor(dark)
    c.drawString(x, y, "Файлы для скачивания")
    y -= 24
    c.setFont("MS", 10.5); c.setFillColor(grey)
    c.drawString(x, y, f"Отправитель: {user} · {len(files)} {plural_files(len(files))} · {fmt_size(total)}")
    y -= 30
    c.setStrokeColor(line); c.line(x, y, right, y)
    y -= 24

    for f in files:
        if y < 150:
            c.showPage()
            y = H - 72
        size = fmt_size(f["size"])
        name = f["name"]
        max_w = right - x - c.stringWidth(size, "MS", 12) - 16
        while len(name) > 4 and c.stringWidth(name, "MS", 12) > max_w:
            name = name[:-2] + "…"
        c.setFont("MS", 12); c.setFillColor(dark); c.drawString(x, y, name)
        c.setFillColor(grey); c.drawRightString(right, y, size)
        y -= 22

    y -= 8
    c.setStrokeColor(line); c.line(x, y, right, y)
    y -= 44
    bw, bh = 190, 34
    c.setFillColor(blue); c.roundRect(x, y - 8, bw, bh, 6, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff")); c.setFont("MSB", 12.5)
    c.drawCentredString(x + bw / 2, y + 4, "Скачать файлы")
    c.linkURL(url, (x, y - 8, x + bw, y - 8 + bh), relative=0)
    y -= 34
    c.setFont("MS", 10.5); c.setFillColor(blue); c.drawString(x, y, url)
    c.linkURL(url, (x, y - 3, x + c.stringWidth(url, "MS", 10.5), y + 11), relative=0)
    y -= 26
    c.setFillColor(grey)
    for text in (f"Ссылка действует до {expires.astimezone(DISPLAY_TZ):%d.%m.%Y}, после этого файлы удаляются.",
                 "Регистрация и пароль для скачивания не нужны.",
                 "Если кнопка не открывается, скопируйте ссылку в адресную строку браузера."):
        c.drawString(x, y, text)
        y -= 16
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------
# Обработчики
# --------------------------------------------------------------------------

async def page(request: Request):
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def me(request: Request):
    user = current_user(request)
    return JSONResponse({
        "user": user,
        "max_share_bytes": MAX_SHARE_BYTES,
        "max_files": MAX_FILES,
        "daily_quota_bytes": DAILY_QUOTA_BYTES,
        "used_today_bytes": used_today(user),
        "expiration_days": EXPIRATION_DAYS,
    })


async def create_upload(request: Request):
    """Новая отправка: один share в Pingvin на все выбранные файлы."""
    user = current_user(request)
    check_origin(request)
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(400, "Неверный запрос.")
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        raise ApiError(400, "Не выбраны файлы.")
    if len(files) > MAX_FILES:
        raise ApiError(400, f"Слишком много файлов: не больше {MAX_FILES} за одну отправку.")

    names, sizes = [], []
    for f in files:
        if not isinstance(f, dict):
            raise ApiError(400, "Неверный запрос.")
        try:
            size = int(f.get("size"))
        except (TypeError, ValueError):
            raise ApiError(400, "Не указан размер файла.")
        name = clean_name(f.get("name"))
        if size <= 0:
            raise ApiError(400, f"Файл «{name}» пустой.")
        names.append(name)
        sizes.append(size)
    names = unique_names(names)
    total = sum(sizes)

    if total > MAX_SHARE_BYTES:
        raise ApiError(413, f"Вместе файлы больше допустимых {fmt_size(MAX_SHARE_BYTES)} за одну отправку.")
    if used_today(user) + total > DAILY_QUOTA_BYTES:
        raise ApiError(429, f"Превышен лимит {fmt_size(DAILY_QUOTA_BYTES)} в сутки.")
    active = store.one(
        "SELECT COUNT(*) AS n FROM batches WHERE user = ? AND status = 'uploading' AND updated_at > ?",
        (user, now() - STALE_SECONDS),
    )["n"]
    if active >= MAX_ACTIVE_UPLOADS:
        raise ApiError(429, "Слишком много одновременных загрузок. Дождитесь окончания текущих.")

    chunk_size = await pingvin.chunk_size()
    share_id = new_share_id()
    r = await pingvin.call(
        "POST", "/api/shares",
        json.dumps({
            "id": share_id,
            "expiration": f"{EXPIRATION_DAYS}-days",
            "recipients": [],
            "security": {},
            "size": total,
        }).encode(),
        "application/json",
    )
    if r.status >= 400:
        log.error("pingvin: создание share: %s %s", r.status, r.text())
        raise ApiError(502, "Хранилище не приняло файлы. Попробуйте позже.")

    batch_id = secrets.token_urlsafe(18)
    t = now()
    store.q(
        "INSERT INTO batches (id, user, share_id, size, file_count, chunk_size, status, client_ip, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'uploading', ?, ?, ?)",
        (batch_id, user, share_id, total, len(names), chunk_size,
         request.headers.get("x-forwarded-for", ""), t, t),
    )
    out = []
    for i, (name, size) in enumerate(zip(names, sizes)):
        total_chunks = (size + chunk_size - 1) // chunk_size
        store.q(
            "INSERT INTO batch_files (batch_id, idx, file_id, name, size, total_chunks) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, i, str(uuid.uuid4()), name, size, total_chunks),
        )
        out.append({"index": i, "name": name, "total_chunks": total_chunks, "next_chunk": 0})

    log.info("share start user=%s share=%s files=%d size=%d", user, share_id, len(names), total)
    return JSONResponse({"upload_id": batch_id, "chunk_size": chunk_size, "files": out})


async def upload_chunk(request: Request):
    user = current_user(request)
    check_origin(request)
    batch_id = request.path_params["upload_id"]
    fi = request.path_params["file_index"]
    index = request.path_params["index"]
    async with batch_locks.setdefault(batch_id, asyncio.Lock()):
        b = get_batch(batch_id, user)
        if b["status"] != "uploading":
            raise ApiError(409, "Загрузка уже завершена или отменена.")
        f = get_file(batch_id, fi)
        if index < f["next_chunk"]:
            # Эта часть уже принята (повтор после обрыва связи) — просто подтверждаем.
            return JSONResponse({"next_chunk": f["next_chunk"]})
        if index > f["next_chunk"] or index >= f["total_chunks"]:
            raise ApiError(409, "Части файла пришли не по порядку.", next_chunk=f["next_chunk"])

        need = expected_length(f, b["chunk_size"], index)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) != need:
            raise ApiError(400, "Неверный размер части файла.")
        body = await request.body()
        if len(body) != need:
            raise ApiError(400, "Неверный размер части файла.")

        if not f["inited"]:
            r = await pingvin.call(
                "POST", f"/api/shares/{b['share_id']}/files/upload-init",
                json.dumps({"id": f["file_id"], "name": f["name"], "totalChunks": f["total_chunks"]}).encode(),
                "application/json",
            )
            if r.status >= 400:
                log.error("pingvin: upload-init share=%s: %s %s", b["share_id"], r.status, r.text())
                raise ApiError(502, "Хранилище не приняло файл. Повторяем…")
            store.q("UPDATE batch_files SET inited = 1 WHERE batch_id = ? AND idx = ?", (batch_id, fi))

        qs = (f"id={quote(f['file_id'])}&name={quote(f['name'], safe='')}"
              f"&chunkIndex={index}&totalChunks={f['total_chunks']}")
        r = await pingvin.call("POST", f"/api/shares/{b['share_id']}/files?{qs}",
                               body, "application/octet-stream")
        if r.status < 300:
            store.q("UPDATE batch_files SET next_chunk = ? WHERE batch_id = ? AND idx = ?",
                    (index + 1, batch_id, fi))
            store.q("UPDATE batches SET updated_at = ? WHERE id = ?", (now(), batch_id))
            return JSONResponse({"next_chunk": index + 1})

        data = r.json()
        if r.status == 400 and isinstance(data, dict) and "expectedChunkIndex" in data:
            expected = int(data["expectedChunkIndex"])
            store.q("UPDATE batch_files SET next_chunk = ? WHERE batch_id = ? AND idx = ?",
                    (expected, batch_id, fi))
            raise ApiError(409, "Части файла пришли не по порядку.", next_chunk=expected)

        log.error("pingvin: часть %d файла %d share=%s: %s %s",
                  index, fi, b["share_id"], r.status, r.text())
        raise ApiError(502, "Хранилище не приняло часть файла. Повторяем…")


async def complete_upload(request: Request):
    user = current_user(request)
    check_origin(request)
    batch_id = request.path_params["upload_id"]
    async with batch_locks.setdefault(batch_id, asyncio.Lock()):
        b = get_batch(batch_id, user)
        files = store.q("SELECT * FROM batch_files WHERE batch_id = ? ORDER BY idx", (batch_id,))
        if b["status"] == "uploading":
            if any(f["next_chunk"] != f["total_chunks"] for f in files):
                raise ApiError(409, "Не все файлы загружены полностью.")
            r = await pingvin.call("POST", f"/api/shares/{b['share_id']}/complete")
            if r.status >= 400:
                log.error("pingvin: complete share=%s: %s %s", b["share_id"], r.status, r.text())
                raise ApiError(502, "Не удалось завершить загрузку. Попробуйте ещё раз.")
            store.q("UPDATE batches SET status = 'done', updated_at = ? WHERE id = ?", (now(), batch_id))
            log.info("share done user=%s share=%s files=%d size=%d names=%r",
                     user, b["share_id"], len(files), b["size"], [f["name"] for f in files])
        elif b["status"] != "done":
            raise ApiError(409, "Загрузка отменена.")
    batch_locks.pop(batch_id, None)
    expires = expires_of(b)
    return JSONResponse({
        "url": f"{PUBLIC_URL}/s/{b['share_id']}",
        "files": [{"name": f["name"], "size": f["size"]} for f in files],
        "size": b["size"],
        "user": user,
        "expires_at": expires.isoformat(),
        "expires_date": f"{expires.astimezone(DISPLAY_TZ):%d.%m.%Y}",
        "pdf_name": PDF_NAME,
    })


async def upload_pdf(request: Request):
    """PDF со ссылкой для завершённой отправки (только её владельцу)."""
    user = current_user(request)
    b = get_batch(request.path_params["upload_id"], user)
    if b["status"] != "done":
        raise ApiError(409, "Загрузка ещё не завершена.")
    files = store.q("SELECT name, size FROM batch_files WHERE batch_id = ? ORDER BY idx", (b["id"],))
    data = await asyncio.to_thread(build_pdf, user, f"{PUBLIC_URL}/s/{b['share_id']}",
                                   files, b["size"], expires_of(b))
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"files.pdf\"; filename*=UTF-8''{quote(PDF_NAME)}",
        "Cache-Control": "no-store",
    })


async def cancel_upload(request: Request):
    user = current_user(request)
    check_origin(request)
    batch_id = request.path_params["upload_id"]
    async with batch_locks.setdefault(batch_id, asyncio.Lock()):
        b = get_batch(batch_id, user)
        if b["status"] == "uploading":
            await delete_share(b["share_id"])
            store.q("UPDATE batches SET status = 'cancelled', updated_at = ? WHERE id = ?", (now(), batch_id))
            log.info("share cancelled user=%s share=%s", user, b["share_id"])
    batch_locks.pop(batch_id, None)
    return JSONResponse({"ok": True})


async def health(request: Request):
    return PlainTextResponse("ok")


async def api_error(request: Request, exc: ApiError):
    return JSONResponse({"error": exc.message, **exc.extra}, status_code=exc.status)


# --------------------------------------------------------------------------
# Фоновая очистка брошенных загрузок
# --------------------------------------------------------------------------

async def cleanup_loop():
    while True:
        try:
            rows = store.q(
                "SELECT id, share_id, user FROM batches WHERE status = 'uploading' AND updated_at < ?",
                (now() - STALE_SECONDS,),
            )
            for row in rows:
                await delete_share(row["share_id"])
                store.q("UPDATE batches SET status = 'abandoned', updated_at = ? WHERE id = ?",
                        (now(), row["id"]))
                batch_locks.pop(row["id"], None)
                log.info("share abandoned user=%s share=%s", row["user"], row["share_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("очистка: %s", e)
        await asyncio.sleep(900)


@asynccontextmanager
async def lifespan(app):
    global store
    missing = [k for k in ("PINGVIN_USER", "PINGVIN_PASSWORD", "PUBLIC_URL", "MAIL_ORIGIN")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))
    store = Store(DB_PATH)
    task = asyncio.create_task(cleanup_loop())
    log.info("gateway запущен: pingvin=%s public=%s mail=%s", PINGVIN_URL, PUBLIC_URL, MAIL_ORIGIN)
    yield
    task.cancel()


U = f"{PREFIX}/api/uploads/{{upload_id}}"
app = Starlette(
    routes=[
        Route(f"{PREFIX}/", page, methods=["GET"]),
        Route(f"{PREFIX}/api/me", me, methods=["GET"]),
        Route(f"{PREFIX}/api/uploads", create_upload, methods=["POST"]),
        Route(f"{U}/files/{{file_index:int}}/chunks/{{index:int}}", upload_chunk, methods=["POST"]),
        Route(f"{U}/complete", complete_upload, methods=["POST"]),
        Route(f"{U}/pdf", upload_pdf, methods=["GET"]),
        Route(U, cancel_upload, methods=["DELETE"]),
        Route("/health", health, methods=["GET"]),
    ],
    exception_handlers={ApiError: api_error},
    lifespan=lifespan,
)
