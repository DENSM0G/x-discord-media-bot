from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

STATUS_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?"
    r"(?:x\.com|twitter\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|girlcockx\.com|cunnyx\.com)"
    r"/([A-Za-z0-9_]{1,30})/status/(\d+)",
    re.IGNORECASE,
)


@dataclass
class ExtractedItem:
    raw_path: Path
    source_type: str | None  # "photo", "animated_gif", "video" или None


@dataclass
class ExtractionResult:
    canonical_url: str
    tweet_id: str
    tweet_author: str
    tweet_text: str
    items: list[ExtractedItem]


def normalize_tweet_url(raw_url: str) -> tuple[str, str, str]:
    """Приводит любую ссылку (включая зеркала) к каноническому виду https://x.com/<user>/status/<id>."""
    match = STATUS_URL_RE.search(raw_url)
    if not match:
        parsed = urlparse(raw_url)
        raise ValueError(f"Некорректная ссылка на пост X: {parsed.netloc}{parsed.path}")
    username, tweet_id = match.group(1), match.group(2)
    return f"https://x.com/{username}/status/{tweet_id}", username, tweet_id


def _normalize_proxy_for_cli(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    if proxy_url.startswith("socks5://"):
        return "socks5h://" + proxy_url[len("socks5://"):]
    return proxy_url


class MediaExtractor:
    def __init__(self, cookies_file: Path, proxy_url: str | None) -> None:
        self.cookies_file = cookies_file
        self.proxy_url = _normalize_proxy_for_cli(proxy_url)

    async def extract(self, raw_url: str, target_folder: Path) -> ExtractionResult:
        canonical_url, url_user, tweet_id = normalize_tweet_url(raw_url)
        raw_dir = target_folder / "_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        result = await self._run_gallery_dl(canonical_url, url_user, tweet_id, raw_dir)
        if result and result.items:
            return result

        # Очищаем _raw перед запуском резервного экстрактора
        for leftover in raw_dir.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)

        logger.warning("gallery-dl не вернул медиа для %s, переключаемся на yt-dlp.", canonical_url)
        fallback = await self._run_yt_dlp(canonical_url, url_user, tweet_id, raw_dir)
        if fallback and fallback.items:
            return fallback

        raise RuntimeError(
            "Не удалось извлечь медиафайлы из поста X. "
            "Возможно, пост удален, аккаунт закрыт или требуется обновить config/cookies.txt."
        )

    async def _run_gallery_dl(
        self, canonical_url: str, url_user: str, tweet_id: str, raw_dir: Path
    ) -> ExtractionResult | None:
        cmd: list[str] = [
            sys.executable,
            "-m",
            "gallery_dl",
            "--directory",
            str(raw_dir),
            "--filename",
            "{num:>02}_{id}.{extension}",
            "--write-metadata",
            "--no-mtime",
        ]
        if self.cookies_file.is_file():
            cmd.extend(["--cookies", str(self.cookies_file)])
        if self.proxy_url:
            cmd.extend(["--proxy", self.proxy_url])
        cmd.append(canonical_url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        media_files = sorted(
            p for p in raw_dir.iterdir()
            if p.is_file() and not p.name.endswith(".json") and p.stat().st_size > 0
        )
        if not media_files:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            logger.debug("gallery-dl stderr: %s", err_text)
            return None

        tweet_author = f"@{url_user}"
        tweet_text = ""
        items: list[ExtractedItem] = []

        for media_path in media_files:
            sidecar_candidates = [
                Path(f"{media_path}.json"),
                media_path.with_suffix(".json"),
            ]
            meta_json: dict[str, Any] = {}
            for sc in sidecar_candidates:
                if sc.is_file():
                    try:
                        meta_json = json.loads(sc.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        meta_json = {}
                    break

            if meta_json:
                author_obj = meta_json.get("author") or meta_json.get("user") or {}
                if isinstance(author_obj, dict) and author_obj.get("name"):
                    tweet_author = f"@{author_obj['name']}"
                if not tweet_text and isinstance(meta_json.get("content"), str):
                    tweet_text = meta_json["content"].strip()

            raw_type = meta_json.get("type") if isinstance(meta_json.get("type"), str) else None
            items.append(ExtractedItem(raw_path=media_path, source_type=raw_type))

        return ExtractionResult(
            canonical_url=canonical_url,
            tweet_id=tweet_id,
            tweet_author=tweet_author,
            tweet_text=tweet_text,
            items=items,
        )

    async def _run_yt_dlp(
        self, canonical_url: str, url_user: str, tweet_id: str, raw_dir: Path
    ) -> ExtractionResult | None:
        cmd: list[str] = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--yes-playlist",
            "--write-info-json",
            "--no-mtime",
            "-f",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format",
            "mp4",
            "-o",
            str(raw_dir / "%(playlist_index|1)02d_%(id)s.%(ext)s"),
        ]
        if self.cookies_file.is_file():
            cmd.extend(["--cookies", str(self.cookies_file)])
        if self.proxy_url:
            cmd.extend(["--proxy", self.proxy_url])
        cmd.append(canonical_url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        media_files = sorted(
            p for p in raw_dir.iterdir()
            if p.is_file() and not p.name.endswith(".json") and p.stat().st_size > 0
        )
        if not media_files:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            logger.error("yt-dlp error for %s: %s", canonical_url, err_text)
            return None

        tweet_author = f"@{url_user}"
        tweet_text = ""
        items: list[ExtractedItem] = []

        for media_path in media_files:
            info_path = media_path.with_suffix(".info.json")
            if info_path.is_file():
                try:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                    if info.get("uploader_id"):
                        tweet_author = f"@{info['uploader_id']}"
                    if not tweet_text and isinstance(info.get("description"), str):
                        tweet_text = info["description"].strip()
                except (OSError, json.JSONDecodeError):
                    pass
            items.append(ExtractedItem(raw_path=media_path, source_type=None))

        return ExtractionResult(
            canonical_url=canonical_url,
            tweet_id=tweet_id,
            tweet_author=tweet_author,
            tweet_text=tweet_text,
            items=items,
        )