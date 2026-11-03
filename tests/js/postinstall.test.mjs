// 根 postinstall 的 CI 跳过判定（node --test tests/js/postinstall.test.mjs）
//
// 回归点：根 postinstall 里嵌套调用 `npm install --prefix web`，在 npm 自己的
// 生命周期钩子里再起一个 npm 进程。npm 10 偶发 "Exit handler never called!"
// 直接让 job 挂掉——v2.1.1 的 macOS release 就死在这一步，而同样配置的
// v2.1.0 是通过的（随机复发）。CI 工作流本就显式跑 `npm --prefix web ci`，
// 所以在 CI 里跳过这次嵌套安装。
import assert from "node:assert/strict";
import { test } from "node:test";

import { shouldInstallWeb } from "../../scripts/postinstall.mjs";

test("GitHub Actions 默认注入 CI=true → 不在 postinstall 里装 web", () => {
  assert.equal(shouldInstallWeb({ CI: "true" }), false);
  assert.equal(shouldInstallWeb({ CI: "TRUE" }), false);
  assert.equal(shouldInstallWeb({ CI: "1" }), false);
});

test("本地开发（无 CI）→ 照旧自动装 web，npm install 一条命令够用", () => {
  assert.equal(shouldInstallWeb({}), true);
});

test("显式关闭（CI=false / CI=0）不该被当成 CI", () => {
  assert.equal(shouldInstallWeb({ CI: "false" }), true);
  assert.equal(shouldInstallWeb({ CI: "0" }), true);
  assert.equal(shouldInstallWeb({ CI: "  " }), true);
});
