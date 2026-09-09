// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// 每个用例后卸载组件，隔离 DOM 状态
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// jsdom 未实现的滚动 API（ChatView 自动跟随用到 scrollIntoView）
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
