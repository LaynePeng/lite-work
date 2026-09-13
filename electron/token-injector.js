// lite-work Core 鉴权令牌注入（Electron 主进程）
//
// 背景：本地/远程 Core 默认开启 Bearer 鉴权（见 litework/cli.py 自动生成 token），
// 渲染进程自身不携带令牌，由主进程在默认 session 的 onBeforeSendHeaders 里注入。
//
// 关键约束：Electron 的 WebRequest「同一事件只有最后注册的监听器生效」
// （官方文档：Only the last attached listener will be used）。而同一个 Electron
// 进程里可以存在多个项目窗口（菜单/界面的「新建项目窗口」），每个窗口各自拉起
// 一个本地 Core（`--port 0` → 端口随机、令牌各自独立）。若每开一个窗口就重新注册
// 一次监听器，后开的窗口会把先开窗口的注入器顶掉——先开窗口从此带着「另一个 Core
// 的令牌」发请求，全部 401（用户看到的就是「第一个窗口没法操作，停止任务报
// 401 Unauthorized」）。重启某个窗口的 Core（换端口 + 换令牌）同理。
//
// 因此这里只安装「一次」监听器，令牌按「请求目标 origin」查表注入：每个 Core 一个
// origin（http://127.0.0.1:<port>），窗口内的 API 调用都是同源相对路径，命中即注入
// 对应令牌。未登记的 origin 一律不注入——顺带避免了把 Core 令牌泄露给第三方 origin。
//
// getSession 传函数而非 session 对象：`session.defaultSession` 在 app ready 之前
// 不可用，延迟到真正要安装监听器时再取。

/** 取 URL 的 origin（http://127.0.0.1:51234）；非法 URL 返回 ""。 */
function originOf(url) {
  try {
    return new URL(String(url)).origin;
  } catch {
    return "";
  }
}

function createTokenInjector(getSession) {
  const tokensByOrigin = new Map(); // origin -> token
  let installed = false;

  function install() {
    if (installed) return;
    const sess = getSession();
    if (!sess || !sess.webRequest) return;
    installed = true;
    sess.webRequest.onBeforeSendHeaders((details, callback) => {
      const requestHeaders = details.requestHeaders || {};
      const token = tokensByOrigin.get(originOf(details.url));
      if (token) requestHeaders["Authorization"] = `Bearer ${token}`;
      callback({ requestHeaders });
    });
  }

  return {
    /** 登记某个 Core 的令牌并（首次）安装注入器；token 为空表示该 Core 未开鉴权。 */
    register(url, token) {
      if (!url || !token) return;
      tokensByOrigin.set(originOf(url), token);
      install();
    },
    /** 窗口关闭 / Core 重启后释放旧 origin 的令牌登记。 */
    release(url) {
      const origin = originOf(url);
      if (origin) tokensByOrigin.delete(origin);
    },
    /** 测试与排查用：某 URL 当前会注入的令牌。 */
    tokenFor(url) {
      return tokensByOrigin.get(originOf(url));
    },
    get installed() {
      return installed;
    },
    get size() {
      return tokensByOrigin.size;
    },
  };
}

module.exports = { createTokenInjector, originOf };
