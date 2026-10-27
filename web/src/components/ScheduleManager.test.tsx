// 盯梢计划管理：纯函数（间隔人性化 / 计划摘要 / 下次运行时间）+ 列表与编辑器交互。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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
  // 插件源：探针参数里含数组字段（结构化 config_schema 写法）
  { name: "watch-astock", source_kind: "astock", display_name: "股票盯梢",
    description: "盯股价规则", version: "1.0.0", probe_interval_hint: 300,
    config_schema: {
      min_level: "最低信号级别（info / action / urgent）",
      rules: { type: "json", label: "价位规则", hint: "数组，每项 {code, kind, price, op}" },
    } },
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

// ---------------------------------------------------------------- probe_config
//
// 回归：探针参数是任意 JSON（后端 Dict[str, Any]）。编辑器曾用 String(v) 转换，
// 数组/对象变成 "[object Object]"，保存时把字符串写回后端 → 插件静默失效。

const RULES = [
  { code: "000651", kind: "stop_loss", price: 36.0, op: "lt" },
  { code: "600519", kind: "stop_loss", price: 1800, op: "lt" },
  { code: "000001", kind: "stop_loss", price: 10.5, op: "lt" },
];

const astockPlan = (over: Partial<ScheduleInfo> = {}): ScheduleInfo => base({
  id: "sch_a", name: "盯股价", source: "astock", prompt: "报告异动",
  probe_config: { min_level: "action", rules: RULES },
  ...over,
});

const mountWith = (items: ScheduleInfo[]) => {
  vi.spyOn(api, "schedules").mockResolvedValue({ schedules: items } as never);
  return render(<ScheduleManager sources={SOURCES} sessionId="sess_1" />);
};

const openEdit = async (name: string) => {
  await waitFor(() => expect(screen.getByText(name)).toBeInTheDocument());
  fireEvent.click(screen.getByText("编辑"));
};

const jsonBox = (container: HTMLElement) =>
  container.querySelector("textarea.sched-json") as HTMLTextAreaElement | null;

/** 把组件用到的那条 JSON 编辑框样式注入 jsdom，才能断言 computed style。 */
function injectSchedJsonCss(): string {
  // vitest 里 import.meta.url 不是 file: 协议，直接用 cwd 拼路径
  const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");
  const rule = css.match(/\.sched-field > textarea\.sched-json \{[^}]*\}/)?.[0] ?? "";
  expect(rule).not.toBe("");
  const style = document.createElement("style");
  style.textContent = rule;
  document.head.appendChild(style);
  return rule;
}

