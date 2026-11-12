# -*- coding: utf-8 -*-
"""Mega Drive background tool: measure, sample, choose, emit.

Flow
----
  python md_fundo_v2.py measure --photos photos/ --grid grid.txt
  python md_fundo_v2.py sample  --source wall.png
  python md_fundo_v2.py choose D --samples wall
  python md_fundo_v2.py emit   --samples wall --name wall

The idea: palette and tile count are one problem. The tool measures, proposes
four complete results (image rebuilt from the tiles, which is what the ROM will
show), you choose, and the answer becomes the default for the next run.

Conventions
-----------
- Every color is snapped to the real 512-color Mega Drive grid
  (levels 0, 52, 87, 116, 144, 172, 206, 255). These are exactly the values the
  rescomp converter reads without loss, so the snap here is what guarantees the
  final on-screen color.
- Index 0 is always the plane "backdrop": on the MD pixel 0 of a plane is
  transparent. With transparency = no, the backdrop gets color 0 and the art
  may use index 0 with no hole. With transparency = yes, the art uses 1..15 and
  0 is a hole.
- N palettes = N sets of 16 colors in CRAM. The palette is chosen PER TILE,
  not per band: every tile tries all candidates and keeps the one that paints
  it with the lowest error, then each palette is rebuilt from the tiles that
  picked it and the assignment is repeated until it stops improving. That is
  what the map entry allows (bits 13-14 select the palette of that tile), so
  the horizontal bands are only the starting guess. The indexed PNG carries
  the palette in bits 4-5 of each pixel (the format rescomp reads).
- Before the first question the tool prints what the image asks for: how many
  colors it has (in the file and after the snap to the MD grid) and how many
  unique 8x8 tiles the image holds, including the worst 320x240 view. The view
  is measured with a sliding window (8 px per step, from the top to the last
  complete window), with identical and mirrored tiles counted once. That is the
  size of the problem the answers have to deal with.
- The eight setup questions (palettes, palette source, mode, transparency,
  dithering, scroll, resize, tile cap) are ALWAYS asked. Skipping them requires --no-ask, which exists
  only for an explicit "no questions, just the result". A previous answer is
  never offered as the prompt default: the default shown is the factory value,
  and the stored profile is only the fallback used by --no-ask.
- Question 6 asks whether the image is a background that scrolls; the answer
  suggests question 7 (resize): a scrolling plane follows the screen height
  (224), a still image follows the screen width (320). "none" keeps the pixels.
- Question 8 asks for the maximum number of unique tiles the final image may
  have; every sample is cut to that ceiling (0 = no limit). The cap is a hard
  budget, so it wins over the recipe's own target. Question 8b asks where the
  cap applies: the whole image (every tile of the picture counted once) or each
  320x240 view (a per-view budget, for a background the ROM streams while the
  camera moves). With "view" the image may hold more tiles than the cap as
  long as no single 320x240 sliding window passes it, at every camera position.
  Tiles are compared by their 4-bit colour indices; palette and normal/shadow
  priority are map attributes and do not make a different tile.
- shadow/highlight are alternative copies of the SAME palette slots: the ROM
  keeps both sets and the code rewrites the CRAM when it wants the flash, so a
  variant never consumes a slot. Load the normal set once, then swap.
- report.md measures the final image again from its 4-bit colour indices: a
  320x240 window sliding 8 px at a time (all camera positions), identical and
  mirrored tiles counted once, with the peak count, its X/Y coordinates and the
  cap check. The preflight number is only the source-drawing estimate.
- An alpha channel is composited on white (or --bg-color RRGGBB); it is never
  dropped, because PIL would write black wherever the pixel is transparent.
- When a side is over 512 px (the biggest plane), question 9 asks whether to
  split the image into segments, shrink it to fit, or keep the resolution
  ("none"). "none" knowingly breaks the plane limit: the tool reports it and
  leaves the workaround to the ROM. Split cuts the image into
  the fewest pieces that fit, as evenly as possible (632 -> 312 + 320, never a
  sliver), runs the four samples per segment, each in its own folder, and
  writes split.json with the position and size of every piece.
- Images are padded to multiples of 8 by default: the missing rows/columns
  are added as white (or --pad-color RRGGBB) on the bottom/right border, never
  inside the art. Use --no-pad to require exact multiples instead.
- Samples are always numbered the same way:
      1 = A fidelity   2 = B worst case   3 = C balance worst   4 = D balance
  The number is the choice; the letter is the recipe.
"""
import argparse
import glob
import json
import os
import pathlib
import re
import shutil
import sys
import time
import traceback

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# A windowed .exe has no console: sys.stdout/stderr are None there, and the
# GUI replaces them with its own log stream, which has no reconfigure().
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from md_color import MD_GRID_LAB, MD_GRID_RGB, rgb_to_lab  # noqa: E402

APP_NAME = "MD Background Tools"


def _user_root():
    """Folder for profile.json, the default res/ folder and error logs.

    A frozen .exe that already has a profile.json beside it keeps using that
    folder (older installs). Otherwise it is Documents/MD Background Tools, a
    place every user can write to and find (the .exe may sit in a read-only
    or downloads folder)."""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.exists(os.path.join(exe_dir, "profile.json")):
        return exe_dir
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    base = docs if os.path.isdir(docs) else os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return exe_dir


if getattr(sys, "frozen", False):
    ROOT = _user_root()
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(ROOT, "profile.json")

LEVELS = np.array([0, 52, 87, 116, 144, 172, 206, 255], dtype=np.float64)

BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]])
BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
])

# 8x8 clustered-dot (halftone) threshold matrix, 45 degree screen: 0..63.
# Two 4x4 cells on the diagonal (top-left, bottom-right) hold the dark dots and
# take the low thresholds; the other diagonal holds the light dots, grown from
# their own centre with the high thresholds. v1 put both low cells in the left
# column, which turned the screen into vertical 4 px stripes.
_CLUSTER4 = np.array([[12, 5, 6, 13],
                      [4, 0, 1, 7],
                      [11, 3, 2, 8],
                      [15, 10, 9, 14]])
HALFTONE8 = np.block([[2 * _CLUSTER4, 63 - 2 * _CLUSTER4],
                      [62 - 2 * _CLUSTER4, 2 * _CLUSTER4 + 1]])
# The 8 px screen above is correct but coarse: on 320 px art it reads as a
# checkerboard. "halftone" uses the same 45 degree layout with a 4 px period.
_CLUSTER2 = np.array([[0, 1], [3, 2]])
HALFTONE4 = np.block([[2 * _CLUSTER2, 15 - 2 * _CLUSTER2],
                      [14 - 2 * _CLUSTER2, 2 * _CLUSTER2 + 1]])

# The MD DAC has 15 steps. Normal colours use the even steps (the 8 LEVELS);
# shadow shows step n and highlight step n + 7 for a 3-bit level n.
DAC15 = np.array([0, 29, 52, 70, 87, 101, 116, 130, 144, 158, 172, 187, 206, 228, 255],
                 dtype=np.float64)

try:  # optional: numba makes the error diffusion loops ~50x faster
    from numba import njit as _njit
except Exception:  # pragma: no cover - plain Python fallback
    def _njit(*a, **k):
        if a and callable(a[0]):
            return a[0]
        return lambda f: f


def _blue_noise_matrix(n=16, sigma=1.5):
    """Deterministic blue-noise threshold matrix (16x16 by default).

    Greedy void filling with a Gaussian inhibition kernel: each new rank goes
    to the emptiest cell, so the dots stay spread out in the frequency domain.
    """
    radius = max(1, int(3 * sigma))
    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    kern = np.exp(-(xx * xx + yy * yy) / (2.0 * sigma * sigma))
    kern /= kern.sum()
    rng = np.random.default_rng(0x5EED)
    tie = rng.random((n, n)) * 1e-9
    density = np.zeros((n, n), np.float64)
    used = np.zeros((n, n), bool)
    rank = np.full((n, n), -1, np.int32)
    for k in range(n * n):
        score = density + tie
        score[used] = np.inf
        y, x = divmod(int(np.argmin(score)), n)
        rank[y, x] = k
        used[y, x] = True
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                density[(y + dy) % n, (x + dx) % n] += kern[dy + radius, dx + radius]
    return rank


BLUE16 = _blue_noise_matrix(16)

MODES = ("normal", "shadow", "highlight", "both")
DITHERS = ("none", "ordered4", "ordered8", "floyd", "atkinson", "stucki",
           "halftone", "bluenoise", "riemersma")

# Fixed slot order: the number never moves, the letter is the recipe.
LETTERS = "ABCD"


# --------------------------------------------------------------------------
# color
# --------------------------------------------------------------------------
def lab_to_rgb(lab):
    lab = np.asarray(lab, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0

    def inv(f):
        return np.where(f ** 3 > 216.0 / 24389.0, f ** 3, (116.0 * f - 16.0) * 27.0 / 24389.0)

    xyz = np.stack([inv(fx), inv(fy), inv(fz)], axis=-1) * np.array([0.95047, 1.0, 1.08883])
    m = np.array([[3.2404542, -1.5371385, -0.4985314],
                  [-0.9692660, 1.8760108, 0.0415560],
                  [0.0556434, -0.2040259, 1.0572252]])
    lin = xyz @ m.T
    c = np.where(lin > 0.0031308, 1.055 * np.maximum(lin, 0.0) ** (1 / 2.4) - 0.055, 12.92 * lin)
    return np.clip(c * 255.0 + 0.5, 0, 255).astype(np.uint8)


def snap_md(rgb):
    """Snap every color to the 512-color MD grid (nearest in Lab).

    Done in chunks: the direct version builds an (N, 512, 3) float matrix, which
    is gigabytes on a full image and dies with MemoryError around one megapixel
    - and it dies before anything is written to disk.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    shape = rgb.shape[:-1]
    lab = rgb_to_lab(rgb).reshape(-1, 3)
    out = np.empty((lab.shape[0], 3), np.uint8)
    passo = 16384
    for i in range(0, lab.shape[0], passo):
        d = dists(lab[i:i + passo], MD_GRID_LAB)
        out[i:i + passo] = MD_GRID_RGB[d.argmin(1)]
    return out.reshape(shape + (3,))


def dists(pts, cent):
    """Squared Lab distance, without blowing memory ((n,3) x (k,3) -> (n,k))."""
    pts = np.asarray(pts, dtype=np.float64)
    cent = np.asarray(cent, dtype=np.float64)
    return np.maximum((pts * pts).sum(1)[:, None] + (cent * cent).sum(1)[None, :] - 2.0 * pts @ cent.T, 0.0)


def luma(rgb):
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def has_alpha(path):
    im = Image.open(path)
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def transparent_fraction(path):
    """Fraction of fully transparent pixels; 0 for an opaque image."""
    im = Image.open(path)
    if not has_alpha(path):
        return 0.0
    a = np.asarray(im.convert("RGBA"))[..., 3]
    return float((a == 0).mean())


def load(path, bg=None):
    """RGB pixels. An alpha channel is composited on bg (default white), never
    dropped: PIL would turn every transparent pixel into black."""
    bg = PAD_FILL if bg is None else bg
    im = Image.open(path)
    if has_alpha(path):
        rgba = im.convert("RGBA")
        base = Image.new("RGB", rgba.size, tuple(int(v) for v in bg))
        base.paste(rgba, (0, 0), rgba)
        return np.asarray(base, dtype=np.uint8)
    return np.asarray(im.convert("RGB"), dtype=np.uint8)


PAD_FILL = (255, 255, 255)


def parse_fill(text):
    """Border color for the padding: RRGGBB (the # is optional)."""
    if not text:
        return PAD_FILL
    t = text.strip().lstrip("#")
    if len(t) == 6:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
    raise SystemExit(f"pad color must be RRGGBB: {text}")


RESIZE_W, RESIZE_H = 320, 224
PLANE_MAX = 512          # the biggest plane is 64x64 tiles = 512x512 px


def resize_to(rgb, how):
    """Resize for the screen: 'width' -> 320 wide, 'height' -> 224 tall.

    Nearest neighbour on purpose: it never invents a colour and never blurs the
    pixels the palette will have to carry, which is what a tile conversion
    wants. LANCZOS would mix neighbours and add colours that were not there.
    """
    if how in (None, "", "none"):
        return rgb
    h, w = rgb.shape[:2]
    if how == "width":
        nw, nh = RESIZE_W, max(1, int(round(h * RESIZE_W / w)))
    elif how == "height":
        nh, nw = RESIZE_H, max(1, int(round(w * RESIZE_H / h)))
    else:
        raise SystemExit(f"unknown resize: {how}")
    if (nw, nh) == (w, h):
        return rgb
    return np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.NEAREST), np.uint8)


def pad8(rgb, fill=PAD_FILL):
    """Grow the image to multiples of 8, always at the bottom/right edge.

    A plane is made of 8x8 tiles, so a side that is not a multiple of 8 cannot
    be tiled. Nothing is inserted inside the image: the missing rows/columns go
    on the border, so every original pixel keeps its (x, y).
    """
    h, w = rgb.shape[:2]
    ph, pw = (-h) % 8, (-w) % 8
    if not ph and not pw:
        return rgb, (0, 0)
    out = np.empty((h + ph, w + pw, 3), np.uint8)
    out[:, :] = np.asarray(fill, np.uint8)
    out[:h, :w] = rgb
    return out, (pw, ph)


def auto_gain(rgb):
    """Gain so the art fits the usable MD grid range (the capture is very dark)."""
    l = luma(rgb.astype(np.float64))
    p = float(np.percentile(l, 99.5))
    if p < 1:
        return 1.0
    return float(np.clip(230.0 / p, 1.0, 2.6))


def prepare(rgb, gain, brightness=0.0):
    if gain == 1.0 and brightness == 0.0:
        return rgb
    return np.clip(rgb.astype(np.float64) * gain + brightness, 0, 255).astype(np.uint8)


def file_uri(path):
    """file:///... — clickable in the terminal and in the browser."""
    return pathlib.Path(path).resolve().as_uri()


def show_link(label, path):
    """Print the file as a link: OSC 8 in a terminal; file:// always."""
    u = file_uri(path)
    name = os.path.basename(path)
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        print(f"{label}: \033]8;;{u}\033\\{name}\033]8;;\033\\")
    else:
        print(f"{label}: {name}")
    print(f"  link: {u}")


def make_unique_dir(path):
    """Create path, or the first free path_2, path_3... if it already exists.

    A run must never overwrite a previous result and must not fail just because
    the folder is already there. Explicit "." (or the current directory) is
    respected as-is: that is the documented "drop the files here" choice.
    """
    path = os.path.normpath(path)
    if not os.path.exists(path):
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            pass
    parent, name = os.path.split(path)
    n = 2
    while True:
        cand = os.path.join(parent, f"{name}_{n}")
        if not os.path.exists(cand):
            try:
                os.makedirs(cand)
                print(f"folder already exists: using {cand}")
                return cand
            except FileExistsError:
                pass
        n += 1


def write_open_page(folder, title, items):
    """Local page: opens the comparison and the four samples with one click."""
    lines = ["<!doctype html>", '<meta charset="utf-8">', f"<title>{title}</title>",
             '<body style="margin:0;background:#16161a;color:#e8e8e8;'
             'font:14px/1.4 Segoe UI,sans-serif">',
             f'<h2 style="margin:0;padding:14px 14px 6px">{title}</h2>',
             '<p style="margin:0;padding:0 14px 12px">']
    for i, (name, label) in enumerate(items):
        sep = "" if i == len(items) - 1 else " · "
        lines.append(f'<a style="color:#8cf" href="{name}">{label}</a>{sep}')
    lines.append("</p>")
    lines.append(f'<img src="{items[0][0]}" style="max-width:100%;display:block">')
    lines.append("</body>")
    path = os.path.join(folder, "open.html")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
def _pp(pts, w, k, rnd):
    n = len(pts)
    p = np.asarray(w, np.float64)
    s = p.sum()
    p = np.full(n, 1.0 / n) if not np.isfinite(s) or s <= 0 else p / s
    c = [pts[int(rnd.choice(n, p=p))]]
    d2 = ((pts - c[0]) ** 2).sum(1)
    for _ in range(1, k):
        pp = d2 / (d2.sum() + 1e-12)
        pp = np.nan_to_num(pp, nan=0.0, posinf=0.0, neginf=0.0)
        s = pp.sum()
        pp = np.full(n, 1.0 / n) if s <= 0 else pp / s
        i = int(rnd.choice(n, p=pp))
        c.append(pts[i])
        d2 = np.minimum(d2, ((pts - pts[i]) ** 2).sum(1))
    return np.array(c)


def _dedupe(cent, pts, w):
    seen = {}
    for j in range(len(cent)):
        key = tuple(np.round(cent[j], 2))
        if key in seen:
            d = dists(pts, cent).min(1) * w
            cent[j] = pts[int(d.argmax())]
        seen[key] = j
    return cent


def kmeans_md(lab, k, weights=None, seed=0, iterations=12, sample=40000):
    """k-means in Lab with the codebook restricted to the MD grid."""
    rnd = np.random.default_rng(seed)
    pts = np.asarray(lab, dtype=np.float64).reshape(-1, 3)
    k = max(1, min(k, len(np.unique(np.round(pts, 2), axis=0))))
    w = np.ones(len(pts)) if weights is None else np.maximum(np.asarray(weights, np.float64).reshape(-1), 1e-6)
    if len(pts) > sample:
        p = w / w.sum()
        ii = rnd.choice(len(pts), sample, replace=False, p=p)
        sub, wsub = pts[ii], w[ii]
    else:
        sub, wsub = pts, w
    cent = _pp(sub, wsub, k, rnd)
    for _ in range(iterations):
        d = dists(pts, cent)
        idx = d.argmin(1)
        new = cent.copy()
        worst = pts[int(d.min(1).argmax())]
        for j in range(k):
            m = idx == j
            new[j] = np.average(pts[m], axis=0, weights=w[m]) if m.any() else worst
        new = rgb_to_lab(snap_md(lab_to_rgb(new)))
        new = _dedupe(new, pts, w)
        if np.abs(new - cent).max() < 0.4:
            cent = new
            break
        cent = new
    idx = dists(pts, cent).argmin(1)
    pal = snap_md(lab_to_rgb(cent))
    return pal, idx.reshape(np.asarray(lab).shape[:-1])


def refine_palette(lab_flat, pal, k, weights=None, seed=0, sample=6000):
    """Guarantee k distinct grid colors: fill with the color that cuts error the most."""
    rnd = np.random.default_rng(seed)
    pts = np.asarray(lab_flat, np.float64).reshape(-1, 3)
    if len(pts) > sample:
        sub = pts[rnd.choice(len(pts), sample, replace=False)]
    else:
        sub = pts
    current = np.unique(np.asarray(pal, np.uint8), axis=0)
    if len(current) >= k:
        return current[:k]
    used = {tuple(int(v) for v in c) for c in current}
    cand = np.array([c for c in MD_GRID_RGB.astype(np.uint8) if tuple(int(v) for v in c) not in used])
    if not len(cand):
        return current
    d = dists(sub, rgb_to_lab(current)).min(1)
    while len(current) < k and len(cand):
        dc = dists(sub, rgb_to_lab(cand))
        new = np.minimum(d[:, None], dc)
        j = int((d[:, None] - new).sum(0).argmax())
        current = np.vstack([current, cand[j:j + 1]])
        d = new[:, j]
        cand = np.delete(cand, j, axis=0)
    return current


def border_key(rgb, border=2):
    """Border color: with transparency those pixels become index 0 (hole)."""
    h, w = rgb.shape[:2]
    px = np.concatenate([rgb[:border].reshape(-1, 3), rgb[-border:].reshape(-1, 3),
                         rgb[:, :border].reshape(-1, 3), rgb[:, -border:].reshape(-1, 3)])
    colors, count = np.unique(px, axis=0, return_counts=True)
    return colors[int(count.argmax())]


KEY_TOLERANCE = 3.0   # dE around the border colour that still counts as backdrop


def key_mask(rgb, key, tol=KEY_TOLERANCE):
    """Pixels that are the backdrop: the key colour, give or take tol (Lab dE)."""
    key = np.asarray(key, np.float64).reshape(1, 3)
    exact = np.all(rgb == key.astype(np.uint8), axis=2)
    if tol <= 0:
        return exact
    d = np.sqrt(dists(rgb_to_lab(rgb.reshape(-1, 3).astype(np.float64)),
                      rgb_to_lab(key))[:, 0]).reshape(rgb.shape[:2])
    return exact | (d <= tol)


def backdrop_mask(rgb, resp):
    """Backdrop pixels of the SOURCE, matched against the colour found on the
    border (key_src), not against its MD-grid snap. v1 compared with the snapped
    colour, so a 250,250,250 border (snapped to 255) marked no pixel at all.
    With key_holes False (the border is part of the art) there are no holes."""
    if resp.get("key_holes") is False:
        return np.zeros(rgb.shape[:2], bool)
    src = resp.get("key_src")
    return key_mask(rgb, src if src is not None else resp["key"])


KEY_BORDER_MIN = 0.85   # share of the border that must be the key colour


def backdrop_is_real(rgb, key_src, path=None):
    """Is the border colour a real backdrop, or just the edge of the art?

    v2.5 always turned the border colour into holes (index 0). On a picture
    whose edge is a sky or a gradient, the most common border colour is only
    one tone of it, and every pixel near that tone became a flat backdrop
    hole (and, with shadow, a dark block). A real backdrop is either
    transparency in the file or a border that is (almost) all one colour.
    Returns (real, share of the border that matches, reason).
    """
    if path is not None:
        try:
            if transparent_fraction(path) > 0.001:
                return True, 1.0, "transparent pixels in the file"
        except Exception:
            pass
    b = 2
    px = np.concatenate([rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                         rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3)])
    share = float(key_mask(px.reshape(-1, 1, 3), key_src).mean())
    if share >= KEY_BORDER_MIN:
        return True, share, f"{share:.0%} of the border is that colour"
    return False, share, (f"only {share:.0%} of the border is that colour: the "
                          "border is part of the art")


