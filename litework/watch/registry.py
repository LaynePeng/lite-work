# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""盯梢数据源扩展点：`WatchSource` 插件基类 + 内置源 + 注册表。

**为什么做成插件扩展点**：盯邮件、盯 Slack、盯 Jira、盯部署状态……这类需求
不可能穷举进主程序（而且要新增依赖、凭据、企业私有 API）。因此主程序只提供
「调度 + 指纹比对 + 变化时唤 Agent + 通知」这条**通用管线**，具体「怎么取快照」
交给插件。插件装进 `~/.lite-work/plugins/` 即被发现，走与工具插件完全相同的
安装/治理机制（见 `docs/plugin-guide.md` §1）。

插件形态（目录插件）：

    ~/.lite-work/plugins/watch-email/
    └── plugin.py     # class EmailWatch(WatchSource): ...

    from litework.watch.registry import WatchSource, ProbeResult

    class EmailWatch(WatchSource):
        name = "watch-email"
        source_kind = "email"          # 计划里写 "source": "email"
        display_name = "邮箱"

        async def probe(self, cfg, ctx) -> ProbeResult:
            msgs = ctx.call_tool("email_list", {"unread": True, "max": 20})
            fp = "\\n".join(f"{m['uid']}|{m['subject']}" for m in msgs)
            return ProbeResult(snapshot=fp, summary=f"{len(msgs)} 封未读")

**关键约定：探针只负责「取快照」，不做判断。**
`snapshot` 是用于比对的原始文本（会被哈希）；`summary` 给人和 Agent 看。
"有没有变化"由调度器算哈希判定——这样"每 5 分钟盯一次"不会每个 tick 都烧
token，只有指纹变了才唤起 LLM。

**安全线**：探针输出（尤其邮件正文、网页内容）属于**不可信输入**，会作为
prompt 的一部分进入 Agent。调度器据此钉死权限档（默认 read，最多 read+web），
不开放 edit/execute。
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.watch.registry")


# ---------------------------------------------------------------- 数据结构


@dataclass
class ProbeResult:
    """一次探测的结果。

    - `snapshot`：**用于指纹比对**的文本。只要它变了就认为「有变化」。
      建议放稳定、可比对的字段（id/时间戳/标题/状态），不要放不稳定的
      随机内容（如包含请求 id 的完整响应），否则会每次都判为变化。
    - `summary`：给人/Agent 看的简短描述（如「3 封未读」「构建 #412 失败」）。
    - `changed_hint`：可选，探针自己已经知道变化了什么时给出（如新增条目列表），
      会直接拼进给 Agent 的 prompt，比让 Agent 自己 diff 更省 token。
    - `ok=False`：探测失败（网络/凭据问题）。调度器记为 error 并按普通结果
      记账，**不会**因为失败就触发 Agent。
    """

    snapshot: str = ""
    summary: str = ""
    changed_hint: str = ""
    ok: bool = True
    error: str = ""

    def fingerprint(self) -> str:
        return hashlib.sha256(self.snapshot.encode("utf-8", errors="replace")).hexdigest()[:32]


