#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Generate mario-drv-dupliques.html from a Nix Graphviz .dot file.

Usage:
  python generate_mario_drv_html.py
  python generate_mario_drv_html.py mario-drv-graph.dot -o mario-drv-dupliques.html
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict, deque
from pathlib import Path

DEFAULT_DOT = Path.home() / "Downloads" / "mario-drv-graph.dot"
DEFAULT_OUT = Path.home() / "Downloads" / "mario-drv-dupliques.html"
DEFAULT_ROOT = "9xppv8fijaxf857a49a3xknjs06lml9a-mario-1.0.drv"
GCC_ID = "p4im0xqqxp2nf6bnw9fz06dmddbd3xr4-gcc-wrapper-14.3.0.drv"
STDLINUX_ID = "j2c9icqxbadyawrycqylwrj6zcaxmrzf-stdenv-linux.drv"

LABEL_RE = re.compile(r'^"([^"]+)"\s*\[label\s*=\s*"([^"]*)"')
EDGE_RE = re.compile(r'^"([^"]+)"\s*->\s*"([^"]+)"')

RUNTIME_JS = r"""
var NLAYERS = 0;
for (var _i = 0; _i < INST.length; _i++) {
  if (INST[_i][1] + 1 > NLAYERS) NLAYERS = INST[_i][1] + 1;
}
var first = DRV.first;
var labels = DRV.labels;
var ids = DRV.ids;
var copies = DRV.copies;
var inputs = DRV.inputs;

function isDup(drv, depth) { return depth !== first[drv]; }
function shortLab(drv) {
  var lab = labels[drv].replace(/\.drv$/,'');
  return lab.length > 15 ? lab.slice(0, 14) + '...' : lab;
}

var nodeW = 104, nodeH = 20, gapX = 4, gapY = 3, maxCols = 18, left = 56;
var NS = 'http://www.w3.org/2000/svg';

function buildLayout() {
  var by = [];
  for (var d = 0; d < NLAYERS; d++) by[d] = [];
  for (var k = 0; k < INST.length; k++) {
    var p = INST[k];
    by[p[1]].push({ drv: p[0], depth: p[1] });
  }
  for (d = 0; d < NLAYERS; d++) {
    by[d].sort(function (a, b) {
      var da = isDup(a.drv, a.depth) ? 1 : 0;
      var db = isDup(b.drv, b.depth) ? 1 : 0;
      if (da !== db) return da - db;
      return labels[a.drv] < labels[b.drv] ? -1 : labels[a.drv] > labels[b.drv] ? 1 : 0;
    });
  }
  var pos = {};
  var ranks = [];
  var y = 48;
  for (d = 0; d < NLAYERS; d++) {
    var arr = by[d];
    var cols = Math.min(maxCols, Math.max(1, arr.length));
    var rows = Math.ceil(arr.length / cols) || 1;
    var h = 22 + rows * (nodeH + gapY) + 6;
    ranks.push({ d: d, y: y, h: h, n: arr.length, cols: cols, rows: rows });
    for (var i = 0; i < arr.length; i++) {
      var it = arr[i];
      var col = i % cols, row = Math.floor(i / cols);
      pos[it.drv + '@' + it.depth] = {
        x: left + col * (nodeW + gapX),
        y: y + 22 + row * (nodeH + gapY),
        w: nodeW, h: nodeH, drv: it.drv, depth: it.depth
      };
    }
    y += h + 10;
  }
  return { pos: pos, ranks: ranks, width: left + maxCols * (nodeW + gapX) + 28, height: y + 20 };
}

var L = buildLayout();

function nodeFill(p) {
  if (p.drv === ROOT) return { fill: '#1d4ed8', stroke: '#1d4ed8', fg: '#fff' };
  if (isDup(p.drv, p.depth)) return { fill: '#ffedd5', stroke: '#c2410c', fg: '#9a3412' };
  return { fill: '#fff', stroke: '#94a3b8', fg: '#0f172a' };
}

function el(name, attrs) {
  var n = document.createElementNS(NS, name);
  if (attrs) {
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) n.setAttribute(k, attrs[k]);
    }
  }
  return n;
}

function txt(x, y, content, attrs) {
  var t = el('text', attrs);
  t.setAttribute('x', x);
  t.setAttribute('y', y);
  t.textContent = content;
  return t;
}

var stage = document.getElementById('stage');
var svg = el('svg', {
  xmlns: NS,
  viewBox: '0 0 ' + L.width + ' ' + L.height,
  preserveAspectRatio: 'xMinYMin meet'
});
svg.style.fontFamily = 'Segoe UI, system-ui, sans-serif';
svg.style.display = 'block';
var viewport = el('g', { id: 'viewport' });
svg.appendChild(viewport);
viewport.appendChild(el('rect', {
  x: '0', y: '0', width: String(L.width), height: String(L.height), fill: '#f8fafc'
}));
viewport.appendChild(txt(16, 28, NLAYERS + ' layers — chaque boite est une instance .drv', {
  'font-size': '16', 'font-weight': '700', fill: '#0f172a'
}));
viewport.appendChild(txt(16, 44, 'Blanc = premiere apparition · orange = copie · bleu = mario · clic = tout l arbre de dependances', {
  'font-size': '11', fill: '#64748b'
}));

for (var ri = 0; ri < L.ranks.length; ri++) {
  var r = L.ranks[ri];
  viewport.appendChild(el('rect', {
    x: '8', y: String(r.y), width: String(L.width - 16), height: String(r.h),
    rx: '8', fill: '#eef2ff'
  }));
  viewport.appendChild(txt(14, r.y + 15, 'L' + r.d + '  ' + r.n + ' .drv', {
    'font-size': '11', 'font-weight': '700', fill: '#334155'
  }));
}

var edgeG = el('g', { id: 'edges', fill: 'none', stroke: '#1d4ed8', 'stroke-width': '1.4', opacity: '0.8' });
viewport.appendChild(edgeG);

var nodeEls = [];
var keys = Object.keys(L.pos);
for (var ni = 0; ni < keys.length; ni++) {
  var key = keys[ni];
  var p = L.pos[key];
  var c = nodeFill(p);
  var g = el('g', { 'class': 'node', 'data-drv': String(p.drv), 'data-depth': String(p.depth), 'data-key': key });
  g.style.cursor = 'pointer';
  var title = document.createElementNS(NS, 'title');
  title.textContent = labels[p.drv] + '  L' + p.depth + (isDup(p.drv, p.depth) ? ' (copie)' : ' (premiere)');
  g.appendChild(title);
  g.appendChild(el('rect', {
    x: String(p.x), y: String(p.y), width: String(p.w), height: String(p.h),
    rx: '4', fill: c.fill, stroke: c.stroke
  }));
  g.appendChild(txt(p.x + p.w / 2, p.y + p.h / 2 + 1, shortLab(p.drv), {
    'text-anchor': 'middle', 'dominant-baseline': 'middle', 'font-size': '9', fill: c.fg
  }));
  viewport.appendChild(g);
  nodeEls.push(g);
}

stage.innerHTML = '';
stage.appendChild(svg);

var panel = document.getElementById('panel');
var selKey = null;

function edgePath(a, b) {
  var x1 = a.x + a.w / 2, y1 = a.y + a.h;
  var x2 = b.x + b.w / 2, y2 = b.y;
  var mid = (y1 + y2) / 2;
  return 'M ' + x1 + ' ' + y1 + ' C ' + x1 + ' ' + mid + ', ' + x2 + ' ' + mid + ', ' + x2 + ' ' + y2;
}

function collectDeps(drv, depth) {
  var keep = {};
  var stack = [[drv, depth]];
  while (stack.length) {
    var cur = stack.pop();
    var k = cur[0] + '@' + cur[1];
    if (keep[k]) continue;
    keep[k] = true;
    var ins = inputs[cur[0]] || [];
    for (var i = 0; i < ins.length; i++) {
      var nk = ins[i] + '@' + (cur[1] + 1);
      if (L.pos[nk]) stack.push([ins[i], cur[1] + 1]);
    }
  }
  return keep;
}

function drawDepEdges(keep) {
  while (edgeG.firstChild) edgeG.removeChild(edgeG.firstChild);
  var ks = Object.keys(keep);
  for (var i = 0; i < ks.length; i++) {
    var from = L.pos[ks[i]];
    if (!from) continue;
    var ins = inputs[from.drv] || [];
    for (var j = 0; j < ins.length; j++) {
      var tk = ins[j] + '@' + (from.depth + 1);
      if (!keep[tk]) continue;
      var to = L.pos[tk];
      if (to) edgeG.appendChild(el('path', { d: edgePath(from, to) }));
    }
  }
}

function clearHot() {
  selKey = null;
  for (var i = 0; i < nodeEls.length; i++) {
    nodeEls[i].classList.remove('dim', 'hot', 'dep');
  }
  while (edgeG.firstChild) edgeG.removeChild(edgeG.firstChild);
  panel.style.display = 'none';
}

function highlight(drv, depth) {
  var key = drv + '@' + depth;
  selKey = key;
  var keep = collectDeps(drv, depth);
  var nDep = Object.keys(keep).length;
  for (var i = 0; i < nodeEls.length; i++) {
    var k = nodeEls[i].getAttribute('data-key');
    nodeEls[i].classList.toggle('dim', !keep[k]);
    nodeEls[i].classList.toggle('hot', k === key);
    nodeEls[i].classList.toggle('dep', !!keep[k] && k !== key);
  }
  drawDepEdges(keep);
  document.getElementById('pTitle').textContent = labels[drv];
  document.getElementById('pMeta').textContent =
    'L' + depth + (isDup(drv, depth) ? ' (copie)' : ' (premiere)') +
    '  |  ' + nDep + ' noeuds dans l arbre de dependances' +
    '  |  ' + (inputs[drv] || []).length + ' entrees directes';
  document.getElementById('pId').textContent = ids[drv];
  panel.style.display = 'block';
}

for (var ei = 0; ei < nodeEls.length; ei++) {
  (function (g) {
    g.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (didDrag) return;
      var drv = +g.getAttribute('data-drv');
      var depth = +g.getAttribute('data-depth');
      var key = drv + '@' + depth;
      if (selKey === key) clearHot(); else highlight(drv, depth);
    });
  })(nodeEls[ei]);
}
svg.addEventListener('click', function () {
  if (!didDrag) clearHot();
});

document.getElementById('q').addEventListener('input', function (ev) {
  var q = ev.target.value.trim().toLowerCase();
  if (!q) { clearHot(); return; }
  for (var i = 0; i < labels.length; i++) {
    if (labels[i].toLowerCase().indexOf(q) >= 0 || ids[i].toLowerCase().indexOf(q) >= 0) {
      highlight(i, first[i]);
      return;
    }
  }
});

var scale = 1, dragging = false, didDrag = false, lx = 0, ly = 0, sx = 0, sy = 0;

function applySize() {
  svg.setAttribute('width', String(Math.round(L.width * scale)));
  svg.setAttribute('height', String(Math.round(L.height * scale)));
}

function fitWidth() {
  var w = stage.clientWidth || 900;
  scale = Math.max(0.12, Math.min(2.2, (w - 24) / L.width));
  applySize();
  stage.scrollLeft = 0;
  stage.scrollTop = 0;
}

function zoomAt(clientX, clientY, factor) {
  var rect = stage.getBoundingClientRect();
  var cx = clientX - rect.left;
  var cy = clientY - rect.top;
  var wx = (stage.scrollLeft + cx) / scale;
  var wy = (stage.scrollTop + cy) / scale;
  scale = Math.min(4, Math.max(0.12, scale * factor));
  applySize();
  stage.scrollLeft = wx * scale - cx;
  stage.scrollTop = wy * scale - cy;
}

var jumpEl = document.getElementById('jump');
if (jumpEl) {
  jumpEl.min = '0';
  jumpEl.max = String(Math.max(0, NLAYERS - 1));
}

fitWidth();

stage.addEventListener('wheel', function (ev) {
  if (ev.ctrlKey || ev.metaKey) {
    ev.preventDefault();
    zoomAt(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.12 : 0.89);
  }
}, { passive: false });

stage.addEventListener('mousedown', function (ev) {
  if (ev.button !== 0) return;
  if (!svg.contains(ev.target)) return;
  ev.preventDefault();
  dragging = true;
  didDrag = false;
  lx = ev.clientX;
  ly = ev.clientY;
  sx = stage.scrollLeft;
  sy = stage.scrollTop;
  stage.classList.add('drag');
});
window.addEventListener('mouseup', function () {
  dragging = false;
  stage.classList.remove('drag');
});
window.addEventListener('mousemove', function (ev) {
  if (!dragging) return;
  var dx = ev.clientX - lx;
  var dy = ev.clientY - ly;
  if (Math.abs(dx) > 4 || Math.abs(dy) > 4) didDrag = true;
  if (!didDrag) return;
  stage.scrollLeft = sx - dx;
  stage.scrollTop = sy - dy;
});

function gotoLayer(d) {
  var r = L.ranks[d];
  document.getElementById('jumpLab').textContent = 'L' + d;
  stage.scrollTop = Math.max(0, r.y * scale - 16);
}

document.getElementById('jump').addEventListener('input', function (ev) {
  gotoLayer(+ev.target.value);
});

document.getElementById('reset').onclick = function () {
  fitWidth();
  document.getElementById('q').value = '';
  document.getElementById('jump').value = '0';
  document.getElementById('jumpLab').textContent = 'L0';
  clearHot();
};

var zoomInBtn = document.getElementById('zoomIn');
var zoomOutBtn = document.getElementById('zoomOut');
if (zoomInBtn) zoomInBtn.onclick = function () {
  zoomAt(stage.getBoundingClientRect().left + stage.clientWidth / 2,
    stage.getBoundingClientRect().top + stage.clientHeight / 2, 1.2);
};
if (zoomOutBtn) zoomOutBtn.onclick = function () {
  zoomAt(stage.getBoundingClientRect().left + stage.clientWidth / 2,
    stage.getBoundingClientRect().top + stage.clientHeight / 2, 1 / 1.2);
};

function download(name, blob) {
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 1500);
}
document.getElementById('dlSvg').onclick = function () {
  var copy = svg.cloneNode(true);
  copy.removeAttribute('style');
  download('mario-drv-dupliques.svg', new Blob(
    ['<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(copy)],
    { type: 'image/svg+xml' }
  ));
};
""".strip()


