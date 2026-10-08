# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""项目运行环境清单（W5，对齐手册 pp.52-55「环境即第一类上下文」）。

解决什么：交付了仓库 ≠ 交付了能跑的项目。工具链、安装命令、验证命令、
数据基线——这些信息目前全靠用户手打或 Agent 猜（然后猜错）。

设计取舍：
- 配置用 JSON（`.litework/project.json`），不引 pyyaml（同 gate.json 的理由：
  为配置文件加依赖不值得，尤其 pyyaml 目前只是传递依赖）；
- 加载失败一律回退"无清单"——**不因配置坏掉而卡任务**；
- prompt 注入只给**命令列表**（紧凑摘要），不灌全文（token 考虑）；
- `verify` 命令直接喂给 W2 完成门禁作为默认验证源（两个特性互补而非重复）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 环境清单文件位置（项目根下 .litework/ 目录）
PROJECT_MANIFEST_RELPATH = ".litework/project.json"

#: 已知的项目类型检测规则（按优先级排列；命中多个时全部纳入 setup/verify）
_DETECTORS: List[Dict[str, Any]] = [
    {
        "id": "python",
        "files": ["pyproject.toml", "setup.py", "requirements.txt"],
        "setup": ["pip install -e '.[dev]'", "pip install -r requirements.txt"],
        "verify": ["python -m pytest -q"],
    },
    {
        "id": "node",
        "files": ["package.json"],
        "setup": ["npm install"],
        "verify": ["npm test"],
    },
    {
        "id": "rust",
        "files": ["Cargo.toml"],
        "setup": [],
        "verify": ["cargo test"],
    },
    {
        "id": "go",
        "files": ["go.mod"],
        "setup": [],
        "verify": ["go test ./..."],
    },
]


def manifest_path(workspace: str) -> Path:
    return Path(workspace) / PROJECT_MANIFEST_RELPATH


def load_runtime_manifest(workspace: Optional[str]) -> Optional[Dict[str, Any]]:
    """读环境清单；缺失/损坏/非法返回 None（绝不因配置问题卡任务）。"""
    if not workspace:
        return None
    path = manifest_path(workspace)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _normalize(raw)


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """校验与归一化：宽容解析（字段缺失/类型不对 → 默认空值），不抛错。"""
    def _str_list(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if isinstance(x, str) and x.strip()]

    _nw = raw.get("network")
    network_raw: Dict[str, Any] = _nw if isinstance(_nw, dict) else {}
    return {
        "version": int(raw.get("version") or 1),
        "runtime": {str(k): str(v) for k, v in (raw.get("runtime") or {}).items()}
        if isinstance(raw.get("runtime"), dict) else {},
        "setup": _str_list(raw.get("setup")),
        "verify": _str_list(raw.get("verify")),
        "network": {
            "allow": _str_list(network_raw.get("allow")),
            "default": str(network_raw.get("default") or "allow"),
        },
        "artifacts": _str_list(raw.get("artifacts")),
        "notes": str(raw.get("notes") or ""),
    }

def generate_runtime_manifest(workspace: str) -> Dict[str, Any]:
    """按项目类型检测产出初版清单（**不写盘**——由调用方决定写入时机）。

    检测逻辑：扫描 workspace 根的特征文件，命中则合并对应类型的 setup/verify。
    什么都没检测到 → 返回带空命令的骨架（用户/Agent 后续补充）。
    """
    ws = Path(workspace)
    detected: List[Dict[str, Any]] = []
    for detector in _DETECTORS:
        if any((ws / f).is_file() for f in detector["files"]):
            detected.append(detector)

    setup: List[str] = []
    verify: List[str] = []
    runtime: Dict[str, str] = {}
    for d in detected:
        for cmd in d["setup"]:
            if cmd not in setup:
                setup.append(cmd)
        for cmd in d["verify"]:
            if cmd not in verify:
                verify.append(cmd)

    if any(d["id"] == "python" for d in detected):
        runtime["python"] = "detected"
    if any(d["id"] == "node" for d in detected):
        runtime["node"] = "detected"

    return {
        "version": 1,
        "runtime": runtime,
        "setup": setup,
        "verify": verify,
        "network": {"allow": [], "default": "allow"},
        "artifacts": [],
        "notes": f"由检测生成（{' + '.join(d['id'] for d in detected) or '未检测到已知类型'}）；请按实际项目补充/修正",
    }


def save_runtime_manifest(workspace: str, manifest: Dict[str, Any]) -> bool:
    """写入环境清单；已存在时不覆盖（返回 False）。"""
    path = manifest_path(workspace)
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return True
    except OSError:
        return False


def runtime_summary_for_prompt(manifest: Optional[Dict[str, Any]]) -> str:
    """给 system prompt 稳定层用的紧凑摘要（只给命令，不灌全文）。

    无清单 → 空串（不注入任何内容，prompt 无变化）。
    """
    if not manifest:
        return ""
    parts: List[str] = []
    if manifest.get("setup"):
        cmds = "；".join(f"`{c}`" for c in manifest["setup"])
        parts.append(f"- **安装**：{cmds}")
    if manifest.get("verify"):
        cmds = "；".join(f"`{c}`" for c in manifest["verify"])
        parts.append(f"- **验证**：{cmds}")
    if not parts:
        return ""
    return (
        "\n\n### 项目环境清单 (Runtime Manifest)\n"
        "以下命令来自 `.litework/project.json`（项目声明的运行契约）：\n"
        + "\n".join(parts)
        + "\n任务涉及验证时**优先使用上述验证命令**（而非自行猜测）。"
    )


def verify_commands(manifest: Optional[Dict[str, Any]]) -> List[str]:
    """给 W2 完成门禁用的默认验证命令列表。"""
    if not manifest:
        return []
    return list(manifest.get("verify") or [])
