#!/usr/bin/env python3
"""Make the pictures of CVA6Flow's Help, in docs/help/.

Each picture is a screenshot of the page itself, made headless in Chromium
with the daxpy sample from tests/ loaded, in the light and the dark theme as
the page draws them, labelled and cut to what its Help section explains. The
Help shows docs/help/CVA6Flow_<name>_<theme>.png and leaves out a picture
that is missing. Make them again after a change to the page, its themes or
the sample, so the Help shows the page as it is.

    python3 scripts/make_CVA6Flow_help.py                  # every picture
    python3 scripts/make_CVA6Flow_help.py --only load,pins # some of them
    python3 scripts/make_CVA6Flow_help.py --theme light -n # list, write none

It needs Playwright for Python and its Chromium, which nothing else here does:

    pip install playwright
    python3 -m playwright install chromium
"""
import argparse
import base64
import contextlib
import functools
import http.server
import os
import re
import sys
import tempfile
import threading
import time


# The viewer the pictures show, its page and the sample they are made from,
# by its label in tests/samples.js.
VIEWER = "CVA6Flow"
PAGE = "CVA6Flow.html"
SAMPLE = "daxpy"


# SHARED BEGIN py-repo-root

# Needs: os


def repo_root():
    """The nearest folder above this script holding a .git, so a moved tree
    needs no parent count fixed. Without one, as in a release archive, the
    parent of the script's folder, which is the documented layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = here
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.dirname(here)
        path = parent

# SHARED END py-repo-root


# SHARED BEGIN py-human-size

# Needs: none


def human(size):
    """A size in bytes as whole B, or as KiB, MiB or GiB with one decimal."""
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"

# SHARED END py-human-size


# SHARED BEGIN py-page-limits

# Needs: os, re, PAGE, py-repo-root

# The declarations of the page's limits block, a product of integers each.
LIMIT_PATTERN = r"\bconst {name} = ([\d_ *]+);"


def page_limit(text, name):
    """The value of one constant of the page's limits block, or None."""
    match = re.search(LIMIT_PATTERN.format(name=name), text)
    if not match:
        return None
    value = 1
    for factor in match.group(1).split("*"):
        value *= int(factor.strip())
    return value


def page_root():
    """The folder holding PAGE: the repository root in a clone, or the
    viewer's folder beside scripts/ in a container image, where the root
    holds gem5 or CVA6. The repository root when neither has the page."""
    root = repo_root()
    viewer = os.path.splitext(PAGE)[0]
    for folder in (root, os.path.join(root, viewer)):
        if os.path.isfile(os.path.join(folder, PAGE)):
            return folder
    return root


