# lite-work Core API 参考

> `litework/core/` 模块的公开接口速查。签名与代码一致，行号随版本可能漂移。

## types.py — 基础数据类型

```python
@dataclass
class Message:
    role: str                     # system / user / assistant / tool
    content: Optional[str]
    tool_calls: Optional[List[ToolCall]] = None
    name: Optional[str] = None    # tool 消息对应的工具名
    tool_call_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]
    @classmethod
    def from_dict(cls, data) -> "Message"

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str                # JSON 字符串，解析失败走 json_repair

@dataclass
class ToolDefinition:
    name: str; description: str; parameters: Dict[str, Any]

@dataclass
class Context:
    """流经中间件管道的上下文载体（session 引用 + 透传数据）。"""

class Plugin(ABC):
    def install(self, kernel) -> None   # 唯一入口，装到 Kernel 上即生效
```

## kernel.py — 会话容器

```python
class Kernel:
    def __init__(self, session_id: str)
    def use(self, plugin: Plugin) -> "Kernel"      # 链式安装插件
    def register_service(self, name: str, service: Any)   # 依赖注入
    def get_service(self, name: str) -> Any        # 未注册返回 None
    def has_service(self, name: str) -> bool
    def push_message(self, message: Message)       # 追加进会话流水账
    @property
    def session_id(self) -> str
```

内核本身无业务逻辑：事件总线、服务、消息、中间件都由插件在
`install()` 时挂进来。上层（AgentLoop、工具、编排）全部面向这组
最小接口编程。

## events.py — 强类型事件总线

```python
class TypedEventBus:
    def __init__(self, strict: Optional[bool] = None)  # 环境变量 LITEWORK_STRICT_EVENTS
    def on(self, event: str, listener: Listener) -> "TypedEventBus"
    def off(self, event: str, listener: Listener) -> None
    async def emit(self, event: str, data: Any = None)   # 顺序 await 各监听器
```

- `strict=True` 时 emit 前按 TypedDict 校验负载，不合规抛
  `EventPayloadError`（TypeError 子类）——CI/测试必开；
- 事件清单（各负载见 `events.py` 内 TypedDict 定义）：

| 事件 | 负载 | 时机 |
|---|---|---|
| `message:added` | MessageAddedPayload | 新消息入上下文 |
| `llm:stream` | LLMStreamPayload | 流式增量 |
| `llm:turn_start` / `llm:retry` | LLMTurnStartPayload / LLMRetryPayload | LLM 调用阶段 |
| `tool:before_execute` / `tool:after_execute` | ToolBefore/AfterExecutePayload | 工具执行切面 |
| `approval:request` / `approval:resolved` | ApprovalRequest/ResolvedPayload | HITL 审批 |
| `task:start` / `task:done` / `task:error` / `task:stop` | Task*Payload | 任务生命周期 |
| `stats:update` / `context:stats` | StatsUpdatePayload / ContextStatsPayload | 用量与上下文仪表 |
| `subagent:started` / `subagent:progress` / `subagent:completed` | Subagent*Payload | 子 Agent 进度 |
| `agent:spawned` / `agent:closed` | AgentSpawned/ClosedPayload | 多 Agent 管理 |
| `skill:loaded` / `todo:updated` | SkillLoadedPayload / TodoUpdatedPayload | 技能与看板 |
| `question:request` / `question:resolved` | QuestionRequest/ResolvedPayload | ask_user 提问 |
| `chat:queued` | ChatQueuedPayload | 排队消息注入 |

## pipeline.py — 洋葱中间件

```python
class Pipeline:
    def __init__(self, name: str = "pipeline")
    def use(self, middleware: Callback) -> None     # 注册切面
    async def run(self, ctx: Context, initial_data: Any) -> Any
```

中间件签名 `async def mw(ctx, data, next)`；调 `next(data)` 进入内层，
返回值逆序回传。典型挂载点：`tool:before_execute`（安全校验、目录隔离）、
`tool:after_execute`（结果加工、观察打包）。

## agent_loop.py — 任务执行器

```python
class AgentLoop:
    def __init__(self, kernel, llm_client, tool_registry, ...)

    async def run_task(self, user_input: str, *, session_id=..., **kw) -> AsyncIterator[dict]
        # 一次完整任务：组上下文 → LLM → 工具批次 → 循环 → 收尾。
        # 以异步生成器产出事件帧（UI/SSE 直接消费）。

    def request_stop(self) -> None                  # 线程安全的中止请求
```

内部关键阶段（按执行顺序）：

| 方法 | 职责 |
|---|---|
| `_initialize_task` | 建 System Prompt、注入技能索引与项目指令 |
| `_trim_context` | 调 ContextManager 裁剪 + 触发两阶段压缩 |
| `_call_llm_with_retry` | 带退避的 LLM 调用，流式增量发事件 |
| `_execute_tool_batch` | 本轮工具调用批次；`_should_parallelize` 判定可并行的调用并发执行 |
| `_apply_action_fusion` | 动作融合：合并模型啰嗦的多步操作，减少无效轮次 |
| `_reduce_to_receipt` | 证据收据：长工具结果压成结论性摘要再进上下文 |
| `_try_compact` / `_summarize_history` | 触发/执行 LLM 摘要压缩（决策见 compaction_economics） |
| `_inject_queued` / `_inject_agent_notifications` | 排队消息、子 Agent 完成通知在 turn 边界注入 |
| `_emit_context_stats` | 窗口占用 / 命中率 / 压缩统计推给上下文面板 |

