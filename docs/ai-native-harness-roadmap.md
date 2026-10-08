# AI-Native Harness Roadmap — Handbook-Informed Plan & Design

**Status**: proposal (not yet implemented) · **Owner**: TBD · **Last updated**: 2026-10-08

**Source material**: *AI-Native Software Engineering — A Practical Handbook* (阿里巴巴《AI Native
研发范式实践手册》, 68 pages, scanned PDF, 2026). Chapter map in [Appendix B](#appendix-b-handbook-index).

This document turns that handbook into a concrete, prioritized engineering plan for lite-work. It is a
*plan plus design*, not an implementation: nothing here is shipped yet. Everything claimed about the
current codebase was verified by reading the code at the commit that introduced this file.

---

## 1. Why this handbook matters for lite-work

lite-work is an **Agent Harness**: it owns the context, the tool surface, the permission gates and the
execution loop of an autonomous coding agent. The handbook's third chapter ("Enterprise AI R&D
Infrastructure", pp. 33–61) is essentially a specification of that same artifact, written from
enterprise production experience. Its four core Harness responsibilities (p. 33) map one-to-one onto
our modules, and its five *challenges* (pp. 15–32) are the failure modes we should expect as users push
lite-work into longer, higher-risk tasks.

The most valuable transferable ideas are **protocols**, not features:

| Handbook protocol | One-line summary |
| --- | --- |
| Guardrail (pp. 60–63) | Rules + immutable ChangeSet + three-state (`PASS`/`BLOCKED`/`UNKNOWN`) submissions with mandatory Evidence, aggregated **mechanically**; `UNKNOWN` never counts as pass. |
| Trajectory (pp. 64–66) | A standard object model (`Session/Task/Step/ModelCall/ToolCall/SkillExecution/StateChange/Outcome`) linking system telemetry to behavioural semantics; reused as evaluation data. |
| Environment manifest (pp. 52–55) | The project's *runtime* (toolchain, config, data baseline, network boundary, **verification commands**, artifacts) is first-class context, not tribal knowledge. |
| Identity & Policy (pp. 56–59) | Effective permission = user ∩ agent capability ∩ platform policy ∩ delegation ∩ runtime constraints; deterministic decisions; a third outcome, *Challenge*, instead of a raw 403. |
| Sandbox lifecycle (pp. 48–51) | Explicit lifecycle states, server-side TTL reclamation (no orphans), structured exec surface, default-deny egress, credentials never inside the sandbox. |
| Tool evaluation (p. 47) | Hit rate (was the right tool/skill chosen?) and success rate (did it complete?), fed back into descriptions, splits and deletions. |

A principle worth restating, because lite-work already follows it: *strictly enforced rules must be
enforced by deterministic code, not by asking the model to remember* (p. 34). Where our current
enforcement is prompt-based, that is a bug.

---

## 2. What lite-work already does well

Verified in-tree; these are **not** workstreams below.

| Handbook requirement | Current implementation |
| --- | --- |
| Huge tool outputs degrade the context; archive them and keep a summary | Observation packing (`observation_pack`) + `obs_recall` paging: full text archived under `~/.lite-work/truncations/observations`, placeholder in context (`litework/core/agent_loop.py:187-194`). |
| Compression must never lose evidence ("fail-open") | Evidence-receipt reducer, opt-in, falls back to the raw result on any failure (`litework/core/agent_loop.py:743-771`). |
| Deterministic enforcement of red lines | `SecurityGuard` (sensitive-env scrubbing, dangerous-command blacklist) + `ApprovalGate` (600 s, `litework/security/`) + worktree isolation guard (`litework/core/agent_loop.py:884-920`). |
| Task claiming / shared work | Shared task pool + atomic claim, plus collab plugins (`litework/builtin_plugins/collab-*`). |
| Skill trigger conditions | `SKILL.md` frontmatter `triggers`, `version`, `allowed-tools`, `argument-hint`; index injection with `skill_trigger_mode=substring` (`litework/tools/skills.py`). |
| Progressive delegation | Agent profiles with `domains` + `extra_tools`; child permissions can never exceed the parent's (`litework/core/permissions.py`, `litework/orchestration/`). |
| Cost/token observability | Per-session task snapshot: prompt/output tokens, cache hit/miss, compression count, `tool_calls`, `blocked`, cost estimate (`litework/app.py:1087-1100`). |

---

## 3. Workstreams

Priority: **P0** = small, closes a real gap, fits existing architecture · **P1** = structural, medium
effort · **P2** = large or product-dependent. Sizes are engineering-days for one engineer.

### W1 — Background-command orphan reclamation (P0, ~1d)

**Motivation.** The Sandbox lifecycle control plane must reclaim expired instances server-side,
because an agent can lose its manager through a crash or disconnect (p. 49: "TTL is an easily
underestimated design … the service side reclaims on expiry to avoid orphan instances").

**Current state.** `BackgroundRegistry` (`litework/tools/shell.py`) tracks background tasks with
per-task timeouts, status listing and `kill`, and it is shared app-wide
(`litework/app.py: self._bg_registry`). Tasks are **in-memory only**: if the backend dies, the child
processes have no owner and nothing reclaims them on the next start. Worktree leftovers *are*
cleaned up (`litework/app.py:1445 worktree_clean`), which shows the pattern exists for other
resource types.

**Design.** Persist a task journal and reconcile on startup.

```
$LITEWORK_CONFIG_DIR/bg_tasks.json      # written on add(); removed on harvest/kill
{ "tasks": [ { "task_id": "...", "pid": 12345, "pgid": 12345,
               "command": "npm run dev", "workspace": "/abs/path",
               "started_at": 1759900000.0, "timeout_s": 300 } ] }
```

* `BackgroundRegistry.add/remove` write-through the journal (best-effort, never raises into the tool).
* New `AgentApp.reclaim_orphan_tasks()` called from server startup, before accepting requests:
  for each journal entry, if the pid is alive **and** its start time matches (guard against pid reuse),
  kill the process tree and log `[reclaim] killed orphan pid=… task_id=…`; then clear the journal.
* Extend `/api/status` with `running_background: <int>` so the UI can surface leftovers.

**Tests.** (1) Unit: journal round-trip; stale pid → no kill; live pid → `kill_process_tree` called
(monkeypatch). (2) Integration: start backend, run a background command, `SIGKILL` the backend, restart
with the same config dir, assert the child is gone and the journal is empty.

**Acceptance.** After any forced restart, `pgrep` shows no process that was started by a pre-restart
background task, and `/api/status.running_background == 0`.

**Risks.** Pid reuse → mitigate with start-time comparison and by only killing pids we ourselves
recorded. Never kill on evidence older than N days without logging loudly.

---

### W2 — Completion evidence gate (P0, ~3d)

**Motivation.** Handbook pp. 60–63 (Guardrail): high-risk actions must be gated by *rules + facts +
Evidence*, with a **three-state** result and **mechanical** aggregation; a missing required check, a
`BLOCKED`, or an `UNKNOWN` cannot produce a pass, and `UNKNOWN` must never be reinterpreted as `PASS`.
Today lite-work has gates for *permission* (approval) and *quality opinion* (Jev), but nothing that
ties "the task says it is done" to "here is the evidence".

**Current state.** `ApprovalGate` (human confirmation, `litework/security/approval.py`), Jev-based
judgement plugin, `review_code`, `pytest`/`mypy` CI. The "evidence receipt" reducer exists but only
for token economy. Task completion is asserted by the model in prose — this session produced a
concrete failure of exactly that kind ("done" claimed without re-running the checks).

**Design — keep it small and local, mirroring Guardrail's shape.**

```yaml
# .litework/gate.yml (optional, per project; defaults are built in)
version: 1
spec_id: litework-completion-v1
checks:
  - id: verify-commands
    required: true
    criteria:
      pass:    "every command in the change set was executed and its recorded exit code is 0"
      blocked: "a recorded exit code is non-zero"
      unknown: "no command was run, or output was unparseable"
    evidence: ["command", "exit_code", "captured_at", "output_excerpt"]
  - id: changed-files
    required: true
    criteria: { pass: "change set non-empty and inside the workspace", blocked: "path outside workspace", unknown: "no change set recorded" }
    evidence: ["paths", "source: git|write_tool"]
```

* New module `litework/core/gate.py`: `GateSpec` (load + validate + `digest`), `GateRun`
  (`spec_digest`, `change_set_digest`, append-only `submissions`), `aggregate()` implementing:
  *missing required check → not pass; any `BLOCKED` → blocked; any `UNKNOWN` → unknown; else pass.*
* New tool `gate_submit` (one check at a time, `PASS|BLOCKED|UNKNOWN` + Evidence), registered for
  write-capable agents only, and **not** available to the agent that *consumes* the result.
* The result is exposed in the task snapshot and as an event `gate:result`; a config flag
  `completion_gate = "off" | "advisory" | "enforced"` decides whether a non-pass result merely warns or
  blocks the final "done" claim (rollout: advisory first).
* Evidence is stored as *redacted summaries plus references*, never credentials or full logs (p. 63).

**Tests.** Aggregation truth table (missing/BLOCKED/UNKNOWN/partial-pass); spec digest stability;
evidence redaction; `enforced` mode rejects a completion claim with no verify command; `advisory`
mode only emits an event.

**Acceptance.** With `enforced`, a task that edits `.py` files without running a verification command
cannot report success; with `advisory`, the same task completes but the UI shows an `UNKNOWN` gate.

**Risks.** Over-gating is worse than no gating (see the earlier over-strict background-command block).
Mitigate: ship `advisory`, keep the default rule set tiny (two checks), make per-project opt-out easy.

---

### W3 — Skill/tool evaluation loop (P0, ~3d)

**Motivation.** Handbook p. 47: as tools and skills multiply, the agent may pick the wrong capability
or fail to use the right one; measure **hit rate** and **success rate**, then feed the results back
into descriptions, skill steps/boundaries, or deletion. p. 46 adds that CLI/MCP capabilities need a
companion Skill to tell the agent *when* to use them.

**Current state.** `SKILL.md` frontmatter has `triggers`, `description`, `allowed-tools`
(`litework/tools/skills.py`), matching is substring-based (`skill_trigger_mode`). Aggregate counters
exist (`tool_calls`, `blocked` in `litework/app.py:1087`), but nothing is attributed per skill or per
tool, and there is no notion of a *wrong* choice.

**Design.**

1. Frontmatter additions (backward compatible, unknown keys already ignored):
   `scope:` (when it applies), `not-for:` (negative triggers — the highest-value signal for hit rate).
2. Attribution: every `tool:before_execute` / `tool:after_execute` event already carries the tool name
   (`litework/core/events.py:357`); add `skill_id` and `skill_source: auto|explicit` when the call came
   from a skill-triggered context, and a derived `outcome: ok|error|cancelled`.
3. Metric store: append-only JSONL per session under `$CONFIG_DIR/metrics/tool_events.jsonl`
   (`{ts, session_id, agent_id, tool, skill_id, outcome, duration_ms, tokens_hint}`).
4. Report: a CLI/`/api/metrics/tools` view with hit rate (skill triggered *and* its tools used), success
   rate (no `error`/retry), and the top-N failing pairs; suggestions are generated for humans, never
   auto-edited.

**Tests.** Fixture session → expected counters; `not-for` suppresses a trigger; missing frontmatter
degrades gracefully.

**Acceptance.** Running the report on real sessions lists at least: per-skill trigger count, per-tool
success rate, and the three lowest-success tools; adding a `not-for` line demonstrably changes
triggering in a unit test.

**Risks.** Instrumentation cost must stay negligible (append-only, no serialization of payloads).

---

### W4 — Capability manifests: declared permissions & dependencies (P1, ~2d)

**Motivation.** p. 28–30 (capability versioning: a capability must be traceable to a source and
version, and its usable scope must be visible); p. 56 (policy: *what may this agent do, on which
resources*). Installing a plugin or upgrading a skill silently widens the attack surface today.

**Current state.** Community plugins are described by a `manifest.json` fetched by
`litework/tools/plugin_loader.py:856+`; the parser only understands `name`/`version`/entry/tools —
no `permissions`/`requires`. Built-in plugins are directories (`plugin.py`, `recipe.md`, `icon.svg`) with
no manifest at all. MCP tools are gated per *server* (`enabled`) and always prompt on call
(`litework/mcp/manager.py:77-94`).

**Design.**

```json
{ "name": "office-plugin", "version": "1.4.1",
  "permissions": { "fs_write": ["产出物/**"], "exec": [], "network": [], "skills": ["load_skill"] },
  "requires": { "python": ">=3.11", "binaries": ["python3"] } }
```

* Extend the loader to parse and validate these fields (unknown fields ignored, not fatal).
* Installation/upgrade computes a *permission diff* against the installed version and requires explicit
  confirmation when permissions grow; publish the diff in the plugin panel.
* Surface the declared permissions in the plugin list API so the UI can show "this plugin can execute
  commands" before the first run.

**Tests.** Parse/validate; upgrade with widened permissions → flagged; narrowed → silent; MCP server
entries keep working unchanged (regression).

**Acceptance.** A crafted plugin declaring `exec` produces a visible warning at install time, and the
existing community sync flow (`AGENTS.md` §6) still works end to end.

---

### W5 — Project environment manifest (P1, ~3d)

**Motivation.** pp. 52–55: delivering a repository is not delivering a runnable project; toolchain,
config, data baseline, network boundary, *verification command* and artifacts must become
program-consumable context. pp. 16–20 (challenge 1: environment- and verification-driven work) is the
most cited blocker in the case studies.

**Current state.** `litework/tools/project_scaffold.py` creates `AGENTS.md`, `素材/{原始,参考}` and
`产出物/<品类>/` — rules and folders, but no runtime contract. The system prompt injects `AGENTS.md`
only (`litework/core/system_prompt.py:57`).

**Design.**

```yaml
# .litework/env.yml  (project-scoped, committed by the user)
version: 1
runtime: { python: "3.11", node: "20" }
setup:   ["pip install -e '.[dev]'"]
verify:  ["pytest -q", "npm test --prefix web"]
network: { allow: ["pypi.org", "registry.npmjs.org"], default: deny }
artifacts: ["web/dist/**"]
```

* New loader `litework/core/env_manifest.py` (validate + expose), injected into the *stable* part of the
  system prompt as a compact summary (commands only, not full text), and shown in the file tab.
* `project-init` skill generates a first draft by detecting languages/`package.json`/`pyproject.toml`.
* Wire `verify` into W2's gate as the default source of verification commands (so the two features
  compose instead of duplicating).
* Out of scope for this workstream: actual sandboxing of `network.allow`; that is W8.

**Tests.** Loader validation (missing file → default; malformed → warning + default); prompt injection
contains the verify commands; scaffold produces a loadable file.

**Acceptance.** A fresh project created through the UI has `.litework/env.yml` whose `verify` commands
run green, and the agent's prompt shows them without the user pasting anything.

---

### W6 — Untrusted-content provenance & injection guard (P1, ~3d)

**Motivation.** p. 55: the input side needs a "prompt firewall": recognize untrusted input and
injection attempts, because a wrong action may originate from a fetched page/comment rather than user
intent. pp. 48–50 also warn that allowing arbitrary network egress amplifies prompt-injection and
dependency-poisoning consequences.

**Current state.** Nothing marks content provenance. `webfetch` results, file contents and tool
outputs enter the context as plain strings; the only protections are env scrubbing, a dangerous-command
blacklist, the approval gate and path guards (verified: no `untrusted`/injection handling anywhere in
`litework/`).

**Design.**

1. **Tag provenance at the boundary**: tool results carry `trust: "local" | "external"`
   (`webfetch`, MCP results, uploaded files → `external`; repo files, our own tool output → `local`).
2. **Wrap external content** in an explicit envelope with a fixed trailer:
   `<untrusted source="web" url="…"> … </untrusted>` plus a prompt rule: content inside is data, never
   instructions.
3. **Escalate on combination**: if the *same turn* contains external content **and** a high-risk tool
   call (domain `execute` or an external-write MCP tool), require approval even if the tool would
   normally be auto-approved, and label the approval card with the provenance ("requested after reading
   an external page").
4. **Optional rule-based detector** for classic patterns (`ignore previous instructions`,
   `system:`/role-marker spoofing, base64 blobs near instruction verbs) → warn event, never a hard block
   (false positives are expensive).

**Tests.** Envelope formatting; provenance propagation through compaction; escalation triggers only on
the combination; detector unit cases; regression: a normal local edit still auto-approves.

**Acceptance.** A crafted page asking the agent to run a destructive command yields a labelled approval
request instead of a silent execution.

---

### W7 — Trajectory model & export (P1, ~5d)

**Motivation.** pp. 64–66: system telemetry answers "is it healthy"; a **Trajectory** answers "why did
it behave this way". The object model is given explicitly (Session/Task/Trajectory/Step/ModelCall/
ToolCall/SkillExecution/StateChange/Outcome), linked to traces by `TraceContext`, and reused as
evaluation data through a deterministic-rules-first **cascade funnel** (rules over all → LLM on the
grey zone → dimension analysis).

**Current state.** Rich but transient: SSE events (`litework/server/tasks.py:22`), session snapshots
(`litework/core/session_store.py`), task counters, `obs_recall` archives. There is **no** trajectory
concept and no export (verified: zero hits for `trajectory`/`轨迹` in `litework/`).

**Design.** Two layers, deliberately modest:

1. **Emit**: extend the existing event bus with the missing semantic fields (`step_id`, `parent_step_id`,
   `turn`, `model_call_id`) so every emitted event can be placed on a trajectory without new plumbing.
2. **Persist**: `$CONFIG_DIR/trajectories/<session_id>/<task_id>.jsonl`, one JSON object per line:
   `{"type": "step|model_call|tool_call|skill_exec|state_change|outcome", "ts": …, "step_id": …,
     "data": {…}}` with a `header` line carrying `session_id`, `task_id`, `workspace`, `agent_id`,
   `started_at`, and a `trace` object (`trace_id`, `span_id`) ready for OTel/ATIF interop.
3. **Export/analysis**: `GET /api/trajectories/{session_id}` (paginated) + a CLI summary that runs the
   cascade funnel's step 1: deterministic rules over all trajectories (failed tool, retry storm,
   context-growth, cost outlier), leaving ranked candidates for a human or an LLM pass.

