from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import string
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRANSLIT_MAP: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

TOKEN_ALPHABET = string.ascii_lowercase + string.digits


@dataclass
class DeletedPostNotice:
    """Data required to edit a Discord message after its files are removed from disk."""
    channel_id: int
    message_id: int
    reason: str
    deleted_at_ts: int
    parent_channel_id: int | None = None
    webhook_id: int | None = None
    gallery_url: str | None = None

    @property
    def discord_marker(self) -> str:
        return f"`deleted from bot server:` <t:{self.deleted_at_ts}:f>"


@dataclass
class QueuedJob:
    """Persistent representation of a pending Discord message job stored in queue.json."""
    message_id: int
    channel_id: int
    guild_id: int | None
    author_id: int
    author_display_name: str
    author_avatar_url: str | None
    urls: list[str]
    user_comment: str = ""
    parent_channel_id: int | None = None
    next_url_index: int = 0
    created_at_ts: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QueuedJob | None:
        try:
            urls = raw.get("urls")
            if not isinstance(urls, list) or not urls:
                return None
            return cls(
                message_id=int(raw["message_id"]),
                channel_id=int(raw["channel_id"]),
                guild_id=int(raw["guild_id"]) if raw.get("guild_id") is not None else None,
                author_id=int(raw["author_id"]),
                author_display_name=str(raw.get("author_display_name") or "User"),
                author_avatar_url=str(raw["author_avatar_url"]) if raw.get("author_avatar_url") else None,
                urls=[str(u) for u in urls],
                user_comment=str(raw.get("user_comment") or ""),
                parent_channel_id=(
                    int(raw["parent_channel_id"]) if raw.get("parent_channel_id") is not None else None
                ),
                next_url_index=max(0, int(raw.get("next_url_index", 0))),
                created_at_ts=int(raw.get("created_at_ts", int(time.time()))),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class StorageQueueStatus:
    """Dynamic snapshot of disk space and a post's position in the FIFO cleanup queue."""
    folder_name: str
    position: int
    total_posts: int
    older_posts_count: int
    older_posts_bytes: int
    current_post_bytes: int
    total_storage_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int
    min_free_space_bytes: int
    headroom_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_username(display_name: str, user_id: int) -> str:
    """Converts a Discord display name into a URL-safe username segment."""
    lowered = display_name.strip().lower()
    transliterated = "".join(TRANSLIT_MAP.get(ch, ch) for ch in lowered)
    cleaned = re.sub(r"\s+", "_", transliterated)
    cleaned = re.sub(r"[^a-z0-9_-]", "", cleaned)
    cleaned = re.sub(r"[_-]{2,}", "_", cleaned).strip("_-")
    if not cleaned:
        return f"user_{user_id}"
    return cleaned[:32]


def format_deletion_reason(deleted_at_ts: int) -> str:
    return f"`deleted from bot server:` <t:{deleted_at_ts}:f>"


class StorageManager:
    def __init__(self, data_dir: Path, min_free_space_bytes: int) -> None:
        self.data_dir = data_dir
        self.min_free_space_bytes = min_free_space_bytes
        self.index_file = self.data_dir / "index.json"
        self.queue_file = self.data_dir / "queue.json"

        self._lock = threading.RLock()
        self.route_to_folder: dict[str, str] = {}
        self.message_to_folder: dict[int, str] = {}
        self.folder_sizes: dict[str, int] = {}
        self.active_folders: set[str] = set()
        self.pending_discord_notices: list[DeletedPostNotice] = []
        self._queued_jobs: dict[int, QueuedJob] = {}

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_queue()

    # ------------------------------------------------------------------
    # Internal atomic JSON & size helpers
    # ------------------------------------------------------------------

    def _write_json_atomic(self, target_path: Path, payload: Any) -> None:
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)

    def _compute_folder_size(self, folder_path: Path) -> int:
        total = 0
        if not folder_path.is_dir():
            return 0
        for root, _, filenames in os.walk(folder_path):
            for name in filenames:
                file_path = Path(root) / name
                try:
                    total += file_path.stat().st_size
                except OSError:
                    continue
        return total

    def _build_notice_from_meta(
        self,
        meta: dict[str, Any] | None,
        deleted_at_ts: int,
        reason: str | None = None,
    ) -> DeletedPostNotice | None:
        if not meta:
            return None
        discord_info = meta.get("discord") or {}
        msg_ids = discord_info.get("bot_message_ids") or []
        channel_id = discord_info.get("channel_id")
        if not channel_id or not msg_ids:
            return None

        parent_ch = discord_info.get("parent_channel_id")
        webhook_id = discord_info.get("webhook_id")
        return DeletedPostNotice(
            channel_id=int(channel_id),
            message_id=int(msg_ids[0]),
            reason=reason or format_deletion_reason(deleted_at_ts),
            deleted_at_ts=deleted_at_ts,
            parent_channel_id=int(parent_ch) if parent_ch is not None else None,
            webhook_id=int(webhook_id) if webhook_id is not None else None,
            gallery_url=discord_info.get("gallery_url"),
        )

    # ------------------------------------------------------------------
    # Persistent disk queue (data/queue.json)
    # ------------------------------------------------------------------

    def _load_queue(self) -> None:
        with self._lock:
            self._queued_jobs.clear()
            if not self.queue_file.is_file():
                return
            try:
                with self.queue_file.open("r", encoding="utf-8") as f:
                    raw_items = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read queue.json (%s) — resetting queue.", exc)
                self._save_queue()
                return

            if not isinstance(raw_items, list):
                return

            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                job = QueuedJob.from_dict(entry)
                if job is not None:
                    self._queued_jobs[job.message_id] = job

            if self._queued_jobs:
                logger.info("Restored %d pending job(s) from queue.json.", len(self._queued_jobs))

    def _save_queue(self) -> None:
        ordered = sorted(self._queued_jobs.values(), key=lambda j: (j.created_at_ts, j.message_id))
        self._write_json_atomic(self.queue_file, [job.to_dict() for job in ordered])

    def enqueue_job(self, job: QueuedJob) -> None:
        with self._lock:
            self._queued_jobs[job.message_id] = job
            self._save_queue()

    def update_job_progress(self, message_id: int, next_url_index: int) -> None:
        with self._lock:
            job = self._queued_jobs.get(message_id)
            if job is None:
                return
            job.next_url_index = max(0, next_url_index)
            self._save_queue()

    def dequeue_job(self, message_id: int) -> None:
        with self._lock:
            if message_id in self._queued_jobs:
                self._queued_jobs.pop(message_id, None)
                self._save_queue()

    def list_queued_jobs(self) -> list[QueuedJob]:
        with self._lock:
            return sorted(self._queued_jobs.values(), key=lambda j: (j.created_at_ts, j.message_id))

    # ------------------------------------------------------------------
    # Post folder allocation & registration
    # ------------------------------------------------------------------

    def allocate_post_folder(self) -> Path:
        """Creates a new YYYY-MM-DD_HH-MM-SS_SSS directory and protects it from FIFO cleanup."""
        with self._lock:
            while True:
                now = datetime.now()
                millis = now.microsecond // 1000
                folder_name = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{millis:03d}"
                folder_path = self.data_dir / folder_name
                if not folder_path.exists():
                    folder_path.mkdir(parents=True, exist_ok=False)
                    (folder_path / ".previews").mkdir(exist_ok=True)
                    self.active_folders.add(folder_name)
                    return folder_path
                time.sleep(0.002)

    def release_active_folder(self, folder_name: str) -> None:
        with self._lock:
            self.active_folders.discard(folder_name)

    def generate_unique_route(self, display_name: str, user_id: int) -> tuple[str, str]:
        """Generates a unique (url_username, url_token) pair."""
        safe_user = sanitize_username(display_name, user_id)
        with self._lock:
            while True:
                token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
                route_key = f"{safe_user}/{token}"
                if route_key not in self.route_to_folder:
                    return safe_user, token

    def save_index(self) -> None:
        with self._lock:
            self._write_json_atomic(self.index_file, self.route_to_folder)

    def register_post(self, folder_path: Path, meta: dict[str, Any]) -> None:
        """Persists meta.json for a new post and updates in-memory and on-disk indexes."""
        with self._lock:
            folder_name = folder_path.name
            meta["folder_name"] = folder_name
            meta_path = folder_path / "meta.json"
            self._write_json_atomic(meta_path, meta)

            route_key = f"{meta['url_username']}/{meta['url_token']}"
            self.route_to_folder[route_key] = folder_name
            self.folder_sizes[folder_name] = self._compute_folder_size(folder_path)

            discord_info = meta.get("discord") or {}
            for msg_id in discord_info.get("bot_message_ids", []):
                self.message_to_folder[int(msg_id)] = folder_name

            self.save_index()

    def attach_discord_messages(
        self,
        folder_name: str,
        guild_id: int | None,
        channel_id: int,
        author_id: int,
        author_display_name: str,
        bot_message_ids: list[int],
        parent_channel_id: int | None = None,
        webhook_id: int | None = None,
        gallery_url: str | None = None,
    ) -> None:
        """Links sent Discord (webhook or bot) message IDs to the stored post."""
        with self._lock:
            meta = self.load_meta(folder_name)
            if not meta:
                return

            meta["discord"] = {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "parent_channel_id": parent_channel_id,
                "webhook_id": webhook_id,
                "author_id": author_id,
                "author_display_name": author_display_name,
                "bot_message_ids": [int(mid) for mid in bot_message_ids],
                "gallery_url": gallery_url,
            }
            folder_path = self.data_dir / folder_name
            self._write_json_atomic(folder_path / "meta.json", meta)
            self.folder_sizes[folder_name] = self._compute_folder_size(folder_path)

            for msg_id in bot_message_ids:
                self.message_to_folder[int(msg_id)] = folder_name

    # ------------------------------------------------------------------
    # Lookups & safe file resolution
    # ------------------------------------------------------------------

    def load_meta(self, folder_name: str) -> dict[str, Any] | None:
        meta_path = self.data_dir / folder_name / "meta.json"
        if not meta_path.is_file():
            return None
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            data.setdefault("folder_name", folder_name)
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def get_folder_name_by_route(self, username: str, token: str) -> str | None:
        with self._lock:
            return self.route_to_folder.get(f"{username}/{token}")

    def get_post_by_route(self, username: str, token: str) -> dict[str, Any] | None:
        folder_name = self.get_folder_name_by_route(username, token)
        if not folder_name:
            return None
        return self.load_meta(folder_name)

    def get_post_by_message_id(self, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            folder_name = self.message_to_folder.get(int(message_id))
        if not folder_name:
            return None
        return self.load_meta(folder_name)

    def resolve_media_path(self, username: str, token: str, relative_path: str) -> Path | None:
        """Safely resolves a media path while blocking path traversal attempts."""
        folder_name = self.get_folder_name_by_route(username, token)
        if not folder_name:
            return None

        folder_root = (self.data_dir / folder_name).resolve()
        candidate = (folder_root / relative_path).resolve()

        if not candidate.is_relative_to(folder_root) or not candidate.is_file():
            return None
        return candidate

    def resolve_original_files(self, username: str, token: str) -> list[tuple[str, Path]]:
        """Returns ordered (filename, absolute_path) pairs for all original media files in a post."""
        meta = self.get_post_by_route(username, token)
        if not meta:
            return []

        folder_name = meta["folder_name"]
        folder_root = (self.data_dir / folder_name).resolve()
        resolved: list[tuple[str, Path]] = []

        for item in meta.get("files") or []:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not filename:
                continue
            candidate = (folder_root / filename).resolve()
            if candidate.is_relative_to(folder_root) and candidate.is_file():
                resolved.append((Path(filename).name, candidate))

        return resolved

    # ------------------------------------------------------------------
    # Dynamic disk & FIFO queue statistics (for web gallery footer)
    # ------------------------------------------------------------------

    def get_post_storage_status(self, folder_name: str) -> StorageQueueStatus | None:
        """Calculates current disk space and exact FIFO cleanup queue position for a post."""
        with self._lock:
            if folder_name not in self.folder_sizes:
                folder_path = self.data_dir / folder_name
                if not folder_path.is_dir():
                    return None
                self.folder_sizes[folder_name] = self._compute_folder_size(folder_path)

            ordered_folders = sorted(self.folder_sizes.keys())
            try:
                index = ordered_folders.index(folder_name)
            except ValueError:
                return None

            older_bytes = sum(self.folder_sizes[name] for name in ordered_folders[:index])
            current_bytes = self.folder_sizes[folder_name]
            total_storage_bytes = sum(self.folder_sizes.values())

        usage = shutil.disk_usage(self.data_dir)
        headroom = max(0, usage.free - self.min_free_space_bytes)

        return StorageQueueStatus(
            folder_name=folder_name,
            position=index + 1,
            total_posts=len(ordered_folders),
            older_posts_count=index,
            older_posts_bytes=older_bytes,
            current_post_bytes=current_bytes,
            total_storage_bytes=total_storage_bytes,
            disk_free_bytes=usage.free,
            disk_total_bytes=usage.total,
            min_free_space_bytes=self.min_free_space_bytes,
            headroom_bytes=headroom,
        )

    # ------------------------------------------------------------------
    # Deletion, startup audit & FIFO disk cleanup
    # ------------------------------------------------------------------

    def delete_post(self, folder_name: str, reason: str | None = None) -> DeletedPostNotice | None:
        """Deletes a post directory from disk, updates indexes, and returns a Discord notice."""
        with self._lock:
            meta = self.load_meta(folder_name)
            notice: DeletedPostNotice | None = None

            if meta:
                route_key = f"{meta.get('url_username')}/{meta.get('url_token')}"
                self.route_to_folder.pop(route_key, None)

                discord_info = meta.get("discord") or {}
                for msg_id in discord_info.get("bot_message_ids") or []:
                    self.message_to_folder.pop(int(msg_id), None)

                if reason is not None:
                    now_ts = int(time.time())
                    notice = self._build_notice_from_meta(
                        meta,
                        deleted_at_ts=now_ts,
                        reason=reason or format_deletion_reason(now_ts),
                    )

            self.folder_sizes.pop(folder_name, None)
            folder_path = self.data_dir / folder_name
            if folder_path.exists():
                shutil.rmtree(folder_path, ignore_errors=True)

            self.save_index()
            return notice

    def _validate_folder_integrity(self, folder_path: Path) -> tuple[bool, dict[str, Any] | None]:
        """Verifies meta.json validity and presence of all referenced media and preview files."""
        meta_path = folder_path / "meta.json"
        if not meta_path.is_file():
            return False, None

        try:
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False, None

        if not isinstance(meta, dict):
            return False, None

        files = meta.get("files")
        if not isinstance(files, list) or len(files) == 0:
            return False, meta

        if not meta.get("url_username") or not meta.get("url_token"):
            return False, meta

        for item in files:
            if not isinstance(item, dict):
                return False, meta
            filename = item.get("filename")
            if not filename:
                return False, meta

            media_file = folder_path / filename
            if not media_file.is_file() or media_file.stat().st_size == 0:
                return False, meta

            preview_rel = item.get("preview")
            if preview_rel:
                preview_file = folder_path / preview_rel
                if not preview_file.is_file() or preview_file.stat().st_size == 0:
                    return False, meta

        return True, meta

    def audit_and_rebuild_index(self) -> list[DeletedPostNotice]:
        """
        Startup integrity audit:
        removes corrupted folders, rebuilds index.json and folder size cache,
        and collects Discord edit notices for removed posts.
        """
        with self._lock:
            self.route_to_folder.clear()
            self.message_to_folder.clear()
            self.folder_sizes.clear()
            notices: list[DeletedPostNotice] = []

            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.save_index()
                return notices

            for entry in sorted(self.data_dir.iterdir(), key=lambda p: p.name):
                if not entry.is_dir():
                    continue

                is_valid, meta = self._validate_folder_integrity(entry)
                if not is_valid:
                    logger.warning("Audit: folder %s is corrupted or incomplete — removing.", entry.name)
                    now_ts = int(time.time())
                    notice = self._build_notice_from_meta(
                        meta,
                        deleted_at_ts=now_ts,
                        reason=format_deletion_reason(now_ts),
                    )
                    if notice:
                        notices.append(notice)
                    shutil.rmtree(entry, ignore_errors=True)
                    continue

                assert meta is not None
                route_key = f"{meta['url_username']}/{meta['url_token']}"
                self.route_to_folder[route_key] = entry.name
                self.folder_sizes[entry.name] = self._compute_folder_size(entry)

                discord_info = meta.get("discord") or {}
                for msg_id in discord_info.get("bot_message_ids", []):
                    self.message_to_folder[int(msg_id)] = entry.name

            self.save_index()
            self.pending_discord_notices.extend(notices)
            logger.info(
                "Audit complete: valid posts = %d, removed corrupted = %d, pending queue = %d.",
                len(self.route_to_folder),
                len(notices),
                len(self._queued_jobs),
            )
            return notices

    def ensure_free_space(self) -> list[DeletedPostNotice]:
        """
        Checks free disk space and deletes oldest folders (FIFO) until free space
        meets the configured threshold. Returns Discord notices for edited posts.
        """
        notices: list[DeletedPostNotice] = []
        if self.min_free_space_bytes <= 0:
            return notices

        with self._lock:
            while True:
                free_bytes = shutil.disk_usage(self.data_dir).free
                if free_bytes >= self.min_free_space_bytes:
                    break

                candidates = [
                    p.name
                    for p in sorted(self.data_dir.iterdir(), key=lambda item: item.name)
                    if p.is_dir() and p.name not in self.active_folders
                ]

                if not candidates:
                    logger.warning(
                        "Free space below threshold (%.2f GB), but no folders left to purge.",
                        free_bytes / (1024 ** 3),
                    )
                    break

                oldest_folder = candidates[0]
                now_ts = int(time.time())
                logger.info("FIFO cleanup: deleting oldest folder %s", oldest_folder)
                notice = self.delete_post(
                    oldest_folder,
                    reason=format_deletion_reason(now_ts),
                )
                if notice:
                    notices.append(notice)

        return notices