@dataclass
class ProbeContext:
    """传给 `probe()` 的上下文（只读能力，不给插件的写权限）。"""

    schedule_id: str = ""
    workspace: str = ""
    #: 调用已有工具（如 email_list / webfetch / read_file）。由调度器注入，
    #: 签名 async (tool_name, args) -> Any。插件若拿不到（None）应自行降级。
    call_tool: Optional[Any] = None
    #: 只读配置（app.config），插件可读不可写。
    config: Dict[str, Any] = field(default_factory=dict)

    async def tool(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        """调用内核工具的便捷包装；不可用时抛 RuntimeError（插件可捕获降级）。"""
        if self.call_tool is None:
            raise RuntimeError("当前上下文不可调用工具")
        return await self.call_tool(name, args or {})


# ---------------------------------------------------------------- 插件基类


class WatchSource:
    """盯梢数据源插件基类。

    子类需覆盖：`name`、`source_kind`、`display_name`、`probe()`。
    其余（`describe`、`config_schema`、`probe_interval_hint`）可选，用于 UI 与建议。
    """

    #: 插件标识（与目录名一致，只允许 [A-Za-z0-9._-]）
    name: str = "watch-source"
    #: 数据源类型标识——计划里的 `source` 字段就填这个值
    source_kind: str = ""
    #: 展示名（设置页/计划编辑器用）
    display_name: str = ""
    description: str = ""
    version: str = "1.0.0"

    #: 该源建议的最小探测间隔（秒）。调度器用它给用户提示，不强制。
    probe_interval_hint: int = 300

    #: 配置字段说明（纯展示用，主程序不校验）：{"folder": "邮箱文件夹"}
    config_schema: Dict[str, str] = {}

    async def probe(self, cfg: Dict[str, Any], ctx: ProbeContext) -> ProbeResult:
        """取一次快照。必须实现。异常会被调度器捕获记为 error。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 probe()")

    def describe(self) -> Dict[str, Any]:
        """供 API/UI 展示的元信息。"""
        return {
            "name": self.name,
            "source_kind": self.source_kind,
            "display_name": self.display_name or self.source_kind,
            "description": self.description,
            "version": self.version,
            "probe_interval_hint": self.probe_interval_hint,
            "config_schema": dict(self.config_schema),
        }


# ---------------------------------------------------------------- 内置源

_COMMAND_DENY = (
    "rm ", "mv ", "dd ", "mkfs", "shutdown", "reboot", ">", ">>", "|", "curl ", "wget ",
    "sudo", "chmod", "chown", ":(){", "kill ", "pkill",
)


class CommandWatch(WatchSource):
    """内置：跑一条**只读**命令取快照（如 `git log --oneline -1`）。

    无人值守 + 命令执行是高危组合，因此这里做**白名单式保守拦截**：
    含管道/重定向/删除/网络下载等字符或命令直接判为配置错误，不执行。
    需要更复杂的行为请写插件（插件里可以调用内核的 execute_command 工具，
    走正常审批与权限域）。
    """

    name = "watch-command"
    source_kind = "command"
    display_name = "命令输出"
    description = "运行一条只读命令，把输出作为快照（如 git status / 目录列表）"
    probe_interval_hint = 300
    config_schema = {"command": "只读命令，如 git log --oneline -1"}

    async def probe(self, cfg: Dict[str, Any], ctx: ProbeContext) -> ProbeResult:
        cmd = str(cfg.get("command") or "").strip()
        if not cmd:
            return ProbeResult(ok=False, error="未配置 command")
        lowered = cmd.lower()
        if any(tok in lowered for tok in _COMMAND_DENY):
            return ProbeResult(
                ok=False,
                error="只读源不接受管道/重定向/删除/下载类命令；需要写操作请用插件",
            )
        if ctx.call_tool is None:
            return ProbeResult(ok=False, error="当前上下文不可执行命令")
        try:
            out = await ctx.tool("execute_command", {"command": cmd, "timeout": 30})
        except Exception as exc:  # noqa: BLE001 - 探针失败不该让整轮调度炸掉
            return ProbeResult(ok=False, error=f"命令执行失败: {exc}")
        text = str(out)[:20000]
        return ProbeResult(snapshot=text, summary=f"{cmd} → {text.splitlines()[0][:80] if text.strip() else '（无输出）'}")


def _tail_text(path: str, max_bytes: int = 2000) -> str:
    """读文件尾部若干字节的文本，用于给 Agent 提供「变了什么」的线索。

    刻意保守：遇到二进制内容（含 NUL 字节）或解码失败就返回空串——
    prompt 里塞乱码比没有线索更糟（既费 token 又可能误导模型）。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read(max_bytes)
    except OSError:
        return ""
    if b"\x00" in raw:
        return ""          # 二进制（如图片/产物），不给线索
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-20:])


class FileWatch(WatchSource):
    """内置：盯文件/目录（存在性、大小、mtime；目录则列出条目）。

    纯本地读取，不经过工具层，因此无需审批——这正是「初始范围」里最省事的
    盯梢形态（如盯某个构建产物、日志文件、lock 文件变化）。
    """

    name = "watch-file"
    source_kind = "file"
    display_name = "文件 / 目录"
    description = "盯文件的存在与大小/mtime，或目录的条目列表"
    probe_interval_hint = 60
    config_schema = {"path": "工作区相对路径或绝对路径；目录则列条目"}

    async def probe(self, cfg: Dict[str, Any], ctx: ProbeContext) -> ProbeResult:
        raw = str(cfg.get("path") or "").strip()
        if not raw:
            return ProbeResult(ok=False, error="未配置 path")
        path = raw if os.path.isabs(raw) else os.path.join(ctx.workspace or ".", raw)
        try:
            if not os.path.exists(path):
                return ProbeResult(snapshot="MISSING", summary=f"{raw} 不存在")
            if os.path.isdir(path):
                names = sorted(os.listdir(path))
                return ProbeResult(
                    snapshot="\n".join(names),
                    summary=f"{raw}/ 下 {len(names)} 项",
                    changed_hint="目录条目：" + ", ".join(names[:20]),
                )
            st = os.stat(path)
            fp = f"{st.st_size}|{int(st.st_mtime)}"
            # 变化线索：给 Agent「变了什么」，而不只是「变了」。
            # 文本类文件取尾部若干行（日志/产物的变化通常在末尾），
            # 二进制或读取失败则不提供——prompt 里的线索宁可缺失也不塞乱码。
            hint = _tail_text(path, max_bytes=2000)
            res = ProbeResult(
                snapshot=fp,
                summary=f"{raw}：{st.st_size} 字节，mtime {int(st.st_mtime)}",
            )
            if hint:
                res.changed_hint = f"{raw} 当前尾部内容：\n{hint}"
            return res
        except OSError as exc:
            return ProbeResult(ok=False, error=f"读取失败: {exc}")