**Tests.** Event → trajectory line mapping; header/step ordering; export pagination; rule detection on
synthetic trajectories (one per rule).

**Acceptance.** For any completed session, `trajectories/…jsonl` reconstructs a readable timeline, and
the rule pass flags at least the retry-storm and failure cases in fixtures.

**Risks.** Storage growth → retention policy (size-cap per session, prune oldest), redaction of
secrets must reuse the existing scrubbing rules.

---

### W8 — Sandbox-grade isolation & egress policy (P2, ~10d+, backlog)

**Motivation.** pp. 48–51 (Sandbox architecture: lifecycle control plane, exec surface, network
boundary, credential boundary) and pp. 52–55 (environment as first-class context).

**Current state.** Local shell with a blacklist and a hard timeout; worktree isolation per session;
credentials are stripped from the environment (`SENSITIVE_ENV_VARS`). No OS-level isolation, no
egress control, no PTY session for interactive tools.

**Assessment.** For a single-user desktop app the cost/benefit is poor: we are not running untrusted
multi-tenant workloads. **Recommendation: keep as backlog**; if the product ever targets teams, the
entry point is (a) an egress allowlist enforced by a proxy, (b) placeholder credentials injected by a
sidecar, (c) explicit lifecycle states surfaced in `/api/status`. Tracked here for completeness.

