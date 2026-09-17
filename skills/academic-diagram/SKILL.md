---
name: academic-diagram
description: Use when a user wants a TikZ figure for an academic paper — an icon, an editable architecture/pipeline/flow template, or an example — and wants you to find, edit, and verify it. The skill for paper figures, powered by the OpenTikZ library. For Office documents (PlantUML/Mermaid embedded into Word/PPT) use diagram-to-office instead; for patent application drawings use the patent skill's own black-and-white engine.
version: "0.1.1"
triggers: tikz,TikZ,opentikz,论文配图,论文图,学术图,科研绘图,科研图,架构图,算法图,算法流程图,框图,论文插图,paper figure,architecture diagram,algorithm figure
---

# Using OpenTikZ

> **Scope: academic papers.** This skill draws paper figures (editable, colored
> TikZ). Do **not** use it for: Office deliverables that need PlantUML/Mermaid
> rendered and embedded into Word/PPT (use `diagram-to-office`), or patent
> application drawings, which are black-and-white line art produced by the
> patent skill's own engine (`render_invention_figures.py`) — not by TikZ.

OpenTikZ is a library of copyable TikZ **icons**, editable **templates** (neural
nets, encoder-decoder, training pipelines, system block diagrams, flowcharts),
and full **examples**. This skill is how you go from a user's request to a
finished, compiling `.tex` figure — reliably, and without guessing.

There is **one** skill (this file). Per-template knowledge lives in each
template's `edit_contract` (inside its `meta.json`), which you read at edit time.
Do not look for per-template `skill.md` files; they no longer exist.

## 0. First: which mode are you in, and where is the library?

**Pick the mode:**
- **Mode A — produce a figure for the user's own project** (the default). The
  OpenTikZ library is read-only and lives elsewhere; the user is working in their
  own paper/LaTeX project. You will *copy* a figure out of the library and edit the
  copy in their project. Never modify the library.
- **Mode B — contribute back to the OpenTikZ repo itself.** The user is editing the
  library: their working directory *is* the repo. Edit in place and run the repo
  tooling (see §6).

**Locate the library root (`OTROOT`) for Mode A:**
1. If the environment variable `${CLAUDE_PLUGIN_ROOT}` is set, you were installed as
   a Claude Code plugin: `OTROOT = ${CLAUDE_PLUGIN_ROOT}`.
2. If the directory containing this `SKILL.md` directly contains `catalog.json`
   (self-contained skill install — the lite-work distribution copies the whole
   library into one skill directory, e.g. `~/.agents/skills/academic-diagram/`), then
   `OTROOT` is that directory.
3. Otherwise (cloned repo / another agent), `OTROOT` is the OpenTikZ repo root — the
   directory two levels above this `SKILL.md` (`skills/using-opentikz/SKILL.md` →
   `../../`).
4. If you can only read the repo remotely (e.g. over GitHub, no local clone), treat
   the GitHub repo as `OTROOT` and fetch raw file contents on demand.

Confirm `OTROOT` once (e.g. list `OTROOT`/catalog.json) before relying on it. The
**output target is always the user's current working directory**, never `OTROOT`.

## 1. Communicate precisely (do this first, throughout)

Your job is to produce the figure the user actually wants, not a plausible guess.
Classify every ambiguity before acting:

**Material ambiguity → ask, or offer two concrete alternatives.**
Trigger when the answer changes the figure's *structure or identity*, is *hard to
reverse*, or you would otherwise be guessing between genuinely different intents.
Ask one crisp question with concrete options; never a vague "what do you want?".

- "draw a transformer" → encoder-only, decoder-only, or encoder-decoder? (ask)
- "add attention" → self-attention or cross-attention, and where? (ask)
- "highlight the new part" → which part is new? (ask)
- Use **offer-two-alternatives** when seeing beats describing — an aesthetic or
  layout fork (two color schemes, two layouts). Produce both and let the user
  pick. Don't use it for continuous quantities (widths, counts).

**Safe-default ambiguity → proceed, state the assumption in one line, keep it
cheap to reverse.** When there is a sensible default and changing it is a
one-line tweak, don't interrupt — just decide and tell the user what you assumed.

- palette: default light Okabe-Ito unless "dark" is mentioned.
- width: default standalone (no `\resizebox`) unless a venue/column width is named.
- spacing / size / an unspecified-but-conventional count: use the template default
  (e.g. a 4-layer net `4,6,6,2`).

**Anti-interrogation guardrail.** Ask at most ~2–3 questions up front, batched;
resolve everything else with assume-and-state. Every delivery ends with a
one-line summary: *"Assumptions: light palette, single-column width, 4 layers —
say the word to change any."*