def order_palette(pal, idx):
    """Most used color at index 0 (the backdrop), the rest by luma."""
    count = np.bincount(idx.reshape(-1), minlength=len(pal))
    first = int(count.argmax())
    rest = [i for i in range(len(pal)) if i != first]
    order_luma = luma(np.asarray(pal, np.float64))
    rest.sort(key=lambda i: order_luma[i])
    order = [first] + rest
    remap = np.zeros(len(pal), np.uint8)
    for new, old in enumerate(order):
        remap[old] = new
    return np.asarray(pal, np.uint8)[order], remap[idx]


def arrange15(pal, idx):
    """No index 0 in the art: sort the 15 by luma."""
    order = np.argsort(luma(np.asarray(pal, np.float64)))
    remap = np.zeros(len(pal), np.uint8)
    remap[order] = np.arange(len(pal))
    return np.asarray(pal, np.uint8)[order], remap[idx]


def _nivel(c):
    """Which of the 8 MD levels a channel value is (0..7)."""
    return int(np.abs(LEVELS - float(c)).argmin())


def _do_nivel(n):
    return int(LEVELS[int(np.clip(n, 0, 7))])


def shadow_palette(pal):
    """What the VDP shows on a LOW priority tile: half brightness.

    Shadow is not a palette: it is the shadow/highlight circuit darkening the
    pixels of the tile that has priority 0. The DAC has 15 steps and a normal
    3-bit level n sits on step 2n; shadow shows step n. These colours are OFF
    the normal 8-level grid (v1 snapped them back onto it, e.g. level 7 gave
    116 instead of 130).
    """
    lv = np.vectorize(_nivel)(np.asarray(pal, int))
    return DAC15[lv].round().astype(np.uint8)


def highlight_palette(pal):
    """What a highlight pixel shows: DAC step n + 7 (halfway to white).

    Highlight is not a tile attribute: on hardware it needs a sprite pixel of
    PAL3 index 14 over the area. This table only measures what it would look
    like, so the tool can say where it would pay off. (v1 used n + n/2 on the
    normal grid: level 4 gave 206 instead of 187.)
    """
    lv = np.vectorize(_nivel)(np.asarray(pal, int))
    return DAC15[lv + 7].round().astype(np.uint8)


def variants(pal, mode):
    """Deprecated: shadow/highlight are not spare palettes, they are VDP effects.

    Kept so old profiles/samples keep loading; new code uses shadow_palette()
    and highlight_palette().
    """
    out = {"normal": np.asarray(pal, np.uint8)}
    if mode in ("shadow", "both"):
        out["shadow"] = shadow_palette(pal)
    if mode in ("highlight", "both"):
        out["highlight"] = highlight_palette(pal)
    return out


def mode_variants(mode):
    return {"normal": ["normal"], "shadow": ["shadow"], "highlight": ["highlight"],
            "both": ["normal", "shadow", "highlight"]}[mode]


# --------------------------------------------------------------------------
# shadow-aware planning (v2.4)
# --------------------------------------------------------------------------
# With shadow on, every palette p has a free second version: the same 16 CRAM
# slots shown at half brightness on a low priority tile. v2.3 and earlier chose
# the 15 colours, dithered and reduced as if shadow did not exist, and only at
# the end let each finished tile try the darker version. Now the darker version
# is a real option from the start: a tile picks one of 2n "effective" palettes
# (p normal or p shadowed), the dither runs inside that choice, the reduction
# compares tiles as they will be shown, and each palette is re-fitted to the
# pixels of its normal tiles plus the UN-shadowed pixels of its shadow tiles, so
# the 15 colours can leave the darks to the shadow.
# Effective tile_pal: 0..n-1 normal, n..2n-1 shadowed (real palette = tp % n).

def shadow_on(resp):
    """Shadow chosen together with the palettes (mode shadow or both)."""
    return (resp.get("mode") in ("shadow", "both")
            and not resp.get("shadow_late"))


def eff_pals(pals, resp):
    """The palettes a tile may pick: n normal, plus their shadow if planned."""
    base = [np.asarray(p, np.uint8) for p in pals]
    if shadow_on(resp):
        return base + [shadow_palette(p) for p in base]
    return base


def split_tile_pal(tile_pal, n):
    """Effective tile_pal -> (real palette 0..n-1, shadow mask or None)."""
    tp = np.asarray(tile_pal)
    if tp.size and int(tp.max()) >= n:
        return (tp % n).astype(np.uint8), tp >= n
    return tp.astype(np.uint8), None


def unshadow(rgb):
    """Normal-grid colour that the VDP would show as this colour when shadowed.

    Shadow shows DAC step n for level n (0..130); this inverts it, continuous,
    so the palette k-means can fit the colour the CRAM must hold. Values above
    the brightest shadow step (130) cannot be reached and stay at level 7.
    """
    x = np.interp(np.asarray(rgb, np.float64), DAC15[:8], np.arange(8.0))
    return np.interp(x, np.arange(8.0), LEVELS.astype(np.float64))


def shadow_equivalent(rgb, shadow_tiles):
    """The image with the pixels of shadow tiles replaced by unshadow()."""
    out = np.asarray(rgb, np.float64).copy()
    if shadow_tiles is not None and np.any(shadow_tiles):
        sel = np.repeat(np.repeat(np.asarray(shadow_tiles, bool), 8, 0), 8, 1)
        out[sel] = unshadow(out[sel])
    return out


SHADOW_SEED_MAX = 135   # a tile whose bright pixels fit the shadow range


def shadow_seed_tiles(rgb):
    """Tiles that can be drawn entirely in shadow: 95% of their channel
    values at or below the brightest shadow step. Seeds the first palettes."""
    h, w = rgb.shape[:2]
    nh, nw = h // 8, w // 8
    mx = np.asarray(rgb[:nh * 8, :nw * 8], np.float64).max(2)
    q = np.quantile(mx.reshape(nh, 8, nw, 8).transpose(0, 2, 1, 3).reshape(nh, nw, 64),
                    0.95, axis=2)
    return q <= SHADOW_SEED_MAX


# --------------------------------------------------------------------------
# painting (indices) and dithering
# --------------------------------------------------------------------------
SPECK_FLOOR = 0.10    # below this a pair is specks, not a tone
MIX_CEILING = 0.45    # ... and above this even a far pair blends
DITHER_FLOOR = 6.0    # dE a solid color may cost before a mix is worth it
FEW_PAIRS = 10        # candidate pairs tried per pixel before falling back

# How fast the required coverage grows with the pair distance. Two colors far
# apart need both well represented to read as a tone; two close ones blend even
# at 10%. "full" keeps the old flat floor and squeezes out every bit of colour,
# "clean" asks for more coverage and leaves flat areas clean.
DITHER_STRENGTHS = {"clean": (None, 10.0),
                    "balanced": (130.0, 8.0),
                    "full": (None, DITHER_FLOOR)}
DITHER_DEFAULT = "balanced"


def _ordered_mix(lab, lab_pal, shape, n, strength=DITHER_DEFAULT, matrix=None):
    """Ordered dither: per pixel, the PAIR whose mix lands closest to the target.

    The two nearest colors are not always the best mix. A light skin tone sits
    outside the MD grid: the nearest color is a greenish grey, but a warm tone
    and a grey mixed 2:1 land much closer while both stay far on their own. So
    the pair is chosen by the distance from the target to the segment between
    the two colors, and the ordered pattern turns the mix ratio into dots.
    Because the segment includes its ends, a pair is never worse than the
    single nearest color, which is the pair with ratio 0.

    A mix is only used when both colors cover enough of the cell. The eye reads
    a tone when the two are well represented, and reads specks when one of them
    is a few percent - the further apart they are, the sooner it looks like
    dirt. SPECK_FLOOR is the absolute minimum and the preset raises it with the
    pair distance. When no pair qualifies, the solid nearest color wins.
    """
    sep_ref, floor = DITHER_STRENGTHS.get(strength, DITHER_STRENGTHS[DITHER_DEFAULT])
    k = len(lab_pal)
    ii, jj = np.triu_indices(k, 1)
    u = lab_pal[ii] - lab_pal[jj]
    b = lab_pal[jj]
    uu = (u * u).sum(1)
    keep = uu > 1e-9
    ii, jj, u, b, uu = ii[keep], jj[keep], u[keep], b[keep], uu[keep]
    if sep_ref is None:
        need = np.full(len(ii), SPECK_FLOOR)
    else:
        need = np.clip(np.sqrt(uu) / sep_ref, SPECK_FLOOR, MIX_CEILING)
    bb = (b * b).sum(1)
    bu = (b * u).sum(1)
    bayer = matrix if matrix is not None else (BAYER4 if n == 4 else BAYER8)
    n = bayer.shape[0]
    yy = np.arange(shape[0])[:, None] % n
    xx = np.arange(shape[1])[None, :] % n
    thr = ((bayer[yy, xx] + 0.5) / (n * n)).reshape(-1)
    total = lab.shape[0]
    idx = np.empty(total, np.uint8)
    err = np.empty(total)
    for s in range(0, total, 8192):
        e = min(total, s + 8192)
        sub = lab[s:e]
        dd = dists(sub, lab_pal)
        d0 = np.sqrt(dd.min(1))
        solid = dd.argmin(1)
        l2 = (sub * sub).sum(1)[:, None]
        lu = sub @ u.T
        lb = sub @ b.T
        t = np.clip((lu - bu) / uu, 0.0, 1.0)
        d2 = l2 - 2.0 * (lb + t * lu) + (bb + 2.0 * t * bu + t * t * uu)
        row = np.arange(len(sub))
        order = np.argsort(d2, axis=1)[:, :FEW_PAIRS]
        pick = np.full(len(sub), -1, int)
        for c in range(order.shape[1]):
            cand = order[:, c]
            tk = t[row, cand]
            ok = (np.minimum(tk, 1.0 - tk) >= need[cand]) & (pick < 0)
            pick = np.where(ok, cand, pick)
        live = pick >= 0
        pick = np.maximum(pick, 0)
        tk = t[row, pick]
        mix = np.sqrt(np.maximum(d2[row, pick], 0.0))
        out = np.where(tk > thr[s:e], ii[pick], jj[pick])
        flat = d0 <= floor
        out = np.where(live, out, solid)
        out = np.where(flat, solid, out)
        idx[s:e] = out
        err[s:e] = np.where(live & ~flat, mix, d0)
    return idx.reshape(shape), err.reshape(shape)


ATKINSON_KERNEL = (
    (1, 0, 1 / 8), (2, 0, 1 / 8),
    (-1, 1, 1 / 8), (0, 1, 1 / 8), (1, 1, 1 / 8),
    (0, 2, 1 / 8),
)

STUCKI_KERNEL = (
    (1, 0, 8 / 42), (2, 0, 4 / 42),
    (-2, 1, 2 / 42), (-1, 1, 4 / 42), (0, 1, 8 / 42),
    (1, 1, 4 / 42), (2, 1, 2 / 42),
    (-2, 2, 1 / 42), (-1, 2, 2 / 42), (0, 2, 4 / 42),
    (1, 2, 2 / 42), (2, 2, 1 / 42),
)


def _diffusion_strength(strength):
    return {"clean": 0.65, "balanced": 0.85, "full": 1.0}.get(strength, 0.85)


FLOYD_KERNEL = ((1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16))

# How far (in Lab units) the diffused value may wander outside the box spanned
# by the palette. Without a bound, an area the palette cannot represent keeps
# pushing error forward until the value saturates and the output turns into
# extreme colours (v1 behaviour on riemersma/stucki "full").
DIFFUSE_MARGIN = 12.0


@_njit(cache=True)
def _nearest(p0, p1, p2, pal):
    best, bi = 1e30, 0
    for i in range(pal.shape[0]):
        d = (pal[i, 0] - p0) ** 2 + (pal[i, 1] - p1) ** 2 + (pal[i, 2] - p2) ** 2
        if d < best:
            best, bi = d, i
    return bi


@_njit(cache=True)
def _diffuse_core(buf, pal, kern, strength, lo, hi):
    h, w = buf.shape[0], buf.shape[1]
    out = np.zeros((h, w), np.uint8)
    for y in range(h):
        ltr = (y % 2 == 0)
        for k in range(w):
            x = k if ltr else w - 1 - k
            for c in range(3):
                if buf[y, x, c] < lo[c]:
                    buf[y, x, c] = lo[c]
                elif buf[y, x, c] > hi[c]:
                    buf[y, x, c] = hi[c]
            i = _nearest(buf[y, x, 0], buf[y, x, 1], buf[y, x, 2], pal)
            out[y, x] = i
            e0 = (buf[y, x, 0] - pal[i, 0]) * strength
            e1 = (buf[y, x, 1] - pal[i, 1]) * strength
            e2 = (buf[y, x, 2] - pal[i, 2]) * strength
            for j in range(kern.shape[0]):
                dx = int(kern[j, 0])
                if not ltr:
                    dx = -dx
                nx = x + dx
                ny = y + int(kern[j, 1])
                if 0 <= nx < w and ny < h:
                    wt = kern[j, 2]
                    buf[ny, nx, 0] += e0 * wt
                    buf[ny, nx, 1] += e1 * wt
                    buf[ny, nx, 2] += e2 * wt
    return out


def _bounds(lab_pal):
    lab_pal = np.asarray(lab_pal, np.float64)
    return lab_pal.min(0) - DIFFUSE_MARGIN, lab_pal.max(0) + DIFFUSE_MARGIN


def _diffuse(lab_flat, lab_pal, shape, kernel, strength=0.85):
    """Serpentine error diffusion with a fixed kernel, error bounded by the palette."""
    h, w = shape
    buf = np.ascontiguousarray(lab_flat.reshape(h, w, 3), np.float64).copy()
    pal = np.ascontiguousarray(lab_pal, np.float64)
    lo, hi = _bounds(pal)
    kern = np.asarray(kernel, np.float64)
    return _diffuse_core(buf, pal, kern, float(strength), lo, hi)


def _hilbert_order(shape):
    """Indices of all pixels sorted along a Hilbert curve."""
    h, w = shape
    n = 1
    while n < max(h, w):
        n <<= 1
    ys, xs = np.indices((h, w))
    x = xs.copy()
    y = ys.copy()
    d = np.zeros((h, w), np.int64)
    s = n >> 1
    while s:
        rx = (x & s) != 0
        ry = (y & s) != 0
        d += (s * s) * ((3 * rx.astype(np.int64)) ^ ry.astype(np.int64))
        rot = (~ry) & rx
        x[rot] = n - 1 - x[rot]
        y[rot] = n - 1 - y[rot]
        sw = ~ry
        x2 = np.where(sw, y, x)
        y2 = np.where(sw, x, y)
        x, y = x2, y2
        s >>= 1
    return np.argsort(d.ravel(), kind="stable")


RIEMERSMA_QUEUE = 16
RIEMERSMA_RATIO = 1.0 / 16.0   # weight of the oldest entry vs the newest


@_njit(cache=True)
def _riemersma_core(lab, order, pal, weights, strength, lo, hi):
    n = order.shape[0]
    q = weights.shape[0]
    queue = np.zeros((q, 3))
    out = np.zeros(lab.shape[0], np.uint8)
    head = 0                      # queue[head] is the newest entry
    for t in range(n):
        f = order[t]
        p = np.empty(3)
        for c in range(3):
            acc = 0.0
            for j in range(q):
                acc += queue[(head + j) % q, c] * weights[j]
            v = lab[f, c] + acc * strength
            if v < lo[c]:
                v = lo[c]
            elif v > hi[c]:
                v = hi[c]
            p[c] = v
        i = _nearest(p[0], p[1], p[2], pal)
        out[f] = i
        head = (head - 1) % q
        for c in range(3):
            # Riemersma's queue holds the error of the ORIGINAL pixel; v1 pushed
            # the corrected value, so every error was counted again and again.
            queue[head, c] = lab[f, c] - pal[i, c]
    return out


def _riemersma(lab_flat, lab_pal, shape, strength=0.85, queue_len=RIEMERSMA_QUEUE):
    """Riemersma dither: error queue along a Hilbert curve.

    Newest error weighs 1, the oldest RIEMERSMA_RATIO, exponential in between,
    as in the original paper. The weights are scaled to sum 1 so "strength"
    keeps the same meaning as in the kernel diffusions.
    """
    h, w = shape
    lab = np.ascontiguousarray(lab_flat.reshape(-1, 3), np.float64)
    pal = np.ascontiguousarray(lab_pal, np.float64)
    order = np.ascontiguousarray(_hilbert_order(shape), np.int64)
    r = RIEMERSMA_RATIO ** (1.0 / (queue_len - 1))
    weights = r ** np.arange(queue_len)
    weights = weights / weights.sum()
    lo, hi = _bounds(pal)
    return _riemersma_core(lab, order, pal, weights, float(strength), lo, hi).reshape(h, w)


def _box3(img):
    """3x3 box blur of an (h, w, c) image, edge padded."""
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    h, w = img.shape[:2]
    return sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)) / 9.0


def _dither_error(lab_flat, lab_pal, idx, shape):
    """Error a dithered result really leaves, as the eye mixes it (3x3 blur)."""
    h, w = shape
    src = _box3(lab_flat.reshape(h, w, 3))
    got = _box3(np.asarray(lab_pal, np.float64)[idx.reshape(h, w)])
    return np.sqrt(((src - got) ** 2).sum(2))


def _paint_lab_err(lab, lab_pal, shape, mode, strength=DITHER_DEFAULT):
    """Paint the pixels and return the error each one is left with."""
    dd = dists(lab, lab_pal)
    row = np.arange(len(dd))
    if mode == "none":
        i0 = dd.argmin(1)
        return i0.reshape(shape), np.sqrt(dd[row, i0]).reshape(shape)
    if mode.startswith("ordered"):
        return _ordered_mix(lab, lab_pal, shape, 4 if mode.endswith("4") else 8,
                            strength)
    if mode == "halftone":
        return _ordered_mix(lab, lab_pal, shape, 4, strength, matrix=HALFTONE4)
    if mode == "bluenoise":
        return _ordered_mix(lab, lab_pal, shape, 16, strength, matrix=BLUE16)
    s = _diffusion_strength(strength)
    if mode == "atkinson":
        out = _diffuse(lab, lab_pal, shape, ATKINSON_KERNEL, s)
    elif mode == "stucki":
        out = _diffuse(lab, lab_pal, shape, STUCKI_KERNEL, s)
    elif mode == "riemersma":
        out = _riemersma(lab, lab_pal, shape, s)
    else:
        out = floyd(lab, lab_pal, shape, s)
    # v1 returned the nearest-colour error here, so a tile chose its palette
    # by a number that had nothing to do with the dithered pixels it received
    return out, _dither_error(lab, lab_pal, out, shape)


def _paint_lab(lab, lab_pal, shape, mode, strength=DITHER_DEFAULT):
    return _paint_lab_err(lab, lab_pal, shape, mode, strength)[0]


def floyd(lab_flat, lab_pal, shape, strength=0.85):
    """Floyd-Steinberg, serpentine, through the shared bounded diffusion core."""
    return _diffuse(lab_flat, lab_pal, shape, FLOYD_KERNEL, strength)


