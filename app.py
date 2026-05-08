"""
MarkItDown Web — Convert any file or URL to Markdown.
Backend: Flask + microsoft/markitdown
Production: Gunicorn

Temp file strategy:
- Each request gets its own TemporaryDirectory (auto-deleted on context exit)
- On startup: sweep UPLOAD_FOLDER for leftover files older than 60s
- Background thread sweeps every SWEEP_INTERVAL_SECS
- /tmp on Linux is cleaned by OS on reboot — extra safety net
"""

import os
import time
import shutil
import logging
import socket
import traceback
import tempfile
import threading
import ipaddress
from collections import defaultdict
from contextlib import contextmanager
from urllib.parse import urlparse
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("markitdown-web")

try:
    from markitdown import MarkItDown
    logger.info("markitdown loaded successfully")
except ImportError:
    MarkItDown = None
    logger.error("markitdown NOT installed — run: pip install 'markitdown[all]'")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "markitdown_uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE   = 50 * 1024 * 1024   # 50 MB
MAX_TEXT_SIZE   = 1  * 1024 * 1024   # 1 MB  (text paste)
MAX_AGE_SECS    = 60 * 15            # 15 min: orphan sweep threshold
SWEEP_INTERVAL_SECS = 60 * 5         # sweep every 5 min
RATE_LIMIT      = 20                 # max requests / minute / IP

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".html", ".htm",
    ".csv", ".json", ".xml",
    ".txt", ".md", ".rst",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".mp3", ".wav", ".m4a",
    ".zip",
}

# ---------------------------------------------------------------------------
# C1 FIX — Singleton MarkItDown (init once, reuse across requests)
# Protected by a lock so concurrent workers don't double-init.
# ---------------------------------------------------------------------------
_md_lock = threading.Lock()
_md_instance = None


def get_md_converter():
    global _md_instance
    if MarkItDown is None:
        return None, "markitdown not installed. Run: pip install 'markitdown[all]'"
    if _md_instance is None:
        with _md_lock:
            if _md_instance is None:               # double-checked locking
                try:
                    _md_instance = MarkItDown()
                    logger.info("MarkItDown singleton created")
                except Exception as e:
                    logger.error("Failed to init MarkItDown: %s", e)
                    return None, str(e)
    return _md_instance, None


