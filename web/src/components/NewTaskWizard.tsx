// SPDX-License-Identifier: Apache-2.0
// 新建任务向导弹窗：目标 + 执行方式（先规划/直接干）+ 三个可选开关 + 协作模式。
// 无模板、无任务类型快捷——目标由用户手动输入（与 dots/Muse 的「给目标」对齐，
// 但保留轻量：向导弹完即用现有管道执行，不让 AI 先出计划给你批准）。
import { useMemo, useState } from "react";
import type { CollabMode } from "../types";

export type TaskExecMode = "plan" | "direct";

/** 向导确认后的任务配置（App 层据此建会话/写目标/套配置/发引导指令）。 */
export interface NewTaskConfig {
  goal: string;
  mode: TaskExecMode;
  loop: boolean;
  autoContinue: boolean;
  worktree: boolean;
  /** 协作模式名称；"" = 跟随默认 */
  collab: string;
  /** 用户预填的计划项（每行一个）；空数组 = 交给 Agent 自己规划 */
  planItems: string[];
}

export default function NewTaskWizard({
  collabModes,
  workspaceIsGit,
  onCancel,
  onSubmit,
}: {
  collabModes: CollabMode[];
  workspaceIsGit: boolean;
  onCancel: () => void;
  onSubmit: (cfg: NewTaskConfig) => void;
}) {
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<TaskExecMode>("plan");
  const [loop, setLoop] = useState(false);
  const [autoContinue, setAutoContinue] = useState(false);
  const [worktree, setWorktree] = useState(false);
  const [collab, setCollab] = useState("");
  // 计划项（可选）：每行一个 TODO 步骤；留空则交给 Agent 规划
  const [planText, setPlanText] = useState("");

  const canStart = useMemo(() => goal.trim().length > 0, [goal]);

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal wizard-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>新建任务</h2>
          <button className="modal-close" onClick={onCancel} title="取消">✕</button>
        </div>
        <div className="modal-body wizard-body">
          <label className="wizard-label" htmlFor="wizard-goal">任务目标</label>
          <textarea
            id="wizard-goal"
            className="wizard-goal"
            placeholder="例如：给季度总结生成一份 PPT 大纲并起草内容…"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={3}
            autoFocus
          />

          <div className="wizard-section">
            <div className="wizard-label">
              计划项（可选）
              <span className="wizard-hint">每行一个步骤；留空则让 Agent 自动规划</span>
            </div>
            <textarea
              className="wizard-plan"
              placeholder={"（可选）预置 TODO 计划…\n例如：\n收集素材\n写正文第三章\n排版校对"}
              value={planText}
              onChange={(e) => setPlanText(e.target.value)}
              rows={4}
            />
          </div>

          <div className="wizard-section">
            <div className="wizard-label">执行方式</div>
            <div className="wizard-radio-row">
              <label className={`wizard-radio ${mode === "plan" ? "checked" : ""}`}>
                <input
                  type="radio" name="wizard-mode" checked={mode === "plan"}
                  onChange={() => setMode("plan")}
                />
                <span>先规划再执行（推荐）</span>
                <small>先建 TODO 看板再逐项推进，任务卡立即有进度</small>
              </label>
              <label className={`wizard-radio ${mode === "direct" ? "checked" : ""}`}>
                <input
                  type="radio" name="wizard-mode" checked={mode === "direct"}
                  onChange={() => setMode("direct")}
                />
                <span>直接干</span>
                <small>琐碎任务不强制规划</small>
              </label>
            </div>
          </div>

          <div className="wizard-check-row">
            <label className="wizard-check" title="任务结束后自动循环推进，直到目标完成（/loop）">
              <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} />
              目标循环 <code>/loop</code>
            </label>
            <label className="wizard-check" title="任务结束后若 TODO 有未完成项自动续推（/continue）">
              <input type="checkbox" checked={autoContinue} onChange={(e) => setAutoContinue(e.target.checked)} />
              自动续推 <code>/continue</code>
            </label>
            <label
              className={`wizard-check ${workspaceIsGit ? "" : "disabled"}`}
              title={workspaceIsGit
                ? "在独立分支+目录中执行，主工作区不受影响（/worktree）"
                : "当前项目不是 git 仓库，隔离工作树不可用"}
            >
              <input
                type="checkbox" checked={worktree} disabled={!workspaceIsGit}
                onChange={(e) => setWorktree(e.target.checked)}
              />
              隔离工作树 <code>/worktree</code>
            </label>
          </div>

          <div className="wizard-section wizard-collab-row">
            <div className="wizard-label">协作模式</div>
            <select className="wizard-collab" aria-label="协作模式" value={collab} onChange={(e) => setCollab(e.target.value)}>
              <option value="">跟随默认</option>
              {collabModes.map((m) => (
                <option key={m.name} value={m.name}>{m.display_name}</option>
              ))}
            </select>
          </div>

          <div className="wizard-actions">
            <button className="btn-cancel" onClick={onCancel}>取消</button>
            <button
              className="btn-start"
              disabled={!canStart}
              onClick={() => onSubmit({
                goal: goal.trim(), mode, loop, autoContinue, worktree, collab,
                planItems: planText.split(/\r?\n/).map((l) => l.trim()).filter(Boolean),
              })}
            >
              开始任务
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
