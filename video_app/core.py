import base64
import json
import mimetypes
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ROOT = "https://openrouter.ai"
MODEL = "heygen/avatar-iv"
TERMINAL_ERRORS = {"failed", "cancelled", "expired"}
POLL_INTERVAL = 30
LINK_WAIT_SECONDS = 3600
MAX_IMAGE_BYTES = 20 * 1024 * 1024
STANDARD_RESOLUTIONS = ("720p", "1080p")


def data_directory():
    override = os.environ.get("OPENROUTER_VIDEO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.sys.platform == "darwin":
        return Path.home() / "Library/Application Support/OpenRouter Video"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "OpenRouter Video"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "openrouter-video"


def api_url(value):
    resolved = urljoin(API_ROOT + "/", value)
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "openrouter.ai"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/api/v1/videos/")
    ):
        raise ValueError("Небезопасный адрес OpenRouter: токен не будет отправлен.")
    return resolved


def public_url(value):
    resolved = urljoin(API_ROOT + "/", value)
    parsed = urlsplit(resolved)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Ожидалась корректная HTTPS-ссылка.")
    return resolved


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if request.get_method() != "GET":
            raise ValueError("Перенаправление POST запрещено: запрос не будет повторён.")
        target = public_url(new_url)
        redirected = super().redirect_request(request, response, code, message, headers, target)
        if redirected and urlsplit(target).netloc != urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def read_inputs(image_path, text_mode, text_path, inline_text, voice_id):
    image = Path(image_path).expanduser()
    if not image.is_file():
        raise ValueError("Выберите существующее референсное изображение.")
    if image.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Референсное изображение должно быть не больше 20 МБ.")
    mime = mimetypes.guess_type(str(image))[0]
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("Поддерживаются PNG, JPEG, WebP и GIF.")
    image_bytes = image.read_bytes()
    if not image_bytes:
        raise ValueError("Файл изображения пуст.")
    if text_mode == "file":
        source = Path(text_path).expanduser()
        if not source.is_file():
            raise ValueError("Выберите существующий текстовый файл UTF-8.")
        if source.stat().st_size > 1024 * 1024:
            raise ValueError("Текстовый файл должен быть не больше 1 МБ.")
        prompt = source.read_text(encoding="utf-8-sig")
    else:
        prompt = inline_text
    if not prompt.strip():
        raise ValueError("Текст не может быть пустым.")
    if not voice_id.strip():
        raise ValueError("Укажите voice_id.")
    inputs = {
        "image_path": str(image.resolve()),
        "text_mode": text_mode,
        "text_path": str(Path(text_path).expanduser().resolve()) if text_path.strip() else "",
        "inline_text": inline_text,
        "prompt": prompt,
        "voice_id": voice_id.strip(),
        "model": MODEL,
        "image_mime": mime,
    }
    return inputs, image_bytes


def generation_options(duration=None, resolution=None, size=None):
    if duration is not None and (type(duration) is not int or duration < 1):
        raise ValueError("Длительность должна быть положительным целым числом секунд.")
    if resolution is not None and resolution not in STANDARD_RESOLUTIONS:
        raise ValueError("Выберите разрешение 720p или 1080p либо задайте размер в пикселях.")
    if size is not None and (
        not isinstance(size, str) or re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size) is None
    ):
        raise ValueError("Размер должен содержать положительные ширину и высоту: например, 1280x720.")
    if resolution is not None and size is not None:
        raise ValueError("Нельзя одновременно задавать стандартное разрешение и точный размер.")
    return {
        field: value for field, value in (
            ("duration", duration), ("resolution", resolution), ("size", size)
        ) if value is not None
    }


def make_payload(inputs, image_bytes):
    image_data = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": inputs["model"],
        "prompt": inputs["prompt"],
        "input_references": [{
            "type": "image_url",
            "image_url": {"url": "data:{};base64,{}".format(inputs["image_mime"], image_data)},
        }],
        "provider": {"options": {"heygen": {"voice_id": inputs["voice_id"]}}},
    }
    payload.update(generation_options(
        inputs.get("duration"), inputs.get("resolution"), inputs.get("size")
    ))
    return payload


class ApiError(Exception):
    pass


class ApiClient:
    def __init__(self, token):
        self.token = token.strip()
        self.opener = build_opener(SafeRedirect())

    def request(self, url, payload=None):
        target = API_ROOT + "/api/v1/videos" if payload is not None else api_url(url)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(target, data=body, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Title": "OpenRouter Video Desktop",
        }, method="POST" if payload is not None else "GET")
        try:
            with self.opener.open(request, timeout=90) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ApiError("API вернул ответ не в формате JSON-объекта.")
            return result
        except HTTPError as error:
            detail = error.read(65536).decode("utf-8", errors="replace")
            raise ApiError("HTTP {}: {}".format(error.code, detail[:2000])) from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise ApiError(str(error)) from error

    def download(self, url, destination, authenticated=False):
        target = api_url(url) if authenticated else public_url(url)
        headers = {"Authorization": "Bearer " + self.token} if authenticated else {}
        request = Request(target, headers=headers)
        destination = Path(destination)
        partial = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
        try:
            with self.opener.open(request, timeout=90) as response, partial.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            os.replace(str(partial), str(destination))
            return str(destination)
        finally:
            partial.unlink(missing_ok=True)


