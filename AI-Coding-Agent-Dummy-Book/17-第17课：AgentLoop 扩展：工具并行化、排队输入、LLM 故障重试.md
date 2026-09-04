# 第 17 课：AgentLoop 扩展——工具并行化、排队输入、LLM 故障重试

## 本课定位

第 16 课完成了 AgentLoop 主循环状态机：Think → Act → Observe 的完整闭环。但**串行执行工具**和**遇到 LLM 瞬时故障就失败**，在真实使用中会有两个明显的痛点：

1. **慢**：模型一次发起 5 个只读工具调用（读 3 个文件 + 搜 1 次 + 看目录树），串行执行要等最慢的一个×5；其实它们之间没有依赖，完全应该并行。
2. **脆**：长思考模型（或网络抖动）偶尔返回 429/5xx/超时，任务直接死掉，前面几十步白跑；而这类故障往往重试一次就好了。

本课给 AgentLoop 加三个扩展：**工具精细并行化**、**执行中排队输入**、**LLM 瞬时故障自动重试**。

## 一、工具精细并行化（Parallel Tool Calls）

### 1.1 为什么不能"无脑全并行"

并行能提速，但会破坏**顺序依赖语义**。最典型的例子：

```
模型发起: write_file("a.py", "v2")  →  read_file("a.py")
```

如果这两个并行执行，`read_file` 可能读到**旧版本**——顺序依赖被打破。更危险的是 `write A` + `write B` 同时写同一个文件，产生竞态。

所以并行化的核心不是"快"，而是**在保持语义的前提下提速**。

### 1.2 三种模式：never / always / auto

`litecode/core/agent_loop.py` 通过配置 `parallel_tool_calls` 控制：

```python
# app.py 默认配置
"parallel_tool_calls": "auto",
```

- **never**：永远串行（最保守，行为与第 16 课完全一致）；
- **always**：永远并行（快，但可能踩顺序依赖的坑）；
- **auto**（默认）：**写类工具串行 + 只读工具并行**的精细模式。

### 1.3 auto 模式的判定逻辑

```python
# agent_loop.py（核心）
def _should_parallelize(self, tool_calls) -> bool:
    """并行判定：never 串行；always 并行；auto 仅本轮全只读时并行。"""
    if self.parallel_tool_calls == "never" or len(tool_calls) <= 1:
        return False
    if self.parallel_tool_calls == "always":
        return True
    # auto：本轮所有工具都不是写类工具，才并行
    return all(self.registry.is_readonly(call.name) for call in tool_calls)
```

关键点：**auto 只看"这一轮"**。只要本轮出现一个写类工具，整轮退回串行——这是最稳妥的语义保证。`is_readonly` 由工具注册时的元数据决定（`read_file`/`search_code`/`git_status` 等是只读；`write_file`/`execute_command`/`git_commit` 等是写类）。

### 1.4 并行执行 + 结果按原序回填

并行最隐蔽的坑：`asyncio.gather` 返回结果顺序与**输入顺序一致**，但如果工具本身修改了共享状态（比如写类工具被误判为只读），结果顺序对不上就会错位。因此执行后必须**按工具调用顺序重建消息链**：

```python
# agent_loop.py（核心）
async def _execute_in_parallel(self, tool_calls, messages):
    # 并行执行，保持原始顺序
    results = await asyncio.gather(
        *(self._run_tool(call) for call in tool_calls)
    )
    # 按原序回填 tool 消息（gather 保序，顺序不会错位）
    for call, result in zip(tool_calls, results):
        messages.append(Message(
            role="tool", name=call.name,
            tool_call_id=call.id, content=result,
        ))
```

### 1.5 顺序依赖风险分析

| 场景 | 串行 | auto 并行 | 说明 |
|---|---|---|---|
| read A → read B | ✓ | ✓ | 无依赖，提速明显 |
| write A → read A | ✓ | ✗（退回串行） | 读到旧版本 |
| git status → git diff | ✓ | ✓ | 只读，无依赖 |
| write A → write A | ✓ | ✗（退回串行） | 竞态覆盖 |
| search → read | ✓ | ✓ | 结果按原序回填，LLM 感知正确 |

**工程原则**：并行化是**性能优化**，不是功能。默认 `auto` 只在"本轮全只读"时提速，一旦有写操作立刻退化为串行——宁慢勿错。

## 二、执行中补充指令（排队输入）

### 2.1 场景与前端队列

任务正在跑（比如"重构整个模块"），用户发现计划有遗漏，想补充一条指令："顺便把 README 也更新了"。如果只能等任务结束再发下一条消息，体验很差；如果强行中断，前面几十步白跑。

前端在对话框顶部维护一个**待发送队列**（PendingQueue）。任务运行期间用户输入的消息进入这个队列，以独立卡片形式展示，支持拖拽排序和删除。当前任务完成后，队列中的第一条消息自动作为新任务发送，依次类推直到队列清空。

- 队列消息**不进入**当前对话的消息流，避免与流式工具输出混淆
- 多条消息可排队，拖动 ⠿ 手柄调整顺序
- 任务完成后自动发送下一条，无需用户手动干预

### 2.2 后端队列设计（备用通道）

`litecode/server/tasks.py` 的 `TaskHandle` 维护一个异步队列：

```python
# tasks.py（核心）
class TaskHandle:
    def __init__(self, ...):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    def queue_input(self, text: str) -> int:
        """任务运行中补充指令，下一回合注入对话。返回队列长度。"""
        self.queue.put_nowait({"role": "user", "content": text})
        self._forward_event({
            "type": "chat:queued",
            "data": {"content": text, "queue_size": self.queue.qsize()},
        })
        return self.queue.qsize()
```

