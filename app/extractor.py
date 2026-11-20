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
from typing import Any

from app.config import Config

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


class ExtractionError(RuntimeError):
    """Raised when media cannot be extracted from an X post."""


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


async def _run_subprocess(
    cmd: list[str],
    timeout: float = 180.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ExtractionError(f"Extractor timed out after {int(timeout)}s")

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
        return None, [], None, []

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("gallery-dl returned invalid JSON for %s: %s", url, exc)
        return None, [], None, []

    if not isinstance(payload, list):
        return None, [], None, []

    main_meta: dict[str, Any] | None = None
    main_media: list[dict[str, Any]] = []
    quoted_meta: dict[str, Any] | None = None
    quoted_media: list[dict[str, Any]] = []

    for entry in payload:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        tag = entry[0]
        if tag == 2 and isinstance(entry[1], dict):
            meta_dict = entry[1]
            if meta_dict.get("quote_id"):
                if quoted_meta is None:
                    quoted_meta = meta_dict
            elif main_meta is None:
                main_meta = meta_dict
        elif tag == 3 and len(entry) >= 3 and isinstance(entry[2], dict):
            item_url = str(entry[1] or "")
            item_meta = dict(entry[2])
            item_meta["_media_url"] = item_url
            if item_meta.get("quote_id"):
                quoted_media.append(item_meta)
                if quoted_meta is None:
                    quoted_meta = item_meta
            else:
                main_media.append(item_meta)
                if main_meta is None:
                    main_meta = item_meta

    return main_meta, main_media, quoted_meta, quoted_media


def _collect_media_files(directory: Path) -> list[Path]:
    return [
        p for p in sorted(directory.iterdir(), key=lambda x: x.name)
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS and p.stat().st_size > 0
    ]


async def _download_with_gallery_dl(
    url: str,
    download_dir: Path,
    config: Config,
) -> list[Path]:
    cmd = _gallery_dl_base_cmd(config) + [
        "-D", str(download_dir),
        "-o", "extractor.twitter.quoted=false",
        "-o", "extractor.twitter.text-tweets=false",
        "-o", "extractor.twitter.retweets=true",
        "-o", "extractor.twitter.videos=true",
        "-o", "filename={num:>02}_{filename}.{extension}",
        url,
    ]
    code, _, stderr = await _run_subprocess(cmd, timeout=240.0)
    files = _collect_media_files(download_dir)
    if code != 0 and not files:
        raise ExtractionError(f"gallery-dl failed (rc={code}): {stderr.strip()[:300]}")
    return files


async def _download_with_yt_dlp(
    url: str,
    download_dir: Path,
    config: Config,
) -> tuple[list[Path], dict[str, Any]]:
    """Fallback extractor via yt-dlp when gallery-dl fails or yields no files."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--no-mtime",
        "--write-info-json",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(download_dir / "%(playlist_index|01)02d_%(id)s.%(ext)s"),
    ]
    proxy = _proxy_for_cli(config.proxy_url)
    if proxy:
        cmd.extend(["--proxy", proxy])
    if config.cookies_file.is_file() and config.cookies_file.stat().st_size > 0:
        cmd.extend(["--cookies", str(config.cookies_file)])
    cmd.append(url)

    code, _, stderr = await _run_subprocess(cmd, timeout=240.0)
    files = _collect_media_files(download_dir)
    if code != 0 and not files:
        raise ExtractionError(f"yt-dlp fallback failed (rc={code}): {stderr.strip()[:300]}")

    info_data: dict[str, Any] = {}
    info_files = sorted(download_dir.glob("*.info.json"))
    if info_files:
        try:
            with info_files[0].open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                info_data = loaded
        except (OSError, json.JSONDecodeError):
            pass
        for info_file in info_files:
            info_file.unlink(missing_ok=True)

    return files, info_data


def _detect_gif_filenames(
    downloaded_files: list[Path],
    media_items: list[dict[str, Any]],
) -> set[str]:
    """Identifies X animated GIFs (delivered as muted MP4 from tweet_video) by index prefix."""
    gif_prefixes: set[str] = set()
    for idx, item in enumerate(media_items, start=1):
        media_url = str(item.get("_media_url") or "").lower()
        item_type = str(item.get("type") or "").lower()
        if "tweet_video" in media_url or item_type == "animated_gif":
            num = _safe_int(item.get("num")) or idx
            gif_prefixes.add(f"{num:02d}_")

    gif_names: set[str] = set()
    for fpath in downloaded_files:
        if fpath.suffix.lower() == ".gif":
            gif_names.add(fpath.name)
            continue
        if any(fpath.name.startswith(prefix) for prefix in gif_prefixes):
            gif_names.add(fpath.name)
    return gif_names


async def extract_post(
    raw_url: str,
    target_dir: Path,
    config: Config,
) -> ExtractedPost:
    """
    Downloads media from an X post into target_dir and collects full post metadata & stats.
    Implements TZ 4.2 Quote Tweet rules:
      1. If the main tweet has its own media -> download only those.
      2. If the main tweet has no media, but quotes another tweet -> download media from the quoted tweet
         and preserve both main post metadata and quoted author attribution.
    """
    canonical_url = normalize_x_url(raw_url)
    match = X_URL_REGEX.search(canonical_url)
    fallback_tweet_id = match.group("tweet_id") if match else "unknown"
    fallback_handle = (match.group("user") or "").lstrip("@") if match else ""
    if fallback_handle.lower() in {"i", "i/web"}:
        fallback_handle = ""

    fetched_ts = int(time.time())
    staging_dir = target_dir / "_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        main_raw_meta, main_media_items, quoted_raw_meta, quoted_media_items = (
            await _probe_with_gallery_dl(canonical_url, config)
        )
        parsed_main = _parse_gallery_dl_meta(main_raw_meta) if main_raw_meta else {
            "tweet_id": fallback_tweet_id,
            "author_name": fallback_handle or "Unknown",
            "author_handle": fallback_handle,
            "post_text": "",
            "stats": PostStats(),
        }

        quoted_media = False
        quoted_author_name: str | None = None
        quoted_author_handle: str | None = None
        quoted_url: str | None = None
        active_media_items = main_media_items
        download_target_url = canonical_url

        # Quote Tweet logic: fetch quoted media ONLY when the main tweet has zero media of its own
        if not main_media_items and quoted_media_items:
            parsed_quote = _parse_gallery_dl_meta(quoted_raw_meta or quoted_media_items[0])
            q_id = parsed_quote["tweet_id"]
            if q_id:
                q_handle = parsed_quote["author_handle"]
                quoted_media = True
                quoted_author_name = parsed_quote["author_name"]
                quoted_author_handle = q_handle or None
                quoted_url = f"https://x.com/{q_handle or 'i'}/status/{q_id}"
                download_target_url = quoted_url
                active_media_items = quoted_media_items

        downloaded_files: list[Path] = []
        extractor_used = "gallery-dl"
        gallery_err: Exception | None = None

        if active_media_items or main_raw_meta is None:
            try:
                downloaded_files = await _download_with_gallery_dl(
                    download_target_url,
                    staging_dir,
                    config,
                )
            except ExtractionError as exc:
                gallery_err = exc
                logger.warning("gallery-dl download failed for %s: %s", download_target_url, exc)

        # Fallback to yt-dlp if gallery-dl produced no files
        if not downloaded_files:
            logger.info("Falling back to yt-dlp for %s", download_target_url)
            try:
                downloaded_files, yt_info = await _download_with_yt_dlp(
                    download_target_url,
                    staging_dir,
                    config,
                )
                extractor_used = "yt-dlp"
                if yt_info and not main_raw_meta:
                    parsed_main["author_name"] = str(
                        yt_info.get("uploader") or parsed_main["author_name"]
                    ).strip()
                    parsed_main["author_handle"] = str(
                        yt_info.get("uploader_id") or parsed_main["author_handle"]
                    ).strip().lstrip("@")
                    parsed_main["post_text"] = str(
                        yt_info.get("description") or yt_info.get("title") or ""
                    ).strip()
                    parsed_main["stats"] = PostStats(
                        likes=_safe_int(yt_info.get("like_count")),
                        retweets=_safe_int(yt_info.get("repost_count")),
                        replies=_safe_int(yt_info.get("comment_count")),
                        views=_safe_int(yt_info.get("view_count")),
                    )
            except ExtractionError as yt_err:
                detail = f"gallery-dl: {gallery_err}; yt-dlp: {yt_err}" if gallery_err else str(yt_err)
                raise ExtractionError(f"No media could be extracted from {canonical_url} ({detail})")

        if not downloaded_files:
            raise ExtractionError(f"Post {canonical_url} contains no downloadable media.")

        gif_filenames = (
            _detect_gif_filenames(downloaded_files, active_media_items)
            if extractor_used == "gallery-dl"
            else {f.name for f in downloaded_files if f.suffix.lower() == ".gif"}
        )

        # Move finalized files from _staging into target_dir
        final_files: list[Path] = []
        final_gif_names: set[str] = set()
        for idx, src_path in enumerate(downloaded_files, start=1):
            ext = src_path.suffix.lower()
            clean_name = f"{idx:02d}_{parsed_main['tweet_id'] or fallback_tweet_id}{ext}"
            dest_path = target_dir / clean_name
            shutil.move(str(src_path), str(dest_path))
            final_files.append(dest_path)
            if src_path.name in gif_filenames:
                final_gif_names.add(clean_name)

        final_handle = parsed_main["author_handle"] or fallback_handle
        final_tweet_id = parsed_main["tweet_id"] or fallback_tweet_id
        if final_handle:
            canonical_url = f"https://x.com/{final_handle}/status/{final_tweet_id}"

        return ExtractedPost(
            original_url=canonical_url,
            tweet_id=final_tweet_id,
            author_name=parsed_main["author_name"] or final_handle or "Unknown",
            author_handle=final_handle,
            post_text=parsed_main["post_text"],
            stats=parsed_main["stats"],
            stats_fetched_at_ts=fetched_ts,
            files=final_files,
            gif_filenames=final_gif_names,
            quoted_media=quoted_media,
            quoted_author_name=quoted_author_name,
            quoted_author_handle=quoted_author_handle,
            quoted_url=quoted_url,
            extractor_used=extractor_used,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)