describe("ScheduleManager：probe_config 的数组/对象字段", () => {
  afterEach(() => vi.restoreAllMocks());

  it("打开编辑器时保留结构：textarea 里是格式化 JSON，不是 [object Object]", async () => {
    const { container } = mountWith([astockPlan()]);
    await openEdit("盯股价");
    const box = jsonBox(container);
    expect(box).not.toBeNull();
    expect(box!.value).not.toContain("[object Object]");
    expect(box!.value).toContain('"code"');
    expect(box!.value).toContain('"price"');
    // 格式化过（有缩进换行），不是压成一行
    expect(box!.value.split("\n").length).toBeGreaterThan(3);
    // 纯字符串字段仍是单行 input
    expect(screen.getByDisplayValue("action")).toBeInTheDocument();
  });

  it("原样保存：提交的 rules 仍是数组，内容与原始一致", async () => {
    const update = vi.spyOn(api, "updateSchedule")
      .mockResolvedValue({ schedule: astockPlan() } as never);
    mountWith([astockPlan()]);
    await openEdit("盯股价");
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const payload = update.mock.calls[0][1] as Record<string, unknown>;
    const cfg = payload.probe_config as Record<string, unknown>;
    expect(Array.isArray(cfg.rules)).toBe(true);
    expect(cfg.rules).toEqual(RULES);
    expect(cfg.min_level).toBe("action");   // 普通字符串字段不受影响
  });

  it("编辑 JSON 文本后保存：提交修改后的数组", async () => {
    const update = vi.spyOn(api, "updateSchedule")
      .mockResolvedValue({ schedule: astockPlan() } as never);
    const { container } = mountWith([astockPlan()]);
    await openEdit("盯股价");
    const box = jsonBox(container)!;
    fireEvent.change(box, { target: { value: box.value.replace("36", "34.5") } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const cfg = (update.mock.calls[0][1] as Record<string, unknown>).probe_config as
      { rules: { price: number }[] };
    expect(Array.isArray(cfg.rules)).toBe(true);
    expect(cfg.rules[0].price).toBe(34.5);
    expect(cfg.rules).toHaveLength(RULES.length);
  });

  it("非法 JSON：给出可操作报错并阻止提交", async () => {
    const update = vi.spyOn(api, "updateSchedule");
    const create = vi.spyOn(api, "createSchedule");
    const { container } = mountWith([astockPlan()]);
    await openEdit("盯股价");
    fireEvent.change(jsonBox(container)!, { target: { value: "{bad" } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(screen.getByText(/不是合法 JSON/)).toBeInTheDocument());
    expect(update).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });

  it("编辑过程中控件不抖动：清空或敲一半仍是 JSON 编辑器", async () => {
    const update = vi.spyOn(api, "updateSchedule")
      .mockResolvedValue({ schedule: astockPlan() } as never);
    const { container } = mountWith([astockPlan()]);
    await openEdit("盯股价");
    // 全选删光：值变成空串，控件必须还是 textarea（否则光标/内容当场丢失）
    fireEvent.change(jsonBox(container)!, { target: { value: "" } });
    expect(jsonBox(container)).not.toBeNull();
    // 敲一半（不完整 JSON）仍是 textarea
    fireEvent.change(jsonBox(container)!, { target: { value: "[{" } });
    expect(jsonBox(container)).not.toBeNull();
    // 清空后保存：该字段被跳过（不写空串覆盖原结构）
    fireEvent.change(jsonBox(container)!, { target: { value: "  " } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const cfg = (update.mock.calls[0][1] as Record<string, unknown>).probe_config as
      Record<string, unknown>;
    expect("rules" in cfg).toBe(false);
    expect(cfg.min_level).toBe("action");
  });

  it("数字/布尔字段不做无根据的类型还原：仍是单行 input", async () => {
    // 源没声明 type，原值是数字/布尔——编辑器用文本承载，保存时按原类型还原
    const update = vi.spyOn(api, "updateSchedule")
      .mockResolvedValue({ schedule: base() } as never);
    const { container } = mountWith([
      base({ id: "sch_n", name: "盯阈值", source: "file", prompt: "x",
        probe_config: { path: "a.log", threshold: 60, strict: true } }),
    ]);
    await openEdit("盯阈值");
    expect(jsonBox(container)).toBeNull();       // 数字/布尔不是 JSON 编辑器
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const cfg = (update.mock.calls[0][1] as Record<string, unknown>).probe_config as
      Record<string, unknown>;
    expect(cfg.threshold).toBe(60);              // 不被退化成 "60"
    expect(cfg.strict).toBe(true);
    expect(cfg.path).toBe("a.log");
  });

  it("向后兼容：config_schema 为纯字符串的老数据源仍是单行 input", async () => {
    const { container } = mountWith([
      base({ id: "sch_f", name: "盯日志", source: "file", prompt: "总结",
        probe_config: { path: "deploy.log" } }),
    ]);
    await openEdit("盯日志");
    expect(jsonBox(container)).toBeNull();
    const input = screen.getByDisplayValue("deploy.log");
    expect(input.tagName).toBe("INPUT");
    expect(input.getAttribute("placeholder")).toBe("工作区相对路径");
  });

  it("声明了 type: json 但还没值（新建）：仍渲染空 textarea 与 placeholder", async () => {
    mountWith([]);
    await waitFor(() => expect(api.schedules).toHaveBeenCalled());
    fireEvent.click(screen.getByText("+ 新建"));
    fireEvent.change(screen.getByDisplayValue("不盯（定点执行）"),
      { target: { value: "astock" } });
    const box = await waitFor(() => {
      const el = document.querySelector("textarea.sched-json") as HTMLTextAreaElement | null;
      expect(el).not.toBeNull();
      return el!;
    });
    expect(box.value).toBe("");                       // 不要把 null 画进框里
    expect(box.getAttribute("placeholder")).toContain("数组");
    expect(screen.getByText("价位规则")).toBeInTheDocument();   // 用 label 而非原始 key
  });

  it("历史损坏值（旧 bug 写回的 [object Object]）显式报错，不静默提交", async () => {
    const update = vi.spyOn(api, "updateSchedule");
    mountWith([
      base({ id: "sch_d", name: "旧的坏计划", source: "astock", prompt: "x",
        probe_config: { rules: "[object Object],[object Object]" } }),
    ]);
    await openEdit("旧的坏计划");
    // 损坏值也放进多行框（不让用户面对一个单行 input 无从下手）
    const box = document.querySelector("textarea.sched-json") as HTMLTextAreaElement | null;
    expect(box).not.toBeNull();
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(screen.getByText(/上次保存损坏/)).toBeInTheDocument());
    expect(update).not.toHaveBeenCalled();
  });

  it("清空损坏字段后保存：跳过该字段，其余字段正常提交", async () => {
    const update = vi.spyOn(api, "updateSchedule")
      .mockResolvedValue({ schedule: base() } as never);
    mountWith([
      base({ id: "sch_d2", name: "坏的半修", source: "astock", prompt: "x",
        probe_config: { min_level: "action", rules: "[object Object]" } }),
    ]);
    await openEdit("坏的半修");
    fireEvent.change(document.querySelector("textarea.sched-json")!,
      { target: { value: "" } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const cfg = (update.mock.calls[0][1] as Record<string, unknown>).probe_config as
      Record<string, unknown>;
    expect("rules" in cfg).toBe(false);
    expect(cfg.min_level).toBe("action");
  });

  it("多行 JSON 不撑开表单：编辑器有高度上限与内部滚动", async () => {
    injectSchedJsonCss();
    const { container } = mountWith([astockPlan()]);
    await openEdit("盯股价");
    const box = jsonBox(container)!;
    expect(box.className).toContain("sched-json");
    const cs = getComputedStyle(box);
    expect(cs.maxHeight).toBe("260px");
    expect(cs.overflowY).toBe("auto");
    expect(box.value.split("\n").length).toBeGreaterThan(10);   // 3 条规则已多行
  });

  it("JSON 编辑框用等宽字体，缩进可辨", async () => {
    injectSchedJsonCss();
    const { container } = mountWith([astockPlan()]);
    await openEdit("盯股价");
    const family = getComputedStyle(jsonBox(container)!).fontFamily;
    expect(family.toLowerCase()).toContain("monospace");
  });
});
