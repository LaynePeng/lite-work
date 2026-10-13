// 盯梢计划管理：纯函数（间隔人性化 / 计划摘要 / 下次运行时间）+ 列表与编辑器交互。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import type { ScheduleInfo, WatchSourceInfo } from "../api";
import { ScheduleManager, describeSchedule, humanInterval, nextRunText } from "./ScheduleManager";

const SOURCES: WatchSourceInfo[] = [
  { name: "watch-file", source_kind: "file", display_name: "文件 / 目录",
    description: "盯文件的存在与大小", version: "1.0.0", probe_interval_hint: 60,
    config_schema: { path: "工作区相对路径" } },
  { name: "watch-url", source_kind: "url", display_name: "网页内容",
    description: "定时抓网页", version: "1.0.0", probe_interval_hint: 900,
    config_schema: { url: "要盯的网址" } },
];

const base = (over: Partial<ScheduleInfo> = {}): ScheduleInfo => ({
  id: "sch_1", name: "盯部署", enabled: true, schedule_mode: "interval",
  interval_seconds: 300, time_of_day: "09:00", cron: "", prompt: "总结变化",
  source: "", permissions: "read", notify: ["in_app"], catch_up: "once",
  ...over,
});

describe("盯梢计划：展示辅助函数", () => {
  it("humanInterval 输出可读间隔", () => {
    expect(humanInterval(300)).toBe("5 分钟");
    expect(humanInterval(3600)).toBe("1 小时");
    expect(humanInterval(86400)).toBe("1 天");
    expect(humanInterval(90)).toBe("90 秒");
    expect(humanInterval(0)).toBe("1 分钟"); // 下限 60s
  });

  it("describeSchedule 区分「定点执行」与「盯梢（有变化才触发）」", () => {
    expect(describeSchedule(base(), SOURCES)).toBe("每 5 分钟 · 定点执行");
    expect(describeSchedule(base({ source: "file" }), SOURCES))
      .toBe("每 5 分钟 · 盯文件 / 目录（有变化才触发）");
    expect(describeSchedule(base({ schedule_mode: "daily", time_of_day: "08:30" }), SOURCES))
      .toBe("每天 08:30 · 定点执行");
    expect(describeSchedule(base({ schedule_mode: "cron", cron: "0 9 * * *" }), SOURCES))
      .toBe("cron 0 9 * * * · 定点执行");
  });

  it("describeSchedule 对未知数据源回退显示 kind 本身（不吞信息）", () => {
    expect(describeSchedule(base({ source: "email" }), SOURCES))
      .toBe("每 5 分钟 · 盯email（有变化才触发）");
  });

  it("nextRunText 空值返回空串，有值格式化到分钟", () => {
    expect(nextRunText(null)).toBe("");
    expect(nextRunText(undefined)).toBe("");
    const t = Math.floor(new Date(2026, 2, 1, 9, 5).getTime() / 1000);
    expect(nextRunText(t)).toBe("03-01 09:05");
  });
});

