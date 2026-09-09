# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""版本号单一事实源守护：npm 侧文件版本必须与 litework/__version__ 一致。

改版只改 litework/__init__.py；构建入口自动跑 scripts/sync-version.mjs，
本测试保证忘跑同步时 CI 直接红。
"""
from __future__ import annotations

import json
import os

import litework

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_python_version_is_pure_semver():
    # 动态版本要求：不含空格等非法字符（pyproject attr 直读）
    v = litework.__version__
    assert isinstance(v, str) and v.strip() == v and " " not in v


def test_npm_versions_match_python_version():
    targets = ["package.json", "web/package.json", "package-lock.json", "web/package-lock.json"]
    for t in targets:
        path = os.path.join(ROOT, t)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("version") == litework.__version__, (
            f"{t} 版本 {data.get('version')} 与 litework.__version__ "
            f"{litework.__version__} 不一致——请运行 node scripts/sync-version.mjs"
        )


def test_no_stale_version_literals_in_source():
    """Python/Electron 源码不允许再出现版本字面量（单一事实源原则）。"""
    import re

    checks = {
        os.path.join(ROOT, "litework", "cli.py"): r'VERSION\s*=\s*["\']\d',
        os.path.join(ROOT, "litework", "server", "app.py"): r'VERSION\s*=\s*["\']\d',
        os.path.join(ROOT, "litework", "tools", "web.py"): r'USER_AGENT\s*=.*["\']\d+\.\d+',
        os.path.join(ROOT, "electron", "preload.js"): r'version.*["\']\d+\.\d+',
    }
    for path, pattern in checks.items():
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert not re.search(pattern, content), (
            f"{os.path.relpath(path, ROOT)} 出现硬编码版本字面量，应引用 __version__ / app.getVersion()"
        )