---

### W9 — Adoption / productivity metrics (P2, ~3d)

**Motivation.** pp. 27–28 (challenge 3: is AI actually improving delivery?) and p. 37, which shows the
actual query shape used internally: join a per-session fact table with a code fact table to compute an
adoption rate.

**Current state.** Token/cost/cache counters exist; git integration exists (`litework/tools/git.py`,
`git_commit`); the `weekly-report` skill already produces periodic reports.

**Design.** Join task snapshots with commits: `{session_id, agent_id, commits, files_touched,
lines_added, lines_survived_7d}` → adoption rate, rewrite rate, cost per merged change; expose as JSON
and let `weekly-report` render it. Keep it local-only and opt-in.

**Acceptance.** For a session with commits, the report attributes changed lines to agent runs and
computes a cost per surviving line.

---

### W10 — Post-task reflection → skill draft (P2, ~4d)

**Motivation.** p. 29–31 (digital-employee stage: every task ends with a reflection; loop maintenance
focuses on environment/verification) and case study 1 (p. 3), where failed trajectories are turned into
skills ("失败轨迹→SKILL 化").

**Current state.** Sub-agents produce structured reports; shared tasks exist; skills are
frontmatter + Markdown. There is no reflection artifact and no path from a session to a new skill.

**Design.** At task end, generate a short structured reflection into
`$CONFIG_DIR/reflections/<session_id>.md` (`what worked`, `what failed`, `rule that would have prevented
it`) from the trajectory (W7) — rules first, LLM second. A UI action "save as skill draft" renders a
`SKILL.md` skeleton (name/description/triggers/not-for/steps) into `skills/drafts/` for human review;
nothing is auto-published (consistent with the plugin governance rules in `AGENTS.md` §6).

