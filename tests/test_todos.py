"""todo_write 工具测试：校验 / 存储 / 事件推送 / Agent 裁剪 / 持久化。"""
import asyncio

import pytest

from litework.core.types import Message
from litework.tools.todos import TodoPlugin, current_session_id


class _EventBus:
    """捕获事件的假总线（与 kernel.events.emit 同签名）。"""

    def __init__(self):
        self.events = []

    async def emit(self, name, payload):
        self.events.append((name, payload))


def test_todo_write_validates_and_emits():
    plugin = TodoPlugin()
    bus = _EventBus()
    plugin.bind("s1", bus)
    current_session_id.set("s1")

    result = asyncio.run(plugin.execute("todo_write", {
        "todos": [
            {"content": "调研代码结构", "status": "completed"},
            {"content": "实现功能", "status": "in_progress"},
            {"content": "补测试", "status": "pending"},
        ],
    }))

    assert "[Error]" not in result
    assert "共 3 项" in result
    # 看板状态已存储
    assert len(plugin.get("s1")) == 3
    assert plugin.get("s1")[0]["status"] == "completed"
    # 事件已推送
    assert bus.events == [("todo:updated", {"todos": plugin.get("s1")})]


def test_todo_write_rejects_bad_status_and_empty_content():
    plugin = TodoPlugin()
    current_session_id.set("s2")
    bad = asyncio.run(plugin.execute("todo_write", {
        "todos": [{"content": "x", "status": "doing"}],
    }))
    assert "[Error]" in bad and "status 非法" in bad

    empty = asyncio.run(plugin.execute("todo_write", {
        "todos": [{"content": "  ", "status": "pending"}],
    }))
    assert "[Error]" in empty and "content" in empty

    not_list = asyncio.run(plugin.execute("todo_write", {"todos": "x"}))
    assert "[Error]" in not_list


def test_todo_write_full_replace_and_bound_events():
    """全量覆盖语义：第二次调用替换第一次；未绑定的会话只存储不推送。"""
    plugin = TodoPlugin()
    bus = _EventBus()
    plugin.bind("s3", bus)
    current_session_id.set("s3")

    asyncio.run(plugin.execute("todo_write", {
        "todos": [{"content": "旧项", "status": "pending"}],
    }))
    asyncio.run(plugin.execute("todo_write", {
        "todos": [{"content": "新项A", "status": "completed"},
                  {"content": "新项B", "status": "pending"}],
    }))
    assert [t["content"] for t in plugin.get("s3")] == ["新项A", "新项B"]
    assert len(bus.events) == 2  # 每次调用都推送

    # 未绑定会话：存储成功但不推送
    current_session_id.set("s4")
    r = asyncio.run(plugin.execute("todo_write", {
        "todos": [{"content": "静默项", "status": "pending"}],
    }))
    assert "[Error]" not in r
    assert plugin.get("s4")[0]["content"] == "静默项"
    assert len(bus.events) == 2  # s3 的事件数不变


def test_plan_agent_whitelist_contains_todo_write():
    from litework.core.agent_profile import default_plan_agent, default_build_agent
    assert "todo_write" in default_plan_agent().tools
    # build 无白名单（全量），todo_write 经插件注册自然可用
    assert default_build_agent().tools is None


def test_build_registry_exposes_todo_write(tmp_path):
    from litework.app import AgentApp
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    names = {t.name for t in app.build_registry().get_tools()}
    assert "todo_write" in names
    plan_names = {t.name for t in app.create_agent_registry("plan").get_tools()}
    assert "todo_write" in plan_names


# ---------------------------------------------------------------- 持久化

def test_todo_board_persists_and_restores(tmp_path):
    """todo_write 落盘 → 新插件实例（模拟重启）经 get() 恢复。"""
    storage = str(tmp_path / "boards")
    plugin = TodoPlugin(storage_dir=storage)
    token = current_session_id.set("s-persist")
    try:
        result = asyncio.run(plugin.execute(
            "todo_write",
            {"todos": [{"content": "第一步", "status": "completed"},
                       {"content": "第二步", "status": "pending"}]}))
    finally:
        current_session_id.reset(token)
    assert "TODO 清单已更新" in result

    # 新实例（模拟服务重启）：内存为空 → 从磁盘恢复
    plugin2 = TodoPlugin(storage_dir=storage)
    todos = plugin2.get("s-persist")
    assert [t["content"] for t in todos] == ["第一步", "第二步"]
    assert todos[0]["status"] == "completed"

    # bind() 也会从磁盘恢复（任务启动路径）
    plugin3 = TodoPlugin(storage_dir=storage)
    plugin3.bind("s-persist", _EventBus())
    assert len(plugin3.get("s-persist")) == 2


def test_todo_board_delete_board(tmp_path):
    storage = str(tmp_path / "boards")
    plugin = TodoPlugin(storage_dir=storage)
    token = current_session_id.set("s-del")
    try:
        asyncio.run(plugin.execute("todo_write", {"todos": [{"content": "x", "status": "pending"}]}))
    finally:
        current_session_id.reset(token)
    assert plugin.get("s-del")
    plugin.delete_board("s-del")
    assert plugin.get("s-del") == []
    plugin2 = TodoPlugin(storage_dir=storage)
    assert plugin2.get("s-del") == []  # 磁盘也清了


def test_todo_board_unsafe_session_id(tmp_path):
    """诡异 session_id 不炸：非法字符替换，空串不落盘。"""
    storage = str(tmp_path / "boards")
    plugin = TodoPlugin(storage_dir=storage)
    token = current_session_id.set("../../evil")
    try:
        result = asyncio.run(plugin.execute("todo_write", {"todos": [{"content": "a", "status": "pending"}]}))
    finally:
        current_session_id.reset(token)
    assert "TODO 清单已更新" in result
    # 未越界：文件都在 storage 内（非法字符已替换，无路径分隔符）
    import os
    for name in os.listdir(storage):
        assert "/" not in name and "\\" not in name
    assert plugin.get("../../evil")


def test_todo_persistence_endpoint_roundtrip(tmp_path):
    """App 级：todo_plugin 落盘目录在 config_dir 下；GET /api/todos 恢复看板。"""
    import os

    from fastapi.testclient import TestClient

    from litework.app import AgentApp
    from litework.server.app import create_app

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    assert app.todo_plugin.storage_dir == os.path.join(str(tmp_path / ".lc"), "todo_boards")

    token = current_session_id.set("s-web")
    try:
        asyncio.run(app.todo_plugin.execute(
            "todo_write", {"todos": [{"content": "任务A", "status": "in_progress"}]}))
    finally:
        current_session_id.reset(token)

    with TestClient(create_app(app)) as client:
        r = client.get("/api/todos", params={"session_id": "s-web"})
        assert r.status_code == 200
        todos = r.json()["todos"]
        assert len(todos) == 1 and todos[0]["content"] == "任务A"

        # 模拟重启：全新 App 实例共享 config_dir → 端点返回持久化看板
        app2 = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
        with TestClient(create_app(app2)) as client2:
            r2 = client2.get("/api/todos", params={"session_id": "s-web"})
            assert r2.json()["todos"][0]["content"] == "任务A"

        # 删除会话 → 看板清理
        app.session_store.save("s-web", [Message(role="user", content="hi")])
        with TestClient(create_app(app)) as client3:
            assert client3.delete("/api/sessions/s-web").status_code == 200
            r3 = client3.get("/api/todos", params={"session_id": "s-web"})
            assert r3.json()["todos"] == []
