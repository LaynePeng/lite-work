# AGENTS.md — Agent 工作规范（lite-work）

> 本文件会被注入 Agent 的 System Prompt。这里的规则是**红线**，违反会造成  
> 构建失败、CI 挂掉或污染仓库。每次执行构建/打包/发版相关命令前，先回读本文件。

## 1\. 版本管理（单一事实源）

-   版本号唯一来源：`litework/__init__.py` 的 `__version__`；
-   **改版本号后必须执行** `node scripts/sync-version.mjs`——它把版本同步到  
    根 `package.json`、根 `package-lock.json`、`web/package.json`、`web/package-lock.json`；
-   同步产生的 4 个文件改动**必须与版本提交一起 commit**，否则 CI 的  
    "Committed npm version files are out of sync" 检查直接失败（v1.6.1 踩过）；
-   发版流程：bump `__version__` → `sync-version` → 测试 → commit →  
    `git tag vX.Y.Z` → `git push origin main vX.Y.Z`；
-   手动改了任何 npm 侧版本文件后，跑一次 `sync-version` 校准。

## 2\. 构建 / 打包矩阵（严禁跨平台乱用）

| 平台 | 命令 | 说明 |
| --- | --- | --- |
| **Windows** | `powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1` | **唯一正确的 Windows 打包入口**：sync-version → web 构建 → PyInstaller → NSIS，产出 `release/lite-work Setup X.Y.Z.exe` |
| Windows 清缓存 | `... build-windows.ps1 -Clean` | 清 PyInstaller 分析缓存（干净出包用） |
| **macOS** | `./scripts/build-macos.sh` | **macOS 一键打包入口**：依赖检查 → web 构建 → `package.mjs`（sync-version → 图标 → PyInstaller → .app）→ spawn-helper 权限修复，产出 `release/lite-work-<版本>-<arch>.dmg` |
| macOS 变体 | `./scripts/build-macos.sh --clean` / `--skip-web` / `--skip-deps` | 清 PyInstaller 缓存 / 跳过前端构建 / 跳过依赖检查 |
| macOS 底层 | `npm run package`（= `scripts/package.mjs`） | 供 `build-macos.sh` 内部调用的 **macOS 专用**流程（iconutil / hdiutil / DMG / .app）——**在 Windows 上运行必失败，也不要直接调它**，永远从 `build-macos.sh` 进 |
| 仅后端 | `node scripts/package-backend.mjs` | PyInstaller --onedir，输出 `release/backend/` |
| 仅前端 | `npm run build:web` | tsc + vite，输出 `web/dist/` |

**血泪教训（v1.6.1）**：在 Windows 上跑了 `npm run package`，在 icon 步骤因  
`iconutil` 不存在而崩溃。Windows 打包一律走 `build-windows.ps1`，macOS 打包  
一律走 `build-macos.sh`——两个脚本都内置 sync-version，不要绕过它们手动拼流程。

## 3\. 测试

```bash
# 后端（strict=事件负载校验 fail-fast，必须带）
LITEWORK_STRICT_EVENTS=1 .venv/Scripts/python -m pytest tests/ --timeout=120   # Windows
LITEWORK_STRICT_EVENTS=1 .venv/bin/python -m pytest tests/ --timeout=120       # macOS/Linux

# 前端
cd web && npm test
```

已知的**环境性失败**（与本机相关，非代码问题，不要试图"修复"）：

-   `test_security.py::test_trusted_skill_path_read_allowed`（Windows HOME 环境变量）；
-   `test_mechanisms.py::test_recall_chunk_pagination`（本机 I/O 过慢导致超时）。

提交前至少跑：`test_server.py test_security.py test_agent_loop.py test_context_and_tokens.py test_llm_adapters.py test_compact.py`。

## 4\. 命令纪律

1.  **跑任何构建/打包/发版命令前，先确认该脚本的平台与作用**（看脚本头部注释），  
    不要凭名字猜（`npm run package` ≠ 通用打包）；
2.  后台长任务（打包/安装依赖）**运行期间禁止**执行 `git stash` / `git checkout`  
    等改变工作树的操作——会把构建搞崩（v1.6.1 踩过）；
3.  修改代码前先读相关文件；编辑用精确 patch 工具，不做无谓的全文件重写；
4.  未经用户明确要求：不 force push、不改 tag、不删除远端分支/文件、  
    不运行删除类命令；
5.  提交信息用 `feat:` / `fix:` / `docs:` / `chore:` / `perf:` / `test:` 前缀；
6.  日志在 `~/.lite-work/logs/`（lite-work.log 后端 / electron.log 前端+Core stdout，  
    electron.log 时间戳为 UTC）——排查运行时问题先看日志再猜代码。

## 5\. 项目结构速查

-   `litework/core/` 内核（AgentLoop / 事件总线 / 上下文管理），详见 `docs/architecture.md`  
    与 `docs/core-api.md`；
-   `litework/server/` FastAPI（REST + SSE）；`litework/llm/` 手写流式适配器；
-   `litework/security/` 审批门（ApprovalGate，asyncio.Future + 600s 超时）；
-   `litework/security/approval.py` 的 resolve 用 `call_soon_threadsafe` 唤醒，改这里要  
    同时看 `server/routers/chat.py` 的 `/api/approve`（它负责广播 `approval:resolved`  
    关审批卡）；