class UrlWatch(WatchSource):
    """内置：抓网页取快照（复用 webfetch 工具的缓存与安全策略）。

    `snapshot` 取内容哈希而非全文——网页常含动态噪声（时间戳、CSRF token），
    全文比对会次次判为变化。需要精细比对时用 `selector_text` 关键词过滤。
    """

    name = "watch-url"
    source_kind = "url"
    display_name = "网页内容"
    description = "定时抓取网页，内容变化时提醒（复用 webfetch 的缓存与安全策略）"
    probe_interval_hint = 900
    config_schema = {"url": "要盯的网址", "contains": "可选：只关注包含该关键词的片段"}

    async def probe(self, cfg: Dict[str, Any], ctx: ProbeContext) -> ProbeResult:
        url = str(cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return ProbeResult(ok=False, error="url 必须是 http(s) 地址")
        if ctx.call_tool is None:
            return ProbeResult(ok=False, error="当前上下文不可抓取网页")
        try:
            out = await ctx.tool("webfetch", {"url": url, "maxChars": 40000})
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(ok=False, error=f"抓取失败: {exc}")
        text = str(out)
        keyword = str(cfg.get("contains") or "").strip()
        if keyword:
            lines = [ln for ln in text.splitlines() if keyword in ln]
            text = "\n".join(lines) if lines else "（未匹配到关键词）"
        return ProbeResult(
            snapshot=text,
            summary=f"{url}：{'匹配 ' + keyword if keyword else f'{len(text)} 字符'}",
            changed_hint=text[:800],
        )


BUILTIN_SOURCES: List[WatchSource] = [CommandWatch(), FileWatch(), UrlWatch()]


# ---------------------------------------------------------------- 注册表


class WatchSourceRegistry:
    """数据源注册表：内置源 + 插件源。

    插件发现复用 `plugin_loader`（扫描 ~/.lite-work/plugins/，找出 Plugin 子类）。
    `WatchSource` 不是 `Plugin` 子类，所以在加载器里会被跳过（它只认 Plugin）；
    因此这里**额外独立扫描**一遍同一批文件，挑出 WatchSource 子类。
    两者互不干扰：一个插件目录里同时放工具插件与盯梢源也可以。
    """

    def __init__(self) -> None:
        self._sources: Dict[str, WatchSource] = {}
        for src in BUILTIN_SOURCES:
            self._sources[src.source_kind] = src

    def register(self, source: WatchSource) -> None:
        kind = (source.source_kind or "").strip()
        if not kind:
            logger.warning("忽略未声明 source_kind 的盯梢源: %s", type(source).__name__)
            return
        self._sources[kind] = source

    def get(self, kind: str) -> Optional[WatchSource]:
        return self._sources.get((kind or "").strip())

    def kinds(self) -> List[str]:
        return sorted(self._sources.keys())

    def describe_all(self) -> List[Dict[str, Any]]:
        return [src.describe() for src in self._sources.values()]

    def load_from_dir(self, plugin_dir: str) -> int:
        """扫描插件目录，注册其中的 WatchSource 子类。返回新注册数量。

        错误隔离与 plugin_loader 一致：单个文件失败只记日志，不影响其他插件。
        """
        if not plugin_dir or not os.path.isdir(plugin_dir):
            return 0
        import importlib.util
        import sys

        added = 0
        for entry in sorted(os.listdir(plugin_dir)):
            full = os.path.join(plugin_dir, entry)
            module_file: Optional[str] = None
            if entry.endswith(".py") and os.path.isfile(full):
                module_file = full
            elif os.path.isdir(full):
                for cand in ("plugin.py", "__init__.py"):
                    if os.path.isfile(os.path.join(full, cand)):
                        module_file = os.path.join(full, cand)
                        break
            if not module_file:
                continue
            mod_name = f"litework_watchsrc_{os.path.basename(module_file)}_{abs(hash(full)) & 0xFFFF:x}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, module_file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                # 多文件插件（如 astock-plugin：plugin.py + astock_*.py 兄弟模块）
                # 的裸 import 依赖插件目录在 sys.path——工具插件加载器
                # （tools/plugin_loader._load_module）就是这么做的；盯梢源加载
                # 此前漏了这步，多文件插件的 WatchSource 一直注册失败
                # （ModuleNotFoundError，工具能加载但盯梢源静默丢失）。
                _src_dir = os.path.dirname(os.path.abspath(module_file))
                if _src_dir not in sys.path:
                    sys.path.insert(0, _src_dir)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
            except Exception:  # noqa: BLE001 - 单个插件失败不影响其他
                logger.warning("加载盯梢源插件失败: %s", module_file, exc_info=True)
                continue
            for obj in vars(mod).values():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, WatchSource)
                    and obj is not WatchSource
                ):
                    try:
                        inst = obj()
                    except Exception:  # noqa: BLE001
                        logger.warning("实例化盯梢源失败: %s", obj.__name__, exc_info=True)
                        continue
                    self.register(inst)
                    added += 1
        return added
