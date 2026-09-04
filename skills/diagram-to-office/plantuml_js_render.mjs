#!/usr/bin/env node
/* PlantUML 纯 JS 离线渲染（基于 @plantuml/core，TeaVM 编译，无需 Java/网络）。
 *
 * 用法:
 *   node plantuml_js_render.mjs <input.puml> <output.svg> [--dark]
 *
 * 依赖: @plantuml/core（npm install -g @plantuml/core 或本地 node_modules）
 * 会依次在: 环境变量 PLANTUML_CORE_DIR / 本地 node_modules / 全局 npm root 查找。
 * 输出: SVG 文本文件；stdout 打印 SVG 路径。
 */
import { createRequire } from 'module';
import { execSync } from 'child_process';
import { readFileSync, writeFileSync } from 'fs';
import { fileURLToPath, pathToFileURL } from 'url';
import path from 'path';

const require = createRequire(import.meta.url);

function findCoreDir() {
  // 1) 环境变量显式指定
  if (process.env.PLANTUML_CORE_DIR) return process.env.PLANTUML_CORE_DIR;
  // 2) 本地 node_modules（脚本同目录或上级）
  const here = path.dirname(process.argv[1]);
  let dir = here;
  while (true) {
    const cand = path.join(dir, 'node_modules', '@plantuml', 'core');
    if (existsSync(cand)) return cand;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  // 2b) 安装包内置位置（~/.agents 同步不带 node_modules，由
  //     LITEWORK_SKILLS_SOURCE 指向 _internal/skills）
  if (process.env.LITEWORK_SKILLS_SOURCE) {
    const cand = path.join(process.env.LITEWORK_SKILLS_SOURCE,
      'diagram-to-office', 'node_modules', '@plantuml', 'core');
    if (existsSync(cand)) return cand;
  }
  // 3) 全局 npm root
  try {
    const root = execSync('npm root -g', { encoding: 'utf8' }).trim();
    const cand = path.join(root, '@plantuml', 'core');
    if (existsSync(cand)) return cand;
  } catch (_) { /* ignore */ }
  return null;
}

function existsSync(p) {
  try { require('fs').statSync(p); return true; } catch (_) { return false; }
}

async function loadEngine() {
  const coreDir = findCoreDir();
  if (!coreDir) {
    throw new Error(
      '未找到 @plantuml/core，请先运行: npm install -g @plantuml/core\n' +
      '（一次预装后完全离线，无需 Java）'
    );
  }
  const plantumlJs = path.join(coreDir, 'plantuml.js');
  const vizJs = path.join(coreDir, 'viz-global.js');
  // @plantuml/core 是浏览器版引擎（引用 window/document），Node 中注入垫片。
  // viz-global.js 是 CommonJS 风格 UMD 脚本（引用 require/__filename），
  // 在 ESM 中需要先注入全局变量再加载，否则报 "require is not defined"。
  if (typeof globalThis.window === 'undefined') {
    // ---- 迷你 DOM 实现：让 @plantuml/core（浏览器引擎）在 Node 中构建并
    // 序列化 SVG。元素记录 tag/attributes/children，XMLSerializer 递归还原。
    function makeEl(tag) {
      const el = {
        tagName: String(tag),
        attributes: {},
        children: [],
        nodeType: 1,
        style: {},
        setAttribute(k, v) { el.attributes[k] = String(v); },
        setAttributeNS(_ns, k, v) { el.setAttribute(k, v); },
        getAttribute(k) { return k in el.attributes ? el.attributes[k] : null; },
        appendChild(c) { el.children.push(c); return c; },
        removeChild(c) {
          el.children = el.children.filter((x) => x !== c);
          return c;
        },
        insertBefore(c) { el.children.push(c); return c; },
        addEventListener() {},
        removeEventListener() {},
        classList: { add() {}, remove() {}, toggle() {} },
        getBoundingClientRect: () => ({ width: 0, height: 0, x: 0, y: 0 }),
        // 引擎用 getBBox / getComputedTextLength 测量文本，估算即可
        getBBox() {
          const t = _textLen(el);
          return { width: t * 8, height: 16, x: 0, y: 0 };
        },
        getComputedTextLength() { return _textLen(el) * 8; },
        createProcessingInstruction: () => ({ nodeType: 7, text: '' }),
        set innerHTML(v) { el.children = [{ nodeType: 3, text: String(v) }]; },
        get innerHTML() { return ''; },
        set textContent(v) {
          el.children = el.children.filter((c) => c.nodeType !== 3);
          el.children.push({ nodeType: 3, text: String(v == null ? '' : v) });
        },
        get textContent() { return _text(el); },
      };
      return el;
    }
    function _textLen(el) {
      const s = _text(el);
      return s ? s.length : 0;
    }
    function _text(el) {
      let out = '';
      const walk = (n) => {
        if (n.nodeType === 3) out += String(n.text ?? '');
        else for (const c of n.children || []) walk(c);
      };
      walk(el);
      return out;
    }
    function esc(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    class MiniXMLSerializer {
      serializeToString(node) {
        if (!node) return '';
        if (node.nodeType === 3) return esc(node.text ?? '');
        // 处理指令（如 XML 声明）会被引擎追加在 SVG 尾部且 target 为空，
        // 属于无效 XML；SVG 渲染不需要它，直接忽略。
        if (node.nodeType === 7) return '';
        if (node.nodeType === 9) {
          // 文档节点：序列化其子元素（仅取元素节点）
          const el = (node.children || []).find((c) => c.nodeType === 1);
          return el ? this.serializeToString(el) : '';
        }
        let s = `<${node.tagName}`;
        for (const [k, v] of Object.entries(node.attributes || {})) {
          s += ` ${k}="${esc(v)}"`;
        }
        if (!node.children || node.children.length === 0) return s + '/>';
        s += '>';
        for (const c of node.children) s += this.serializeToString(c);
        return s + `</${node.tagName}>`;
      }
    }
    // 测量画布：引擎用 canvas 2D 上下文测文本宽度（布局近似，SVG 输出仍准确）
    const fakeCtx = new Proxy({
      measureText: (t) => ({ width: String(t == null ? '' : t).length * 8 }),
      font: '14px sans-serif',
      fillStyle: '#000',
      strokeStyle: '#000',
      setTransform() {},
      save() {},
      restore() {},
      beginPath() {},
      fillRect() {},
      clearRect() {},
    }, {
      get(target, prop) {
        if (prop in target) return target[prop];
        return () => ({ width: 0, height: 0 });
      },
    });
    const fakeCanvas = {
      getContext: () => fakeCtx,
      width: 1024,
      height: 768,
      style: {},
    };
    // 引擎用 DOMParser 解析 Graphviz 输出的 SVG 字符串，返回元素树
    class MiniDOMParser {
      parseFromString(xml, _mime) {
        const root = { tagName: '#document', children: [], nodeType: 9 };
        const stack = [root];
        const tokenRe =
          /<\?[\s\S]*?\?>|<!--[\s\S]*?-->|<![CDATA\[[\s\S]*?\]\]>|<\/?([A-Za-z][\w:.-]*)((?:"[^"]*"|'[^']*'|[^"'<>])*?)(\/?)>|([^<]+)/g;
        let m;
        while ((m = tokenRe.exec(xml)) !== null) {
          const tok = m[0];
          if (tok.startsWith('<?') || tok.startsWith('<!--')) continue;
          if (tok.startsWith('<![CDATA[')) {
            stack[stack.length - 1].children.push({ nodeType: 3, text: tok.slice(9, -3) });
            continue;
          }
          if (tok.startsWith('</')) {
            // 闭合标签：从栈中弹出直到同名元素（SVG 结构规整，通常栈顶即匹配）
            const tag = m[1];
            for (let i = stack.length - 1; i >= 1; i--) {
              if (stack[i].tagName === tag) {
                stack.length = i;
                break;
              }
            }
            continue;
          }
          if (m[1]) { // 开始标签或自闭合标签
            const tag = m[1];
            const attrsRaw = m[2] || '';
            const selfClose = m[3] === '/';
            const el = makeEl(tag);
            el.ownerDocument = docStub;
            const attrRe = /([\w:.-]+)\s*=\s*("([^"]*)"|'([^']*)')/g;
            let am;
            while ((am = attrRe.exec(attrsRaw)) !== null) {
              el.attributes[am[1]] = am[3] != null ? am[3] : (am[4] || '');
            }
            stack[stack.length - 1].children.push(el);
            if (!selfClose) stack.push(el);
          } else if (m[4] !== undefined) { // 文本节点
            const txt = m[4].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
              .replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&');
            if (txt.trim()) stack[stack.length - 1].children.push({ nodeType: 3, text: txt });
          }
        }
        return {
          documentElement: root.children.find((c) => c.nodeType === 1) || makeEl('svg'),
          ownerDocument: root,
        };
      }
    }
    const docBody = makeEl('body');
    const docStub = {
      documentElement: makeEl('html'),
      createElement: (tag) => String(tag).toLowerCase() === 'canvas' ? fakeCanvas : (() => { const e = makeEl(tag); e.ownerDocument = docStub; return e; })(),
      createElementNS: (_ns, tag) => { const e = makeEl(tag); e.ownerDocument = docStub; return e; },
      createTextNode: (text) => ({ nodeType: 3, text: String(text) }),
      createProcessingInstruction: () => ({ nodeType: 7, text: '' }),
      head: makeEl('head'),
      body: docBody,
      currentScript: null,
      baseURI: 'file:///tmp/',
      implementation: {
        createDocument: () => { const e = makeEl('svg'); e.ownerDocument = docStub; return e; },
        createDocumentType: () => ({}),
        createHTMLDocument: () => docStub,
        createProcessingInstruction: () => ({ nodeType: 7, text: '' }),
      },
    };
    globalThis.window = globalThis;
    globalThis.document = docStub;
    globalThis.XMLSerializer = MiniXMLSerializer;
    globalThis.DOMParser = MiniDOMParser;
    globalThis._measureCanvas = fakeCanvas;
  }
  if (existsSync(vizJs)) {
    if (typeof globalThis.require === 'undefined') {
      globalThis.require = createRequire(import.meta.url);
    }
    if (typeof globalThis.__filename === 'undefined') {
      globalThis.__filename = fileURLToPath(import.meta.url);
    }
    await import(pathToFileURL(vizJs).href);
  }
  const mod = await import(pathToFileURL(plantumlJs).href);
  return mod;
}

function renderToStringPromise(engine, lines, options) {
  return new Promise((resolve, reject) => {
    try {
      engine.renderToString(lines, resolve, reject, options || {});
    } catch (err) {
      reject(err);
    }
  });
}

async function main() {
  const [input, output] = process.argv.slice(2);
  const dark = process.argv.includes('--dark');
  if (!input || !output) {
    console.error('用法: node plantuml_js_render.mjs <input.puml> <output.svg> [--dark]');
    process.exit(2);
  }
  let engine;
  try {
    engine = await loadEngine();
  } catch (err) {
    console.error('[plantuml-js] ' + (err && err.message ? err.message : String(err)));
    process.exit(1);
  }
  const source = readFileSync(input, 'utf8');
  const lines = source.split(/\r\n|\r|\n/);
  try {
    const svg = await renderToStringPromise(engine, lines, { dark });
    writeFileSync(output, svg, 'utf8');
    console.log(output);
  } catch (err) {
    console.error('[plantuml-js] 渲染失败: ' + (err && err.message ? err.message : String(err)));
    process.exit(1);
  }
}

main();
