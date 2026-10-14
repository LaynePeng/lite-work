# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Web Push（PWA 通知）：VAPID 密钥对、订阅簿与盯梢通知投递。

单用户多设备：订阅簿就是设备清单（每台手机/PWA 一条，按 endpoint 去重）。
VAPID 密钥对首用生成、存 config_dir（重启不变——换密钥会让全部已订阅设备
失效，浏览器只在用户重新打开 PWA 时才重新订阅）。投递走独立线程池：
pywebpush 是阻塞 requests 调用，且推送服务端偶尔很慢，绝不能占事件循环。
404/410 视为订阅失效直接清除；其余失败只记日志（尽力而为通道）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.server.push")

# 设备离线期间推送服务缓存消息的窗口（秒）；过期即弃（盯梢讲时效，不补陈货）
PUSH_TTL_SECONDS = 3600
# 线程池上限：设备数本来就少，2 个 worker 够用且不占内存
PUSH_WORKERS = 2


class PushManager:
    def __init__(self, config_dir: str,
                 subject: str = "mailto:lite-work@localhost") -> None:
        self._key_path = os.path.join(config_dir, "vapid_key.pem")
        self._subs_path = os.path.join(config_dir, "push_subscriptions.json")
        self._subject = subject
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=PUSH_WORKERS,
                                        thread_name_prefix="webpush")
        self._private_pem: Optional[str] = None
        self._public_b64url: Optional[str] = None
        self._subs: List[Dict[str, Any]] = self._load_subs()

    # ------------------------------------------------------------ VAPID 密钥

    def _ensure_keys(self) -> None:
        """惰性生成/加载密钥对（首查公钥或首条投递时才落盘）。"""
        if self._private_pem is not None:
            return
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_private_key,
        )

        if os.path.isfile(self._key_path):
            with open(self._key_path, "r", encoding="utf-8") as f:
                pem = f.read()
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("utf-8")
            os.makedirs(os.path.dirname(self._key_path), exist_ok=True)
            with open(self._key_path, "w", encoding="utf-8") as f:
                f.write(pem)
            try:
                os.chmod(self._key_path, 0o600)
            except OSError:
                pass  # Windows 无 POSIX 权限位
        key = load_pem_private_key(pem.encode("utf-8"), password=None)
        # 公钥取未压缩点（65 字节）→ base64url 无填充：
        # pushManager.subscribe 的 applicationServerKey 正是这个格式
        point = key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint)
        self._private_pem = pem
        self._public_b64url = base64.urlsafe_b64encode(point).rstrip(b"=").decode("ascii")

    def public_key(self) -> str:
        self._ensure_keys()
        return self._public_b64url or ""

    # ------------------------------------------------------------ 订阅簿

    def _load_subs(self) -> List[Dict[str, Any]]:
        try:
            with open(self._subs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            subs = data.get("subscriptions") if isinstance(data, dict) else data
            return [s for s in (subs or [])
                    if isinstance(s, dict) and s.get("endpoint")]
        except (OSError, ValueError):
            return []

    def _save_subs(self) -> None:
        os.makedirs(os.path.dirname(self._subs_path), exist_ok=True)
        tmp = self._subs_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"subscriptions": self._subs}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._subs_path)

    def subscribe(self, sub: Dict[str, Any]) -> int:
        """登记/刷新一条订阅（同 endpoint 覆盖旧的）。返回当前设备数。"""
        endpoint = str(sub.get("endpoint") or "")
        keys = sub.get("keys") or {}
        if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("订阅缺少 endpoint 或 keys（p256dh/auth）")
        with self._lock:
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            self._subs.append({
                "endpoint": endpoint,
                "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
                "device": str(sub.get("device") or "")[:60],
            })
            self._save_subs()
            return len(self._subs)

    def unsubscribe(self, endpoint: str) -> bool:
        with self._lock:
            before = len(self._subs)
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            removed = before != len(self._subs)
            if removed:
                self._save_subs()
            return removed

    def _drop(self, endpoint: str) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            self._save_subs()

    # ------------------------------------------------------------ 投递

    def notify(self, title: str, body: str, tag: str = "",
               data: Optional[Dict[str, Any]] = None) -> None:
        """向全部设备投递（尽力而为，立即返回）。"""
        if not self._subs:
            return
        payload = {"title": title, "body": body, "tag": tag or None,
                   "data": data or {}}
        for sub in list(self._subs):
            self._pool.submit(self._send_one, sub, payload)

    def _send_one(self, sub: Dict[str, Any], payload: Dict[str, Any]) -> None:
        try:
            from pywebpush import WebPushException, webpush

            self._ensure_keys()
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=self._private_pem,
                vapid_claims={"sub": self._subject},
                ttl=PUSH_TTL_SECONDS,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001 - 尽力而为通道，失败只记录
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                # 订阅失效（PWA 被删/浏览器吊销授权）：清掉，设备重开时重新订阅
                logger.info("[Push] 订阅已失效，移除：%s",
                            str(sub.get("endpoint", ""))[:64])
                self._drop(sub["endpoint"])
            else:
                logger.warning("[Push] 投递失败 status=%s: %s", status, exc)

    def on_watch_note(self, note: Dict[str, Any]) -> None:
        """盯梢通知 → 设备推送（WatchNotificationBus.on_push 回调）。

        门槛与前端 wantsSystemNotification 一致：计划声明 system，且
        「无变化」不打扰（那正是盯梢的意义）。
        """
        notify = note.get("notify") or []
        if "system" not in notify or note.get("status") == "unchanged":
            return
        self.notify(
            title=f"盯梢：{note.get('name') or '计划'}",
            body=str(note.get("detail") or note.get("status") or "")[:200],
            tag=f"watch-{note.get('schedule_id')}",
            data={"kind": "watch", "id": note.get("id"),
                  "schedule_id": note.get("schedule_id"),
                  "task_id": note.get("task_id")},
        )

    def close(self) -> None:
        self._pool.shutdown(wait=False)
