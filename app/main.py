from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import uvicorn

from app.bot import XMediaBot
from app.config import Config, load_config
from app.storage import StorageManager
from app.web import create_web_app


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _create_discord_bot(config: Config, storage: StorageManager) -> XMediaBot:
    connector = None
    http_proxy: str | None = None

    if config.proxy_url:
        if config.proxy_url.lower().startswith(("socks4://", "socks5://", "socks5h://")):
            from aiohttp_socks import ProxyConnector

            normalized = config.proxy_url.replace("socks5h://", "socks5://", 1)
            connector = ProxyConnector.from_url(normalized, rdns=True)
            logging.getLogger(__name__).info("Discord client using SOCKS proxy: %s", config.proxy_url)
        else:
            http_proxy = config.proxy_url
            logging.getLogger(__name__).info("Discord client using HTTP proxy: %s", config.proxy_url)

    return XMediaBot(config=config, storage=storage, connector=connector, proxy=http_proxy)


async def _run_services() -> None:
    logger = logging.getLogger("app.main")
    config = load_config()

    if config.temp_dir.exists():
        shutil.rmtree(config.temp_dir, ignore_errors=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)

    storage = StorageManager(
        data_dir=config.data_dir,
        min_free_space_bytes=config.min_free_space_bytes,
    )
    storage.audit_and_rebuild_index()

    web_app = create_web_app(config=config, storage=storage)
    uvicorn_config = uvicorn.Config(
        app=web_app,
        host=config.web_host,
        port=config.web_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )
    web_server = uvicorn.Server(uvicorn_config)

    bot = _create_discord_bot(config, storage)

    async def _run_bot() -> None:
        try:
            await bot.start(config.discord_token)
        finally:
            if not bot.is_closed():
                await bot.close()

    logger.info(
        "Starting services (web=%s:%d, codec=%s, preset=%s, view=%s, max_jobs=%d, max_post=%.0f%%, dms=%s)...",
        config.web_host,
        config.web_port,
        config.video_codec,
        config.video_preset,
        config.default_view_mode,
        config.max_concurrent_jobs,
        config.max_post_disk_percent,
        config.allow_dms,
    )

    web_task = asyncio.create_task(web_server.serve(), name="uvicorn")
    bot_task = asyncio.create_task(_run_bot(), name="discord-bot")

    try:
        done, pending = await asyncio.wait(
            {web_task, bot_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                logger.error("Service '%s' crashed: %s", task.get_name(), exc)
        web_server.should_exit = True
        if not bot.is_closed():
            await bot.close()
        for task in pending:
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                task.cancel()
    except asyncio.CancelledError:
        web_server.should_exit = True
        if not bot.is_closed():
            await bot.close()
        raise


def main() -> None:
    _setup_logging()
    try:
        asyncio.run(_run_services())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()