`/api/chat` 在会话已有任务运行时，不再报 409，而是调用 `queue_input`：

```python
# server/app.py（备用通道）
if running:
    handle.queue_input(prompt)
    return {"task_id": handle.task_id, "queued": True}   # 标记"已入队"
```

前端默认使用独立队列，仅在特定场景（如 Web 模式直接调用 API）下走后端通道。两种队列互不干扰——前端队列在任务完成后发起新任务，后端队列在当前回合边界注入。

### 2.3 AgentLoop 回合前注入

AgentLoop 在每回合开始前，先清空排队消息：

```python
# agent_loop.py（核心）
# A-. 注入任务运行期间用户补充的指令（排队输入在下一回合进入对话）
while not self.input_queue.empty():
    item = self.input_queue.get_nowait()
    messages.append(Message(role="user", content=item["content"]))
```

要点：**只在回合边界注入**——不会打断正在执行的工具，也不会在 LLM 响应中途插入消息导致状态错乱。

## 三、LLM 瞬时故障自动重试

### 3.1 可重试 vs 不可重试错误

不是所有错误都值得重试。`litecode/llm/base.py` 定义了错误分类：

```python
# llm/base.py（核心）
class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable   # True = 瞬时故障，值得重试

# HTTP 状态码判据：瞬时故障（可重试）
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
```

- **可重试（retryable=True）**：超时、网络中断、限流（429）、服务端 5xx——重试很可能成功；
- **不可重试（retryable=False）**：鉴权失败（401/403）、参数错误（400）、模型不存在——重试 100 次也是白费，还会烧钱。

适配器在抛出错误时标记：

```python
# llm/openai_compat.py（核心）
except httpx.TimeoutException as exc:
    raise LLMError(f"[LLM Error] 请求超时: {exc}", retryable=True) from exc
except httpx.HTTPStatusError as exc:
    status = exc.response.status_code
    raise LLMError(f"[LLM Error] HTTP {status}: {exc}",
                   retryable=status in RETRYABLE_STATUS) from exc
```

### 3.2 指数退避重试

`agent_loop.py` 在调用 LLM 的外层包裹重试逻辑：

```python
# agent_loop.py（核心）
for attempt in range(self.llm_retries + 1):
    try:
        content, tool_calls, usage = await self.adapter.chat_stream(
            processed, tools, self.kernel.events)
        break
    except LLMError as exc:
        retryable = bool(getattr(exc, "retryable", False))
        if not retryable or attempt >= self.llm_retries:
            raise   # 不可重试或已达上限 → 原样抛出
        delay = min(2 ** attempt, 30)   # 指数退避：1s, 2s, 4s...
        await self.kernel.events.emit("llm:retry", {
            "attempt": attempt + 1, "max": self.llm_retries,
            "delay": delay, "reason": str(exc),
        })
        await asyncio.sleep(delay)
```

三个设计点：

1. **指数退避**：第一次失败等 1s，第二次 2s，第三次 4s……封顶 30s。避免"一失败就猛重试"把服务端打得更惨（429 场景尤其重要）；
2. **上限可控**：`llm_retries` 默认 2 次（配置 `max_llm_retries`），连续失败说明问题不在运气，继续重试只是烧 Token；
3. **事件可见**：`llm:retry` 事件推给前端，UI 显示"⚠️ 模型请求超时，3s 后重试（第 1/2 次）"——用户知道任务没死，只是网络抖了一下。

### 3.3 为什么不能盲目重试

| 错误 | retryable | 重试价值 |
|---|---|---|
| 408/429/5xx | ✓ | 高：服务端瞬时过载，稍后即恢复 |
| 网络中断 / 超时 | ✓ | 高：网络抖动常见 |
| 401/403 鉴权失败 | ✗ | 零：Key 错了重试也没用 |
| 400 参数错误 | ✗ | 零：请求形状错了，重试还是错 |
| 上下文超窗 | ✗ | 零：需要裁剪而非重试 |

**工程原则**：重试是给"瞬时故障"的保险丝，不是给"配置错误"的万能药。宁可快速失败暴露问题，也不要无限重试烧 Token。

## 四、测试设计

本课的三个特性都有配套测试（`tests/test_agent_loop.py`）：

```python
# 并行化四用例
def test_parallel_never_runs_serially(...)          # never → 严格串行
def test_parallel_always_runs_concurrently(...)     # always → 全并行
def test_parallel_auto_readonly_parallelizes(...)   # auto + 全只读 → 并行
def test_parallel_auto_write_serializes(...)        # auto + 含写 → 串行

# 排队注入
def test_queue_input_injected_before_next_turn(...)  # 补充指令进入下一回合

# 重试次数
def test_llm_retry_on_transient_error(...)           # 瞬时故障自动重试
def test_llm_no_retry_on_fatal_error(...)            # 不可重试错误立即失败
```

## 本课小结

1. **工具精细并行化**：`never/always/auto` 三模式；auto 默认"写类串行 + 只读并行"，结果按原序回填——性能优化以不破坏语义为前提；
2. **排队输入**：`TaskHandle.queue` 在回合边界注入补充指令，前端"已入队"标记消除歧义；
3. **LLM 故障重试**：`LLMError.retryable` 区分瞬时/致命错误；指数退避 + 重试上限 + `llm:retry` 事件可见。

下一篇进入 **第 18 课：安全沙箱与高危拦截实战**——把第 7 课的安全模型落地为动态黑白名单、三级风险控制与 Web 审批卡。