## context_manager.py — 上下文裁剪与两阶段压缩

```python
class ContextManager:
    def __init__(self, max_allowed_tokens: int = 48000, keep_recent_full_turns: int = 2)

    def prune_messages(self, messages: List[Message], *, token_budget: int, ...) -> List[Message]
        # 免费裁剪：保留最近 N 个完整 turn，更早的工具大结果换占位符

    def split_for_compaction(self, messages, *, ...) -> tuple[head, tail]
        # 切出待摘要的 head 与必须保留的 tail（按 turn 边界，不切碎配对）
```

辅助：`repair_tool_call_pairs(messages)` 修复被裁剪/截断破坏的
tool_call ↔ tool_result 配对（防 API 报错），阶段候选由
`_stage1_candidates` / `_oldest_turn_ranges` 等私有方法给出。

## observation_pack.py — 观察打包（句柄化）

```python
@dataclass
class Observation:      # id(内容哈希) / tool_name / content / 估算 tokens
@dataclass
class RecallChunk:      # content / offset / next_offset（支持续读）

def observations_dir(truncation_dir) -> Optional[str]
def archive_observation(root: str, obs: Observation) -> str     # 落盘，返回句柄
def archive_text(root, content, tool_name="tool") -> Optional[Observation]
def read_recall_chunk(root, obs_id, offset=0) -> RecallChunk    # obs_recall 工具的后端
def placeholder_for(obs: Observation) -> str                    # 上下文里的占位符文本
def project_observations(root, messages, ...) -> List[Message]  # 把历史中的句柄投影回可读内容
```

大工具结果归档后上下文只留 `placeholder_for` 生成的句柄；模型需要细节时
调用 `obs_recall` 工具按 `id + offset` 分页回读。

## compaction_economics.py — 压缩经济学

```python
@dataclass
class CompactionDecision:
    compact: bool; reason: str
    saving_tokens: int             # 每个后续请求省下的 tokens
    write_cost_tokens: int         # 摘要调用的一次性写入成本
    cache_debt_tokens: int         # 旧前缀作废产生的缓存债
    breakeven_requests: Optional[int]           # 回本请求数
    expected_remaining_requests: Optional[int]  # 预期剩余请求数
    cache_write_read_ratio: Optional[float]     # 供应商缓存写/读价比

def cache_write_read_ratio(pricing: Optional[dict]) -> Optional[float]

def decide_compaction(*, head_tokens, summary_tokens, context_tokens,
                      context_window, current_turn, max_turns,
                      pricing=None, window_reserve_ratio=0.9,
                      subsequent_margin=1.5) -> CompactionDecision
```

决策规则：**window_protection**（上下文 ≥ 90% 窗口，保命，压）或
**economic**（`breakeven × 1.5 ≤ 预期剩余请求数`，划算，压）；否则推迟
（reason=`deferred_economic` 等）。缺定价数据时视为无缓存债（ratio=1）。

## truncator.py / token_counter.py / json_repair.py / state_tracker.py

```python
# truncator.py
def truncate_tool_output(text: str, *, max_bytes: int, output_dir: str, tool_name: str) -> TruncationResult
    # 超限截断 + 全文落盘（句柄），过期文件自动清理

# token_counter.py
class TokenCounter:
    count_text_tokens(text: str) -> int
    @classmethod count_message_tokens(cls, message: Message) -> int
    @classmethod count_messages_tokens(cls, messages) -> int

# json_repair.py
def safe_json_parse(json_string: str) -> Tuple[bool, Any, str]
    # 容错解析模型输出的脏 JSON，返回 (ok, value, error)

# state_tracker.py
class AgentStateTracker:
    def __init__(self, loop_threshold: int = 3)
    def register_and_check_loop(self, tool_name: str, args_str: str) -> bool
    # 同一工具+同参连续超过阈值 → True（死循环，AgentLoop 据此干预）

# commands.py
def build_command_list(skills=None) -> List[Dict[str, str]]   # 斜杠命令表
def parse_skill_command(prompt: str) -> Optional[Dict[str, str]]
```

## session_store.py — 会话持久化

```python
@dataclass
class SessionSnapshot:
    session_id: str; messages: List[Message]; metadata: Dict[str, Any]
    to_dict() / from_dict()

class SessionStore:
    def __init__(self, storage_dir: str = "./.lite-work/sessions")
    def save(self, session_id, messages, metadata=None) -> None   # JSON 原子写（tmp + rename）
    def load(self, session_id) -> Optional[SessionSnapshot]
    def list(self) -> List[Dict[str, Any]]                        # 会话列表（含元信息）
    def delete(self, session_id) -> bool
    def update_metadata(self, session_id, updates) -> Optional[SessionSnapshot]
    def get_or_create_conversation_id(self, session_id, provider_id) -> str
```

## 扩展一个内核插件的最小示例

```python
from litework.core.types import Plugin

class MyGuardPlugin(Plugin):
    def install(self, kernel) -> None:
        bus = kernel.get_service("events")
        pipeline = kernel.get_service("tool_pipeline")

        @pipeline.use
        async def block_rm(ctx, data, next):
            if getattr(data, "name", "") == "execute_command" and "rm -rf" in data.arguments:
                raise PermissionError("blocked by MyGuardPlugin")
            return await next(data)

        bus.on("tool:after_execute", lambda p: print("tool done:", p["tool"]))
```

插件即一切：内置的安全审批、观察打包、目录隔离都是这么挂上去的，
用户插件与内核机制走完全相同的通道。
