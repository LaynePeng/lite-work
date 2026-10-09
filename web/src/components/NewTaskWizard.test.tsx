// SPDX-License-Identifier: Apache-2.0
// 新建任务向导弹窗单测：空目标禁用 / 提交配置 / 非 git 置灰 / 取消。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import NewTaskWizard from "./NewTaskWizard";
import type { CollabMode } from "../types";

const collabModes: CollabMode[] = [
  { name: "review", display_name: "评审模式", description: "d", source: "builtin", version: "1" },
];

function renderWizard(props?: Partial<React.ComponentProps<typeof NewTaskWizard>>) {
  const onCancel = vi.fn();
  const onSubmit = vi.fn();
  const view = render(
    <NewTaskWizard
      collabModes={collabModes}
      workspaceIsGit
      onCancel={onCancel}
      onSubmit={onSubmit}
      {...props}
    />
  );
  return { onCancel, onSubmit, view };
}

describe("NewTaskWizard 新建任务向导", () => {
  it("空目标时「开始任务」禁用；默认选中先规划再执行", () => {
    renderWizard();
    expect(screen.getByRole("button", { name: "开始任务" })).toBeDisabled();
    // 默认执行方式 = 先规划再执行
    const plan = screen.getByRole("radio", { name: /先规划再执行/ });
    expect(plan).toBeChecked();
  });

  it("输入目标后提交完整配置（默认 mode=plan，开关全关，collab 跟随默认）", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderWizard();
    await user.type(screen.getByRole("textbox", { name: "任务目标" }), "写一份季度 PPT 大纲");
    await user.click(screen.getByRole("button", { name: "开始任务" }));
    expect(onSubmit).toHaveBeenCalledWith({
      goal: "写一份季度 PPT 大纲",
      mode: "plan",
      loop: false,
      autoContinue: false,
      worktree: false,
      collab: "",
      planItems: [],
    });
  });

  it("勾选开关与协作模式后提交对应配置", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderWizard();
    await user.type(screen.getByRole("textbox", { name: "任务目标" }), "调查竞品定价");
    await user.click(screen.getByRole("radio", { name: /直接干/ }));
    await user.click(screen.getByRole("checkbox", { name: /目标循环/ }));
    await user.click(screen.getByRole("checkbox", { name: /自动续推/ }));
    await user.click(screen.getByRole("checkbox", { name: /隔离工作树/ }));
    await user.selectOptions(screen.getByRole("combobox", { name: /协作模式/ }), "review");
    await user.click(screen.getByRole("button", { name: "开始任务" }));
    expect(onSubmit).toHaveBeenCalledWith({
      goal: "调查竞品定价",
      mode: "direct",
      loop: true,
      autoContinue: true,
      worktree: true,
      collab: "review",
      planItems: [],
    });
  });

  it("当前项目非 git 仓库时「隔离工作树」开关置灰", () => {
    renderWizard({ workspaceIsGit: false });
    expect(screen.getByRole("checkbox", { name: /隔离工作树/ })).toBeDisabled();
  });

  it("填写计划项：每行拆分、去空行、去首尾空格后随配置提交", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderWizard();
    await user.type(screen.getByRole("textbox", { name: "任务目标" }), "发布新版本");
    const plan = screen.getByPlaceholderText(/预置 TODO 计划/);
    await user.type(plan, "收集素材\n 写正文第三章 \n\n排版校对");
    await user.click(screen.getByRole("button", { name: "开始任务" }));
    const lastCall = onSubmit.mock.calls[onSubmit.mock.calls.length - 1][0];
    expect(lastCall.planItems).toEqual(["收集素材", "写正文第三章", "排版校对"]);
  });

  it("「取消」关闭向导（onCancel 触发；提交不触发）", async () => {
    const user = userEvent.setup();
    const { onCancel, onSubmit } = renderWizard();
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
