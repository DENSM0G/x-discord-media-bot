from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.config import Config, get_sanitized_subprocess_env

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}

MIN_WATCHABLE_VIDEO_KBPS = 120
PREVIEW_MAX_DIMENSION = 1600

# Progressive (max_dimension, fps) presets for converting X MP4-GIFs into real autoplaying .gif
GIF_CONVERSION_PRESETS: tuple[tuple[int, int], ...] = (
    (560, 20),
    (440, 15),
    (320, 12),
    (240, 10),
)


@dataclass
class MediaProbeInfo:
    media_type: str  # "image" | "gif" | "video"
    width: int
    height: int
    duration: float
    has_audio: bool
    size_bytes: int
    mime_type: str


@dataclass
class PreparedAttachment:
    original_filename: str
    media_type: str
    send_path: Path | None
    size_bytes: int
    compressed: bool = False
    skipped_too_large: bool = False


# ----------------------------------------------------------------------
# System RAM & CPU safeguards (Linux / Proxmox LXC)
# ----------------------------------------------------------------------

def get_available_ram_bytes() -> int | None:
    """Reads available container RAM from /proc/meminfo (works accurately inside LXC via lxcfs)."""
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None

    values_kb: dict[str, int] = {}
    try:
        with meminfo.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                tokens = parts[1].strip().split()
                if tokens and tokens[0].isdigit():
                    values_kb[key] = int(tokens[0])
    except OSError:
        return None

    if "MemAvailable" in values_kb:
        return values_kb["MemAvailable"] * 1024

    if "MemFree" in values_kb:
        fallback_kb = (
            values_kb.get("MemFree", 0)
            + values_kb.get("Buffers", 0)
            + values_kb.get("Cached", 0)
        )
        return fallback_kb * 1024
    return None


async def wait_for_available_ram(
    min_free_ram_bytes: int,
    poll_interval: float = 2.0,
    timeout: float = 900.0,
) -> bool:
    """Waits until free RAM meets the configured threshold to prevent OOM Killer crashes."""
    if min_free_ram_bytes <= 0:
        return True

    deadline = asyncio.get_running_loop().time() + timeout
    logged_wait = False

    while True:
        avail = get_available_ram_bytes()
        if avail is None or avail >= min_free_ram_bytes:
            if logged_wait:
                logger.info(
                    "RAM recovered (%.0f MB available) — resuming processing.",
                    (avail or 0) / (1024 ** 2),
                )
            return True

        if asyncio.get_running_loop().time() >= deadline:
            logger.warning(
                "Timed out waiting for %.0f MB free RAM (currently %.0f MB) — proceeding cautiously.",
                min_free_ram_bytes / (1024 ** 2),
                avail / (1024 ** 2),
            )
            return False

        if not logged_wait:
            logger.warning(
                "Low RAM: %.0f MB available < %.0f MB threshold — pausing heavy job to avoid OOM.",
                avail / (1024 ** 2),
                min_free_ram_bytes / (1024 ** 2),
            )
            logged_wait = True

        await asyncio.sleep(poll_interval)


