from __future__ import annotations

import re

UNRESOLVED_INLINE_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:__)?(?:SILO)?INLINE_?CODE_?\d+(?:TOKEN|__)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


class ArtifactIntegrityError(ValueError):
    pass


def assert_no_unresolved_inline_code_tokens(text: str, artifact_name: str) -> None:
    match = UNRESOLVED_INLINE_CODE_RE.search(text)
    if match:
        raise ArtifactIntegrityError(
            f"{artifact_name} 含未恢复的 Markdown 内联代码占位符：{match.group(0)}"
        )
