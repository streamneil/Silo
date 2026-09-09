from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        return headers

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 6000,
        response_format: dict[str, str] | None = None,
    ) -> str:
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # The production llama.cpp model defaults to xhigh reasoning. These
            # corpus extraction calls need faithful structured output, not long
            # hidden deliberation that consumes the token budget and can leave
            # message.content empty.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.llm_base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise LLMError("模型返回了空内容")
                return content.strip()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"模型调用失败：{str(exc)[:300]}") from exc

    async def json(self, system: str, user: str) -> dict[str, Any]:
        content = await self.chat(
            system,
            user,
            temperature=0.1,
            max_tokens=8000,
            response_format={"type": "json_object"},
        )
        try:
            return self._parse_json_object(content)
        except LLMError as first_error:
            repaired = await self.chat(
                "你是 JSON 修复器。只返回一个语法完整的 JSON 对象，不添加解释。",
                "修复下面被截断或格式错误的 JSON；保留已有信息并补齐括号和字符串：\n"
                + content[:16000],
                temperature=0,
                max_tokens=8000,
                response_format={"type": "json_object"},
            )
            try:
                return self._parse_json_object(repaired)
            except LLMError as second_error:
                raise LLMError(f"模型连续两次没有返回合法 JSON：{second_error}") from first_error

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.S)
        candidate = fenced.group(1) if fenced else content
        if not candidate.lstrip().startswith("{"):
            start, end = candidate.find("{"), candidate.rfind("}")
            if start >= 0 and end > start:
                candidate = candidate[start : end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型没有返回合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise LLMError("模型 JSON 顶层必须是对象")
        return value

    async def diagnostic(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.settings.llm_base_url}/models", headers=self._headers()
                )
                response.raise_for_status()
                models = [item.get("id") for item in response.json().get("data", [])]
            return {
                "ready": True,
                "base_url": self.settings.llm_base_url,
                "configured_model": self.settings.llm_model,
                "available_models": models,
            }
        except Exception as exc:
            return {
                "ready": False,
                "base_url": self.settings.llm_base_url,
                "configured_model": self.settings.llm_model,
                "error": str(exc)[:200],
            }