def _compute_ffmpeg_threads(config: Config) -> int:
    cpus = os.cpu_count() or 2
    per_job = max(1, cpus // max(1, config.max_concurrent_jobs))
    return min(4, per_job)


def _with_low_priority(cmd: list[str]) -> list[str]:
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10", *cmd]
    return cmd


async def _run_cmd(cmd: list[str], timeout: float = 600.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_sanitized_subprocess_env(),
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return -1, "", f"Process timed out after {int(timeout)}s"

    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


# ----------------------------------------------------------------------
# Media probing & WebP preview/poster generation (no JPEG banding)
# ----------------------------------------------------------------------

def _probe_image_sync(file_path: Path) -> tuple[int, int, bool]:
    with Image.open(file_path) as img:
        is_animated = bool(getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1)
        transposed = ImageOps.exif_transpose(img)
        return transposed.width, transposed.height, is_animated


async def _probe_video(file_path: Path) -> tuple[int, int, float, bool]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(file_path),
    ]
    code, stdout, _ = await _run_cmd(cmd, timeout=30.0)
    if code != 0 or not stdout.strip():
        return 0, 0, 0.0, False

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return 0, 0, 0.0, False

    width = 0
    height = 0
    has_audio = False
    duration = 0.0

    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and width == 0:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            rotation = 0
            tags = stream.get("tags") or {}
            if "rotate" in tags:
                try:
                    rotation = abs(int(tags["rotate"])) % 360
                except (TypeError, ValueError):
                    rotation = 0
            for side in stream.get("side_data_list") or []:
                if "rotation" in side:
                    try:
                        rotation = abs(int(side["rotation"])) % 360
                    except (TypeError, ValueError):
                        pass
            if rotation in (90, 270):
                width, height = height, width
            if duration <= 0.0:
                try:
                    duration = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif codec_type == "audio":
            has_audio = True

    return width, height, max(0.0, duration), has_audio


async def probe_media(file_path: Path, is_gif_hint: bool = False) -> MediaProbeInfo:
    size_bytes = file_path.stat().st_size
    ext = file_path.suffix.lower()
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    if ext == ".gif":
        w, h, _ = await asyncio.to_thread(_probe_image_sync, file_path)
        return MediaProbeInfo(
            media_type="gif",
            width=w,
            height=h,
            duration=0.0,
            has_audio=False,
            size_bytes=size_bytes,
            mime_type="image/gif",
        )

    if ext in IMAGE_EXTENSIONS:
        w, h, is_animated = await asyncio.to_thread(_probe_image_sync, file_path)
        return MediaProbeInfo(
            media_type="gif" if is_animated else "image",
            width=w,
            height=h,
            duration=0.0,
            has_audio=False,
            size_bytes=size_bytes,
            mime_type=mime_type,
        )

    w, h, duration, has_audio = await _probe_video(file_path)
    is_gif = is_gif_hint or (not has_audio and 0.0 < duration <= 12.0)
    return MediaProbeInfo(
        media_type="gif" if is_gif else "video",
        width=w,
        height=h,
        duration=duration,
        has_audio=has_audio,
        size_bytes=size_bytes,
        mime_type=mime_type if mime_type != "application/octet-stream" else "video/mp4",
    )


def _save_image_as_webp_sync(
    src_path: Path,
    dest_path: Path,
    quality: int,
    max_dimension: int = PREVIEW_MAX_DIMENSION,
) -> None:
    with Image.open(src_path) as img:
        if getattr(img, "is_animated", False):
            img.seek(0)
        frame = ImageOps.exif_transpose(img)
        if frame.mode not in ("RGB", "RGBA"):
            frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
        if max(frame.width, frame.height) > max_dimension:
            frame.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        frame.save(dest_path, format="WEBP", quality=quality, method=4)


async def _generate_video_poster_webp(
    video_path: Path,
    poster_path: Path,
    duration: float,
    quality: int,
) -> bool:
    """Extracts a poster frame via ffmpeg -> PNG -> Pillow WebP (immune to missing ffmpeg libwebp)."""
    tmp_png = poster_path.with_suffix(".tmp.png")
    seek_sec = min(0.5, duration / 2.0) if duration > 0.2 else 0.0
    cmd = _with_low_priority([
        "ffmpeg", "-y",
        "-ss", f"{seek_sec:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale='min(iw,{PREVIEW_MAX_DIMENSION})':'min(ih,{PREVIEW_MAX_DIMENSION})':force_original_aspect_ratio=decrease",
        str(tmp_png),
    ])
    try:
        code, _, _ = await _run_cmd(cmd, timeout=45.0)
        if code != 0 or not tmp_png.is_file() or tmp_png.stat().st_size == 0:
            cmd_retry = _with_low_priority([
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-frames:v", "1",
                str(tmp_png),
            ])
            code, _, _ = await _run_cmd(cmd_retry, timeout=45.0)
            if code != 0 or not tmp_png.is_file() or tmp_png.stat().st_size == 0:
                return False

        await asyncio.to_thread(
            _save_image_as_webp_sync,
            tmp_png,
            poster_path,
            quality,
            PREVIEW_MAX_DIMENSION,
        )
        return poster_path.is_file() and poster_path.stat().st_size > 0
    finally:
        tmp_png.unlink(missing_ok=True)


async def process_downloaded_media(
    folder_path: Path,
    raw_files: list[Path],
    config: Config,
    gif_filenames: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Probes all downloaded originals and builds lightweight WebP previews/posters
    inside folder_path/.previews for instant gallery rendering.
    """
    gif_hints = gif_filenames or set()
    previews_dir = folder_path / ".previews"
    previews_dir.mkdir(exist_ok=True)

    files_meta: list[dict[str, Any]] = []

    for index, file_path in enumerate(raw_files, start=1):
        info = await probe_media(file_path, is_gif_hint=(file_path.name in gif_hints))
        preview_rel: str | None = None
        preview_path = previews_dir / f"{file_path.stem}.webp"

        if file_path.suffix.lower() in IMAGE_EXTENSIONS or file_path.suffix.lower() == ".gif":
            try:
                await asyncio.to_thread(
                    _save_image_as_webp_sync,
                    file_path,
                    preview_path,
                    config.preview_quality,
                )
                if preview_path.is_file() and preview_path.stat().st_size > 0:
                    preview_rel = f".previews/{preview_path.name}"
            except Exception as exc:
                logger.warning("Failed to build WebP preview for %s: %s", file_path.name, exc)
        else:
            ok = await _generate_video_poster_webp(
                file_path,
                preview_path,
                info.duration,
                config.preview_quality,
            )
            if ok:
                preview_rel = f".previews/{preview_path.name}"

        files_meta.append({
            "index": index,
            "filename": file_path.name,
            "type": info.media_type,
            "preview": preview_rel,
            "width": info.width,
            "height": info.height,
            "duration": round(info.duration, 2),
            "has_audio": info.has_audio,
            "size_bytes": info.size_bytes,
            "mime_type": info.mime_type,
            "is_mp4_gif": info.media_type == "gif" and file_path.suffix.lower() in VIDEO_EXTENSIONS,
        })

    return files_meta


# ----------------------------------------------------------------------
# Discord attachment preparation (autoplaying GIFs, modern codecs, size re-check)
# ----------------------------------------------------------------------

def _normalize_preset_for_codec(codec: str, raw_preset: str) -> str:
    """Maps generic or cross-codec preset names safely to the active encoder."""
    p = raw_preset.strip().lower()

    if codec == "libsvtav1":
        if p.isdigit():
            return str(max(0, min(13, int(p))))
        mapping = {
            "veryslow": "2",
            "slower": "4",
            "slow": "5",
            "medium": "6",
            "fast": "8",
            "faster": "10",
            "veryfast": "11",
        }
        return mapping.get(p, "4")

    if codec == "libvpx-vp9":
        if p.isdigit():
            return str(max(0, min(5, int(p))))
        mapping = {
            "veryslow": "0",
            "slower": "1",
            "slow": "2",
            "medium": "3",
            "fast": "4",
            "faster": "5",
        }
        return mapping.get(p, "1")

    valid_x265 = {
        "ultrafast", "superfast", "veryfast", "faster",
        "fast", "medium", "slow", "slower", "veryslow",
    }
    if p in valid_x265:
        return p
    if p.isdigit():
        num = int(p)
        if num <= 2:
            return "veryslow"
        if num <= 4:
            return "slower"
        if num <= 6:
            return "slow"
        if num <= 8:
            return "medium"
        return "fast"
    return "slower"


def _select_max_video_dimension(video_kbps: int) -> int | None:
    if video_kbps >= 1400:
        return 1080
    if video_kbps >= 650:
        return 720
    if video_kbps >= 280:
        return 480
    return 360


def _compress_image_to_webp_sync(
    src_path: Path,
    dest_path: Path,
    max_file_size_bytes: int,
) -> bool:
    """Compresses oversized static images to WebP (avoiding JPEG banding) until within limit."""
    with Image.open(src_path) as img:
        base = ImageOps.exif_transpose(img)
        if base.mode not in ("RGB", "RGBA"):
            base = base.convert("RGBA" if "A" in base.getbands() else "RGB")

        scale_steps = [1.0, 0.85, 0.7, 0.55, 0.4]
        quality_steps = [92, 84, 76, 68]

        for scale in scale_steps:
            if scale < 1.0:
                new_w = max(64, int(base.width * scale))
                new_h = max(64, int(base.height * scale))
                candidate = base.resize((new_w, new_h), Image.Resampling.LANCZOS)
            else:
                candidate = base

            for quality in quality_steps:
                candidate.save(dest_path, format="WEBP", quality=quality, method=5)
                if dest_path.is_file() and dest_path.stat().st_size <= max_file_size_bytes:
                    return True

    return False


async def _convert_gif_for_discord(
    src_path: Path,
    temp_job_dir: Path,
    max_file_size_bytes: int,
    config: Config,
) -> Path | None:
    """
    Converts X MP4-GIFs (or oversized GIFs) into real looping .gif files so Discord
    autoplays them inline instead of rendering a click-to-play video player.
    """
    dest_path = temp_job_dir / f"{src_path.stem}.gif"
    threads = _compute_ffmpeg_threads(config)

    for max_dim, fps in GIF_CONVERSION_PRESETS:
        await wait_for_available_ram(config.min_free_ram_bytes)
        vf = (
            f"fps={fps},"
            f"scale='min(iw,{max_dim})':'min(ih,{max_dim})':force_original_aspect_ratio=decrease:flags=lanczos,"
            f"split[s0][s1];"
            f"[s0]palettegen=stats_mode=diff[p];"
            f"[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
        )
        cmd = _with_low_priority([
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", vf,
            "-loop", "0",
            "-threads", str(threads),
            str(dest_path),
        ])
        code, _, stderr = await _run_cmd(cmd, timeout=180.0)
        if code != 0 or not dest_path.is_file() or dest_path.stat().st_size == 0:
            logger.warning("GIF conversion failed (%dpx/%dfps) for %s: %s", max_dim, fps, src_path.name, stderr.strip()[-200:])
            dest_path.unlink(missing_ok=True)
            return None

        final_size = dest_path.stat().st_size
        if final_size <= max_file_size_bytes:
            logger.info(
                "Prepared autoplaying GIF for %s (%dpx @ %dfps -> %.2f MB)",
                src_path.name,
                max_dim,
                fps,
                final_size / (1024 ** 2),
            )
            return dest_path

        logger.info(
            "Converted GIF %s (%dpx @ %dfps) is %.2f MB (> %.2f MB limit) — trying smaller preset.",
            src_path.name,
            max_dim,
            fps,
            final_size / (1024 ** 2),
            max_file_size_bytes / (1024 ** 2),
        )
        dest_path.unlink(missing_ok=True)

    return None


async def _encode_video_attempt(
    src_path: Path,
    dest_path: Path,
    codec: str,
    preset: str,
    video_kbps: int,
    audio_kbps: int,
    has_audio: bool,
    max_dim: int | None,
    threads: int,
) -> bool:
    vf_filters: list[str] = []
    if max_dim is not None:
        vf_filters.append(
            f"scale=w='min(iw,{max_dim})':h='min(ih,{max_dim})':force_original_aspect_ratio=decrease"
        )
    vf_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    cmd: list[str] = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-vf", ",".join(vf_filters),
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
    ]

    maxrate_kbps = max(video_kbps, int(video_kbps * 1.25))
    bufsize_kbps = max(video_kbps * 2, int(video_kbps * 2.0))

    if codec == "libsvtav1":
        cmd.extend([
            "-c:v", "libsvtav1",
            "-preset", preset,
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
        ])
    elif codec == "libvpx-vp9":
        cmd.extend([
            "-c:v", "libvpx-vp9",
            "-cpu-used", preset,
            "-row-mt", "1",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
        ])
    else:  # libx265
        cmd.extend([
            "-c:v", "libx265",
            "-preset", preset,
            "-tag:v", "hvc1",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
        ])

    if has_audio and audio_kbps > 0:
        if codec == "libvpx-vp9":
            cmd.extend(["-c:a", "libopus", "-b:a", f"{audio_kbps}k"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", f"{audio_kbps}k"])
    else:
        cmd.append("-an")

    if dest_path.suffix.lower() == ".mp4":
        cmd.extend(["-movflags", "+faststart"])

    cmd.append(str(dest_path))

    code, _, stderr = await _run_cmd(_with_low_priority(cmd), timeout=900.0)
    if code != 0 or not dest_path.is_file() or dest_path.stat().st_size == 0:
        logger.warning("ffmpeg (%s) encode failed for %s: %s", codec, src_path.name, stderr.strip()[-300:])
        dest_path.unlink(missing_ok=True)
        return False
    return True


async def _compress_video_for_discord(
    src_path: Path,
    temp_job_dir: Path,
    meta_item: dict[str, Any],
    max_file_size_bytes: int,
    config: Config,
) -> Path | None:
    """
    Compresses a video into temp_job_dir using the configured modern codec & preset.
    Re-verifies final byte size and performs a tighter retry if single-pass VBR overshoots.
    """
    duration = float(meta_item.get("duration") or 0.0)
    has_audio = bool(meta_item.get("has_audio", False))
    if duration <= 0.0:
        _, _, duration, has_audio = await _probe_video(src_path)
    if duration <= 0.0:
        duration = 10.0

    codec = config.video_codec
    preset = _normalize_preset_for_codec(codec, config.video_preset)
    out_ext = ".webm" if codec == "libvpx-vp9" else ".mp4"
    dest_path = temp_job_dir / f"{src_path.stem}_compressed{out_ext}"
    threads = _compute_ffmpeg_threads(config)

    for attempt_idx, budget_ratio in enumerate((0.92, 0.74), start=1):
        target_bits = max_file_size_bytes * 8 * budget_ratio
        total_kbps = int(target_bits / duration / 1000.0)

        if has_audio:
            if total_kbps >= 600:
                audio_kbps = 96
            elif total_kbps >= 300:
                audio_kbps = 64
            else:
                audio_kbps = 48
        else:
            audio_kbps = 0

        video_kbps = total_kbps - audio_kbps
        if video_kbps < MIN_WATCHABLE_VIDEO_KBPS:
            logger.info(
                "Skipping Discord attachment for %s: duration=%.1fs requires %d kbps (< %d kbps minimum).",
                src_path.name,
                duration,
                video_kbps,
                MIN_WATCHABLE_VIDEO_KBPS,
            )
            dest_path.unlink(missing_ok=True)
            return None

        max_dim = _select_max_video_dimension(video_kbps)
        if attempt_idx == 2 and max_dim is not None and max_dim > 480:
            max_dim = 480

        await wait_for_available_ram(config.min_free_ram_bytes)
        logger.info(
            "Encoding %s (attempt %d): codec=%s preset=%s target=%dkbps audio=%dkbps max_dim=%s",
            src_path.name,
            attempt_idx,
            codec,
            preset,
            video_kbps,
            audio_kbps,
            max_dim,
        )
        ok = await _encode_video_attempt(
            src_path=src_path,
            dest_path=dest_path,
            codec=codec,
            preset=preset,
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            has_audio=has_audio,
            max_dim=max_dim,
            threads=threads,
        )
        if not ok:
            continue

        final_size = dest_path.stat().st_size
        if 0 < final_size <= max_file_size_bytes:
            return dest_path

        logger.warning(
            "Encoded %s is %d bytes (> %d limit) on attempt %d — retrying tighter bitrate.",
            src_path.name,
            final_size,
            max_file_size_bytes,
            attempt_idx,
        )
        dest_path.unlink(missing_ok=True)

    return None


async def prepare_discord_attachments(
    folder_path: Path,
    files_meta: list[dict[str, Any]],
    max_file_size_bytes: int,
    config: Config,
    temp_job_dir: Path,
) -> list[PreparedAttachment]:
    """
    Stages all files for Discord inside temp_job_dir:
    - Converts X MP4-GIFs (and oversized GIFs) into real .gif files so Discord autoplays them.
    - Compresses oversized images to WebP and oversized videos via modern codecs.
    - Performs a final strict byte-size verification pass before returning files.
    """
    temp_job_dir.mkdir(parents=True, exist_ok=True)
    safe_limit = max(1024 * 1024, max_file_size_bytes - (128 * 1024))

    prepared: list[PreparedAttachment] = []

    for item in files_meta:
        filename = str(item["filename"])
        media_type = str(item.get("type") or "image")
        src_path = folder_path / filename

        if not src_path.is_file():
            continue

        orig_size = src_path.stat().st_size

        # GIFs: convert X MP4-GIFs (and oversized GIFs) into real .gif so Discord autoplays them
        if media_type == "gif":
            if src_path.suffix.lower() == ".gif" and 0 < orig_size <= safe_limit:
                staged_path = temp_job_dir / filename
                await asyncio.to_thread(shutil.copy2, src_path, staged_path)
                prepared.append(
                    PreparedAttachment(
                        original_filename=filename,
                        media_type=media_type,
                        send_path=staged_path,
                        size_bytes=staged_path.stat().st_size,
                        compressed=False,
                        skipped_too_large=False,
                    )
                )
                continue

            gif_path = await _convert_gif_for_discord(
                src_path=src_path,
                temp_job_dir=temp_job_dir,
                max_file_size_bytes=safe_limit,
                config=config,
            )
            if gif_path is not None:
                prepared.append(
                    PreparedAttachment(
                        original_filename=filename,
                        media_type=media_type,
                        send_path=gif_path,
                        size_bytes=gif_path.stat().st_size,
                        compressed=True,
                        skipped_too_large=False,
                    )
                )
                continue

            if src_path.suffix.lower() in VIDEO_EXTENSIONS and 0 < orig_size <= safe_limit:
                staged_path = temp_job_dir / filename
                await asyncio.to_thread(shutil.copy2, src_path, staged_path)
                prepared.append(
                    PreparedAttachment(
                        original_filename=filename,
                        media_type=media_type,
                        send_path=staged_path,
                        size_bytes=staged_path.stat().st_size,
                        compressed=False,
                        skipped_too_large=False,
                    )
                )
                continue

            compressed_path = await _compress_video_for_discord(
                src_path=src_path,
                temp_job_dir=temp_job_dir,
                meta_item=item,
                max_file_size_bytes=safe_limit,
                config=config,
            )
            prepared.append(
                PreparedAttachment(
                    original_filename=filename,
                    media_type=media_type,
                    send_path=compressed_path,
                    size_bytes=compressed_path.stat().st_size if compressed_path else 0,
                    compressed=compressed_path is not None,
                    skipped_too_large=compressed_path is None,
                )
            )
            continue

        # Standard images & videos fitting within Discord limit -> stage clean copy
        if 0 < orig_size <= safe_limit:
            staged_path = temp_job_dir / filename
            await asyncio.to_thread(shutil.copy2, src_path, staged_path)
            prepared.append(
                PreparedAttachment(
                    original_filename=filename,
                    media_type=media_type,
                    send_path=staged_path,
                    size_bytes=staged_path.stat().st_size,
                    compressed=False,
                    skipped_too_large=False,
                )
            )
            continue

        # Oversized static image -> compress to WebP (avoiding JPEG banding)
        if media_type == "image":
            webp_dest = temp_job_dir / f"{src_path.stem}.webp"
            ok = await asyncio.to_thread(
                _compress_image_to_webp_sync,
                src_path,
                webp_dest,
                safe_limit,
            )
            if ok and webp_dest.is_file() and 0 < webp_dest.stat().st_size <= safe_limit:
                prepared.append(
                    PreparedAttachment(
                        original_filename=filename,
                        media_type=media_type,
                        send_path=webp_dest,
                        size_bytes=webp_dest.stat().st_size,
                        compressed=True,
                        skipped_too_large=False,
                    )
                )
            else:
                webp_dest.unlink(missing_ok=True)
                prepared.append(
                    PreparedAttachment(
                        original_filename=filename,
                        media_type=media_type,
                        send_path=None,
                        size_bytes=0,
                        compressed=False,
                        skipped_too_large=True,
                    )
                )
            continue

        # Oversized video -> encode via configured modern video codec
        compressed_path = await _compress_video_for_discord(
            src_path=src_path,
            temp_job_dir=temp_job_dir,
            meta_item=item,
            max_file_size_bytes=safe_limit,
            config=config,
        )
        prepared.append(
            PreparedAttachment(
                original_filename=filename,
                media_type=media_type,
                send_path=compressed_path,
                size_bytes=compressed_path.stat().st_size if compressed_path else 0,
                compressed=compressed_path is not None,
                skipped_too_large=compressed_path is None,
            )
        )

    # Final safety verification pass before returning files to Discord sender
    verified: list[PreparedAttachment] = []
    for att in prepared:
        if att.send_path is not None:
            if not att.send_path.is_file():
                verified.append(
                    PreparedAttachment(
                        original_filename=att.original_filename,
                        media_type=att.media_type,
                        send_path=None,
                        size_bytes=0,
                        compressed=False,
                        skipped_too_large=True,
                    )
                )
                continue
            actual_size = att.send_path.stat().st_size
            if actual_size <= 0 or actual_size > max_file_size_bytes:
                logger.warning(
                    "Final size check rejected %s (%d bytes > %d limit).",
                    att.send_path.name,
                    actual_size,
                    max_file_size_bytes,
                )
                att.send_path.unlink(missing_ok=True)
                verified.append(
                    PreparedAttachment(
                        original_filename=att.original_filename,
                        media_type=att.media_type,
                        send_path=None,
                        size_bytes=0,
                        compressed=False,
                        skipped_too_large=True,
                    )
                )
                continue
            att.size_bytes = actual_size
        verified.append(att)

    return verified