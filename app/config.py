from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

VALID_VIEW_MODES: tuple[str, ...] = ("carousel", "comic", "masonry", "grid")
SUPPORTED_VIDEO_CODECS: tuple[str, ...] = ("libsvtav1", "libvpx-vp9", "libx265")
DEFAULT_VIDEO_PRESETS: dict[str, str] = {
    "libsvtav1": "4",
    "libvpx-vp9": "1",
    "libx265": "slower",
}
DEFAULT_TRUSTED_PROXY_IPS: tuple[str, ...] = ("127.0.0.1", "::1")
SENSITIVE_SUBPROCESS_ENV_KEYS: frozenset[str] = frozenset({"DISCORD_TOKEN"})


def get_sanitized_subprocess_env() -> dict[str, str]:
    """Returns a copy of os.environ with bot secrets stripped for external CLI processes."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in SENSITIVE_SUBPROCESS_ENV_KEYS
    }


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int, minimum: int | None = None) -> int:
    if value is None or not value.strip():
        result = default
    else:
        try:
            result = int(value.strip())
        except ValueError:
            logger.warning("Invalid integer value '%s', falling back to %d", value, default)
            result = default
    if minimum is not None:
        return max(minimum, result)
    return result


def _parse_float(
    value: str | None,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if value is None or not value.strip():
        result = default
    else:
        try:
            result = float(value.strip())
        except ValueError:
            logger.warning("Invalid float value '%s', falling back to %.2f", value, default)
            result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _parse_int_set(value: str | None) -> frozenset[int]:
    if not value or not value.strip():
        return frozenset()
    result: set[int] = set()
    for token in value.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            result.add(int(cleaned))
        except ValueError:
            logger.warning("Ignoring invalid integer ID '%s' in list.", cleaned)
    return frozenset(result)


def _parse_ip_networks(value: str | None, default: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    if value is None:
        raw_items = list(default)
    elif not value.strip():
        return ()
    else:
        raw_items = [part.strip() for part in value.split(",") if part.strip()]

    networks: list[IPNetwork] = []
    for entry in raw_items:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid IP/CIDR entry '%s' in TRUSTED_PROXY_IPS.", entry)
    return tuple(networks)


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
    max_post_disk_percent: float
    min_free_ram_mb: int
    max_concurrent_jobs: int

    allow_dms: bool
    allowed_guild_ids: frozenset[int]
    max_comment_length: int

    default_view_mode: str
    show_storage_status: bool
    max_concurrent_downloads_per_ip: int
    trusted_proxy_networks: tuple[IPNetwork, ...]
    restrict_web_to_trusted_proxies: bool

    video_codec: str
    video_preset: str
    preview_format: str
    preview_quality: int

    delete_original_message: bool
    add_delete_reaction: bool
    add_like_reaction: bool
    delete_files_on_retract: bool

    @property
    def min_free_space_bytes(self) -> int:
        return int(self.min_free_space_gb * (1024 ** 3))

    @property
    def min_free_ram_bytes(self) -> int:
        return int(self.min_free_ram_mb * (1024 ** 2))


def load_config(env_path: Path | str | None = None, require_token: bool = True) -> Config:
    base_dir = Path(__file__).resolve().parent.parent
    dotenv_target = Path(env_path) if env_path else (base_dir / ".env")
    # Under systemd hardening, .env is root-only (600) and already injected via EnvironmentFile
    if dotenv_target.is_file() and os.access(dotenv_target, os.R_OK):
        load_dotenv(dotenv_target)

    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    if require_token and (not discord_token or discord_token == "your_discord_bot_token_here"):
        raise ValueError("DISCORD_TOKEN is missing or left as placeholder in .env")

    public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").strip().rstrip("/")
    web_host = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    web_port = _parse_int(os.getenv("WEB_PORT"), 8080, minimum=1)

    raw_proxy = os.getenv("PROXY_URL", "").strip()
    proxy_url = raw_proxy if raw_proxy else None

    raw_data_dir = Path(os.getenv("DATA_DIR", "./data").strip() or "./data")
    data_dir = raw_data_dir if raw_data_dir.is_absolute() else (base_dir / raw_data_dir).resolve()

    raw_temp_dir = Path(os.getenv("TEMP_DIR", "/tmp/xbot_compress").strip() or "/tmp/xbot_compress")
    temp_dir = raw_temp_dir if raw_temp_dir.is_absolute() else (base_dir / raw_temp_dir).resolve()

    cookies_file = (base_dir / "config" / "cookies.txt").resolve()
    web_dir = (base_dir / "web").resolve()

    default_view_mode = os.getenv("DEFAULT_VIEW_MODE", "carousel").strip().lower()
    if default_view_mode not in VALID_VIEW_MODES:
        logger.warning(
            "Unsupported DEFAULT_VIEW_MODE '%s' (allowed: %s), falling back to 'carousel'",
            default_view_mode,
            ", ".join(VALID_VIEW_MODES),
        )
        default_view_mode = "carousel"

    video_codec = os.getenv("VIDEO_CODEC", "libsvtav1").strip().lower()
    if video_codec not in SUPPORTED_VIDEO_CODECS:
        logger.warning(
            "Unsupported VIDEO_CODEC '%s' (supported: %s), falling back to 'libsvtav1'",
            video_codec,
            ", ".join(SUPPORTED_VIDEO_CODECS),
        )
        video_codec = "libsvtav1"

    raw_preset = os.getenv("VIDEO_PRESET", "").strip().lower()
    video_preset = raw_preset if raw_preset else DEFAULT_VIDEO_PRESETS[video_codec]

    preview_format = os.getenv("PREVIEW_FORMAT", "webp").strip().lower() or "webp"
    preview_quality = min(100, _parse_int(os.getenv("PREVIEW_QUALITY"), 90, minimum=1))

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
        min_free_space_gb=_parse_float(os.getenv("MIN_FREE_SPACE_GB"), 5.0, minimum=0.0),
        max_post_disk_percent=_parse_float(os.getenv("MAX_POST_DISK_PERCENT"), 50.0, minimum=0.0, maximum=95.0),
        min_free_ram_mb=_parse_int(os.getenv("MIN_FREE_RAM_MB"), 512, minimum=0),
        max_concurrent_jobs=_parse_int(os.getenv("MAX_CONCURRENT_JOBS"), 2, minimum=1),
        allow_dms=_parse_bool(os.getenv("ALLOW_DMS"), False),
        allowed_guild_ids=_parse_int_set(os.getenv("ALLOWED_GUILD_IDS")),
        max_comment_length=_parse_int(os.getenv("MAX_COMMENT_LENGTH"), 50, minimum=0),
        default_view_mode=default_view_mode,
        show_storage_status=_parse_bool(os.getenv("SHOW_STORAGE_STATUS"), True),
        max_concurrent_downloads_per_ip=_parse_int(os.getenv("MAX_CONCURRENT_DOWNLOADS_PER_IP"), 10, minimum=0),
        trusted_proxy_networks=_parse_ip_networks(os.getenv("TRUSTED_PROXY_IPS"), DEFAULT_TRUSTED_PROXY_IPS),
        restrict_web_to_trusted_proxies=_parse_bool(os.getenv("RESTRICT_WEB_TO_TRUSTED_PROXIES"), False),
        video_codec=video_codec,
        video_preset=video_preset,
        preview_format=preview_format,
        preview_quality=preview_quality,
        delete_original_message=_parse_bool(os.getenv("DELETE_ORIGINAL_MESSAGE"), True),
        add_delete_reaction=_parse_bool(os.getenv("ADD_DELETE_REACTION"), True),
        add_like_reaction=_parse_bool(os.getenv("ADD_LIKE_REACTION"), True),
        delete_files_on_retract=_parse_bool(os.getenv("DELETE_FILES_ON_RETRACT"), True),
    )