**Acceptance.** A task that hit and recovered from a failure yields a reflection naming the failure
class, and the draft is loadable by the skill loader after a human moves it into place.

---

## 4. Sequencing

```
P0  W1 orphan reclamation ──┐
    W2 evidence gate ───────┼──> (both touch task lifecycle; do W1 first: it is 1 day and unblocks clean restarts)
    W3 skill/tool metrics ──┘        (W3 depends on event fields, independent of W1/W2)

P1  W5 env manifest ──> feeds verify commands into W2's gate (do after W2)
    W4 capability manifests ── independent
    W6 provenance guard ── independent
    W7 trajectory ──> data source for W10 and for W3's richer metrics
P2  W9 metrics, W10 reflection (both prefer W7), W8 sandbox (backlog)
```

Rationale: W1 is a one-day structural fix that removes orphan processes. W2 is the highest-value
protocol (it makes "done" mean something) and needs W5 for good default verification commands. W3 gives
the feedback loop that keeps tool/skill descriptions honest as the surface grows. W7 should land before
W9/W10 to avoid building analytics on ephemeral data.

---

## 5. Cross-cutting principles (apply to every workstream)

1. **Deterministic enforcement, not reminders.** If a rule must hold, encode it in code; prompts are
   for guidance (handbook p. 34). When reviewing a design, ask: "what happens if the model ignores the
   instruction?"
