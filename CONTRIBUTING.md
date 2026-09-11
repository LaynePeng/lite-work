# 贡献指南

感谢关注 lite-work！本项目的卖点之一是**内核纯手写、不依赖 LangChain 等
高层框架**，因此对贡献者的核心要求是：改动能读懂、可测试、风格一致。

## 开发环境

```bash
# 前提：Python 3.11+、Node 18+
python3 -m venv .venv
.venv/bin/pip install -e .[dev]    # Windows: .venv\Scripts\pip install -e .[dev]
npm install

npm run dev    # 开发模式：Python Core + Vite + Electron
```

国内网络：pip 可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`；
Electron 二进制下载失败时执行 `node node_modules/electron/install.js`。

## 提交前必做

```bash
# 后端测试（strict=事件负载校验 fail-fast，必须开启）
LITEWORK_STRICT_EVENTS=1 .venv/bin/python -m pytest tests/

# 前端测试
cd web && npm test
```

- 后端测试**必须**带 `LITEWORK_STRICT_EVENTS=1`——所有事件负载都有
  TypedDict 契约，strict 模式下违反契约直接失败；
- 新增/修改事件负载时同步更新 `litework/core/events.py` 里的 TypedDict。

## 代码约定

1. **内核保持小而纯**：`litework/core/` 不引入新的大依赖；新能力优先做成
   插件（工具插件 / 协作模式插件 / MCP / 技能）而不是改内核；
2. **一切皆事件**：UI 需要的任何状态变化都通过 `TypedEventBus` 事件暴露，
   不要为 UI 开专用内部接口；
3. **上下文是稀缺资源**：新增工具的输出要有界——超长结果走截断落盘 +
   句柄（`truncator` / `observation_pack`），不要把大文本直接塞进上下文；
4. **提示词约定不如管道强制**：涉及权限/安全的行为用中间件硬校验；
5. 气泡类 UI 提示必须回答"它何时消失"再动手（见 README「UI 交互约定」）。

## 提交规范

- Commit message 用 `feat:` / `fix:` / `docs:` / `chore:` / `perf:` /
  `test:` 前缀，一句话说清做了什么；
- 版本号单一事实源：`litework/__init__.py` 的 `__version__`（构建自动同步）。

## 提交 PR

1. Fork → 建 feature 分支 → 提交 → PR 到 `main`；
2. PR 描述里写清：动机、改动点、已跑过的测试；
3. 涉及新工具 / 新事件的改动，请附最小复现或测试用例。
