# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""安全卫士测试（对应第18课（安全沙箱实战）：三级风险 + 动态黑白名单 + 路径拦截）。"""
import pytest

from litework.security.guard import SecurityGuard, ThreatLevel
from litework.llm.base import decode_utf8_incremental
from litework.tools.filesystem import FileSystemTools


def test_high_risk_rm_rf():
    r = SecurityGuard().check_shell_command("rm -rf /")
    assert r.level == ThreatLevel.HIGH


def test_high_risk_macos_system_commands():
    guard = SecurityGuard()
    assert guard.check_shell_command("csrutil disable").level == ThreatLevel.HIGH
    assert guard.check_shell_command("diskutil eraseDisk JHFS+ X disk2").level == ThreatLevel.HIGH
    assert guard.check_shell_command("rm -rf ~").level == ThreatLevel.HIGH
    assert guard.check_shell_command("rm -rf $HOME").level == ThreatLevel.HIGH


def test_medium_risk_macos_commands():
    guard = SecurityGuard()
    assert guard.check_shell_command("fdesetup remove").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("nvram -d boot-args").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("security delete-generic-password -s svc").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("osascript -e 'tell app \"Finder\" to quit'").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("softwareupdate --install -a").level == ThreatLevel.MEDIUM


def test_high_risk_fork_bomb_and_mkfs():
    guard = SecurityGuard()
    assert guard.check_shell_command("mkfs.ext4 /dev/sda1").level == ThreatLevel.HIGH
    assert guard.check_shell_command(":(){ :|:& };:").level == ThreatLevel.HIGH


def test_medium_risk_rm_and_sudo():
    guard = SecurityGuard()
    assert guard.check_shell_command("rm hello.txt").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("sudo apt install git").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("kill -9 1234").level == ThreatLevel.MEDIUM


def test_safe_command():
    guard = SecurityGuard()
    assert guard.check_shell_command("ls -la").level == ThreatLevel.SAFE
    assert guard.check_shell_command("git status").level == ThreatLevel.SAFE


def test_whitelist_prefix_bypasses_medium():
    # "git status" 白名单放行，即便其前缀不是中危也应为 SAFE
    guard = SecurityGuard()
    assert guard.check_shell_command("git status --short").level == ThreatLevel.SAFE


def test_whitelist_prefix_cannot_bypass_via_compound_command():
    # 回归：`cd xxx && rm file` 曾因命中白名单前缀 `cd ` 直接 SAFE，rm 未被审查
    guard = SecurityGuard()
    assert guard.check_shell_command(
        'cd /Users/layne/codes/Test && rm hello.py && echo "已删除"'
    ).level == ThreatLevel.MEDIUM
    # 分号 / 管道 / 反引号 / 命令替换 / 重定向 同样视为复合命令
    assert guard.check_shell_command("cd /tmp; rm x").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("cd /tmp | rm x").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("cd /tmp `rm x`").level == ThreatLevel.MEDIUM
    assert guard.check_shell_command("cd /tmp $(rm x)").level == ThreatLevel.MEDIUM
    # 复合命令中的高危同样不可绕过
    assert guard.check_shell_command("cd /tmp && rm -rf /").level == ThreatLevel.HIGH
    assert guard.check_shell_command("echo hi && sudo rm x").level == ThreatLevel.MEDIUM


def test_whitelist_never_overrides_high_risk():
    # 回归：白名单 `git push` 曾在高危检查之前放行，`git push --force` 漏过拦截
    guard = SecurityGuard()
    assert guard.check_shell_command("git push --force origin main").level == ThreatLevel.HIGH
    # 简单命令的白名单放行不受影响
    assert guard.check_shell_command("git push").level == ThreatLevel.SAFE
    assert guard.check_shell_command("git push origin main").level == ThreatLevel.SAFE


def test_forbidden_paths():
    guard = SecurityGuard()
    assert guard.check_path(".env").level == ThreatLevel.HIGH
    assert guard.check_path("src/.env.production").level == ThreatLevel.HIGH
    assert guard.check_path("~/.ssh/id_rsa").level == ThreatLevel.HIGH
    assert guard.check_path("src/main.ts").level == ThreatLevel.SAFE


def test_check_tool_for_file_tools():
    guard = SecurityGuard()
    r = guard.check_tool("read_file", {"filePath": "/etc/shadow"})
    assert r.level == ThreatLevel.HIGH
    r = guard.check_tool("read_file", {"filePath": "src/index.ts"})
    assert r.level == ThreatLevel.SAFE


def test_dynamic_config_reload_merges_with_defaults():
    guard = SecurityGuard()
    assert guard.check_shell_command("custom-danger-xyz").level == ThreatLevel.SAFE

    guard.apply_config({
        "high_risk_patterns": [r"\bcustom-danger-xyz\b"],
        "medium_risk_patterns": [],
        "whitelist": [],
        "forbidden_paths": [],
    })
    # 新增规则生效
    assert guard.check_shell_command("custom-danger-xyz").level == ThreatLevel.HIGH
    # 代码默认基线仍然生效（合并语义，而非整体替换）
    assert guard.check_shell_command("rm -rf /").level == ThreatLevel.HIGH
    assert guard.check_shell_command("sudo apt install git").level == ThreatLevel.MEDIUM


def test_invalid_regex_ignored():
    guard = SecurityGuard()
    guard.apply_config({"high_risk_patterns": ["[invalid"]})
    assert guard.check_shell_command("anything").level == ThreatLevel.SAFE


