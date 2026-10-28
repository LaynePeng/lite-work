// 盯梢通知：状态→语气映射、系统通知门槛、提示条渲染与自动收起。
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WatchToaster } from "./WatchToaster";
import {
  notificationLabel,
  notificationTone,
  wantsSystemNotification,
  type WatchNotification,
} from "../hooks/useWatchNotifications";

const note = (over: Partial<WatchNotification> = {}): WatchNotification => ({
  id: 1,
  at: 1700000000,
  schedule_id: "sch_1",
  name: "盯部署",
  status: "changed",
  detail: "有新提交",
  ...over,
});

describe("盯梢通知：语气与文案映射", () => {
  it("status → tone（changed/ok 好，error 坏，unchanged 中性，skipped 警告）", () => {
    expect(notificationTone("changed")).toBe("good");
    expect(notificationTone("ok")).toBe("good");
    expect(notificationTone("error")).toBe("bad");
    expect(notificationTone("unchanged")).toBe("muted");
    expect(notificationTone("skipped")).toBe("warn");
    expect(notificationTone("未知状态")).toBe("muted");
  });

  it("status → 中文标签（未知状态原样回显，不吞信息）", () => {
    expect(notificationLabel("changed")).toBe("检测到变化");
    expect(notificationLabel("unchanged")).toBe("无变化");
    expect(notificationLabel("whatever")).toBe("whatever");
  });
});

describe("盯梢通知：系统通知门槛", () => {
  it("只有计划声明 system 才弹系统通知", () => {
    expect(wantsSystemNotification(note({ notify: ["in_app"] }))).toBe(false);
    expect(wantsSystemNotification(note({ notify: ["system"] }))).toBe(true);
    expect(wantsSystemNotification(note({ notify: undefined }))).toBe(false);
  });

  it("「无变化」不打扰用户——那正是盯梢的意义", () => {
    expect(wantsSystemNotification(note({ notify: ["system"], status: "unchanged" }))).toBe(false);
    expect(wantsSystemNotification(note({ notify: ["system"], status: "changed" }))).toBe(true);
    expect(wantsSystemNotification(note({ notify: ["system"], status: "error" }))).toBe(true);
  });
});

describe("WatchToaster", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("渲染通知内容，并可手动关闭", () => {
    const onDismiss = vi.fn();
    render(<WatchToaster items={[note()]} onDismiss={onDismiss} />);
    expect(screen.getByText("盯部署")).toBeInTheDocument();
    expect(screen.getByText("有新提交")).toBeInTheDocument();
    expect(screen.getByText("检测到变化")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("关闭通知"));
    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it("无通知时不渲染任何东西", () => {
    const { container } = render(<WatchToaster items={[]} onDismiss={() => {}} />);
    expect(container.querySelector(".watch-toaster")).toBeNull();
  });

  it("可堆叠：多条计划同时到点时都在", () => {
    render(
      <WatchToaster
        items={[note({ id: 1, name: "盯部署" }), note({ id: 2, name: "盯邮件", status: "error", detail: "凭据失效" })]}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText("盯部署")).toBeInTheDocument();
    expect(screen.getByText("盯邮件")).toBeInTheDocument();
    expect(screen.getByText("出错")).toBeInTheDocument();
  });

  it("超时自动收起（错误留得更久）", () => {
    const onDismiss = vi.fn();
    render(<WatchToaster items={[note({ id: 7, status: "changed" })]} onDismiss={onDismiss} />);
    act(() => {
      vi.advanceTimersByTime(6100);
    });
    expect(onDismiss).toHaveBeenCalledWith(7);
  });

  it("提供会话时显示「打开会话」并可跳转", () => {
    const onOpen = vi.fn();
    const onDismiss = vi.fn();
    render(
      <WatchToaster
        items={[note()]}
        onDismiss={onDismiss}
        onOpenSession={onOpen}
        sessionOf={() => "sess_9"}
      />,
    );
    fireEvent.click(screen.getByText("打开会话 →"));
    expect(onOpen).toHaveBeenCalledWith("sess_9");
    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it("拿不到会话时不显示跳转按钮", () => {
    render(<WatchToaster items={[note()]} onDismiss={() => {}} sessionOf={() => undefined} />);
    expect(screen.queryByText("打开会话 →")).toBeNull();
  });

  it("钉住的条目（从盯梢页签「重新弹出」）不自动收起，取消钉住后恢复计时", () => {
    const onDismiss = vi.fn();
    const { rerender } = render(
      <WatchToaster items={[note({ id: 3 })]} onDismiss={onDismiss} pinned={[3]} />,
    );
    act(() => { vi.advanceTimersByTime(20_000); });
    expect(onDismiss).not.toHaveBeenCalled();

    rerender(<WatchToaster items={[note({ id: 3 })]} onDismiss={onDismiss} pinned={[]} />);
    act(() => { vi.advanceTimersByTime(6100); });
    expect(onDismiss).toHaveBeenCalledWith(3);
  });
});
