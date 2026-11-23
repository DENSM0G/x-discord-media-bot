from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.config import Config, get_sanitized_subprocess_env

if TYPE_CHECKING:
    from app.storage import DeletedPostNotice, StorageManager

logger = logging.getLogger(__name__)

# Matches X/Twitter links and common mirror domains (including optional <...> wrappers)
X_URL_REGEX = re.compile(
    r"<?https?://(?:(?:www|mobile|m)\.)?"
    r"(?:x\.com|twitter\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|fixvx\.com|twittpr\.com|girlcockx\.com|xcancel\.com)"
    r"/(?:(?P<user>[A-Za-z0-9_]{1,32}|i/web)|i)/status(?:es)?/(?P<tweet_id>\d+)(?:[^\s<>]*)?>?",
    re.IGNORECASE,
)

# Matches any HTTP(S) URL (including optional Discord <...> suppression brackets)
ANY_URL_REGEX = re.compile(r"<?https?://[^\s<>]+>?", re.IGNORECASE)

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".avif",
    ".mp4", ".webm", ".mov", ".m4v", ".mkv",
}

DISK_WATCH_INTERVAL_SEC = 1.0


class ExtractionError(RuntimeError):
    """Raised when media cannot be extracted from an X post."""


class PostSizeLimitError(ExtractionError):
    """Raised when a post exceeds MAX_POST_DISK_PERCENT or exhausts remaining disk space."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class MessageAnalysis:
    x_urls: list[str]
    user_comment: str
    text_length_without_urls: int
    exceeds_limit: bool


@dataclass
class PostStats:
    likes: int | None = None
    retweets: int | None = None
    replies: int | None = None
    views: int | None = None
    bookmarks: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "likes": self.likes,
            "retweets": self.retweets,
            "replies": self.replies,
            "views": self.views,
            "bookmarks": self.bookmarks,
        }


@dataclass
class ExtractedPost:
    original_url: str
    tweet_id: str
    author_name: str
    author_handle: str
    post_text: str
    stats: PostStats
    stats_fetched_at_ts: int
    files: list[Path]
    gif_filenames: set[str] = field(default_factory=set)
    quoted_media: bool = False
    quoted_author_name: str | None = None
    quoted_author_handle: str | None = None
    quoted_url: str | None = None
    extractor_used: str = "gallery-dl"


def _format_bytes_short(size: int) -> str:
    val = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{size} B"


def normalize_x_url(raw_url: str) -> str:
    """Converts any X/Twitter mirror URL into canonical https://x.com/<user>/status/<id>."""
    cleaned = raw_url.strip().strip("<>")
    match = X_URL_REGEX.search(cleaned)
    if not match:
        return cleaned
    user = match.group("user") or "i"
    if user.lower() == "i/web":
        user = "i"
    tweet_id = match.group("tweet_id")
    return f"https://x.com/{user}/status/{tweet_id}"


def extract_x_urls(text: str) -> list[str]:
    """Returns deduplicated canonical X status URLs in order of appearance."""
    seen_ids: set[str] = set()
    urls: list[str] = []
    for match in X_URL_REGEX.finditer(text):
        tweet_id = match.group("tweet_id")
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        urls.append(normalize_x_url(match.group(0)))
    return urls


def _clean_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty).strip()


def analyze_user_message(text: str, max_comment_length: int) -> MessageAnalysis:
    """
    Analyzes a Discord message per TZ 4.2:
    - Extracts canonical X URLs.
    - Checks remaining text length after temporarily stripping ALL URLs (including non-X URLs).
    - Builds the publishable user_comment by stripping ONLY X/mirror URLs (preserving external URLs).
    """
    x_urls = extract_x_urls(text)
    if not x_urls:
        return MessageAnalysis(
            x_urls=[],
            user_comment="",
            text_length_without_urls=0,
            exceeds_limit=False,
        )

    text_without_any_urls = _clean_whitespace(ANY_URL_REGEX.sub(" ", text))
    effective_length = len(text_without_any_urls)
    exceeds_limit = effective_length > max_comment_length

    comment_with_external_urls = _clean_whitespace(X_URL_REGEX.sub(" ", text))
    return MessageAnalysis(
        x_urls=x_urls,
        user_comment=comment_with_external_urls,
        text_length_without_urls=effective_length,
        exceeds_limit=exceeds_limit,
    )


def _proxy_for_cli(proxy_url: str | None) -> str | None:
    """Uses socks5h:// for CLI tools so DNS resolution also routes through Xray/SOCKS5."""
    if not proxy_url:
        return None
    if proxy_url.startswith("socks5://"):
        return "socks5h://" + proxy_url[len("socks5://"):]
    return proxy_url


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = int(value)
        return num if num >= 0 else None
    except (TypeError, ValueError):
        return None