describe("ScheduleManager", () => {
  afterEach(() => vi.restoreAllMocks());

  const mount = (items: ScheduleInfo[] = []) => {
    vi.spyOn(api, "schedules").mockResolvedValue({ schedules: items } as never);
    return render(<ScheduleManager sources={SOURCES} sessionId="sess_1" />);
  };

  it("空列表给出引导文案与「新建」入口", async () => {
    const { container } = mount([]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    await waitFor(() => expect(container.textContent).toContain("还没有盯梢"));
    expect(screen.getByText("+ 新建")).toBeInTheDocument();
  });

  it("列出计划：名称、摘要、状态、下次运行、操作按钮", async () => {
    const t = Math.floor(Date.now() / 1000) + 3600;
    const { container } = mount([
      base({ name: "盯构建", source: "file", last_status: "changed", next_run: t }),
      base({ id: "sch_2", name: "日报", enabled: false, last_status: "error" }),
    ]);
    await waitFor(() => expect(container.textContent).toContain("盯构建"));
    expect(container.textContent).toContain("盯文件 / 目录（有变化才触发）");
    expect(container.textContent).toContain("有变化");
    expect(container.textContent).toContain("出错");
    expect(container.textContent).toContain("下次 ≈");
    expect(container.textContent).toContain("启用");   // 停用的计划显示「启用」
    expect(container.textContent).toContain("立即跑");
  });

  it("新建：选数据源后出现该源的探针参数，且权限档只有两档", async () => {
    mount([]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("+ 新建"));
    // 数据源下拉可选到内置源
    const sourceSelect = screen.getByDisplayValue("不盯（定点执行）");
    fireEvent.change(sourceSelect, { target: { value: "url" } });
    await waitFor(() => expect(screen.getByPlaceholderText("要盯的网址")).toBeInTheDocument());
    // 权限档必须是保守的两档——只检查「权限」这一个 select 的选项，
    // 不要全页面扫（catch_up 的「定点执行」也含「执行」二字，会误判）
    const permSelect = screen.getByDisplayValue("只读（只看本地，不联网）");
    const permOpts = Array.from(permSelect.querySelectorAll("option")).map((o) => o.textContent ?? "");
    expect(permOpts).toHaveLength(2);
    expect(permOpts.some((o) => o.includes("只读 + 联网"))).toBe(true);
  });

  it("校验：缺名称或提示词时拒绝保存并给出原因", async () => {
    const create = vi.spyOn(api, "createSchedule");
    mount([]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("+ 新建"));
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(screen.getByText("请填写计划名称")).toBeInTheDocument());
    expect(create).not.toHaveBeenCalled();
  });

  it("cron 段数不对时拒绝保存", async () => {
    mount([]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("+ 新建"));
    fireEvent.change(screen.getByPlaceholderText("如：盯部署状态"), { target: { value: "x" } });
    fireEvent.change(screen.getByPlaceholderText("告诉 Agent 发现变化后做什么"),
      { target: { value: "y" } });
    fireEvent.change(screen.getByDisplayValue("按间隔"), { target: { value: "cron" } });
    fireEvent.change(screen.getByPlaceholderText("0 9 * * *"), { target: { value: "bad" } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(screen.getByText(/cron 需要 5 段/)).toBeInTheDocument());
  });

  it("保存新计划：载荷含权限档与启用状态，并绑定当前会话", async () => {
    const create = vi.spyOn(api, "createSchedule")
      .mockResolvedValue({ schedule: base() } as never);
    mount([]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("+ 新建"));
    fireEvent.change(screen.getByPlaceholderText("如：盯部署状态"), { target: { value: "盯部署" } });
    fireEvent.change(screen.getByPlaceholderText("告诉 Agent 发现变化后做什么"),
      { target: { value: "总结" } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(create).toHaveBeenCalled());
    const payload = create.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.name).toBe("盯部署");
    expect(payload.permissions).toBe("read");
    expect(payload.enabled).toBe(true);
    expect(payload.session_id).toBe("sess_1");
  });

  it("启停计划：调用 PATCH 翻转 enabled", async () => {
    const update = vi.spyOn(api, "updateSchedule").mockResolvedValue({ schedule: base() } as never);
    mount([base()]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("停用"));
    await waitFor(() => expect(update).toHaveBeenCalledWith("sch_1", { enabled: false }));
  });

  it("删除计划：调用 DELETE", async () => {
    const del = vi.spyOn(api, "deleteSchedule").mockResolvedValue({ ok: true } as never);
    mount([base()]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("删除"));
    await waitFor(() => expect(del).toHaveBeenCalledWith("sch_1"));
  });

  it("立即跑：调用 run 并展示触发结果", async () => {
    const run = vi.spyOn(api, "runSchedule").mockResolvedValue(
      { ok: true, run: { status: "changed", detail: "有变化" } } as never);
    mount([base()]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("立即跑"));
    await waitFor(() => expect(run).toHaveBeenCalledWith("sch_1"));
    await waitFor(() => expect(screen.getByText(/已触发/)).toBeInTheDocument());
  });

  it("后端报错时展示错误文案而不崩", async () => {
    vi.spyOn(api, "schedules").mockRejectedValue(new Error("409 计划不存在"));
    const { container } = render(<ScheduleManager sources={SOURCES} sessionId="sess_1" />);
    await waitFor(() => expect(container.querySelector(".sched-err")).not.toBeNull());
    expect(container.textContent).toContain("409 计划不存在");
  });
});
