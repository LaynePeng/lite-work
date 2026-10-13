// Cross-platform icon generation: app-icon.svg -> PNG (sharp) -> .icns (macOS) / .ico (Windows)
// Depends on sharp (SVG rendering) and app-builder-bin (ICNS/ICO conversion), both devDependencies.
//
// 关键行为：**两种格式都生成**，不按当前平台分叉。
// 原因：electron-builder 的 mac.icon / win.icon 是配置里写死的路径，打包时
// 必须已存在；而 CI 只在一台机器上打一个平台的包很常见，按平台分叉会导致
// 另一个平台的图标文件缺失 → 打包退回默认 Electron 图标（用户看到「不是我
// 装的那个应用」）。生成成本很低，一律生成省掉这类隐形坑。
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const svg = path.join(root, "scripts", "app-icon.svg");
const outDir = path.join(root, "release", "resources");
const png1024 = path.join(outDir, "app-icon-1024.png");

fs.mkdirSync(outDir, { recursive: true });

function appBuilderBin() {
  // app-builder-bin 布局按平台不同：win/linux 是 <platform>/<arch>/app-builder[.exe]，
  // mac 是 mac/app-builder_<amd64|arm64>（无 arch 子目录）。
  const base = path.join(root, "node_modules", "app-builder-bin");
  const bin = process.platform === "win32"
    ? path.join(base, "win", process.arch, "app-builder.exe")
    : process.platform === "darwin"
      ? path.join(base, "mac", process.arch === "arm64" ? "app-builder_arm64" : "app-builder_amd64")
      : path.join(base, "linux", process.arch, "app-builder");
  if (fs.existsSync(bin)) {
    // npm 解包后二进制有时丢了执行位（本机就踩过：Permission denied），
    // 这里兜底补上，避免「换个环境打包就失败」。
    if (process.platform !== "win32") {
      try { fs.chmodSync(bin, 0o755); } catch { /* 只读 FS 等场景忽略 */ }
    }
    return bin;
  }
  return "app-builder"; // PATH fallback
}

/** 调 app-builder 生成图标；输出文件名固定为 app-icon.<ext>。
 *  该工具默认产出 icon.<ext>，这里统一改名为配置里引用的名字。 */
function genPlatformIcon(builder, format, ext) {
  const produced = path.join(outDir, `icon.${ext}`);
  try { fs.rmSync(produced, { force: true }); } catch { /* ignore */ }
  execSync(`"${builder}" icon -i "${png1024}" -f ${format} --out "${outDir}"`, { stdio: "inherit" });
  const target = path.join(outDir, `app-icon.${ext}`);
  if (fs.existsSync(produced)) {
    fs.rmSync(target, { force: true });
    fs.renameSync(produced, target);
    return true;
  }
  if (fs.existsSync(target)) return true;   // 部分版本会直接按目标名输出
  console.warn(`[icon] 警告：${format} 生成后未找到产物（${produced} / ${target}）`);
  return false;
}

async function main() {
  console.log("[icon] 1/3: SVG -> 1024 PNG (sharp)");
  await sharp(svg).resize(1024, 1024).png().toFile(png1024);

  const builder = appBuilderBin();
  console.log("[icon] 2/3: Generate platform icons (.icns + .ico，两个都生成)");
  let anyFailed = false;
  if (!genPlatformIcon(builder, "icns", "icns")) anyFailed = true;
  if (!genPlatformIcon(builder, "ico", "ico")) anyFailed = true;

  // 保留 1024 PNG（Linux / 通用兜底）
  fs.copyFileSync(png1024, path.join(outDir, "app-icon.png"));
  console.log("[icon] 3/3: Done:");
  for (const f of fs.readdirSync(outDir)) {
    if (f.startsWith("app-icon") || f === "icon.ico" || f === "icon.icns") {
      console.log(`   release/resources/${f}`);
    }
  }
  if (anyFailed) {
    // 不直接 exit(1)：mac 上交叉生成 .ico 偶有失败，但已生成的那个仍可用。
    // 用明确的警告文案让打包者知道该平台图标可能缺失。
    console.warn("[icon] 警告：部分图标生成失败，请检查上面输出（缺失平台的打包会退回默认图标）");
  }
}

main().catch((err) => {
  console.error("[icon] Generation failed:", err.message);
  process.exit(1);
});
