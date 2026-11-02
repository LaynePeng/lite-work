// Core 进程存活判定（纯函数，可单测：node --test tests/js/core-health.test.mjs）
//
// 背景：`spawnCoreOnce` 里 Core 就绪后的 `exit` 此前完全没人管——不写日志、
// 不通知窗口、不重连。于是 Core 崩溃时用户只看到「窗口还在但什么都不动」，
// 而日志一行都不留，线上「闪退」长期查无实据。
//
// 判定为什么要单独抽出来：这里的分支最容易写错，而错了就是「误报崩溃」
// （用户被无端弹横幅）或「漏报崩溃」（回到什么都没记的老状态）。抽成纯函数
// 才能把每种退出形态都钉死。

/**
 * 退出是否属于「我们主动回收」，不该报给用户。
 *
 * killBackendTree 会先把 child 从 coreChildren 摘掉再杀，因此
 * 「不在集合里」= 主动回收。除此之外的退出都是事故。
 *
 * @param {boolean} resolved  是否已收到 LITEWORK_CORE_READY
 * @param {boolean} tracked   child 是否仍在 coreChildren 中
 * @returns {"starting-failed"|"intentional"|"unexpected"}
 */
function classifyExit(resolved, tracked) {
  if (!resolved) return "starting-failed";  // 启动期退出 → 由调用方 reject
  if (!tracked) return "intentional";        // 已被 killBackendTree 摘除
  return "unexpected";
}

/**
 * 把退出信息格式化成一行给人看的诊断文本。
 *
 * 刻意同时给出「退出码 / 信号 / 存活时长」：这三者组合能区分几类完全不同的
 * 故障——启动即崩（存活 <2s，多为核心自身异常）、跑一阵才崩（内存泄漏 /
 * 某次请求触发）、被 SIGKILL（多为系统 OOM killer）、被 SIGSEGV（原生崩溃）。
 */
function describeExit(code, signal, uptimeMs) {
  const secs = Math.max(0, Math.round((Number(uptimeMs) || 0) / 1000));
  const how = signal ? `signal=${signal}` : `code=${code === null || code === undefined ? "null" : code}`;
  return `${how} 存活=${secs}s`;
}

/**
 * 组装推给渲染进程的 `core-lost` 负载。
 *
 * uptimeMs 保留毫秒精度：界面上按秒显示，但排查时「1.2s」和「1.9s」有区别。
 */
function buildCoreLostPayload({ code, signal, uptimeMs, restartable }) {
  return {
    code: code === undefined ? null : code,
    signal: signal || null,
    uptimeMs: Number(uptimeMs) || 0,
    restartable: restartable === true,
  };
}

module.exports = { classifyExit, describeExit, buildCoreLostPayload };