2. **`UNKNOWN` is not `PASS`.** Absence of evidence must resolve downwards. The existing Jev behaviour
   (low confidence → fall back to base rules, do not adopt) is the same principle.
3. **Fail open on evidence, fail closed on permission.** Compression and summarization must never
   silently drop the raw record; permission gates must default to "ask".
4. **Never over-block.** The recent over-strict background-command block (reverted, see Appendix A) is
   the cautionary example: a guard that cannot be satisfied trains users to bypass it.
5. **No silent capability growth.** New permissions arrive with a diff, a version and a digest.
6. **Keep the surface small.** Every new tool costs context and adds a wrong-choice failure mode;
   prefer extending existing protocols (events, snapshots) over new subsystems.

---

## 6. Verification & rollout

* Each workstream ships with unit tests **and** one integration test through the HTTP API; backend
  gates are `LITEWORK_STRICT_EVENTS=1 pytest` + `mypy` (CI parity, see `AGENTS.md` §3/§3.1), frontend
  gates are `tsc -b` + `vitest`.
* New behaviour is feature-flagged (`completion_gate`, metrics collection, trajectory persistence) with
  **advisory defaults**; enforcement is opted into per project.
* Documentation: user-visible protocols get a section in `docs/usage.md` or `docs/web-api.md`; internal
  protocols (gate, trajectory, env manifest) get a dedicated page under `docs/`.