def read_page_limits():
    """(MAX_JSON_BYTES, MAX_STREAM_INSTRUCTIONS) from PAGE in page_root(),
    or None when the page or either constant is missing."""
    path = os.path.join(page_root(), PAGE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    limits = (page_limit(text, "MAX_JSON_BYTES"),
              page_limit(text, "MAX_STREAM_INSTRUCTIONS"))
    return None if None in limits else limits

# SHARED END py-page-limits


# SHARED BEGIN py-help-capture

# Needs: base64, contextlib, functools, http.server, os, re, threading, time,
# PAGE, WINDOW, page_root, and a GEOMETRY_JS defining window.__shot.rect

# Where the Help looks for its pictures, beside the page, and the scale it
# shows them at, which the page declares and this script reads.
SHOT_DIR = os.path.join("docs", "help")
SCALE_PATTERN = r"\bconst HELP_SHOT_SCALE = ([\d.]+);"
SHOT_PATTERN = r'<img data-shot="([a-z-]+)"'

# A picture past this is reported, since the Help loads them all at once.
SHOT_BUDGET = 150 * 1024

# When the JSONs of the folder the Load JSON picture lists were written, as
# year, month, day, hour, minute and second, in local time.
DEMO_TIME = (2026, 10, 12, 19, 2, 0)

# The labels drawn over a picture, an overlay of the page, so they take its
# font. They are near black on light and near white on dark, a neutral no
# stage colour takes, with a halo of the ground so they read on any cell.
MARKS_JS = r"""
([marks, clip]) => {
  document.getElementById('help-shot-marks')?.remove();
  const light = document.documentElement.dataset.theme === 'light';
  const ink = light ? '#15181d' : '#f2efe8';
  const onInk = light ? '#ffffff' : '#15161a';
  const halo = light ? 'rgba(255,255,255,0.95)' : 'rgba(14,14,16,0.92)';
  const token = name => getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.id = 'help-shot-marks';
  svg.setAttribute('width', String(innerWidth));
  svg.setAttribute('height', String(innerHeight));
  svg.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;'
    + 'pointer-events:none;overflow:visible';
  document.body.appendChild(svg);
  const add = (name, attrs, parent = svg) => {
    const node = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attrs)) {
      node.setAttribute(key, String(value));
    }
    parent.appendChild(node);
    return node;
  };
  const font = getComputedStyle(document.body).fontFamily;
  for (const m of marks.filter(m => m.band)) {
    const [x, y, w, h] = m.band;
    add('rect', { x, y, width: w, height: h, fill: token(m.ground) });
  }
  for (const m of marks.filter(m => m.box)) {
    const [x, y, w, h] = m.box;
    const shape = { x, y, width: w, height: h, rx: 5, fill: 'none' };
    add('rect', { ...shape, stroke: halo, 'stroke-width': 4.5 });
    add('rect', { ...shape, stroke: ink, 'stroke-width': 2 });
  }
  for (const m of marks.filter(m => m.pointer)) {
    const [x, y] = m.pointer;
    add('path', { d: 'M0 0 L0 17 L4.4 13.2 L7.4 20 L10.2 18.8 L7.3 12.1 '
      + 'L12.8 12.1 Z', transform: `translate(${x} ${y})`, fill: '#ffffff',
      stroke: '#111111', 'stroke-width': 1.3, 'stroke-linejoin': 'round' });
  }
  const leaders = add('g', {});
  const placed = [];
  for (const m of marks.filter(m => m.pill)) {
    const size = m.size || 12, pad = 0.75 * size, h = 1.85 * size, gap = 6;
    const group = add('g', {});
    const label = add('text', { x: 0, y: 0, 'font-family': font,
      'font-size': size, 'font-weight': 600, fill: onInk,
      'dominant-baseline': 'central' }, group);
    label.textContent = m.pill;
    const w = label.getComputedTextLength() + 2 * pad;
    const top = m.y - h / 2;
    const inside = x => Math.max(clip[0] + 4,
      Math.min(clip[0] + clip[2] - 4 - w, x));
    let left = inside(m.anchor === 'start' ? m.x
      : m.anchor === 'end' ? m.x - w : m.x - w / 2);
    for (let pass = 0; pass < 12; pass++) {
      const hit = placed.find(p => left < p.x + p.w + gap
        && left + w + gap > p.x && top < p.y + p.h && top + h > p.y);
      if (!hit) break;
      left = inside(left + w / 2 >= hit.x + hit.w / 2 ? hit.x + hit.w + gap
        : hit.x - w - gap);
    }
    placed.push({ x: left, y: top, w, h });
    const back = document.createElementNS(ns, 'rect');
    for (const [key, value] of Object.entries({ x: left, y: top, width: w,
      height: h, rx: h / 2, fill: ink, stroke: halo, 'stroke-width': 1.5 })) {
      back.setAttribute(key, String(value));
    }
    group.insertBefore(back, label);
    label.setAttribute('x', String(left + pad));
    label.setAttribute('y', String(m.y + 0.5));
    for (const [x2, y2] of m.to || []) {
      const [x1, y1] = [left + w / 2, m.y];
      const angle = Math.atan2(y2 - y1, x2 - x1), head = 8, half = 4.5;
      const [bx, by] = [x2 - head * Math.cos(angle),
        y2 - head * Math.sin(angle)];
      const tip = `${x2},${y2} ${bx - half * Math.sin(angle)},`
        + `${by + half * Math.cos(angle)} ${bx + half * Math.sin(angle)},`
        + `${by - half * Math.cos(angle)}`;
      const line = { x1, y1, x2: bx, y2: by };
      add('line', { ...line, stroke: halo, 'stroke-width': 4.5,
        'stroke-linecap': 'round' }, leaders);
      add('polygon', { points: tip, fill: halo, stroke: halo,
        'stroke-width': 3, 'stroke-linejoin': 'round' }, leaders);
      add('line', { ...line, stroke: ink, 'stroke-width': 1.75 }, leaders);
      add('polygon', { points: tip, fill: ink }, leaders);
    }
  }
}
"""

# A screenshot cut to at most 256 colours and written as an indexed PNG,
# which keeps the page's flat fills exact at a third of the size. The page's
# browser does the work, so nothing else needs installing.
PALETTE_JS = r"""
async (b64) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const image = await createImageBitmap(new Blob([bytes]));
  const canvas = document.createElement('canvas');
  canvas.width = image.width;
  canvas.height = image.height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(image, 0, 0);
  const { data, width, height } = ctx.getImageData(0, 0, image.width,
    image.height);
  const counts = new Map();
  for (let i = 0; i < width * height; i++) {
    const key = (data[4 * i] << 16) | (data[4 * i + 1] << 8)
      | data[4 * i + 2];
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const rgb = k => [k >> 16, (k >> 8) & 255, k & 255];
  let palette = [...counts.keys()].map(rgb);
  if (counts.size > 256) {
    const spread = list => {
      let best = [-1, 0];
      for (const shift of [16, 8, 0]) {
        let lo = 255, hi = 0;
        for (const [k] of list) {
          const v = (k >> shift) & 255;
          lo = Math.min(lo, v);
          hi = Math.max(hi, v);
        }
        if (hi - lo > best[0]) best = [hi - lo, shift];
      }
      return best;
    };
    const boxes = [[...counts.entries()]];
    while (boxes.length < 256) {
      let pick = -1, widest = 0;
      boxes.forEach((list, i) => {
        if (list.length < 2) return;
        const weight = list.reduce((s, [, c]) => s + c, 0);
        const score = spread(list)[0] * Math.sqrt(weight);
        if (score > widest) [pick, widest] = [i, score];
      });
      if (pick < 0) break;
      const list = boxes[pick];
      const shift = spread(list)[1];
      list.sort((a, b) => ((a[0] >> shift) & 255) - ((b[0] >> shift) & 255));
      const total = list.reduce((s, [, c]) => s + c, 0);
      let run = 0, cut = 1;
      for (; cut < list.length - 1; cut++) {
        run += list[cut - 1][1];
        if (run >= total / 2) break;
      }
      boxes.splice(pick, 1, list.slice(0, cut), list.slice(cut));
    }
    palette = boxes.map(list => {
      let r = 0, g = 0, b = 0, w = 0, top = list[0];
      for (const entry of list) {
        const [k, c] = entry;
        const [cr, cg, cb] = rgb(k);
        r += cr * c;
        g += cg * c;
        b += cb * c;
        w += c;
        if (c > top[1]) top = entry;
      }
      return top[1] * 2 > w ? rgb(top[0])
        : [Math.round(r / w), Math.round(g / w), Math.round(b / w)];
    });
  }
  const nearest = new Map();
  const indexOf = key => {
    let found = nearest.get(key);
    if (found !== undefined) return found;
    const [r, g, b] = rgb(key);
    let best = Infinity;
    palette.forEach(([pr, pg, pb], i) => {
      const d = 2 * (pr - r) ** 2 + 4 * (pg - g) ** 2 + 3 * (pb - b) ** 2;
      if (d < best) [best, found] = [d, i];
    });
    nearest.set(key, found);
    return found;
  };
  const lines = new Uint8Array(height * (width + 1));
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = 4 * (y * width + x);
      lines[y * (width + 1) + 1 + x] = indexOf((data[i] << 16)
        | (data[i + 1] << 8) | data[i + 2]);
    }
  }
  const zipped = new Uint8Array(await new Response(new Blob([lines])
    .stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer());
  const table = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const chunk = (type, body) => {
    const out = new Uint8Array(12 + body.length);
    const view = new DataView(out.buffer);
    view.setUint32(0, body.length);
    for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
    out.set(body, 8);
    let c = 0xffffffff;
    for (let i = 4; i < 8 + body.length; i++) {
      c = table[(c ^ out[i]) & 255] ^ (c >>> 8);
    }
    view.setUint32(8 + body.length, (c ^ 0xffffffff) >>> 0);
    return out;
  };
  const head = new Uint8Array(13);
  new DataView(head.buffer).setUint32(0, width);
  new DataView(head.buffer).setUint32(4, height);
  head.set([8, 3, 0, 0, 0], 8);
  const parts = [Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', head), chunk('PLTE', Uint8Array.from(palette.flat())),
    chunk('IDAT', zipped), chunk('IEND', new Uint8Array(0))];
  const png = new Uint8Array(parts.reduce((s, p) => s + p.length, 0));
  let at = 0;
  for (const part of parts) {
    png.set(part, at);
    at += part.length;
  }
  let text = '';
  for (let i = 0; i < png.length; i += 32768) {
    text += String.fromCharCode(...png.subarray(i, i + 32768));
  }
  return btoa(text);
}
"""


def read_page():
    with open(os.path.join(page_root(), PAGE), encoding="utf-8") as f:
        return f.read()


def help_scale(text):
    """The scale the Help shows its pictures at, or None."""
    match = re.search(SCALE_PATTERN, text)
    return float(match.group(1)) if match else None


def help_shot_names(text):
    """The pictures the Help shows, in its order."""
    return list(dict.fromkeys(re.findall(SHOT_PATTERN, text)))


@contextlib.contextmanager
def serve(root):
    """The page's folder over HTTP on a free local port, as GitHub Pages
    serves it, for as long as the block runs."""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = functools.partial(Quiet, directory=root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/{PAGE}"
    finally:
        server.shutdown()


def pill(text, x, y, to=None, anchor="middle", size=12):
    """A label centred on x, or starting or ending there, with an arrow to
    the point to, or to each point of a list of them. Labels that would
    overlap move apart along their line, inside the picture."""
    points = [] if to is None else [to] if isinstance(to[0], (int, float)) \
        else list(to)
    return {"pill": text, "x": x, "y": y, "to": points, "anchor": anchor,
            "size": size}


def box(x, y, w, h):
    return {"box": [x, y, w, h]}


def band(x, y, w, h, ground="--bg2"):
    """A strip in one of the page's grounds, to hold labels."""
    return {"band": [x, y, w, h], "ground": ground}


def pointer(x, y):
    """The pointer's arrow, which a headless screenshot leaves out."""
    return {"pointer": [x, y]}


def pill_width(text, size=12):
    """About how wide a label is drawn, for sizing a picture to hold it."""
    return 0.61 * size * len(text) + 1.5 * size


def lay_out(items, lanes, target_y, bounds, gap=8):
    """Labels for points along one edge, as (text, x) pairs, each in the
    lane and at the place nearest its point, between the bounds, where it
    overlaps no label and its arrow up to (x, target_y) runs behind none.
    A label strays from its point only as far as it must."""
    placed, marks = [], []
    for text, x in sorted(items, key=lambda item: item[1]):
        half = pill_width(text) / 2
        tries = [x] + [right + gap + half for _, right, _ in placed] \
            + [left - gap - half for left, _, _ in placed]
        best = None
        for lane, y in enumerate(lanes):
            for centre in tries:
                centre = max(bounds[0] + half, min(bounds[1] - half, centre))
                if any(other == lane and centre - half - gap < right
                       and centre + half + gap > left
                       for left, right, other in placed):
                    continue
                if any(other < lane and left - 2 < x + (centre - x)
                       * (lanes[other] - target_y) / (y - target_y)
                       < right + 2 for left, right, other in placed):
                    continue
                stray = abs(centre - x)
                cost = stray + 40 * lane + (1000 if stray > half + 40 else 0)
                if best is None or cost < best[0]:
                    best = (cost, lane, centre)
        _, lane, centre = best or (0, 0, x)
        placed.append((centre - half, centre + half, lane))
        marks.append(pill(text, centre, lanes[lane], to=(x, target_y)))
    return marks


def stagger(items, lanes, target_y, gap=8):
    """Labels for close points along one edge, as (text, x) pairs. Each
    takes the line and the place nearest its point where it overlaps no
    label, its arrow runs behind no label and it hides no arrow drawn
    before it. Returns the labels and the right end of the widest."""
    placed, marks = [], []

    def crossing(x, centre, lane, at):
        """Where an arrow from a label in lane to x crosses line at."""
        return x + (centre - x) * (lanes[at] - target_y) \
            / (lanes[lane] - target_y)

    for text, x in sorted(items, key=lambda item: item[1]):
        half = pill_width(text) / 2
        tries = {x}
        for left, right, lane, tx, centre in placed:
            tries.update((right + gap + half, left - gap - half))
            for at in range(len(lanes)):
                if at < lane:
                    over = crossing(tx, centre, lane, at)
                    tries.update((over + half + 4, over - half - 4))
        best = None
        for lane in range(len(lanes)):
            for centre in tries:
                clash = any(other == lane and centre - half - gap < right
                            and centre + half + gap > left
                            for left, right, other, _, _ in placed)
                behind = any(other < lane and left - 2 < crossing(
                    x, centre, lane, other) < right + 2
                    for left, right, other, _, _ in placed)
                hides = any(other > lane and centre - half - 2 < crossing(
                    tx, middle, other, lane) < centre + half + 2
                    for _, _, other, tx, middle in placed)
                if clash or behind or hides:
                    continue
                cost = abs(centre - x) + 30 * lane
                if best is None or cost < best[0]:
                    best = (cost, lane, centre)
        _, lane, centre = best or (0, len(lanes) - 1, x)
        placed.append((centre - half, centre + half, lane, x, centre))
        marks.append(pill(text, centre, lanes[lane], to=(x, target_y)))
    return marks, max(right for _, right, _, _, _ in placed)


def js(page, source, arg=None):
    return page.evaluate(source, arg)


def rect(page, selector):
    """An element's box in the window, as (x, y, w, h)."""
    found = js(page, "s => window.__shot.rect(s)", selector)
    if found is None:
        raise RuntimeError(f"{selector} is not on the page")
    return found["x"], found["y"], found["w"], found["h"]


def union(*boxes):
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return x0, y0, x1 - x0, y1 - y0


def grow(clip, left, top=None, right=None, bottom=None):
    """clip widened by a margin each side, kept inside the window."""
    top = left if top is None else top
    right = left if right is None else right
    bottom = top if bottom is None else bottom
    x, y, w, h = clip
    x0, y0 = max(0, x - left), max(0, y - top)
    x1 = min(WINDOW[0], x + w + right)
    y1 = min(WINDOW[1], y + h + bottom)
    return x0, y0, x1 - x0, y1 - y0


def settle(page, ms=250):
    """Two frames and a little more, so a redraw has landed."""
    js(page, "() => new Promise(r => requestAnimationFrame(() => "
             "requestAnimationFrame(r)))")
    page.wait_for_timeout(ms)


def capture(page, clip, marks):
    """The window cut to clip, with marks drawn over it, as PNG bytes."""
    clip = [round(v) for v in clip]
    js(page, MARKS_JS, [marks, clip])
    x, y, w, h = clip
    png = page.screenshot(clip={"x": x, "y": y, "width": w, "height": h},
                          animations="disabled")
    js(page, "() => document.getElementById('help-shot-marks')?.remove()")
    return png


def pack(page, png):
    """png as an indexed PNG, or as it was when that is no smaller."""
    packed = base64.b64decode(js(page, PALETTE_JS,
                                 base64.b64encode(png).decode("ascii")))
    return packed if len(packed) < len(png) else png


def demo_folder(where, runs):
    """A folder laid out as calibration runs lay out their JSONs, each its
    size on disk but empty, since the panel reads nothing before a click.
    Their dates are fixed, so making the pictures again changes none."""
    root = os.path.join(where, "tests")
    start = time.mktime(DEMO_TIME + (0, 0, -1))
    for folder, programs in runs.items():
        os.makedirs(os.path.join(root, folder), exist_ok=True)
        for n, (program, mib) in enumerate(programs):
            path = os.path.join(root, folder, f"{program}.json")
            with open(path, "wb") as f:
                f.truncate(int(mib * 1024 * 1024))
            os.utime(path, (start + 41 * n, start + 41 * n))
    return root


# SHARED END py-help-capture


# SHARED BEGIN py-help-shots

# Needs: SAMPLE, DEMO_LAST, STAGE_*, TOOLTIP_PICK, EXECUTE_KINDS, BRANCH_*,
# ARROW*, place_overview, overview_extra, py-help-capture

# The window every picture is cut from, and the smaller one the overview
# takes, so it still reads at the Help's width. Then how long the sample may
# take to load, and the label column's width in the diagram pictures.
WINDOW = (1280, 760)
OVERVIEW_WINDOW = (1040, 630)
LOAD_TIMEOUT_MS = 120_000
LABEL_SHOT_W = 240

# Helpers the shots call in the page: boxes of elements, rows and cells in
# window coordinates, from the page's own geometry, a row finder taking a
# test as source, and a way to put a row and a cycle where a shot wants.
GEOMETRY_JS = r"""
() => {
  const box = el => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  };
  const view = () => box(document.getElementById('timeline-scroll'));
  window.__shot = {
    rect: selector => box(document.querySelector(selector)),
    view,
    rowY: row => view().y + (row - scrollY) * rowHeight(),
    cycleX: cycle => view().x + (cycle - firstVisibleCycle()) * cellW,
    cells: row => cellsOf(rows[row]).map(cell => ({
      kind: cell.kind, c0: cell.c0, c1: cell.c1,
      x0: window.__shot.cycleX(cell.c0), x1: window.__shot.cycleX(cell.c1),
    })),
    find: (source, from = 0) => {
      const test = (0, eval)(source);
      for (let row = Math.max(0, from); row < rows.length; row++) {
        if (test(rows[row], row)) return row;
      }
      return -1;
    },
    place: (row, cycle, zoom, share, above) => {
      setZoomTo(String(zoom));
      const visible = viewportWidth() / cellW;
      setScrollY(Math.max(0, row - above));
      setScrollX(cycle - extent.renderLo - visible * share);
    },
  };
}
"""


def open_viewer(browser, url, theme, scale):
    """A fresh page of the viewer in theme, with the sample loaded and Main
    Code shown, and the errors it raises from then on."""
    context = browser.new_context(
        viewport={"width": WINDOW[0], "height": WINDOW[1]},
        device_scale_factor=scale)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(url)
    page.wait_for_function("() => typeof applyTheme === 'function' "
                           "&& document.readyState === 'complete'")
    js(page, """theme => {
      const select = document.getElementById('theme-select');
      select.value = theme;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }""", theme)
    page.wait_for_function("""label => [...document.querySelectorAll(
      '#sample-select option')].some(o => o.textContent === label)""",
                           arg=SAMPLE, timeout=LOAD_TIMEOUT_MS)
    js(page, """label => {
      const select = document.getElementById('sample-select');
      const option = [...select.options].find(o => o.textContent === label);
      select.value = option.value;
      document.getElementById('sample-load-button').click();
    }""", SAMPLE)
    page.wait_for_function(
        "() => records.length > 0 && document.getElementById("
        "'metric-cycles').textContent.trim() !== '-'",
        timeout=LOAD_TIMEOUT_MS)
    js(page, GEOMETRY_JS)
    page.mouse.move(WINDOW[0] - 2, 2)
    js(page, """() => {
      document.getElementById('main-code-button').click();
      document.activeElement.blur();
    }""")
    settle(page)
    return context, page, errors


def row_y(page, row):
    return js(page, "row => window.__shot.rowY(row)", row)


def row_h(page):
    return js(page, "() => rowHeight()")


def cells(page, row):
    return js(page, "row => window.__shot.cells(row)", row)


def cell_of(page, row, kinds):
    """The first cell of row whose kind is among kinds."""
    found = next((c for c in cells(page, row) if c["kind"] in kinds), None)
    if found is None:
        raise RuntimeError(f"row {row} has no cell of {', '.join(kinds)}")
    return found


def find(page, source, start=0):
    """The first row from start that the JavaScript test source passes, a
    function of the record and its row."""
    row = js(page, "([s, f]) => window.__shot.find(s, f)", [source, start])
    if row < 0:
        raise RuntimeError(f"no row passes {source}")
    return row


def first_cycle(page, row):
    return js(page, "row => Math.min(...cellsOf(rows[row]).map(c => c.c0))",
              row)


def place(page, row, cycle, zoom, share=0.3, above=2):
    """The view at zoom, row above rows from the top and cycle share of the
    way across."""
    js(page, "a => window.__shot.place(...a)",
       [row, cycle, zoom, share, above])
    settle(page)


def narrow_labels(page):
    """The label column at LABEL_SHOT_W, so a picture of the diagram is
    mostly diagram."""
    js(page, "w => { setLabelWidth(w); relayoutAfterLabelResize(); }",
       LABEL_SHOT_W)
    settle(page)


def shot_overview(page):
    """The whole page in a smaller window, its parts named where they leave
    room, larger, since the Help shows it shrunk."""
    page.set_viewport_size({"width": OVERVIEW_WINDOW[0],
                            "height": OVERVIEW_WINDOW[1]})
    settle(page, 400)
    place_overview(page)
    labels = rect(page, "#label-panel")
    view = rect(page, "#timeline-scroll")
    ruler = rect(page, "#ruler")
    bar = rect(page, "#metric-bar")
    end = rect(page, "#help-button")
    legend = js(page, """() => {
      const items = [...document.querySelectorAll('.legend-item')];
      const last = items[items.length - 1].getBoundingClientRect();
      const box = document.getElementById('legend').getBoundingClientRect();
      return [last.right, box.right, last.top + last.height / 2];
    }""")
    size = 14
    if legend[1] - legend[0] > pill_width("Legend", size) + 60:
        named = pill("Legend", legend[0] + 24, legend[2], anchor="start",
                     size=size)
    else:
        named = pill("Legend", legend[1] - 16, legend[2], anchor="end",
                     size=size)
    left = view[0] + 24
    marks = [
        pill("Toolbar", end[0] + end[2] + 18, end[1] + end[3] / 2,
             anchor="start", size=size),
        named,
        pill("Ruler of cycles", view[0] + view[2] * 0.8,
             ruler[1] + ruler[3] + 40,
             to=(view[0] + view[2] * 0.8, ruler[1] + ruler[3] - 2),
             size=size),
        pill("One row per instruction, one column per cycle", left,
             view[1] + view[3] * 0.62, anchor="start", size=size),
        pill("Label column", left, view[1] + view[3] * 0.8,
             to=(labels[0] + labels[2] - 24, view[1] + view[3] * 0.8),
             anchor="start", size=size),
    ]
    # The bar's last line, where its buttons leave room, else just above it.
    last = rect(page, "#metric-bar > button:last-of-type")
    if bar[0] + bar[2] - (last[0] + last[2]) > pill_width("Metric bar",
                                                          size) + 60:
        marks.append(pill("Metric bar", last[0] + last[2] + 24,
                          last[1] + last[3] / 2, anchor="start", size=size))
    else:
        marks.append(pill("Metric bar", left + 260, bar[1] - 32,
                          to=(left + 300, bar[1] + 1), anchor="start",
                          size=size))
    marks += overview_extra(page, size)
    return (0, 0, OVERVIEW_WINDOW[0], OVERVIEW_WINDOW[1]), marks


def shot_load(page, folder):
    """The Load JSON panel listing a folder of runs by subfolder, the page
    behind it left out."""
    page.click("#load-json-button")
    page.set_input_files("#folder-input", folder)
    page.wait_for_function("() => jsonFolder.entries.length > 0")
    js(page, """last => {
      for (const id of ['top-bar', 'view', 'metric-bar']) {
        document.getElementById(id).style.visibility = 'hidden';
      }
      jsonFolder.lastKey = last;
      jsonFolder.lastDir = last.split('/')[0];
      closeJsonBrowser();
      openJsonBrowser();
      document.getElementById('json-browser').style.height = '470px';
      document.getElementById('json-browser-list').scrollTop = 0;
      refreshJsonBrowser(false);
      document.activeElement.blur();
    }""", DEMO_LAST)
    settle(page)
    panel = rect(page, "#json-browser")
    filt = rect(page, "#json-browser-filter")
    heading = rect(page, ".json-dir")
    chosen = rect(page, ".json-row.selected")
    marks = [
        pill("Type words to filter", filt[0] + filt[2] - 10,
             filt[1] + filt[3] / 2, anchor="end"),
        pill("A subfolder", heading[0] + 190, heading[1] + heading[3] / 2,
             anchor="start"),
        pill("A click loads it", chosen[0] + 250,
             chosen[1] + chosen[3] + 18,
             to=(chosen[0] + 200, chosen[1] + chosen[3] - 3),
             anchor="start"),
    ]
    return grow(panel, 6), marks


def shot_toolbar(page):
    """The zoom and fetch range controls, named underneath."""
    js(page, "() => setZoomTo('20')")
    settle(page)
    strip = union(rect(page, "#top-bar .toolbar-label"),
                  rect(page, "#main-code-button"))
    under = strip[1] + strip[3] + 6
    lanes = [under + 22, under + 50]
    clip = (strip[0] - 10, strip[1] - 8, strip[2] + 20,
            lanes[-1] + 18 - strip[1] + 8)
    named = []
    for selector, text in (("#zoom-value", "Zoom, or + and -"),
                           ("#fit-all-button", "Fit All, or 0"),
                           ("#range-from", "Fetch numbers to show"),
                           ("#range-reset-button", "Show all"),
                           ("#main-code-button", "Main Code, or G")):
        x, _, w, _ = rect(page, selector)
        named.append((text, x + w / 2))
    marks = [band(clip[0], under, clip[2], lanes[-1] + 18 - under)]
    return clip, marks + lay_out(named, lanes, strip[1] + strip[3] + 1,
                                 (clip[0] + 4, clip[0] + clip[2] - 4))


def shot_stages(page):
    """One loop instruction's cells, each named underneath, the instruction
    at the bottom of the diagram, which alone fills the picture."""
    row = find(page, STAGE_PICK, 40)
    place(page, row, first_cycle(page, row), STAGE_ZOOM, 0.04, 4)
    parts = cells(page, row)
    top, height = row_y(page, row), row_h(page)
    left = rect(page, "#timeline-scroll")[0]
    lanes = [top + height + 20 + 28 * lane for lane in range(3)]
    named, seen = [], set()
    for cell in sorted(parts, key=lambda c: c["x0"]):
        text = STAGE_NAMES.get(cell["kind"])
        if text and text not in seen:
            seen.add(text)
            named.append((text, (cell["x0"] + cell["x1"]) / 2))
    marks, reach = stagger(named, lanes, top + height - 1)
    used = max(lanes.index(m["y"]) for m in marks)
    bottom = lanes[used] + 18
    right = max(max(c["x1"] for c in parts) + 30, reach + 10)
    clip = (left, top - 4 * height, right - left, bottom - (top - 4 * height))
    marks.insert(0, band(left, top + height, right - left,
                         bottom - top - height, "--bg"))
    return clip, marks


def shot_branch(page):
    """A mispredicted branch, the wrong-path instructions it flushed and
    the refetch after it."""
    if BRANCH_RANGE == "all":
        js(page, "() => document.getElementById('range-reset-button')"
                 ".click()")
        settle(page)
    narrow_labels(page)
    row = find(page, "rec => rec.bp_outcome === 'mispred'")
    ex = js(page, "row => rows[row].ex_cycle", row)
    place(page, row, ex, 24, 0.12, 1)
    height = row_h(page)
    flushed = js(page, """row => {
      const out = [];
      for (let r = row + 1; r < rows.length && rows[r].flushed; r++) {
        out.push([r, rows[r].flush_cycle]);
      }
      return out;
    }""", row)
    refetch = flushed[-1][0] + 1 if flushed else row + 1
    labels = rect(page, "#label-panel")
    top = row_y(page, row - 1)
    badge = cell_of(page, row, ("bpMispred",))
    y = row_y(page, row)
    marks = [pill("Mispredicted, caught in Execute", badge["x1"] + 34,
                  y - height * 0.45, to=(badge["x1"] - 10, y + 5),
                  anchor="start")]
    reach = badge["x1"] + 34 + pill_width(marks[0]["pill"])
    if flushed:
        middle, cycle = flushed[len(flushed) // 2]
        tick = js(page, "c => window.__shot.cycleX(c + 1)", cycle)
        text = "Flushed, thrown away at the ✗"
        marks.append(pill(text, tick + 44, row_y(page, middle) + height / 2,
                          to=(tick + 4, row_y(page, middle) + height / 2),
                          anchor="start"))
        reach = max(reach, tick + 44 + pill_width(text))
    # The last of the refetched rows, whose cells' lower edges face rows
    # that start later, so the arrows up to them cross no other cell.
    kinds, text = BRANCH_LEAD
    while any(c["kind"] in kinds for c in cells(page, refetch + 1)):
        refetch += 1
    y = row_y(page, refetch)
    lead = sorted(((c["x0"] + c["x1"]) / 2 for c in cells(page, refetch)
                   if c["kind"] in kinds))
    if lead:
        view = rect(page, "#timeline-scroll")
        x = max(view[0] + 10, lead[0] - pill_width(text) - 10)
        marks.append(pill(text, x, y + height * 2,
                          to=[(x, y + height - 1) for x in lead],
                          anchor="start"))
    right = min(WINDOW[0], reach + 16)
    return (labels[0], top, right - labels[0], y + 3 * height - top), marks


def shot_forwarding(page):
    """The forwarding arrows into and out of one instruction of the loop,
    named from the empty corner above and right of the staircase."""
    narrow_labels(page)
    js(page, """() => {
      const box = document.getElementById('forwarding-on');
      if (!box.checked) box.click();
    }""")
    row = find(page, ARROW_FIND, 60)
    producer = js(page, """row => {
      const edge = forwardingEdges(rows[row])[0];
      return edge.producerRow ?? edge.prodRow;
    }""", row)
    start = cell_of(page, producer, EXECUTE_KINDS)["c0"]
    place(page, row, start - 4, 28, 0.0, 4)
    height = row_h(page)
    labels = rect(page, "#label-panel")
    top, bottom = row_y(page, row - 4), row_y(page, row + 4)
    arrows = [a for a in js(page, ARROWS_JS)
              if top <= min(a["y0"], a["y1"]) and max(a["y0"], a["y1"])
              <= bottom]
    if not arrows:
        raise RuntimeError("no forwarding arrow near the chosen row")
    marks, used = [], []
    right = max(max(a["x0"], a["x1"]) for a in arrows) + 3 * 28
    for arrow in sorted(arrows, key=lambda a: a["y1"]):
        name = ARROW_NAMES.get(arrow["kind"])
        if not name or arrow["kind"] in used:
            continue
        used.append(arrow["kind"])
        mid = ((arrow["x0"] + arrow["x1"]) / 2,
               (arrow["y0"] + arrow["y1"]) / 2)
        y = row_y(page, row - 3) + height * (0.5 + 1.5 * (len(used) - 1))
        line = row - 3 + int(1.5 * (len(used) - 1) + 0.5)
        ends = [max(c["x1"] for c in cells(page, r))
                for r in range(row - 4, line + 2)]
        x = max(mid[0] + 60, max(ends) + 24)
        marks.append(pill(name, x, y, to=mid, anchor="start"))
        right = max(right, x + pill_width(name) + 16)
    clip = (labels[0], top, min(WINDOW[0], right) - labels[0], bottom - top)
    return clip, marks


def hover(page, row):
    """The pointer on row's Execute cell, and where it stands."""
    host = cell_of(page, row, EXECUTE_KINDS)
    x = host["x0"] + min(host["x1"] - host["x0"] - 2, 9)
    y = row_y(page, row) + row_h(page) * 0.62
    page.mouse.move(x, y)
    settle(page)
    return x, y


def shot_tooltip(page):
    """The tooltip of a loop instruction, beside the pointer that raised
    it."""
    row = find(page, TOOLTIP_PICK, 40)
    place(page, row, first_cycle(page, row), 20, 0.12, 6)
    x, y = hover(page, row)
    tip = rect(page, "#tooltip")
    indicator = rect(page, "#tooltip .stage-indicator")
    clip = grow(union(tip, (x - 70, y - 50, 90, 100)), 12, 12, 12, 44)
    marks = [pointer(x, y),
             pill("The stage under the pointer", tip[0] + tip[2],
                  indicator[1] + indicator[3] + 26,
                  to=(indicator[0] + 90, indicator[1] + indicator[3] - 3),
                  anchor="end")]
    return clip, marks


def shot_pins(page):
    """Two neighbouring instructions' tooltips pinned side by side."""
    first = find(page, TOOLTIP_PICK, 40)
    place(page, first, first_cycle(page, first), 20, 0.12, 2)
    for row in (first, first + 1):
        hover(page, row)
        page.keyboard.press("p")
    page.mouse.move(WINDOW[0] - 2, 2)
    settle(page)
    dock = rect(page, "#pin-dock")
    right = js(page, """() => {
      const cards = document.querySelectorAll('.pin-card');
      return cards[cards.length - 1].getBoundingClientRect().right;
    }""")
    title = rect(page, ".pin-card .pin-go")
    clear = rect(page, "#pin-clear")
    marks = [pill("A title goes to its instruction", clear[0] + clear[2] + 40,
                  clear[1] + clear[3] / 2,
                  to=(title[0] + 14, title[1] - 1), anchor="start")]
    return (dock[0], dock[1], right + 10 - dock[0], dock[3]), marks


def shot_metrics(page):
    """The metric bar's first fields and its stall figures, with the
    pointer on a row so the fields under it are filled in. The bar is cut
    to the Help's width, and a figure the cut would halve is left out."""
    row = find(page, TOOLTIP_PICK, 40)
    place(page, row, first_cycle(page, row), 20, 0.12, 4)
    js(page, "() => { document.getElementById('tooltip-on').click(); }")
    hover(page, row)
    bar = rect(page, "#metric-bar")
    clip = (bar[0], bar[1] - 44, 716, bar[3] + 44)
    js(page, """right => {
      for (const el of document.getElementById('metric-bar').children) {
        if (el.getBoundingClientRect().right > right) {
          el.style.visibility = 'hidden';
        }
      }
    }""", clip[0] + clip[2] - 8)
    spans = js(page, """() => {
      const r = id => {
        const box = document.getElementById(id).parentElement
          .getBoundingClientRect();
        return [box.left, box.top, box.right, box.bottom];
      };
      return [r('metric-cycle'), r('metric-pc'), r('metric-flush-stall'),
        r('metric-stall')];
    }""")
    pointer_box = (spans[0][0] - 6, spans[0][1] - 3,
                   spans[1][2] - spans[0][0] + 12, spans[0][3] - spans[0][1]
                   + 6)
    stall_box = (spans[2][0] - 6, spans[2][1] - 3,
                 spans[3][2] - spans[2][0] + 12, spans[2][3] - spans[2][1]
                 + 6)
    marks = [band(clip[0], clip[1], clip[2], 44, "--bg"), box(*pointer_box),
             box(*stall_box),
             pill("The row and cycle under the pointer",
                  pointer_box[0] + pointer_box[2] / 2, clip[1] + 22,
                  to=(pointer_box[0] + pointer_box[2] / 2,
                      pointer_box[1] - 2)),
             pill("Stall figures", stall_box[0] + stall_box[2] + 10,
                  stall_box[1] + stall_box[3] / 2, anchor="start")]
    return clip, marks


def shot_extra(page):
    """Extra Info's stall diagram, over the first few hundred instructions
    of Main Code, where the kinds of stall take turns."""
    js(page, """() => {
      document.getElementById('range-to').value = String(rows[300].id);
      document.getElementById('range-to').dispatchEvent(
        new Event('change'));
    }""")
    settle(page)
    page.keyboard.press("i")
    settle(page)
    js(page, """() => document.getElementById('stall-diagram-wrap')
      .scrollIntoView({ block: 'end' })""")
    settle(page, 400)
    body = rect(page, "#extra-info-body")
    wrap = rect(page, "#stall-diagram-wrap")
    heading = js(page, """() => {
      const wrap = document.getElementById('stall-diagram-wrap');
      const above = [...document.querySelectorAll('#extra-info-body h3')]
        .filter(h => h.compareDocumentPosition(wrap)
          & Node.DOCUMENT_POSITION_FOLLOWING);
      const last = above[above.length - 1];
      return last ? last.getBoundingClientRect().top : null;
    }""")
    top = wrap[1] if heading is None else heading
    return (body[0], top - 12, body[2], wrap[1] + wrap[3] - top + 14), []


def shot_view(page):
    """The View menu, open under its button."""
    page.keyboard.press("v")
    settle(page)
    panel = rect(page, "#view-menu .menu-panel")
    button = rect(page, "#view-menu summary")
    return grow(union(panel, button), 10), []


# SHARED END py-help-shots


# The sample's loop: the instruction whose cells the stages picture names,
# a multiply held in the decode register, and the one whose tooltip is
# shown, each a test of a record, as JavaScript.
STAGE_PICK = (r"rec => /^fmul/.test(rec.disasm) && !rec.flushed && "
              r"cellsOf(rec).some(cell => cell.kind === 'decBuf')")
TOOLTIP_PICK = r"rec => /^fmul/.test(rec.disasm) && !rec.flushed"
STAGE_ZOOM = 34
STAGE_NAMES = {
    "fe1Req": "Fetch1 request", "fe2": "Fetch2 response",
    "feBuf": "Front-end FIFO", "dec": "Decode", "decBuf": "Decode register",
    "is": "Issue", "fp": "Execute", "co": "Commit",
}
EXECUTE_KINDS = ("int", "fp", "mem", "branch")

# The sample's one misprediction ends the loop inside Main Code, and the
# branch picture names the bubble before the refetch, by kind.
BRANCH_RANGE = "main"
BRANCH_LEAD = (("bubbleMispred",), "The bubble")

# A consumer that read an operand from the scoreboard, and every arrow in
# view, from its start to its head as the page draws them, by how the
# value came.
ARROW_FIND = (r"rec => forwardingEdges(rec).some(edge => "
              r"rec[`fwd_${edge.operand}_via`] !== 'wb')")
ARROWS_JS = r"""
() => {
  const view = window.__shot.view(), rowH = rowHeight();
  const first = firstVisibleCycle(), out = [];
  const last = Math.min(rows.length - 1, scrollY + Math.ceil(view.h / rowH));
  for (let row = scrollY; row <= last; row++) {
    const consumer = rows[row];
    if (consumer.is_cycle == null) continue;
    const edges = forwardingEdges(consumer);
    edges.forEach((edge, slot) => out.push({
      kind: consumer[`fwd_${edge.operand}_via`] === 'wb' ? 'wb' : 'sb',
      x0: view.x + (edge.producer.wb_cycle + 0.5 - first) * cellW,
      y0: view.y + (edge.producerRow - scrollY + 0.5) * rowH,
      x1: view.x + (consumer.is_cycle + 0.5 - first) * cellW,
      y1: view.y + (row - scrollY + (slot + 1) / (edges.length + 1))
        * rowH }));
  }
  return out;
}
"""
ARROW_NAMES = {"wb": "Forwarded at writeback",
               "sb": "Read from the scoreboard"}


# The first cycle of the stretch of the loop that holds the most
# writebacks, among the first few hundred, so the track has bars to show.
WB_STRETCH_JS = r"""
() => {
  const wbs = rangeMemWritebacks;
  let best = [0, extent.renderLo];
  for (const wb of wbs.slice(0, 400)) {
    const lo = wb.alloc_cycle ?? wb.send_cycle, hi = lo + 50;
    const count = wbs.filter(other => (other.alloc_cycle ?? other.send_cycle)
      < hi && other.ack_cycle >= lo).length;
    if (count > best[0]) best = [count, lo];
  }
  return best[1];
}
"""


def place_overview(page):
    """A stretch of the loop that holds writebacks, so the track has bars,
    from the oldest instruction still in flight there."""
    stretch = js(page, WB_STRETCH_JS)
    row = find(page, "rec => cellsOf(rec).some(c => c.c1 > %d)" % stretch)
    place(page, row, stretch, 12, 0.2, 1)


def overview_extra(page, size):
    """The memory writeback track, named in the widest stretch of a lane
    that no bar crosses."""
    gap = js(page, """() => {
      const canvas = document.getElementById('mem-wb-canvas')
        .getBoundingClientRect();
      const laneH = memWbLaneH();
      let best = [0, canvas.left, canvas.top + laneH / 2];
      for (let lane = 0; lane < memWbLanes; lane++) {
        const y = lane * laneH + 2;
        const bars = memWbRender.filter(bar => Math.abs(bar.y - y) < 1)
          .map(bar => [bar.x0, bar.x1]).sort((a, b) => a[0] - b[0]);
        let from = 0;
        for (const [x0, x1] of bars.concat([[canvas.width, canvas.width]])) {
          if (x0 - from > best[0]) {
            best = [x0 - from, canvas.left + (from + x0) / 2,
              canvas.top + (lane + 0.5) * laneH];
          }
          from = Math.max(from, x1);
        }
      }
      return best.slice(1);
    }""")
    return [pill("Memory writebacks", gap[0], gap[1], size=size)]


def shot_writebacks(page):
    """The memory writeback track over a stretch of the loop that holds
    several writebacks, the parts of one of them named in a strip under
    it."""
    narrow_labels(page)
    stretch = js(page, WB_STRETCH_JS)
    js(page, """lo => {
      setZoomTo('14');
      setScrollX(lo - extent.renderLo - 6);
    }""", stretch)
    settle(page)
    panel = rect(page, "#mem-wb-panel")
    canvas = rect(page, "#mem-wb-canvas")
    bar = js(page, """() => {
      const canvas = document.getElementById('mem-wb-canvas')
        .getBoundingClientRect();
      const laneH = memWbLaneH(), first = firstVisibleCycle();
      const x = cycle => canvas.left + (cycle - first) * cellW;
      const shown = rangeMemWritebacks.filter(wb => wb.is_linked
        && x(wb.alloc_cycle ?? wb.send_cycle) > canvas.left + 60
        && x(wb.ack_cycle + 1) < canvas.left + 420);
      shown.sort((a, b) => (b.flush_slot ?? 0) - (a.flush_slot ?? 0));
      const wb = shown[0];
      if (!wb) return null;
      const top = canvas.top + (wb.flush_slot ?? 0) * laneH;
      return { alloc: x(wb.alloc_cycle ?? wb.send_cycle), send:
        x(wb.send_cycle), ack: x(wb.ack_cycle + 1), evict: x(wb.evict_cycle)
        + 0.5, top, bottom: top + laneH, laneBottom: canvas.top
        + memWbLanes * laneH };
    }""")
    if bar is None:
        raise RuntimeError("no linked writeback in the stretch shown")
    under = max(bar["laneBottom"], canvas[1] + canvas[3])
    clip = (panel[0], panel[1], 716, under - panel[1] + 70)
    y = bar["bottom"] - 3
    marks = [band(panel[0], under, 716, 70, "--bg"),
             pill("The eviction that caused it", bar["evict"] - 14,
                  under + 20, to=(bar["evict"], y), anchor="end"),
             pill("Waiting to be sent", bar["alloc"] + 6, under + 48,
                  to=((bar["alloc"] + bar["send"]) / 2, y), anchor="end"),
             pill("The AXI write, to its acknowledgement",
                  (bar["send"] + bar["ack"]) / 2 + 30, under + 34,
                  to=((bar["send"] + bar["ack"]) / 2, y), anchor="start")]
    return clip, marks


# The pictures in the Help's order, each a function of the page that leaves
# it as the picture shows and returns the clip and the labels.
SHOTS = (
    ("overview", shot_overview),
    ("load", shot_load),
    ("toolbar", shot_toolbar),
    ("stages", shot_stages),
    ("branch", shot_branch),
    ("forwarding", shot_forwarding),
    ("tooltip", shot_tooltip),
    ("pins", shot_pins),
    ("metrics", shot_metrics),
    ("extra", shot_extra),
    ("writebacks", shot_writebacks),
    ("view", shot_view),
)

# The folder the Load JSON picture lists, as the calibration runs lay out
# their JSONs, in MiB, one past the whole-file limit, and the JSON it marks
# as loaded last.
DEMO_RUNS = {
    "rtl": [("atomic_fence", 2.1), ("basic_test", 6.8),
            ("btb_pressure", 14.2), ("daxpy", 31.4), ("fp_addmul", 22.6),
            ("full_test", 720.0), ("int_div", 0.9), ("store_fwd", 0.7)],
    "rtl_ddr3": [("atomic_fence", 2.3), ("basic_test", 7.4),
                 ("btb_pressure", 15.0), ("daxpy", 33.1),
                 ("fp_addmul", 23.9)],
}
DEMO_LAST = "rtl/daxpy.json"


# SHARED BEGIN py-help-main

# Needs: argparse, os, sys, tempfile, SHOTS, DEMO_RUNS, VIEWER, PAGE,
# SAMPLE, page_root, open_viewer, py-help-capture, py-human-size

def main():
    names = [name for name, _ in SHOTS]
    parser = argparse.ArgumentParser(
        description=f"Make the pictures of {VIEWER}'s Help, screenshots of "
                    f"the page with {SAMPLE} loaded, in "
                    f"{SHOT_DIR}/ beside the page.")
    parser.add_argument(
        "--only", metavar="NAMES",
        help=f"Make only these pictures, comma separated, of "
             f"{', '.join(names)}")
    parser.add_argument(
        "--theme", choices=("both", "light", "dark"), default="both",
        help="Make the pictures in one theme only. Defaults to both")
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Name the pictures that would be made, and make none")
    args = parser.parse_args()

    chosen = names
    if args.only:
        chosen = [name.strip() for name in args.only.split(",")
                  if name.strip()]
        unknown = [name for name in chosen if name not in names]
        if unknown:
            print(f"[ERROR] No picture is called {', '.join(unknown)}. "
                  f"They are {', '.join(names)}.", file=sys.stderr)
            return 1
    if not os.path.isfile(os.path.join(page_root(), PAGE)):
        print(f"[ERROR] {PAGE} is not in {page_root()}.", file=sys.stderr)
        return 1
    text = read_page()
    scale = help_scale(text)
    if scale is None:
        print(f"[ERROR] {PAGE} declares no HELP_SHOT_SCALE.",
              file=sys.stderr)
        return 1
    shown = help_shot_names(text)
    for name in sorted(set(shown) - set(names)):
        print(f"[WARN] The Help shows {name}, which this script does not "
              f"make.")
    for name in sorted(set(names) - set(shown)):
        print(f"[WARN] This script makes {name}, which the Help does not "
              f"show.")
    themes = ("light", "dark") if args.theme == "both" else (args.theme,)
    out_dir = os.path.join(page_root(), SHOT_DIR)
    planned = [(name, theme,
                os.path.join(out_dir, f"{VIEWER}_{name}_{theme}.png"))
               for theme in themes for name in chosen]
    if args.dry_run:
        for _, _, path in planned:
            print(f"[INFO] Would write {os.path.relpath(path)}")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] Playwright is not installed. Install it and its "
              "Chromium with: pip install playwright && python3 -m "
              "playwright install chromium", file=sys.stderr)
        return 1
    os.makedirs(out_dir, exist_ok=True)
    shots = dict(SHOTS)
    total, failed = 0, 0
    with tempfile.TemporaryDirectory() as scratch, \
            serve(page_root()) as url, sync_playwright() as playwright:
        folder = demo_folder(scratch, DEMO_RUNS)
        browser = playwright.chromium.launch()
        for name, theme, path in planned:
            context, page, errors = open_viewer(browser, url, theme, scale)
            try:
                shot = shots[name]
                clip, marks = (shot(page, folder) if name == "load"
                               else shot(page))
                png = pack(page, capture(page, clip, marks))
            except Exception as err:
                failed += 1
                print(f"[ERROR] {name} in {theme}: {err}", file=sys.stderr)
                continue
            finally:
                context.close()
            with open(path, "wb") as f:
                f.write(png)
            total += len(png)
            note = ("" if len(png) <= SHOT_BUDGET
                    else f", past the {human(SHOT_BUDGET)} aimed for")
            print(f"[INFO] Wrote {os.path.relpath(path)} "
                  f"({human(len(png))}{note})")
            for error in errors:
                print(f"[WARN] {name} in {theme}: the page raised {error}")
        browser.close()
    print(f"[INFO] {len(planned) - failed} pictures, {human(total)} in all")
    return 1 if failed else 0

# SHARED END py-help-main


if __name__ == "__main__":
    sys.exit(main())
