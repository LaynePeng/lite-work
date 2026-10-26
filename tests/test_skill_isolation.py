# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""技能子进程的包来源隔离守护。

背景（真实的「在我机器上能跑」风险）：随包技能解释器是**裸 CPython**，
`ENABLE_USER_SITE=True` 会把 `~/.local/lib/pythonX.Y/site-packages` 放进
sys.path。于是开发机 `pip3 install --user` 装过的任何包，都可能被技能脚本
悄悄读到 —— 本机全绿，换一台干净机器（同事 clone、CI、用户安装包）同一个
脚本直接 `ModuleNotFoundError`。

三条防线（缺一即重新敞开）：

1. 技能子进程带 `PYTHONNOUSERSITE=1`（`isolate_skill_env`）——覆盖
   ppt_edit 的 `_run_script` 与 ppt_images 的 `_spawn_env`；
2. 打包产物解释器的 `sitecustomize.py` 主动把 user site 移出 sys.path
   （`scripts/package-backend.mjs` 的 `SITECUSTOMIZE_SRC`）——兜住不经
   上述两条路径、直接调用解释器的脚本；
3. 开发态把 venv 解释器前置进 PATH（`use_builtin_skill_python`）——
   SKILL.md 里 7 个技能都写裸 `python3 ...`，不前置就命中系统解释器。

注意第 1 条**不能**加到 `execute_command` 的交互 shell：`PYTHONNOUSERSITE=1`
会让用户的 `pip install --user` 直接报错（`User site-packages are disabled`）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _sitecustomize_src() -> str:
    """从 package-backend.mjs 抽出随包 sitecustomize 的模板内容。"""
    text = (ROOT / "scripts" / "package-backend.mjs").read_text(encoding="utf-8")
    m = re.search(r"const SITECUSTOMIZE_SRC = `(.*?)`;", text, re.S)
    assert m, "package-backend.mjs 里找不到 SITECUSTOMIZE_SRC 模板"
    return m.group(1).replace("\\\\", "\\")


class TestIsolateSkillEnv:
    def test_sets_pythonnousersite(self):
        from litework.tools.skills import isolate_skill_env

        env: dict = {}
        assert isolate_skill_env(env)["PYTHONNOUSERSITE"] == "1"


class TestIsolateSkillEnvEntrypoints:
    def test_ppt_images_both_branches_isolated(self):
        """两条分支（调用方给 env / 现造 env）都要过隔离，漏一即敞开。"""
        from litework.tools import ppt_images

        assert ppt_images._spawn_env({"PATH": "/x"}, None)["PYTHONNOUSERSITE"] == "1"
        assert ppt_images._spawn_env(None, None)["PYTHONNOUSERSITE"] == "1"


class TestShellNotIsolated:
    def test_execute_command_does_not_disable_user_site(self):
        """交互 shell 不能设 PYTHONNOUSERSITE——会打断用户的 pip install --user。

        实测：设了之后 `pip install --user six` 直接
        `ERROR: Can not perform a '--user' install.`
        """
        from litework.tools.shell import SENSITIVE_ENV_VARS

        assert "PYTHONNOUSERSITE" not in SENSITIVE_ENV_VARS


class TestSitecustomizeTemplate:
    """随包 sitecustomize 必须同时做两件事，且不能互相破坏。"""

    def test_template_disables_user_site(self):
        src = _sitecustomize_src()
        assert "ENABLE_USER_SITE = False" in src
        assert "getusersitepackages" in src, "要拿到 user site 路径才能从 sys.path 移除"

    def test_template_still_injects_internal(self):
        src = _sitecustomize_src()
        assert "_internal" in src, "必须保留 _internal 注入（打包态依赖来源）"
        assert "_is_internal" in src

    def test_template_is_valid_python(self):
        """语法必须成立：模板里注释写错语法会直接搞崩构建。"""
        compile(_sitecustomize_src(), "sitecustomize.py", "exec")

    @pytest.mark.skipif(sys.platform == "win32", reason="路径语义按 POSIX 验证")
    def test_template_strips_user_site_and_keeps_internal(self, tmp_path):
        """实跑一次：user site 被移出 sys.path，_internal 仍被注入。"""
        app = tmp_path / "app"
        internal = app / "_internal"
        (internal / "skills").mkdir(parents=True)
        (internal / "pymupdf").mkdir(parents=True)
        sp = internal / "skill-python" / "lib" / "python3.14" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "sitecustomize.py").write_text(_sitecustomize_src(), encoding="utf-8")

        # 假 user site（含一个可识别包），并手动塞进 sys.path 模拟裸解释器默认
        user_site = tmp_path / "usersite"
        (user_site / "fakepkg_isolation").mkdir(parents=True)
        (user_site / "fakepkg_isolation" / "__init__.py").write_text("X=1\n")

        probe = textwrap.dedent(f"""
            import sys, site
            site.ENABLE_USER_SITE = True
            # 让 getusersitepackages() 返回这个假目录（模板按它定位并移除）
            site.getusersitepackages = lambda: {str(user_site)!r}
            sys.path.insert(0, {str(user_site)!r})
            sys.path.insert(0, {str(sp)!r})
            import sitecustomize as sc
            print("INSIDE", any({str(user_site)!r} == p for p in sys.path))
            print("ENABLE", site.ENABLE_USER_SITE)
            print("FOUND", sc._found or "")
        """)
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[:500]
        assert "INSIDE False" in r.stdout, f"user site 未被移出: {r.stdout}"
        assert "ENABLE False" in r.stdout
        assert str(internal) in r.stdout, f"_internal 未被注入: {r.stdout}"
