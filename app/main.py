from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from app.bot import XMediaDiscordBot
from app.config import load_config
from app.storage import StorageManager
from app.web import create_web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("xbot.main")


async def run_services() -> None:
    config = load_config(require_token=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)

    storage = StorageManager(
        data_dir=config.data_dir,
        min_free_space_bytes=config.min_free_space_bytes,
    )

    logger.info("Запуск стартового аудита хранилища...")
    storage.audit_and_rebuild_index()

    fastapi_app = create_web_app(config=config, storage=storage)
    uvicorn_config = uvicorn.Config(
        app=fastapi_app,
        host=config.web_host,
        port=config.web_port,
        log_level="info",
        access_log=False,
    )
    web_server = uvicorn.Server(uvicorn_config)

    bot = XMediaDiscordBot(config=config, storage=storage)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_stop_signal() -> None:
        logger.info("Получен сигнал остановки, завершаем работу...")
        stop_event.set()
        web_server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop_signal)
        except NotImplementedError:
            pass

    web_task = asyncio.create_task(web_server.serve(), name="uvicorn-web")
    bot_task = asyncio.create_task(bot.start(config.discord_token), name="discord-bot")

    done, pending = await asyncio.wait(
        [web_task, bot_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if not bot.is_closed():
        await bot.close()
    web_server.should_exit = True

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for task in done:
        exc = task.exception()
        if exc:
            raise exc


def main() -> None:
    try:
        asyncio.run(run_services())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.critical("Критическая ошибка при запуске: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()