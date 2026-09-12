# lite-work 执行方式指南

lite-work 支持多种运行形态：**桌面应用（Electron）**、**纯浏览器**、**远程 Core** 与**仅后端 API**。
本文按使用场景列出所有执行方式；快速上手见根目录 README，Web API 细节见 `docs/web-api.md`。

## 1. 桌面应用（完整体验，推荐）

### 1.1 安装包（无需任何环境）

从 [Releases](https://github.com/LaynePeng/lite-work/releases) 下载对应安装包：

| 平台 | 文件 |
| --- | --- |
| macOS (Apple Silicon) | `lite-work-<版本>-arm64.dmg` |
| Windows | `lite-work Setup <版本>.exe`（NSIS 安装包） |

- macOS 包未签名：首次打开如提示「无法验证开发者」，右键应用 →「打开」；如提示「已损坏」，运行
  `xattr -dr com.apple.quarantine "/Applications/lite-work.app"`
- 安装包自带 PyInstaller 打包的 Python 后端二进制，无需 Python / Node。

### 1.2 源码运行桌面版（开发/二次开发）

```bash
# 前提：Python 3.11+、Node 18+
python3 -m venv .venv
# Windows: .venv\Scripts\pip install -e .[dev]；macOS/Linux: .venv/bin/pip install -e .[dev]
.venv/bin/pip install -e .[dev]
npm install

npm run dev    # 开发模式：Python Core + Vite 热更新 + Electron 窗口
npm start      # 生产模式：构建前端 → 自动拉起 Core → Electron 窗口
```

> 办公依赖（python-docx / openpyxl / python-pptx / reportlab / pandas / matplotlib）已包含在主依赖中，无需单独安装。
> 国内网络受限：pip 可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`；Electron 二进制下载失败时执行 `node node_modules/electron/install.js`。

## 2. 纯浏览器（轻量 Web UI）

```bash
# 一次性步骤：构建前端静态资源（web/dist），serve 会自动托管
npm run build:web

# 启动后端（默认 127.0.0.1:8787）
# Windows: .venv\Scripts\python -m litework serve
.venv/bin/python -m litework serve --no-token
```

浏览器访问 `http://127.0.0.1:8787`。

### serve 常用参数（完整见 `litework/cli.py`）

| 参数 | 说明 |
| --- | --- |
| `--host` | 监听地址，默认 `127.0.0.1`；局域网/手机访问用 `0.0.0.0` |
| `--port` | 端口，默认 `8787` |
| `--token xxx` | 指定访问令牌（默认自动生成随机令牌并打印） |
| `--no-token` | 关闭鉴权（仅限本机调试，切勿暴露公网） |
| `--workspace /path` | 指定工作区目录 |
| `--config-dir /path` | 配置/会话目录（默认 `~/.lite-work`） |
| `--api-key` / `--base-url` / `--model` | LLM 供应商覆盖（默认读 `DEEPSEEK_API_KEY`） |
| `--log-level` | `debug/info/warning/error`，默认 `info` |

> **鉴权说明**：未指定 token 时自动生成，并以就绪标记
> `LITEWORK_CORE_READY port=… token=…` 打印在终端（供 Electron 自动注入）。
> 纯浏览器手动使用时建议 `--no-token`（仅本机）；开启鉴权后 `/api/*` 需带
> `Authorization: Bearer <token>`。CORS 默认放行 `localhost:5173`，可用环境变量
> `LITEWORK_CORS_ORIGINS` 追加来源。

> **开发联调**：Web 前端以 Vite 开发态联调后端时，用
> `LITEWORK_CORE_TOKEN=<token> npm run dev` 让 Vite 代理自动注入请求头。

## 3. 远程 Core（窗口直连远端后端）

在 `~/.lite-work/client.json` 配置后，桌面窗口直接连接远程后端，不本地拉起 Core：

```json
{
  "coreUrl": "http://192.168.1.10:8787",
  "token": "远程服务启动时打印的 token"
}
```

远端执行 `lite-work serve --host 0.0.0.0` 即可被访问（务必配置 token，勿用 `--no-token`）。

## 4. 仅后端 API（无 UI，第三方集成）

```bash
# 方式 A：源码环境直接起服务（不需要 web/dist）
python -m litework serve --no-token

# 方式 B：打包成独立后端二进制（PyInstaller --onedir → release/backend/）
node scripts/package-backend.mjs
```

- 服务会托管 REST API（`/api/*`）与 SSE 事件流；三方 UI 可参考 `docs/web-api.md`
  与官方前端 `web/src/api.ts` 实现对接。
- `litework serve` 若未构建前端，访问首页返回 JSON 提示（后端本身可用）；
  构建 `web/dist` 后同一端口即提供完整 Web UI。
- 预缓存 matplotlib 字体可执行 `python -m litework warmup`（安装期调用）。

## 5. 运行日志与排障

- 日志目录 `~/.lite-work/logs/`（Windows：`C:\Users\<用户名>\.lite-work\logs\`），
  `lite-work.log` 后端 / `electron.log` 主进程（UTC 时间戳），按 5 MiB 滚动保留 3 份。
- 启动报错优先看日志，其次核对 Python 3.11+ / Node 18+ 与 `web/dist` 是否构建。