def bands_of(h, n):
    if n <= 1:
        return [(0, h)]
    step = max(8, (h // 8 // n) * 8)
    cuts = [0]
    for _ in range(n - 1):
        cuts.append(min(h - 8, cuts[-1] + step))
    cuts.append(h)
    return [(cuts[i], cuts[i + 1]) for i in range(n) if cuts[i + 1] > cuts[i]]


def paint(rgb, pals, resp, bands):
    h, w = rgb.shape[:2]
    base = 1
    idx = np.zeros((h, w), np.uint8)
    for i, (y0, y1) in enumerate(bands):
        sub = rgb[y0:y1]
        lab = rgb_to_lab(sub).reshape(-1, 3)
        colors = np.asarray(pals[i], np.uint8)[base:]
        idx[y0:y1] = _paint_lab(lab, rgb_to_lab(colors), sub.shape[:2], resp["dithering"],
                                resp.get("dither_strength", DITHER_DEFAULT)) + base
    if resp.get("key") is not None:
        idx[backdrop_mask(rgb, resp)] = 0
    band_tile = np.array([next(i for i, (a, b) in enumerate(bands) if a <= cy * 8 < b)
                          for cy in range(h // 8)], np.uint8)
    return idx, band_tile


# --------------------------------------------------------------------------
# tiles
# --------------------------------------------------------------------------
def to_tiles(idx):
    h, w = idx.shape
    t = idx.reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3).reshape(-1, 8, 8)
    return np.ascontiguousarray(t, np.uint8)


def from_tiles(t, h, w):
    nh, nw = h // 8, w // 8
    return t.reshape(nh, nw, 8, 8).transpose(0, 2, 1, 3).reshape(h, w)


def dedup(tiles):
    """Equal and mirrored (H, V, HV) tiles become one; the map keeps the mirror."""
    bank, uniq, tilemap = {}, [], []
    for t in tiles:
        found = None
        for hf in (0, 1):
            for vf in (0, 1):
                tt = t
                if hf:
                    tt = tt[:, ::-1]
                if vf:
                    tt = tt[::-1, :]
                i = bank.get(np.ascontiguousarray(tt).tobytes())
                if i is not None:
                    found = (i, hf, vf)
                    break
            if found:
                break
        if found is None:
            i = len(uniq)
            bank[np.ascontiguousarray(t).tobytes()] = i
            uniq.append(np.ascontiguousarray(t))
            found = (i, 0, 0)
        tilemap.append(found)
    return np.array(uniq, np.uint8), np.array(tilemap, np.int32).reshape(-1, 3)


def rebuild(uniq, tilemap, h, w):
    nh, nw = h // 8, w // 8
    out = np.empty((nh, nw, 8, 8), np.uint8)
    for cy in range(nh):
        for cx in range(nw):
            i, hf, vf = tilemap[cy * nw + cx]
            t = uniq[i]
            if hf:
                t = t[:, ::-1]
            if vf:
                t = t[::-1, :]
            out[cy, cx] = t
    return out.transpose(0, 2, 1, 3).reshape(h, w)


def _kmeans_rgb(X, k, seed=0, iterations=10):
    rnd = np.random.default_rng(seed)
    k = min(k, len(X))
    cent = X[rnd.choice(len(X), k, replace=False)].copy()
    for _ in range(iterations):
        d = np.maximum((X * X).sum(1)[:, None] + (cent * cent).sum(1)[None, :] - 2 * X @ cent.T, 0)
        m = d.argmin(1)
        new = cent.copy()
        for j in range(k):
            sel = m == j
            if sel.any():
                new[j] = X[sel].mean(0)
        if np.abs(new - cent).max() < 0.5:
            cent = new
            break
        cent = new
    d = np.maximum((X * X).sum(1)[:, None] + (cent * cent).sum(1)[None, :] - 2 * X @ cent.T, 0)
    return cent, d.argmin(1)


# One view = 320 x VIEW_H px. NTSC H40 shows 224 lines (28 tiles); PAL V30
# shows 240 (30 tiles). v1 always measured 240 while the generated C code sets
# 224, so the NTSC cap was 2 tile rows stricter than the screen. Default 224;
# --view-height 240 for PAL.
WIN_W, WIN_H = 40, 28


def set_view_height(px):
    global WIN_H
    if px not in (224, 240):
        raise SystemExit("view height must be 224 (NTSC) or 240 (PAL)")
    WIN_H = px // 8


def view_label():
    return f"320x{WIN_H * 8}"
WIN_STEP = 1                 # one tile = 8 px per slide, both axes


def windows_of(nh, nw):
    """Every complete 320x240 view, sliding one tile (8 px) at a time.

    Coordinates are tile ranges (x0, x1, y0, y1), end exclusive. The window
    never leaves the image: the last origin is nh - WIN_H / nw - WIN_W. On an
    axis smaller than the window the whole axis is used (a single position).
    """
    ww, wh = min(WIN_W, nw), min(WIN_H, nh)
    for y0 in range(0, nh - wh + 1, WIN_STEP):
        for x0 in range(0, nw - ww + 1, WIN_STEP):
            yield x0, x0 + ww, y0, y0 + wh


def tile_ids(idx):
    """Canonical tile id per position, after mirror dedup.

    Only the 4-bit colour index matters: palette (PNG bits 4-5) and
    normal/shadow priority (bit 7) are map attributes, not part of the drawing.
    """
    _, tilemap = dedup(to_tiles(np.asarray(idx, np.uint8) & 0x0F))
    return tilemap[:, 0]


def window_peak(ids):
    """Worst sliding 320x240 view of an id grid.

    Returns (unique tiles, x0, y0, x1, y1, windows) in tile coordinates, end
    exclusive. If the image is smaller than a view on an axis, the whole image
    is the only view.
    """
    ids = np.asarray(ids)
    nh, nw = ids.shape
    best = (-1, 0, 0, 0, 0)
    nwin = 0
    for x0, x1, y0, y1 in windows_of(nh, nw):
        n = int(np.unique(ids[y0:y1, x0:x1]).size)
        nwin += 1
        if n > best[0]:
            best = (n, x0, y0, x1, y1)
    if nwin == 0:
        return (int(np.unique(ids).size), 0, 0, nw, nh, 1)
    return best + (nwin,)


def window_peak_of(idx):
    """Worst sliding 320x240 view of the 4-bit index image."""
    h, w = idx.shape
    return window_peak(tile_ids(idx).reshape(h // 8, w // 8))


def max_window_tiles(idx):
    """Worst view: the most unique tiles inside a 320x240 sliding window."""
    return window_peak_of(idx)[0]


def count_for_cap(idx, resp):
    """The number the tile cap is compared against: whole image or one view."""
    if resp.get("cap_scope", "image") == "view":
        return max_window_tiles(idx)
    return len(dedup(to_tiles(idx))[0])


def snap_colors(cols):
    """Snap a list of colors to the MD grid without building a huge matrix."""
    out = []
    for i in range(0, len(cols), 4096):
        c = cols[i:i + 4096]
        lab = rgb_to_lab(c.astype(np.float64))
        out.append(MD_GRID_RGB[dists(lab, MD_GRID_LAB).argmin(1)])
    return np.concatenate(out) if out else cols


def preflight(rgb):
    """What the image asks for, before any question is asked.

    Colors: distinct RGB values in the file and how many survive the snap to the
    512-color MD grid - the second one is the real ceiling for the conversion.
    Tiles: how many 8x8 tiles the image has and the worst 320x240 sliding view
    of the image, which is the number the tile cap has to deal with.
    """
    h, w = rgb.shape[:2]
    u = rgb.astype(np.uint32)
    packed = (u[..., 0] << 16) | (u[..., 1] << 8) | u[..., 2]
    uniq = np.unique(packed)
    cores = int(uniq.size)

    cols = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], 1).astype(np.uint8)
    sn = snap_colors(cols).astype(np.uint32)
    cores_md = int(np.unique((sn[:, 0] << 16) | (sn[:, 1] << 8) | sn[:, 2]).size)

    nh, nw = h // 8, w // 8
    blk = rgb[:nh * 8, :nw * 8].reshape(nh, 8, nw, 8, 3).transpose(0, 2, 1, 3, 4)
    flat = np.ascontiguousarray(blk).reshape(nh * nw, 192)
    uniq_tiles, ids = np.unique(flat, axis=0, return_inverse=True)
    ids = ids.reshape(nh, nw)

    # mirror dedup on the source drawing: H, V and HV variants share one id
    canon, canon_of = {}, np.empty(len(uniq_tiles), np.int64)
    for k, u in enumerate(uniq_tiles):
        t = u.reshape(8, 8, 3)
        variants = (t, t[:, ::-1], t[::-1], t[::-1, ::-1])
        key = min(v.tobytes() for v in variants)
        if key not in canon:
            canon[key] = len(canon)
        canon_of[k] = canon[key]
    ids = canon_of[ids]

    pior, px0, py0, px1, py1, nwin = window_peak(ids)
    return {"cores": cores, "cores_md": cores_md, "tiles": nh * nw,
            "tiles_unicos": len(canon),
            "pior_tela": pior, "pior_x0": px0, "pior_y0": py0,
            "pior_x1": px1, "pior_y1": py1, "janelas": nwin,
            "w": w, "h": h}


def print_preflight(info):
    print(f"\nimage: {info['w']}x{info['h']} px")
    print(f"  colors: {info['cores']} in the file \u00b7 {info['cores_md']} on the MD grid")
    if info["tiles"] == info["pior_tela"]:
        print(f"  tiles: {info['tiles']} slots \u00b7 worst {view_label()} view has "
              f"{info['pior_tela']} unique")
    else:
        print(f"  tiles: {info['tiles']} slots in the image \u00b7 worst {view_label()} view has "
              f"{info['pior_tela']} unique")
    if "tiles_unicos" in info:
        print(f"  unique tiles in the whole image: {info['tiles_unicos']} "
              f"(mirrors deduped)")
    print(f"    sliding {view_label()} window, 8 px steps, {info['janelas']} windows, "
          f"mirrors deduped: peak at x={info['pior_x0'] * 8}..{info['pior_x1'] * 8} "
          f"y={info['pior_y0'] * 8}..{info['pior_y1'] * 8} px")
    print("    (source drawing estimate; report.md measures the converted "
          "4-bit indices)")


def _vars4(t):
    """The four mirror variants: normal, H, V, HV."""
    return (t, t[:, ::-1], t[::-1], t[::-1, ::-1])


MERGE_BATCH = 16   # merges per view per pass; the next pass recomputes neighbours
_DEBUG_REDUCE = bool(os.environ.get("MD_REDUCE_DEBUG"))
VIEW_SPREAD_X = 20   # tiles between the views scored in one pass (half a view)
MERGE_NEIGHBOURS = 4   # nearest partners per pattern rescored with the group cost


def _blur8(x):
    """3x3 box blur of an (N, 8, 8, C) array, edge padded."""
    p = np.pad(x, ((0, 0), (1, 1), (1, 1), (0, 0)), mode="edge")
    return sum(p[:, dy:dy + 8, dx:dx + 8]
               for dy in range(3) for dx in range(3)) / 9.0


def reduce_tiles_windows(idx, pals, tile_pal, budget, seed=0, space_w=100.0):
    """Merge tiles until every 320x240 sliding view fits the budget.

    Views slide 8 px at a time, so the same tile belongs to many views. A tile
    pattern is merged into another globally, choosing the best mirror: the
    drawing disappears from every view at once, so a view can only lose a
    distinct tile, never gain one. That makes the cap monotonic and guarantees
    every camera position, not just fixed blocks.

    The merge cost is not just the average Lab distance. It also penalizes:
    - a different number of index-0 (backdrop/key) pixels, which otherwise
      clones white holes into dark areas;
    - a different edge energy, which otherwise clones text/logo into clouds;
    - a different palette, which would paint the target indices with the
      wrong colours;
    - distance between the two patterns inside the image, which otherwise
      clones a block from the other side of the screen.
    A blurred term keeps the decision on what the eye sees instead of on the
    dither noise. Palette and normal/shadow priority stay per map position.
    """
    h, w = idx.shape
    nh, nw = h // 8, w // 8
    idx4 = np.ascontiguousarray(idx, np.uint8) & 0x0F
    uniq, tilemap = dedup(to_tiles(idx4))
    npat = len(uniq)
    mapa = tilemap[:, 0].reshape(nh, nw)
    mfl = (tilemap[:, 1] | (tilemap[:, 2] << 1)).reshape(nh, nw)

    # Every pattern drawn with EVERY palette (Vp[palette, mirror, pattern]).
    # A merged position keeps its own palette, so pattern b must be judged as
    # it will look in a's palette. v1 drew each pattern only in its own most
    # used palette: the same indices that are blue in one palette are white in
    # another, and its PAL_W penalty (20000) was tiny next to the distances
    # (~10^5), so blue dress tiles came back white.
    # v1 also built the HV mirror as lp[::-1, ::-1, :], which reversed the
    # ORDER OF THE PATTERNS instead of the rows: every HV comparison was made
    # against some other tile.
    pal_arr = np.asarray(pals, np.uint8)
    npal = len(pal_arr)
    uses = np.zeros((npat, npal), np.int32)
    np.add.at(uses, (mapa.ravel(), tile_pal.reshape(-1)), 1)
    pal_mode = uses.argmax(1)
    Vp = np.empty((npal, 4, npat, 192), np.float32)
    for p in range(npal):
        lp = pal_arr[p][uniq].astype(np.float64)
        lp = rgb_to_lab(lp.reshape(-1, 3)).reshape(npat, 8, 8, 3)
        lp = lp + 1.0 * _blur8(lp)
        Vp[p, 0] = lp.reshape(npat, -1)
        Vp[p, 1] = lp[:, :, ::-1].reshape(npat, -1)
        Vp[p, 2] = lp[:, ::-1, :].reshape(npat, -1)
        Vp[p, 3] = lp[:, ::-1, ::-1].reshape(npat, -1)

    def feat(k, f, p):
        """Pattern(s) k under mirror(s) f drawn in palette(s) p."""
        return Vp[p, f, k]

    # pattern statistics used by the merge penalty
    zero = (uniq == 0).sum(axis=(1, 2)).astype(np.float32)
    grid = uniq.astype(np.float32)
    edge = (np.abs(np.diff(grid, axis=2)).mean(axis=(1, 2))
            + np.abs(np.diff(grid, axis=1)).mean(axis=(1, 2)))
    flat = mapa.ravel()
    cnt = np.bincount(flat, minlength=npat).astype(np.float32)
    yy = np.repeat(np.arange(nh, dtype=np.float32), nw)
    xx = np.tile(np.arange(nw, dtype=np.float32), nh)
    cy = np.bincount(flat, weights=yy, minlength=npat) / np.maximum(cnt, 1)
    cx = np.bincount(flat, weights=xx, minlength=npat) / np.maximum(cnt, 1)

    KEY_W, EDGE_W = 5000.0, 300.0
    SPACE_W = float(space_w)
    edge_q = float(np.quantile(edge, 0.90))
    zero_q = float(np.quantile(zero, 0.90))
    # Text, logos and white details are expensive to lose: make them the last
    # sources chosen for the merge, so the reduction eats flat clouds first.
    importance = (1.0 + 6.0 * (edge >= edge_q)
                  + 6.0 * (zero >= zero_q)).astype(np.float32)


    parent = np.arange(npat)
    flip = np.zeros(npat, np.int32)
    alive = np.ones(npat, bool)
    # every live pattern carries the ORIGINAL patterns merged into it, with the
    # mirror each one needs to be drawn from the root
    members = {k: [k] for k in range(npat)}
    mflip = np.zeros(npat, np.int32)
    cnt0 = cnt.copy()

    gc_cache = {}

    def group_cost(a, b, f):
        """Mean distance (per use) from every original pattern drawn by root a
        to root b's drawing under mirror f: the damage the merge really does.
        Comparing only the two roots let a chain drift: light grey -> grey ->
        dark grey -> black, each step small, the arm turned black."""
        key = (a, b, f)
        if key in gc_cache:
            return gc_cache[key]
        ms = np.asarray(members[a], np.int64)
        fs = mflip[ms] ^ f
        pm = pal_mode[ms]
        d = ((feat(ms, 0, pm) - feat(np.full(len(ms), b), fs, pm)) ** 2).sum(1)
        w = cnt0[ms]
        gc_cache[key] = v = float((d * w).sum() / max(float(w.sum()), 1e-9))
        return v

    def resolve(k):
        f = 0
        while parent[k] != k:
            f ^= int(flip[k])
            k = int(parent[k])
        return int(k), f

    def apply():
        r = [resolve(k) for k in range(npat)]
        rk = np.array([x[0] for x in r], np.int32)
        rf = np.array([x[1] for x in r], np.int32)
        return rk[mapa], rf[mapa]

    passes = 0
    _t0 = time.time()
    while True:
        m2, f2 = apply()
        views, over = [], []
        for x0, x1, y0, y1 in windows_of(nh, nw):
            ids = set(m2[y0:y1, x0:x1].ravel())
            views.append((x0, x1, y0, y1, ids))
            if len(ids) > budget:
                over.append(len(views) - 1)
        if not over:
            break
        passes += 1

        cand = np.array(sorted(set().union(*(views[k][4] for k in over))), np.int32)
        cand = cand[alive[cand]]
        if len(cand) < 2:
            break
        # Distances are computed per scored view (at most one view of tiles,
        # 1120x1120), not for every candidate pair: on a wide pan the full
        # matrix had 7000+ x 7000+ cells per mirror, ran out of memory and
        # pushed the machine into swapping for minutes per pass.
        def pair_dist(ci):
            """dist[x, y]: candidate ci[x] replaced by ci[y] (both drawn in
            ci[x]'s palette, best mirror) plus the key/edge/space penalty."""
            ks = cand[ci]
            n = len(ks)
            dd = np.full((n, n), np.inf, np.float32)
            ff = np.zeros((n, n), np.int32)
            pc = pal_mode[ks]
            for p in np.unique(pc):
                rows = np.nonzero(pc == p)[0]
                A = Vp[p, 0, ks[rows]]
                for f in range(4):
                    B = Vp[p, f, ks]
                    d = (A * A).sum(1)[:, None] + (B * B).sum(1)[None, :] - 2.0 * A @ B.T
                    sub_d, sub_f = dd[rows], ff[rows]
                    m = d < sub_d
                    dd[rows] = np.where(m, d, sub_d)
                    ff[rows] = np.where(m, f, sub_f)
            zc, ec = zero[ks], edge[ks]
            dy = cy[ks][:, None] - cy[ks][None, :]
            dx = cx[ks][:, None] - cx[ks][None, :]
            dd += (KEY_W * np.abs(zc[:, None] - zc[None, :])
                   + EDGE_W * np.abs(ec[:, None] - ec[None, :])
                   + SPACE_W * np.sqrt(dy * dy + dx * dx))
            dd[np.arange(n), np.arange(n)] = np.inf
            dd[:, ~alive[ks]] = np.inf
            return dd, ff

        # Pair choice (v2.1). v1 ranked merges by distance x number of uses, so
        # a tile used once - a detail of an arm or a face - was the cheapest
        # thing to destroy, and it went to whatever partner was left: on the Rei
        # image a 1.3 dE tile became an 82 dE one. It also took the first of an
        # UNSORTED list of 8 global neighbours that happened to be in the view.
        # Now every pattern looks for its nearest partner among ALL the live
        # patterns of the view, the merge is ranked by how different the two
        # drawings are (the use count only breaks ties), the rarer of the two is
        # the one replaced, and a pass takes a small batch so the next pass
        # recomputes the neighbours instead of forcing worse leftovers.
        col = {int(p): i for i, p in enumerate(cand)}
        pairs = []
        # Views slide 8 px, so neighbouring views share almost every tile: a
        # wide pan has thousands of them and scoring each one made a single
        # pass take minutes. Per pass, score only over-budget views at least
        # half a view apart (fullest first); the rest are caught next pass.
        spread = []
        for vi in sorted(over, key=lambda v: -len(views[v][4])):
            x0, _, y0, _ = views[vi][:4]
            if all(abs(x0 - views[u][0]) >= VIEW_SPREAD_X
                   or abs(y0 - views[u][2]) >= max(1, WIN_H // 2) for u in spread):
                spread.append(vi)
        for vi in spread:
            x0, x1, y0, y1, ids = views[vi]
            ins = np.array(sorted(col[int(p)] for p in ids if alive[p] and int(p) in col),
                           np.int64)
            if len(ins) < 2:
                continue
            batch = max(1, min(len(ins) - budget, MERGE_BATCH))
            sub, subf = pair_dist(ins)
            kk = min(MERGE_NEIGHBOURS, len(ins) - 1)
            near = np.argpartition(sub, kk - 1, axis=1)[:, :kk]
            props = []
            for r, ai in enumerate(ins):
                best_p = None
                for c in near[r]:
                    bi = int(ins[c])
                    if not np.isfinite(sub[r, c]):
                        continue
                    for x, y in ((r, int(c)), (int(c), r)):   # both directions
                        a, b = int(cand[ins[x]]), int(cand[ins[y]])
                        f = int(subf[x, y])
                        pa = int(pal_mode[a])
                        raw = float(((feat(a, 0, pa) - feat(b, f, pa)) ** 2).sum())
                        pen = float(sub[x, y]) - raw   # key/edge/space/palette
                        cost = ((group_cost(a, b, f) + pen) * float(importance[a])
                                * (1.0 + 0.05 * np.log1p(float(cnt[a]))))
                        if best_p is None or cost < best_p[0]:
                            best_p = (cost, a, b, f)
                if best_p is not None:
                    props.append(best_p)
            props.sort()
            taken, used_v = 0, set()
            for cost, a, b, f in props:
                if taken >= batch:
                    break
                if a in used_v or b in used_v:
                    continue
                pairs.append((cost, a, b, f))
                used_v.update((a, b))
                taken += 1

        if _DEBUG_REDUCE:
            print(f"    [reduce] pass {passes}: {len(over)} views over, "
                  f"{len(spread)} scored, {len(cand)} candidates, "
                  f"{len(pairs)} pairs, peak {max(len(v[4]) for v in views)}, "
                  f"{time.time() - _t0:.0f}s", file=sys.stderr, flush=True)
        if not pairs:
            break
        pairs.sort()
        used = set()
        # v1 applied every proposal of every over-budget view at once. Views
        # overlap, so the merges added up far past the budget (cap 300 ended
        # at a 196 peak). Now each merge is counted against the live views and
        # is only taken while a view that holds both patterns is still over.
        live = [set(v[4]) for v in views]
        for cost, a, b, f in pairs:
            if not alive[a] or not alive[b] or a in used or b in used:
                continue
            helps = any(len(s) > budget and a in s and b in s for s in live)
            if not helps:
                continue
            parent[a] = b
            flip[a] = f
            alive[a] = False
            cnt[b] += cnt[a]
            for m in members[a]:
                mflip[m] ^= f
            members[b].extend(members.pop(a))
            for key in [k for k in gc_cache if k[0] in (a, b) or k[1] in (a, b)]:
                del gc_cache[key]
            used.update((a, b))
            for s in live:
                if a in s:
                    s.discard(a)
                    s.add(b)
        if passes > 4 * npat:
            break

    m2, f2 = apply()
    rec = np.empty((nh, nw, 8, 8), np.uint8)
    for r in range(nh):
        for c in range(nw):
            # mfl is how this position mirrors its own pattern; f2 is how the
            # pattern mirrors the one it was merged into. v1 dropped mfl, so
            # every mirrored copy came back unmirrored, merged or not.
            rec[r, c] = _vars4(uniq[m2[r, c]])[f2[r, c] ^ mfl[r, c]]
    out_idx = rec.transpose(0, 2, 1, 3).reshape(nh * 8, nw * 8)
    return np.ascontiguousarray(out_idx, np.uint8), tile_pal.copy()


LOCAL_SPACE_W = 1500.0   # sprite mode: distance penalty 15x the default


def reduce_tiles_windows_local(idx, pals, tile_pal, budget, seed=0):
    """View reduction for sprites/transparent objects: merges stay local.

    A merge that reuses a tile from far away is what destroys an arm or a face.
    v1 ran a k-means per over-budget view, one view after the other: centroids
    of dithered tiles became flat blocks, and every reduced view changed its
    neighbours, which were then reduced again (cap 300 ended at a 151-213
    peak). Now it is the same budget-aware global merge, with the distance
    penalty raised so a replacement tile comes from the same area.
    """
    return reduce_tiles_windows(idx, pals, tile_pal, budget, seed, space_w=LOCAL_SPACE_W)


def reduce_tiles(idx, pals, tile_pal, budget, seed=0):
    """8x8 block VQ that keeps real tiles.

    Clustering runs on what the eye sees (Lab plus a blurred copy, the same
    feature as the global merge), and every cluster is replaced by its MEDOID:
    the member tile closest to the centre, with its own pixels and palette.
    v1 used the centroid itself: the average of dithered tiles is a flat
    in-between tone, repainted without dither, which produced the flat grey
    8x8 blocks. A medoid is a pattern that already exists, so a reduced view
    never hands its neighbours a brand new drawing either.
    """
    h, w = idx.shape
    nh, nw = h // 8, w // 8
    T = to_tiles(idx)
    if budget >= len(dedup(T)[0]):
        return idx, tile_pal
    flat = tile_pal.reshape(-1)
    pal_arr = np.asarray(pals, np.float64)
    rgbt = pal_arr[flat[:, None, None], T]
    lab = rgb_to_lab(rgbt.reshape(-1, 3)).reshape(len(T), 8, 8, 3)
    X = (lab + _blur8(lab)).reshape(len(T), 192)
    C, tilemap = _kmeans_rgb(X, budget, seed)
    Tn = np.zeros((len(C), 8, 8), np.uint8)
    Cpal = np.zeros(len(C), np.uint8)
    for j in range(len(C)):
        members = np.nonzero(tilemap == j)[0]
        if not len(members):
            continue
        d = ((X[members] - C[j]) ** 2).sum(1)
        m = int(members[int(d.argmin())])
        Tn[j] = T[m]
        Cpal[j] = flat[m]
    new_idx = from_tiles(np.ascontiguousarray(Tn[tilemap], np.uint8), h, w)
    return new_idx, Cpal[tilemap].reshape(nh, nw)





def metrics(ref_rgb, new_rgb):
    a = rgb_to_lab(ref_rgb.reshape(-1, 3))
    b = rgb_to_lab(new_rgb.reshape(-1, 3))
    de = np.sqrt(((a - b) ** 2).sum(1))
    return {"de_mean": float(de.mean()), "de_p95": float(np.percentile(de, 95)),
            "de_max": float(de.max())}


PERCEIVED_SIGMA = 1.0   # px: how far the eye mixes neighbouring dots


def _srgb_to_linear(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return 255.0 * np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


def _gauss(img, sigma):
    r = max(1, int(3 * sigma))
    k = np.exp(-(np.arange(-r, r + 1) ** 2) / (2.0 * sigma * sigma))
    k /= k.sum()
    p = np.pad(img, ((r, r), (0, 0), (0, 0)), mode="edge")
    img = sum(k[i] * p[i:i + img.shape[0]] for i in range(2 * r + 1))
    p = np.pad(img, ((0, 0), (r, r), (0, 0)), mode="edge")
    return sum(k[i] * p[:, i:i + img.shape[1]] for i in range(2 * r + 1))


def percebido(ref_rgb, new_rgb, mask=None, sigma=PERCEIVED_SIGMA):
    """What the eye sees when a dither is on.

    Per-pixel error always rises with dithering - that is what dithering does.
    The number that matters is the error left after the eye mixes the dots.
    Light mixes linearly, so both images are blurred in linear light (Gaussian)
    and compared in Lab, over the pixels of mask (2-D images required).
    """
    a = _linear_to_srgb(_gauss(_srgb_to_linear(ref_rgb.astype(np.float64)), sigma))
    b = _linear_to_srgb(_gauss(_srgb_to_linear(new_rgb.astype(np.float64)), sigma))
    if mask is not None:
        a, b = a[mask], b[mask]
    return metrics(a.reshape(-1, 3), b.reshape(-1, 3))["de_mean"]


# --------------------------------------------------------------------------
# complete result
# --------------------------------------------------------------------------
HL_MIN_GAIN = 1.0    # dE a tile must gain to be worth a highlight sprite
SPRITES_SCREEN, SPRITES_LINE, SPRITE_PX_LINE = 80, 20, 320   # H40 VDP limits


def highlight_sprites(win):
    """How many sprites would draw the highlight of the tiles in win.

    Highlight needs a sprite pixel of PAL3 index 14 over the area. Sprites are
    1..4 x 1..4 tiles; this covers win greedily (top-left first, the box
    trimmed to the winning tiles inside it), then checks the VDP limits per
    screen view (sprites that touch the view), per scanline (sprites and
    sprite pixels on the worst line) and the VRAM the sprite patterns take.
    """
    win = np.asarray(win, bool)
    nh, nw = win.shape
    left = win.copy()
    boxes = []
    for r in range(nh):
        for c in range(nw):
            if not left[r, c]:
                continue
            blk = left[r:r + 4, c:c + 4]
            rr, cc = np.nonzero(blk)
            bh, bw = int(rr.max()) + 1, int(cc.max()) + 1
            boxes.append((r, c, bh, bw))
            left[r:r + bh, c:c + bw] = False
    line_n = np.zeros(nh, np.int32)
    line_px = np.zeros(nh, np.int32)
    for r, c, bh, bw in boxes:
        line_n[r:r + bh] += 1
        line_px[r:r + bh] += bw * 8
    view_max = 0
    if boxes:
        b = np.asarray(boxes)
        for x0, x1, y0, y1 in windows_of(nh, nw):
            hit = ((b[:, 0] < y1) & (b[:, 0] + b[:, 2] > y0)
                   & (b[:, 1] < x1) & (b[:, 1] + b[:, 3] > x0))
            view_max = max(view_max, int(hit.sum()))
    out = {"sprites": len(boxes),
           "sprite_tiles": int(sum(bh * bw for _, _, bh, bw in boxes)),
           "view_max": view_max,
           "line_max": int(line_n.max()) if boxes else 0,
           "line_px_max": int(line_px.max()) if boxes else 0}
    out["fits"] = (out["view_max"] <= SPRITES_SCREEN
                   and out["line_max"] <= SPRITES_LINE
                   and out["line_px_max"] <= SPRITE_PX_LINE)
    return out


def highlight_note(hs):
    """One line for logs and reports."""
    if not hs or not hs["sprites"]:
        return "highlight: no tile gains enough to be worth a sprite"
    lim = (f"limits {SPRITES_SCREEN}/screen, {SPRITES_LINE}/line, "
           f"{SPRITE_PX_LINE} px/line")
    return (f"highlight sprites: {hs['sprites']} ({hs['sprite_tiles']} tiles of VRAM) "
            f"· worst view {hs['view_max']} · worst line {hs['line_max']} sprites / "
            f"{hs['line_px_max']} px · {'FITS' if hs['fits'] else 'DOES NOT FIT'} "
            f"({lim})")


def vdp_effects(rgb, idx, tile_pal, pals, painted, mode, shadow_mask=None):
    """Per tile: normal brightness, or let the VDP darken it (shadow).

    Shadow is free: it is the tile priority bit, and the S/H circuit halves the
    channel values of a low priority tile. So a tile that is too bright for its
    palette can be darkened by the hardware instead of costing a colour. The
    tile keeps its palette; only the priority changes.

    Highlight cannot be done per tile in a plane: on hardware it needs a sprite
    pixel of PAL3 index 14 over the area. So it is measured, not applied: the
    tool reports how many tiles would gain and how much, and paints the preview
    of that best case.
    """
    h, w = rgb.shape[:2]
    nh, nw = h // 8, w // 8
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)

    # the tile picks by what the eye sees: compare 4x4 block averages, not the
    # single pixel, so dithering and a slightly darker tile are judged fairly
    h4, w4 = (h // 4) * 4, (w // 4) * 4
    ref4 = rgb[:h4, :w4].reshape(h4 // 4, 4, w4 // 4, 4, 3).mean(axis=(1, 3))
    lab_ref4 = rgb_to_lab(ref4.reshape(-1, 3))

    def dmap(img):
        im4 = img[:h4, :w4].reshape(h4 // 4, 4, w4 // 4, 4, 3).mean(axis=(1, 3))
        d = np.sqrt(((lab_ref4 - rgb_to_lab(im4.reshape(-1, 3))) ** 2).sum(1))
        return d.reshape(nh, 2, nw, 2).mean(axis=(1, 3))

    dn = dmap(painted)
    out = painted
    mask = shadow_mask
    if mode in ("shadow", "both") and shadow_mask is None:
        escuro = np.asarray([shadow_palette(p) for p in pals], np.uint8)
        dark = escuro[rows, idx]
        ds = dmap(dark)
        mask = ds < dn
        out = painted.copy()
        sel = mask.repeat(8, 0).repeat(8, 1)
        out[sel] = dark[sel]

    hl_tiles, hl_gain, hl_img, win = 0, 0.0, None, None
    if mode in ("highlight", "both"):
        claro = np.asarray([highlight_palette(p) for p in pals], np.uint8)
        light = claro[rows, idx]
        if mask is not None and np.any(mask):
            # a highlight sprite over a SHADOW pixel does not brighten it: the
            # two cancel and the pixel shows its normal colour (v2.4 used the
            # full highlight there, so the estimate was too optimistic)
            normal = np.asarray(pals, np.uint8)[rows, idx]
            sm8 = np.repeat(np.repeat(np.asarray(mask, bool), 8, 0), 8, 1)
            light = np.where(sm8[..., None], normal, light)
        cur = dmap(out)
        dh = dmap(light)
        # only tiles that clearly gain are worth a sprite
        win = dh < cur - HL_MIN_GAIN
        hl_tiles = int(win.sum())
        hl_gain = float((cur[win] - dh[win]).mean()) if hl_tiles else 0.0
        hl_img = out.copy()
        sel = win.repeat(8, 0).repeat(8, 1)
        hl_img[sel] = light[sel]
        hole = idx == 0
        hl_img[hole] = out[hole]   # backdrop pixels: the sprite pixel is clear
    return out, mask, hl_tiles, hl_gain, hl_img, win


def reduce_for_cap(idx, pals, tile_pal, resp, vq, seed=0):
    """Apply the tile cap (per view or per image). pals are the palettes that
    tile_pal indexes (the effective ones when shadow is planned)."""
    h, w = idx.shape
    uniq, tilemap = dedup(to_tiles(idx))
    if vq is not None:
        if resp.get("cap_scope", "image") == "view":
            if vq < max_window_tiles(idx):
                if resp.get("sprite"):
                    idx, tile_pal = reduce_tiles_windows_local(
                        idx, pals, tile_pal, vq, seed)
                else:
                    idx, tile_pal = reduce_tiles_windows(idx, pals, tile_pal, vq, seed)
                uniq, tilemap = dedup(to_tiles(idx))
        elif vq < len(uniq):
            idx, tile_pal = reduce_tiles(idx, pals, tile_pal, vq, seed)
            uniq, tilemap = dedup(to_tiles(idx))
    rec = rebuild(uniq, tilemap, h, w)
    assert np.array_equal(rec, idx), "tile rebuild does not match"
    peak = window_peak_of(idx)
    if (vq is not None and resp.get("cap_scope", "image") == "view"
            and peak[0] > vq):
        raise SystemExit(f"internal error: worst view has {peak[0]} tiles, "
                         f"above the cap {vq}")
    return idx, tile_pal, uniq, tilemap, rec, peak


def backdrop_shown(pals, rec, shadow_mask):
    """Colour of the index-0 pixels: CRAM colour 0, and with shadow/highlight
    on a low priority tile shows it at half brightness too (v2.3 painted it at
    full brightness in the preview and the metric)."""
    key = np.asarray(pals[0][0], np.uint8)
    hole = rec == 0
    out = np.repeat(key[None], int(hole.sum()), 0)
    if shadow_mask is not None and np.any(shadow_mask):
        dark = np.repeat(np.repeat(np.asarray(shadow_mask, bool), 8, 0), 8, 1)[hole]
        out[dark] = shadow_palette(key.reshape(1, 3))[0]
    return out


def metric_keep(rgb, resp, shadow_mask=None):
    """Pixels the dE counts: everything but the backdrop of the SOURCE. On a
    shadow tile the backdrop is shown darkened, so there it does count."""
    if resp.get("key") is None:
        return np.ones(rgb.shape[:2], bool)
    bm = backdrop_mask(rgb, resp)
    if shadow_mask is not None and np.any(shadow_mask):
        bm = bm & ~np.repeat(np.repeat(np.asarray(shadow_mask, bool), 8, 0), 8, 1)
    return ~bm


def build(rgb, pals, resp, vq=None, seed=0):
    """Rebuild the image from tiles, each tile on the palette it picked."""
    h, w = rgb.shape[:2]
    idx, tile_pal, _ = assign_palettes(rgb, pals, resp)
    idx, tile_pal, uniq, tilemap, rec, peak = reduce_for_cap(
        idx, eff_pals(pals, resp), tile_pal, resp, vq, seed)
    eff = np.asarray(eff_pals(pals, resp), np.uint8)
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    new = eff[rows, rec]
    tile_pal, shadow_mask = split_tile_pal(tile_pal, len(pals))
    hl_tiles, hl_gain, hl_img, hl_win = 0, 0.0, None, None
    mode = resp.get("mode")
    if shadow_mask is not None:
        # shadow already planned with the palettes: only highlight is measured
        if mode == "both":
            _, _, hl_tiles, hl_gain, hl_img, hl_win = vdp_effects(
                rgb, rec, tile_pal, pals, new, "highlight", shadow_mask)
    elif mode in ("shadow", "highlight", "both"):
        new, shadow_mask, hl_tiles, hl_gain, hl_img, hl_win = vdp_effects(
            rgb, rec, tile_pal, pals, new, mode)
    if (rec == 0).any():
        new[rec == 0] = backdrop_shown(pals, rec, shadow_mask)
    # The metric leaves out the backdrop of the SOURCE. v1 left out every pixel
    # that came out as index 0, so art turned into backdrop by the reduction
    # vanished from the average instead of counting as an error.
    keep = metric_keep(rgb, resp, shadow_mask)
    if keep.any():
        m = metrics(rgb[keep], new[keep])
    else:
        m = {"de_mean": 0.0, "de_p95": 0.0, "de_max": 0.0}
    # the dE is always the EXPORTED image; highlight (not exportable on a
    # plane) gets its own best-case number next to it
    hl_sprites, de_hl = None, None
    if hl_img is not None:
        hl_sprites = highlight_sprites(hl_win)
        if keep.any():
            de_hl = metrics(rgb[keep], hl_img[keep])["de_mean"]
    usage = np.bincount(tile_pal.reshape(-1), minlength=len(pals)).tolist()
    if resp.get("dithering", "none") == "none":
        seen = m["de_mean"]
    else:
        # v1 passed rgb[keep] (a flat list of pixels) to the block average, which
        # then had nothing to average and returned the plain dE: "perceived"
        # always equalled the mean (seen in every sweep.csv row)
        seen = percebido(rgb, new, keep) if keep.any() else 0.0
    return {"idx": idx, "tile_pal": tile_pal, "uniq": uniq, "tilemap": tilemap, "rgb": new,
            "de_perceived": seen, "tiles": len(uniq), "bytes_tiles": int(len(uniq)) * 32,
            "bytes_map": int((h // 8) * (w // 8) * 2),
            "window_tiles": peak[0],
            "window_x0": peak[1] * 8, "window_y0": peak[2] * 8,
            "window_x1": peak[3] * 8, "window_y1": peak[4] * 8,
            "window_count": peak[5],
            "de_mean": m["de_mean"], "de_p95": m["de_p95"], "de_max": m["de_max"],
            "palette_usage": usage, "vq": vq,
            "shadow_mask": shadow_mask, "hl_tiles": hl_tiles, "hl_gain": hl_gain,
            "rgb_hl": hl_img, "hl_sprites": hl_sprites, "de_hl": de_hl}





def palette_options(rgb, pals, resp):
    """Paint the whole image with each palette: (index, per-pixel error) each.

    Painting every palette over the full image keeps the ordered dither grid
    aligned with the picture, so a tile that changes palette keeps the same
    phase as its neighbours.
    """
    h, w = rgb.shape[:2]
    lab = rgb_to_lab(rgb).reshape(-1, 3)
    base = 1
    mask = None
    if resp.get("key") is not None:
        mask = backdrop_mask(rgb, resp).reshape(-1)
    opts = []
    effs = eff_pals(pals, resp)
    hole_err = None
    if mask is not None and mask.any() and shadow_on(resp):
        # A hole shows the backdrop, and on a shadow tile the VDP shows it at
        # half brightness. v2.4/2.5 scored holes as 0 for every option, so a
        # tile that was mostly backdrop picked shadow on a whim (a star) and
        # turned into a dark block. Now the hole costs what it shows.
        key = np.asarray(pals[0][0], np.uint8).reshape(1, 3)
        src = lab[mask]
        hole_err = []
        for kc in (key, shadow_palette(key)):
            hole_err.append(np.sqrt(((src - rgb_to_lab(kc.astype(np.float64))) ** 2).sum(1)))
    for j, pal in enumerate(effs):
        pal = np.asarray(pal, np.uint8)
        i, e = _paint_lab_err(lab, rgb_to_lab(pal[base:]), (h, w), resp["dithering"],
                              resp.get("dither_strength", DITHER_DEFAULT))
        i = i + base
        if mask is not None:
            i.reshape(-1)[mask] = 0
            e.reshape(-1)[mask] = (0.0 if hole_err is None
                                   else hole_err[1 if j >= len(pals) else 0])
        opts.append((i, (e ** 2).reshape(h, w)))
    return opts


SHADOW_MIN_GAIN = 0.75   # dE (tile RMS) shadow must win by to be taken
SHADOW_NEIGH_W = 3.0     # dE per 4-neighbour in the other state, flat areas
SHADOW_FLAT_MARGIN = 2.0  # extra dE shadow must win by on a flat tile
SHADOW_FLAT_STD = 8.0    # source tile Lab std where a tile counts as flat


def shadow_choice(rgb, block, n):
    """Normal or shadow per tile, with a margin and neighbour coherence.

    The plain argmin let every tile pick on its own: on a smooth sky the
    normal and the shadowed tone often tie within a fraction of a dE but with
    different hues, so near-ties flipped tile by tile into a visible 8x8
    mosaic that the per-pixel mean did not punish. Now shadow must win by
    SHADOW_MIN_GAIN, and a few passes charge SHADOW_NEIGH_W for every
    neighbour in the other state, scaled by how flat the source tile is (on
    detail, edges between tiles are not visible and the cost fades).
    Returns effective tile palettes (0..n-1 normal, n..2n-1 shadow).
    """
    h, w = rgb.shape[:2]
    nh, nw = h // 8, w // 8
    rm = np.sqrt(np.maximum(block, 0.0))
    bn = rm[:n].argmin(0)
    bs = rm[n:].argmin(0) + n
    iy, ix = np.arange(nh)[:, None], np.arange(nw)[None, :]
    en, es = rm[bn, iy, ix], rm[bs, iy, ix]
    lab = rgb_to_lab(rgb[:nh * 8, :nw * 8].reshape(-1, 3).astype(np.float64))
    lab = lab.reshape(nh, 8, nw, 8, 3).transpose(0, 2, 1, 3, 4).reshape(nh, nw, 64, 3)
    std = lab.std(axis=2).mean(axis=2)
    flat = np.clip(1.0 - std / SHADOW_FLAT_STD, 0.0, 1.0)
    margin = SHADOW_MIN_GAIN + SHADOW_FLAT_MARGIN * flat
    st = es < en - margin
    nb_tot = np.zeros((nh, nw))
    nb_tot[1:] += 1
    nb_tot[:-1] += 1
    nb_tot[:, 1:] += 1
    nb_tot[:, :-1] += 1
    for _ in range(6):
        f = st.astype(np.float64)
        nb = np.zeros((nh, nw))
        nb[1:] += f[:-1]
        nb[:-1] += f[1:]
        nb[:, 1:] += f[:, :-1]
        nb[:, :-1] += f[:, 1:]
        cost_s = es + SHADOW_NEIGH_W * flat * (nb_tot - nb)
        cost_n = en + SHADOW_NEIGH_W * flat * nb
        new = cost_s < cost_n - margin
        if np.array_equal(new, st):
            break
        st = new
    return np.where(st, bs, bn).astype(np.uint8)


SEAM_W = 1.0       # weight of the extra tone jump at a tile edge (smooth areas)
SEAM_TOL = 1.0     # dE of extra jump that is ignored
SEAM_SLACK = 2.5   # an option may cost up to this much more own error (dE)


def _strips4(lab, nh, nw, k=3):
    t = lab[:nh * 8, :nw * 8].reshape(nh, 8, nw, 8, 3).transpose(0, 2, 1, 3, 4)
    return np.stack([t[:, :, :k].mean((2, 3)), t[:, :, -k:].mean((2, 3)),
                     t[:, :, :, :k].mean((2, 3)), t[:, :, :, -k:].mean((2, 3))], 2)


def smooth_seams(rgb, block, n, tile_pal, shown):
    """Soften the borders that shadow regions draw on smooth areas.

    Measured on a dark sky (4 palettes): at the border of a shadow region the
    result jumped 8-10 dE where the source changes 4 - each side was only a
    few dE off, but in opposite directions (the shadow a bit darker, the
    normal tile a bit lighter), so the step doubled and the 8x8 staircase
    showed. Here every tile next to a border between normal and shadow, on a
    smooth part of the source, may switch to any option (any palette, normal
    or shadowed) whose own error is at most SEAM_SLACK worse, when that cuts
    the jump to its 4 neighbours beyond what the source has. The rest of the
    image is untouched. Red-black passes, so neighbours settle together.
    """
    h, w = rgb.shape[:2]
    nh, nw = h // 8, w // 8
    m = len(shown)
    rm = np.sqrt(np.maximum(block, 0.0))
    src_lab = rgb_to_lab(rgb.reshape(-1, 3).astype(np.float64)).reshape(h, w, 3)
    lab_t = src_lab[:nh * 8, :nw * 8].reshape(nh, 8, nw, 8, 3).transpose(0, 2, 1, 3, 4)
    flat = np.clip(1.0 - lab_t.reshape(nh, nw, 64, 3).std(axis=2).mean(axis=2)
                   / SHADOW_FLAT_STD, 0.0, 1.0)
    S = _strips4(src_lab, nh, nw)
    O = np.stack([_strips4(rgb_to_lab(np.asarray(x, np.float64).reshape(-1, 3))
                           .reshape(h, w, 3), nh, nw) for x in shown])   # (m,nh,nw,4,3)
    iy, ix = np.arange(nh)[:, None], np.arange(nw)[None, :]
    tp = np.asarray(tile_pal).astype(np.int64)
    best_own = rm.min(0)

    def jump(a, b):
        return np.sqrt(((a - b) ** 2).sum(-1))

    # source jumps across each side (0 where there is no neighbour)
    s_r = np.zeros((nh, nw)); s_r[:, :-1] = jump(S[:, :-1, 3], S[:, 1:, 2])
    s_l = np.zeros((nh, nw)); s_l[:, 1:] = s_r[:, :-1]
    s_b = np.zeros((nh, nw)); s_b[:-1] = jump(S[:-1, :, 1], S[1:, :, 0])
    s_t = np.zeros((nh, nw)); s_t[1:] = s_b[:-1]
    sides = ((3, 2, (0, 1), s_r), (2, 3, (0, -1), s_l), (1, 0, (1, 0), s_b), (0, 1, (-1, 0), s_t))
    parity = (iy + ix) % 2
    for _ in range(6):
        shadow = tp >= n
        border = np.zeros((nh, nw), bool)
        border[:, :-1] |= shadow[:, :-1] != shadow[:, 1:]
        border[:, 1:] |= shadow[:, 1:] != shadow[:, :-1]
        border[:-1] |= shadow[:-1] != shadow[1:]
        border[1:] |= shadow[1:] != shadow[:-1]
        cand = border & (flat > 0.3)
        if not cand.any():
            break
        changed = False
        for p in (0, 1):
            cost = np.empty((m, nh, nw))
            for j in range(m):
                c = rm[j] + 0.0
                for mine, theirs, (dy, dx), sj in sides:
                    nb = np.roll(np.roll(tp, -dy, 0), -dx, 1)
                    nbs = O[nb, np.roll(np.roll(np.broadcast_to(iy, (nh, nw)), -dy, 0), -dx, 1),
                            np.roll(np.roll(np.broadcast_to(ix, (nh, nw)), -dy, 0), -dx, 1), theirs]
                    ex = np.maximum(0.0, jump(O[j, :, :, mine], nbs) - sj - SEAM_TOL)
                    valid = np.ones((nh, nw), bool)
                    if dy == 1:
                        valid[-1] = False
                    if dy == -1:
                        valid[0] = False
                    if dx == 1:
                        valid[:, -1] = False
                    if dx == -1:
                        valid[:, 0] = False
                    c = c + SEAM_W * flat * np.where(valid, ex, 0.0)
                c = np.where(rm[j] <= best_own + SEAM_SLACK, c, np.inf)
                cost[j] = c
            new = cost.argmin(0)
            upd = cand & (parity == p)
            if np.any(new[upd] != tp[upd]):
                changed = True
                tp = np.where(upd, new, tp)
        if not changed:
            break
    return tp.astype(np.uint8)


def assign_palettes(rgb, pals, resp):
    """Every tile picks the palette that paints it with the lowest error.

    The palette bits live in the map entry, so this is exactly what the Mega
    Drive allows: a few palettes in CRAM and every tile free to pick any of
    them - no bands and no geometric restriction.
    """
    h, w = rgb.shape[:2]
    nh, nw = h // 8, w // 8
    opts = palette_options(rgb, pals, resp)
    stack = np.stack([o[0] for o in opts])
    block = np.stack([o[1].reshape(nh, 8, nw, 8).mean(axis=(1, 3)) for o in opts])
    tile_pal = block.argmin(0).astype(np.uint8)
    if shadow_on(resp) and len(block) == 2 * len(pals):
        n = len(pals)
        state = shadow_choice(rgb, block, n) >= n
        bn = block[:n].argmin(0)
        bs = block[n:].argmin(0) + n
        tile_pal = np.where(state, bs, bn).astype(np.uint8)
        if not resp.get("shadow_seam_off"):
            effs = np.asarray(eff_pals(pals, resp), np.uint8)
            tile_pal = smooth_seams(rgb, block, n, tile_pal,
                                    [effs[j][stack[j]] for j in range(len(effs))])
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    idx = np.take_along_axis(stack, rows[None], axis=0)[0]
    iy = np.arange(nh)[:, None]
    ix = np.arange(nw)[None, :]
    err = float(block[tile_pal, iy, ix].sum())
    return idx, tile_pal, err


def paint_tiles(rgb, pals, resp, tile_pal):
    """Assemble the index image using the palette each tile picked."""
    stack = np.stack([o[0] for o in palette_options(rgb, pals, resp)])
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    return np.take_along_axis(stack, rows[None], axis=0)[0]


def palettes_for_tiles(rgb, tile_pal, idx, n, resp, seed=0, previous=None, px_weights=None):
    """Recompute each palette from the pixels of the tiles that picked it.

    A palette that kept fewer than ~2% of the tiles is re-seeded from the tiles
    the current result gets worst, so an empty cluster does not stay empty and
    the CRAM slot is not wasted.
    """
    h, w = rgb.shape[:2]
    nh, nw = h // 8, w // 8
    k = 15
    mask = None
    if resp.get("key") is not None:
        mask = backdrop_mask(rgb, resp).reshape(-1)
    pick = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    rec = np.asarray(eff_pals(previous, resp), np.uint8)[pick, idx]
    lab = rgb_to_lab(rgb).reshape(-1, 3)
    # the error is measured on what is shown; the palette is fitted to what
    # the CRAM must hold (shadow tiles un-shadowed)
    tile_pal, sh = split_tile_pal(tile_pal, n)
    lab_fit = lab if sh is None else rgb_to_lab(shadow_equivalent(rgb, sh)).reshape(-1, 3)
    err = ((rgb_to_lab(rec.reshape(-1, 3)) - lab) ** 2).sum(1).reshape(h, w)
    if mask is not None:
        err.reshape(-1)[mask] = 0.0
    block = err.reshape(nh, 8, nw, 8).mean(axis=(1, 3))
    order = np.argsort(block, axis=None)[::-1]
    floor = max(4, (nh * nw) // 50)
    out, usage = [], []
    for i in range(n):
        mine = tile_pal == i
        usage.append(int(mine.sum()))
        if mine.sum() < floor:
            take = np.zeros(nh * nw, bool)
            take[order[:max(floor, (nh * nw) // 10)]] = True
            mine = take.reshape(nh, nw)
        hit = np.repeat(np.repeat(mine, 8, 0), 8, 1).reshape(-1)
        if mask is not None:
            hit = hit & ~mask
        if hit.sum() < k * 4:
            out.append(None if previous is None else previous[i])
            continue
        px = lab_fit[hit]
        wx = None if px_weights is None else px_weights[hit]
        pal_x, _ = kmeans_md(px, k, wx, seed + i)
        pal_x = refine_palette(px, pal_x, k, wx, seed + i)
        out.append(finish_palette(px.reshape(-1, 1, 3), pal_x, resp))
    return out, usage


def _weighted_err(rgb, pals, idx, tile_pal, px_weights, resp=None):
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    rec = np.asarray(eff_pals(pals, resp or {}), np.uint8)[rows, idx]
    d = ((rgb_to_lab(rec.reshape(-1, 3)) - rgb_to_lab(rgb.reshape(-1, 3))) ** 2).sum(1)
    return float((d * px_weights).sum())


def optimize_palettes(rgb, resp, seed=0, weights=None, rounds=3, px_weights=None):
    """Band palettes seed the run, then the tiles take over.

    Every accepted round lowers the total error, so the result is never worse
    than the horizontal-band solution it started from. With px_weights (one
    weight per pixel, the worst-case recipe) the per-tile rounds cluster with
    those weights and accept by the weighted error too; v1 used the weights
    only for the band seed, so B drifted back to A.
    """
    n = int(resp["palettes"])
    bands = bands_of(rgb.shape[0], n)
    pals = palettes_per_band(rgb, resp, bands, weights, seed)
    idx, tile_pal, err = assign_palettes(rgb, pals, resp)
    if px_weights is not None:
        err = _weighted_err(rgb, pals, idx, tile_pal, px_weights, resp)
    if shadow_on(resp) and resp.get("palette_file") is None:
        # second seed: the dark tiles fitted as their un-shadowed colours, so
        # the 15 colours start free of the darks the shadow can draw
        eq = shadow_equivalent(rgb, shadow_seed_tiles(rgb))
        eq = np.clip(np.round(eq), 0, 255).astype(np.uint8)
        pals_s = palettes_per_band(eq, resp, bands, weights, seed)
        idx_s, tp_s, err_s = assign_palettes(rgb, pals_s, resp)
        if px_weights is not None:
            err_s = _weighted_err(rgb, pals_s, idx_s, tp_s, px_weights, resp)
        if err_s < err:
            pals, idx, tile_pal, err = pals_s, idx_s, tp_s, err_s
    best = (err, pals, idx, tile_pal)
    for r in range(rounds):
        cand, _ = palettes_for_tiles(rgb, tile_pal, idx, n, resp, seed + 31 * (r + 1), pals,
                                     px_weights)
        cand = [pals[i] if c is None else c for i, c in enumerate(cand)]
        idx2, tp2, err2 = assign_palettes(rgb, cand, resp)
        if px_weights is not None:
            err2 = _weighted_err(rgb, cand, idx2, tp2, px_weights, resp)
        if err2 < best[0] - 1e-6:
            best = (err2, cand, idx2, tp2)
            pals, tile_pal = cand, tp2
        else:
            break
    return best[1], best[2], best[3]


def finish_palette(sub, pal_x, resp):
    # index 0 is the shared backdrop, so the 15 colours are arranged on 1..15
    chave = np.asarray(resp.get("key") or (0, 0, 0), np.uint8).reshape(1, 3)
    nodith = {"dithering": "none", "transparency": True, "key": resp.get("key"),
              "key_src": resp.get("key_src")}
    idx_t = paint(sub, [np.vstack([chave, pal_x])], nodith, [(0, sub.shape[0])])[0]
    idx_t = np.clip(idx_t.astype(np.int16) - 1, 0, len(pal_x) - 1).astype(np.uint8)
    pal_x, _ = arrange15(pal_x, idx_t)
    return np.vstack([chave, pal_x])


def palettes_per_band(rgb, resp, bands, weights_fn=None, seed=0):
    k = 15
    out = []
    for i, (y0, y1) in enumerate(bands):
        sub = rgb[y0:y1]
        lab = rgb_to_lab(sub).reshape(-1, 3)
        weights = weights_fn(y0, y1, sub, lab) if weights_fn else None
        weights = None if weights is None else np.asarray(weights, np.float64).reshape(-1)
        if resp.get("key") is not None:
            m = backdrop_mask(sub, resp).reshape(-1)
            lab = lab[~m]
            if weights is not None:
                weights = weights[~m]
        pal_x, _ = kmeans_md(lab, k, weights, seed + i)
        pal_x = refine_palette(lab, pal_x, k, weights, seed + i)
        out.append(finish_palette(sub, pal_x, resp))
    return out


def worst_case_weights(rgb, pals, idx, tile_pal, resp=None):
    """Weight on the pixels the current result gets worst, tile palettes included."""
    pal_img = np.asarray(eff_pals(pals, resp or {}), np.uint8)
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    rec = pal_img[rows, idx]
    a = rgb_to_lab(rgb.reshape(-1, 3))
    b = rgb_to_lab(rec.reshape(-1, 3))
    err = np.sqrt(((a - b) ** 2).sum(1))
    return 1.0 + 3.0 * (err / (err.mean() + 1e-6)) ** 1.5





def make_samples(rgb, resp, seed=0):
    """Four complete results with the fixed palette answers. Slots never move."""
    h, w = rgb.shape[:2]
    n = int(resp["palettes"])

    existing = resp.get("palette_file")
    if existing:
        if n > 1:
            raise SystemExit("a palette file works for 1 palette; for N use an own palette.")
        pal = load_palette(existing)
        if len(pal) < 16:
            # index 0 is the shared backdrop, never a colour of its own
            chave = np.asarray(resp.get("key") or (0, 0, 0), np.uint8).reshape(1, 3)
            pal = np.vstack([chave, pal])[:16]
        pals = [pal[:16]]
        idx0, _ = paint(rgb, pals, resp, [(0, h)])
        base = count_for_cap(idx0, resp)
        cap = resp.get("max_tiles")
        top = base if not cap else min(base, cap)
        plan = [(1.0, "100%", "fidelity"), (0.85, "85%", "balance-85"),
                (0.65, "65%", "balance-65"), (0.45, "45%", "balance-45")]
        out = []
        for f, label, recipe in plan:
            vq = max(8, int(f * top))
            if f >= 1:
                vq = None if top >= base else top
            out.append(dict(recipe=recipe, label=label, pals=pals, dither=None, vq=vq))
        return out

    pals_a, idx_a, tp_a = optimize_palettes(rgb, resp, seed)

    # B: worst case, weight on the error left by the per-tile result A
    weights = worst_case_weights(rgb, pals_a, idx_a, tp_a, resp)
    pals_b, _, _ = optimize_palettes(rgb, resp, seed + 10,
                                     weights=lambda y0, y1, sub, lab: weights[y0 * w:y1 * w],
                                     px_weights=weights)

    base = count_for_cap(idx_a, resp)
    cap = resp.get("max_tiles")
    top = base if not cap else min(base, cap)
    a_vq = cap if (cap and cap < base) else None
    same_pal = all(np.array_equal(np.asarray(a), np.asarray(b))
                   for a, b in zip(pals_a, pals_b))
    if same_pal:
        # no distinct worst-case palette: B becomes a dither comparison and C
        # keeps the old economy fallback so the slot is not a duplicate of D
        recipes = [
            dict(recipe="fidelity", label="fidelity", pals=pals_a, vq=a_vq, dither=None),
            dict(recipe="worst-case", label="worst case", pals=pals_a, vq=a_vq, dither=None),
            dict(recipe="economy", label="economy", pals=pals_a, vq=max(8, int(0.60 * top)), dither=None),
            dict(recipe="balance", label="balance", pals=pals_a, vq=max(8, int(0.85 * top)), dither=None),
        ]
        alt = "none" if resp["dithering"] != "none" else "ordered8"
        recipes[1] = dict(recipe="dither", label=f"dither {alt}", pals=pals_a, vq=None, dither=alt)
    else:
        # C: same 85% budget as D, but with B's worst-case palette. This is the
        # balanced comparison the author asked for: A vs B at 100% and at 85%.
        recipes = [
            dict(recipe="fidelity", label="fidelity", pals=pals_a, vq=a_vq, dither=None),
            dict(recipe="worst-case", label="worst case", pals=pals_b, vq=a_vq, dither=None),
            dict(recipe="balance-worst", label="balance worst", pals=pals_b,
                 vq=max(8, int(0.85 * top)), dither=None),
            dict(recipe="balance", label="balance", pals=pals_a, vq=max(8, int(0.85 * top)), dither=None),
        ]
    return recipes





def load_palette(path):
    if path.lower().endswith(".png"):
        im = Image.open(path)
        raw = im.getpalette() or []
        colors = np.array(raw, np.uint8).reshape(-1, 3)[:16]
    else:
        lines = [l.strip() for l in open(path, encoding="utf-8", errors="replace") if l.strip()]
        if lines and lines[0].upper() == "JASC-PAL":
            lines = lines[3:]
        colors = np.array([[int(x) for x in l.split()[:3]] for l in lines], np.uint8)
    return snap_md(colors.astype(np.float64))


# --------------------------------------------------------------------------
# comparison sheet
# --------------------------------------------------------------------------
def font(size):
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def best_crop(ref, results, cw, ch, step=8):
    h, w = ref.shape[:2]
    cw, ch = min(cw, w), min(ch, h)
    acc = np.zeros((h, w), np.float64)
    lab_ref = rgb_to_lab(ref.reshape(-1, 3))
    for r in results:
        de = np.sqrt(((lab_ref - rgb_to_lab(r["rgb"].reshape(-1, 3))) ** 2).sum(1)).reshape(h, w)
        acc += de
    best, where = -1.0, (0, 0)
    for y in range(0, h - ch + 1, step):
        for x in range(0, w - cw + 1, step):
            v = acc[y:y + ch, x:x + cw].mean()
            if v > best:
                best, where = v, (x, y)
    return where[0], where[1], cw, ch


def _wrap(dr, text, fnt, maxw):
    """Break a caption into as many lines as it needs to fit the column."""
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if not cur or dr.textlength(t, font=fnt) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def sheet(ref_rgb, items, path, crop, ref_label="", extras=None):
    f_txt, f_small = font(15), font(12)
    W, H = ref_rgb.width, ref_rgb.height
    cx, cy, cw, ch = crop
    margin, gap = 14, 12
    extras = extras or []

    # the caption must fit the column, otherwise it lands on top of the next one
    dr0 = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    blocks = [_wrap(dr0, it["line"], f_small, W - 4) for it in items]
    nlines = max(len(b) for b in blocks)
    header = 21 + 13 * nlines + 8

    ex_blocks = [_wrap(dr0, lab, f_small, W - 4) for lab, _im in extras] if extras else []
    n_ex = max((len(b) for b in ex_blocks), default=0)
    extra_h = (16 + 13 * n_ex + 8 + H) if extras else 0

    width = 4 * W + 3 * gap + 2 * margin
    height = 26 + H + 12 + header + H + 12 + 22 + ch * 2 + 16 + extra_h
    img = Image.new("RGB", (width, height), (22, 22, 26))
    dr = ImageDraw.Draw(img)
    dr.text((margin, 10), f"reference — {ref_label}", font=f_small, fill=(150, 220, 150))
    img.paste(ref_rgb, (margin, 26))
    y = 26 + H + 12
    for k, it in enumerate(items):
        x = margin + k * (W + gap)
        dr.text((x, y), it["title"], font=f_txt, fill=(255, 255, 255))
        for i, ln in enumerate(blocks[k]):
            dr.text((x, y + 21 + 13 * i), ln, font=f_small, fill=(140, 190, 255))
        img.paste(it["img"], (x, y + header))
    y += header + H + 12
    dr.text((margin, y), f"detail 2x — same region ({cx},{cy})", font=f_small, fill=(160, 160, 160))
    y += 22
    for k, it in enumerate(items):
        x = margin + k * (W + gap)
        d = it["img"].crop((cx, cy, cx + cw, cy + ch)).resize((cw * 2, ch * 2), Image.NEAREST)
        img.paste(d, (x, y))
    y += ch * 2
    if extras:
        y += 16
        for k, (label, im) in enumerate(extras):
            x = margin + k * (W + gap)
            for i, ln in enumerate(ex_blocks[k]):
                dr.text((x, y + 13 * i), ln, font=f_small, fill=(255, 220, 150))
            img.paste(im, (x, y + 13 * n_ex + 8))
    img.save(path, quality=92, subsampling=0)
    return path


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------
def empty_profile():
    return {"version": 1, "choices": [],
            "preferences": {"palettes": 1, "palette_file": None, "mode": "normal",
                            "transparency": False, "dithering": "ordered4",
                            "dither_strength": DITHER_DEFAULT,
                            "scroll": False, "resize": "none", "max_tiles": None, "cap_scope": "image", "oversize": "resize",
                            "recipe": "fidelity", "lambda": 0.35, "vq": None}}


def load_profile():
    if os.path.exists(PROFILE):
        try:
            d = json.load(open(PROFILE, encoding="utf-8"))
            base = empty_profile()
            base["preferences"].update(d.get("preferences", {}))
            base["choices"] = d.get("choices", [])
            return base
        except Exception:
            pass
    return empty_profile()


def save_profile(p):
    json.dump(p, open(PROFILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _ask(text, default, options=None):
    if options:
        while True:
            r = input(f"{text} [{'/'.join(options)}] ({default}): ").strip().lower() or default
            if r in options:
                return r
            print("  invalid answer")
    return input(f"{text} ({default}): ").strip() or default


def ask(args, profile):
    """Ask the five setup questions.

    - Always asks. Skipping requires an explicit --no-ask.
    - The defaults shown are the factory values, never the last answers.
    - The stored profile is used only by --no-ask, as a fallback.
    - A value given on the command line counts as an answer and makes the tool
      skip only that one question.
    """
    stored = profile["preferences"]

    if getattr(args, "no_ask", False):
        print("[-no-ask] no questions: using the stored answers as fallback")
        return {
            "palettes": args.palettes or stored["palettes"],
            "palette_file": getattr(args, "palette_file", None) or stored.get("palette_file"),
            "mode": args.mode or stored["mode"],
            "transparency": (args.transparency == "yes") if getattr(args, "transparency", None) else stored["transparency"],
            "dithering": args.dithering or stored["dithering"],
            "dither_strength": (getattr(args, "dither_strength", None)
                                or stored.get("dither_strength", DITHER_DEFAULT)),
            "scroll": (args.scroll == "yes") if getattr(args, "scroll", None) else stored["scroll"],
            "resize": args.resize or stored["resize"],
            "max_tiles": (args.max_tiles if getattr(args, "max_tiles", None) is not None
                          else stored.get("max_tiles")),
            "cap_scope": (getattr(args, "cap_scope", None)
                          or stored.get("cap_scope", "image")),
            "backdrop": (getattr(args, "backdrop", None)
                         or stored.get("backdrop", "border")),
            "backdrop_rgb": (getattr(args, "backdrop_color", None)
                             or stored.get("backdrop_rgb")),
            "oversize": (getattr(args, "oversize", None)
                         or stored.get("oversize", "resize")),
        }

    factory = empty_profile()["preferences"]

    print("\n--- palette (applies to all four samples) ---")

    if getattr(args, "palettes", None):
        n = int(args.palettes)
        print(f"1) palettes: {n} (from the command line)")
    else:
        n = int(_ask("1) How many palettes (1 to 4, each tile picks its best one)?",
                str(factory["palettes"])))
    if n < 1 or n > 4:
        raise SystemExit("1 to 4 palettes.")

    palette_file = getattr(args, "palette_file", None)
    if palette_file:
        print(f"2) palette file: {palette_file} (from the command line)")
    else:
        if _ask("2) Own palette or a file?", "o", ["o", "f"]) == "f":
            palette_file = input("   path to the .pal or .png: ").strip()
    if palette_file and not os.path.exists(palette_file):
        raise SystemExit(f"not found: {palette_file}")

    if getattr(args, "mode", None):
        mode = args.mode
        print(f"3) mode: {mode} (from the command line)")
    else:
        mode = _ask("3) Mode (normal, shadow, highlight, both)", factory["mode"], list(MODES))

    if getattr(args, "backdrop", None):
        backdrop = args.backdrop
        print(f"4) backdrop: {backdrop} (from the command line)")
    else:
        backdrop = _ask("4) Backdrop colour, index 0 of every palette"
                        " (border or a fixed one)?", "border", ["border", "color"])
    backdrop_rgb = getattr(args, "backdrop_color", None)
    if backdrop == "color" and not backdrop_rgb:
        while True:
            txt = input("   RRGGBB of the backdrop: ").strip().lstrip("#")
            try:
                if len(txt) == 6:
                    int(txt, 16)
                    backdrop_rgb = txt
                    break
            except ValueError:
                pass
            print("   six hex digits, like 101820")
    # index 0 is ALWAYS the backdrop, so the palette machinery always runs in
    # "hole" mode: 15 own colours at 1..15 plus the shared backdrop at 0
    trans = True

    if getattr(args, "dithering", None):
        dith = args.dithering
        print(f"5) dithering: {dith} (from the command line)")
    else:
        dith = _ask("5) Dithering (" + ", ".join(DITHERS) + ")",
                    factory["dithering"], list(DITHERS))

    if dith == "none":
        dst = DITHER_DEFAULT
    elif getattr(args, "dither_strength", None):
        dst = args.dither_strength
        print(f"5b) dither strength: {dst} (from the command line)")
    else:
        dst = _ask("5b) Dither strength (clean = fewer specks, full = more colour)",
                   factory["dither_strength"], list(DITHER_STRENGTHS))

    if n > 4:
        raise SystemExit("1 to 4 palettes.")

    if getattr(args, "cmd", "sample") != "sample":
        scroll, resize = stored["scroll"], stored["resize"]
    else:
        if getattr(args, "scroll", None):
            scroll = args.scroll == "yes"
            print(f"6) background that scrolls: {'yes' if scroll else 'no'} (from the command line)")
        else:
            scroll = _ask("6) Background that scrolls (a plane that pans)?", "n", ["y", "n"]) == "y"
        hint = "height" if scroll else "width"
        if getattr(args, "resize", None):
            resize = args.resize
            print(f"7) resize: {resize} (from the command line)")
        else:
            resize = _ask("7) Resize: width 320, height 224 or none?", hint,
                          ["width", "height", "none"])

    max_tiles = None
    if getattr(args, "cmd", "sample") == "sample":
        if getattr(args, "max_tiles", None) is not None:
            max_tiles = int(args.max_tiles)
            print(f"8) max unique tiles: {max_tiles} (from the command line)")
        else:
            while True:
                txt = _ask("8) Maximum unique tiles for the final image"
                           " (0 or 'no' = keep the standard four samples)", "0")
                if txt.strip().lower() in ("", "n", "no", "nao", "n\u00e3o", "none", "-"):
                    max_tiles = 0
                    break
                try:
                    max_tiles = int(txt)
                except ValueError:
                    print("  invalid answer")
                    continue
                if max_tiles < 0:
                    print("  invalid answer")
                    continue
                break
        max_tiles = None if max_tiles == 0 else max(8, max_tiles)

    cap_scope = "image"
    if getattr(args, "cmd", "sample") == "sample" and max_tiles:
        if getattr(args, "cap_scope", None):
            cap_scope = args.cap_scope
            print(f"8b) tile cap scope: {cap_scope} (from the command line)")
        elif getattr(args, "no_ask", False):
            cap_scope = stored.get("cap_scope", "image")
            print(f"[-no-ask] tile cap scope: {cap_scope} (stored fallback)")
        else:
            cap_scope = _ask("8b) Does the tile cap apply to the whole image"
                             " or to each 320x240 view?", "image", ["image", "view"])

    return {"palettes": n, "palette_file": palette_file, "mode": mode,
            "transparency": trans, "dithering": dith, "dither_strength": dst,
            "scroll": scroll, "resize": resize, "max_tiles": max_tiles,
            "cap_scope": cap_scope, "backdrop": backdrop, "backdrop_rgb": backdrop_rgb}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def write_indexed_png(idx, pals, path, tile_pal=None, shadow_mask=None):
    """P-mode PNG in the rescomp format.

    bits 4-5 carry the palette, bit 7 the priority: rescomp turns bit 7 into the
    tile's priority bit in the map, and with shadow/highlight enabled a low
    priority tile is darkened by the VDP. So "shadow per tile" is written here,
    not in code.
    """
    out = np.ascontiguousarray(idx, np.uint8).copy()
    if tile_pal is not None:
        rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1).astype(np.uint8)
        out |= (rows << 4)
    prio = np.full(out.shape, 0x80, np.uint8)
    if shadow_mask is not None:
        # True = the VDP darkens this tile, so its priority bit must be 0
        escuro = np.repeat(np.repeat(np.asarray(shadow_mask, bool), 8, 0), 8, 1)
        prio[escuro] = 0x00
    out |= prio
    im = Image.fromarray(out, "P")
    # every palette at 16 * p and again at 128 + 16 * p (priority bit set), so
    # each pixel value points at its real colour. v1 wrote only palette 0, and
    # every pixel with bit 7 set (all of them) showed black in any viewer.
    flat = np.zeros((256, 3), np.uint8)
    for p, pal in enumerate(pals[:4]):
        colors = np.asarray(pal, np.uint8)[:16]
        flat[16 * p:16 * p + len(colors)] = colors
        flat[128 + 16 * p:128 + 16 * p + len(colors)] = colors
    im.putpalette(flat.reshape(-1).tolist())
    im.save(path)





def segments_of(h, w, limit=PLANE_MAX):
    """Cut the image into as few pieces as fit limit, as evenly as they can be.

    Every cut lands on a multiple of 8 and no piece is smaller than 8 px, so a
    632 px side becomes 312 + 320 instead of 512 + a 120 px leftover (or, worse,
    520 -> 512 + an 8 px sliver).
    """
    def cuts(size):
        n = max(1, -(-size // limit))
        if n == 1:
            return [(0, size)]
        step = max(8, (size // n) // 8 * 8)
        out, pos = [], 0
        for i in range(n - 1):
            nxt = pos + step
            if size - nxt < 8 * (n - i - 1):
                nxt = size - 8 * (n - i - 1)
            out.append((pos, nxt))
            pos = nxt
        out.append((pos, size))
        return out

    return [(x0, x1, y0, y1) for (y0, y1) in cuts(h) for (x0, x1) in cuts(w)]


def scale_to_fit(rgb, limit=PLANE_MAX):
    """Shrink to the biggest plane, with nearest neighbour (no invented colours)."""
    h, w = rgb.shape[:2]
    if max(h, w) <= limit:
        return rgb
    f = limit / max(h, w)
    nw, nh = max(8, int(round(w * f))), max(8, int(round(h * f)))
    return np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.NEAREST), np.uint8)


def apply_pad(rgb, a):
    if getattr(a, "no_pad", False):
        return rgb, ""
    h, w = rgb.shape[:2]
    out, (pw, ph) = pad8(rgb, parse_fill(getattr(a, "pad_color", None)))
    if not (pw or ph):
        return out, ""
    edges = []
    if ph:
        edges.append(f"{ph} row(s) at the bottom")
    if pw:
        edges.append(f"{pw} column(s) at the right")
    return out, f"  \u00b7  padded {w}x{h} -> {w + pw}x{h + ph} ({', '.join(edges)})"


def c_name(s):
    """A name that works as a C symbol in the generated code."""
    s = re.sub(r"[^0-9A-Za-z_]", "_", os.path.splitext(os.path.basename(s))[0])
    return s if s and not s[0].isdigit() else "img_" + s


def run_samples(rgb, resp, a, folder, title, export_name=None):
    """Four complete results for one image (or one segment)."""
    floor = metrics(rgb, snap_md(rgb))["de_mean"]
    recipes = make_samples(rgb, resp, a.seed)

    os.makedirs(folder, exist_ok=True)
    gain = resp.get("gain", 1.0)
    items, data, results = [], [], []
    for n, r in enumerate(recipes, 1):
        resp_r = dict(resp)
        if r.get("dither"):
            resp_r["dithering"] = r["dither"]
        res = build(rgb, r["pals"], resp_r, r["vq"], a.seed + n)
        results.append(res)
        write_indexed_png(res["idx"], r["pals"], os.path.join(folder, f"{n}_idx.png"),
                          res["tile_pal"], res.get("shadow_mask"))
        preview = Image.fromarray(res["rgb"])
        preview.save(os.path.join(folder, f"{n}.png"))
        letter = LETTERS[n - 1]
        label = f"{n} \u00b7 {letter} - {r['label']}"
        line = (f"{resp['palettes']} pal \u00b7 {res['tiles']} tiles \u00b7 "
                f"{(res['bytes_tiles'] + res['bytes_map']) // 1024} KB \u00b7 "
                f"dE {res['de_mean']:.2f} (+{res['de_mean'] - floor:.2f} from floor)")
        if resp.get("cap_scope") == "view" and resp.get("max_tiles"):
            line += (f" \u00b7 worst view {res['window_tiles']}"
                     f" at y {res['window_y0']}..{res['window_y1']}")
        if res.get("shadow_mask") is not None:
            line += f" \u00b7 shadow {int(res['shadow_mask'].sum())} tiles"
        items.append({"title": label, "line": line, "img": preview})
        data.append({"n": n, "letter": letter, "label": label, "recipe": r["recipe"],
                     "palettes": [np.asarray(p, np.uint8).tolist() for p in r["pals"]],
                     "tile_pal": res["tile_pal"].tolist(),
                     "palette_usage": res["palette_usage"], "vq": r["vq"], "tiles": res["tiles"],
                     "window_tiles": res.get("window_tiles", 0),
                     "window_x0": res.get("window_x0", 0),
                     "window_y0": res.get("window_y0", 0),
                     "window_x1": res.get("window_x1", 0),
                     "window_y1": res.get("window_y1", 0),
                     "window_count": res.get("window_count", 0),
                     "tilemap": np.asarray(res["tilemap"]).tolist(),
                     "shadow_rows": (["".join("1" if v else "0" for v in row)
                                      for row in res["shadow_mask"]]
                                     if res.get("shadow_mask") is not None else None),
                     "dither": resp_r["dithering"],
                     "bytes_tiles": res["bytes_tiles"], "bytes_map": res["bytes_map"],
                     "de_mean": res["de_mean"], "de_p95": res["de_p95"], "de_max": res["de_max"],
                     "de_perceived": res["de_perceived"],
                     "grid_floor": floor, "image": f"{n}.png", "indexed": f"{n}_idx.png"})
        extra = ("  percebido %.2f" % res["de_perceived"]
                 if res["de_perceived"] < res["de_mean"] - 0.05 else "")
        print(f"  {label:<26} {res['tiles']:>5} tiles  {res['de_mean']:>5.2f} dE "
              f"(+{res['de_mean'] - floor:.2f})  p95 {res['de_p95']:>4.1f}{extra}")

    if len(recipes[0]["pals"]) > 1:
        print("  palette split of sample 1: "
              + " / ".join(str(u) for u in results[0]["palette_usage"]) + " tiles")

    ref = Image.fromarray(rgb)
    crop = best_crop(rgb, results, 96, 64)

    # the samples always use the normal palette; show what the flash variants do
    extras, extra_files = [], []
    r0 = results[0]
    if resp["mode"] in ("highlight", "both") and r0.get("rgb_hl") is not None:
        im = Image.fromarray(r0["rgb_hl"])
        im.save(os.path.join(folder, "variant_highlight.png"))
        extras.append((f"highlight preview (not exported): {r0['hl_tiles']} tiles "
                       f"gain {r0['hl_gain']:.1f} dE, dE {r0['de_mean']:.2f} -> "
                       f"{(r0['de_hl'] or 0):.2f} · {highlight_note(r0['hl_sprites'])}", im))
        print(f"  sample 1 {highlight_note(r0['hl_sprites'])}")
        extra_files.append(("variant_highlight.png", "highlight preview"))

    path = sheet(ref, items, os.path.join(folder, "comparison.jpg"), crop,
                 f"{title} \u00b7 gain x{gain:.2f}", extras=extras)

    open_page = write_open_page(folder, f"samples - {title}",
                                [("comparison.jpg", "comparison")] +
                                [(d["image"], d["label"]) for d in data] + extra_files)
    json.dump({"source": os.path.abspath(a.source), "width": rgb.shape[1], "height": rgb.shape[0],
               "gain": gain, "answers": resp, "seed": a.seed,
               "palette_scope": "tile", "variants": mode_variants(resp["mode"]), "samples": data,
               "link": file_uri(path), "open": file_uri(open_page)},
              open(os.path.join(folder, "samples.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print()
    show_link("sheet", path)
    show_link("open", open_page)

    if export_name and not getattr(a, "no_ask", False):
        print()
        while True:
            try:
                txt = input("which sample? (1..4 or A..D, Enter = decide later): ").strip()
            except EOFError:
                txt = ""
            if not txt:
                print("  nothing exported; use 'choose' and 'emit' when you decide.")
                return
            try:
                n = parse_choice(txt)
            except SystemExit:
                print("  answer 1..4 or A..D")
                continue
            destino = os.path.join(os.getcwd(), "res", f"{export_name}.res")
            do_emit(os.path.join(folder, "samples.json"), export_name, destino, resp, n)
            return
    print(f"next: pick a sample and export it (window: Export; console: "
          f"emit --samples \"{os.path.basename(folder)}\" --n 1..4)")
    return data, path, open_page


def cmd_sample(a):
    set_view_height(int(getattr(a, "view_height", None) or 224))
    profile = load_profile()
    bg = parse_fill(getattr(a, "bg_color", None))
    rgb0 = load(a.source, bg)
    h0, w0 = rgb0.shape[:2]
    alpha_note = ""
    if has_alpha(a.source):
        alpha_note = f"  \u00b7  alpha on #{int(bg[0]):02X}{int(bg[1]):02X}{int(bg[2]):02X}"

    # before any question: how many colors and how many tiles the image asks for
    print_preflight(preflight(rgb0))

    resp = ask(a, profile)
    resize_note = ""
    if resp.get("resize") in ("width", "height"):
        rgb0 = resize_to(rgb0, resp["resize"])
        h1, w1 = rgb0.shape[:2]
        if (w1, h1) != (w0, h0):
            resize_note = f"  \u00b7  resized {w0}x{h0} -> {w1}x{h1} ({resp['resize']})"
    rgb0, pad_note = apply_pad(rgb0, a)
    if rgb0.shape[0] % 8 or rgb0.shape[1] % 8:
        raise SystemExit(f"image sides must be multiples of 8: {rgb0.shape[1]}x{rgb0.shape[0]}")

    split = False
    plane_note = ""
    hh, ww = rgb0.shape[:2]
    if hh > PLANE_MAX or ww > PLANE_MAX:
        if getattr(a, "oversize", None):
            plane = a.oversize
            print(f"plane limit: {plane} (from the command line)")
        elif getattr(a, "no_ask", False):
            plane = (profile["preferences"].get("oversize") or "resize")
            print(f"[-no-ask] plane limit: {plane} (stored fallback)")
        else:
            plane = _ask(f"9) {ww}x{hh} is over the {PLANE_MAX}x{PLANE_MAX} plane limit:"
                         " split into segments, resize to fit, or keep the resolution"
                         " (none)?", "resize", ["split", "resize", "none"])
        resp["oversize"] = plane
        if plane == "split":
            split = True
        elif plane == "none":
            plane_note = (f"  \u00b7  kept at {rgb0.shape[1]}x{rgb0.shape[0]}, over the"
                          " 512x512 plane limit (the ROM has to deal with it)")
        else:
            rgb0 = scale_to_fit(rgb0)
            rgb0, pad_note = apply_pad(rgb0, a)
            plane_note = f"  \u00b7  shrunk to {rgb0.shape[1]}x{rgb0.shape[0]} (plane limit)"

    gain = auto_gain(rgb0) if getattr(a, "gain", "auto") in (None, "auto") else float(a.gain)
    rgb = prepare(rgb0, gain, float(getattr(a, "brightness", 0) or 0))
    resp["gain"] = gain
    resp["brightness"] = float(getattr(a, "brightness", 0) or 0)

    print(f"\nsource: {os.path.basename(a.source)}  {rgb0.shape[1]}x{rgb0.shape[0]}  \u00b7  "
          f"gain x{gain:.2f}{alpha_note}{resize_note}{plane_note}{pad_note}")
    if resp.get("resize") == "height" and rgb0.shape[1] < RESIZE_W:
        print(f"warning: {rgb0.shape[1]} px wide, narrower than the screen ({RESIZE_W})")
    if resp.get("resize") == "width" and rgb0.shape[0] > RESIZE_H:
        print(f"warning: {rgb0.shape[0]} px tall, taller than the screen ({RESIZE_H});"
              " needs a vertical scroll or a crop")
    # index 0 is always the backdrop: one colour, shared by every palette.
    # The art lives on 1..15, so a plane behind shows through wherever the
    # image uses the backdrop colour. It must be a real MD colour.
    if resp.get("backdrop") == "color" and resp.get("backdrop_rgb"):
        chave = parse_fill(resp["backdrop_rgb"])
    else:
        chave = border_key(rgb)
    # the source colour finds the backdrop pixels; the snapped one goes to CRAM
    resp["view_height"] = WIN_H * 8
    resp["key_src"] = [int(v) for v in np.asarray(chave).reshape(-1)[:3]]
    if resp.get("backdrop") == "color" and resp.get("backdrop_rgb"):
        resp["key_holes"] = True    # the author chose the colour: keep the holes
    else:
        real, _, why = backdrop_is_real(rgb, resp["key_src"], a.source)
        resp["key_holes"] = bool(real)
        if not real:
            print(f"note: border colour not used as transparent ({why}); index 0 "
                  "is still the backdrop colour, but the art keeps every pixel")
    chave = snap_md(np.asarray(chave, np.float64).reshape(1, 1, 3))[0, 0]
    resp["key"] = [int(v) for v in chave]
    resp["sprite"] = transparent_fraction(a.source) > 0.05
    resp["transparency"] = True
    print(f"backdrop: #{int(chave[0]):02X}{int(chave[1]):02X}{int(chave[2]):02X}"
          " -> index 0 of every palette (the VDP always shows the backdrop there)")
    if resp.get("max_tiles"):
        escopo = f"per {view_label()} view" if resp.get("cap_scope") == "view" else "for the whole image"
        print(f"tile cap: {resp['max_tiles']} unique tiles {escopo}")
    print(f"palette: {resp['palettes']} \u00b7 "
          f"{'file ' + resp['palette_file'] if resp['palette_file'] else 'own'}"
          f" \u00b7 {resp['mode']} \u00b7 transparency={'yes' if resp['transparency'] else 'no'}"
          f" \u00b7 {resp['dithering']} \u00b7 per tile")
    print("samples: 1=A fidelity \u00b7 2=B worst case \u00b7 3=C balance worst \u00b7 4=D balance")

    nome_base = os.path.splitext(os.path.basename(a.source))[0]
    # the comparison images go where the tool was run, not into a fixed folder
    # of the project: a subfolder named after the image, so two runs never
    # overwrite each other (use --out . to drop the files right in the cwd)
    raw_folder = os.path.normpath(a.out or os.path.join(os.getcwd(), nome_base))
    folder = (raw_folder if os.path.abspath(raw_folder) == os.getcwd()
              else make_unique_dir(raw_folder))
    os.makedirs(folder, exist_ok=True)
    setattr(a, "actual_out", folder)   # the GUI reads this after cmd_sample
    base = os.path.basename(a.source)

    if not split:
        run_samples(rgb, resp, a, folder, base, export_name=c_name(nome_base))
        return 0

    segs = segments_of(rgb.shape[0], rgb.shape[1])
    print(f"plane limit: {rgb.shape[1]}x{rgb.shape[0]} -> {len(segs)} segment(s)")
    plan = []
    for i, (x0, x1, y0, y1) in enumerate(segs, 1):
        name = f"seg{i:02d}_{x1 - x0}x{y1 - y0}"
        sub = rgb[y0:y1, x0:x1]
        print(f"\n-- segment {i}/{len(segs)}: {x1 - x0}x{y1 - y0} at ({x0},{y0})")
        run_samples(sub, resp, a, os.path.join(folder, name), f"{base} {name}",
                    export_name=c_name(f"{nome_base}_{name}"))
        plan.append({"name": name, "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0,
                     "folder": os.path.abspath(os.path.join(folder, name))})
    json.dump({"source": os.path.abspath(a.source), "width": rgb.shape[1], "height": rgb.shape[0],
               "plane_max": PLANE_MAX, "segments": plan},
              open(os.path.join(folder, "split.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\nsplit plan: {os.path.join(folder, 'split.json')}")
    return 0


def parse_choice(s):
    s = str(s).strip().upper()
    if s and s in LETTERS:
        return LETTERS.index(s) + 1
    n = int(s)
    if n < 1 or n > 4:
        raise SystemExit("choice must be 1..4 or A..D")
    return n


def cmd_choose(a):
    folder = a.samples
    path = os.path.join(folder, "samples.json") if os.path.isdir(folder) else folder
    data = json.load(open(path, encoding="utf-8"))
    n = parse_choice(a.choice)
    chosen = data["samples"][n - 1]
    json.dump({"folder": os.path.abspath(os.path.dirname(path)), "choice": chosen,
               "answers": data["answers"], "variants": data["variants"]},
              open(os.path.join(os.path.dirname(path), "choice.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    profile = load_profile()
    p = profile["preferences"]
    p["recipe"] = chosen["recipe"]
    p["palettes"] = data["answers"]["palettes"]
    p["palette_file"] = data["answers"].get("palette_file")
    p["mode"] = data["answers"]["mode"]
    p["transparency"] = data["answers"]["transparency"]
    p["dithering"] = data["answers"]["dithering"]
    p["dither_strength"] = data["answers"].get("dither_strength", DITHER_DEFAULT)
    p["scroll"] = data["answers"].get("scroll", False)
    p["resize"] = data["answers"].get("resize", "none")
    p["max_tiles"] = data["answers"].get("max_tiles")
    p["cap_scope"] = data["answers"].get("cap_scope", "image")
    p["oversize"] = data["answers"].get("oversize", "resize")
    if chosen.get("vq"):
        p["vq"] = chosen["vq"]
        p["lambda"] = min(0.9, p.get("lambda", 0.35) + 0.10)
    else:
        p["lambda"] = max(0.05, p.get("lambda", 0.35) - 0.10)
    profile["choices"].append({"source": data["source"], "n": n, "letter": chosen["letter"],
                               "recipe": chosen["recipe"], "tiles": chosen["tiles"],
                               "de_mean": chosen["de_mean"]})
    save_profile(profile)
    print(f"chose {chosen['letter']} ({n}): {chosen['label']} · "
          f"{chosen['tiles']} tiles · dE {chosen['de_mean']:.2f}")
    print(f"stored: recipe={p['recipe']} · lambda={p['lambda']:.2f} · "
          f"{len(profile['choices'])} choice(s); used only when you ask for --no-ask.")
    return 0


def cmd_emit(a):
    path = os.path.join(a.samples, "samples.json") if os.path.isdir(a.samples) else a.samples
    # the samples were made with known answers; v1 asked again (or, with
    # --no-ask, took the stored profile), so the report could name another mode
    answers = json.load(open(path, encoding="utf-8")).get("answers")
    resp = dict(answers) if answers else ask(a, load_profile())
    res = a.res or os.path.join(os.getcwd(), "res", f"{a.name}.res")
    return do_emit(path, a.name, res, resp, a.n)


def do_emit(path, name, res, resp, n_arg=None):
    """Export one sample: indexed image, palettes, .res, map JSON and preview.

    Split out of cmd_emit so the sample flow can call it right after the choice,
    without asking the setup questions a second time.
    """
    data = json.load(open(path, encoding="utf-8"))
    set_view_height(int(data.get("answers", {}).get("view_height") or 240))
    if n_arg:
        n = parse_choice(n_arg)
        chosen = data["samples"][n - 1]
    else:
        chosen = json.load(open(os.path.join(os.path.dirname(path), "choice.json"),
                                encoding="utf-8"))["choice"]
        n = chosen["n"]
    im = Image.open(os.path.join(os.path.dirname(path), chosen["indexed"]))
    assert im.mode == "P", "the sample PNG should be indexed"

    # Measure the exported 4-bit indices (palette and priority are map
    # attributes). Doing it here also fixes older samples.json files that were
    # written before the sliding 320x240 measurement existed.
    med = window_peak_of(np.array(im, np.uint8) & 0x0F)
    chosen = dict(chosen)
    chosen["window_tiles"] = med[0]
    chosen["window_x0"], chosen["window_y0"] = med[1] * 8, med[2] * 8
    chosen["window_x1"], chosen["window_y1"] = med[3] * 8, med[4] * 8
    chosen["window_count"] = med[5]

    res_dir = os.path.dirname(res)
    os.makedirs(res_dir, exist_ok=True)
    png = os.path.join(res_dir, f"{name}16.png")
    im.save(png)

    pals = [np.asarray(p, np.uint8) for p in chosen["palettes"]]
    for i, pal in enumerate(pals):
        with open(os.path.join(res_dir, f"{name}_pal{i}.pal"), "w",
                  encoding="ascii", newline="") as f:
            f.write("JASC-PAL\r\n0100\r\n%d\r\n" % len(pal))
            for c in pal:
                f.write(f"{int(c[0])} {int(c[1])} {int(c[2])}\r\n")

    # shadow/highlight are not spare palettes: the VDP applies them to the same
    # CRAM. Shadow is the tile priority bit, already carried by the PNG (bit 7).
    lines = []
    for i in range(len(pals)):
        lines.append(f'PALETTE {name}_pal{i} "{name}_pal{i}.pal"')
    lines.append(f'IMAGE {name} "{name}16.png" BEST ALL')
    open(res, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

    c0 = np.asarray(pals[0][0], np.uint8)   # index 0: the shared backdrop
    n0 = [_nivel(int(v)) for v in c0]       # exact 3-bit levels of the MD colour
    vdp = (n0[0] << 1) | (n0[1] << 5) | (n0[2] << 9)
    report = [f"# {name}", ""]
    if data.get("link"):
        report += [f"[comparison sheet]({data['link']})", ""]
    report += [f"sample {n} ({chosen['letter']}): {chosen['label']}",
               f"tiles: {chosen['tiles']} ({chosen['bytes_tiles']} B) + map {chosen['bytes_map']} B",
               f"dE mean {chosen['de_mean']:.2f} · p95 {chosen['de_p95']:.2f}", ""]
    resp_emit = data.get("answers", {})
    win_w = chosen.get("window_x1", 0) - chosen.get("window_x0", 0)
    win_h = chosen.get("window_y1", 0) - chosen.get("window_y0", 0)
    win_w = win_w or 320
    win_h = win_h or WIN_H * 8
    report += ["## tile measurement", "",
               f"- window: {win_w}x{win_h} px ({win_w // 8}x{win_h // 8} tiles)",
               "- step: 8 px (1 tile) on both axes, from the top to the last complete window",
               f"- windows checked: {chosen.get('window_count', 0)}",
               "- dedup: identical or mirrored tiles (H, V and HV) count once; the "
               "comparison uses the 4-bit colour indices only (palette and "
               "normal/shadow priority are map attributes, not part of the drawing)",
               f"- peak: {chosen.get('window_tiles', 0)} unique tiles at "
               f"x={chosen.get('window_x0', 0)}..{chosen.get('window_x1', 0)} px, "
               f"y={chosen.get('window_y0', 0)}..{chosen.get('window_y1', 0)} px "
               "(end exclusive)",
               f"- whole image: {chosen['tiles']} unique tiles"]
    if resp_emit.get("cap_scope") == "view" and resp_emit.get("max_tiles"):
        dentro = chosen.get("window_tiles", 0) <= resp_emit["max_tiles"]
        report += [f"- tile cap: {resp_emit['max_tiles']} per 320x240 view — "
                   + ("respected in every window" if dentro else "NOT respected")]
    report += [""]
    report += ["## palettes", ""]
    for i, pal in enumerate(pals):
        report.append(f"### palette {i}")
        for j, c in enumerate(pal):
            report.append(f"- {j:2d}: #{int(c[0]):02X}{int(c[1]):02X}{int(c[2]):02X}")
    # ---- the C side: plane A only, window off, free scroll on the pad
    img_w, img_h = im.size
    tiles_w, tiles_h = (img_w + 7) // 8, (img_h + 7) // 8
    plane_w = 64 if tiles_w > 32 else 32
    plane_h = 64 if tiles_h > 32 else 32
    max_sx, max_sy = max(0, img_w - 320), max(0, img_h - 224)

    report += ["", "## C", "",
               f"{img_w}x{img_h} px = {tiles_w}x{tiles_h} tiles, plane {plane_w}x{plane_h}.",
               "The image goes on plane A only: the window plane stays off and plane B "
               "is left empty.", ""]
    if max_sx or max_sy:
        pans = ([f"{max_sx} px across"] if max_sx else []) + \
               ([f"{max_sy} px down"] if max_sy else [])
        report += [f"Bigger than the 320x224 display, so the pad pans it freely: "
                   f"up to {' and '.join(pans)}.", ""]
    if tiles_w > 64 or tiles_h > 64:
        report += [f"ATENCAO: {tiles_w}x{tiles_h} tiles do not fit one plane (at most "
                   "64x64 tiles = 512x512 px). Split or resize the image.", ""]
    report += ["```c", f"extern const Image {name};"]
    for i in range(len(pals)):
        report.append(f"extern const Palette {name}_pal{i};")
    report += [
        "",
        f"#define IMG_W  {img_w}",
        f"#define IMG_H  {img_h}",
        f"#define MAX_SX {max_sx:<4}       // 0 when the image fits the 320 px of the screen",
        f"#define MAX_SY {max_sy:<4}       // 0 when the image fits the 224 px of the screen",
        "",
        "int main(bool hardReset)",
        "{",
        "    s16 sx = 0, sy = 0;",
        "    u16 joy;",
        "",
        "    VDP_setScreenWidth320();",
        "    VDP_setScreenHeight224();",
        "",
        "    // nothing is ever drawn on the window plane",
        "    VDP_setWindowOff();",
        "",
        f"    // backdrop: #{int(c0[0]):02X}{int(c0[1]):02X}{int(c0[2]):02X}"
        f" = CRAM 0x{vdp:04X}, index 0 of every palette.",
        "    // A plane behind this one shows through wherever the art uses index 0.",
        "    VDP_setBackgroundColor(0);",
        "",
        f"    VDP_setPlaneSize({plane_w}, {plane_h}, TRUE);",
        "    VDP_clearPlane(BG_A, TRUE);",
        "",
        f"    VDP_drawImageEx(BG_A, &{name},",
        "                    TILE_ATTR_FULL(PAL0, FALSE, FALSE, FALSE, TILE_USER_INDEX),",
        "                    0, 0, FALSE, TRUE);",
        "",
    ]
    for i in range(len(pals)):
        report.append(f"    PAL_setPalette(PAL{i}, {name}_pal{i}.data, DMA);")
    shadow_rows = chosen.get("shadow_rows")
    if shadow_rows:
        n_sh = sum(r.count("1") for r in shadow_rows)
        report += ["",
                   f"    // {n_sh} tiles are marked LOW priority in the map (PNG bit 7),",
                   "    // so the shadow/highlight circuit darkens exactly those tiles.",
                   "    // Highlight would need a sprite pixel of PAL3 index 14: it is not",
                   "    // encoded here, because a plane tile has no highlight attribute.",
                   "    // NOTE: with shadow/highlight on, the backdrop (index 0) is also",
                   "    // shown at half brightness, and so is every low priority pixel",
                   "    // of plane B and of the sprites.",
                   "    VDP_setHilightShadow(TRUE);"]
    elif resp["mode"] in ("highlight", "both"):
        report += ["",
                   "    // highlight is measured but not encoded: on hardware it needs a",
                   "    // sprite pixel of PAL3 index 14 over the area (see the report)."]
    report += [
        "",
        "    while (TRUE)",
        "    {",
        "        joy = JOY_readJoypad(JOY_1);",
        "        if (joy & BUTTON_LEFT)  sx -= 2;",
        "        if (joy & BUTTON_RIGHT) sx += 2;",
        "        if (joy & BUTTON_UP)    sy -= 2;",
        "        if (joy & BUTTON_DOWN)  sy += 2;",
        "        if (sx < 0)      sx = 0;",
        "        if (sx > MAX_SX) sx = MAX_SX;",
        "        if (sy < 0)      sy = 0;",
        "        if (sy > MAX_SY) sy = MAX_SY;",
        "",
        "        // the VDP pushes the plane content right by the scroll value, so the",
        "        // camera only pans right when the value is negative",
        "        VDP_setHorizontalScroll(BG_A, -sx);",
        "        VDP_setVerticalScroll(BG_A, sy);",
        "",
        "        SYS_doVBlankProcess();",
        "    }",
        "    return 0;",
        "}",
        "```", ""]
    report += ["", "## files", "",
               f"- `{name}16.png` - indexed image: bits 0-3 colour, 4-5 palette, "
               "7 priority (0 = the VDP darkens that tile). Colours are the base "
               "palettes, never pre-darkened.",
               f"- `{name}_pal0..{len(pals) - 1}.pal` - the base palettes. Index 0 "
               f"is the same everywhere: #{int(c0[0]):02X}{int(c0[1]):02X}{int(c0[2]):02X}"
               f" (CRAM 0x{vdp:04X}); indices 1-15 are the palette's own 15 colours.",
               f"- `{name}.res` - rescomp resource.",
               f"- `{name}_map.json` - palette and normal/shadow for every tile "
               "position (row-major). The same drawing may be normal in one place "
               "and shadow in another, so this is per position.",
               f"- `{name}_preview.png` - RGB preview of what the result shows.",
               "",
               f"Backdrop: #{int(c0[0]):02X}{int(c0[1]):02X}{int(c0[2]):02X}"
               f" (CRAM 0x{vdp:04X}) at index 0 of all {len(pals)} palettes. The "
               "VDP always shows the backdrop where a plane pixel is index 0, so "
               "this is the colour to set as the backdrop - and any plane behind "
               "will show through there.",
               ""]
    open(os.path.join(os.path.dirname(path), "report.md"), "w", encoding="utf-8").write("\n".join(report))

    show_link("png", png)
    show_link("res", res)

    # map by position: palette and normal/shadow for every cell. The shadow is a
    # property of the position, not of the drawing: the same tile can be normal
    # in one place and shadowed in another.
    tp = chosen.get("tile_pal")
    if tp:
        nh, nw = len(tp), len(tp[0])
        sh = chosen.get("shadow_rows")
        sh_flat = "".join(sh) if sh else "0" * (nh * nw)
        mapa = {
            "image": name,
            "tile_w": nw,
            "tile_h": nh,
            "palettes": len(pals),
            "backdrop_rgb": f"#{int(c0[0]):02X}{int(c0[1]):02X}{int(c0[2]):02X}",
            "backdrop_cram": f"0x{vdp:04X}",
            "pal": "".join(str(int(v)) for row in tp for v in row),
            "shadow": sh_flat,
            "note": ("row-major: index = y * tile_w + x. 'pal' is the palette "
                     "digit of the tile, 'shadow' is 1 where the VDP must darken "
                     "it (low priority). The same tile may appear normal in one "
                     "place and shadow in another, so both are per position. "
                     "Index 0 of every palette is the backdrop colour; a plane "
                     "behind shows through where the art uses index 0."),
        }
        mapa_path = os.path.join(res_dir, f"{name}_map.json")
        with open(mapa_path, "w", encoding="utf-8") as f:
            json.dump(mapa, f, ensure_ascii=False, indent=1)
        show_link("map", mapa_path)

    # the RGB preview of the chosen sample, next to the export
    prev = os.path.join(os.path.dirname(path), chosen.get("image", ""))
    if os.path.exists(prev):
        destino = os.path.join(res_dir, f"{name}_preview.png")
        shutil.copyfile(prev, destino)
        show_link("preview", destino)

    print(f"palettes: {len(pals)} \u00b7 mode: {resp['mode']}"
          + (" \u00b7 shadow per tile written into the map" if chosen.get("shadow_rows") else "")
          + (" \u00b7 highlight measured only" if resp["mode"] in ("highlight", "both") else ""))
    return 0


# --------------------------------------------------------------------------
# measure (period and texture)
# --------------------------------------------------------------------------
def guess_scale(imgs, m):
    best, found = None, 1.0
    candidates = []
    for i in range(len(imgs) - 1):
        d = (m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1])
        if abs(d[0]) + abs(d[1]) < 8:
            continue
        a, b = imgs[i].astype(np.int16), imgs[i + 1].astype(np.int16)
        h, w = a.shape[:2]
        for s in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
            sx, sy = -int(round(d[0] * s)), -int(round(d[1] * s))
            if abs(sx) >= w or abs(sy) >= h:
                continue
            x0, x1 = max(0, -sx), min(w, w - sx)
            y0, y1 = max(0, -sy), min(h, h - sy)
            if x1 - x0 < 32 or y1 - y0 < 32:
                continue
            e = float(np.abs(a[y0:y1, x0:x1] - b[y0 + sy:y1 + sy, x0 + sx:x1 + sx]).mean())
            candidates.append((e, s))
        if candidates:
            break
    if candidates:
        # a periodic texture ties many scales: prefer the one closest to 1
        e_min = min(c[0] for c in candidates)
        good = [c for c in candidates if c[0] <= e_min + 0.25]
        best, found = min(good, key=lambda c: (abs(np.log2(c[1])), c[0]))
    return found, best


def build_canvas(imgs, m, s):
    if any(im.shape != imgs[0].shape for im in imgs):
        raise SystemExit("all photos must have the same size")
    h, w = imgs[0].shape[:2]
    xs = [mi[0] * s for mi in m]
    ys = [mi[1] * s for mi in m]
    x0, y0 = int(np.floor(min(xs))), int(np.floor(min(ys)))
    W = int(np.ceil(max(xs) + w)) - x0 + 1
    H = int(np.ceil(max(ys) + h)) - y0 + 1
    total = np.zeros((H, W, 3), np.float64)
    count = np.zeros((H, W), np.float64)
    for im, xi, yi in zip(imgs, xs, ys):
        x, y = int(round(xi)) - x0, int(round(yi)) - y0
        total[y:y + h, x:x + w] += im
        count[y:y + h, x:x + w] += 1
    valid = count > 0
    canvas = np.zeros((H, W, 3), np.uint8)
    canvas[valid] = (total[valid] / count[valid][:, None]).astype(np.uint8)
    return canvas, count, (x0, y0)


def period_error(canvas, count, axis, p):
    if axis == 0:
        a, b = canvas[:, :-p], canvas[:, p:]
        ma, mb = count[:, :-p] > 0, count[:, p:] > 0
    else:
        a, b = canvas[:-p], canvas[p:]
        ma, mb = count[:-p] > 0, count[p:] > 0
    ok = ma & mb
    if ok.sum() < 0.25 * ok.size:
        return None
    return float(np.abs(a[ok].astype(np.int16) - b[ok].astype(np.int16)).mean())


def find_period(canvas, count, step=4):
    result = {}
    for axis, size in ((0, canvas.shape[1]), (1, canvas.shape[0])):
        cand = []
        for p in range(8, size // 2, step):
            e = period_error(canvas, count, axis, p)
            if e is not None:
                cand.append((e, p))
        cand.sort()
        fine = []
        for e, p in cand[:3]:
            for pp in range(max(8, p - step + 1), p + step):
                ee = period_error(canvas, count, axis, pp)
                if ee is not None:
                    fine.append((ee, pp))
        fine.sort()
        result["x" if axis == 0 else "y"] = {"best": fine[0][1] if fine else None,
                                             "error": fine[0][0] if fine else None,
                                             "top5": [{"p": p, "error": round(e, 2)} for e, p in fine[:5]]}
    return result


def cmd_measure(a):
    photos = sorted(glob.glob(os.path.join(a.photos, "*.png")))
    if not photos:
        raise SystemExit("no photos in the folder")
    lines = [l.split() for l in open(a.grid, encoding="utf-8")
             if l.strip() and not l.lstrip().startswith("#")]
    m = [(int(l[1]), int(l[2])) for l in lines[-len(photos):]]
    photos = photos[:len(m)]
    imgs = [load(f) for f in photos]
    if not getattr(a, "no_pad", False):
        fill = parse_fill(getattr(a, "pad_color", None))
        imgs = [pad8(im, fill)[0] for im in imgs]
    s, e = (guess_scale(imgs, m) if a.scale == "auto" else (float(a.scale), None))
    print(f"scale: {s} px/unit" + (f" (error {e:.2f})" if e is not None else ""))
    canvas, count, origin = build_canvas(imgs, m, s)
    per = find_period(canvas, count, a.step)
    print(f"texture: {canvas.shape[1]}x{canvas.shape[0]}  coverage {100 * (count > 0).mean():.0f}%")
    print(f"period X: {per['x']['best']} (error {per['x']['error']:.2f}) · "
          f"Y: {per['y']['best']} (error {per['y']['error']:.2f})")
    raw_folder = os.path.normpath(a.out or os.path.join(os.path.dirname(a.photos), "measured"))
    folder = (raw_folder if os.path.abspath(raw_folder) == os.getcwd()
              else make_unique_dir(raw_folder))
    os.makedirs(folder, exist_ok=True)
    Image.fromarray(canvas).save(os.path.join(folder, "texture.png"))
    cov = np.zeros_like(canvas)
    cov[count > 0] = 255
    Image.fromarray(cov).save(os.path.join(folder, "coverage.png"))
    json.dump({"scale": s, "origin": origin, "period": per,
               "texture": list(canvas.shape[1::-1]), "photos": [os.path.basename(f) for f in photos]},
              open(os.path.join(folder, "measured.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    show_link("texture", os.path.join(folder, "texture.png"))
    show_link("measured", os.path.join(folder, "measured.json"))
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def selftest():
    ok = 0
    for v in LEVELS:
        c = np.array([[[v, v, v]]], np.uint8)
        assert np.array_equal(snap_md(c), c), v
    print("ok 1: the 8 MD levels snap to themselves (rescomp converts with no loss)")
    ok += 1

    PX, PY = 96, 64
    rnd = np.random.default_rng(7)
    base = np.zeros((PY, PX, 3), np.uint8)
    yy, xx = np.mgrid[0:PY, 0:PX]
    # a single dark-brown family, like the wall: 16 colors can handle it
    t = np.clip(0.5 + 0.45 * np.sin(xx / 11.0) * np.cos(yy / 7.0)
                + 0.10 * np.sin((xx + 2 * yy) / 5.0), 0, 1)
    base[..., 0] = (40 + 170 * t).astype(np.uint8)
    base[..., 1] = (25 + 140 * t).astype(np.uint8)
    base[..., 2] = (15 + 110 * t).astype(np.uint8)
    for _ in range(40):
        x, y = int(rnd.integers(0, PX - 8)), int(rnd.integers(0, PY - 8))
        v = float(rnd.uniform(0.15, 0.95))
        base[y:y + 6, x:x + 6] = (np.array([40, 25, 15]) + np.array([170, 140, 110]) * v).astype(np.uint8)
    big = np.tile(base, (8, 8, 1))

    pos = [(0, 0), (128, 0), (256, 0), (0, 96), (128, 96), (256, 96),
           (64, 48), (192, 48), (320, 48)]
    imgs = [big[y:y + 224, x:x + 352] for x, y in pos]
    s, e = guess_scale(imgs, pos)
    assert abs(s - 1.0) < 1e-6, s
    canvas, count, _ = build_canvas(imgs, pos, s)
    per = find_period(canvas, count, 4)
    assert per["x"]["best"] == PX, per["x"]
    assert per["y"]["best"] == PY, per["y"]
    print(f"ok 2: period found {per['x']['best']}x{per['y']['best']} (expected {PX}x{PY})")
    ok += 1

    rgb = big[:224, :352].copy()
    resp = {"palettes": 1, "transparency": False, "dithering": "none", "mode": "normal",
            "palette_file": None}
    bands = bands_of(rgb.shape[0], 1)
    pals = palettes_per_band(rgb, resp, bands, None, 3)
    m = metrics(rgb, build(rgb, pals, resp)["rgb"])
    grid_error = metrics(rgb, snap_md(rgb))["de_mean"]
    assert m["de_mean"] < grid_error + 0.5, (m, grid_error)
    print(f"ok 3: 16 colors, dE mean {m['de_mean']:.2f} "
          f"(the MD grid alone already costs {grid_error:.2f})")
    ok += 1

    res = build(rgb, pals, resp)
    uniq, tilemap = res["uniq"], res["tilemap"]
    rec = rebuild(uniq, tilemap, *res["idx"].shape)
    assert np.array_equal(rec, res["idx"])
    print(f"ok 4: {res['idx'].shape[0] // 8 * res['idx'].shape[1] // 8} tiles -> {len(uniq)} unique, "
          f"exact rebuild")
    ok += 1

    target = max(8, int(len(uniq) * 0.5))
    idx2, tp2 = reduce_tiles(res["idx"], pals, res["tile_pal"], target, 5)
    uniq2, _ = dedup(to_tiles(idx2))
    assert len(uniq2) <= target, (len(uniq2), target)
    m2 = metrics(rgb, np.asarray(pals, np.uint8)[np.repeat(np.repeat(tp2, 8, 0), 8, 1), idx2])
    assert m2["de_mean"] < 12.0, m2
    print(f"ok 5: VQ {len(uniq)} -> {len(uniq2)} tiles (target {target}), dE {m2['de_mean']:.2f}")
    ok += 1

    for mode in DITHERS:
        r = dict(resp, dithering=mode)
        i, _ = paint(rgb, pals, r, bands)
        assert i.shape == res["idx"].shape and i.max() < 16
    print("ok 6: all dithering modes run and stay inside the palette")
    ok += 1

    resp2 = dict(resp, palettes=2, dithering="none")
    b2 = bands_of(rgb.shape[0], 2)
    p_band = palettes_per_band(rgb, resp2, b2, None, 9)
    i_band, bt = paint(rgb, p_band, resp2, b2)
    de_band = metrics(rgb, np.asarray(p_band, np.uint8)[
        np.repeat(bt, 8, 0)[:, None], i_band])["de_mean"]
    p2, _, tp2 = optimize_palettes(rgb, resp2, 9)
    r2 = build(rgb, p2, resp2)
    assert len(p2) == 2 and r2["tile_pal"].shape == (rgb.shape[0] // 8, rgb.shape[1] // 8)
    assert r2["de_mean"] <= de_band + 0.05, (r2["de_mean"], de_band)
    usage = np.bincount(tp2.reshape(-1), minlength=2).tolist()
    print(f"ok 7: palettes per tile {usage}, dE {r2['de_mean']:.2f} "
          f"(bands gave {de_band:.2f})")
    ok += 1

    # ---- v2 regressions -------------------------------------------------
    for H in (HALFTONE8, HALFTONE4):
        q = H.shape[0] // 2
        assert sorted(H.ravel().tolist()) == list(range(H.size))
        diag = H[:q, :q].mean() + H[q:, q:].mean()
        anti = H[:q, q:].mean() + H[q:, :q].mean()
        assert diag < anti, (diag, anti)
    print("ok 8: halftone is a 0..63 permutation with the dots on the diagonal")
    ok += 1

    greys = np.array([[0, 0, 0], [87, 87, 87], [144, 144, 144], [255, 255, 255]], np.uint8)
    lp = rgb_to_lab(greys.astype(np.float64))
    flatg = np.full((64, 64, 3), 115, np.uint8)
    labg = rgb_to_lab(flatg.reshape(-1, 3).astype(np.float64))
    for name, fn in (("riemersma", lambda: _riemersma(labg, lp, (64, 64), 1.0)),
                     ("floyd", lambda: floyd(labg, lp, (64, 64), 1.0))):
        o = fn()
        used = set(np.unique(o).tolist())
        assert used <= {1, 2}, (name, used)
        mean = lp[o.ravel()].mean(0)
        assert abs(mean[0] - labg[0, 0]) < 2.0, (name, mean, labg[0])
    print("ok 9: riemersma and floyd on a flat grey use only the two neighbours, "
          "mean on target")
    ok += 1

    img = np.full((16, 16, 3), 250, np.uint8)
    img[4:12, 4:12] = (40, 60, 160)
    r_key = {"key": [255, 255, 255], "key_src": [int(v) for v in border_key(img)]}
    assert int(backdrop_mask(img, r_key).sum()) == 16 * 16 - 64
    print("ok 10: a 250,250,250 border is found as backdrop (snapped key 255)")
    ok += 1

    sp = shadow_palette(np.array([[255, 255, 255]], np.uint8))[0]
    hp = highlight_palette(np.array([[144, 144, 144]], np.uint8))[0]
    assert tuple(sp) == (130, 130, 130) and tuple(hp) == (187, 187, 187), (sp, hp)
    print("ok 11: shadow 255 -> 130, highlight 144 -> 187 (15-step DAC)")
    ok += 1

    rr = np.random.default_rng(3)
    big_idx = np.repeat(np.repeat(rr.integers(1, 16, (64, 100)), 4, 0), 4, 1).astype(np.uint8)
    tp0 = np.zeros((big_idx.shape[0] // 8, big_idx.shape[1] // 8), np.uint8)
    gp = [np.vstack([[[0, 0, 0]], MD_GRID_RGB[::34][:15]]).astype(np.uint8)]
    before = max_window_tiles(big_idx)
    budget = int(before * 0.5)
    for name, fn in (("global", reduce_tiles_windows), ("local", reduce_tiles_windows_local)):
        ri, _ = fn(big_idx, gp, tp0, budget, 1)
        peak = max_window_tiles(ri)
        assert peak <= budget, (name, peak, budget)
        assert peak >= 0.8 * budget, (name, peak, budget)
        ok += 1
        print(f"ok {ok}: {name} view reduction {before} -> {peak} (cap {budget})")

    # mirrored copies must survive a reduction that has nothing to cut
    base_t = rr.integers(1, 16, (8, 8)).astype(np.uint8)
    row = np.concatenate([base_t, base_t[:, ::-1], base_t[::-1], base_t[::-1, ::-1]], 1)
    mir = np.tile(row, (4, 2))
    tpm = np.zeros((mir.shape[0] // 8, mir.shape[1] // 8), np.uint8)
    out_m, _ = reduce_tiles_windows(mir, gp, tpm, 64, 0)
    assert np.array_equal(out_m, mir), "mirrored tiles changed with nothing to merge"
    ok += 1
    print(f"ok {ok}: mirrored tiles come back mirrored")

    # a merge is judged in the palette of the position that gets replaced:
    # flat index 2 is blue in palette 0 and white in palette 1
    p0 = np.array([[0, 0, 0]] * 16, np.uint8)
    p1 = p0.copy()
    p0[2], p0[3], p1[2] = (52, 87, 206), (52, 87, 172), (255, 255, 255)
    # flat 3 (blue, palette 0) is the common one; flat 2 sits in palette 1,
    # where index 3 is black. Keeping the common drawing would paint the
    # palette 1 tiles black; the right merge keeps flat 2 (blue in palette 0).
    tl = np.zeros((16, 24), np.uint8)
    tl[:, :16], tl[:, 16:] = 3, 2
    tpx = np.array([[0, 0, 1], [0, 0, 1]], np.uint8)
    outx, _ = reduce_tiles_windows(tl, [p0, p1], tpx, 1, 0)
    assert max_window_tiles(outx) <= 1
    assert (outx == 2).all(), "merge ignored the palette of the replaced position"
    ok += 1
    print(f"ok {ok}: merges are judged in the palette of the replaced position")

    import tempfile
    pp = [np.tile(np.array([[10 * (p + 1), 0, 0]], np.uint8), (16, 1)) for p in range(2)]
    ii = np.ones((8, 16), np.uint8)
    tpp = np.array([[0, 1]], np.uint8)
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "t.png")
        write_indexed_png(ii, pp, fp, tpp)
        with Image.open(fp) as im_t:
            back = np.asarray(im_t.convert("RGB"))
    assert tuple(back[0, 0]) == (10, 0, 0) and tuple(back[0, 15]) == (20, 0, 0), back[0]
    ok += 1
    print(f"ok {ok}: indexed PNG shows every palette, priority bit included")

    rd = dict(resp, dithering="floyd")
    rb = build(rgb, pals, rd)
    assert rb["de_perceived"] < rb["de_mean"], (rb["de_perceived"], rb["de_mean"])
    ok += 1
    print(f"ok {ok}: perceived dE {rb['de_perceived']:.2f} < raw {rb['de_mean']:.2f} "
          "with dithering")

    # shadow planned with the palettes: a picture with a bright half and the
    # same detail at half brightness. 1 palette cannot hold both sets of tones;
    # planned shadow draws the dark half with the shadowed copy of the bright
    # colours, so it must beat the old "decide at the end" shadow clearly.
    rng = np.random.default_rng(3)
    cols15 = MD_GRID_RGB.astype(np.uint8)[rng.choice(len(MD_GRID_RGB), 15, replace=False)]
    pick = rng.integers(0, 15, (4, 16, 2))
    tiles_b = np.zeros((4, 16, 8, 8, 3), np.uint8)
    tiles_b[:, :, :, :4] = cols15[pick[..., 0]][:, :, None, None]
    tiles_b[:, :, :, 4:] = cols15[pick[..., 1]][:, :, None, None]
    bright = tiles_b.transpose(0, 2, 1, 3, 4).reshape(32, 128, 3)
    pic = np.vstack([bright, shadow_palette(bright.reshape(-1, 3)).reshape(32, 128, 3)])
    rs = dict(palettes=1, palette_file=None, transparency=False, dithering="none",
              dither_strength="balanced", cap_scope="image", key=None, key_src=None)
    errs = {}
    for nome, extra in (("late", dict(mode="shadow", shadow_late=True)),
                        ("plan", dict(mode="shadow"))):
        rr = dict(rs, **extra)
        pp1, _, _ = optimize_palettes(pic, rr, 0, rounds=2)
        errs[nome] = build(pic, pp1, rr)
    sm = errs["plan"]["shadow_mask"]
    assert sm is not None and sm[4:].mean() > 0.7, "dark half not drawn in shadow"
    assert errs["plan"]["de_mean"] < 0.8 * errs["late"]["de_mean"], (
        errs["plan"]["de_mean"], errs["late"]["de_mean"])
    assert errs["plan"]["tile_pal"].max() < 1, "effective palette leaked out"
    ok += 1
    print(f"ok {ok}: planned shadow dE {errs['plan']['de_mean']:.2f} < late "
          f"{errs['late']['de_mean']:.2f}, {int(sm.sum())} shadow tiles")

    # highlight: a sprite over a shadow tile only cancels the shadow, the
    # exported dE never includes the highlight, and the sprite count covers a
    # 5x5 tile block with 4 sprites (4x4, 1x4, 4x1, 1x1)
    hs = highlight_sprites(np.pad(np.ones((5, 5), bool), ((1, 10), (2, 40))))
    assert hs["sprites"] == 4 and hs["sprite_tiles"] == 25, hs
    assert hs["line_max"] == 2 and hs["line_px_max"] == 40 and hs["fits"], hs
    pal_h = np.array([[0, 0, 0]] + [[116, 116, 116]] * 15, np.uint8)
    ih = np.ones((8, 16), np.uint8)
    pic_h = np.full((8, 16, 3), 172, np.uint8)       # highlight of 116 is 172
    painted_h = np.full((8, 16, 3), 116, np.uint8)
    painted_h[:, 8:] = 58                              # right tile is in shadow
    smh = np.array([[False, True]])
    _, _, nwin, _, img_h, winh = vdp_effects(pic_h, ih, np.zeros((1, 2), np.uint8),
                                             [pal_h], painted_h, "highlight", smh)
    assert winh[0, 0] and tuple(img_h[0, 0]) == (172, 172, 172), img_h[0, 0]
    assert tuple(img_h[0, 8]) == (116, 116, 116), "highlight over shadow must cancel"
    rb2 = build(pic, pp1, dict(rs, mode="both"))
    rb3 = build(pic, pp1, dict(rs, mode="shadow"))
    assert abs(rb2["de_mean"] - rb3["de_mean"]) < 1e-9, "both dE includes highlight"
    ok += 1
    print(f"ok {ok}: highlight cancels over shadow, sprites counted, "
          "exported dE without highlight")

    # a border that is part of the art (a gradient sky) is not a backdrop;
    # a uniform border is
    yy, xx = np.mgrid[0:64, 0:96]
    sky = np.stack([40 + yy, 60 + yy // 2, 100 + xx // 3], 2).astype(np.uint8)
    kk = border_key(sky)
    assert not backdrop_is_real(sky, kk)[0], "gradient border taken as backdrop"
    framed = sky.copy()
    framed[:4], framed[-4:], framed[:, :4], framed[:, -4:] = 0, 0, 0, 0
    assert backdrop_is_real(framed, border_key(framed))[0], "uniform border not a backdrop"
    assert not backdrop_mask(sky, {"key_src": kk, "key": kk, "key_holes": False}).any()

    # shadow on a smooth gradient: no tile-by-tile mosaic, and a hole on a
    # shadow tile counts as the darkened backdrop it shows
    grad = np.stack([60 + yy * 2, 70 + yy * 2, 120 + yy], 2).astype(np.uint8)
    rg = dict(palettes=1, palette_file=None, transparency=True, dithering="none",
              dither_strength="full", cap_scope="image", key=[0, 0, 0],
              key_src=[0, 0, 0], key_holes=False, mode="shadow")
    pg, _, _ = optimize_palettes(grad, rg, 0, rounds=2)
    smg = build(grad, pg, rg)["shadow_mask"]
    flips = int((smg[:, 1:] != smg[:, :-1]).sum())
    assert flips <= smg.shape[0], f"shadow mosaic on a smooth gradient: {flips} flips"
    holes = np.zeros((16, 16, 3), np.uint8)
    holes[:, :, 2] = 116
    holes[::2, ::2] = (116, 116, 116)    # a quarter of each tile is backdrop
    rh = dict(rg, key_holes=True, mode="shadow", key=[116, 116, 116],
              key_src=[116, 116, 116])
    blk = palette_options(holes, [np.array([[116, 116, 116]] + [[0, 0, 116]] * 15,
                                           np.uint8)], rh)
    assert blk[1][1][0, 0] > 0, "a hole on a shadow tile must cost the darkened backdrop"
    ok += 1
    print(f"ok {ok}: fake backdrop detected, no shadow mosaic ({flips} flips), "
          "holes counted on shadow tiles")

    print(f"\n{ok}/{ok} tests passed")
    return 0


# --------------------------------------------------------------------------
def _interativo():
    """True when a person is looking at a console window (the frozen .exe)."""
    if not getattr(sys, "frozen", False):
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _pausa(msg="\npress Enter to close..."):
    try:
        input(msg)
    except Exception:
        pass


def _grava_log(exc):
    """Never fail silently: put the traceback in a file next to the tool."""
    quando = time.strftime("%Y%m%d_%H%M%S")
    destino = os.path.join(ROOT, f"md_fundo_erro_{quando}.log")
    try:
        with open(destino, "w", encoding="utf-8") as f:
            f.write("md_fundo - error report\n")
            f.write(f"when:   {quando}\n")
            f.write(f"argv:   {sys.argv}\n")
            f.write(f"cwd:    {os.getcwd()}\n")
            f.write(f"frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"python: {sys.version}\n\n")
            if isinstance(exc, BaseException):
                f.write("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
            else:
                f.write(str(exc))
        return destino
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Mega Drive background: measure, sample, choose, emit.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("measure", aliases=["medir"], help="rebuild the texture and find the period")
    m.add_argument("--photos", required=True)
    m.add_argument("--grid", required=True)
    m.add_argument("--scale", default="auto")
    m.add_argument("--step", type=int, default=4)
    m.add_argument("--out")
    m.add_argument("--no-pad", action="store_true")
    m.add_argument("--pad-color")
    m.set_defaults(func=cmd_measure)

    for name, fn in (("sample", cmd_sample), ("emit", cmd_emit)):
        p = sub.add_parser(name, aliases=["amostra"] if name == "sample" else ["emitir"])
        p.add_argument("--source")
        p.add_argument("--samples")
        p.add_argument("--out")
        p.add_argument("--name", default="parede")
        p.add_argument("--res")
        p.add_argument("--n")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--no-ask", action="store_true")
        p.add_argument("--palettes", type=int, choices=(1, 2, 3, 4))
        p.add_argument("--palette", choices=("own", "file"))
        p.add_argument("--palette-file")
        p.add_argument("--mode", choices=MODES)
        p.add_argument("--transparency", choices=("yes", "no"),
                       help="deprecated: index 0 is always the backdrop")
        p.add_argument("--backdrop", choices=("border", "color"),
                       help="where the backdrop colour (index 0 of every palette) comes from")
        p.add_argument("--backdrop-color", help="RRGGBB when --backdrop color")
        p.add_argument("--dithering", choices=DITHERS)
        p.add_argument("--dither-strength", choices=list(DITHER_STRENGTHS))
        p.add_argument("--gain", default="auto")
        p.add_argument("--brightness", type=float, default=0.0)
        p.add_argument("--no-pad", action="store_true",
                       help="do not pad the image to multiples of 8")
        p.add_argument("--pad-color", help="RRGGBB of the padded border (default FFFFFF)")
        p.add_argument("--scroll", choices=("yes", "no"))
        p.add_argument("--resize", choices=("width", "height", "none"))
        p.add_argument("--bg-color", help="RRGGBB behind the alpha channel (default FFFFFF)")
        p.add_argument("--oversize", choices=("split", "resize", "none"),
                       help="what to do when a side is over the 512 px plane limit")
        p.add_argument("--max-tiles", type=int,
                       help="cap the unique tiles of every sample (0 = no limit)")
        p.add_argument("--view-height", type=int, choices=(224, 240), default=224,
                       help="height of one view for the per-view cap: 224 NTSC, 240 PAL")
        p.add_argument("--cap-scope", choices=("image", "view"),
                       help="does the tile cap apply to the whole image"
                            " or to each 320x240 view")
        p.set_defaults(func=fn, cmd=name)

    c = sub.add_parser("choose", aliases=["escolher"])
    c.add_argument("choice", help="1..4 or A..D")
    c.add_argument("--samples", required=True)
    c.set_defaults(func=cmd_choose)

    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())

    try:
        # an image dragged onto the exe arrives as the only argument
        if (getattr(sys, "frozen", False) and len(sys.argv) == 2
                and os.path.isfile(sys.argv[1])):
            a = ap.parse_args(["sample", "--source", sys.argv[1]])
            rc = a.func(a)
            _pausa()
            return rc

        if getattr(sys, "frozen", False) and len(sys.argv) == 1:
            # double click: no arguments to guess from, so ask for the image and
            # run the normal sample flow (the eight questions come right after)
            print("md_fundo - Mega Drive background tool")
            print("")
            print("type the path of the image (drag it into this window),")
            print("or press Enter to see the command line help.")
            print("")
            try:
                caminho = input("image: ").strip().strip('"')
            except EOFError:
                caminho = ""
            if not caminho:
                ap.print_help()
                _pausa()
                return 0
            if not os.path.exists(caminho):
                print(f"not found: {caminho}")
                _pausa()
                return 1
            a = ap.parse_args(["sample", "--source", caminho])
            rc = a.func(a)
            _pausa()
            return rc

        a = ap.parse_args()
        return a.func(a)
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        # argparse and the tool's own "raise SystemExit(...)" land here
        if exc.code not in (0, None) and _interativo():
            _pausa()
        raise
    except BaseException as exc:
        log = _grava_log(exc)
        print("\n--- the tool failed ---")
        print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if log:
            print(f"log written: {log}")
        else:
            print("could not write the log file (check the folder permissions)")
        if _interativo():
            _pausa()
        return 1


if __name__ == "__main__":
    sys.exit(main())
