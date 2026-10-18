// 局域网共享决策逻辑单测（node --test tests/js/lan.test.mjs）
//
// 回归点：iPhone 热点场景。热点把 Mac 放在 172.20.10.0/28（网关 172.20.10.1），
// 而 Mac 往往**同时**连着家里 WiFi（192.168.x）——iPhone 在热点网段里够不着
// 192.168.x，所以多网卡时默认必须选热点，否则面板给出的二维码注定连不通。
// 旧的排序把 192.168 排第一，正是踩了这个坑。
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { LAN_SHARE_PORT, ipKindOf, lanAddresses, coreSpawnArgs, selectLanSharePort } = require("../../electron/lan.js");

/** 构造 os.networkInterfaces() 形状的数据。 */
const iface = (address, family = "IPv4", internal = false) => ({ address, family, internal });

test("ipKindOf：仅 172.20.10.0/28 整段算 iPhone 热点", () => {
  assert.equal(ipKindOf("172.20.10.1"), "hotspot");  // 网关
  assert.equal(ipKindOf("172.20.10.2"), "hotspot");  // DHCP 池起点
  assert.equal(ipKindOf("172.20.10.14"), "hotspot"); // 池终点
  assert.equal(ipKindOf("172.20.10.15"), "hotspot"); // /28 广播位，仍在段内
  // 段外：同第三位的下一段、以及私有 172.16-31 的邻居都不算热点
  assert.equal(ipKindOf("172.20.11.2"), "lan");
  assert.equal(ipKindOf("172.21.10.2"), "lan");
  assert.equal(ipKindOf("192.168.1.5"), "lan");
});

test("lanAddresses：热点排最前（iPhone 唯一能到达本机的路径）", () => {
  const got = lanAddresses({
    en0: [iface("192.168.1.5")],
    en7: [iface("172.20.10.2")],
  });
  assert.deepEqual(got.map((a) => a.ip), ["172.20.10.2", "192.168.1.5"]);
  assert.equal(got[0].kind, "hotspot");
  assert.equal(got[0].iface, "en7");
  assert.equal(got[1].kind, "lan");
});

test("lanAddresses：过滤回环 / APIPA / 虚拟网卡，并排掉 IPv6 与非 IPv4", () => {
  const got = lanAddresses({
    lo0: [iface("127.0.0.1", "IPv4", true)],
    en1: [iface("169.254.3.4")],            // APIPA：不可路由
    "vEthernet (Default Switch)": [iface("172.28.0.1")],
    utun3: [iface("fe80::1", "IPv6")],      // Tailscale 之外的 IPv6 一律不要
    en0: [iface("10.0.0.7")],
  });
  assert.deepEqual(got, [{ ip: "10.0.0.7", iface: "en0", kind: "lan" }]);
});

test("lanAddresses：空/异常输入不抛错", () => {
  assert.deepEqual(lanAddresses({}), []);
  assert.deepEqual(lanAddresses(undefined), []);
  assert.deepEqual(lanAddresses({ en0: null }), []);
});

test("coreSpawnArgs：共享时绑 0.0.0.0，打包与开发态参数前缀不同", () => {
  assert.deepEqual(
    coreSpawnArgs({ packaged: true, lanShare: true, port: LAN_SHARE_PORT }),
    ["serve", "--port", "8787", "--host", "0.0.0.0"],
  );
  assert.deepEqual(
    coreSpawnArgs({ packaged: false, lanShare: true, port: 0 }),
    ["-m", "litework", "serve", "--port", "0", "--host", "0.0.0.0"],
  );
  // 未开共享：只绑回环（默认），不追加 --host
  assert.deepEqual(
    coreSpawnArgs({ packaged: true, lanShare: false, port: 0 }),
    ["serve", "--port", "0"],
  );
});

test("selectLanSharePort：空闲则用固定端口，被占用退回 0（随机）", async () => {
  const net = require("node:net");
  assert.equal(await selectLanSharePort(), LAN_SHARE_PORT, "空闲时应返回固定端口");

  // 占住该端口（模拟已有一个实例在跑）→ 必须退回随机，而不是因端口冲突起不来
  const blocker = net.createServer();
  await new Promise((res) => blocker.listen(LAN_SHARE_PORT, "127.0.0.1", res));
  try {
    assert.equal(await selectLanSharePort(), 0, "端口被占用时应退回 0");
  } finally {
    await new Promise((res) => blocker.close(res));
  }
});
