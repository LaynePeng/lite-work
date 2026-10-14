# lite-work Core 云端部署（Ubuntu 24.04 · Docker Compose）

单用户多设备的自托管形态：文件事实源在服务器，桌面版与手机 PWA 都连它。
安全模型是「NSG 默认全关 + Tailscale 访问 + 容器内 Bearer 令牌」——
**不要**在没想清楚 TLS/反代/限流之前把 8787 暴露到公网（Docker 发布端口
会绕过 ufw，防火墙只认 Azure NSG）。

## 1. 服务器初始化（一次性）

```bash
# Docker 官方源 + compose 插件
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # 重新登录生效

# Tailscale（设备间组网；免域名免证书）
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

## 2. 部署 / 升级

```bash
git clone https://github.com/LaynePeng/lite-work.git
cd lite-work/deploy

# 首次构建较慢（node 前端 + apt libreoffice + pip 依赖，~10-20 分钟）
docker compose up -d --build

# 日常升级
git pull && docker compose up -d --build
```

## 3. 拿访问令牌

首启自动生成、存在数据卷（重启不变）：

```bash
docker compose logs core | grep "访问令牌"
```

桌面远端模式 / 手机 PWA 连接时都填它。显式指定则 `.env` 里写
`LITEWORK_TOKEN=xxx`（或 `DEEPSEEK_API_KEY=sk-...` 引导 LLM 配置）。

## 4. 验证

```bash
curl -s http://127.0.0.1:8787/api/health   # {"status":"ok","version":...,"min_client":...}
docker compose ps                          # healthy
```

浏览器打开 `http://<tailscale-ip>:8787`——同源伺服前端，无需额外配置。

## 数据与备份

全部状态在卷 `core-data`（`/data`）：会话、插件、技能、工作区
（`/data/workspace`）、盯梢计划、日志、VAPID 密钥、令牌。备份即备卷：

```bash
docker run --rm -v lite-work_core-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/litework-data-$(date +%F).tgz /data
```

## 内存护栏（4GB 机）

- compose：`mem_limit: 3500m`（给 OS 留 ~500MB）+ Chromium 的 `shm_size: 1g`
- 首启配置播种 `max_running_tasks: 4`（顶层任务并发上限；可在设置页改）
- 子 agent 并发由 `max_parallel_agents`（默认 4）独立约束
- 不够用时：升 B2as v2（2v/8G，改 compose 的 mem_limit 即可），镜像通用
