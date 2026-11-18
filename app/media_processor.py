from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.extractor import ExtractedItem

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@dataclass
class PreparedAttachment:
    path: Path
    filename: str
    is_temp: bool


class MediaProcessor:
    def __init__(
        self,
        temp_dir: Path,
        video_codec: str = "libx265",
        preview_format: str = "webp",
        preview_quality: int = 90,
    ) -> None:
        self.temp_dir = temp_dir
        self.video_codec = video_codec
        self.preview_format = preview_format
        self.preview_quality = preview_quality
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def process_extracted_items(
        self, items: list[ExtractedItem], target_folder: Path
    ) -> tuple[list[dict[str, Any]], str]:
        """
        Переносит оригиналы из _raw в папку поста, классифицирует типы медиа,
        генерирует WebP-превью для изображений и определяет layout_mode.
        """
        previews_dir = target_folder / ".previews"
        previews_dir.mkdir(exist_ok=True)

        processed_files: list[dict[str, Any]] = []

        for idx, item in enumerate(items, start=1):
            ext = item.raw_path.suffix.lower() or ".bin"
            clean_filename = f"{idx:02d}_media{ext}"
            dest_path = target_folder / clean_filename
            shutil.move(str(item.raw_path), str(dest_path))

            size_bytes = dest_path.stat().st_size

            if ext in IMAGE_EXTENSIONS:
                width, height = await asyncio.to_thread(self._get_image_dimensions, dest_path)
                preview_name = f"{idx:02d}_media.{self.preview_format}"
                preview_abs = previews_dir / preview_name
                await asyncio.to_thread(self._generate_preview, dest_path, preview_abs)

                processed_files.append(
                    {
                        "index": idx,
                        "type": "image",
                        "filename": clean_filename,
                        "preview": f".previews/{preview_name}",
                        "width": width,
                        "height": height,
                        "duration": 0.0,
                        "has_audio": False,
                        "size_bytes": size_bytes,
                    }
                )
            else:
                probe = await self._probe_video(dest_path)
                width = probe["width"]
                height = probe["height"]
                duration = probe["duration"]
                has_audio = probe["has_audio"]

                is_gif = (
                    item.source_type == "animated_gif"
                    or ext == ".gif"
                    or (not has_audio and 0 < duration <= 30.0)
                )
                media_type = "gif" if is_gif else "video"

                processed_files.append(
                    {
                        "index": idx,
                        "type": media_type,
                        "filename": clean_filename,
                        "preview": None,
                        "width": width,
                        "height": height,
                        "duration": duration,
                        "has_audio": has_audio,
                        "size_bytes": size_bytes,
                    }
                )

        # Удаляем рабочую папку _raw вместе со служебными json экстрактора
        raw_dir = target_folder / "_raw"
        if raw_dir.exists():
            shutil.rmtree(raw_dir, ignore_errors=True)

        layout_mode = self.determine_layout_mode(processed_files)
        return processed_files, layout_mode

    @staticmethod
    def determine_layout_mode(files: list[dict[str, Any]]) -> str:
        """Выбирает оптимальную раскладку галереи по составу и пропорциям файлов."""
        visuals = [f for f in files if f.get("type") in ("image", "gif")]
        count = len(visuals)

        if count == 0:
            return "video_only"
        if count == 1:
            return "single"

        vertical_count = sum(
            1
            for f in visuals
            if f.get("width", 0) > 0 and f.get("height", 0) > f.get("width", 0) * 1.35
        )
        if vertical_count / count >= 0.6:
            return "vertical_strip"

        if count <= 4:
            return "grid"

        return "carousel"

    @staticmethod
    def _get_image_dimensions(image_path: Path) -> tuple[int, int]:
        with Image.open(image_path) as img:
            transposed = ImageOps.exif_transpose(img)
            return transposed.size

    def _generate_preview(self, source_path: Path, preview_path: Path) -> None:
        with Image.open(source_path) as img:
            img = ImageOps.exif_transpose(img)
            icc_profile = img.info.get("icc_profile")

            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

            max_side = 1600
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

            save_kwargs: dict[str, Any] = {
                "quality": self.preview_quality,
                "method": 4,
            }
            if icc_profile:
                save_kwargs["icc_profile"] = icc_profile

            img.save(preview_path, format=self.preview_format.upper(), **save_kwargs)

    async def _probe_video(self, video_path: Path) -> dict[str, Any]:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        width = 0
        height = 0
        duration = 0.0
        has_audio = False

        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            fmt = data.get("format") or {}
            if fmt.get("duration"):
                duration = float(fmt["duration"])

            for stream in data.get("streams") or []:
                codec_type = stream.get("codec_type")
                if codec_type == "video" and width == 0:
                    width = int(stream.get("width") or 0)
                    height = int(stream.get("height") or 0)
                    if duration == 0.0 and stream.get("duration"):
                        duration = float(stream["duration"])
                elif codec_type == "audio":
                    has_audio = True
        except (ValueError, json.JSONDecodeError):
            pass

        return {
            "width": width,
            "height": height,
            "duration": duration,
            "has_audio": has_audio,
        }

    async def prepare_discord_attachments(
        self,
        files: list[dict[str, Any]],
        folder_path: Path,
        max_bytes: int,
    ) -> tuple[list[PreparedAttachment], list[str]]:
        """
        Готовит список вложений для отправки в Discord:
        оригиналы до лимита берутся напрямую, тяжелые видео сжимаются в temp_dir.
        Возвращает (готовые_вложения, список_пропущенных_имен).
        """
        target_bytes = int(max_bytes * 0.95)
        attachments: list[PreparedAttachment] = []
        skipped: list[str] = []

        for item in files:
            filename = item["filename"]
            src_path = folder_path / filename
            size_bytes = src_path.stat().st_size

            if size_bytes <= target_bytes:
                attachments.append(
                    PreparedAttachment(path=src_path, filename=filename, is_temp=False)
                )
                continue

            if item["type"] in ("video", "gif"):
                compressed = await self._compress_video(
                    src_path=src_path,
                    filename=filename,
                    duration=float(item.get("duration") or 0.0),
                    has_audio=bool(item.get("has_audio")),
                    target_bytes=target_bytes,
                    hard_limit_bytes=max_bytes,
                )
                if compressed:
                    attachments.append(compressed)
                else:
                    skipped.append(filename)
            else:
                compressed_img = await asyncio.to_thread(
                    self._compress_large_image, src_path, filename, target_bytes
                )
                if compressed_img:
                    attachments.append(compressed_img)
                else:
                    skipped.append(filename)

        return attachments, skipped

    async def _compress_video(
        self,
        src_path: Path,
        filename: str,
        duration: float,
        has_audio: bool,
        target_bytes: int,
        hard_limit_bytes: int,
    ) -> PreparedAttachment | None:
        if duration <= 0:
            probe = await self._probe_video(src_path)
            duration = probe["duration"] or 10.0

        # Закладываем 8% запаса на заголовок MP4-контейнера
        usable_bits = target_bytes * 8 * 0.92
        audio_bps = 96_000 if has_audio else 0
        video_bps = int(usable_bits / duration) - audio_bps

        if video_bps < 120_000:
            logger.warning("Видео %s слишком длинное (%.1f с) для сжатия в лимит Discord.", filename, duration)
            return None

        out_name = f"{uuid.uuid4().hex[:8]}_{Path(filename).stem}.mp4"
        out_path = self.temp_dir / out_name

        # Если битрейт невысокий, ограничиваем разрешение до 1080p с четными сторонами
        if video_bps < 1_500_000:
            vf = (
                "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            )
        else:
            vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        cmd: list[str] = [
            "ffmpeg",
            "-y",
            "-i",
            str(src_path),
            "-vf",
            vf,
            "-c:v",
            self.video_codec,
            "-preset",
            "medium",
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(int(video_bps * 1.4)),
            "-bufsize",
            str(int(video_bps * 2.0)),
            "-pix_fmt",
            "yuv420p",
        ]

        if "265" in self.video_codec or "hevc" in self.video_codec:
            cmd.extend(["-tag:v", "hvc1"])

        if has_audio:
            cmd.extend(["-c:a", "aac", "-b:a", "96k"])
        else:
            cmd.append("-an")

        cmd.extend(["-movflags", "+faststart", str(out_path)])

        logger.info("Сжатие видео %s (%s, целевой битрейт %d кбит/с)...", filename, self.video_codec, video_bps // 1000)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0 or not out_path.is_file():
            logger.error("Ошибка ffmpeg для %s: %s", filename, stderr.decode("utf-8", errors="replace")[-500:])
            out_path.unlink(missing_ok=True)
            return None

        if out_path.stat().st_size > hard_limit_bytes:
            logger.warning("Сжатый файл %s всё равно превысил лимит Discord.", filename)
            out_path.unlink(missing_ok=True)
            return None

        return PreparedAttachment(
            path=out_path,
            filename=f"{Path(filename).stem}.mp4",
            is_temp=True,
        )

    def _compress_large_image(
        self, src_path: Path, filename: str, target_bytes: int
    ) -> PreparedAttachment | None:
        out_path = self.temp_dir / f"{uuid.uuid4().hex[:8]}_{Path(filename).stem}.webp"
        with Image.open(src_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
            for quality in (88, 75, 60):
                img.save(out_path, format="WEBP", quality=quality, method=4)
                if out_path.stat().st_size <= target_bytes:
                    return PreparedAttachment(
                        path=out_path,
                        filename=f"{Path(filename).stem}.webp",
                        is_temp=True,
                    )
        out_path.unlink(missing_ok=True)
        return None