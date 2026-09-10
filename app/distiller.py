from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.artifact_integrity import (
    ArtifactIntegrityError,
    assert_no_unresolved_inline_code_tokens,
)
from app.config import Settings
from app.database import Database
from app.llm import LLMClient, LLMError

PROMPT_VERSION = "silo-distill-v2"


@dataclass
class DistillationResult:
    report_markdown: str
    skill_markdown: str
    analyzed_count: int


class Distiller:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient | None = None):
        self.settings = settings
        self.db = db
        self.llm = llm or LLMClient(settings)

    def _cache_identity(self, video: dict[str, Any]) -> tuple[str, str]:
        transcript = video["raw_transcript"]
        digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        cache_model = f"{self.settings.llm_model}|{PROMPT_VERSION}"
        return digest, cache_model

    async def _json_with_retry(self, system: str, user: str) -> dict[str, Any]:
        for attempt in range(4):
            try:
                return await self.llm.json(system, user)
            except LLMError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    async def _chat_with_retry(
        self, system: str, user: str, *, temperature: float = 0.2
    ) -> str:
        for attempt in range(4):
            try:
                return await self.llm.chat(system, user, temperature=temperature)
            except LLMError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    async def _map_video(self, video: dict[str, Any]) -> dict[str, Any]:
        transcript = video["raw_transcript"]
        digest, cache_model = self._cache_identity(video)
        cached = self.db.get_video_analysis(video["aweme_id"], digest, cache_model)
        if cached:
            return cached
        payload = await self._json_with_retry(
            "你是严谨的短视频内容研究员。只依据给定逐字稿分析，不虚构。输出合法 JSON。",
            f"""分析下面一条视频完整逐字稿。保留作者真实表达特征，不把主题词误判为口头禅。

标题：{video.get("title") or "无标题"}
发布时间：{video.get("publish_time")}
点赞数：{video.get("like_count", 0)}
完整逐字稿：
{transcript}

严格输出以下 JSON 字段：
{{
  "hook": {{"type": "", "text": "", "mechanism": ""}},
  "core_argument": "",
  "reasoning_steps": [""],
  "emotional_tone": [""],
  "catchphrases": [""],
  "sentence_patterns": [""],
  "rhetorical_devices": [""],
  "values": [""],
  "topic_tags": [""]
}}
保持精炼：core_argument 不超过 60 字；reasoning_steps 最多 3 项；其余数组最多 3 项；
hook 的 text 和 mechanism 各不超过 40 字；不得重复逐字稿。""",
        )
        self.db.save_video_analysis(video["aweme_id"], digest, cache_model, payload)
        return payload

    async def _map_batch(
        self, indexed_videos: list[tuple[int, dict[str, Any]]]
    ) -> list[tuple[int, dict[str, Any]]]:
        """Analyze several transcripts per request, then cache every item independently."""
        source = [
            {
                "aweme_id": video["aweme_id"],
                "title": video.get("title") or "无标题",
                "publish_time": video.get("publish_time"),
                "like_count": video.get("like_count", 0),
                "transcript": video["raw_transcript"],
            }
            for _, video in indexed_videos
        ]
        try:
            payload = await self._json_with_retry(
                "你是严谨的短视频内容研究员。逐条独立分析，只依据逐字稿，不虚构。输出合法 JSON。",
                """逐条分析下面的完整逐字稿。必须为每个 aweme_id 返回且只返回一项，不得合并作品。
严格输出：
{
  "analyses": [
    {
      "aweme_id": "原值",
      "analysis": {
        "hook": {"type": "", "text": "", "mechanism": ""},
        "core_argument": "",
        "reasoning_steps": [""],
        "emotional_tone": [""],
        "catchphrases": [""],
        "sentence_patterns": [""],
        "rhetorical_devices": [""],
        "values": [""],
        "topic_tags": [""]
      }
    }
  ]
}

保持精炼：每条 core_argument 不超过 60 字；reasoning_steps 最多 3 项；
其余数组最多 3 项；hook 的 text 和 mechanism 各不超过 40 字；不得重复逐字稿。

作品数据：
"""
                + json.dumps(source, ensure_ascii=False),
            )
            returned = payload.get("analyses")
            if not isinstance(returned, list):
                raise LLMError("批量分析没有返回 analyses 数组")
            by_id = {
                str(item.get("aweme_id")): item.get("analysis")
                for item in returned
                if isinstance(item, dict) and isinstance(item.get("analysis"), dict)
            }
        except LLMError:
            # A malformed batch must not kill the entire creator version. Retry each
            # item independently so completed caches remain usable.
            results = []
            for index, video in indexed_videos:
                results.append((index, await self._map_video(video)))
            return results

        results: list[tuple[int, dict[str, Any]]] = []
        missing: list[tuple[int, dict[str, Any]]] = []
        for index, video in indexed_videos:
            analysis = by_id.get(str(video["aweme_id"]))
            if not analysis:
                missing.append((index, video))
                continue
            digest, cache_model = self._cache_identity(video)
            self.db.save_video_analysis(video["aweme_id"], digest, cache_model, analysis)
            results.append((index, analysis))
        if missing:
            fallback = await asyncio.gather(*(self._map_video(video) for _, video in missing))
            results.extend(
                zip([index for index, _ in missing], fallback, strict=True)
            )
        return results

    async def distill(
        self,
        creator: dict[str, Any],
        videos: list[dict[str, Any]],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> DistillationResult:
        usable = [video for video in videos if video.get("transcript_status") == "COMPLETED"]
        if not usable:
            raise RuntimeError("没有可用于蒸馏的完整逐字稿")
        analyses: list[dict[str, Any] | None] = [None] * len(usable)
        pending: list[tuple[int, dict[str, Any]]] = []
        completed = 0
        for index, video in enumerate(usable):
            digest, cache_model = self._cache_identity(video)
            cached = self.db.get_video_analysis(video["aweme_id"], digest, cache_model)
            if cached:
                analyses[index] = cached
                completed += 1
            else:
                pending.append((index, video))
        if progress and completed:
            progress(completed, len(usable), f"已复用 {completed}/{len(usable)} 条分析缓存")

        batches: list[list[tuple[int, dict[str, Any]]]] = []
        batch: list[tuple[int, dict[str, Any]]] = []
        batch_chars = 0
        for item in pending:
            chars = len(str(item[1].get("raw_transcript") or ""))
            if batch and (len(batch) >= 4 or batch_chars + chars > 30000):
                batches.append(batch)
                batch = []
                batch_chars = 0
            batch.append(item)
            batch_chars += chars
        if batch:
            batches.append(batch)

        semaphore = asyncio.Semaphore(min(4, max(2, self.settings.transcription_workers)))

        async def limited(items: list[tuple[int, dict[str, Any]]]):
            async with semaphore:
                return await self._map_batch(items)

        tasks = [asyncio.create_task(limited(items)) for items in batches]
        for task in asyncio.as_completed(tasks):
            results = await task
            for index, analysis in results:
                analyses[index] = analysis
            completed += len(results)
            if progress:
                progress(
                    completed,
                    len(usable),
                    f"已分析 {completed}/{len(usable)} 条完整逐字稿",
                )

        mapped = [analysis for analysis in analyses if analysis is not None]
        if len(mapped) != len(usable):
            raise RuntimeError("部分逐字稿没有生成分析结果")

        bundles = await self._reduce_bundles(mapped)
        evidence = json.dumps(bundles, ensure_ascii=False, indent=2)
        nickname = creator["nickname"]
        coverage = f"共分析 {len(usable)} 条完整逐字稿"
        report_task = asyncio.create_task(
            self._chat_with_retry(
                "你是内容研究与知识建模专家。报告必须基于证据，区分稳定模式和偶发现象。",
                f"""根据博主【{nickname}】的全量逐稿分析数据撰写中文 Markdown 深度报告。
覆盖范围：{coverage}。

分析数据：
{evidence}

报告必须包含：
1. 数据范围与方法；2. 一句话人设；3. 核心议题和价值观；4. 高频开篇钩子；
5. 论证与叙事结构；6. 语言指纹；7. 情绪和节奏；8. 高赞内容规律；
9. 可复用创作公式；10. 可能的偏差与不要模仿的表面特征。
引用示例时只引用分析数据里真实存在的短句，不得虚构原话。
普通中文概念使用中文引号或加粗，不要使用反引号；反引号只用于真正的代码标识符。
禁止输出 INLINECODE、INLINE_CODE 或其他内部占位符。只输出 Markdown 正文。""",
                temperature=0.2,
            )
        )
        slug = re.sub(r"[^a-z0-9-]+", "-", nickname.lower()).strip("-")
        if not slug:
            slug = f"creator-{hashlib.sha1(creator['creator_id'].encode()).hexdigest()[:8]}"
        skill_task = asyncio.create_task(
            self._chat_with_retry(
                "你是 Codex Agent Skill 架构师。生成可执行、具体、可检验的写作 Skill。",
                f"""为博主【{nickname}】生成一份可直接使用的 SKILL.md。
依据是对 {len(usable)} 条完整逐字稿的全量分析：
{evidence}

必须以如下 YAML frontmatter 开头：
---
name: {slug}-style
description: 使用博主【{nickname}】的认知框架、论证方式和语言节奏创作短视频文案。
---

正文必须包含：适用场景、角色与世界观、选题判断、开篇钩子公式、论证步骤、
语言指纹、篇幅和节奏、创作工作流、自检清单、负向约束。
要求模型学习结构和思考方式，不冒充真人，不编造其经历、背书或观点。
只输出完整 Markdown，不要使用包裹全文的代码围栏。
普通中文概念不要使用反引号，禁止输出 INLINECODE、INLINE_CODE 或其他内部占位符。""",
                temperature=0.2,
            )
        )
        report, skill = await asyncio.gather(report_task, skill_task)
        report = self._strip_outer_fence(report)
        skill = self._strip_outer_fence(skill)
        try:
            assert_no_unresolved_inline_code_tokens(report, "分析报告")
            assert_no_unresolved_inline_code_tokens(skill, "SKILL.md")
        except ArtifactIntegrityError as exc:
            raise LLMError(str(exc)) from exc
        if not skill.startswith("---\n"):
            skill = (
                f"---\nname: {slug}-style\n"
                f"description: 使用博主【{nickname}】的认知框架、论证方式"
                "和语言节奏创作短视频文案。\n"
                f"---\n\n{skill}"
            )
        return DistillationResult(report.strip(), skill.strip(), len(usable))

    @staticmethod
    def _strip_outer_fence(text: str) -> str:
        match = re.fullmatch(r"\s*```(?:markdown|md)?\s*\n(.*)\n```\s*", text, re.S | re.I)
        return match.group(1).strip() if match else text.strip()

    async def _reduce_bundles(self, analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(json.dumps(analyses, ensure_ascii=False)) <= 50000:
            return analyses
        chunks = [analyses[start : start + 25] for start in range(0, len(analyses), 25)]
        semaphore = asyncio.Semaphore(min(4, max(2, self.settings.transcription_workers)))

        async def reduce_chunk(chunk: list[dict[str, Any]]) -> dict[str, Any]:
            async with semaphore:
                return await self._json_with_retry(
                    "你负责合并多条内容分析。保留频次、差异与代表性证据，输出合法 JSON。",
                    "将以下分析合并为一个批次画像，输出 persona、topics、hooks、reasoning、"
                    "language、values、outliers 字段：\n"
                    + json.dumps(chunk, ensure_ascii=False),
                )

        return await asyncio.gather(*(reduce_chunk(chunk) for chunk in chunks))
