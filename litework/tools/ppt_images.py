# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""PPT 配图生成：调用生图大模型产出**位图**（照片/插画/3D），不是 SVG 图形。

为什么单独一层而不是直接用 ppt-master 的 image_gen.py：
- 那个脚本靠 `.env` / 环境变量读密钥（`GEMINI_API_KEY` 等），而 lite-work 的
  密钥在 config.json 的 llm.providers 里 —— 用户不该为了配图再填一遍；
  这里把已配置的供应商凭据**映射**到对应 backend 的环境变量上。
- 生图是花额度、耗十几秒的操作，需要「哪些后端可用」的前置探测与
  可读的失败原因，而不是让 Agent 对着脚本的英文报错猜。

凭据映射（供应商 → ppt-master backend）：

| lite-work 供应商 | backend     | 环境变量                          |
| ---------------- | ----------- | --------------------------------- |
| openai           | openai      | OPENAI_API_KEY                    |
| qwen             | qwen        | QWEN_API_KEY / DASHSCOPE_API_KEY  |
| glm              | zhipu       | ZHIPU_API_KEY                     |
| kimi             | （无对应）  | —（生图后端不含 Moonshot）        |

其余后端（gemini / bfl / ideogram / stability / volcengine / tencent /
fal / minimax / modelscope / openrouter / replicate / siliconflow）只能通过
环境变量或 `~/.ppt-master/.env` 配置——它们不是 lite-work 的聊天供应商。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

#: lite-work 供应商 id → (ppt-master backend, 需要注入的环境变量名)
PROVIDER_BACKEND_MAP: Dict[str, tuple[str, tuple[str, ...]]] = {
    "openai": ("openai", ("OPENAI_API_KEY",)),
    "qwen": ("qwen", ("QWEN_API_KEY", "DASHSCOPE_API_KEY")),
    "glm": ("zhipu", ("ZHIPU_API_KEY", "BIGMODEL_API_KEY")),
}

#: 后端 → 人类可读名（用于界面提示）
BACKEND_LABELS: Dict[str, str] = {
    "gemini": "Google Gemini",
    "openai": "OpenAI",
    "qwen": "阿里通义万相",
    "zhipu": "智谱 GLM-Image",
    "volcengine": "火山引擎 Seedream",
    "bfl": "Black Forest Labs FLUX",
    "ideogram": "Ideogram",
    "stability": "Stability AI",
    "tencent": "腾讯混元",
    "fal": "fal.ai",
    "minimax": "MiniMax",
    "modelscope": "ModelScope",
    "openrouter": "OpenRouter",
    "replicate": "Replicate",
    "siliconflow": "SiliconFlow",
}

#: 生图提示词的默认风格后缀：PPT 配图要「图」，不要「插画里带字」
_STYLE_SUFFIX = (
    ", clean professional presentation visual, no text, no letters, no watermark, "
    "high quality, well-lit, suitable as a slide illustration"
)

#: 零配置图库来源（不需要任何 API Key）。
#: Openverse 可筛到 CC0/公有领域（免署名），Wikimedia 多为 CC BY-SA（需署名）。
STOCK_PROVIDERS: Dict[str, str] = {
    "openverse": "Openverse（CC0/公版，免署名）",
    "wikimedia": "Wikimedia Commons（多为 CC BY-SA，需署名）",
    # 这两个要密钥，有就一起用
    "pexels": "Pexels",
    "pixabay": "Pixabay",
}


def _script(name: str) -> str:
    from ..tools.ppt_edit import _skill_dir

    return os.path.join(_skill_dir(), "scripts", name)


