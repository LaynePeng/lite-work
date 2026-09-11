# lite-work Web API 与第三方 UI 开发指南

> 本文面向想基于 lite-work 的 Web API 构建自定义 UI 的开发者——
> 桌面端（Electron + React）之外的手机端、纯网页端、嵌入式面板等。
> 后端实现见 `litework/server/`（FastAPI），官方前端 `web/` 就是这些 API
> 的完整参考实现（`web/src/api.ts` + SSE 消费）。

## 1. 服务形态与鉴权

### 1.1 启动服务

```bash
python -m litework serve        # 自动生成随机 Token，打印在终端
python -m litework serve --port 8765 --host 0.0.0.0   # 局域网/手机可访问
python -m litework serve --no-token    # 仅本地调试，切勿暴露公网
```

`serve` 会同时托管 REST API（`/api/*`）和已构建的前端静态资源（`web/dist`）。
第三方 UI 只需要后端 API，不需要前端静态资源。

### 1.2 Bearer Token 鉴权

开启鉴权后，所有 `/api/*` 请求都要带头：

```http
Authorization: Bearer <token>
```

- 401 = token 缺失或错误；
- 409 = 尚未打开工作区（先调 `POST /api/workspace`）；
- CORS：默认放行 `localhost:5173`（vite 开发态），可用环境变量
  `LITEWORK_CORS_ORIGINS` 追加来源（手机网页端自建域名时需要）。

### 1.3 服务状态探测

```http
GET /api/status
```

```json
{
  "version": "1.6.2",
  "workspace": "C:\\codes\\demo",
  "model": "deepseek-flash",
  "active_provider": "deepseek",
  "api_key_configured": true,
  "active_tasks": 0,
  "token_auth": true
}
```

启动 UI 时先调它：`token_auth=false` 可免头直连；`workspace` 为空则先引导用户选项目。

## 2. 核心交互循环（必须理解的模型）

lite-work 的交互是 **「POST 一个任务 → SSE 订阅事件流 → HTTP 补充操作」**：

```text
┌────────┐  POST /api/chat        ┌──────────┐   GET /api/tasks/{id}/events (SSE)
│  你的   │ ─────────────────────▶ │  Core    │ ────────────────────────────────▶ 事件帧…
│  UI    │  POST /api/approve     │ (FastAPI)│
│        │  POST /api/tasks/stop  └──────────┘
└────────┘
```

关键设计：**任务与 SSE 分离**。`POST /api/chat` 立即返回 `task_id`，
事件流按 `task_id` 单独订阅。这样 UI 崩溃/刷新/断网后都能重连拿回进度——
任务期间未连过任何订阅者的事件会保留（环形缓冲 512 条），首个订阅者连上即补发。

### 2.1 发起任务

```http
POST /api/chat
{ "session_id": "s-abc123", "prompt": "帮我把 tests 全部跑一遍" }
```

响应：

```json
{ "task_id": "t-8f3c..." }
```

- 会话已有任务在跑时返回 `{"task_id": "...", "queued": true}`——
  输入不会丢，会在下一回合边界自动注入对话（排队消息机制）；
- 可选字段：`agent_id`（自定义 Agent）、`reasoning_effort`（"low"/"medium"/"high"）。

### 2.2 订阅事件流（SSE）

```http
GET /api/tasks/{task_id}/events        # text/event-stream
```

每 15 秒一条 `: keepalive` 注释保活；任务结束时发 `data: [DONE]` 并关闭。
每帧格式统一为 `{"type": "<事件名>", "data": <负载>}`：

```text
data: {"type": "llm:stream", "data": {"delta": "好的，我先看", "session_id": "..."}}

data: {"type": "tool:before_execute", "data": {"tool": "execute_command", "arguments": "{...}", ...}}

data: [DONE]
```

断线重连：直接重新 GET 同一 URL。事件是否会丢失取决于订阅过没有——
从未有订阅者时事件保留在缓冲；曾连上后断开，中间事件不回补，
但任务结束状态可通过 `GET /api/sessions` 读会话快照恢复。

### 2.3 事件清单与 UI 渲染建议

以下事件会转发到 SSE（`server/tasks.py` 的 `EVENT_FORWARD`）：

