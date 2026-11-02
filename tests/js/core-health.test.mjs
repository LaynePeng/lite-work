// Core 退出判定单测（node --test tests/js/core-health.test.mjs）
//
// 回归点：Core 就绪后的 `exit` 此前完全静默——不写日志、不通知窗口。
// 用户侧表现为「窗口还在但什么都不动」，日志一行不留，「闪退」因此
// 长期查无实据。这里把每种退出形态钉死，避免修完之后又退回两种错误：
//   - 误报：主动回收（关窗/重启）也弹「Core 崩溃」横幅
//   - 漏报：真崩溃被当成主动回收吞掉
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { classifyExit, describeExit, buildCoreLostPayload } = require("../../electron/core-health.js");

test("classifyExit：启动期退出归为 starting-failed（不是崩溃，由调用方 reject）", () => {
  assert.equal(classifyExit(false, true), "starting-failed");
  // 启动期即使已被摘除，仍优先报「启动失败」——启动器需要那个 reject
  assert.equal(classifyExit(false, false), "starting-failed");
});

test("classifyExit：就绪后已从 coreChildren 摘除 = 主动回收，不报用户", () => {
  // killBackendTree 先 delete 再 kill，所以「不在集合里」就是主动
  assert.equal(classifyExit(true, false), "intentional");
});

test("classifyExit：就绪后仍在 coreChildren 中 = 意外退出", () => {
  assert.equal(classifyExit(true, true), "unexpected");
});

test("describeExit：有信号时优先报信号（SIGSEGV/SIGKILL 指向原生崩溃或被 OOM 杀）", () => {
  assert.match(describeExit(null, "SIGSEGV", 5000), /signal=SIGSEGV/);
  assert.match(describeExit(null, "SIGKILL", 30000), /存活=30s/);
});

test("describeExit：无信号时报退出码，0 也要正确显示", () => {
  assert.match(describeExit(1, null, 1200), /code=1/);
  assert.match(describeExit(0, null, 2000), /code=0/);
  // code=null 且无信号（极少见）不能渲染成 "code=undefined"
  assert.match(describeExit(null, null, 1000), /code=null/);
});

test("describeExit：存活时长按秒四舍五入，缺省值不产生 NaN", () => {
  assert.match(describeExit(1, null, 0), /存活=0s/);
  assert.match(describeExit(1, null, 1500), /存活=2s/);
  assert.match(describeExit(1, null, undefined), /存活=0s/);
  assert.doesNotMatch(describeExit(1, null, undefined), /NaN/);
});

test("buildCoreLostPayload：字段规整，signal 缺省为 null（不是空串）", () => {
  const p = buildCoreLostPayload({ code: 1, signal: null, uptimeMs: 1500, restartable: true });
  assert.deepEqual(p, { code: 1, signal: null, uptimeMs: 1500, restartable: true });
});

test("buildCoreLostPayload：code 缺省时补 null，避免前端显示 undefined", () => {
  const p = buildCoreLostPayload({ signal: "SIGSEGV", uptimeMs: 800 });
  assert.equal(p.code, null);
  assert.equal(p.signal, "SIGSEGV");
  assert.equal(p.restartable, false); // 缺省不给重试按钮
});

test("buildCoreLostPayload：uptimeMs 非数字时归零（前端要做除法，不能是 NaN）", () => {
  assert.equal(buildCoreLostPayload({ code: 1, uptimeMs: undefined }).uptimeMs, 0);
  assert.equal(buildCoreLostPayload({ code: 1, uptimeMs: "abc" }).uptimeMs, 0);
  assert.equal(buildCoreLostPayload({ code: 1, uptimeMs: 1234.6 }).uptimeMs, 1234.6);
});

test("buildCoreLostPayload：restartable 只认严格的 true", () => {
  assert.equal(buildCoreLostPayload({ restartable: "yes" }).restartable, false);
  assert.equal(buildCoreLostPayload({ restartable: 1 }).restartable, false);
  assert.equal(buildCoreLostPayload({ restartable: true }).restartable, true);
});
