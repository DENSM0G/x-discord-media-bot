from __future__ import annotations

import html
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.storage import StorageManager, StorageQueueStatus

logger = logging.getLogger(__name__)

URL_LINKIFY_REGEX = re.compile(r"(https?://[^\s<>&\"']+)", re.IGNORECASE)


def format_bytes(size: int) -> str:
    if size < 0:
        size = 0
    units = ("B", "KB", "MB", "GB", "TB")
    val = float(size)
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{size} B"


def format_number(value: int | None) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}".replace(",", " ")


def format_utc_timestamp(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def linkify_post_text(raw_text: str) -> str:
    """Safely escapes post text, preserves line breaks, and converts URLs into clickable links."""
    if not raw_text:
        return ""
    escaped = html.escape(raw_text)

    def _replace(match: re.Match[str]) -> str:
        url = match.group(1)
        trailing = ""
        while url and url[-1] in ".,);:!?":
            trailing = url[-1] + trailing
            url = url[:-1]
        return (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer" class="post-link">{url}</a>'
            f"{trailing}"
        )

    linked = URL_LINKIFY_REGEX.sub(_replace, escaped)
    return linked.replace("\r\n", "\n").replace("\n", "<br>")


class _UnseekableZipBuffer(io.RawIOBase):
    """In-memory streaming sink for zipfile.ZipFile so archives stream without touching disk."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, b: bytes | bytearray | memoryview) -> int:
        data = bytes(b)
        self._chunks.append(data)
        return len(data)

    def pop_bytes(self) -> bytes:
        if not self._chunks:
            return b""
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _stream_zip_files(files: list[tuple[str, Path]], chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """Yields a ZIP archive on the fly (ZIP_STORED) using constant RAM."""
    buf = _UnseekableZipBuffer()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for arcname, file_path in files:
            try:
                st = file_path.stat()
                mtime = time.localtime(st.st_mtime)[:6]
            except OSError:
                continue

            zinfo = zipfile.ZipInfo(filename=arcname, date_time=mtime)
            zinfo.compress_type = zipfile.ZIP_STORED

            with zf.open(zinfo, mode="w", force_zip64=st.st_size >= (1 << 31)) as dest:
                with file_path.open("rb") as src:
                    while True:
                        chunk = src.read(chunk_size)
                        if not chunk:
                            break
                        dest.write(chunk)
                        pending = buf.pop_bytes()
                        if pending:
                            yield pending

            pending = buf.pop_bytes()
            if pending:
                yield pending

    tail = buf.pop_bytes()
    if tail:
        yield tail


def _serialize_storage_status(status: StorageQueueStatus | None) -> dict[str, Any] | None:
    if status is None:
        return None
    data = status.to_dict()
    data.update({
        "older_posts_human": format_bytes(status.older_posts_bytes),
        "current_post_human": format_bytes(status.current_post_bytes),
        "total_storage_human": format_bytes(status.total_storage_bytes),
        "disk_free_human": format_bytes(status.disk_free_bytes),
        "disk_total_human": format_bytes(status.disk_total_bytes),
        "min_free_human": format_bytes(status.min_free_space_bytes),
        "headroom_human": format_bytes(status.headroom_bytes),
    })
    return data


def _build_og_metadata(
    meta: dict[str, Any],
    base_url: str,
    username: str,
    token: str,
) -> dict[str, Any]:
    """Preserves OpenGraph tags so bare gallery URLs generate rich embeds when shared elsewhere."""
    author_name = meta.get("author_name") or "X Post"
    author_handle = meta.get("author_handle") or ""
    title = f"{author_name} (@{author_handle})" if author_handle else author_name
    post_text = str(meta.get("post_text") or "").strip()
    description = (post_text[:240] + "…") if len(post_text) > 240 else post_text

    og_image: str | None = None
    og_video: str | None = None
    og_video_width: int | None = None
    og_video_height: int | None = None

    files = meta.get("files") or []
    for item in files:
        if item.get("type") == "video" and og_video is None:
            og_video = f"{base_url}/{username}/{token}/media/{item['filename']}"
            og_video_width = item.get("width") or None
            og_video_height = item.get("height") or None
        if og_image is None:
            preview = item.get("preview")
            if preview:
                og_image = f"{base_url}/{username}/{token}/media/{preview}"
            elif item.get("type") == "image":
                og_image = f"{base_url}/{username}/{token}/media/{item['filename']}"

    return {
        "title": title,
        "description": description or f"{len(files)} media item(s) archived from X",
        "url": f"{base_url}/{username}/{token}",
        "image": og_image,
        "video": og_video,
        "video_width": og_video_width,
        "video_height": og_video_height,
        "type": "video.other" if og_video else "article",
    }


def create_web_app(config: Config, storage: StorageManager) -> FastAPI:
    app = FastAPI(title="x-discord-media-bot", docs_url=None, redoc_url=None)

    static_dir = config.web_dir / "static"
    templates_dir = config.web_dir / "templates"

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["human_bytes"] = format_bytes
    templates.env.filters["human_number"] = format_number
    templates.env.filters["utc_ts"] = format_utc_timestamp

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            content=(
                "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>x-discord-media-bot</title>"
                "<style>body{background:#0f1117;color:#d6dce8;font-family:system-ui,sans-serif;"
                "display:grid;place-items:center;min-height:100vh;margin:0;}</style></head>"
                "<body><main style='text-align:center'><h1>x-discord-media-bot</h1>"
                "<p style='color:#8e98ad'>Archive gallery service is running.</p></main></body></html>"
            ),
            status_code=200,
        )

    @app.get("/{username}/{token}", response_class=HTMLResponse)
    async def gallery_page(request: Request, username: str, token: str) -> HTMLResponse:
        meta = storage.get_post_by_route(username, token)
        if not meta:
            return templates.TemplateResponse(
                request=request,
                name="gone.html",
                context={"username": username, "token": token},
                status_code=410,
            )

        folder_name = str(meta["folder_name"])
        raw_status = storage.get_post_storage_status(folder_name)
        storage_status = _serialize_storage_status(raw_status)

        media_prefix = f"/{username}/{token}/media"
        enriched_files: list[dict[str, Any]] = []
        for item in meta.get("files") or []:
            entry = dict(item)
            filename = entry["filename"]
            preview = entry.get("preview")
            entry["original_url"] = f"{media_prefix}/{filename}"
            if preview:
                entry["preview_url"] = f"{media_prefix}/{preview}"
            elif entry.get("type") == "video" or entry.get("is_mp4_gif"):
                entry["preview_url"] = ""
            else:
                entry["preview_url"] = entry["original_url"]
            entry["size_human"] = format_bytes(int(entry.get("size_bytes") or 0))
            enriched_files.append(entry)

        og = _build_og_metadata(meta, config.public_base_url, username, token)

        created_ts = meta.get("created_at_ts")
        if not created_ts and meta.get("created_at"):
            try:
                created_ts = int(datetime.fromisoformat(str(meta["created_at"])).timestamp())
            except ValueError:
                created_ts = None

        context: dict[str, Any] = {
            "meta": meta,
            "username": username,
            "token": token,
            "files": enriched_files,
            "default_view_mode": config.default_view_mode,
            "post_text_html": linkify_post_text(str(meta.get("post_text") or "")),
            "stats": meta.get("stats") or {},
            "stats_fetched_at_ts": meta.get("stats_fetched_at_ts"),
            "created_at_ts": created_ts,
            "storage_status": storage_status,
            "download_all_url": f"/{username}/{token}/download-all",
            "status_api_url": f"/{username}/{token}/status",
            "og": og,
        }
        return templates.TemplateResponse(request=request, name="gallery.html", context=context)

    @app.get("/{username}/{token}/status")
    async def gallery_status(username: str, token: str) -> JSONResponse:
        folder_name = storage.get_folder_name_by_route(username, token)
        if not folder_name:
            raise HTTPException(status_code=410, detail="Post has been removed from the server.")
        status = storage.get_post_storage_status(folder_name)
        if status is None:
            raise HTTPException(status_code=410, detail="Post has been removed from the server.")
        return JSONResponse(_serialize_storage_status(status))

    @app.get("/{username}/{token}/download-all")
    async def download_all_zip(username: str, token: str) -> StreamingResponse:
        files = storage.resolve_original_files(username, token)
        if not files:
            raise HTTPException(status_code=410, detail="Files are no longer available on the server.")

        meta = storage.get_post_by_route(username, token) or {}
        tweet_id = str(meta.get("tweet_id") or token)
        safe_handle = re.sub(r"[^A-Za-z0-9_-]", "", str(meta.get("author_handle") or username)) or "xpost"
        zip_filename = f"{safe_handle}_{tweet_id}.zip"

        return StreamingResponse(
            _stream_zip_files(files),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/{username}/{token}/media/{file_path:path}")
    async def serve_media(username: str, token: str, file_path: str) -> FileResponse:
        resolved = storage.resolve_media_path(username, token, file_path)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Media file not found or removed.")
        return FileResponse(
            path=str(resolved),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return app