## 2. Workflow

1. **Understand & classify.** Restate the goal in one sentence; classify
   ambiguity per §1; ask the (few) material questions, batched.
2. **Discover.** Read `catalog.json` at `${OTROOT}` (see §0). Match the request to
   icons / templates / examples by `name`, `tags`, `domain`. If several fit,
   present the candidates (with their `id`s) and let the user choose; if one
   clearly fits, name it and proceed.
3. **Select & confirm.** Confirm the chosen base plus the material parameters
   (e.g. layer counts, labels, which part is highlighted).
4. **Copy, then edit (Mode A) / edit in place (Mode B).**
   - **Mode A:** copy the chosen item's `.tex` from `OTROOT` into the user's project
     (a sensible path like `figures/<name>.tex`); tell the user where you put it.
     Edit *that copy*, never the file under `OTROOT`. If the figure is already in
     the user's project, edit it there. For a **template**, read its `edit_contract`
     from `OTROOT`'s `catalog.json` (or the item's `meta.json`) and: edit only the
     contract's `parameters`; follow its `operations` as the recipe; keep every
     `invariant`; preserve the `node_naming` scheme; colours via palette names only.
     For an **icon/example** (no contract), edit under the same hard rules.
   - **Mode B:** edit the item in place inside the repo, under the same rules.
5. **Verify by compiling — in the user's project (Mode A).** Run the user's LaTeX on
   the copied/edited file (`latexmk -pdf <file>.tex`, or `pdflatex`). Fix failures
   before returning — never hand back a figure you didn't compile. (Regenerating
   `.svg` previews and `catalog.json` is a *Mode B / contributor* task — skip it
   when producing a figure for a user.) If no local LaTeX toolchain exists
   (`latexmk` / `pdflatex` / `xelatex` / `tectonic` all missing), say so
   explicitly, hand over the unverified `.tex` together with the library's
   `preview.svg` of the same item as a visual reference, and point the user to
   Overleaf or a local TeX install for compilation.
6. **Deliver.** Return the edited `.tex` and the one-line assumptions summary.

## 3. Hard rules (never violate)

- The `.tex` must stay **standalone-compilable** (`\documentclass{standalone}`).
- **Colors only via the five palette names** (`otblue otorange otteal otpurple
  otgray`); tints/shades like `otblue!15` are fine. Never inline a hex value or a
  stock xcolor name (`blue`, `red`). See `reference/color-palettes/`.
- **Preserve node names / the `node_naming` scheme** — they are the contract that
  lets edits target the right parts. Give any new node a clear semantic name.
- **Keep figures parametric** — drive counts/spacing/labels through the `\def`
  block at the top; don't hard-code what a parameter already controls.
- **Separate icon from label — never overlay a `\pic` on a node's centred text.**
  For any box that carries *both* a glyph and a label, make the box node empty
  (`{}`), then place the icon with a standalone `\pic` in the box's **upper half**
  and the label as a *separate* text node in the **lower half** (or icon-left /
  text-right for a wide chip). Size the box tall enough for both bands. Dropping a
  `\pic` on top of a node's own `{text}`, or faking the offset with `\hspace`,
  collides every time — this is the single most common rework.
- **Budget padding — size a box to its content *plus* margin, never flush to it.**
  Set `minimum width`/`minimum height` to the widest/tallest content **plus a
  per-side padding budget of ≥0.25 cm**. For a multi-column in-box layout (e.g.
  two icon+label stacks), also give an explicit inter-column gap (≥0.3 cm) and keep
  each column's widest line clear of the frame. If any text touches the border,
  widen the box or push the columns inward — do **not** shrink the text.
- **标签不压箭头线** — 把标签放在箭头路径之外，不写在 `\draw` 的
  `node[...]` 里（那会把标签渲染在箭头中点上）。正确做法：
  先画箭头 `\draw[arr] (A) -- (B);`，再用独立节点放标签：
  `\node[lab, anchor=south] at ($(A)!0.5!(B)$) {标签};`
  anchor 选 `south`（上方）、`north`（下方）或 `west`（右侧），
  让标签白底遮住的是空白区域而非箭头。

- **禁用正交路由 `-|` / `|-` 连接框** — 这两个操作符会让箭头先走一段
  与框边**平行的线**再拐弯，视觉上"贴着框边滑行"。
  短距离连接一律用直线 `(A) -- (B)`；
  长距离跨行连接用显式坐标绕行（见 §4 "回边走道"）。

