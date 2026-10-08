# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""完成证据门禁（Completion Evidence Gate）。

设计对齐《AI Native 研发范式实践手册》的 Guardrail（pp. 60-63），但只保留
**可机械执行**的部分——门禁不解释业务语义，只做协议约束与聚合：

1. 规则版本化：`GateSpec` 有稳定 digest，运行绑定当时选中的版本（历史判断依据
   不因后续改规则而改变）；
2. 每个必检项提交**三态**结果：`pass` / `blocked` / `unknown`，且必须带 Evidence
   （来源、观测时间、范围、摘要或失败原因）；
3. **机械聚合**：必检项缺失、或存在 `blocked`/`unknown` → 不得形成可放行结果；
   `UNKNOWN` 绝不能被当作 `PASS`。

与手册的差异（有意为之，落地取舍）：
- 配置用 JSON（`.litework/gate.json`）而非 YAML：避免为配置引入新依赖（pyyaml
  当前只是传递依赖，未在 pyproject 声明）；
- 门禁结果只用于**保守**判定（不通过 → 提醒模型补证据 / 记录事件），不做业务
  语义解释，也不替代人工审批。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STATUSES = ("pass", "blocked", "unknown")

# Evidence 上限与脱敏：门禁只存"摘要 + 引用"，不复制凭证与大段数据
EVIDENCE_MAX_CHARS = 500
_TOKEN_PATTERNS = [
    re.compile(r"\b(sk|pk|ghp|gho|glpat|xox[baprs])[-_][A-Za-z0-9_\-]{12,}"),  # 常见 API Key
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),                                   # 超长不透明串
]

# 默认规则：两条必检项，覆盖"改了东西"与"验过没有"
DEFAULT_SPEC: Dict[str, Any] = {
    "version": 1,
    "spec_id": "litework-completion-v1",
    "checks": [
        {
            "id": "changed-files",
            "required": True,
            "criteria": {
                "pass": "改动全部落在工作区内",
                "blocked": "存在工作区外路径",
                "unknown": "没有记录到改动",
            },
            "evidence": ["paths", "source"],
        },
        {
            "id": "verify-commands",
            "required": True,
            "criteria": {
                "pass": "每条验证命令的退出码均为 0",
                "blocked": "存在非 0 退出码",
                "unknown": "没有执行过验证命令，或输出无法解析",
            },
            "evidence": ["command", "exit_code", "captured_at"],
        },
    ],
}


def redact_evidence(text: str) -> str:
    """Evidence 摘要化：截断 + 脱敏（凭证不进日志/事件/快照）。"""
    out = str(text or "").replace("\r", " ").strip()
    for pat in _TOKEN_PATTERNS:
        out = pat.sub("[redacted]", out)
    if len(out) > EVIDENCE_MAX_CHARS:
        out = out[:EVIDENCE_MAX_CHARS] + "…"
    return out


@dataclass
class CheckSpec:
    id: str
    required: bool = True
    criteria: Dict[str, str] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)


@dataclass
class Submission:
    """一次检查项提交：三态结果 + Evidence。"""

    check_id: str
    status: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "message": redact_evidence(self.message),
            "evidence": {k: redact_evidence(str(v)) for k, v in self.evidence.items()},
        }