class History:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else data_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(str(self.directory / "history.sqlite3"))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                local_id TEXT PRIMARY KEY,
                created REAL NOT NULL,
                inputs TEXT NOT NULL,
                image BLOB NOT NULL,
                status TEXT NOT NULL,
                remote_id TEXT,
                polling_url TEXT,
                response TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                completed_at REAL,
                next_poll REAL,
                failures INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS immutable_inputs
            BEFORE UPDATE OF inputs, image, created ON jobs
            BEGIN
                SELECT RAISE(ABORT, 'Submitted inputs are immutable');
            END;
        """)
        self.connection.execute(
            "UPDATE jobs SET status='uncertain', error=? WHERE status='submitting'",
            ("Приложение закрылось во время отправки. Результат POST неизвестен. "
             "Автоматическая повторная отправка отключена; проверьте историю OpenRouter.",),
        )
        self.connection.commit()
        try:
            (self.directory / "history.sqlite3").chmod(0o600)
        except OSError:
            pass

    def create(self, inputs, image):
        local_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO jobs(local_id,created,inputs,image,status) VALUES(?,?,?,?,?)",
            (local_id, time.time(), json.dumps(inputs, ensure_ascii=False), image, "submitting"),
        )
        self.connection.commit()
        return local_id

    def all(self):
        return self.connection.execute(
            "SELECT local_id,created,inputs,status,remote_id,error FROM jobs ORDER BY created DESC"
        ).fetchall()

    def get(self, local_id):
        row = self.connection.execute("SELECT * FROM jobs WHERE local_id=?", (local_id,)).fetchone()
        result = dict(row)
        result["inputs"] = json.loads(result["inputs"])
        result["response"] = json.loads(result["response"])
        return result

    def update(self, local_id, **fields):
        allowed = {"status", "remote_id", "polling_url", "response", "error", "completed_at", "next_poll", "failures"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Изменять исходные параметры запроса запрещено.")
        if "response" in fields:
            fields["response"] = json.dumps(fields["response"], ensure_ascii=False)
        self.connection.execute(
            "UPDATE jobs SET {} WHERE local_id=?".format(
                ",".join(name + "=?" for name in fields)
            ), list(fields.values()) + [local_id],
        )
        self.connection.commit()

    def apply_response(self, local_id, response, now=None):
        now = time.time() if now is None else now
        job = self.get(local_id)
        merged = dict(job["response"])
        merged.update({key: value for key, value in response.items() if value is not None})
        for field in ("unsigned_urls", "urls"):
            old = job["response"].get(field, [])
            incoming = response.get(field)
            if incoming is None:
                incoming = []
            if not isinstance(incoming, list) or not all(isinstance(url, str) for url in incoming):
                raise ValueError("Некорректный массив " + field)
            merged[field] = list(dict.fromkeys(old + incoming))
        status = response.get("status") or job["status"]
        if job["status"] == "completed" and status not in TERMINAL_ERRORS:
            status = "completed"
        remote_id = response.get("id") or job["remote_id"]
        polling = response.get("polling_url") or job["polling_url"]
        if not polling and remote_id:
            polling = "/api/v1/videos/" + quote(str(remote_id), safe="")
        if polling:
            polling = api_url(polling)
        completed_at = job["completed_at"]
        if status == "completed" and completed_at is None:
            completed_at = now
        should_poll = bool(polling) and status not in TERMINAL_ERRORS
        if status == "completed":
            should_poll = should_poll and not merged["urls"] and now - completed_at < LINK_WAIT_SECONDS
        error = response.get("error")
        self.update(
            local_id,
            response=merged,
            status=status,
            remote_id=remote_id,
            polling_url=polling,
            error=(error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)) if error else "",
            completed_at=completed_at,
            next_poll=now + POLL_INTERVAL if should_poll else None,
            failures=0,
        )

    def poll_error(self, local_id, error, now=None):
        now = time.time() if now is None else now
        job = self.get(local_id)
        failures = job["failures"] + 1
        retry = now + min(30 * (2 ** min(failures, 4)), 300)
        if job["completed_at"] is not None and now - job["completed_at"] >= LINK_WAIT_SECONDS:
            retry = None
        self.update(local_id, error=str(error), failures=failures, next_poll=retry)

    def due(self):
        return self.connection.execute(
            "SELECT local_id,polling_url FROM jobs WHERE next_poll IS NOT NULL AND next_poll<=?",
            (time.time(),),
        ).fetchall()

    def setting(self, name, default=""):
        row = self.connection.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set_setting(self, name, value):
        self.connection.execute(
            "INSERT OR REPLACE INTO settings(name,value) VALUES(?,?)", (name, value)
        )
        self.connection.commit()

    def close(self):
        self.connection.close()
