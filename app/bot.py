from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from aiohttp_socks import ProxyConnector

from app.config import Config
from app.extractor import STATUS_URL_RE, MediaExtractor
from app.media_processor import MediaProcessor, PreparedAttachment
from app.storage import DeletedPostNotice, StorageManager

logger = logging.getLogger(__name__)

DELETE_EMOJI = "❌"
PENDING_EMOJI = "⏳"
DISCORD_BATCH_SIZE = 10
DEFAULT_FILESIZE_LIMIT = 20 * 1024 * 1024  # 20 МиБ


@dataclass
class DownloadJob:
    message: discord.Message
    urls: list[str]
    user_comment: str


def extract_urls_and_comment(content: str) -> tuple[list[str], str]:
    """Извлекает уникальные ссылки X/Twitter и возвращает очищенный комментарий пользователя."""
    found_urls: list[str] = []
    seen_tweet_ids: set[str] = set()

    for match in STATUS_URL_RE.finditer(content):
        tweet_id = match.group(2)
        if tweet_id not in seen_tweet_ids:
            seen_tweet_ids.add(tweet_id)
            found_urls.append(match.group(0))

    cleaned_comment = STATUS_URL_RE.sub("", content)
    # Убираем возможные остатки query-параметров, прилипших к URL
    cleaned_comment = re.sub(r"\?[A-Za-z0-9_=&%-]+", "", cleaned_comment)
    cleaned_comment = re.sub(r"\n{3,}", "\n\n", cleaned_comment).strip()
    return found_urls, cleaned_comment


