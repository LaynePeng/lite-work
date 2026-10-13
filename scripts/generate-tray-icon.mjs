// 托盘图标生成：tray-icon.svg -> electron/tray-icon.png
//
// 为什么单独一个脚本而不复用 generate-icon.mjs：
//   1. 托盘图标是**单色小字形**，与应用图标（渐变底 + 闪电 + 齿轮）不是同一份素材，
//      16px 下应用图标会糊成一团；
//   2. 输出位置也不同——必须落在 electron/ 下，因为 electron-builder 的
//      files 白名单只含 electron/**，放 release/resources 不会进包
//      （Tray 加载不到 → 托盘常驻静默失效，这正是本脚本要修的坑）；
//   3. 不依赖平台专属的 iconutil/app-builder，跨三平台都能跑，因此可以放进
//      所有打包入口而不必分平台分支。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const svg = path.join(root, "scripts", "tray-icon.svg");
const outDir = path.join(root, "electron");

async function main() {
  if (!fs.existsSync(svg)) {
    console.error(`[tray-icon] 源文件不存在: ${svg}`);
    process.exit(1);
  }
  fs.mkdirSync(outDir, { recursive: true });
  // 64px：覆盖普通与 HiDPI 托盘（Electron 会按需缩放）。透明底 + 白色字形，
  // macOS 上再由 setTemplateImage 交给系统按主题着色。
  const out = path.join(outDir, "tray-icon.png");
  await sharp(svg).resize(64, 64).png().toFile(out);
  console.log(`[tray-icon] Done: ${path.relative(root, out)}`);
}

main().catch((err) => {
  console.error("[tray-icon] Generation failed:", err.message);
  process.exit(1);
});