# ---------------------------------------------------------------------------
# C2 FIX — Temp directory context manager
# ---------------------------------------------------------------------------
@contextmanager
def request_tempdir():
    """
    Yields a fresh temporary directory Path.
    Entire directory (including any nested files markitdown may create)
    is deleted on context exit regardless of success or exception.
    """
    tmp = Path(tempfile.mkdtemp(dir=UPLOAD_FOLDER, prefix="req_"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Startup + background sweep — orphan cleanup
# ---------------------------------------------------------------------------
def sweep_upload_folder(max_age: float = MAX_AGE_SECS) -> int:
    """Delete entries in UPLOAD_FOLDER older than max_age seconds."""
    now = time.time()
    removed = 0
    try:
        for entry in UPLOAD_FOLDER.iterdir():
            try:
                age = now - entry.stat().st_mtime
                if age > max_age:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
                    logger.info("Swept orphan: %s (age=%.0fs)", entry.name, age)
            except Exception:
                pass
    except Exception:
        pass
    return removed


def _background_sweeper():
    """Daemon thread: sweeps temp folder on a fixed interval."""
    while True:
        time.sleep(SWEEP_INTERVAL_SECS)
        removed = sweep_upload_folder()
        if removed:
            logger.info("Background sweep: removed %d orphan(s)", removed)


# C2 FIX: use max_age=60 (not 0) to avoid sweeping dirs from sibling
# Gunicorn workers that are actively processing requests right now.
_startup_removed = sweep_upload_folder(max_age=60)
logger.info("Startup sweep: removed %d orphan(s)", _startup_removed)

_sweeper = threading.Thread(target=_background_sweeper, daemon=True, name="TempSweeper")
_sweeper.start()
logger.info("Sweeper started (interval=%ds, max_age=%ds)", SWEEP_INTERVAL_SECS, MAX_AGE_SECS)


# ---------------------------------------------------------------------------
# C3 FIX — SSRF protection
# ---------------------------------------------------------------------------
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_safe_url(url: str) -> tuple[bool, str]:
    """
    Returns (safe, reason).
    Blocks: loopback, private ranges, link-local, metadata endpoints.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False, "Invalid host"
        if host.lower() in _BLOCKED_HOSTS:
            return False, "Blocked host"
        try:
            resolved = socket.gethostbyname(host)
            ip = ipaddress.ip_address(resolved)
            if not ip.is_global:
                return False, "Non-routable IP address"
        except socket.gaierror:
            return False, "Cannot resolve host"
        return True, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# W2 FIX — Simple in-memory rate limiter (per IP, per minute)
# ---------------------------------------------------------------------------
_rate_map: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()


def check_rate_limit() -> bool:
    """Returns True if request is allowed, False if rate limit exceeded."""
    ip = request.remote_addr or "unknown"
    now = time.time()
    with _rate_lock:
        timestamps = _rate_map[ip]
        _rate_map[ip] = [t for t in timestamps if now - t < 60]
        if len(_rate_map[ip]) >= RATE_LIMIT:
            return False
        _rate_map[ip].append(now)
        return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def health():
    md, err = get_md_converter()
    try:
        tmp_entries = sum(1 for _ in UPLOAD_FOLDER.iterdir())
    except Exception:
        tmp_entries = -1

    return jsonify({
        "status": "ok" if md else "degraded",
        "markitdown_available": md is not None,
        "error": err,
        "tmp_entries": tmp_entries,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "max_file_size_mb": MAX_FILE_SIZE // (1024 * 1024),
        "max_text_size_kb": MAX_TEXT_SIZE // 1024,
    })


@app.route("/api/convert/file", methods=["POST"])
def convert_file():
    if not check_rate_limit():
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return jsonify({
            "error": f"Unsupported format: {ext}",
            "supported": sorted(SUPPORTED_EXTENSIONS),
        }), 415

    md, err = get_md_converter()
    if md is None:
        return jsonify({"error": err}), 503

    try:
        with request_tempdir() as tmpdir:
            safe_name = secure_filename(file.filename) or f"upload{ext}"
            tmp_path = tmpdir / safe_name
            file.save(str(tmp_path))
            logger.info("Converting file: %s (%d bytes)", file.filename, tmp_path.stat().st_size)
            result = md.convert(str(tmp_path))
            content = result.text_content or ""
            logger.info("File conversion OK: %d chars", len(content))
            return jsonify({
                "success": True,
                "filename": file.filename,
                "markdown": content,
                "char_count": len(content),
                "line_count": content.count("\n"),
            })
    except Exception:
        logger.error("File conversion error for %s:\n%s", file.filename, traceback.format_exc())
        return jsonify({"error": "Conversion failed. Please check your file and try again."}), 500


@app.route("/api/convert/url", methods=["POST"])
def convert_url():
    if not check_rate_limit():
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "URL must start with http:// or https://"}), 400

    safe, reason = is_safe_url(url)
    if not safe:
        logger.warning("SSRF blocked: %s — %s", url, reason)
        return jsonify({"error": "URL not allowed"}), 403

    md, err = get_md_converter()
    if md is None:
        return jsonify({"error": err}), 503

    try:
        logger.info("Converting URL: %s", url)
        result = md.convert(url)
        content = result.text_content or ""
        logger.info("URL conversion OK: %d chars", len(content))
        return jsonify({
            "success": True,
            "url": url,
            "markdown": content,
            "char_count": len(content),
            "line_count": content.count("\n"),
        })
    except Exception:
        logger.error("URL conversion error for %s:\n%s", url, traceback.format_exc())
        return jsonify({"error": "Failed to fetch or convert URL."}), 500


@app.route("/api/convert/text", methods=["POST"])
def convert_text():
    """Convert raw HTML or plain text pasted by user."""
    if not check_rate_limit():
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text") or ""
    fmt = data.get("format", "html").lower()

    if not text.strip():
        return jsonify({"error": "No text provided"}), 400

    text_bytes = len(text.encode("utf-8"))
    if text_bytes > MAX_TEXT_SIZE:
        return jsonify({
            "error": f"Text too large ({text_bytes // 1024} KB). Maximum is {MAX_TEXT_SIZE // 1024} KB."
        }), 413

    if fmt not in ("html", "text"):
        fmt = "html"

    md, err = get_md_converter()
    if md is None:
        return jsonify({"error": err}), 503

    ext = ".html" if fmt == "html" else ".txt"

    try:
        with request_tempdir() as tmpdir:
            tmp_path = tmpdir / f"input{ext}"
            tmp_path.write_text(text, encoding="utf-8")
            logger.info("Converting text (%s, %d chars)", fmt, len(text))
            result = md.convert(str(tmp_path))
            content = result.text_content or ""
            logger.info("Text conversion OK: %d chars out", len(content))
            return jsonify({
                "success": True,
                "markdown": content,
                "char_count": len(content),
                "line_count": content.count("\n"),
            })
    except Exception:
        logger.error("Text conversion error (fmt=%s, len=%d):\n%s", fmt, len(text), traceback.format_exc())
        return jsonify({"error": "Conversion failed. Please check your input."}), 500


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def request_too_large(e):
    return jsonify({"error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)} MB"}), 413


@app.errorhandler(429)
def too_many_requests(e):
    return jsonify({"error": "Too many requests. Please slow down."}), 429


@app.errorhandler(500)
def internal_error(e):
    logger.error("Unhandled 500 error: %s", traceback.format_exc())
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entrypoint (dev only — use gunicorn in prod)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Starting MarkItDown Web on http://0.0.0.0:%d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
