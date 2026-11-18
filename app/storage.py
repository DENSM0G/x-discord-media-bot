from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import string
import time
from dataclasses import dataclass
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
    """Данные для редактирования сообщения в Discord после удаления файлов с диска."""
    channel_id: int
    message_id: int
    reason: str


def sanitize_username(display_name: str, user_id: int) -> str:
    """Преобразует ник пользователя Discord в безопасный сегмент URL."""
    lowered = display_name.strip().lower()
    transliterated = "".join(TRANSLIT_MAP.get(ch, ch) for ch in lowered)
    cleaned = re.sub(r"\s+", "_", transliterated)
    cleaned = re.sub(r"[^a-z0-9_-]", "", cleaned)
    cleaned = re.sub(r"[_-]{2,}", "_", cleaned).strip("_-")
    if not cleaned:
        return f"user_{user_id}"
    return cleaned[:32]


class StorageManager:
    def __init__(self, data_dir: Path, min_free_space_bytes: int) -> None:
        self.data_dir = data_dir
        self.min_free_space_bytes = min_free_space_bytes
        self.index_file = self.data_dir / "index.json"

        self.route_to_folder: dict[str, str] = {}
        self.message_to_folder: dict[int, str] = {}
        self.active_folders: set[str] = set()
        self.pending_discord_notices: list[DeletedPostNotice] = []

        self.data_dir.mkdir(parents=True, exist_ok=True)

    def allocate_post_folder(self) -> Path:
        """Создает новую директорию формата YYYY-MM-DD_HH-MM-SS_SSS и защищает её от очистки."""
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
        self.active_folders.discard(folder_name)

    def generate_unique_route(self, display_name: str, user_id: int) -> tuple[str, str]:
        """Генерирует уникальную пару (url_username, url_token)."""
        safe_user = sanitize_username(display_name, user_id)
        while True:
            token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
            route_key = f"{safe_user}/{token}"
            if route_key not in self.route_to_folder:
                return safe_user, token

    def _write_json_atomic(self, target_path: Path, payload: Any) -> None:
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target_path)

    def save_index(self) -> None:
        self._write_json_atomic(self.index_file, self.route_to_folder)

    def register_post(self, folder_path: Path, meta: dict[str, Any]) -> None:
        """Сохраняет meta.json нового поста и обновляет персистентный индекс."""
        meta_path = folder_path / "meta.json"
        self._write_json_atomic(meta_path, meta)

        route_key = f"{meta['url_username']}/{meta['url_token']}"
        folder_name = folder_path.name
        self.route_to_folder[route_key] = folder_name

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
    ) -> None:
        """Привязывает ID отправленных сообщений бота к сохраненному посту."""
        meta = self.load_meta(folder_name)
        if not meta:
            return

        meta["discord"] = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "author_id": author_id,
            "author_display_name": author_display_name,
            "bot_message_ids": bot_message_ids,
        }
        meta_path = self.data_dir / folder_name / "meta.json"
        self._write_json_atomic(meta_path, meta)

        for msg_id in bot_message_ids:
            self.message_to_folder[int(msg_id)] = folder_name

    def load_meta(self, folder_name: str) -> dict[str, Any] | None:
        meta_path = self.data_dir / folder_name / "meta.json"
        if not meta_path.is_file():
            return None
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def get_post_by_route(self, username: str, token: str) -> dict[str, Any] | None:
        route_key = f"{username}/{token}"
        folder_name = self.route_to_folder.get(route_key)
        if not folder_name:
            return None
        return self.load_meta(folder_name)

    def get_post_by_message_id(self, message_id: int) -> dict[str, Any] | None:
        folder_name = self.message_to_folder.get(message_id)
        if not folder_name:
            return None
        return self.load_meta(folder_name)

    def resolve_media_path(self, username: str, token: str, relative_path: str) -> Path | None:
        """Безопасно возвращает путь к медиафайлу, защищая от выхода за пределы папки (Path Traversal)."""
        route_key = f"{username}/{token}"
        folder_name = self.route_to_folder.get(route_key)
        if not folder_name:
            return None

        folder_root = (self.data_dir / folder_name).resolve()
        candidate = (folder_root / relative_path).resolve()

        if not candidate.is_relative_to(folder_root) or not candidate.is_file():
            return None
        return candidate

    def delete_post(self, folder_name: str, reason: str | None = None) -> DeletedPostNotice | None:
        """Удаляет папку поста с диска, чистит индексы и возвращает данные для отметки в Discord."""
        meta = self.load_meta(folder_name)
        notice: DeletedPostNotice | None = None

        if meta:
            route_key = f"{meta.get('url_username')}/{meta.get('url_token')}"
            self.route_to_folder.pop(route_key, None)

            discord_info = meta.get("discord") or {}
            msg_ids = discord_info.get("bot_message_ids") or []
            for msg_id in msg_ids:
                self.message_to_folder.pop(int(msg_id), None)

            channel_id = discord_info.get("channel_id")
            if reason and channel_id and msg_ids:
                notice = DeletedPostNotice(
                    channel_id=int(channel_id),
                    message_id=int(msg_ids[0]),
                    reason=reason,
                )

        folder_path = self.data_dir / folder_name
        if folder_path.exists():
            shutil.rmtree(folder_path, ignore_errors=True)

        self.save_index()
        return notice

    def _validate_folder_integrity(self, folder_path: Path) -> tuple[bool, dict[str, Any] | None]:
        """Проверяет валидность meta.json и наличие всех заявленных медиафайлов."""
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
            if item.get("type") == "image" and preview_rel:
                preview_file = folder_path / preview_rel
                if not preview_file.is_file() or preview_file.stat().st_size == 0:
                    return False, meta

        return True, meta

    def audit_and_rebuild_index(self) -> list[DeletedPostNotice]:
        """
        Процедура аудита при старте сервиса:
        удаляет битые папки, пересобирает index.json и формирует список сообщений Discord для правки.
        """
        self.route_to_folder.clear()
        self.message_to_folder.clear()
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
                logger.warning("Аудит: папка %s повреждена или неполна — удаляем.", entry.name)
                if meta:
                    discord_info = meta.get("discord") or {}
                    msg_ids = discord_info.get("bot_message_ids") or []
                    channel_id = discord_info.get("channel_id")
                    if channel_id and msg_ids:
                        notices.append(
                            DeletedPostNotice(
                                channel_id=int(channel_id),
                                message_id=int(msg_ids[0]),
                                reason="⚠️ Файлы удалены с сервера",
                            )
                        )
                shutil.rmtree(entry, ignore_errors=True)
                continue

            assert meta is not None
            route_key = f"{meta['url_username']}/{meta['url_token']}"
            self.route_to_folder[route_key] = entry.name

            discord_info = meta.get("discord") or {}
            for msg_id in discord_info.get("bot_message_ids", []):
                self.message_to_folder[int(msg_id)] = entry.name

        self.save_index()
        self.pending_discord_notices.extend(notices)
        logger.info(
            "Аудит завершен: валидных постов = %d, удалено поврежденных = %d.",
            len(self.route_to_folder),
            len(notices),
        )
        return notices

    def ensure_free_space(self) -> list[DeletedPostNotice]:
        """
        Проверяет остаток свободного места на диске.
        При нехватке удаляет самые старые папки (FIFO) и возвращает список уведомлений для Discord.
        """
        notices: list[DeletedPostNotice] = []
        if self.min_free_space_bytes <= 0:
            return notices

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
                    "Свободного места меньше порога (%.2f ГБ), но удалять больше нечего.",
                    free_bytes / (1024 ** 3),
                )
                break

            oldest_folder = candidates[0]
            logger.info("Очистка хранилища (FIFO): удаляем старую папку %s", oldest_folder)
            notice = self.delete_post(
                oldest_folder,
                reason="⚠️ Файлы удалены с сервера (очистка хранилища)",
            )
            if notice:
                notices.append(notice)

        return notices