// PWA 图标生成：scripts/app-icon.svg → web/public/icons/*.png
// 用法：node scripts/generate-pwa-icons.mjs（依赖根 devDependencies 的 sharp）
// app-icon.svg 改动后重跑一次，产物直接提交（web 构建不做图标生成）。
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";

const root = process.cwd();
const src = path.join(root, "scripts", "app-icon.svg");
const outDir = path.join(root, "web", "public", "icons");

const svg = await readFile(src, "utf8");

// maskable / apple-touch 版：圆角改直角（全出血方形），内容整体缩进
// （launcher 会按圆形/圆角裁切，内容必须落在 80% 安全区内）
function fullBleed(scale) {
  let s = svg.replace(/rx="190"/g, 'rx="0"');
  const glossEnd = s.indexOf("/>", s.indexOf("url(#gloss)")) + 2;
  const svgClose = s.lastIndexOf("</svg>");
  return (
    s.slice(0, glossEnd) +
    `<g transform="translate(512 512) scale(${scale}) translate(-512 -512)">` +
    s.slice(glossEnd, svgClose) +
    "</g>" +
    s.slice(svgClose)
  );
}

await mkdir(outDir, { recursive: true });

// 常规图标（圆角留白，透明底）
await sharp(Buffer.from(svg)).resize(192, 192).png().toFile(path.join(outDir, "icon-192.png"));
await sharp(Buffer.from(svg)).resize(512, 512).png().toFile(path.join(outDir, "icon-512.png"));
// maskable（launcher 圆形/圆角裁切不丢内容）
await sharp(Buffer.from(fullBleed(0.78))).resize(192, 192).png().toFile(path.join(outDir, "maskable-192.png"));
await sharp(Buffer.from(fullBleed(0.78))).resize(512, 512).png().toFile(path.join(outDir, "maskable-512.png"));
// apple-touch-icon（iOS 自己做圆角裁切，喂全出血方图）
await sharp(Buffer.from(fullBleed(1))).resize(180, 180).png().toFile(path.join(outDir, "apple-touch-icon.png"));

console.log("[pwa-icons] web/public/icons/ 生成完毕");