def test_incremental_utf8_keeps_complete_text_before_split_character():
    text = "正在检查项目，然后调用工具"
    raw = text.encode("utf-8")
    first, pending = decode_utf8_incremental(b"", raw[:-1])
    second, pending = decode_utf8_incremental(pending, raw[-1:])
    assert first + second == text
    assert pending == b""


async def test_external_file_access_requires_matching_capability(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("outside", encoding="utf-8")
    tools = FileSystemTools(str(workspace))

    with pytest.raises(PermissionError, match="项目外路径未获授权"):
        await tools.execute("read_file", {"filePath": str(external)})

    read = await tools.execute("read_file", {
        "filePath": str(external),
        "_approved_external_access": "read",
        "withLineNumbers": False,
    })
    assert "outside" in read

    with pytest.raises(PermissionError, match="项目外写入未获授权"):
        await tools.execute("write_file", {
            "filePath": str(external),
            "content": "changed",
            "_approved_external_access": "read",
        })
    assert external.read_text(encoding="utf-8") == "outside"

    await tools.execute("write_file", {
        "filePath": str(external),
        "content": "changed",
        "_approved_external_access": "write",
    })
    assert external.read_text(encoding="utf-8") == "changed"


# ---------------------------------------------------------------- 技能目录受信（读取免审批）

def test_trusted_skill_path_read_allowed(tmp_path):
    """读取技能目录（用户级/安装包内置）不应触发"项目外路径"审批。"""
    import asyncio

    from litework.security.approval import ApprovalGate
    from litework.security.guard import SecurityGuard
    from litework.security.plugin import SecurityPlugin

    ws = tmp_path / "proj"
    ws.mkdir()
    skill_dir = tmp_path / "agentshome" / ".agents" / "skills" / "diagram-to-office"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")

    # HOME 指向隔离目录 → 受信前缀 = <隔离>/~/.agents/skills
    import os
    import litework.security.plugin as plugin_mod

    orig_home = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path / "agentshome")
    try:
        sp = SecurityPlugin(SecurityGuard(), ApprovalGate(), str(ws), None)
        # 读取受信路径 → 放行（不发审批）
        assert sp._is_trusted_skill_path(str(skill_dir / "render_diagram.py")) is True
        assert sp._is_trusted_skill_path(str(skill_dir)) is True
        # 非技能路径 → 仍走审批
        assert sp._is_trusted_skill_path(str(tmp_path / "other" / "x.txt")) is False
        assert sp._is_trusted_skill_path("/etc/passwd") is False
    finally:
        if orig_home is not None:
            os.environ["HOME"] = orig_home
        else:
            os.environ.pop("HOME", None)


# ---------------------------------------------------------------- MCP 工具审批

async def test_mcp_tool_call_always_requires_approval():
    """MCP 工具（mcp_*）注册进 Agent 注册表后，每次调用必须经人工审批。

    回归锁：register_tools 不再被静态白名单过滤（见 test_mcp.py），
    安全边界完全依赖本审批门——拒绝时 cancel、工具不执行。
    （SecurityPlugin._request_approval 不传 auto_approve，
    故 auto_approve 配置无法旁路 MCP 审批，见 approval.py:36。）
    """
    from litework.core.kernel import Kernel
    from litework.security.approval import ApprovalGate
    from litework.security.plugin import SecurityPlugin

    kernel = Kernel(session_id="test-mcp-approval")
    plugin = SecurityPlugin(SecurityGuard(), ApprovalGate(timeout_seconds=5), workspace="/tmp")
    plugin.install(kernel)

    # 用户点「拒绝」→ _request_approval 返回 False
    # （实例属性替换：不自动绑定 self，签名 = (kernel, action, reason, rule=None)）
    requested: list[str] = []

    async def _deny(kernel, action: str, reason: str, rule=None) -> bool:
        requested.append(action)
        return False

    orig = plugin._request_approval
    plugin._request_approval = _deny
    try:
        hook_data = {"toolName": "mcp_server_x_navigate_page",
                     "args": {"url": "http://example.com"}, "cancel": False, "reason": ""}
        result = await kernel.before_tool.run(kernel.ctx, hook_data)
    finally:
        plugin._request_approval = orig

    # 审批确实被请求，且调用被取消
    assert any("mcp_server_x_navigate_page" in a for a in requested), "MCP 调用未触发审批"
    assert result["cancel"] is True
    assert "MCP" in result["reason"] or "拒绝" in result["reason"]


async def test_mcp_tool_call_approved_passes_through():
    """用户点「允许」→ MCP 调用放行（cancel=False），正常执行。"""
    from litework.core.kernel import Kernel
    from litework.security.approval import ApprovalGate
    from litework.security.plugin import SecurityPlugin

    kernel = Kernel(session_id="test-mcp-allow")
    plugin = SecurityPlugin(SecurityGuard(), ApprovalGate(timeout_seconds=5), workspace="/tmp")
    plugin.install(kernel)

    async def _allow(kernel, action: str, reason: str, rule=None) -> bool:
        return True

    plugin._request_approval = _allow
    hook_data = {"toolName": "mcp_server_x_click",
                 "args": {"selector": "#btn"}, "cancel": False, "reason": ""}
    result = await kernel.before_tool.run(kernel.ctx, hook_data)
    assert result["cancel"] is False
