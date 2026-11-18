from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    discord_token: str
    public_base_url: str
    web_host: str
    web_port: int
    proxy_url: str | None

    base_dir: Path
    data_dir: Path
    temp_dir: Path
    cookies_file: Path
    web_dir: Path

    min_free_space_gb: float
    video_codec: str
    preview_format: str
    preview_quality: int
    max_concurrent_jobs: int

    delete_original_message: bool
    add_delete_reaction: bool
    delete_files_on_retract: bool

    @property
    def min_free_space_bytes(self) -> int:
        return int(self.min_free_space_gb * (1024 ** 3))


def load_config(env_path: Path | str | None = None, require_token: bool = True) -> Config:
    base_dir = Path(__file__).resolve().parent.parent
    dotenv_target = Path(env_path) if env_path else (base_dir / ".env")
    load_dotenv(dotenv_target)

    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    if require_token and (not discord_token or discord_token == "your_discord_bot_token_here"):
        raise ValueError("Не задан DISCORD_TOKEN в файле .env")

    public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").strip().rstrip("/")
    web_host = os.getenv("WEB_HOST", "0.0.0.0").strip()
    web_port = int(os.getenv("WEB_PORT", "8080"))

    raw_proxy = os.getenv("PROXY_URL", "").strip()
    proxy_url = raw_proxy if raw_proxy else None

    raw_data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir = raw_data_dir if raw_data_dir.is_absolute() else (base_dir / raw_data_dir).resolve()

    raw_temp_dir = Path(os.getenv("TEMP_DIR", "/tmp/xbot_compress"))
    temp_dir = raw_temp_dir if raw_temp_dir.is_absolute() else (base_dir / raw_temp_dir).resolve()

    cookies_file = (base_dir / "config" / "cookies.txt").resolve()
    web_dir = (base_dir / "web").resolve()

    return Config(
        discord_token=discord_token,
        public_base_url=public_base_url,
        web_host=web_host,
        web_port=web_port,
        proxy_url=proxy_url,
        base_dir=base_dir,
        data_dir=data_dir,
        temp_dir=temp_dir,
        cookies_file=cookies_file,
        web_dir=web_dir,
        min_free_space_gb=float(os.getenv("MIN_FREE_SPACE_GB", "5.0")),
        video_codec=os.getenv("VIDEO_CODEC", "libx265").strip(),
        preview_format=os.getenv("PREVIEW_FORMAT", "webp").strip().lower(),
        preview_quality=int(os.getenv("PREVIEW_QUALITY", "90")),
        max_concurrent_jobs=max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2"))),
        delete_original_message=_parse_bool(os.getenv("DELETE_ORIGINAL_MESSAGE"), True),
        add_delete_reaction=_parse_bool(os.getenv("ADD_DELETE_REACTION"), True),
        delete_files_on_retract=_parse_bool(os.getenv("DELETE_FILES_ON_RETRACT"), True),
    )