def parse_dot(dot_path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    labels: dict[str, str] = {}
    text = dot_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = LABEL_RE.match(line)
        if m:
            nid = m.group(1)
            if nid.endswith(".drv"):
                labels[nid] = m.group(2)

    inputs: dict[str, list[str]] = {nid: [] for nid in labels}
    for line in text.splitlines():
        m = EDGE_RE.match(line)
        if not m:
            continue
        src, dst = m.group(1), m.group(2)
        if src in labels and dst in labels:
            inputs[dst].append(src)
    return labels, inputs


def unfold(
    labels: dict[str, str],
    inputs: dict[str, list[str]],
    root_id: str,
    max_depth: int,
) -> tuple[list[str], list[list[int]], list[int], list[int], list[list[int]]]:
    if root_id not in labels:
        raise SystemExit(f"Root not found in DOT: {root_id}")

    drv_ids = sorted(labels)
    index_of = {nid: i for i, nid in enumerate(drv_ids)}

    seen: set[tuple[str, int]] = {(root_id, 0)}
    queue: deque[tuple[str, int]] = deque([(root_id, 0)])
    inst: list[list[int]] = []
    depths: dict[str, list[int]] = defaultdict(list)

    while queue:
        nid, depth = queue.popleft()
        inst.append([index_of[nid], depth])
        depths[nid].append(depth)
        if depth >= max_depth:
            continue
        for inp in inputs[nid]:
            key = (inp, depth + 1)
            if key not in seen:
                seen.add(key)
                queue.append(key)

    first = []
    copies = []
    for nid in drv_ids:
        ds = sorted(depths.get(nid, []))
        first.append(ds[0] if ds else -1)
        copies.append(len(ds))

    input_idx = [[index_of[x] for x in inputs[nid]] for nid in drv_ids]
    return drv_ids, inst, first, copies, input_idx


def build_html(
    drv_ids: list[str],
    labels: dict[str, str],
    inst: list[list[int]],
    first: list[int],
    copies: list[int],
    input_idx: list[list[int]],
    root_id: str,
) -> str:
    n_layers = 1 + max(p[1] for p in inst)
    index_of = {nid: i for i, nid in enumerate(drv_ids)}
    payload = {
        "labels": [labels[nid] for nid in drv_ids],
        "ids": drv_ids,
        "first": first,
        "copies": copies,
        "inputs": input_idx,
    }
    data_js = (
        "const DRV = "
        + json.dumps(payload, separators=(",", ":"))
        + ";\nconst INST = "
        + json.dumps(inst, separators=(",", ":"))
        + f";\nconst GCC = {index_of.get(GCC_ID, -1)};"
        + f"\nconst STDLINUX = {index_of.get(STDLINUX_ID, -1)};"
        + f"\nconst ROOT = {index_of[root_id]};"
    )
    last = n_layers - 1
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Graphe .drv duplique - {n_layers} layers</title>
<style>
  html, body {{ margin: 0; height: 100%; overflow: hidden; font-family: Segoe UI, system-ui, sans-serif; background: #e8eef6; color: #0f172a; }}
  body {{ display: flex; flex-direction: column; }}
  header {{ flex: 0 0 auto; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; background: #fff; border-bottom: 1px solid #dbe3ee; }}
  h1 {{ font-size: 15px; margin: 0; }}
  .meta {{ color: #64748b; font-size: 12px; }}
  input[type=search] {{ height: 32px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 0 10px; min-width: 220px; }}
  input[type=range] {{ width: 180px; }}
  button {{ height: 32px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 0 12px; background: #0f172a; color: #fff; cursor: pointer; }}
  button.secondary {{ background: #fff; color: #0f172a; }}
  .note {{ flex: 0 0 auto; padding: 8px 14px; font-size: 12px; color: #334155; background: #fff7ed; border-bottom: 1px solid #fed7aa; }}
  #err {{ flex: 0 0 auto; display: none; padding: 16px; color: #b91c1c; font-size: 14px; white-space: pre-wrap; }}
  #stage {{ flex: 1 1 auto; min-height: 0; overflow: auto; cursor: grab; background: #f8fafc; }}
  #stage.drag {{ cursor: grabbing; user-select: none; }}
  svg {{ display: block; }}
  .dim rect {{ opacity: 0.10; }}
  .dim text {{ opacity: 0.10; }}
  .hot rect {{ stroke: #1d4ed8; stroke-width: 2.5; }}
  .dep rect {{ stroke: #2563eb; stroke-width: 1.6; }}
  #panel {{ position: absolute; right: 12px; top: 108px; width: 300px; background: #fff; border: 1px solid #dbe3ee; border-radius: 8px; padding: 12px; font-size: 12px; display: none; z-index: 2; }}
  #panel h2 {{ margin: 0 0 6px; font-size: 13px; }}
  #panel code {{ font-size: 10px; color: #475569; word-break: break-all; }}
  label.jump {{ font-size: 12px; color: #334155; display: flex; align-items: center; gap: 8px; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Graphe .drv duplique - {n_layers} layers (0 a {last})</h1>
    <div class="meta">{len(inst)} instances - {len(drv_ids)} hashes uniques - molette = defiler - Ctrl+molette = zoom - clic = arbre de dependances</div>
  </div>
  <input id="q" type="search" placeholder="Chercher un .drv"/>
  <label class="jump">Layer <input id="jump" type="range" min="0" max="{last}" value="0"/> <b id="jumpLab">L0</b></label>
  <button type="button" class="secondary" id="zoomOut">-</button>
  <button type="button" class="secondary" id="zoomIn">+</button>
  <button type="button" id="dlSvg">Telecharger SVG</button>
  <button type="button" class="secondary" id="reset">Reinitialiser</button>
</header>
<div class="note">Source: .dot. Clique un noeud pour surligner toutes ses dependances. Orange = copie d un hash deja vu. Mario reste en bleu. Molette = defiler jusqu au dernier layer.</div>
<div id="err"></div>
<div id="stage"></div>
<aside id="panel">
  <h2 id="pTitle"></h2>
  <div id="pMeta"></div>
  <p><code id="pId"></code></p>
</aside>
<script>
{data_js}
try {{
{RUNTIME_JS}
}} catch (e) {{
var box = document.getElementById('err');
box.style.display = 'block';
box.textContent = 'Erreur: ' + (e && e.message ? e.message : e);
console.error(e);
}}
</script>
</body>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Generate duplicated .drv HTML graph from a Nix .dot file")
    p.add_argument("dot", nargs="?", type=Path, default=DEFAULT_DOT, help="Path to mario-drv-graph.dot")
    p.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help="Output HTML path")
    p.add_argument("--root", default=DEFAULT_ROOT, help="Root .drv store path")
    p.add_argument("--max-depth", type=int, default=80)
    args = p.parse_args()

    if not args.dot.is_file():
        raise SystemExit(f"Missing DOT file: {args.dot}")

    labels, inputs = parse_dot(args.dot)
    drv_ids, inst, first, copies, input_idx = unfold(labels, inputs, args.root, args.max_depth)
    html = build_html(drv_ids, labels, inst, first, copies, input_idx, args.root)
    args.out.write_text(html, encoding="utf-8")
    n_layers = 1 + max(x[1] for x in inst)
    print(f"dot={args.dot}")
    print(f"html={args.out}")
    print(f"unique={len(drv_ids)} instances={len(inst)} layers={n_layers}")
    print(f"bytes={args.out.stat().st_size}")


if __name__ == "__main__":
    main()
