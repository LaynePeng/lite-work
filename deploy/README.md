# lite-work Core 自托管部署（Ubuntu 24.04 · Docker Compose）

单用户多设备的自托管形态：文件事实源在服务器，桌面版与手机 PWA 都连它。
云上 VM 或局域网常开机（NAS/小主机/旧笔记本）皆可，流程完全一致。

安全模型是「端口默认只绑 127.0.0.1 + Tailscale 访问 + 容器内 Bearer 令牌」。
**不要**在没想清楚 TLS/反代/限流之前把 8787 暴露到公网（Docker 发布端口
会绕过 ufw，防火墙只认云厂商安全组）。

## 0. 网络形态速查

| | 方案 A（推荐） | 方案 B（局域网直连） |
|---|---|---|
| 做法 | 端口保持默认回环 + Tailscale serve 出 HTTPS | `LITEWORK_BIND=0.0.0.0` 放开端口 |
| 手机访问 | `https://机器名.<tailnet>.ts.net` | `http://<局域网IP>:8787` |
| 传输加密 | ✅ WireGuard + 自动 HTTPS | ❌ 令牌明文过空口 |
| Web 推送 | ✅（HTTPS 才是安全上下文） | ❌（浏览器拒注册 Service Worker） |
| 出差/4G 连回 | ✅ | ❌ 仅同一局域网 |
| 适用 | 默认 | 家庭网络单人应急，勿在办公/公共 WiFi 用 |

## 1. 服务器初始化（一次性）

```bash
# Docker 官方源 + compose 插件
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # 重新登录生效

# Tailscale（设备间组网；免域名免证书）
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=litework
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

## 4. 对外服务（方案 A：tailscale serve）

容器默认只监听 `127.0.0.1:8787`，由 Tailscale 反代成 HTTPS 域名：

```bash
sudo tailscale serve --bg http://127.0.0.1:8787
# 输出形如 https://litework.<你的tailnet>.ts.net
```

首次可能提示去管理台（admin.tailscale.com）给 tailnet 开启 HTTPS 证书，
按提示点一次即可，之后全自动。

手机端（iOS 16.4+）：

1. App Store 装 Tailscale → 登录同一账号
2. Safari 打开 `https://litework.<tailnet>.ts.net` → 粘贴访问令牌
3. 分享 → 添加到主屏幕（PWA 安装）
4. 从主屏图标进入 → 会话列表右上角 🔔 开推送

方案 B（局域网直连）：`.env` 写 `LITEWORK_BIND=0.0.0.0` 后
`docker compose up -d`，手机浏览器开 `http://<局域网IP>:8787`。
没有推送通知，且令牌明文传输——只在家用网络这么干。

免部署变体：不想跑服务器，桌面版侧栏「📱 手机」面板可一键让手机直连
本机 Core（同样的局域网语义与限制，扫码配对，详见 CHANGELOG 2.0.0）。

## 5. 验证

```bash
curl -s http://127.0.0.1:8787/api/health   # {"status":"ok","version":...,"min_client":...}
docker compose ps                          # healthy
```

浏览器打开 `https://litework.<tailnet>.ts.net`——同源伺服前端，无需额外配置。

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
- 不够用时：升内存并改 compose 的 mem_limit 即可，镜像通用
