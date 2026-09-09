// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ErrorBoundary from "./ErrorBoundary";

function Boom(): never {
  throw new Error("渲染层爆炸");
}

function Good() {
  return <div>一切正常</div>;
}

describe("ErrorBoundary（分层兜底的基础组件）", () => {
  it("子组件正常渲染时不拦截", () => {
    render(
      <ErrorBoundary>
        <Good />
      </ErrorBoundary>
    );
    expect(screen.getByText("一切正常")).toBeInTheDocument();
  });

  it("子组件抛错时展示兜底 UI（含错误信息与重新加载按钮）", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>
    );
    expect(screen.getByText("界面渲染出错")).toBeInTheDocument();
    expect(screen.getByText(/渲染层爆炸/)).toBeInTheDocument();
    // 重新加载入口存在（window.location.reload）
    expect(screen.getByRole("button", { name: /重新加载/ })).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("多个兄弟 Boundary 互不影响（分层兜底语义）", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <div>
        <ErrorBoundary>
          <Good />
        </ErrorBoundary>
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>
      </div>
    );
    // 左侧正常区域不受右侧崩溃影响
    expect(screen.getByText("一切正常")).toBeInTheDocument();
    expect(screen.getByText("界面渲染出错")).toBeInTheDocument();
    consoleError.mockRestore();
  });
});
