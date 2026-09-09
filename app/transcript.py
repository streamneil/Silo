from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


_VISION_OCR_SOURCE = r"""import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    fputs("usage: vision-ocr IMAGE\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL) else {
    fputs("cannot read image\n", stderr)
    exit(3)
}
var rect = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fputs("cannot decode image\n", stderr)
    exit(4)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
request.usesLanguageCorrection = true
try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])

let observations = (request.results ?? []).sorted {
    if abs($0.boundingBox.maxY - $1.boundingBox.maxY) > 0.015 {
        return $0.boundingBox.maxY > $1.boundingBox.maxY
    }
    return $0.boundingBox.minX < $1.boundingBox.minX
}
let lines = observations.compactMap { $0.topCandidates(1).first?.string }
print(lines.joined(separator: "\n"))
"""


class TranscriptError(RuntimeError):
    pass


def normalize_transcript(text: str) -> str:
    """Light normalization that keeps the creator's linguistic fingerprint."""
    text = html.unescape(text).replace("\ufeff", "").replace("\u200b", "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if "\n" not in text and len(text) > 120:
        text = re.sub(r"([。！？!?])", r"\1\n", text)
    return text.strip()


def _walk_for_subtitles(value: Any, path: str = "") -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = f"{path}.{key}" if path else key
            lowered = key.lower()
            if any(token in lowered for token in ("subtitle", "caption_info", "video_text")):
                found.append(child)
            if len(path.split(".")) < 6:
                found.extend(_walk_for_subtitles(child, next_path))
    elif isinstance(value, list) and len(path.split(".")) < 6:
        for child in value:
            found.extend(_walk_for_subtitles(child, path))
    return found


def _subtitle_text(value: Any) -> str | None:
    if isinstance(value, str):
        if value.startswith("http"):
            return None
        return value.strip() or None
    if isinstance(value, list):
        segments: list[str] = []
        for item in value:
            if isinstance(item, str) and not item.startswith("http"):
                segments.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("utterance")
                if isinstance(text, str):
                    segments.append(text)
        return "\n".join(segments).strip() or None
    if isinstance(value, dict):
        for key in ("text", "content", "utterance", "subtitle_text"):
            if isinstance(value.get(key), str) and not value[key].startswith("http"):
                return value[key].strip() or None
        for key in ("list", "captions", "subtitles", "items"):
            text = _subtitle_text(value.get(key))
            if text:
                return text
    return None


def _subtitle_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        for key in ("url", "download_url", "webvtt_url", "subtitle_url"):
            found = _subtitle_url(value.get(key))
            if found:
                return found
        urls = value.get("url_list")
        if isinstance(urls, list):
            return next(
                (url for url in urls if isinstance(url, str) and url.startswith("http")), None
            )
        for child in value.values():
            found = _subtitle_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _subtitle_url(child)
            if found:
                return found
    return None


def parse_subtitle_payload(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload = json.loads(stripped)
            text = _subtitle_text(payload)
            if text:
                return normalize_transcript(text)
        except json.JSONDecodeError:
            pass
    lines: list[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return normalize_transcript("\n".join(lines))


def image_urls(metadata: dict[str, Any]) -> list[str]:
    """Return one original image URL per slide in a Douyin image post."""
    images = metadata.get("images")
    if not isinstance(images, list):
        return []
    urls: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        candidates = image.get("url_list") or image.get("download_url_list") or []
        if isinstance(candidates, list):
            url = next(
                (
                    value
                    for value in candidates
                    if isinstance(value, str) and value.startswith("http")
                ),
                None,
            )
            if url:
                urls.append(url)
    return urls


class TranscriptService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def official_subtitle(
        self, metadata: dict[str, Any], cookie_header: str = ""
    ) -> str | None:
        for candidate in _walk_for_subtitles(metadata):
            direct = _subtitle_text(candidate)
            if direct and len(direct) > 20:
                return normalize_transcript(direct)
            url = _subtitle_url(candidate)
            if not url:
                continue
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"}
            if cookie_header:
                headers["Cookie"] = cookie_header
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                text = parse_subtitle_payload(response.text)
                if text:
                    return text
        return None

    async def download_video(self, url: str, destination: Path, cookie_header: str = "") -> Path:
        if not url:
            raise TranscriptError("视频没有可用的下载地址")
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.douyin.com/",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            output.write(chunk)
            if destination.stat().st_size < 1024:
                raise TranscriptError("下载到的视频文件异常")
            return destination
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, TranscriptError):
                raise
            raise TranscriptError(f"视频下载失败：{str(exc)[:200]}") from exc

    def _vision_ocr_command(self) -> Path:
        binary = self.settings.data_dir / "bin" / "vision-ocr"
        if binary.exists():
            return binary
        swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
        if not Path(swiftc).exists():
            raise TranscriptError("Mac 未安装 Swift 编译器，无法启用图文 OCR")
        binary.parent.mkdir(parents=True, exist_ok=True)
        source = binary.with_suffix(".swift")
        source.write_text(_VISION_OCR_SOURCE, encoding="utf-8")
        result = subprocess.run(
            [swiftc, str(source), "-O", "-o", str(binary)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        source.unlink(missing_ok=True)
        if result.returncode != 0 or not binary.exists():
            raise TranscriptError(f"图文 OCR 初始化失败：{result.stderr[-500:]}")
        return binary

    def _ocr_image(self, image_path: Path) -> str:
        result = subprocess.run(
            [str(self._vision_ocr_command()), str(image_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise TranscriptError(f"图片 OCR 失败：{result.stderr[-300:]}")
        return normalize_transcript(result.stdout)

    def video_frame_text(
        self,
        media_path: Path,
        description: str = "",
        duration_seconds: float = 0,
    ) -> dict[str, Any]:
        """Extract visible text when a video has no recognizable speech.

        Douyin also represents static note cards as videos with background music.
        In that case ASR is expected to return no speech, so macOS Quick Look is
        used to render the representative frame and Apple Vision reads its text.
        """
        qlmanage = shutil.which("qlmanage") or "/usr/bin/qlmanage"
        if not Path(qlmanage).exists():
            raise TranscriptError("Mac 缺少 qlmanage，无法提取无声视频的画面文字")
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="silo-video-ocr-") as tmp:
            tmp_dir = Path(tmp)
            result = subprocess.run(
                [qlmanage, "-t", "-s", "1800", "-o", str(tmp_dir), str(media_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            image_path = next(tmp_dir.glob("*.png"), None)
            if result.returncode != 0 or not image_path:
                raise TranscriptError(f"视频画面提取失败：{result.stderr[-300:]}")
            recognized = self._ocr_image(image_path)

        if not recognized:
            raise TranscriptError("视频没有人声，画面 OCR 也没有识别出文字")
        sections: list[str] = []
        description = normalize_transcript(description)
        if description:
            sections.append(f"【作品说明】\n{description}")
        sections.append(f"【静态视频画面文字】\n{recognized}")
        text = normalize_transcript("\n\n".join(sections))
        duration = max(0.0, float(duration_seconds or 0))
        return {
            "id": media_path.stem,
            "task": "video_ocr",
            "model": "Apple-Vision",
            "language": "zh",
            "duration": duration,
            "text": text,
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": duration,
                    "text": recognized,
                }
            ],
            "words": [],
            "processing_time": round(time.monotonic() - started_at, 3),
        }

    async def image_post_text(
        self, metadata: dict[str, Any], cookie_header: str = ""
    ) -> str | None:
        """Build the canonical text for an image post from its caption and every slide."""
        urls = image_urls(metadata)
        if not urls:
            return None
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.douyin.com/",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        sections: list[str] = []
        description = normalize_transcript(
            str(metadata.get("desc") or metadata.get("item_title") or "")
        )
        if description:
            sections.append(f"【作品说明】\n{description}")
        with tempfile.TemporaryDirectory(prefix="silo-image-ocr-") as tmp:
            tmp_dir = Path(tmp)
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                for index, url in enumerate(urls, start=1):
                    image_path = tmp_dir / f"{index:03d}.img"
                    try:
                        last_error: Exception | None = None
                        for attempt in range(3):
                            try:
                                response = await client.get(url, headers=headers)
                                response.raise_for_status()
                                image_path.write_bytes(response.content)
                                last_error = None
                                break
                            except Exception as exc:
                                last_error = exc
                                if attempt < 2:
                                    await asyncio.sleep(1.5 * (attempt + 1))
                        if last_error:
                            raise last_error
                        if image_path.stat().st_size < 256:
                            raise TranscriptError("下载到的图片文件异常")
                        recognized = self._ocr_image(image_path)
                    except Exception as exc:
                        if isinstance(exc, TranscriptError):
                            raise
                        raise TranscriptError(
                            f"第 {index} 张图片处理失败：{str(exc)[:200]}"
                        ) from exc
                    sections.append(
                        f"【图片 {index}/{len(urls)}】\n{recognized or '（本页未识别到文字）'}"
                    )
        text = normalize_transcript("\n\n".join(sections))
        if not text:
            raise TranscriptError("图文作品没有提取出任何文字")
        return text

    def transcribe(self, media_path: Path) -> str:
        payload = self.transcribe_details(media_path)
        text = normalize_transcript(str(payload.get("text") or ""))
        if not text:
            raise TranscriptError("ASR 没有识别出文字")
        return text

    def transcribe_details(self, media_path: Path) -> dict[str, Any]:
        """Call the private GPU ASR service and retain its timestamped response."""
        if not media_path.exists() or media_path.stat().st_size == 0:
            raise TranscriptError(f"待转写媒体不存在或为空：{media_path}")
        headers = {}
        if self.settings.asr_api_key:
            headers["X-API-Key"] = self.settings.asr_api_key
        try:
            with (
                media_path.open("rb") as media,
                httpx.Client(timeout=self.settings.asr_timeout_seconds) as client,
            ):
                response = client.post(
                    f"{self.settings.asr_base_url}/v1/audio/transcriptions",
                    headers=headers,
                    files={"file": (media_path.name, media, "application/octet-stream")},
                    data={
                        "language": self.settings.asr_language,
                        "timestamps": "true",
                        "response_format": "verbose_json",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = str(exc.response.json().get("detail") or "")
            except Exception:
                detail = exc.response.text[-300:]
            raise TranscriptError(
                f"ASR 服务返回 {exc.response.status_code}：{detail or '未知错误'}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise TranscriptError(f"ASR 服务调用失败：{str(exc)[:500]}") from exc
        if not isinstance(payload, dict) or not str(payload.get("text") or "").strip():
            raise TranscriptError("ASR 服务响应缺少完整文字")
        if not isinstance(payload.get("segments"), list) or not isinstance(
            payload.get("words"), list
        ):
            raise TranscriptError("ASR 服务响应缺少时间戳")
        return payload

    def diagnostic(self) -> dict[str, Any]:
        asr_ready = False
        asr_error = ""
        headers = {"X-API-Key": self.settings.asr_api_key} if self.settings.asr_api_key else {}
        try:
            response = httpx.get(f"{self.settings.asr_base_url}/ready", headers=headers, timeout=5)
            asr_ready = response.is_success and bool(response.json().get("model_loaded"))
            if not response.is_success:
                asr_error = f"HTTP {response.status_code}"
        except Exception as exc:
            asr_error = str(exc)[:200]
        return {
            "asr_base_url": self.settings.asr_base_url,
            "asr_ready": asr_ready,
            "asr_error": asr_error,
            "vision_ocr_ready": bool(shutil.which("swiftc")) or Path("/usr/bin/swiftc").exists(),
        }
