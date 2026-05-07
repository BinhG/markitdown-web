# MarkItDown Web

A professional web-based Markdown converter powered by [Microsoft MarkItDown](https://github.com/microsoft/markitdown).

## Features

- **File Upload** — PDF, DOCX, XLSX, PPTX, HTML, images, audio, ZIP...
- **URL Fetch** — Extract Markdown from any webpage
- **Raw Text** — Paste HTML or plain text and convert instantly
- Copy to clipboard & download as `.md`
- Dark mode UI with syntax highlighting

## Quick Start (Local)

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5050
```

## Production Deploy (Docker)

```bash
docker compose up -d
```

The app runs on port **5050** with Gunicorn (4 workers, 2 threads).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5050` | HTTP port |
| `FLASK_DEBUG` | `0` | Enable debug mode |

## Traefik / Reverse Proxy

Update `docker-compose.yml` labels with your domain:
```yaml
- "traefik.http.routers.markitdown.rule=Host(`markitdown.yourdomain.com`)"
```
