from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from app.config import Config
from app.extractor import (
    ExtractionError,
    ExtractedPost,
    PostSizeLimitError,
    analyze_user_message,
    extract_post,
)
from app.media_processor import (
    PreparedAttachment,
    prepare_discord_attachments,
    process_downloaded_media,
    wait_for_available_ram,
)
from app.storage import DeletedPostNotice, QueuedJob, StorageManager

logger = logging.getLogger(__name__)

WEBHOOK_NAME = "xBot-Relay"
SEPARATOR_LINE = "────────────────────"
DELETE_EMOJI = "❌"
LIKE_EMOJI = "🩶"
PENDING_EMOJI = "⏳"
DISCORD_MAX_CONTENT = 2000
DELETION_MARKER_RESERVE = 60
MAX_ATTACHMENTS_PER_MESSAGE = 10


def _sanitize_webhook_username(display_name: str) -> str:
    """Ensures webhook username obeys Discord's naming rules (1..80 chars, no forbidden words)."""
    cleaned = re.sub(r"\s+", " ", (display_name or "").strip())
    if not cleaned:
        return "User"
    cleaned = re.sub(r"discord", "d\u200biscord", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"clyde", "c\u200blyde", cleaned, flags=re.IGNORECASE)
    if cleaned.lower() in {"everyone", "here"}:
        cleaned = f"{cleaned}_"
    return cleaned[:80]


def _format_number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_stats_line(stats: dict[str, int | None]) -> str:
    parts: list[str] = []
    if stats.get("likes") is not None:
        parts.append(f"❤️ {_format_number(int(stats['likes']))}")
    if stats.get("retweets") is not None:
        parts.append(f"🔁 {_format_number(int(stats['retweets']))}")
    if stats.get("replies") is not None:
        parts.append(f"💬 {_format_number(int(stats['replies']))}")
    if stats.get("views") is not None:
        parts.append(f"👁️ {_format_number(int(stats['views']))}")
    return "  ".join(parts)


def _escape_markdown_inline(text: str) -> str:
    return text.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")


def _build_first_message_content(
    extracted: ExtractedPost,
    gallery_url: str,
    user_comment: str,
    link_index: int,
    total_links: int,
    has_skipped_large_media: bool,
) -> str:
    """Builds the TZ 4.2 first Discord message while reserving room for a future FIFO deletion marker."""
    effective_limit = DISCORD_MAX_CONTENT - DELETION_MARKER_RESERVE

    header_lines: list[str] = []
    if total_links > 1:
        header_lines.append(f"**{link_index}/{total_links}**")
    if user_comment:
        header_lines.append(user_comment)

    header_lines.append(SEPARATOR_LINE)
    header_lines.append(
        f"-# [View & download originals](<{gallery_url}>) • [Original post on X](<{extracted.original_url}>)"
    )
    header_lines.append(f"-# Author can delete this post by reacting with {DELETE_EMOJI}")
    header_lines.append(SEPARATOR_LINE)

    author_name_esc = _escape_markdown_inline(extracted.author_name)
    if extracted.author_handle:
        author_line = f"**{author_name_esc}** (`@{extracted.author_handle}`)"
    else:
        author_line = f"**{author_name_esc}**"
    header_lines.append(author_line)

    if extracted.quoted_media:
        if extracted.quoted_author_handle:
            header_lines.append(f"-# ↪ Quoted media from `@{extracted.quoted_author_handle}`")
        elif extracted.quoted_author_name:
            header_lines.append(f"-# ↪ Quoted media from {_escape_markdown_inline(extracted.quoted_author_name)}")
        else:
            header_lines.append("-# ↪ Media from quoted post")

    footer_lines: list[str] = []
    stats_line = _format_stats_line(extracted.stats.to_dict())
    if stats_line:
        footer_lines.append(stats_line)
    footer_lines.append(f"-# Stats fetched: <t:{extracted.stats_fetched_at_ts}:f>")
    if has_skipped_large_media:
        footer_lines.append(
            "-# ⚠️ Video is too large for Discord attachment — watch in original quality via the link above"
        )

    header_block = "\n".join(header_lines)
    footer_block = "\n".join(footer_lines)
    fixed_len = len(header_block) + len(footer_block) + 4  # spacing newlines

    if fixed_len > effective_limit - 40 and user_comment:
        overflow = fixed_len - (effective_limit - 40)
        trimmed_comment = user_comment[: max(0, len(user_comment) - overflow - 3)].rstrip() + "…"
        if total_links > 1:
            header_lines[1] = trimmed_comment
        else:
            header_lines[0] = trimmed_comment
        header_block = "\n".join(header_lines)
        fixed_len = len(header_block) + len(footer_block) + 4

    post_text = extracted.post_text.strip()
    body_block = ""
    if post_text:
        max_body = min(1350, effective_limit - fixed_len)
        if max_body > 40:
            if len(post_text) > max_body:
                suffix = "… [full text on page]"
                cut_at = max(1, max_body - len(suffix))
                body_block = post_text[:cut_at].rstrip() + suffix
            else:
                body_block = post_text

    sections = [header_block]
    if body_block:
        sections.append(body_block)
    sections.append(footer_block)

    result = "\n".join(sections)
    if len(result) > effective_limit:
        result = result[: effective_limit - 1] + "…"
    return result