| 事件 | 负载要点 | UI 怎么用 |
|---|---|---|
| `llm:stream` | `delta` 流式增量 | 追加到当前助手气泡 |
| `llm:turn_start` / `llm:retry` | 轮次开始 / 重试中 | 显示"思考中/重试(第n次)"状态 |
| `message:added` | 新消息入上下文 | 历史同步（轮次内消息） |
| `tool:before_execute` | `tool`、`arguments` | 弹工具卡（标题+参数摘要） |
| `tool:after_execute` | `tool`、结果摘要、时长 | 工具卡收尾（成功/失败/耗时） |
| `approval:request` | `id`、`tool`、风险详情 | **渲染审批卡**（允许/拒绝按钮） |
| `approval:resolved` | `id`、`approved` | 按 id 关闭对应审批卡 |
| `question:request` / `question:resolved` | `id`、`question` | ask_user 提问卡：展示问题→提交答案 |
| `task:start` / `task:done` / `task:error` | 任务生命周期 | 开始/结束状态；error 显示错误 |
| `stats:update` | token 用量 | 顶部用量仪表 |
| `context:stats` | 窗口占用/压缩统计/`session` 会话累计 | 上下文面板 |
| `subagent:started` / `subagent:progress` / `subagent:completed` | 子 Agent id/role/进度/summary | 子 Agent 看板卡片 |
| `agent:closed` | 关闭的子 Agent | 看板移除卡片 |
| `skill:loaded` | 加载的技能名 | 技能徽标 |
| `todo:updated` | TODO 列表 | 看板/清单渲染 |

UI 最低可用集：`llm:stream` + `tool:*` + `approval:*` + `task:done/error`。
其余是增强项，可按需渲染。

### 2.4 交互回执（审批 / 提问 / 停止）

```http
POST /api/approve       { "approval_id": "...", "approved": true }   # 审批卡回执
POST /api/question      { "question_id": "...", "answer": "用方案A" } # ask_user 回执
POST /api/tasks/{task_id}/stop                                      # 中止任务
```

无论 approved 与否都会向全部活跃任务广播 `approval:resolved`，
UI 按 id 匹配关卡片即可（即使任务已死，卡片也能被关闭）。

## 3. 搭一个最小网页端（约 100 行）

```html
<!-- mini-ui.html -->
<script type="module">
const BASE = "http://localhost:8765";
const HEADERS = { "Content-Type": "application/json",
                  "Authorization": "Bearer <token>" };

async function openSession() {
  const r = await fetch(`${BASE}/api/sessions`, {
    method: "POST", headers: HEADERS,
    body: JSON.stringify({ name: "手机端会话" }),
  });
  return (await r.json()).session_id;
}

async function send(sessionId, prompt, onEvent) {
  const { task_id } = await fetch(`${BASE}/api/chat`, {
    method: "POST", headers: HEADERS,
    body: JSON.stringify({ session_id: sessionId, prompt }),
  }).then(r => r.json());

  const es = new EventSource(`${BASE}/api/tasks/${task_id}/events`);
  es.onmessage = (e) => {
    if (e.data === "[DONE]") { es.close(); return; }
    const { type, data } = JSON.parse(e.data);
    if (type === "llm:stream") onEvent(data.delta);
    if (type === "approval:request") renderApproval(data);   // 渲染审批卡
    if (type === "question:request") renderQuestion(data);   // 渲染提问卡
    if (type === "task:error") onEvent("\n[任务出错] " + (data.error ?? ""));
  };
}
</script>
```

要点：

1. **EventSource 不能带自定义 Header**。token 鉴权开启时，要么先
   `fetch` 校验 token（401 则要求重新输入），要么部署时用反向代理
   （如 Nginx）把 token 注入 `Authorization` 头，让 EventSource 免带；
2. SSE 事件是**无前缀命名**（`data:` 行），`[DONE]` 是普通 data 帧——
   `EventSource.onmessage` 全能收到，无需按事件名 addEventListener；
3. 手机端注意 SSE 经长连接，移动网络切换会断开——`es.onerror` 里
   重连（同一 URL 重 GET 即可，见 §2.2）。

## 4. 会话管理 API

