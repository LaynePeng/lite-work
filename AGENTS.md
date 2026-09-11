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

-   `litework/core/` 内核（AgentLoop / 事件总线 / 上下文管理），详见 `docs/architecture.md`、  
    `docs/plugin-guide.md` 与 `docs/web-api.md`；
-   `litework/server/` FastAPI（REST + SSE）；`litework/llm/` 手写流式适配器；
-   `litework/security/` 审批门（ApprovalGate，asyncio.Future + 600s 超时）；
-   `litework/security/approval.py` 的 resolve 用 `call_soon_threadsafe` 唤醒，改这里要  
    同时看 `server/routers/chat.py` 的 `/api/approve`（它负责广播 `approval:resolved`  
    关审批卡）；

## 6\. 插件许可与同步治理（lite-work-plugins 为上游）

社区插件仓库 [laynepeng/lite-work-plugins](https://github.com/laynepeng/lite-work-plugins)  
是 office-plugin / ocr-plugin / webfetch-plugin / collab-\* 等插件的**上游事实源**。

1.  **许可红线**：lite-work-plugins 整体为 **MIT**。凡来自（或同步自）该仓库的  
    插件/技能代码，**严禁添加 Apache 头**（`# SPDX-License-Identifier: Apache-2.0`  
    等一律不得出现）——同步副本保持社区原样，许可信息不得改写、不得污染；  
    主仓库自有代码（core/server/web 等）仍为 Apache-2.0，两者并存互不覆盖；
2.  **版本以社区为主**：插件功能版本号独立于主应用（如 office-plugin v1.3.0），  
    以社区 manifest 为准，「检查社区更新」的对比基准就是它；
3.  **修改流程（顺序不可反）**：发现插件问题 → 先在 lite-work-plugins 修复并  
    更新版本号 → 再把社区新版同步内置。**禁止只改主仓库内置副本**——  
    那会造成与上游分叉，且会被用户的社区更新覆盖丢失；
4.  **「同步内置」＝等价于用户在设置里点升级**：把社区包代码**原样**引入内置  
    （office-plugin → `litework/tools/office.py`，collab-\* → `litework/builtin_plugins/`），  
    只允许承载所必需的机械适配（截掉社区分发包装类等），**不夹带**主仓库侧的  
    额外改动（TOOL_NAMES、Agent 工具白名单、提示词、前端行为等——用户手动  
    升级社区插件时这些都不会变，同步也不应变）；
5.  **已知边界**：内置 `litework/tools/office.py` 已按本原则与社区 v1.3.0 同源  
    （MIT、无许可证头）；`ocr.py` / `web.py` 仍是主仓库原生文件，待社区对齐时  
    再按同一原则处理，不要顺手「统一」它们。