class XMediaDiscordBot(discord.Client):
    def __init__(self, config: Config, storage: StorageManager) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True

        if config.proxy_url and config.proxy_url.startswith(("socks4", "socks5")):
            connector = ProxyConnector.from_url(config.proxy_url, rdns=True)
            super().__init__(intents=intents, connector=connector)
        elif config.proxy_url:
            super().__init__(intents=intents, proxy=config.proxy_url)
        else:
            super().__init__(intents=intents)

        self.config = config
        self.storage = storage
        self.extractor = MediaExtractor(
            cookies_file=config.cookies_file,
            proxy_url=config.proxy_url,
        )
        self.processor = MediaProcessor(
            temp_dir=config.temp_dir,
            video_codec=config.video_codec,
            preview_format=config.preview_format,
            preview_quality=config.preview_quality,
        )
        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._workers_started = False

    async def on_ready(self) -> None:
        logger.info("Discord-бот подключен как %s (ID: %s)", self.user, self.user.id if self.user else "?")

        if not self._workers_started:
            self._workers_started = True
            for i in range(self.config.max_concurrent_jobs):
                asyncio.create_task(self._worker_loop(i + 1))

        # Обрабатываем отложенные уведомления после стартового аудита диска и проверяем место
        startup_notices = list(self.storage.pending_discord_notices)
        self.storage.pending_discord_notices.clear()
        startup_notices.extend(self.storage.ensure_free_space())

        if startup_notices:
            await self._apply_deleted_notices(startup_notices)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.content:
            return

        urls, user_comment = extract_urls_and_comment(message.content)
        if not urls:
            return

        try:
            await message.add_reaction(PENDING_EMOJI)
        except discord.HTTPException:
            pass

        await self.queue.put(DownloadJob(message=message, urls=urls, user_comment=user_comment))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        if str(payload.emoji) != DELETE_EMOJI:
            return

        post_meta = self.storage.get_post_by_message_id(payload.message_id)
        if not post_meta:
            return

        discord_info = post_meta.get("discord") or {}
        author_id = discord_info.get("author_id")
        if author_id is None or int(author_id) != payload.user_id:
            return

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        bot_msg_ids = discord_info.get("bot_message_ids") or []
        for msg_id in bot_msg_ids:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            self.storage.message_to_folder.pop(int(msg_id), None)

        folder_name = post_meta.get("folder_name")
        if folder_name and self.config.delete_files_on_retract:
            self.storage.delete_post(folder_name, reason=None)
            logger.info("Пост %s удален по запросу автора (ID %s).", folder_name, payload.user_id)

    async def _worker_loop(self, worker_num: int) -> None:
        logger.debug("Воркер #%d запущен.", worker_num)
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job)
            except Exception as exc:
                logger.exception("Непредвиденная ошибка в воркере #%d: %s", worker_num, exc)
            finally:
                self.queue.task_done()

    async def _process_job(self, job: DownloadJob) -> None:
        message = job.message
        channel = message.channel
        author = message.author
        display_name = author.display_name

        # Освобождаем место перед началом новой загрузки
        pre_notices = self.storage.ensure_free_space()
        if pre_notices:
            await self._apply_deleted_notices(pre_notices)

        all_succeeded = True

        for idx, raw_url in enumerate(job.urls):
            folder_path: Path | None = None
            temp_attachments: list[PreparedAttachment] = []

            try:
                folder_path = self.storage.allocate_post_folder()
                extraction = await self.extractor.extract(raw_url, folder_path)
                processed_files, layout_mode = await self.processor.process_extracted_items(
                    extraction.items, folder_path
                )

                url_username, url_token = self.storage.generate_unique_route(display_name, author.id)
                meta: dict[str, Any] = {
                    "folder_name": folder_path.name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "url_username": url_username,
                    "url_token": url_token,
                    "source_url": extraction.canonical_url,
                    "tweet_author": extraction.tweet_author,
                    "tweet_text": extraction.tweet_text,
                    "discord": None,
                    "layout_mode": layout_mode,
                    "files": processed_files,
                }
                self.storage.register_post(folder_path, meta)

                # Подготовка файлов под актуальный лимит сервера Discord
                filesize_limit = (
                    message.guild.filesize_limit if message.guild else DEFAULT_FILESIZE_LIMIT
                )
                attachments, skipped = await self.processor.prepare_discord_attachments(
                    files=processed_files,
                    folder_path=folder_path,
                    max_bytes=filesize_limit,
                )
                temp_attachments = [a for a in attachments if a.is_temp]

                # Формирование Сообщения №1 (текстового со ссылкой и инструкцией)
                gallery_url = f"{self.config.public_base_url}/{url_username}/{url_token}"
                lines: list[str] = []
                if idx == 0 and job.user_comment:
                    lines.append(f"💬 **{display_name}:** {job.user_comment}")
                lines.append(gallery_url)
                if skipped:
                    lines.append(
                        f"*(Часть тяжелых файлов доступна только по ссылке выше: {', '.join(skipped)})*"
                    )
                lines.append(
                    f"Для удаления ответа от бота, {display_name} может нажать на эмодзи {DELETE_EMOJI}."
                )

                first_msg = await channel.send("\n".join(lines))
                sent_message_ids: list[int] = [first_msg.id]

                # Отправка медиафайлов батчами по 10 штук
                for batch_start in range(0, len(attachments), DISCORD_BATCH_SIZE):
                    batch = attachments[batch_start : batch_start + DISCORD_BATCH_SIZE]
                    discord_files = [
                        discord.File(fp=str(item.path), filename=item.filename)
                        for item in batch
                    ]
                    media_msg = await channel.send(files=discord_files)
                    sent_message_ids.append(media_msg.id)

                # Привязываем ID сообщений бота к meta.json
                self.storage.attach_discord_messages(
                    folder_name=folder_path.name,
                    guild_id=message.guild.id if message.guild else None,
                    channel_id=channel.id,
                    author_id=author.id,
                    author_display_name=display_name,
                    bot_message_ids=sent_message_ids,
                )

                if self.config.add_delete_reaction:
                    try:
                        await first_msg.add_reaction(DELETE_EMOJI)
                    except discord.HTTPException:
                        pass

            except Exception as exc:
                all_succeeded = False
                logger.error("Ошибка при обработке %s: %s", raw_url, exc)
                if folder_path:
                    self.storage.delete_post(folder_path.name, reason=None)

                try:
                    err_msg = await channel.send(
                        f"⚠️ Не удалось загрузить медиа по ссылке <{raw_url}>: `{exc}`"
                    )
                    await asyncio.sleep(15)
                    await err_msg.delete()
                except discord.HTTPException:
                    pass
            finally:
                for tmp in temp_attachments:
                    tmp.path.unlink(missing_ok=True)
                if folder_path:
                    self.storage.release_active_folder(folder_path.name)

        # Проверяем свободное место после сохранения файлов
        post_notices = self.storage.ensure_free_space()
        if post_notices:
            await self._apply_deleted_notices(post_notices)

        # Удаляем исходное сообщение пользователя или снимаем индикатор ⏳
        if all_succeeded and self.config.delete_original_message:
            try:
                await message.delete()
                return
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        if self.user:
            try:
                await message.remove_reaction(PENDING_EMOJI, self.user)
            except discord.HTTPException:
                pass

    async def _apply_deleted_notices(self, notices: list[DeletedPostNotice]) -> None:
        """Редактирует старые сообщения бота в Discord, добавляя пометку об удалении файлов."""
        for notice in notices:
            try:
                channel = self.get_channel(notice.channel_id) or await self.fetch_channel(
                    notice.channel_id
                )
                if not isinstance(channel, discord.abc.Messageable):
                    continue

                msg = await channel.fetch_message(notice.message_id)
                if notice.reason not in msg.content:
                    await msg.edit(content=f"{msg.content}\n{notice.reason}")

                if self.user:
                    try:
                        await msg.remove_reaction(DELETE_EMOJI, self.user)
                    except discord.HTTPException:
                        pass
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.debug(
                    "Не удалось обновить сообщение %s в канале %s: %s",
                    notice.message_id,
                    notice.channel_id,
                    exc,
                )