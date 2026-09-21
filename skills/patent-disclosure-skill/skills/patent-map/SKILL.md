---
name: patent-map
description: "专利地图：基于通俗解读已入库的 Obsidian 案例，点名后打开五种图。地形按 bge-small-zh 向量摊开。"
user-invocable: false
---

# 专利地图

须用户点名（专利地图 / 案例地图 / `/专利地图` / `/patent-map`）。  
**不要**因「读专利 / 写交底」自动进入。解读继续只入库；本包只读 vault、起本地页。

1. **`Read`** `prompts/guardrails.md` → `intake.md`
2. 确认库路径（与解读同一份：`PATENT_READER_OBSIDIAN_VAULT` 或 `~/.patent-disclosure-skill/obsidian_vault.txt`）
3. 启动：

```bash
python skills/patent-map/tools/serve_map.py
```

stdout 机读前缀：`MAP_URL:` / `MAP_PORT:` / `MAP_VAULT:` / `MAP_SOURCE:` / `MAP_CACHE:` / `MAP_MODEL:`。  
端口由系统随机分配（`127.0.0.1:0`），避免冲突。对话里把 URL 发给用户；**不要**把服务焊进解读入库。

地形图按解读短文向量摊开。加载名 `BAAI/bge-small-zh-v1.5`，磁盘只装 **Qdrant ONNX**（不要 PyTorch）。数据与模型默认目录：`{Documents}/patent-disclosure-skill/patent-map/`（与 oa 同级）。未装 `fastembed` 时图仍能开，地形退回 IPC 簇。

可选：

```bash
pip install -r skills/patent-map/tools/requirements.txt
python skills/patent-map/tools/ensure_model.py
```

单独拷走本包时：`python tools/serve_map.py`。

取数：优先扫库目录 `Research/Patents/**/*_解读_*.md`。若本机 `obsidian` CLI 可用且库已打开，用 `obsidian vaults` / `base:query` 核对，不替代磁盘扫描。
