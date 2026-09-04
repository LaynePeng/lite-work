"""JSON 容错解析（对应课程第2课 safeJsonParse）。

解析失败时绝不 crash，而是返回错误信息，由 AgentLoop 回填给 LLM 自愈。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def safe_json_parse(json_string: str) -> Tuple[bool, Any, str]:
    """尝试解析 JSON，返回 (success, data, error)。"""
    try:
        return True, json.loads(json_string), ""
    except Exception as first_err:
        cleaned = _FENCE_RE.sub("", json_string).strip()
        try:
            return True, json.loads(cleaned), ""
        except Exception:
            return (
                False,
                None,
                f'JSON Parse Failed: {first_err}. Raw output was: "{json_string[:500]}". '
                "请将参数格式化为合法 JSON。",
            )