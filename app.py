"""
MarkItDown Web — Convert any file or URL to Markdown.
Backend: Flask + microsoft/markitdown
Production: Gunicorn
"""

import os
import uuid
import logging
import traceback
import tempfile
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

UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "markitdown_uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
logger.info("Upload folder: %s", UPLOAD_FOLDER)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
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


def get_md_converter():
    if MarkItDown is None:
        return None, "markitdown not installed. Run: pip install 'markitdown[all]'"
    try:
        md = MarkItDown()
        return md, None
    except Exception as e:
        logger.error("Failed to init MarkItDown: %s", e)
        return None, str(e)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def health():
    md, err = get_md_converter()
    return jsonify({
        "status": "ok" if md else "degraded",
        "markitdown_available": md is not None,
        "error": err,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "upload_folder": str(UPLOAD_FOLDER),
        "max_file_size_mb": MAX_FILE_SIZE // (1024 * 1024),
    })


@app.route("/api/convert/file", methods=["POST"])
def convert_file():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": f"Unsupported format: {ext}", "supported": sorted(SUPPORTED_EXTENSIONS)}), 415
    md, err = get_md_converter()
    if md is None:
        return jsonify({"error": err}), 503
    safe_name = secure_filename(file.filename)
    tmp_path = UPLOAD_FOLDER / f"{uuid.uuid4().hex}_{safe_name}"
    try:
        file.save(str(tmp_path))
        logger.info("Converting file: %s (%d bytes)", file.filename, tmp_path.stat().st_size)
        result = md.convert(str(tmp_path))
        content = result.text_content or ""
        logger.info("File conversion OK: %d chars", len(content))
        return jsonify({"success": True, "filename": file.filename, "markdown": content, "char_count": len(content), "line_count": content.count("\n")})
    except Exception as e:
        logger.error("File conversion error for %s:\n%s", file.filename, traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/api/convert/url", methods=["POST"])
def convert_url():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "URL must start with http:// or https://"}), 400
    md, err = get_md_converter()
    if md is None:
        return jsonify({"error": err}), 503
    try:
        logger.info("Converting URL: %s", url)
        result = md.convert(url)
        content = result.text_content or ""
        logger.info("URL conversion OK: %d chars", len(content))
        return jsonify({"success": True, "url": url, "markdown": content, "char_count": len(content), "line_count": content.count("\n")})
    except Exception as e:
        logger.error("URL conversion error for %s:\n%s", url, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/convert/text", methods=["POST"])
def convert_text():
    """Convert raw HTML or plain text pasted by user."""
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text") or ""
    fmt = data.get("format", "html").lower()
    if not text.strip():
        return jsonify({"error": "No text provided"}), 400
    md, err = get_md_converter()
    if md is None:
        return jsonify({"error": err}), 503
    ext = ".html" if fmt == "html" else ".txt"
    tmp_path = UPLOAD_FOLDER / f"{uuid.uuid4().hex}{ext}"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        logger.info("Converting text (%s, %d chars)", fmt, len(text))
        result = md.convert(str(tmp_path))
        content = result.text_content or ""
        logger.info("Text conversion OK: %d chars out", len(content))
        return jsonify({"success": True, "markdown": content, "char_count": len(content), "line_count": content.count("\n")})
    except Exception as e:
        logger.error("Text conversion error (fmt=%s, len=%d):\n%s", fmt, len(text), traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.errorhandler(413)
def request_too_large(e):
    return jsonify({"error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)} MB"}), 413


@app.errorhandler(500)
def internal_error(e):
    logger.error("Unhandled 500 error: %s", traceback.format_exc())
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Starting MarkItDown Web on http://0.0.0.0:%d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