def _apply_proxy(env: Dict[str, str], proxy_config: Optional[Dict[str, Any]]) -> None:
    """把用户配置的代理注入子进程环境（media scope）。

    为什么必须做：生图/图库脚本用 `requests`，而 requests 只认
    `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 环境变量（它不像 httpx 那样能
    从调用参数传代理）。不注入 = 子进程**绕过用户配置的代理直连**，
    在需要代理的网络里表现为「图库连不上」「生图超时」——这正是最初的漏配。

    两个方向都要处理：
    - 配了代理且 media scope 勾选 → 写入四个大小写变体 + NO_PROXY；
    - **没配代理 → 主动清掉继承来的代理变量**。否则用户的 shell 里若有
      `HTTPS_PROXY`，应用里明明「关闭代理」了、配图却还在偷偷走代理
      （与 llm/webfetch 的 trust_env=False 行为保持一致）。
    """
    from ..core.proxy import proxy_config as read_proxy, proxy_env

    proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "NO_PROXY", "no_proxy")
    cfg = proxy_config or {}
    injected = proxy_env(cfg, "media")
    if injected:
        env.update(injected)
        return
    # 未启用 / 未勾选 media：清空继承值，保证「关掉就是直连」
    if not (read_proxy(cfg).get("enabled")):
        for key in proxy_vars:
            env.pop(key, None)


def _ensure_requests(env: Dict[str, str]) -> Dict[str, str]:
    """给技能解释器补上 `requests`（生图/图库脚本全依赖它）。

    为什么需要：skills 脚本跑在**随包技能解释器**（skill-python）里，那是裸
    CPython。打包态靠 sitecustomize 把 `_internal` 挂进 sys.path 借用主程序
    收集的第三方包；**开发态没有 `_internal`**，于是 `import requests` 直接
    ModuleNotFoundError（实测：本机 venv 有 requests，但 skill-python 没有）。

    这里把「当前进程能 import 到的 requests 所在目录」塞进 PYTHONPATH，
    开发态/打包态都能自愈，不必要求用户装两份依赖。
    """
    existing = env.get("PYTHONPATH", "")
    try:
        import requests as _rq  # noqa: F401 - 只为定位包路径

        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(_rq.__file__)))
    except Exception:
        return env
    parts = [p for p in existing.split(os.pathsep) if p]
    if pkg_parent not in parts:
        env["PYTHONPATH"] = os.pathsep.join([pkg_parent] + parts)
    return env


