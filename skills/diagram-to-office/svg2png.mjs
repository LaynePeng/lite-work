#!/usr/bin/env node
/* SVG → PNG 转换（基于 @resvg/resvg-js，跨平台 native，无需系统库）。
 *
 * 用法:
 *   node svg2png.mjs <input.svg> <output.png> [--scale 2] [--width 1024]
 *
 * 依赖: @resvg/resvg-js（npm install -g @resvg/resvg-js 或本地 node_modules）
 * 查找顺序: 环境变量 RESVG_DIR / 本地 node_modules / 全局 npm root。
 * 输出: PNG 文件；stdout 打印 PNG 路径。
 */
import { createRequire } from 'module';
import { execSync } from 'child_process';
import { readFileSync, writeFileSync } from 'fs';
import { pathToFileURL } from 'url';
import path from 'path';

const require = createRequire(import.meta.url);

function existsSync(p) {
  try { require('fs').statSync(p); return true; } catch (_) { return false; }
}

function findResvgDir() {
  if (process.env.RESVG_DIR) return process.env.RESVG_DIR;
  const here = path.dirname(process.argv[1]);
  let dir = here;
  while (true) {
    const cand = path.join(dir, 'node_modules', '@resvg', 'resvg-js');
    if (existsSync(cand)) return cand;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  try {
    const root = execSync('npm root -g', { encoding: 'utf8' }).trim();
    const cand = path.join(root, '@resvg', 'resvg-js');
    if (existsSync(cand)) return cand;
  } catch (_) { /* ignore */ }
  return null;
}

async function main() {
  const [input, output] = process.argv.slice(2);
  const scaleIdx = process.argv.indexOf('--scale');
  const scale = scaleIdx >= 0 && process.argv[scaleIdx + 1]
    ? parseFloat(process.argv[scaleIdx + 1]) : 2;
  const widthIdx = process.argv.indexOf('--width');
  const width = widthIdx >= 0 && process.argv[widthIdx + 1]
    ? parseInt(process.argv[widthIdx + 1], 10) : 0;

  if (!input || !output) {
    console.error('用法: node svg2png.mjs <input.svg> <output.png> [--scale 2] [--width 1024]');
    process.exit(2);
  }

  const resvgDir = findResvgDir();
  if (!resvgDir) {
    console.error('[svg2png] 未找到 @resvg/resvg-js，请先运行: npm install -g @resvg/resvg-js');
    process.exit(1);
  }

  let Resvg;
  try {
    ({ Resvg } = await import(pathToFileURL(path.join(resvgDir, 'index.js')).href));
  } catch (err) {
    console.error('[svg2png] 加载 @resvg/resvg-js 失败: ' + (err && err.message ? err.message : String(err)));
    process.exit(1);
  }

  const svg = readFileSync(input, 'utf8');
  const options = { fitTo: { mode: 'zoom', value: scale } };
  if (width > 0) options.fitTo = { mode: 'width', value: width };
  try {
    const resvg = new Resvg(svg, options);
    const png = resvg.render().asPng();
    writeFileSync(output, png);
    console.log(output);
  } catch (err) {
    console.error('[svg2png] 转换失败: ' + (err && err.message ? err.message : String(err)));
    process.exit(1);
  }
}

main();
