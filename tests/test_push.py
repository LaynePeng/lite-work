# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Web Push（PWA 通知）：VAPID 密钥持久化、订阅簿、盯梢门槛、失效回收、HTTP 面。"""
import base64

import pytest

from litework.app import AgentApp
from litework.server.app import create_app
from litework.server.push import PushManager
from tests.conftest import live_server


def test_vapid_keypair_persisted(tmp_path):
    """公钥为 base64url 未压缩点；密钥落盘后新实例保持一致。"""
    pm = PushManager(str(tmp_path))
    pub = pm.public_key()
    assert pub and "=" not in pub
    raw = base64.urlsafe_b64decode(pub + "===")
    assert len(raw) == 65 and raw[0] == 4  # 0x04 || X || Y
    assert pm.public_key() == pub
    # 同目录新实例读回同一把私钥 → 公钥一致（换钥会让全部设备失联）
    assert PushManager(str(tmp_path)).public_key() == pub
    assert (tmp_path / "vapid_key.pem").is_file()


def test_subscribe_dedupe_and_unsubscribe(tmp_path):
    pm = PushManager(str(tmp_path))
    sub = {"endpoint": "https://push.example.com/abc",
           "keys": {"p256dh": "BPublic", "auth": "secret"}, "device": "iPhone"}
    assert pm.subscribe(sub) == 1
    # 同 endpoint 重复订阅 = 刷新（覆盖旧的），不是追加
    assert pm.subscribe({**sub, "device": "iPhone 2"}) == 1
    assert pm.subscribe({"endpoint": "https://push.example.com/def",
                         "keys": {"p256dh": "B2", "auth": "s2"}}) == 2
    # 订阅簿持久化：新实例读回
    assert len(PushManager(str(tmp_path))._subs) == 2
    # 非法订阅被拒（缺 endpoint / 缺密钥）
    with pytest.raises(ValueError):
        pm.subscribe({"endpoint": "", "keys": {}})
    assert pm.unsubscribe("https://push.example.com/abc") is True
    assert pm.unsubscribe("https://push.example.com/abc") is False


def test_watch_note_gate(tmp_path):
    """门槛与前端 wantsSystemNotification 一致：声明 system 且非 unchanged。"""
    pm = PushManager(str(tmp_path))
    sent: list = []
    pm.notify = lambda **kw: sent.append(kw)  # type: ignore[method-assign]
    # 仅 in_app：不推
    pm.on_watch_note({"notify": ["in_app"], "status": "changed", "name": "盯部署"})
    assert sent == []
    # system + unchanged：不推（无变化正是盯梢的意义，不打扰）
    pm.on_watch_note({"notify": ["system"], "status": "unchanged", "name": "盯部署"})
    assert sent == []
    # system + changed → 推
    pm.on_watch_note({"notify": ["in_app", "system"], "status": "changed",
                      "name": "盯部署", "detail": "有新提交", "schedule_id": "s1",
                      "id": 7, "task_id": "t1"})
    assert len(sent) == 1
    assert sent[0]["title"] == "盯梢：盯部署"
    assert sent[0]["body"] == "有新提交"
    assert sent[0]["data"]["schedule_id"] == "s1"


def test_bus_on_push_wired_by_create_app(tmp_path):
    """create_app 把推送回调挂到通知总线；回调异常不影响总线广播。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    create_app(app, token=None)
    assert app.watch_notifications.on_push is not None

    # 总线照常广播 + 回调收到（含补全后的 id）
    q = app.watch_notifications.subscribe()
    received = []
    app.watch_notifications.on_push = lambda note: received.append(note["id"])
    item = app.watch_notifications.push({"status": "changed"})
    assert received == [item["id"]]
    assert not q.empty()

    # 回调抛异常：总线不炸、广播照发
    def _boom(_note):
        raise RuntimeError("push 通道故障")
    app.watch_notifications.on_push = _boom
    app.watch_notifications.push({"status": "error"})
    assert not q.empty()


def test_send_one_drops_dead_subscription(tmp_path, monkeypatch):
    """404/410 清除订阅（PWA 被删/授权吊销）；其他失败保留订阅。"""
    import pywebpush as pw

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code

    # 410 → 清除
    pm = PushManager(str(tmp_path))
    pm.subscribe({"endpoint": "https://push.example.com/dead",
                  "keys": {"p256dh": "B", "auth": "a"}})

    def _gone(*a, **k):
        raise pw.WebPushException("gone", response=_Resp(410))

    monkeypatch.setattr(pw, "webpush", _gone)
    pm._send_one(pm._subs[0], {"title": "t", "body": "b"})
    assert len(pm._subs) == 0

    # 网络类失败（无响应对象）→ 保留，下条通知仍会尝试
    pm2 = PushManager(str(tmp_path))
    pm2.subscribe({"endpoint": "https://push.example.com/alive",
                   "keys": {"p256dh": "B", "auth": "a"}})

    def _net_err(*a, **k):
        raise pw.WebPushException("connection timed out")

    monkeypatch.setattr(pw, "webpush", _net_err)
    pm2._send_one(pm2._subs[0], {"title": "t", "body": "b"})
    assert len(pm2._subs) == 1


async def test_push_endpoints(tmp_path):
    """HTTP 面：vapid-key / subscribe / unsubscribe（含鉴权）。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    fast_app = create_app(app, token="secret")
    async with live_server(fast_app) as (c, _s):
        hdr = {"Authorization": "Bearer secret"}
        r = await c.get("/api/push/vapid-key", headers=hdr)
        assert r.status_code == 200
        assert r.json()["public_key"]
        # 无令牌 → 401（与全路由一致）
        r = await c.get("/api/push/vapid-key")
        assert r.status_code == 401

        sub = {"endpoint": "https://push.example.com/x",
               "keys": {"p256dh": "B", "auth": "a"}, "device": "iPhone"}
        r = await c.post("/api/push/subscribe", json=sub, headers=hdr)
        assert r.status_code == 200 and r.json()["devices"] == 1
        # 非法载荷 → 400
        r = await c.post("/api/push/subscribe",
                         json={"endpoint": "", "keys": {}}, headers=hdr)
        assert r.status_code == 400
        # 注销
        r = await c.post("/api/push/unsubscribe",
                         json={"endpoint": "https://push.example.com/x"}, headers=hdr)
        assert r.status_code == 200 and r.json()["ok"] is True