def _first_non_none(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        val = mapping.get(key)
        if val is not None:
            return val
    return None


def _parse_gallery_dl_meta(meta: dict[str, Any]) -> dict[str, Any]:
    author = meta.get("author") if isinstance(meta.get("author"), dict) else {}
    user = meta.get("user") if isinstance(meta.get("user"), dict) else {}
    src_author = author if author.get("name") or author.get("nick") else user

    handle = str(src_author.get("name") or "").strip().lstrip("@")
    display_name = str(src_author.get("nick") or handle or "Unknown").strip()
    content = str(meta.get("content") or "").strip()

    stats = PostStats(
        likes=_safe_int(_first_non_none(meta, "favorite_count", "like_count")),
        retweets=_safe_int(meta.get("retweet_count")),
        replies=_safe_int(meta.get("reply_count")),
        views=_safe_int(_first_non_none(meta, "view_count", "views")),
        bookmarks=_safe_int(meta.get("bookmark_count")),
    )

    return {
        "tweet_id": str(meta.get("tweet_id") or "").strip(),
        "author_name": display_name,
        "author_handle": handle,
        "post_text": content,
        "stats": stats,
    }


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except Exception:
        pass


async def _check_live_disk_quota(
    watch_dir: Path,
    storage: StorageManager,
    config: Config,
    on_fifo_notices: Callable[[list[DeletedPostNotice]], None] | None,
) -> None:
    """
    Reclaims disk space via live FIFO cleanup and verifies that the active download
    has neither exceeded MAX_POST_DISK_PERCENT nor exhausted disk space.
    """
    notices, has_space = await asyncio.to_thread(storage.reclaim_space_live)
    if notices and on_fifo_notices is not None:
        on_fifo_notices(notices)

    max_post_bytes = await asyncio.to_thread(storage.get_max_post_bytes, config.max_post_disk_percent)
    current_size = await asyncio.to_thread(storage.compute_directory_size, watch_dir)

    if max_post_bytes is not None:
        if max_post_bytes <= 0:
            raise PostSizeLimitError(
                "⚠️ Post skipped: no free storage budget left on the bot server for new media."
            )
        if current_size > max_post_bytes:
            raise PostSizeLimitError(
                f"⚠️ Post skipped: media size exceeded the server limit "
                f"({config.max_post_disk_percent:g}% of available bot storage, "
                f"~{_format_bytes_short(max_post_bytes)})."
            )

    if not has_space:
        raise PostSizeLimitError(
            "⚠️ Post skipped: not enough free disk space left on the bot server "
            "(even after clearing older posts)."
        )


async def _run_subprocess(
    cmd: list[str],
    timeout: float = 180.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_sanitized_subprocess_env(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_process(proc)
        await proc.communicate()
        raise ExtractionError(f"Extractor timed out after {int(timeout)}s")

    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def _run_monitored_subprocess(
    cmd: list[str],
    *,
    timeout: float,
    watch_dir: Path,
    storage: StorageManager | None,
    config: Config,
    on_fifo_notices: Callable[[list[DeletedPostNotice]], None] | None,
) -> tuple[int, str, str]:
    """
    Runs a media downloader subprocess with stripped bot secrets and a live disk watcher
    that performs FIFO cleanup mid-download and aborts oversized posts before ENOSPC.
    """
    if storage is None:
        return await _run_subprocess(cmd, timeout=timeout)

    await _check_live_disk_quota(watch_dir, storage, config, on_fifo_notices)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_sanitized_subprocess_env(),
    )
    comm_task = asyncio.create_task(proc.communicate())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _kill_process(proc)
                await comm_task
                raise ExtractionError(f"Extractor timed out after {int(timeout)}s")

            done, _ = await asyncio.wait(
                {comm_task},
                timeout=min(DISK_WATCH_INTERVAL_SEC, remaining),
            )
            if comm_task in done:
                stdout_b, stderr_b = comm_task.result()
                break

            try:
                await _check_live_disk_quota(watch_dir, storage, config, on_fifo_notices)
            except PostSizeLimitError:
                await _kill_process(proc)
                try:
                    await asyncio.wait_for(comm_task, timeout=5.0)
                except Exception:
                    comm_task.cancel()
                raise
    except BaseException:
        if not comm_task.done():
            await _kill_process(proc)
            comm_task.cancel()
        raise

    # Final post-download check catches fast downloads that exceeded the cap in <1s
    await _check_live_disk_quota(watch_dir, storage, config, on_fifo_notices)

    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _gallery_dl_base_cmd(config: Config) -> list[str]:
    cmd = [sys.executable, "-m", "gallery_dl", "--no-mtime"]
    proxy = _proxy_for_cli(config.proxy_url)
    if proxy:
        cmd.extend(["--proxy", proxy])
    if config.cookies_file.is_file() and config.cookies_file.stat().st_size > 0:
        cmd.extend(["--cookies", str(config.cookies_file)])
    return cmd


async def _probe_with_gallery_dl(
    url: str,
    config: Config,
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
    dict[str, Any] | None,
    list[dict[str, Any]],
]:
    """
    Runs gallery-dl in JSON dump mode with quoted=true & text-tweets=true,
    separating main tweet metadata/media from quoted tweet metadata/media.
    """
    cmd = _gallery_dl_base_cmd(config) + [
        "-j",
        "-o", "extractor.twitter.quoted=true",
        "-o", "extractor.twitter.text-tweets=true",
        "-o", "extractor.twitter.retweets=true",
        url,
    ]
    code, stdout, stderr = await _run_subprocess(cmd, timeout=90.0)
    if code != 0 or not stdout.strip():
        logger.warning("gallery-dl probe failed (rc=%d) for %s: %s", code, url, stderr.strip()[:300])