def _group_attachments_into_batches(
    attachments: list[PreparedAttachment],
    filesize_limit: int,
) -> list[list[PreparedAttachment]]:
    """Groups sendable files into batches of <= 10 files and safe total payload size."""
    valid = [att for att in attachments if att.send_path is not None and att.send_path.is_file()]
    if not valid:
        return []

    max_batch_bytes = max(filesize_limit, 24 * 1024 * 1024)
    batches: list[list[PreparedAttachment]] = []
    current: list[PreparedAttachment] = []
    current_bytes = 0

    for att in valid:
        if current and (
            len(current) >= MAX_ATTACHMENTS_PER_MESSAGE
            or (current_bytes + att.size_bytes > max_batch_bytes)
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(att)
        current_bytes += att.size_bytes

    if current:
        batches.append(current)
    return batches


def _inject_deletion_marker(content: str, marker: str, gallery_url: str | None) -> str:
    """Inserts `deleted from bot server:` <t:...:f> right after the gallery hyperlink."""
    if "`deleted from bot server:`" in content:
        return content

    updated: str | None = None
    if gallery_url:
        escaped_url = re.escape(gallery_url)
        pattern = re.compile(rf"(\[[^\]]+\]\(<?{escaped_url}>?\))")
        if pattern.search(content):
            updated = pattern.sub(rf"\1 {marker}", content, count=1)

    if updated is None:
        generic_pattern = re.compile(r"(\[View & download originals\]\([^)]+\))")
        if generic_pattern.search(content):
            updated = generic_pattern.sub(rf"\1 {marker}", content, count=1)

    if updated is None:
        updated = f"{content}\n-# {marker}"

    if len(updated) > DISCORD_MAX_CONTENT:
        updated = updated[: DISCORD_MAX_CONTENT - 1] + "…"
    return updated


class XMediaBot(discord.Client):
    def __init__(
        self,
        config: Config,
        storage: StorageManager,
        *,
        connector: Any | None = None,
        proxy: str | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True

        client_kwargs: dict[str, Any] = {"intents": intents}
        if connector is not None:
            client_kwargs["connector"] = connector
        elif proxy is not None:
            client_kwargs["proxy"] = proxy

        super().__init__(**client_kwargs)
        self.config = config
        self.storage = storage

        self._global_semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._webhook_cache: dict[int, discord.Webhook] = {}
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._enqueued_message_ids: set[int] = set()
        self._startup_done = False

    def _get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    def _spawn_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    def _schedule_deleted_notices(self, notices: list[DeletedPostNotice]) -> None:
        if notices:
            self._spawn_task(self.apply_deleted_notices(notices))

    async def on_ready(self) -> None:
        logger.info("Discord bot connected as %s (id=%s)", self.user, self.user.id if self.user else "?")

        if not self._startup_done:
            self._startup_done = True
            if self.storage.pending_discord_notices:
                pending = list(self.storage.pending_discord_notices)
                self.storage.pending_discord_notices.clear()
                self._schedule_deleted_notices(pending)

            restored_jobs = self.storage.list_queued_jobs()
            if restored_jobs:
                logger.info("Resuming %d persisted job(s) from queue.json...", len(restored_jobs))
                for job in restored_jobs:
                    if job.message_id not in self._enqueued_message_ids:
                        self._enqueued_message_ids.add(job.message_id)
                        self._spawn_task(self._process_queued_job(job))

    # ------------------------------------------------------------------
    # Webhook helpers
    # ------------------------------------------------------------------

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        ch = self.get_channel(channel_id)
        if ch is not None:
            return ch  # type: ignore[return-value]
        try:
            fetched = await self.fetch_channel(channel_id)
            return fetched  # type: ignore[return-value]
        except discord.DiscordException as exc:
            logger.warning("Cannot resolve Discord channel %d: %s", channel_id, exc)
            return None

    async def _get_or_create_webhook(
        self,
        channel: discord.abc.Messageable,
    ) -> tuple[discord.Webhook | None, discord.Thread | None]:
        """Returns (webhook, thread_or_None) for TextChannel/Forum Thread, or (None, None) if unavailable."""
        thread: discord.Thread | None = None
        parent: Any = channel

        if isinstance(channel, discord.Thread):
            thread = channel
            parent = channel.parent
            if parent is None and channel.parent_id is not None:
                parent = await self._resolve_channel(channel.parent_id)

        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            return None, None

        cached = self._webhook_cache.get(parent.id)
        if cached is not None:
            return cached, thread

        try:
            existing = await parent.webhooks()
            for wh in existing:
                if wh.name == WEBHOOK_NAME and (self.user is None or wh.user is None or wh.user.id == self.user.id):
                    self._webhook_cache[parent.id] = wh
                    return wh, thread

            created = await parent.create_webhook(name=WEBHOOK_NAME, reason="Relay X media as sender")
            self._webhook_cache[parent.id] = created
            return created, thread
        except discord.Forbidden:
            logger.warning(
                "Missing 'Manage Webhooks' permission in #%s (%d) — falling back to regular bot messages.",
                getattr(parent, "name", parent.id),
                parent.id,
            )
            return None, None
        except discord.DiscordException as exc:
            logger.warning("Failed to get/create webhook in channel %d: %s", parent.id, exc)
            return None, None

    async def _send_batch_message(
        self,
        channel: discord.abc.Messageable,
        webhook: discord.Webhook | None,
        thread: discord.Thread | None,
        *,
        content: str | None,
        attachments: list[PreparedAttachment],
        username: str,
        avatar_url: str | None,
    ) -> list[int]:
        """
        Sends one message (or recursively splits attachments if Discord returns 413 Payload Too Large).
        Returns list of sent Discord message IDs.
        """
        files = [
            discord.File(str(att.send_path), filename=att.send_path.name)
            for att in attachments
            if att.send_path is not None and att.send_path.is_file()
        ]

        try:
            if webhook is not None:
                send_kwargs: dict[str, Any] = {
                    "wait": True,
                    "username": username,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if avatar_url:
                    send_kwargs["avatar_url"] = avatar_url
                if content:
                    send_kwargs["content"] = content
                if files:
                    send_kwargs["files"] = files
                if thread is not None:
                    send_kwargs["thread"] = thread
                sent = await webhook.send(**send_kwargs)
                return [int(sent.id)]

            send_kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
            if content:
                send_kwargs["content"] = content
            if files:
                send_kwargs["files"] = files
            sent = await channel.send(**send_kwargs)
            return [int(sent.id)]

        except discord.NotFound:
            if webhook is not None and webhook.channel_id:
                self._webhook_cache.pop(webhook.channel_id, None)
            if webhook is not None:
                fresh_wh, fresh_thread = await self._get_or_create_webhook(channel)
                if fresh_wh is not None:
                    return await self._send_batch_message(
                        channel,
                        fresh_wh,
                        fresh_thread,
                        content=content,
                        attachments=attachments,
                        username=username,
                        avatar_url=avatar_url,
                    )
            raise

        except discord.HTTPException as exc:
            if exc.status == 413 and len(attachments) > 1:
                mid = len(attachments) // 2
                logger.warning(
                    "HTTP 413 on batch of %d files — splitting into %d + %d.",
                    len(attachments),
                    mid,
                    len(attachments) - mid,
                )
                first_ids = await self._send_batch_message(
                    channel,
                    webhook,
                    thread,
                    content=content,
                    attachments=attachments[:mid],
                    username=username,
                    avatar_url=avatar_url,
                )
                second_ids = await self._send_batch_message(
                    channel,
                    webhook,
                    thread,
                    content=None,
                    attachments=attachments[mid:],
                    username=username,
                    avatar_url=avatar_url,
                )
                return first_ids + second_ids
            raise

    async def _notify_limit_exceeded(
        self,
        channel: discord.abc.Messageable,
        orig_message: discord.Message | None,
        notice_text: str,
    ) -> None:
        if orig_message is not None:
            try:
                await orig_message.reply(
                    notice_text,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except discord.DiscordException:
                pass
        try:
            await channel.send(notice_text, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException as exc:
            logger.warning("Failed to send size-limit warning to channel: %s", exc)

    # ------------------------------------------------------------------
    # Incoming message handling & job execution
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        if not message.content:
            return

        # Access control: DMs & guild whitelist
        if message.guild is None:
            if not self.config.allow_dms:
                return
        elif self.config.allowed_guild_ids and message.guild.id not in self.config.allowed_guild_ids:
            return

        analysis = analyze_user_message(message.content, self.config.max_comment_length)
        if not analysis.x_urls:
            return

        if analysis.exceeds_limit:
            logger.debug(
                "Ignoring message %d: text without URLs is %d chars (> %d limit).",
                message.id,
                analysis.text_length_without_urls,
                self.config.max_comment_length,
            )
            return

        if message.id in self._enqueued_message_ids:
            return
        self._enqueued_message_ids.add(message.id)

        try:
            await message.add_reaction(PENDING_EMOJI)
        except discord.DiscordException:
            pass

        parent_channel_id: int | None = None
        if isinstance(message.channel, discord.Thread):
            parent_channel_id = message.channel.parent_id

        job = QueuedJob(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            author_id=message.author.id,
            author_display_name=message.author.display_name,
            author_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
            urls=analysis.x_urls,
            user_comment=analysis.user_comment,
            parent_channel_id=parent_channel_id,
            next_url_index=0,
        )
        self.storage.enqueue_job(job)
        self._spawn_task(self._process_queued_job(job))

    async def _process_queued_job(self, job: QueuedJob) -> None:
        channel_lock = self._get_channel_lock(job.channel_id)
        try:
            async with channel_lock:
                async with self._global_semaphore:
                    await self._execute_job(job)
        except Exception:
            logger.exception("Unexpected error while processing job for message %d", job.message_id)
            self.storage.dequeue_job(job.message_id)
        finally:
            self._enqueued_message_ids.discard(job.message_id)

    async def _execute_job(self, job: QueuedJob) -> None:
        # Enforce access control on persisted jobs restored after config changes
        if job.guild_id is None:
            if not self.config.allow_dms:
                self.storage.dequeue_job(job.message_id)
                return
        elif self.config.allowed_guild_ids and job.guild_id not in self.config.allowed_guild_ids:
            self.storage.dequeue_job(job.message_id)
            return

        channel = await self._resolve_channel(job.channel_id)
        if channel is None:
            logger.warning("Dropping queued job %d: channel %d no longer accessible.", job.message_id, job.channel_id)
            self.storage.dequeue_job(job.message_id)
            return

        orig_message: discord.Message | None = None
        try:
            orig_message = await channel.fetch_message(job.message_id)
        except discord.NotFound:
            logger.info("Original message %d was already deleted before processing — skipping.", job.message_id)
            self.storage.dequeue_job(job.message_id)
            return
        except discord.DiscordException:
            orig_message = None

        guild: discord.Guild | None = getattr(channel, "guild", None)
        if guild is None and job.guild_id is not None:
            guild = self.get_guild(job.guild_id)

        filesize_limit = guild.filesize_limit if guild is not None else (10 * 1024 * 1024)

        display_name = job.author_display_name
        avatar_url = job.author_avatar_url
        if orig_message is not None:
            display_name = orig_message.author.display_name
            if orig_message.author.display_avatar:
                avatar_url = str(orig_message.author.display_avatar.url)
        elif guild is not None:
            member = guild.get_member(job.author_id)
            if member is not None:
                display_name = member.display_name
                if member.display_avatar:
                    avatar_url = str(member.display_avatar.url)

        webhook_username = _sanitize_webhook_username(display_name)
        webhook, thread = await self._get_or_create_webhook(channel)

        total_links = len(job.urls)
        succeeded_any = False
        all_succeeded = True

        for idx in range(job.next_url_index, total_links):
            url = job.urls[idx]
            ok = await self._process_single_url(
                job=job,
                url=url,
                link_index=idx + 1,
                total_links=total_links,
                channel=channel,
                guild=guild,
                orig_message=orig_message,
                webhook=webhook,
                thread=thread,
                webhook_username=webhook_username,
                display_name=display_name,
                avatar_url=avatar_url,
                filesize_limit=filesize_limit,
            )
            if ok:
                succeeded_any = True
                self.storage.update_job_progress(job.message_id, idx + 1)
            else:
                all_succeeded = False

        if succeeded_any and all_succeeded and self.config.delete_original_message:
            if orig_message is not None:
                try:
                    await orig_message.delete()
                except discord.DiscordException as exc:
                    logger.warning("Could not delete original message %d: %s", job.message_id, exc)
        elif orig_message is not None and self.user is not None:
            try:
                await orig_message.remove_reaction(PENDING_EMOJI, self.user)
            except discord.DiscordException:
                pass

        self.storage.dequeue_job(job.message_id)

    async def _process_single_url(
        self,
        *,
        job: QueuedJob,
        url: str,
        link_index: int,
        total_links: int,
        channel: discord.abc.Messageable,
        guild: discord.Guild | None,
        orig_message: discord.Message | None,
        webhook: discord.Webhook | None,
        thread: discord.Thread | None,
        webhook_username: str,
        display_name: str,
        avatar_url: str | None,
        filesize_limit: int,
    ) -> bool:
        folder_path: Path | None = None
        temp_job_dir: Path | None = None
        registered = False

        try:
            await wait_for_available_ram(self.config.min_free_ram_bytes)

            folder_path = self.storage.allocate_post_folder()
            temp_job_dir = self.config.temp_dir / folder_path.name

            extracted = await extract_post(
                url,
                folder_path,
                self.config,
                storage=self.storage,
                on_fifo_notices=self._schedule_deleted_notices,
            )
            files_meta = await process_downloaded_media(
                folder_path=folder_path,
                raw_files=extracted.files,
                config=self.config,
                gif_filenames=extracted.gif_filenames,
            )
            if not files_meta:
                raise ExtractionError("No valid media files left after probing.")

            url_username, url_token = self.storage.generate_unique_route(display_name, job.author_id)
            gallery_url = f"{self.config.public_base_url}/{url_username}/{url_token}"
            created_ts = int(time.time())

            meta: dict[str, Any] = {
                "version": 4,
                "folder_name": folder_path.name,
                "url_username": url_username,
                "url_token": url_token,
                "created_at": datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat(),
                "created_at_ts": created_ts,
                "original_url": extracted.original_url,
                "tweet_id": extracted.tweet_id,
                "author_name": extracted.author_name,
                "author_handle": extracted.author_handle,
                "post_text": extracted.post_text,
                "stats": extracted.stats.to_dict(),
                "stats_fetched_at_ts": extracted.stats_fetched_at_ts,
                "quoted_media": extracted.quoted_media,
                "quoted_author_name": extracted.quoted_author_name,
                "quoted_author_handle": extracted.quoted_author_handle,
                "quoted_url": extracted.quoted_url,
                "extractor_used": extracted.extractor_used,
                "user_comment": job.user_comment if link_index == 1 else "",
                "files": files_meta,
            }
            self.storage.register_post(folder_path, meta)
            registered = True

            prepared = await prepare_discord_attachments(
                folder_path=folder_path,
                files_meta=files_meta,
                max_file_size_bytes=filesize_limit,
                config=self.config,
                temp_job_dir=temp_job_dir,
            )

            has_skipped_large = any(att.skipped_too_large for att in prepared)
            batches = _group_attachments_into_batches(prepared, filesize_limit)

            first_content = _build_first_message_content(
                extracted=extracted,
                gallery_url=gallery_url,
                user_comment=job.user_comment if link_index == 1 else "",
                link_index=link_index,
                total_links=total_links,
                has_skipped_large_media=has_skipped_large,
            )

            sent_message_ids: list[int] = []
            first_batch = batches[0] if batches else []
            first_ids = await self._send_batch_message(
                channel,
                webhook,
                thread,
                content=first_content,
                attachments=first_batch,
                username=webhook_username,
                avatar_url=avatar_url,
            )
            sent_message_ids.extend(first_ids)

            if sent_message_ids:
                first_msg_id = sent_message_ids[0]
                partial = channel.get_partial_message(first_msg_id)
                if self.config.add_delete_reaction:
                    try:
                        await partial.add_reaction(DELETE_EMOJI)
                    except discord.DiscordException as exc:
                        logger.warning("Failed to add %s reaction: %s", DELETE_EMOJI, exc)
                if self.config.add_like_reaction:
                    try:
                        await partial.add_reaction(LIKE_EMOJI)
                    except discord.DiscordException as exc:
                        logger.warning("Failed to add %s reaction: %s", LIKE_EMOJI, exc)

            for extra_batch in batches[1:]:
                extra_ids = await self._send_batch_message(
                    channel,
                    webhook,
                    thread,
                    content=None,
                    attachments=extra_batch,
                    username=webhook_username,
                    avatar_url=avatar_url,
                )
                sent_message_ids.extend(extra_ids)

            self.storage.attach_discord_messages(
                folder_name=folder_path.name,
                guild_id=guild.id if guild else job.guild_id,
                channel_id=job.channel_id,
                parent_channel_id=job.parent_channel_id,
                webhook_id=webhook.id if webhook else None,
                author_id=job.author_id,
                author_display_name=display_name,
                bot_message_ids=sent_message_ids,
                gallery_url=gallery_url,
            )

            self.storage.release_active_folder(folder_path.name)
            notices = await asyncio.to_thread(self.storage.ensure_free_space)
            self._schedule_deleted_notices(notices)

            return True

        except PostSizeLimitError as exc:
            logger.warning("Aborted %s due to storage limit: %s", url, exc.user_message)
            if folder_path is not None:
                if registered:
                    self.storage.delete_post(folder_path.name, reason=None)
                else:
                    shutil.rmtree(folder_path, ignore_errors=True)
            prefix = f"({link_index}/{total_links}) " if total_links > 1 else ""
            await self._notify_limit_exceeded(channel, orig_message, f"{prefix}{exc.user_message}")
            return False

        except ExtractionError as exc:
            logger.warning("Extraction failed for %s: %s", url, exc)
            if folder_path is not None:
                if registered:
                    self.storage.delete_post(folder_path.name, reason=None)
                else:
                    shutil.rmtree(folder_path, ignore_errors=True)
            return False

        except Exception:
            logger.exception("Failed processing URL %s for message %d", url, job.message_id)
            if folder_path is not None and not registered:
                shutil.rmtree(folder_path, ignore_errors=True)
            return False

        finally:
            if folder_path is not None:
                self.storage.release_active_folder(folder_path.name)
            if temp_job_dir is not None and temp_job_dir.exists():
                shutil.rmtree(temp_job_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Self-moderation via ❌ reaction
    # ------------------------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        if str(payload.emoji) != DELETE_EMOJI:
            return

        meta = self.storage.get_post_by_message_id(payload.message_id)
        if not meta:
            return

        discord_info = meta.get("discord") or {}
        author_id = discord_info.get("author_id")
        is_author = author_id is not None and int(author_id) == int(payload.user_id)

        is_moderator = False
        if not is_author and payload.guild_id is not None:
            guild = self.get_guild(payload.guild_id)
            if guild is not None:
                member = payload.member or guild.get_member(payload.user_id)
                if member is not None:
                    channel = guild.get_channel_or_thread(payload.channel_id)
                    if channel is not None and channel.permissions_for(member).manage_messages:
                        is_moderator = True

        if not is_author and not is_moderator:
            return

        channel = await self._resolve_channel(payload.channel_id)
        bot_message_ids: list[int] = [int(mid) for mid in (discord_info.get("bot_message_ids") or [])]
        webhook_id = discord_info.get("webhook_id")

        webhook: discord.Webhook | None = None
        thread: discord.Thread | None = None
        if channel is not None and webhook_id is not None:
            webhook, thread = await self._get_or_create_webhook(channel)
            if webhook is not None and webhook.id != int(webhook_id):
                try:
                    webhook = await self.fetch_webhook(int(webhook_id))
                except discord.DiscordException:
                    webhook = None

        for msg_id in bot_message_ids:
            deleted = False
            if webhook is not None:
                try:
                    if thread is not None:
                        await webhook.delete_message(msg_id, thread=thread)
                    else:
                        await webhook.delete_message(msg_id)
                    deleted = True
                except discord.NotFound:
                    deleted = True
                except discord.DiscordException:
                    deleted = False

            if not deleted and channel is not None:
                try:
                    await channel.get_partial_message(msg_id).delete()
                except discord.DiscordException as exc:
                    logger.warning("Could not delete message %d on retract: %s", msg_id, exc)

        folder_name = meta.get("folder_name")
        if folder_name and self.config.delete_files_on_retract:
            self.storage.delete_post(str(folder_name), reason=None)
            logger.info("Retracted post %s by user %d.", folder_name, payload.user_id)

    # ------------------------------------------------------------------
    # FIFO cleanup message editing (`deleted from bot server: <t:...:f>`)
    # ------------------------------------------------------------------

    async def apply_deleted_notices(self, notices: list[DeletedPostNotice]) -> None:
        for notice in notices:
            try:
                channel = await self._resolve_channel(notice.channel_id)
                if channel is None:
                    continue

                try:
                    msg = await channel.fetch_message(notice.message_id)
                except discord.DiscordException:
                    continue

                new_content = _inject_deletion_marker(
                    msg.content,
                    notice.discord_marker,
                    notice.gallery_url,
                )
                if new_content == msg.content:
                    continue

                if notice.webhook_id is not None or msg.webhook_id is not None:
                    wh_id = notice.webhook_id or msg.webhook_id
                    assert wh_id is not None
                    thread: discord.Thread | None = channel if isinstance(channel, discord.Thread) else None
                    try:
                        webhook = await self.fetch_webhook(int(wh_id))
                        if thread is not None:
                            await webhook.edit_message(notice.message_id, content=new_content, thread=thread)
                        else:
                            await webhook.edit_message(notice.message_id, content=new_content)
                    except discord.DiscordException as exc:
                        logger.warning("Webhook edit failed for message %d: %s", notice.message_id, exc)
                    continue

                await msg.edit(content=new_content)
            except discord.DiscordException as exc:
                logger.warning("Could not edit deleted-post notice on message %d: %s", notice.message_id, exc)