// lite-work PWA Service Worker：仅承载 Web Push 与通知点击。
// 刻意不做离线缓存——聊天/任务依赖实时连接，离线壳只会制造「能用」的假象。
// 载荷格式与 litework/server/push.py notify() 对齐：{title, body, tag, data}。

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: "lite-work" };
  }
  const title = payload.title || "lite-work";
  const options = {
    body: payload.body || "",
    tag: payload.tag || undefined,
    // 同 tag 的后续通知覆盖前一条（盯梢状态变化不堆一摞）
    renotify: payload.tag ? true : undefined,
    data: payload.data || {},
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      // 已打开的窗口直接前置；没打开才新开（v1 不做深链跳转）
      const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of all) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow("/");
    })()
  );
});