| 方法与路径 | 说明 |
|---|---|
| `GET /api/sessions` | 会话列表（含自动推导标题：自定义 name > 首条用户消息） |
| `POST /api/sessions` | 新建会话 `{name?, workspace?}` → `{session_id}` |
| `GET /api/sessions/{id}` | 会话快照（messages + metadata），**UI 恢复历史用它** |
| `PATCH /api/sessions/{id}` | 更新名称/元数据 |
| `DELETE /api/sessions/{id}` | 删除会话（同时停掉其后台子 Agent） |
| `POST /api/compact` | 手动压缩 `{session_id, focus?}` |
| `GET /api/todos?session_id=` | 会话 TODO 看板 |
| `GET /api/context/stats?session_id=` | 上下文占用/计费单价/模型窗口 |

会话快照里的 `metadata` 还包含子 Agent 归档（`subagent_records`）、
会话目标（`goal`）、会话级模型覆盖（`model`）等，刷新/重开后
UI 可以完整恢复看板与上下文面板。

## 5. 其他常用 API 速查

| 分组 | 端点 | 说明 |
|---|---|---|
| 工作区 | `POST /api/workspace` `{path}` | 打开项目（任务运行中返回 409） |
| | `GET /api/workspace/tree-json?path=` | 文件树（懒加载 + git 状态字母） |
| | `GET /api/tools?agent_id=` | 当前工具集（随 Agent 裁剪） |
| | `POST /api/projects/create` | 新建项目（可选 git init） |
| LLM | `GET /api/llm/providers` / `GET,POST /api/llm/config` | 供应商元数据与配置 |
| | `POST /api/llm/test` | 测连接（返回 ok/消息/延迟） |
| | `GET /api/model-meta`、`POST /api/model-meta/refresh` | 模型元数据缓存 |
| 安全 | `GET /api/security/summary` 等 | 审批/技能权限/黑白名单管理 |
| 技能 | `GET /api/skills` 等 | 技能列表/加载/权限 |
| Agent | `GET /api/agents` 等 | 多 Agent 看板/协作模式选择器 |
| 文件 | `GET /api/fs/read`、`POST /api/fs/write` | 文件读写（受安全层约束） |
| 配置 | `GET,POST /api/config` | 全局配置（max_steps、pricing…） |

（完整签名以 `litework/server/routers/` 各文件为准，路由均以 `/api` 前缀。）

## 6. 不同终端形态的实践建议

### 6.1 手机端（响应式网页 / PWA）

- 官方 React UI 可直接在手机浏览器用（`--host 0.0.0.0` 后局域网访问）；
  自建轻量 UI 时重点做三件事：聊天流、审批卡、任务状态，其余可省；
- 移动网络不稳定：SSE 断线重连 + 会话快照恢复历史（§4）；
- 安卓/iOS 均无需客户端，PWA manifest + Service Worker 即可"添加到主屏幕"。

### 6.2 纯网页端（公网部署）

- Core 不要直接暴露公网。建议架构：
  `浏览器 ─ HTTPS 反代(Nginx/Caddy) ─ Core(127.0.0.1)`；
- 反代上强制 `Authorization` 注入 + TLS，并放行 SSE
  （`proxy_buffering off;`、`X-Accel-Buffering: no` 已由后端发送）；
- CORS：把你的网页域名加进 `LITEWORK_CORS_ORIGINS`。

### 6.3 嵌入式面板 / IDE 插件

- 只消费 §2.3 的最小事件集即可做出可用面板；
- 工具调用明细、上下文仪表、子 Agent 看板都是增量事件，按需订阅。

### 6.4 桌面替代壳（如 Tauri）

- 官方 Electron 的核心逻辑就是三步：拉起 Python Core → 从就绪标记读
  token → Webview 加载 Web UI。任何壳都能复刻（参考 `electron/` 与
  `docs/architecture.md` 的运行形态说明）。

## 7. 安全红线（自建 UI 必读）

1. **token 是唯一防线**：`--no-token` 只限本机调试；局域网/公网必须带 token；
2. 审批卡（`approval:request`）代表高危操作正在等待人工确认——
   你的 UI **不得**自动代答 `/api/approve`，除非你明确要做一个
   「自动批准」场景并清楚风险（后端也有 `auto_approve` 配置，职责别混在 UI）；
3. 文件读写 API 受安全层约束（工作区边界、三级风险），不要试图绕过；
4. 公网部署必须 HTTPS，token 不要放进 URL query。
