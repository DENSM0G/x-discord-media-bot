from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.storage import StorageManager


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} КБ"
    return f"{size / (1024 ** 2):.1f} МБ"


def create_web_app(config: Config, storage: StorageManager) -> FastAPI:
    app = FastAPI(title="X Media Archive", docs_url=None, redoc_url=None)

    static_dir = config.web_dir / "static"
    templates_dir = config.web_dir / "templates"

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["format_bytes"] = _format_bytes

    @app.get("/health")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/media/{username}/{token}/{filepath:path}")
    async def serve_media(username: str, token: str, filepath: str) -> FileResponse:
        file_path = storage.resolve_media_path(username, token, filepath)
        if not file_path:
            raise HTTPException(status_code=404, detail="Media file not found")

        return FileResponse(
            path=str(file_path),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/{username}/{token}", response_class=HTMLResponse)
    async def view_post(request: Request, username: str, token: str) -> HTMLResponse:
        post = storage.get_post_by_route(username, token)
        if not post:
            return templates.TemplateResponse(
                request=request,
                name="gone.html",
                status_code=410,
            )

        files = post.get("files") or []
        visuals = [f for f in files if f.get("type") in ("image", "gif")]
        videos = [f for f in files if f.get("type") == "video"]

        # Определяем превью-картинку для OpenGraph-карточки
        og_image = ""
        for item in visuals:
            if item.get("type") == "image" and item.get("preview"):
                og_image = f"{config.public_base_url}/media/{username}/{token}/{item['preview']}"
                break

        page_url = f"{config.public_base_url}/{username}/{token}"

        return templates.TemplateResponse(
            request=request,
            name="gallery.html",
            context={
                "post": post,
                "username": username,
                "token": token,
                "visuals": visuals,
                "videos": videos,
                "og_image": og_image,
                "page_url": page_url,
            },
        )

    return app