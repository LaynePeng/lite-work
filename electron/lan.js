// 局域网共享（手机直连本机 Core）的决策逻辑（Electron 主进程）
//
// 抽成独立模块是为了可单测：端口选择与网段识别/排序这两处判定最容易出错，
// 又最难在真机上复现（要凑出"同时连着 WiFi 与 iPhone 热点"这类多网卡环境）。
// 单测见 tests/js/lan.test.mjs。

const nodeNet = require("node:net");

/** 局域网共享的首选端口。
 *
 *  令牌是跨重启稳定的（cli.py 将令牌落盘 ~/.lite-work/server-token），因此手机
 *  书签/主屏图标的唯一失效原因就是端口变化——而 Core 默认 `--port 0` 由系统分配
 *  临时端口，重启桌面版即变。开了共享就尽量绑死这个端口，被占用才退回随机。 */
const LAN_SHARE_PORT = 8787;

/** 是否 iPhone 个人热点的固定网段：172.20.10.0/28。
 *
 *  路由侧：iPhone 开热点后自己是 172.20.10.1，DHCP 池 172.20.10.2–14（掩码 /28），
 *  即整段 0–15 都属于热点网络。判定按整段而非仅池内地址——语义是"这个地址属于
 *  热点网络"，.1/.15 落在段内也一并算上，避免边界条件带来歧义。 */
function ipKindOf(ip) {
  return /^172\.20\.10\.(?:[0-9]|1[0-5])$/.test(String(ip)) ? "hotspot" : "lan";
}

/** 本机可被手机直连的内网 IPv4 列表（按"手机最可能够得着"排序）。
 *
 *  不能裸用 os.networkInterfaces()：断连网卡会带 APIPA 链路本地地址（169.254.*，
 *  不可路由），虚拟网卡（WSL/Hyper-V 默认交换机等）也不是手机所在网段。
 *
 *  排序透露一处关键推理：**只要本机存在 172.20.10.x，就说明正接在 iPhone 热点上，
 *  而这是 iPhone 唯一能到达本机的路径**（iPhone 无法路由到本机的 192.168.x）。
 *  故热点排最前——多网卡时默认选中它，而不是按 192.168 优先（那会给出手机
 *  根本够不着的地址，用户只能看到 Safari 的通用报错）。
 *
 *  @param {Record<string, Array<{family: string, address: string, internal: boolean}>>} interfaces
 *  @returns {Array<{ip: string, iface: string, kind: "hotspot"|"lan"}>} */
function lanAddresses(interfaces) {
  const virtualIf = /WSL|Loopback|VMware|VirtualBox|Docker|ZeroTier|Tailscale|vEthernet \(Default Switch\)/i;
  const rank = (ip) => {
    if (ipKindOf(ip) === "hotspot") return -1;
    if (ip.startsWith("192.168.")) return 0;
    if (ip.startsWith("10.")) return 1;
    const m = ip.match(/^172\.(\d+)\./);
    if (m && Number(m[1]) >= 16 && Number(m[1]) <= 31) return 1;
    return 2;
  };
  const found = [];
  for (const [name, list] of Object.entries(interfaces || {})) {
    for (const ni of list || []) {
      if (ni.family !== "IPv4" || ni.internal) continue;
      if (ni.address.startsWith("169.254.")) continue;
      // Hyper-V 外置交换机会以「vEthernet (物理网卡名)」呈现真实上联口，不能按 vEthernet 一刀切
      if (virtualIf.test(name)) continue;
      found.push({ ip: ni.address, iface: name, kind: ipKindOf(ni.address), rank: rank(ni.address) });
    }
  }
  found.sort((a, b) => a.rank - b.rank);
  return found.map(({ ip, iface, kind }) => ({ ip, iface, kind }));
}

/** 拼 Core 启动参数（abs 端口由调用方决定：共享时优先 LAN_SHARE_PORT）。 */
function coreSpawnArgs({ packaged, lanShare, port }) {
  const args = packaged
    ? ["serve", "--port", String(port)]
    : ["-m", "litework", "serve", "--port", String(port)];
  if (lanShare) args.push("--host", "0.0.0.0");
  return args;
}

/** 在回环上试绑某端口：空闲返回该端口，被占用返回 null。 */
function probePort(port) {
  return new Promise((resolve) => {
    let server;
    try {
      server = nodeNet.createServer();
    } catch {
      resolve(null);
      return;
    }
    server.once("error", () => resolve(null));
    server.once("listening", () => {
      server.close(() => resolve(port));
    });
    try {
      server.listen(port, "127.0.0.1");
    } catch {
      resolve(null);
    }
  });
}

/** 局域网共享要用的端口：首选端口空闲就用它，否则返回 0（交系统随机分配）。 */
async function selectLanSharePort(preferred = LAN_SHARE_PORT) {
  return (await probePort(preferred)) === null ? 0 : preferred;
}

module.exports = { LAN_SHARE_PORT, ipKindOf, lanAddresses, coreSpawnArgs, probePort, selectLanSharePort };
