// Core 鉴权令牌注入器单测（node --test tests/js/token-injector.test.mjs）
//
// 回归点：Electron 的 webRequest 「同一事件只有最后注册的监听器生效」。
// 此前每开一个项目窗口就重新注册一次 onBeforeSendHeaders，后开窗口会把先开
// 窗口的令牌顶掉 → 先开窗口所有请求带错令牌 → 401（"停止失败: 401 Unauthorized"）。
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { createTokenInjector, originOf } = require("../../electron/token-injector.js");

/** 最小 fake session：记录监听器（真实 Electron 会替换，故只应注册一次）。 */
function fakeSession() {
  const listeners = [];
  return {
    listeners,
    webRequest: {
      onBeforeSendHeaders(listener) {
        listeners.push(listener);
      },
    },
  };
}

/** 用当前监听器模拟一次请求，返回最终请求头。 */
function runRequest(sess, url, headers = {}) {
  const listener = sess.listeners[sess.listeners.length - 1];
  assert.ok(listener, "注入器未安装监听器");
  let response = null;
  listener({ url, requestHeaders: { ...headers } }, (resp) => {
    response = resp;
  });
  return response.requestHeaders;
}

test("originOf：取 origin，非法 URL 返回空串", () => {
  assert.equal(originOf("http://127.0.0.1:51234/api/status"), "http://127.0.0.1:51234");
  assert.equal(originOf("not a url"), "");
  assert.equal(originOf(""), "");
});

test("多个 Core 各自注入自己的令牌，且只安装一个监听器", () => {
  const sess = fakeSession();
  const injector = createTokenInjector(() => sess);

  injector.register("http://127.0.0.1:51001", "tok-A");
  injector.register("http://127.0.0.1:51002", "tok-B");

  // 关键：后开的窗口不能再注册一个监听器（否则会顶掉先前的）
  assert.equal(sess.listeners.length, 1);
  assert.equal(injector.installed, true);
  assert.equal(injector.size, 2);

  assert.equal(runRequest(sess, "http://127.0.0.1:51001/api/chat").Authorization, "Bearer tok-A");
  assert.equal(runRequest(sess, "http://127.0.0.1:51002/api/chat").Authorization, "Bearer tok-B");
});

test("未登记 origin 不注入令牌（避免泄露给第三方）", () => {
  const sess = fakeSession();
  const injector = createTokenInjector(() => sess);
  injector.register("http://127.0.0.1:51001", "tok-A");

  const headers = runRequest(sess, "https://example.com/collect");
  assert.equal(headers.Authorization, undefined);
});

test("release 后该 origin 不再注入（关窗释放），其它 Core 不受影响", () => {
  const sess = fakeSession();
  const injector = createTokenInjector(() => sess);
  injector.register("http://127.0.0.1:51001", "tok-A");
  injector.register("http://127.0.0.1:51002", "tok-B");

  injector.release("http://127.0.0.1:51001");

  assert.equal(runRequest(sess, "http://127.0.0.1:51001/api/chat").Authorization, undefined);
  assert.equal(runRequest(sess, "http://127.0.0.1:51002/api/chat").Authorization, "Bearer tok-B");
});

test("Core 重启换端口换令牌：新 origin 用新令牌，旧 origin 已释放", () => {
  const sess = fakeSession();
  const injector = createTokenInjector(() => sess);
  injector.register("http://127.0.0.1:51001", "tok-old");

  // 重启：新端口 + 新令牌，随后释放旧 origin
  injector.register("http://127.0.0.1:52007", "tok-new");
  injector.release("http://127.0.0.1:51001");

  assert.equal(runRequest(sess, "http://127.0.0.1:52007/api/chat").Authorization, "Bearer tok-new");
  assert.equal(runRequest(sess, "http://127.0.0.1:51001/api/chat").Authorization, undefined);
  assert.equal(sess.listeners.length, 1);
});

test("令牌为空（Core 未开鉴权）不安装注入器", () => {
  const sess = fakeSession();
  const injector = createTokenInjector(() => sess);

  injector.register("http://127.0.0.1:51001", "");
  injector.register("", "tok-A");

  assert.equal(sess.listeners.length, 0);
  assert.equal(injector.installed, false);
  assert.equal(injector.size, 0);
});

test("session 尚未就绪（ready 前）时不安装，稍后 register 能补上", () => {
  let sessionRef = null;
  const sess = fakeSession();
  const injector = createTokenInjector(() => sessionRef);

  injector.register("http://127.0.0.1:51001", "tok-A");
  assert.equal(injector.installed, false);

  sessionRef = sess; // app ready
  injector.register("http://127.0.0.1:51002", "tok-B");
  assert.equal(injector.installed, true);
  assert.equal(sess.listeners.length, 1);
  assert.equal(runRequest(sess, "http://127.0.0.1:51001/api/chat").Authorization, "Bearer tok-A");
  assert.equal(runRequest(sess, "http://127.0.0.1:51002/api/chat").Authorization, "Bearer tok-B");
});