* Telemetry must be local-only; no new outbound calls.

---

## 7. Open questions

1. **W2 scope**: should the gate apply to sub-agents as well as the top-level task, or only where a
   human will read the result?
2. **W3 data volumetrics**: is append-only JSONL acceptable long-term, or should tool events go into the
   same store as trajectories (W7) from day one?
3. **W5 ownership**: `.litework/env.yml` is user-committed, but agents may want to *update* it (e.g. after
   adding a dependency). Do we allow agent writes with review, or keep it human-only?
4. **W6 false positives**: what is the acceptable approval-prompt increase? Needs measurement before
   enabling by default.
5. **W7 retention**: how many sessions should remain queryable by default (disk vs usefulness)?

---

## Appendix A — Recently shipped (reference for protocol style)

The file-tab **directory context menu** landed first, because unlike the items above it was a concrete
UI parity bug: files had a right-click menu, directories did not.

Backend (`litework/server/routers/office.py`):

| Endpoint | Contract |
| --- | --- |
| `POST /api/files/create` | `{parent, name, kind: "file"｜"dir"}` → `{ok, path, name, kind}`; guards: parent must exist and be inside the workspace, name is a single component (no separators, no `*?"<>\|`, not `.`/`..`), `.git/` is off-limits, existence → 409, `kind` validated. |
| `DELETE /api/files?path=&recursive=` | files delete as before; **directories require `recursive=true`** (400 otherwise) and are removed with `shutil.rmtree`; workspace root and `.git/` are refused; response gained `kind: "file"｜"dir"`. |
| `POST /api/files/rename` | now accepts directories: the extension-equality rule applies to **files only**; same-parent constraint and name validation unchanged; 409 message generalised. |