- **多条回边走道错开** — 从同一节点出发的多条回边（虚线反馈线），
  各自走不同的 x 深度（左侧走道错开 ≥ 2cm）：
  ```latex
  \coordinate (c1) at (-5.5, y1);  % 回边 1 走 x=-5.5
  \coordinate (c2) at (-7.5, y1);  % 回边 2 走 x=-7.5
  \coordinate (c3) at (-9.5, y1);  % 回边 3 走 x=-9.5
  \draw[darr] (n6.west) -- (c1) -- (c1 |- n5.west) -- (n5.west);
  ```
  不写走道坐标的三条回边会在垂直段完全重叠。

- **跨多行的回投走外侧走道** — 回边跨越 3 行以上的节点时，
  不穿中间的框，走最左或最右侧的空白走道再折回：
  ```latex
  \coordinate (corridor) at ($(s12.east)+(2.5,0)$);
  \draw[darr] (s12.east) -- (corridor)
    -- (corridor |- s5.east) -- (s5.east);
  ```

## 4. Common cross-template operations

These work the same across templates; the per-template `edit_contract` lists the
specific parameter/style names to touch.

**Recolor.** Change the palette color *name* in the relevant style or node, never
a hex. "Make it teal" = `otblue` → `otteal` in that style's `fill`/`draw`.

**Switch to the dark palette.** Replace the light `\definecolor` block with the
dark block from `reference/color-palettes/color-palettes.md` — the names are
identical, so the body is unchanged. The dark palette is for **dark
backgrounds**: also set `\pagecolor{otpaper}`
(`\definecolor{otpaper}{HTML}{1E1E1E}`), or the tints render washed-out grey on a
white page.

**Adapt to a venue / column width.** Wrap the whole `tikzpicture` in `\resizebox`
(needs `\usepackage{graphicx}`):
```
\resizebox{\columnwidth}{!}{\begin{tikzpicture} ... \end{tikzpicture}}
```
In a paper use `\columnwidth` (single column) or `\textwidth` (full width); to
test a target in the standalone file give an explicit width
(`\resizebox{8.4cm}{!}{...}`). Common targets: CVPR/ICCV/ACL single column
≈ 8.4cm, full/double width ≈ 17.8cm; NeurIPS/ICML text ≈ 13.9cm. Caveat:
`\resizebox` scales text too — if the figure is far wider than the column, first
reduce content/spacing (the template's spacing parameters) and resize the rest.

**回边走道（feedback loop routing）.** 画从下游指回上游的虚线回边时：
1. 在框的左侧或右侧定义一个走道坐标（`\coordinate`）
2. 路径：源框边缘 → 水平走到走道 → 垂直走到目标行 → 水平折入目标框边缘
3. 走道与最近框边缘的距离 ≥ 1cm
4. 多条回边各用不同深度的走道，间距 ≥ 2cm
5. 标签放在走道旁边（`anchor=east` 或 `anchor=west`），竖排可用 `rotate=90`

## 5. Reference material

- `reference/color-palettes/` — the canonical five-color Okabe-Ito palette (light
  + dark blocks), the single source of truth for colors.
- `reference/annotations/` — how to add callouts, braces, and highlight labels.
- `reference/layout/` — positioning, alignment, and spacing patterns.
  (Covers in-box placement only; **arrow routing avoidance rules live in §3**
  — labels vs lines, flush ports, staggered lanes, outer-lane returns.)
- `docs/DESIGN_GUIDE.md` — global conventions (line width, node naming, metadata).

## 6. Mode B procedure — editing library content in this copy

If the user is adding/changing library content (not just producing a figure for
their paper), verify each edit with the LaTeX toolchain directly — the upstream
repo tooling (`tools/build_catalog.py`, `render_preview.py`, `validate.py`) is
**not shipped in this skill** (excluded during sync):

- compile check: `latexmk -pdf <file>.tex` (or `pdflatex`); if no engine is
  installed, report that instead of guessing — see §2 for the delivery fallback
- regenerate the preview SVG: `dvisvgm --pdf --bbox=preview --no-fonts <file>.pdf -o preview.svg`
  (keep the committed `preview.svg` in sync with its `.tex`)
- a new/edited template needs an `edit_contract` in its `meta.json` (see §4 of
  `docs/DESIGN_GUIDE.md`): every `parameters[].name` and style it declares must
  actually exist in the `.tex` — verify by inspection, since `validate.py` is not
  available here
- `catalog.json` is a snapshot shipped with the library; do not rebuild it inside
  this skill — to regenerate it, go to the upstream OpenTikZ repository