def _skill_env(
    llm_providers: Optional[Dict[str, Dict[str, Any]]] = None,
    proxy_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """构造子进程环境：供应商密钥映射 + 代理注入。

    两件事必须都做对，否则配图在真实环境里会失败：

    1. **密钥**：生图后端读 `OPENAI_API_KEY` 等环境变量，而 lite-work 的密钥
       在 config.json 的 llm.providers —— 不映射就得让用户填两遍；
    2. **代理**：这些脚本用的是 `requests`，靠 `HTTP_PROXY`/`HTTPS_PROXY`
       环境变量走代理。不注入就会**绕过用户配置的代理直连**，在需要代理的
       网络下表现为「图库连不上 / 生图超时」（实测漏了这个）。

    `proxy_config` 传 `app.config`，按 `media` scope 取代理设置。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    _ensure_requests(env)
    _apply_proxy(env, proxy_config)
    if not llm_providers:
        return env
    for pid, (backend, keys) in PROVIDER_BACKEND_MAP.items():
        conf = llm_providers.get(pid) or {}
        api_key = str(conf.get("api_key") or "").strip()
        if not api_key or "…" in api_key or api_key == "****":
            continue
        for key in keys:
            if not env.get(key):
                env[key] = api_key
        # 后端自身也要显式指定（脚本默认读 IMAGE_BACKEND）
        base_url = str(conf.get("base_url") or "").strip()
        if backend == "openai":
            if base_url and not env.get("OPENAI_BASE_URL"):
                env["OPENAI_BASE_URL"] = base_url
            model = str(conf.get("model") or "").strip()
            # 聊天模型名不是生图模型名（gpt-5.6 ≠ gpt-image-2），
            # 只在用户环境里显式给了 OPENAI_MODEL 时才透传
            if model and env.get("OPENAI_IMAGE_MODEL"):
                env["OPENAI_MODEL"] = env["OPENAI_IMAGE_MODEL"]
    return env


def _spawn_env(
    env: Optional[Dict[str, str]],
    proxy_config: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    """给子进程的最终环境。

    调用方给了 env 就用它（路由层已构造完整：密钥 + 代理）；
    否则现造一份（含 requests 补齐 + 代理注入）——保证**直接调用**
    生图/检索函数时也不会绕开代理。
    """
    if env is not None:
        return dict(env)
    return _skill_env(None, proxy_config)


def _run(args: List[str], env: Optional[Dict[str, str]],
         timeout: float = 180.0,
         proxy_config: Optional[Dict[str, Any]] = None) -> tuple[int, str]:
    from ..tools.ppt_edit import _skill_python
    from ..tools.proc_utils import kill_process_tree, posix_spawn_kwargs

    proc = subprocess.Popen(  # noqa: S603 - 固定脚本 + 受控参数
        [_skill_python(), _script("image_gen.py")] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_ensure_requests(_spawn_env(env, proxy_config)),
        **posix_spawn_kwargs(),
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        proc.wait(timeout=10)
        return 1, f"生图超时（{int(timeout)}s）"
    return proc.returncode, (out or b"").decode("utf-8", errors="replace")


def available_backends(
    llm_providers: Optional[Dict[str, Dict[str, Any]]] = None,
    image_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """哪些生图后端当前可用（有凭据）。

    返回值按「推荐顺序」排列：用户已配置的聊天供应商排前面（零额外配置）。
    """
    env = _skill_env(llm_providers)
    cfg = image_config or {}
    forced = str(cfg.get("backend") or "").strip().lower()
    out: List[Dict[str, Any]] = []
    keys_by_backend: Dict[str, tuple[str, ...]] = {
        backend: keys for backend, keys in PROVIDER_BACKEND_MAP.values()
    }
    # 其余后端的凭据变量（与 ppt-master 的 BACKEND_REGISTRY 对齐的常用子集）
    keys_by_backend.update({
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "volcengine": ("LAS_API_KEY", "VOLCENGINE_API_KEY", "ARK_API_KEY"),
        "bfl": ("BFL_API_KEY",),
        "ideogram": ("IDEOGRAM_API_KEY",),
        "stability": ("STABILITY_API_KEY",),
        "tencent": ("TENCENT_API_KEY", "TOKENHUB_API_KEY"),
        "fal": ("FAL_KEY", "FAL_API_KEY"),
        "minimax": ("MINIMAX_API_KEY",),
        "modelscope": ("MODELSCOPE_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY",),
        "replicate": ("REPLICATE_API_TOKEN", "REPLICATE_API_KEY"),
        "siliconflow": ("SILICONFLOW_API_KEY",),
    })
    for backend, keys in keys_by_backend.items():
        hit = next((k for k in keys if env.get(k)), "")
        if not hit:
            continue
        out.append({
            "backend": backend,
            "label": BACKEND_LABELS.get(backend, backend),
            "key_env": hit,
            # 来自聊天供应商映射 = 用户无需额外配置
            "from_llm_provider": any(
                b == backend for b, _ in PROVIDER_BACKEND_MAP.values()),
        })
    if forced:
        # 用户显式指定的排最前（若它也有凭据）
        out.sort(key=lambda x: 0 if x["backend"] == forced else 1)
    return out


def build_prompt(subject: str, style: str = "", mood: str = "") -> str:
    """把「这页要什么图」拼成生图提示词。

    刻意不带文字：生图模型渲染文字几乎必错，PPT 的标题由版式本身排。
    """
    parts = [(subject or "").strip()]
    if style.strip():
        parts.append(style.strip())
    if mood.strip():
        parts.append(mood.strip())
    return ", ".join(p for p in parts if p) + _STYLE_SUFFIX


def generate(
    *,
    prompt: str,
    out_dir: str,
    filename: str,
    aspect_ratio: str = "16:9",
    image_size: str = "1K",
    backend: str = "",
    model: str = "",
    env: Optional[Dict[str, str]] = None,
    timeout: float = 180.0,
    proxy_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """跑一次生图，返回 {ok, path, backend, error, log}。

    `path` 是生成的图片绝对路径（成功时）；失败时 `error` 给可读原因。
    """
    if not (prompt or "").strip():
        return {"ok": False, "error": "生图提示词为空", "path": "", "log": ""}
    os.makedirs(out_dir, exist_ok=True)
    args = [
        prompt, "-o", out_dir, "-f", filename,
        "--aspect_ratio", aspect_ratio, "--image_size", image_size,
    ]
    if backend:
        args += ["--backend", backend]
    if model:
        args += ["--model", model]
    code, log = _run(args, env, timeout=timeout, proxy_config=proxy_config)
    if code != 0:
        return {"ok": False, "error": _explain(log, backend), "path": "", "log": log[-1500:]}
    # 脚本按 --filename 命名，扩展名取决于后端返回的格式
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        cand = os.path.join(out_dir, filename + ext)
        if os.path.isfile(cand):
            return {"ok": True, "path": cand, "backend": backend,
                    "error": "", "log": log[-600:]}
    # 兜底：目录里最新的图片
    try:
        files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                 if re.search(r"\.(png|jpe?g|webp)$", f, re.I)]
        if files:
            newest = max(files, key=os.path.getmtime)
            return {"ok": True, "path": newest, "backend": backend,
                    "error": "", "log": log[-600:]}
    except OSError:
        pass
    return {"ok": False, "error": "生图脚本退出成功但没有产出图片", "path": "",
            "log": log[-1500:]}


def _explain(log: str, backend: str) -> str:
    """把脚本的英文报错翻成一句能照做的中文。"""
    low = log.lower()
    name = BACKEND_LABELS.get(backend, backend or "生图后端")
    if "no backend" in low or "not configured" in low or "api_key" in low or "unauthorized" in low:
        return f"{name} 没有配置密钥：请在设置里填好对应供应商的 API Key，或改用已配置的生图后端"
    if "rate limit" in low or "429" in low:
        return f"{name} 触发限流，稍后重试或换后端"
    if "quota" in low or "insufficient" in low:
        return f"{name} 额度不足"
    if "refus" in low or "safety" in low or "blocked" in low:
        return "提示词被安全策略拒绝，换一种描述（去掉人物/品牌/敏感场景）"
    tail = log.strip().splitlines()[-3:]
    return f"{name} 生图失败：" + " / ".join(t.strip() for t in tail if t.strip())[:300]


# ---------------------------------------------------------------- 图库检索（零密钥兜底）


def available_stock_providers() -> List[Dict[str, Any]]:
    """哪些图库来源可用。openverse / wikimedia **永远可用**（零配置）。

    这是「没有生图模型也能配图」的兜底：用户没配 OpenAI/通义/智谱时，
    仍然可以从开放图库找到可商用图片——只是不能按需「画」一张。
    """
    env = os.environ
    out: List[Dict[str, Any]] = []
    for pid, label in STOCK_PROVIDERS.items():
        needs_key = pid in ("pexels", "pixabay")
        key_env = f"{pid.upper()}_API_KEY"
        if needs_key and not env.get(key_env):
            continue
        out.append({
            "provider": pid,
            "label": label,
            # 免密钥 + CC0 = 不用在页面上署名
            "zero_config": not needs_key,
            "no_attribution": pid == "openverse",
        })
    return out


def capability(
    llm_providers: Optional[Dict[str, Dict[str, Any]]] = None,
    image_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """配图能力总览：生图 / 图库 / 都没有。

    前端据此决定给什么开关——**不能只判断生图**，否则没有生图模型的用户会
    看到一个禁用的开关就以为「配图不可用」，而其实图库路径一直可用。
    """
    gen = available_backends(llm_providers, image_config)
    stock = available_stock_providers()
    if gen:
        mode = "generate"          # 优先：按需生成，最贴合内容
    elif stock:
        mode = "stock"             # 兜底：检索开放图库
    else:                          # pragma: no cover - openverse 恒可用
        mode = "none"
    return {
        "mode": mode,
        "can_generate": bool(gen),
        "can_stock": bool(stock),
        "backends": gen,
        "providers": stock,
        "default_backend": str((image_config or {}).get("backend") or ""),
    }


def search_stock(
    *,
    query: str,
    out_dir: str,
    filename: str,
    provider: str = "",
    orientation: str = "landscape",
    min_width: int = 900,
    min_height: int = 500,
    no_attribution_only: bool = True,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 90.0,
    proxy_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从开放图库取一张图，返回 {ok, path, provider, attribution, error}。

    默认 `no_attribution_only=True`：PPT 页面上再加一行图片来源很破坏版式，
    故优先 CC0/公有领域。找不到时调用方可以放宽（见 `_tiered_search`）。
    """
    if not (query or "").strip():
        return {"ok": False, "path": "", "provider": "", "attribution": "",
                "error": "图片检索词为空"}
    os.makedirs(out_dir, exist_ok=True)
    args = [
        query,
        # 必须带扩展名：不带时脚本按「无扩展名」处理，产出 `.wk1tgdh0` 这类
        # 随机临时后缀，随后自己报 "Unsupported output image extension"。
        # 传 .jpg 让它按目标格式落盘（实测：不带扩展名 100% 失败）。
        "--filename", f"{filename}.jpg",
        "-o", out_dir,
        "--orientation", orientation,
        "--min-width", str(min_width), "--min-height", str(min_height),
    ]
    if provider:
        args += ["--provider", provider]
    if no_attribution_only:
        args += ["--strict-no-attribution"]

    from ..tools.ppt_edit import _skill_python
    from ..tools.proc_utils import kill_process_tree, posix_spawn_kwargs

    proc = subprocess.Popen(  # noqa: S603 - 固定脚本 + 受控参数
        [_skill_python(), _script("image_search.py")] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_ensure_requests(_spawn_env(env, proxy_config)),
        cwd=out_dir, **posix_spawn_kwargs(),
    )
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        proc.wait(timeout=10)
        return {"ok": False, "path": "", "provider": "", "attribution": "",
                "error": f"图库检索超时（{int(timeout)}s）"}
    log = (raw or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        return {"ok": False, "path": "", "provider": "", "attribution": "",
                "error": _explain_stock(log)}

    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cand = os.path.join(out_dir, filename + ext)
        if os.path.isfile(cand):
            return {
                "ok": True, "path": cand,
                "provider": _picked_provider(log) or provider,
                "attribution": _attribution_of(log),
                "error": "", "log": log[-600:],
            }
    return {"ok": False, "path": "", "provider": "", "attribution": "",
            "error": _explain_stock(log) or "图库检索没有返回可用图片",
            "log": log[-1200:]}


def _picked_provider(log: str) -> str:
    m = re.search(r"picked:.*?from\s+(\w+)", log)
    return m.group(1) if m else ""


def _attribution_of(log: str) -> str:
    """是否需要署名（CC BY / CC BY-SA）。"""
    m = re.search(r"picked:\s*'([^']*)'.*?\(([^)]*)\)", log)
    if not m:
        return ""
    license_text = m.group(2)
    if "no-attribution" in license_text or "CC0" in license_text or "Public" in license_text:
        return ""
    return f"{m.group(1)}（{license_text}）"


def _explain_stock(log: str) -> str:
    low = log.lower()
    if "no acceptable candidate" in low:
        return "图库里没有满足要求的图片：换个更具体的检索词，或放宽尺寸/署名限制"
    if "timeout" in low or "timed out" in low:
        return "图库检索超时（网络较慢），稍后重试"
    if "connection" in low or "network" in low:
        return "图库连不上（检查网络或代理设置）"
    tail = log.strip().splitlines()[-3:]
    return " / ".join(t.strip() for t in tail if t.strip())[:300]


def fetch_stock_tiered(
    *,
    query: str,
    out_dir: str,
    filename: str,
    env: Optional[Dict[str, str]] = None,
    proxy_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """逐级放宽地找图，尽量给一张能用的。

    顺序（代价从小到大）：
    1. 免署名（CC0）+ 高分辨率 —— 最理想，页面不用加出处；
    2. 允许署名 + 高分辨率 —— 需要调用方在页面加一行来源；
    3. 允许署名 + 降分辨率 —— 有些图库只暴露 960px 预览图。
    """
    tiers: List[Dict[str, Any]] = [
        {"no_attribution_only": True, "min_width": 1200, "min_height": 800},
        {"no_attribution_only": False, "min_width": 1200, "min_height": 800},
        {"no_attribution_only": False, "min_width": 900, "min_height": 500},
    ]
    last: Dict[str, Any] = {"ok": False, "path": "", "provider": "", "attribution": "",
                            "error": "没有尝试任何图库"}
    for tier in tiers:
        last = search_stock(
            query=query, out_dir=out_dir, filename=filename, env=env,
            proxy_config=proxy_config,
            no_attribution_only=bool(tier["no_attribution_only"]),
            min_width=int(tier["min_width"]), min_height=int(tier["min_height"]),
        )
        if last.get("ok"):
            return last
    return last