Frontend (`web/src/components/Sidebar.tsx`, `web/src/api.ts`): directory rows gained `onContextMenu`
with `新建文件 / 新建文件夹 / 重命名 / 在文件管理器中打开 / 复制路径 / 删除目录` plus inline rename;
the file menu is unchanged, decided by a `menu.isDir` discriminator. Batch delete deliberately still
refuses directories.

Verification at the time of writing: `tests/test_fs_dir_ops.py` (13 cases) + `Sidebar.test.tsx` (5 new
cases), full backend suite 700 passed, `mypy` clean, frontend 162 passed, `tsc -b` clean.

---

## Appendix B — Handbook index

| Section | Pages | Relevance to lite-work |
| --- | --- | --- |
| 1. Field case studies | 1–14 | Super-individual → digital employee → "cloud Scrum"; experience/hands/brain/evaluation split; failed trajectory → skill. |
| 2. Challenges | 15–32 | Environment & verification, platform capability, productivity measurement, digital-employee autonomy, organisational fit. |
| 3.1 Agent Harness | 33–47 | Four core responsibilities; knowledge base (4 capabilities, OKF); tool system (MCP/Skill/CLI); tool evaluation by hit/success rate. |
| 3.2 Runtime | 48–55 | Sandbox (four boundaries, lifecycle, TTL); coding environment as first-class context (env manifest). |
| 3.3 Identity & Safety | 56–63 | Composite identity; progressive narrowing; PDP/PEP; credential broker; *Challenge* outcome; Guardrail three-state protocol + Evidence + mechanical aggregation. |
| 3.4 Observability | 64–66 | System vs Behaviour layers; Trajectory object model; cascade funnel; trajectories as evaluation assets. |
| 4. Conclusion | 67–68 | Honest status: many items are early; expect architecture churn. |
