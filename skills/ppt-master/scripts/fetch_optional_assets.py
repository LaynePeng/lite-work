#!/usr/bin/env python3
"""按需补全 ppt-master 被精简收录剔除的可选资源目录。

lite-work-plugins 社区仓库收录 ppt-master 时为控制整仓克隆体积，
剔除了三个非运行时必需的资源目录（详见仓库 scripts/sync_ppt_master.py）：
- references/ai-image-comparison/   AI 生图风格/配色/构图参考图（约 43MB）
- templates/sounds/{bigsoundbank,kenney-interface,kenney-ui}/ + sounds_index.json
                                    视频旁白音效库（约 12MB）

用到对应功能（AI 生图策略确认、视频音效）时运行本脚本，从上游
hugohe3/ppt-master 官方 Releases 下载同版本技能包并只提取这些目录。

用法：
    python3 scripts/fetch_optional_assets.py                 # 自动读 SKILL.md 版本
    python3 scripts/fetch_optional_assets.py --version 6.4.0
    python3 scripts/fetch_optional_assets.py --zip /path/to/ppt-master-skill-v6.4.0.zip
                                            # 已手动下载时离线补全（国内网络推荐）
仅使用 Python 标准库。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
UPSTREAM_REPO = "https://github.com/hugohe3/ppt-master"
GUARD = SKILL_DIR / "scripts" / "attribution_guard.py"

# 需要补回的路径（目录带尾斜杠做前缀匹配）
RESTORE_DIR_PREFIXES = (
    "references/ai-image-comparison/",
    "templates/sounds/bigsoundbank/",
    "templates/sounds/kenney-interface/",
    "templates/sounds/kenney-ui/",
)
RESTORE_FILES = ("templates/sounds/sounds_index.json",)


def _skill_version() -> str:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    top = re.search(r'(?m)^version:\s*"?([^\s"]+)', text)
    if top:
        return top.group(1)
    nested = re.search(r'(?m)^\s+version:\s*"?([^\s"]+)', text)
    if nested:
        return nested.group(1)
    raise SystemExit("无法从 SKILL.md 解析版本号，请用 --version 显式指定")


def _release_zip_url(version: str) -> str:
    direct = (
        f"{UPSTREAM_REPO}/releases/download/v{version}/"
        f"ppt-master-skill-v{version}.zip"
    )
    req = urllib.request.Request(direct, method="HEAD",
                                 headers={"User-Agent": "lite-work-plugins"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                return direct
    except OSError:
        pass
    api = f"https://api.github.com/repos/hugohe3/ppt-master/releases/tags/v{version}"
    req = urllib.request.Request(api, headers={"User-Agent": "lite-work-plugins"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.startswith("ppt-master-skill-") and name.endswith(".zip"):
            return asset["browser_download_url"]
    raise SystemExit(f"上游 Releases 未找到 v{version} 的技能包资产")


def _download(url: str, dest: Path) -> None:
    print(f"下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "lite-work-plugins"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    print(f"已下载 {dest}（{dest.stat().st_size // 1048576}MB）")


def _common_prefix(names: list[str]) -> str:
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[: prefix.rfind("/") + 1]
    return prefix if prefix.endswith("/") else ""


def _restore_targets(names: list[str]) -> list[tuple[str, str]]:
    prefix = _common_prefix([n for n in names if not n.endswith("/")])
    restored = []
    for name in names:
        if name.endswith("/"):
            continue
        rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
        rel = rel.lstrip("/")
        if rel.startswith(RESTORE_DIR_PREFIXES) or rel in RESTORE_FILES:
            restored.append((name, rel))
    return restored


def _extract(zf: zipfile.ZipFile, entries: list[tuple[str, str]]) -> int:
    count = 0
    for name, rel in entries:
        dest = (SKILL_DIR / rel).resolve()
        if not str(dest).startswith(str(SKILL_DIR.resolve())):
            raise SystemExit(f"zip 条目路径越界: {name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从上游 Releases 补全被精简收录剔除的可选资源（AI 生图参考图 / 音效库）")
    parser.add_argument("--version", help="指定版本（默认读 SKILL.md）")
    parser.add_argument("--zip", help="本地已下载的官方技能包 zip 路径（离线补全）")
    parser.add_argument("--list", action="store_true",
                        help="仅列出将补全的条目，不实际写入")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="ppt-master-assets-"))
    try:
        zip_path = Path(args.zip).expanduser() if args.zip else None
        if zip_path is None:
            version = args.version or _skill_version()
            zip_path = tmp / f"ppt-master-skill-v{version}.zip"
            _download(_release_zip_url(version), zip_path)
        elif not zip_path.is_file():
            raise SystemExit(f"文件不存在: {zip_path}")

        with zipfile.ZipFile(zip_path) as zf:
            entries = _restore_targets(zf.namelist())
            if not entries:
                raise SystemExit("技能包内未找到待补全的资源条目（目录结构可能已变化，"
                                 "请核对上游打包脚本）")
            if args.list:
                for _, rel in entries:
                    print(rel)
                return 0
            count = _extract(zf, entries)
        print(f"已补全 {count} 个文件到 {SKILL_DIR}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = subprocess.run([sys.executable, str(GUARD)])
    if result.returncode != 0:
        print("警告：补全后完整性校验未通过（exit "
              f"{result.returncode}），请检查技能目录", file=sys.stderr)
        return result.returncode
    print("完整性校验通过（attribution_guard exit 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
