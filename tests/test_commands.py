from litework.core.agent_loop import WRITE_TOOLS, AgentLoop, current_tool_call
from litework.core.commands import build_command_list, parse_skill_command
from litework.core.types import ToolCall


# ---------------------------------------------------------------- 命令解析

def test_parse_skill_command():
    assert parse_skill_command("/skill review 帮我审查") == {"name": "review", "requirement": "帮我审查"}
    assert parse_skill_command("/skill  review   只看核心") == {"name": "review", "requirement": "只看核心"}
    assert parse_skill_command("/skill review") == {"name": "review", "requirement": ""}
    assert parse_skill_command("/skill") == {"name": "", "requirement": ""}
    assert parse_skill_command("普通消息 /skill review") is None
    assert parse_skill_command("/other cmd") is None


def test_build_command_list_with_skills():
    cmds = build_command_list([{"name": "review", "description": "审查流程"}])
    names = [c["name"] for c in cmds]
    assert names == ["skill", "compact", "help", "history", "review"]
    review = cmds[-1]
    assert review["kind"] == "skill"
    assert review["description"] == "审查流程"


# ---------------------------------------------------------------- 并行判定

def _call(name, args="{}"):
    return ToolCall(id=f"call_{name}", name=name, arguments=args)


def _loop(mode):
    loop = AgentLoop.__new__(AgentLoop)
    loop.parallel_tool_calls = mode
    return loop


def test_should_parallelize_auto_readonly():
    loop = _loop("auto")
    assert loop._should_parallelize([_call("read_file"), _call("search_code")]) is True
    assert loop._should_parallelize([_call("read_file"), _call("write_file")]) is False
    assert loop._should_parallelize([_call("execute_command")]) is False


def test_should_parallelize_modes():
    assert _loop("always")._should_parallelize([_call("write_file"), _call("read_file")]) is True
    assert _loop("never")._should_parallelize([_call("read_file"), _call("search_code")]) is False
    # 单个工具无需并行
    assert _loop("always")._should_parallelize([_call("read_file")]) is False


def test_write_tools_covers_mutations():
    for t in ("write_file", "apply_search_replace", "apply_unified_diff",
              "execute_command", "git_commit"):
        assert t in WRITE_TOOLS


# ---------------------------------------------------------------- ContextVar

def test_current_tool_call_contextvar():
    assert current_tool_call.get() is None