@dataclass
class GateSpec:
    spec_id: str
    version: int
    checks: List[CheckSpec]

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "GateSpec":
        checks = [
            CheckSpec(
                id=str(c.get("id") or "").strip(),
                required=bool(c.get("required", True)),
                criteria={str(k): str(v) for k, v in (c.get("criteria") or {}).items()},
                evidence=[str(e) for e in (c.get("evidence") or [])],
            )
            for c in (raw.get("checks") or [])
            if isinstance(c, dict) and str(c.get("id") or "").strip()
        ]
        return cls(
            spec_id=str(raw.get("spec_id") or "litework-completion"),
            version=int(raw.get("version") or 1),
            checks=checks,
        )

    @property
    def digest(self) -> str:
        """规则内容的稳定摘要（sha256 前 16 位）：运行绑定固定版本用。"""
        canonical = json.dumps(
            {
                "spec_id": self.spec_id,
                "version": self.version,
                "checks": [
                    {"id": c.id, "required": c.required, "criteria": c.criteria,
                     "evidence": c.evidence}
                    for c in self.checks
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def check_ids(self) -> List[str]:
        return [c.id for c in self.checks]


def load_spec(workspace: Optional[str], relative_path: str = ".litework/gate.json") -> GateSpec:
    """读项目规则；缺失/损坏 → 默认规则（宁可默认放行语义，也不让门禁因配置失败而卡死）。"""
    if workspace:
        path = os.path.join(workspace, relative_path)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw.get("checks"):
                return GateSpec.from_dict(raw)
        except (OSError, json.JSONDecodeError):
            pass
    return GateSpec.from_dict(DEFAULT_SPEC)


def aggregate(spec: GateSpec, submissions: Dict[str, Submission]) -> Dict[str, Any]:
    """机械聚合（不做语义判断）：

    - 必检项（required）缺失 → 整体 `unknown`（缺失不等于通过）；
    - 任一提交为 `blocked` → 整体 `blocked`；
    - 否则任一必检项 `unknown`（含非法状态）→ 整体 `unknown`；
    - 全部必检项 `pass` → `pass`。
    非必检项缺失/未通过**不影响**整体结论（其状态仍如实列出，供人/插件参考）。
    """
    checks: List[Dict[str, Any]] = []
    verdict = "pass"
    for check in spec.checks:
        sub = submissions.get(check.id)
        if sub is None:
            status = "unknown"
            entry: Dict[str, Any] = {"id": check.id, "required": check.required,
                                     "status": status, "message": "未提交（缺失）",
                                     "evidence": {}}
        else:
            status = sub.status if sub.status in STATUSES else "unknown"
            entry = {"id": check.id, "required": check.required, "status": status,
                     "message": redact_evidence(sub.message),
                     "evidence": {k: redact_evidence(str(v)) for k, v in sub.evidence.items()}}
        checks.append(entry)
        if not check.required:
            continue
        if status == "blocked":
            verdict = "blocked"
        elif status == "unknown" and verdict != "blocked":
            verdict = "unknown"
    return {
        "verdict": verdict,
        "spec_id": spec.spec_id,
        "spec_version": spec.version,
        "spec_digest": spec.digest,
        "checks": checks,
        "at": time.time(),
        "mode": None,
        "opinion": None,
    }


# ------------------------------------------------------------ 事实 → 检查项提交

_EXIT_CODE_RE = re.compile(r"\[Exit Code\]:\s*(-?\d+)")


def parse_exit_code(result_text: str) -> Optional[int]:
    """从工具结果里取最后一条退出码（execute_command 的输出格式）。"""
    codes = _EXIT_CODE_RE.findall(result_text or "")
    if not codes:
        return None
    try:
        return int(codes[-1])
    except ValueError:
        return None


def parse_exit_codes(result_text: str) -> List[int]:
    """取结果文本里的**全部**退出码（动作融合的 then 段可能含多条）。"""
    out: List[int] = []
    for raw in _EXIT_CODE_RE.findall(result_text or ""):
        try:
            out.append(int(raw))
        except ValueError:
            continue
    return out


def derive_submissions(traces: Dict[str, Any]) -> Dict[str, Submission]:
    """把执行事实（命令退出码、改动文件）映射为默认规则的两个检查项提交。

    事实由 loop 收集（不依赖模型自述）：这比"让模型自己声称验过了"更可靠。
    自定义规则里的其它 check id 不在此映射 → 保持缺失（整体 unknown），
    直到有对应的提交来源。
    """
    submissions: Dict[str, Submission] = {}

    commands: List[Dict[str, Any]] = list(traces.get("commands") or [])
    if commands:
        codes = [c.get("exit_code") for c in commands]
        failed = [c for c in commands if isinstance(c.get("exit_code"), int)
                  and c["exit_code"] != 0]
        unparsed = [c for c in commands if c.get("exit_code") is None]
        if failed:
            status = "blocked"
        elif unparsed and len(unparsed) == len(commands):
            status = "unknown"
        else:
            status = "pass"
        submissions["verify-commands"] = Submission(
            check_id="verify-commands",
            status=status,
            message=(f"{len(commands)} 条命令，退出码 {codes}"
                     + (f"；失败 {len(failed)} 条" if failed else "")),
            evidence={
                "command": "; ".join(str(c.get("command", "")) for c in commands[-3:]),
                "exit_code": ",".join(str(c.get("exit_code")) for c in commands[-3:]),
                "captured_at": "; ".join(str(c.get("captured_at", "")) for c in commands[-3:]),
            },
        )

    changed: List[str] = list(traces.get("changed_files") or [])
    if changed:
        outside = [p for p in changed if str(p).startswith("../") or os.path.isabs(str(p))]
        submissions["changed-files"] = Submission(
            check_id="changed-files",
            status="blocked" if outside else "pass",
            message=f"改动 {len(changed)} 个文件" + (f"；越界 {len(outside)} 个" if outside else ""),
            evidence={"paths": ", ".join(changed[-10:]), "source": "write-tools"},
        )

    return submissions
