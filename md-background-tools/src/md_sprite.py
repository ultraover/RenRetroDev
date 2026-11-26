# -*- coding: utf-8 -*-
"""Mega Drive sprite converter: the frames of a character -> one shared palette
-> an SGDK sprite sheet, with the dithering options of the Sweep.

    python md_sprite.py run --source <folder, or files separated by ;> [options]
    python md_sprite.py export --run <run folder> --variant <name or number>
                               [--name hero] [--res C:\\game\\res]
    python md_sprite.py selftest

What it does, in order:

1. Frames. Every image of the folder (or the files given) is a frame. Frames
   are grouped into animations by name: walk_01.png, walk_02.png -> "walk";
   jump1.png, jump2.png -> "jump". Each animation is one row of the sheet,
   in the order the names first appear (natural sort: 2 before 10).
2. Transparency. Index 0 of a sprite is transparent. It comes from the alpha
   channel (below --alpha-threshold), from a key colour (--key-color, give or
   take --key-tolerance dE) or from the border colour of the first frame;
   "auto" takes the alpha channel when the files have one, else the border.
   --edge-clean N (2 by default, for a key or border colour) repairs the
   band of N px along the edge: a pixel closer to the background than to the
   inside of the sprite becomes transparent, the others take the colour of
   the nearest inside pixel, so no halo of the old background is left.
3. Frame size. SGDK wants every frame of a sheet the same size, in multiples
   of 8. The canvases are lined up on the anchor (bottom centre by default:
   the feet stay on the same line in every frame) and, with --frames trim
   (the default), cut to the smallest box that holds the character in EVERY
   frame, then grown to multiples of 8 around the anchor. --frames keep only
   grows the canvas to multiples of 8.
4. Palette. One palette of 15 colours (+ the transparent index 0) for all the
   frames, made from the pixels of every frame together; or the colours of a
   palette file (--palette-file); --lock RRGGBB,... keeps colours that must
   stay exact; --reserve-ops leaves 14 and 15 unused (on PAL3 with
   shadow/highlight on, those two are operators, not colours).
5. Variants. Every dithering x strength asked is painted with that palette
   (the ordered dithers are tied to the frame, so they stay put while the
   character stands still). Each variant gets its numbers: colour error,
   flicker (how much the dots change between neighbouring frames where the
   picture did not), tiles and hardware sprites per frame.
6. Gallery. index.html shows every variant ANIMATED, next to the original,
   on a backdrop colour you pick, at the speed you pick.
7. Export. One variant: the indexed sheet PNG, the .pal, the .res line for
   rescomp (SPRITE name "name.png" w h NONE time) and report.md with C code.
"""
import argparse
import html
import io
import json
import os
import re
import shutil
import sys
import time
import traceback

import numpy as np
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
try:  # frozen .exe: md_fundo_v2 is bundled
    import md_fundo_v2 as md
except ImportError:
    for _d in (os.environ.get("MD_FUNDO_DIR"), _HERE):
        if _d and os.path.isfile(os.path.join(_d, "md_fundo_v2.py")):
            sys.path.insert(0, _d)
            break
    import md_fundo_v2 as md

VERSION_NOTE = "md_sprite 1.0"
IMG_EXT = (".png", ".bmp", ".gif", ".tga", ".webp", ".jpg", ".jpeg")
TRANSPARENCY = ("auto", "alpha", "key", "border")
FRAMES = ("trim", "keep")
ANCHORS = ("bottom", "center", "top-left")
COLOR0 = (255, 0, 255)          # what index 0 holds in the PNG palette
DEFAULT_DITHERS = "none,ordered2,ordered4"
SPRITE_LIMIT_PX = 32            # a hardware sprite is 1..4 x 1..4 tiles
# rescomp 4.02 (SGDK): a SPRITE frame is under 32 tiles a side (248 px at most),
# made of 16 hardware sprites at most, and an animation holds 255 frames at most
FRAME_MAX_PX = 248
HW_PER_FRAME = 16
ANIM_MAX_FRAMES = 255
FITS = ("fit", "crop")


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_sources(text):
    """Image files from a folder, one file, or several separated by ';'."""
    items = text if isinstance(text, (list, tuple)) else str(text or "").split(";")
    items = [p.strip().strip('"') for p in items if p and p.strip().strip('"')]
    files = []
    for p in items:
        if os.path.isdir(p):
            for n in sorted(os.listdir(p), key=natural_key):
                if n.lower().endswith(IMG_EXT) and not n.startswith("."):
                    files.append(os.path.join(p, n))
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise SystemExit(f"not found: {p}")
    if not files:
        raise SystemExit("no images: give a folder of frames or the frame files")
    seen, out = set(), []
    for f in files:
        k = os.path.normcase(os.path.abspath(f))
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def anim_name(path):
    """walk_01 -> walk ; jump2 -> jump ; idle -> idle.

    A name that is only a number (0001.png, 000123.png, a frame dump) has no
    animation name before the number: all such frames of a folder are one
    animation, named after the folder (frames when there is none)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"[\s_\-.]*\d+$", "", stem)
    if base:
        return base
    if re.fullmatch(r"[\s_\-.]*\d+", stem):
        folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
        return re.sub(r"[^0-9A-Za-z_]+", "_", folder).strip("_") or "frames"
    return stem


def single_frame_note(anims):
    """A warning when every animation has one frame: the names may not follow
    the name_number pattern, and nothing would animate."""
    if len(anims) > 1 and all(n == 1 for _, n in anims):
        return (f"note: {len(anims)} animations of ONE frame each - nothing animates. "
                "Name the frames of one animation alike, with a number at the end "
                "(walk_01.png, walk_02.png...).")
    return ""


def group_frames(files):
    """[(animation, [files in natural order])], animations in order of first
    appearance in the natural order of the names."""
    files = sorted(files, key=lambda f: natural_key(os.path.basename(f)))
    groups = {}
    for f in files:
        groups.setdefault(anim_name(f), []).append(f)
    return list(groups.items())


def load_rgba(path):
    with Image.open(path) as im:
        return np.asarray(im.convert("RGBA"), np.uint8).copy()


def has_any_alpha(frames):
    return any(int(f[..., 3].min()) < 255 for f in frames)


def scale_frame(rgba, scale, method):
    if scale == 1.0:
        return rgba
    h, w = rgba.shape[:2]
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    im = Image.fromarray(rgba, "RGBA").resize((nw, nh), md._PIL_RESIZE[method])
    return np.asarray(im, np.uint8).copy()


def opaque_masks(frames, how, key=None, alpha_threshold=128, key_tolerance=3.0,
                 edge_clean=0.0):
    """Per frame, True where the sprite has a pixel. Returns (masks, how, key)."""
    if how == "auto":
        how = "alpha" if has_any_alpha(frames) else "border"
    if how == "border":
        key = md.border_key(frames[0][..., :3])
    if how in ("key", "border"):
        if key is None:
            raise SystemExit("transparency key: give --key-color RRGGBB")
        key = np.asarray(key, np.uint8).reshape(3)
    masks = []
    for f in frames:
        if how == "alpha":
            m = f[..., 3] >= alpha_threshold
        else:
            m = ~md.key_mask(f[..., :3], key, key_tolerance) & (f[..., 3] >= alpha_threshold)
        if edge_clean > 0 and key is not None:
            m, rgb_fix = clean_edge(f[..., :3], m, key, int(round(edge_clean)))
            f[..., :3] = rgb_fix
        masks.append(m)
    return masks, how, (None if key is None else [int(v) for v in key])


def clean_edge(rgb, mask, key, px):
    """Remove the halo a soft edge leaves on a coloured background.

    In the band of px pixels along the edge of the sprite, a pixel closer to
    the key colour than to the nearest pixel inside the sprite (past the band)
    becomes transparent; the others take the colour of that inside pixel, so
    no tint of the old background is left. Returns (mask, rgb)."""
    if px <= 0 or not mask.any():
        return mask, rgb
    from scipy import ndimage
    dist = ndimage.distance_transform_edt(mask)
    band = mask & (dist <= px)
    inner = mask & (dist > px)
    if not inner.any() or not band.any():
        return mask, rgb
    iy, ix = ndimage.distance_transform_edt(~inner, return_distances=False,
                                            return_indices=True)
    near = rgb[iy, ix]
    lab = md.true_lab(rgb.reshape(-1, 3).astype(np.float64))
    lab_n = md.true_lab(near.reshape(-1, 3).astype(np.float64))
    kl = md.true_lab(np.asarray(key, np.float64).reshape(1, 3))
    dk = np.sqrt(md.dists(lab, kl)[:, 0]).reshape(mask.shape)
    di = np.sqrt(((lab - lab_n) ** 2).sum(1)).reshape(mask.shape)
    drop = band & (dk < di)
    out = rgb.copy()
    keep_band = band & ~drop
    out[keep_band] = near[keep_band]
    return mask & ~drop, out


def parse_size(text):
    """'auto' -> None; '32x48' (pixels) or '4x6t' (tiles) -> (w, h) in pixels,
    multiples of 8, at most FRAME_MAX_PX a side (rescomp's limit)."""
    s = str(text or "auto").strip().lower().replace(" ", "")
    if s in ("", "auto", "none"):
        return None
    tiles = s.endswith("t")
    m = re.fullmatch(r"(\d+)[x*](\d+)t?", s)
    if not m:
        raise SystemExit(f"size {text!r}: auto, WxH in pixels (32x48) or in tiles (4x6t)")
    w, h = int(m.group(1)), int(m.group(2))
    if tiles:
        w, h = w * 8, h * 8
    for v in (w, h):
        if v < 8 or v % 8 or v > FRAME_MAX_PX:
            raise SystemExit(f"size {w}x{h}: each side from 8 to {FRAME_MAX_PX} px, in "
                             "multiples of 8 (a SPRITE frame is under 32 tiles a side)")
    return w, h


def align_frames(frames, masks, frames_mode="trim", anchor="bottom", size=None,
                 fit="fit", method="nearest"):
    """Line the canvases up on the anchor and cut every frame to one size in
    multiples of 8. With size (w, h) the frames get exactly that size: "fit"
    scales the character (all frames alike, aspect kept) to fill it, "crop"
    keeps the pixels and cuts what does not fit. Returns (rgb, masks, info)."""
    H = max(f.shape[0] for f in frames)
    W = max(f.shape[1] for f in frames)
    M = 8                                   # room to grow to multiples of 8
    stack_rgb, stack_m = [], []
    for f, m in zip(frames, masks):
        h, w = f.shape[:2]
        if anchor == "bottom":
            ox, oy = (W - w) // 2, H - h
        elif anchor == "center":
            ox, oy = (W - w) // 2, (H - h) // 2
        else:
            ox, oy = 0, 0
        cr = np.zeros((H + 2 * M, W + 2 * M, 3), np.uint8)
        cm = np.zeros((H + 2 * M, W + 2 * M), bool)
        cr[M + oy:M + oy + h, M + ox:M + ox + w] = f[..., :3]
        cm[M + oy:M + oy + h, M + ox:M + ox + w] = m
        stack_rgb.append(cr)
        stack_m.append(cm)
    union = np.zeros_like(stack_m[0])
    for m in stack_m:
        union |= m
    if not union.any():
        raise SystemExit("every frame is fully transparent: check the transparency "
                         "options (alpha threshold, key colour)")
    if anchor == "bottom":
        ax, ay = M + W // 2, M + H
    elif anchor == "center":
        ax, ay = M + W // 2, M + H // 2
    else:
        ax, ay = M, M
    ys, xs = np.nonzero(union)
    ux0, ux1, uy0, uy1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    if size is not None:
        return _to_size(stack_rgb, stack_m, (ux0, ux1, uy0, uy1), (ax, ay), size, fit,
                        anchor, method, W, H, frames_mode)
    if frames_mode == "trim":
        x0, x1, y0, y1 = ux0, ux1, uy0, uy1
    else:
        x0, x1, y0, y1 = M, M + W, M, M + H
    fw = -(-(x1 - x0) // 8) * 8
    fh = -(-(y1 - y0) // 8) * 8
    ex, ey = fw - (x1 - x0), fh - (y1 - y0)
    if anchor == "top-left":
        cx0, cy0 = x0, y0
    elif anchor == "center":
        cx0, cy0 = x0 - ex // 2, y0 - ey // 2
    else:                                   # bottom centre: grow up, split sideways
        cx0, cy0 = x0 - ex // 2, y0 - ey
    # the box may reach past the margin on a canvas already in multiples of 8
    pad_l, pad_t = max(0, -cx0), max(0, -cy0)
    pad_r = max(0, cx0 + fw - stack_m[0].shape[1])
    pad_b = max(0, cy0 + fh - stack_m[0].shape[0])
    out_rgb, out_m = [], []
    for cr, cm in zip(stack_rgb, stack_m):
        if pad_l or pad_t or pad_r or pad_b:
            cr = np.pad(cr, ((pad_t, pad_b), (pad_l, pad_r), (0, 0)))
            cm = np.pad(cm, ((pad_t, pad_b), (pad_l, pad_r)))
        a, b = cy0 + pad_t, cx0 + pad_l
        out_rgb.append(np.ascontiguousarray(cr[a:a + fh, b:b + fw]))
        out_m.append(np.ascontiguousarray(cm[a:a + fh, b:b + fw]))
    info = {"frame_w": fw, "frame_h": fh, "canvas_w": W, "canvas_h": H,
            "anchor": anchor, "anchor_x": ax - cx0, "anchor_y": ay - cy0,
            "frames_mode": frames_mode, "size": "auto", "fit": None,
            "fit_scale": 1.0, "lost_px": 0,
            "content_w": ux1 - ux0, "content_h": uy1 - uy0}
    return out_rgb, out_m, info


def _place_box(box, size, anchor):
    """Top-left of a size box around the content box, by the anchor."""
    x0, x1, y0, y1 = box
    w, h = size
    cx = (x0 + x1) // 2
    if anchor == "top-left":
        return x0, y0
    if anchor == "center":
        return cx - w // 2, (y0 + y1) // 2 - h // 2
    return cx - w // 2, y1 - h                # bottom centre


def _to_size(stack_rgb, stack_m, box, anchor_pt, size, fit, anchor, method, W, H,
             frames_mode):
    if fit not in FITS:
        raise SystemExit(f"--fit: {', '.join(FITS)}")
    fw, fh = size
    x0, x1, y0, y1 = box
    cw, ch = x1 - x0, y1 - y0
    ax, ay = anchor_pt
    out_rgb, out_m = [], []
    lost = 0
    if fit == "fit":
        s = min(fw / cw, fh / ch)
        nw, nh = max(1, min(fw, int(round(cw * s)))), max(1, min(fh, int(round(ch * s))))
        # where the scaled character sits in the frame
        if anchor == "bottom":
            ox, oy = (fw - nw) // 2, fh - nh
        elif anchor == "center":
            ox, oy = (fw - nw) // 2, (fh - nh) // 2
        else:
            ox, oy = 0, 0
        for cr, cm in zip(stack_rgb, stack_m):
            rgba = np.dstack([cr[y0:y1, x0:x1], np.where(cm[y0:y1, x0:x1], 255, 0)
                              .astype(np.uint8)])
            im = Image.fromarray(rgba, "RGBA")
            if (nw, nh) != (cw, ch):
                im = im.resize((nw, nh), md._PIL_RESIZE[method])
            a = np.asarray(im, np.uint8)
            r = np.zeros((fh, fw, 3), np.uint8)
            m = np.zeros((fh, fw), bool)
            r[oy:oy + nh, ox:ox + nw] = a[..., :3]
            m[oy:oy + nh, ox:ox + nw] = a[..., 3] >= 128
            out_rgb.append(r)
            out_m.append(m)
        info_ax = ox + (ax - x0) * s
        info_ay = oy + (ay - y0) * s
        scale = s
    else:
        bx, by = _place_box(box, size, anchor)
        for cr, cm in zip(stack_rgb, stack_m):
            pad = max(0, -bx, -by, bx + fw - cm.shape[1], by + fh - cm.shape[0])
            if pad:
                cr = np.pad(cr, ((pad, pad), (pad, pad), (0, 0)))
                cm = np.pad(cm, ((pad, pad), (pad, pad)))
            a, b = by + pad, bx + pad
            m = cm[a:a + fh, b:b + fw]
            lost += int(cm.sum() - m.sum())
            out_rgb.append(np.ascontiguousarray(cr[a:a + fh, b:b + fw]))
            out_m.append(np.ascontiguousarray(m))
        info_ax, info_ay = ax - bx, ay - by
        scale = 1.0
    info = {"frame_w": fw, "frame_h": fh, "canvas_w": W, "canvas_h": H,
            "anchor": anchor, "anchor_x": int(round(info_ax)), "anchor_y": int(round(info_ay)),
            "frames_mode": frames_mode, "size": f"{fw}x{fh}", "fit": fit,
            "fit_scale": float(scale), "lost_px": lost, "content_w": cw, "content_h": ch}
    return out_rgb, out_m, info


# --------------------------------------------------------------------------
# hardware sprites
# --------------------------------------------------------------------------
def hw_split(used):
    """Cover the used tiles of one frame with hardware sprites (1..4 x 1..4
    tiles): a grid of 4x4-tile blocks at the offset that needs the fewest.
    An estimate: rescomp cuts with its own optimiser (opt_type BALANCED by
    default) and may use fewer or more. Returns the boxes (row, col, h, w)."""
    used = np.asarray(used, bool)
    if not used.any():
        return []
    best = None
    # a grid of 4x4-tile blocks, tried at every offset; each block is trimmed
    # to the used tiles inside it. The fewest sprites wins, then the fewest tiles.
    for oy in range(4):
        for ox in range(4):
            boxes = []
            for r0 in range(-oy, used.shape[0], 4):
                for c0 in range(-ox, used.shape[1], 4):
                    a, b = max(r0, 0), max(c0, 0)
                    blk = used[a:r0 + 4, b:c0 + 4]
                    if blk.size == 0 or not blk.any():
                        continue
                    rr, cc = np.nonzero(blk)
                    boxes.append((a + int(rr.min()), b + int(cc.min()),
                                  int(rr.max() - rr.min()) + 1, int(cc.max() - cc.min()) + 1))
            key = (len(boxes), sum(h * w for _, _, h, w in boxes))
            if best is None or key < best[0]:
                best = (key, boxes)
    return best[1]


def hw_summary(boxes, nh):
    """Numbers of one frame: sprites, sizes, worst line, VRAM."""
    from collections import Counter
    line_n = np.zeros(max(nh, 1), np.int32)
    line_px = np.zeros(max(nh, 1), np.int32)
    for r, c, bh, bw in boxes:
        line_n[r:r + bh] += 1
        line_px[r:r + bh] += bw * 8
    sizes = Counter(f"{bw * 8}x{bh * 8}" for _, _, bh, bw in boxes)
    tiles = int(sum(bh * bw for _, _, bh, bw in boxes))
    return {"sprites": len(boxes),
            "sizes": dict(sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))),
            "line_sprites": int(line_n.max()) if boxes else 0,
            "line_px": int(line_px.max()) if boxes else 0,
            "vram_tiles": tiles, "vram_bytes": tiles * 32}


def sizes_text(sizes):
    return " + ".join(f"{n}x {k}" for k, n in sizes.items()) or "none"


def frame_hw(mask):
    """Hardware sprites of a frame from its mask (index 0 is exactly the
    transparent pixels, so the dithering never changes which tiles are used)."""
    h, w = mask.shape
    used = mask.reshape(h // 8, 8, w // 8, 8).any(axis=(1, 3))
    return hw_summary(hw_split(used), h // 8)


def hw_report(masks):
    """Per-frame hardware numbers and the worst frame."""
    per = [frame_hw(m) for m in masks]
    worst = max(range(len(per)), key=lambda i: (per[i]["sprites"], per[i]["vram_tiles"]))
    return per, worst


def plan_output(source, frames_mode="trim", anchor="bottom", size=None, fit="fit",
                scale=1.0, method="nearest", transparency="auto", key=None,
                alpha_threshold=128, key_tolerance=3.0, edge_clean=2):
    """The output frame and its hardware sprites, without painting (for the
    window, before a conversion). Returns the one-line text."""
    files = list_sources(source)
    ordered = [f for _, fs in group_frames(files) for f in fs]
    raw = [scale_frame(load_rgba(f), scale, method) for f in ordered]
    masks, _, _ = opaque_masks(raw, transparency, key, alpha_threshold, key_tolerance,
                               edge_clean)
    _, m_al, info = align_frames(raw, masks, frames_mode, anchor, parse_size(size), fit,
                                 method)
    per, worst = hw_report(m_al)
    n_over = sum(1 for p in per if p["sprites"] > HW_PER_FRAME)
    txt = output_text(info, per, worst, n_over)
    if size and parse_size(size) and fit == "fit":
        txt += f" · character scaled x{info['fit_scale']:.2f}"
    if info["lost_px"]:
        txt += f" · crop cuts {info['lost_px']} pixels"
    return txt


def output_text(info, per, worst, n_over):
    w = per[worst]
    fw, fh = info["frame_w"], info["frame_h"]
    txt = (f"output frame {fw}x{fh} px ({fw // 8}x{fh // 8} tiles) · hardware sprites per "
           f"frame: up to {w['sprites']} (worst frame: {sizes_text(w['sizes'])}) · worst "
           f"line {w['line_sprites']} sprites / {w['line_px']} px · VRAM up to "
           f"{w['vram_tiles']} tiles ({w['vram_bytes']} bytes)")
    if n_over:
        txt += (f" · {n_over} frame(s) over {HW_PER_FRAME} hardware sprites: rescomp "
                "refuses them (smaller size, or split the sprite in 2)")
    return txt


def fill_holes(rgb, mask):
    """Transparent pixels take the colour of the nearest sprite pixel, so a
    dither does not see a hard step at the rim (they become index 0 after)."""
    if mask.all() or not mask.any():
        return rgb
    from scipy import ndimage
    idx = ndimage.distance_transform_edt(~mask, return_distances=False,
                                         return_indices=True)
    return rgb[idx[0], idx[1]]


# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
def parse_colors(text):
    """'FF0000, 00ff00' -> [[255,0,0],[0,255,0]] snapped to the MD grid."""
    if not text:
        return np.zeros((0, 3), np.uint8)
    out = []
    for t in re.split(r"[,;\s]+", str(text).strip()):
        if t:
            out.append(md.parse_fill(t))
    arr = np.asarray(out, np.float64).reshape(-1, 3)
    return md.snap_md(arr.reshape(1, -1, 3))[0].astype(np.uint8)


def make_palette(px_rgb, k=15, locked=None, palette_file=None, seed=0):
    """The 15 (or fewer) colours of the sprite, as RGB uint8, darkest first.

    px_rgb: every sprite pixel of every frame (N, 3). A palette file gives its
    colours 1..15 (colour 0 of a 16-colour block is the transparent slot)."""
    notes = []
    if palette_file:
        pals, pnotes = md.load_palette_set(palette_file, (0, 0, 0))
        notes += pnotes
        if len(pals) > 1:
            notes.append(f"the file holds {len(pals)} palettes: the sprite uses the first")
        body = np.unique(np.asarray(pals[0][1:], np.uint8), axis=0)
        pal = body[:k]
        if len(body) > k:
            notes.append(f"the file has {len(body)} colours: the first {k} (by "
                         "lightness) are used")
    else:
        locked = np.zeros((0, 3), np.uint8) if locked is None else np.asarray(locked, np.uint8)
        locked = np.unique(locked, axis=0)[:k]
        lab = md.rgb_to_lab(px_rgb.reshape(-1, 3).astype(np.float64))
        free = k - len(locked)
        parts = [locked]
        # few colours in the frames: their MD colours are the palette, nothing
        # is invented to fill the 15 slots
        snapped = np.unique(md.snap_md(px_rgb.reshape(1, -1, 3).astype(np.float64))[0],
                            axis=0)
        have = {tuple(int(v) for v in c) for c in locked}
        rest = np.asarray([c for c in snapped if tuple(int(v) for v in c) not in have],
                          np.uint8).reshape(-1, 3)
        if len(rest) <= free:
            pal = np.unique(np.vstack([locked, rest]).astype(np.uint8), axis=0)
            if len(locked):
                notes.append(f"{len(locked)} locked colour{'s' if len(locked) > 1 else ''}")
            order = np.argsort(md.true_lab(pal.astype(np.float64))[:, 0], kind="stable")
            return pal[order], notes + [f"the frames have {len(pal)} MD colours: all kept"]
        if free > 0:
            w = None
            if len(locked):
                d = np.sqrt(md.dists(lab, md.rgb_to_lab(locked.astype(np.float64))).min(1))
                w = np.clip(d / 10.0, 0.05, 1.0)    # what the locked colours cover counts less
            cent, _ = md.kmeans_md(lab, free, w, seed)
            parts.append(np.asarray(cent, np.uint8))
        pal = np.unique(np.vstack(parts).astype(np.uint8), axis=0)
        if len(pal) < k:
            pal = md.refine_palette(lab, pal, k, None, seed)
        if len(locked):
            notes.append(f"{len(locked)} locked colour{'s' if len(locked) > 1 else ''}: "
                         + ", ".join("%02X%02X%02X" % tuple(int(v) for v in c) for c in locked))
    pal = np.asarray(pal, np.uint8)
    order = np.argsort(md.true_lab(pal.astype(np.float64))[:, 0], kind="stable")
    return pal[order], notes


# --------------------------------------------------------------------------
# painting and numbers
# --------------------------------------------------------------------------
def paint_frame(rgb, mask, pal, dither, strength):
    """Indices 0..15 (0 = transparent) of one frame."""
    h, w = mask.shape
    src = fill_holes(rgb, mask)
    lab = md.rgb_to_lab(src.reshape(-1, 3).astype(np.float64))
    lab_pal = md.rgb_to_lab(np.asarray(pal, np.float64))
    idx = md._paint_lab(lab, lab_pal, (h, w), dither, strength).astype(np.uint8) + 1
    idx[~mask] = 0
    return idx


def frame_numbers(rgb, mask, idx, pal16):
    out = {}
    shown = pal16[idx]
    if mask.any():
        m = md.metrics(rgb[mask], shown[mask])
        out["de_mean"] = m["de_mean"]
        src = fill_holes(rgb, mask)
        view = np.where(mask[..., None], shown, src)
        out["de_perceived"] = md.percebido(src, view, mask)
    else:
        out["de_mean"] = out["de_perceived"] = 0.0
    tiles = md.to_tiles(idx)
    used = tiles.reshape(len(tiles), -1).any(1)
    uniq, _ = md.dedup(tiles[used]) if used.any() else (np.zeros((0, 8, 8)), None)
    out["tiles"] = int(len(uniq))
    nh, nw = idx.shape[0] // 8, idx.shape[1] // 8
    hs = hw_summary(hw_split(used.reshape(nh, nw)), nh)
    out["sprites"] = hs["sprites"]
    out["sprite_tiles"] = hs["vram_tiles"]
    out["line_sprites"] = hs["line_sprites"]
    out["line_px"] = hs["line_px"]
    return out


def flicker(frames_src, masks, idxs, anims):
    """Share of the pixels that did not change in the source between two
    neighbouring frames of an animation but changed colour in the result."""
    vals = []
    pos = 0
    for _, n in anims:
        rng = list(range(pos, pos + n))
        pairs = list(zip(rng, rng[1:])) + ([(rng[-1], rng[0])] if n > 2 else [])
        for i, j in pairs:
            same = masks[i] & masks[j] & np.all(frames_src[i] == frames_src[j], axis=2)
            if same.sum() >= 16:
                vals.append(float((idxs[i][same] != idxs[j][same]).mean()))
        pos += n
    return (float(np.mean(vals)) if vals else 0.0, float(np.max(vals)) if vals else 0.0)


def build_sheet(cells, anims, fw, fh):
    """Rows = animations, columns = frames; empty cells stay index 0."""
    cols = max(n for _, n in anims)
    sheet = np.zeros((fh * len(anims), fw * cols) + cells[0].shape[2:], cells[0].dtype)
    pos = 0
    for r, (_, n) in enumerate(anims):
        for c in range(n):
            sheet[r * fh:(r + 1) * fh, c * fw:(c + 1) * fw] = cells[pos + c]
        pos += n
    return sheet


def save_indexed(sheet_idx, pal16, path):
    im = Image.fromarray(sheet_idx.astype(np.uint8), "P")
    flat = np.zeros((256, 3), np.uint8)
    flat[:16] = pal16
    im.putpalette(flat.reshape(-1).tolist())
    im.save(path, transparency=0)


def save_preview(sheet_idx, pal16, path):
    rgba = np.zeros(sheet_idx.shape + (4,), np.uint8)
    rgba[..., :3] = pal16[sheet_idx]
    rgba[..., 3] = np.where(sheet_idx == 0, 0, 255)
    Image.fromarray(rgba, "RGBA").save(path)


def _csv(text, allowed, name):
    vals = [v.strip() for v in str(text).split(",") if v.strip()]
    if not vals:
        raise SystemExit(f"{name}: empty list")
    for v in vals:
        if v not in allowed:
            raise SystemExit(f"{name}: {v!r} is not one of {', '.join(allowed)}")
    return list(dict.fromkeys(vals))


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def cmd_run(a):
    t0 = time.time()
    md.set_color_weight(getattr(a, "color_weight", None))
    md.set_solid_pref(getattr(a, "solid", None))
    md.set_grey_mode(bool(getattr(a, "grey", False)))
    dithers = _csv(a.dithering, md.DITHERS, "--dithering")
    strengths = _csv(a.dither_strength, tuple(md.DITHER_STRENGTHS), "--dither-strength")
    if a.frames not in FRAMES:
        raise SystemExit(f"--frames: {', '.join(FRAMES)}")
    if a.anchor not in ANCHORS:
        raise SystemExit(f"--anchor: {', '.join(ANCHORS)}")
    if a.transparency not in TRANSPARENCY:
        raise SystemExit(f"--transparency: {', '.join(TRANSPARENCY)}")
    scale = float(a.scale or 1.0)
    if not 0.05 <= scale <= 8:
        raise SystemExit("--scale: from 0.05 to 8 (1 = no resize)")
    method = md.resize_method(a.scale_method)
    fps = float(a.fps or 10)
    if not 1 <= fps <= 60:
        raise SystemExit("--fps: from 1 to 60")

    files = list_sources(a.source)
    groups = group_frames(files)
    anims = [(name, len(fs)) for name, fs in groups]
    ordered = [f for _, fs in groups for f in fs]
    print(f"frames: {len(ordered)} in {len(anims)} animation"
          f"{'s' if len(anims) > 1 else ''}: "
          + " · ".join(f"{n} ({c})" for n, c in anims))
    if single_frame_note(anims):
        print(single_frame_note(anims))

    raw = [scale_frame(load_rgba(f), scale, method) for f in ordered]
    key = parse_colors(a.key_color)[0] if a.key_color else None
    masks, how, key_used = opaque_masks(raw, a.transparency, key, int(a.alpha_threshold),
                                        float(a.key_tolerance), float(a.edge_clean or 0))
    print(f"transparency: {how}"
          + (f" (key #{key_used[0]:02X}{key_used[1]:02X}{key_used[2]:02X})" if key_used else
             f" (alpha below {int(a.alpha_threshold)})"))
    size = parse_size(getattr(a, "size", None))
    fit = getattr(a, "fit", None) or "fit"
    rgb_al, m_al, info = align_frames(raw, masks, a.frames, a.anchor, size, fit, method)
    fw, fh = info["frame_w"], info["frame_h"]
    how_size = (f"size {info['size']} ({fit}" + (f", x{info['fit_scale']:.2f}" if fit == "fit"
                                                  else "") + ")"
                if size else a.frames)
    print(f"frame: {fw}x{fh} px ({fw // 8}x{fh // 8} tiles) · canvas {info['canvas_w']}x"
          f"{info['canvas_h']} · character {info['content_w']}x{info['content_h']} · "
          f"{how_size} · anchor {a.anchor} at ({info['anchor_x']},{info['anchor_y']}) "
          "of the frame")
    if info["lost_px"]:
        print(f"WARNING: crop cut {info['lost_px']} sprite pixels (all frames together): "
              "use fit, or a bigger size")
    if fw > FRAME_MAX_PX or fh > FRAME_MAX_PX:
        print(f"WARNING: a frame over {FRAME_MAX_PX} px a side: rescomp takes frames under "
              "32 tiles a side; choose a size (fit) or split the sprite")
    too_long = [n for n, c in anims if c > ANIM_MAX_FRAMES]
    if too_long:
        print(f"WARNING: over {ANIM_MAX_FRAMES} frames in {', '.join(too_long)}: rescomp "
              "takes 255 frames per animation at most")
    hw_per, hw_worst = hw_report(m_al)
    n_over = sum(1 for p in hw_per if p["sprites"] > HW_PER_FRAME)
    print(output_text(info, hw_per, hw_worst, n_over))

    px = np.concatenate([r[m] for r, m in zip(rgb_al, m_al)], 0)
    gain = md.parse_gain(a.gain, px.reshape(-1, 1, 3))
    bright = float(a.brightness or 0)
    rgb_al = [md.prepare(r, gain, bright) for r in rgb_al]
    px = md.prepare(px.reshape(-1, 1, 3), gain, bright).reshape(-1, 3)
    k = 13 if a.reserve_ops else 15
    locked = parse_colors(a.lock)
    pal, notes = make_palette(px, k, locked, a.palette_file, int(a.seed or 0))
    color0 = np.asarray(parse_colors(a.color0)[0] if a.color0 else COLOR0, np.uint8)
    pal16 = np.zeros((16, 3), np.uint8)
    pal16[0] = color0
    pal16[1:1 + len(pal)] = pal
    if len(pal) < 15:
        pal16[1 + len(pal):] = pal[-1]
    print(f"palette: {len(pal)} colours + transparent"
          + (" (14 and 15 left free)" if a.reserve_ops else "")
          + (f" · gain x{gain:.2f}" if gain != 1.0 else ""))
    for n_ in notes:
        print("  " + n_)

    base_name = md.c_name(a.name or anim_name(ordered[0]) or "sprite")
    raw_out = os.path.normpath(a.out or os.path.join(os.getcwd(), f"sprite_{base_name}"))
    out = raw_out if os.path.abspath(raw_out) == os.getcwd() else md.make_unique_dir(raw_out)
    os.makedirs(os.path.join(out, "variants"), exist_ok=True)
    setattr(a, "actual_out", out)

    src_sheet = build_sheet([np.dstack([r, np.where(m, 255, 0).astype(np.uint8)])
                             for r, m in zip(rgb_al, m_al)], anims, fw, fh)
    Image.fromarray(src_sheet, "RGBA").save(os.path.join(out, "original_sheet.png"))

    specs = []
    for d in dithers:
        for s in (["full"] if d == "none" else strengths):
            specs.append((d, s))
    variants = []
    for d, s in specs:
        t = time.time()
        idxs = [paint_frame(r, m, pal, d, s) for r, m in zip(rgb_al, m_al)]
        nums = [frame_numbers(r, m, i, pal16) for r, m, i in zip(rgb_al, m_al, idxs)]
        fl_mean, fl_max = flicker(rgb_al, m_al, idxs, anims)
        stem = d if d == "none" else f"{d}_{s}"
        sheet = build_sheet(idxs, anims, fw, fh)
        save_indexed(sheet, pal16, os.path.join(out, "variants", f"{stem}.png"))
        save_preview(sheet, pal16, os.path.join(out, "variants", f"{stem}_preview.png"))
        allu = md.dedup(md.to_tiles(sheet)[md.to_tiles(sheet).reshape(-1, 64).any(1)])[0]
        v = {"stem": stem, "dithering": d, "strength": s,
             "sheet": f"variants/{stem}.png", "preview": f"variants/{stem}_preview.png",
             "de_mean": float(np.mean([n["de_mean"] for n in nums])),
             "de_perceived": float(np.mean([n["de_perceived"] for n in nums])),
             "flicker": fl_mean, "flicker_max": fl_max,
             "tiles_max": max(n["tiles"] for n in nums),
             "tiles_total": int(len(allu)),
             "sprites_max": max(n["sprites"] for n in nums),
             "line_sprites_max": max(n["line_sprites"] for n in nums),
             "line_px_max": max(n["line_px"] for n in nums),
             "frames": nums}
        variants.append(v)
        print(f"  {stem:22s} dE {v['de_mean']:5.2f} · perceived {v['de_perceived']:5.2f} · "
              f"flicker {100 * fl_mean:4.1f}% · tiles/frame <= {v['tiles_max']} · "
              f"sprites/frame <= {v['sprites_max']} ({time.time() - t:.1f}s)")

    meta = {"tool": VERSION_NOTE, "name": base_name,
            "sources": [os.path.abspath(f) for f in ordered],
            "anims": [{"name": n, "frames": c} for n, c in anims],
            "frame": info, "transparency": how, "key": key_used,
            "palette": pal16.tolist(), "palette_colours": int(len(pal)),
            "hardware": {"per_frame": hw_per, "worst_frame": hw_worst,
                         "frames_over_limit": n_over, "limit": HW_PER_FRAME,
                         "text": output_text(info, hw_per, hw_worst, n_over)},
            "reserve_ops": bool(a.reserve_ops), "gain": gain, "brightness": bright,
            "color_weight": md.color_weight(), "solid_pref": md.solid_pref(),
            "grey_mode": md.grey_mode(), "fps": fps, "scale": scale,
            "scale_method": method, "palette_notes": notes,
            "args": {k_: v_ for k_, v_ in vars(a).items()
                     if isinstance(v_, (str, int, float, bool, type(None)))
                     and k_ not in ("func",)},
            "variants": variants, "seconds": round(time.time() - t0, 1)}
    with open(os.path.join(out, "sprite.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    write_gallery(out, meta)
    md.show_link("gallery", os.path.join(out, "index.html"))
    if getattr(a, "export_all", False):
        for v in variants:
            export_variant(out, v["stem"])
    return 0


# --------------------------------------------------------------------------
# gallery
# --------------------------------------------------------------------------
def write_gallery(out, meta):
    info = meta["frame"]
    anims = meta["anims"]
    rows = []
    cards = [("original", "original_sheet.png", None)]
    cards += [(v["stem"], v["preview"], v) for v in meta["variants"]]
    for title, img, v in cards:
        if v is None:
            stats = "the frames as they come in (aligned and cut)"
        else:
            stats = (f"dE {v['de_mean']:.2f} · perceived {v['de_perceived']:.2f}<br>"
                     f"flicker {100 * v['flicker']:.1f}% (worst pair "
                     f"{100 * v['flicker_max']:.1f}%)<br>"
                     f"tiles/frame &le; {v['tiles_max']} · all frames {v['tiles_total']}<br>"
                     f"sprites/frame &le; {v['sprites_max']} · worst line "
                     f"{v['line_sprites_max']} sprites / {v['line_px_max']} px")
        rows.append(
            f'<div class="card"><div class="name">{html.escape(title)}</div>'
            f'<canvas data-src="{html.escape(img)}"></canvas>'
            f'<div class="stats">{stats}</div>'
            f'<details><summary>sheet</summary><img class="sheet" '
            f'src="{html.escape(img)}"></details></div>')
    pal = "".join(f'<span class="sw" title="#{c[0]:02X}{c[1]:02X}{c[2]:02X}" '
                  f'style="background:rgb({c[0]},{c[1]},{c[2]})"></span>'
                  for c in meta["palette"][1:1 + meta["palette_colours"]])
    opts = "".join(f'<option value="{i}">{html.escape(x["name"])} ({x["frames"]})</option>'
                   for i, x in enumerate(anims))
    data = json.dumps({"fw": info["frame_w"], "fh": info["frame_h"],
                       "anims": [x["frames"] for x in anims], "fps": meta["fps"]})
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>sprite - {html.escape(meta['name'])}</title>
<style>
body {{ background:#17191e; color:#d8dbe2; font:14px Segoe UI, sans-serif; margin:16px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} .sub {{ color:#8a93a6; margin-bottom:10px; }}
.bar {{ display:flex; gap:16px; align-items:center; flex-wrap:wrap; margin:10px 0 14px; }}
.grid {{ display:flex; flex-wrap:wrap; gap:14px; }}
.card {{ background:#22262f; border:1px solid #343a46; border-radius:8px; padding:10px; }}
.name {{ font-weight:bold; color:#7fb4ff; margin-bottom:6px; font-family:Consolas,monospace; }}
canvas {{ image-rendering:pixelated; display:block; border-radius:4px; }}
.stats {{ color:#b8bfcc; font-size:12px; margin-top:6px; line-height:1.4; }}
.sheet {{ image-rendering:pixelated; max-width:520px; background:var(--bg); margin-top:4px; }}
.sw {{ display:inline-block; width:18px; height:18px; border:1px solid #000; margin-right:2px; }}
</style></head><body>
<h1>{html.escape(meta['name'])}</h1>
<div class="sub">{len(meta['sources'])} frames · {len(anims)} animation(s) ·
frame {info['frame_w']}x{info['frame_h']} px · anchor {html.escape(info['anchor'])}
({info['anchor_x']},{info['anchor_y']}) · transparency {html.escape(meta['transparency'])} ·
{meta['palette_colours']} colours + transparent</div>
<div class="sub">{html.escape((meta.get('hardware') or {}).get('text', ''))}</div>
<div>{pal}</div>
<div class="bar">
 <label>animation <select id="anim">{opts}</select></label>
 <label>speed <input id="fps" type="number" min="1" max="60" value="{meta['fps']:g}" style="width:56px"> fps</label>
 <label>zoom <input id="zoom" type="number" min="1" max="8" value="3" style="width:46px">x</label>
 <label>backdrop <input id="bg" type="color" value="#3a6ea5"></label>
 <label><input id="play" type="checkbox" checked> play</label>
</div>
<div class="grid">{''.join(rows)}</div>
<p class="sub">Export the one you like in the Sprite window (Export one variant), or:
<code>--run sprite-console export --run "this folder" --variant &lt;name&gt;</code></p>
<script>
const D = {data};
const cards = [...document.querySelectorAll("canvas")].map(c => {{
  const im = new Image(); im.src = c.dataset.src; return {{c, im}}; }});
let frame = 0;
function draw() {{
  const z = +document.getElementById("zoom").value || 3;
  const a = +document.getElementById("anim").value || 0;
  const bg = document.getElementById("bg").value;
  document.body.style.setProperty("--bg", bg);
  const n = D.anims[a] || 1, f = frame % n;
  for (const {{c, im}} of cards) {{
    c.width = D.fw * z; c.height = D.fh * z;
    const g = c.getContext("2d"); g.imageSmoothingEnabled = false;
    g.fillStyle = bg; g.fillRect(0, 0, c.width, c.height);
    if (im.complete) g.drawImage(im, f * D.fw, a * D.fh, D.fw, D.fh, 0, 0, c.width, c.height);
  }}
}}
function tick() {{
  const fps = Math.min(60, Math.max(1, +document.getElementById("fps").value || D.fps));
  if (document.getElementById("play").checked) {{ frame++; draw(); }}
  setTimeout(tick, 1000 / fps);
}}
for (const id of ["zoom", "anim", "bg"]) document.getElementById(id).addEventListener("input", () => {{ frame = 0; draw(); }});
for (const {{im}} of cards) im.addEventListener("load", draw);
window.addEventListener("load", draw);
setTimeout(tick, 1000 / D.fps);
</script></body></html>"""
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------
def load_run(folder):
    folder = os.path.dirname(folder) if folder.lower().endswith("sprite.json") else folder
    p = os.path.join(folder, "sprite.json")
    if not os.path.isfile(p):
        raise SystemExit(f"not a sprite run folder (no sprite.json): {folder}")
    with open(p, encoding="utf-8") as f:
        return folder, json.load(f)


def export_variant(folder, variant, name=None, res_dir=None, log=print):
    """Write one variant's files for SGDK. Returns the folder they went to."""
    folder, meta = load_run(folder)
    stems = [v["stem"] for v in meta["variants"]]
    if str(variant).isdigit() and 1 <= int(variant) <= len(stems):
        variant = stems[int(variant) - 1]
    found = [v for v in meta["variants"] if v["stem"] == variant]
    if not found:
        raise SystemExit(f"variant not found: {variant} (have: {', '.join(stems)})")
    v = found[0]
    name = md.c_name((name or "").strip() or meta["name"])
    res_dir = (res_dir or "").strip() or os.path.join(folder, "export", v["stem"], "res")
    os.makedirs(res_dir, exist_ok=True)
    shutil.copyfile(os.path.join(folder, v["sheet"]), os.path.join(res_dir, f"{name}.png"))
    pal16 = np.asarray(meta["palette"], np.uint8)
    with open(os.path.join(res_dir, f"{name}.pal"), "w", encoding="ascii", newline="") as f:
        f.write("JASC-PAL\r\n0100\r\n16\r\n")
        for c in pal16:
            f.write(f"{int(c[0])} {int(c[1])} {int(c[2])}\r\n")
    info = meta["frame"]
    tw, th = info["frame_w"] // 8, info["frame_h"] // 8
    timing = max(1, int(round(60.0 / float(meta.get("fps") or 10))))
    res_line = f'SPRITE {name} "{name}.png" {tw} {th} NONE {timing}'
    res_path = os.path.join(res_dir, f"{name}.res")
    with open(res_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(res_line + "\n")
    anims = meta["anims"]
    lines = [f"# {name}", "",
             f"variant: {v['stem']} · {len(meta['sources'])} frames · "
             f"{len(anims)} animation(s)",
             f"frame: {info['frame_w']}x{info['frame_h']} px ({tw}x{th} tiles) · "
             f"anchor {info['anchor']} at ({info['anchor_x']},{info['anchor_y']}) of "
             "the frame",
             *([f"(the anchor is the {info['anchor']} point of the canvases you drew; "
                "it lies outside the frame when the trim cut empty canvas around it)"]
               if not (0 <= info["anchor_x"] <= info["frame_w"]
                       and 0 <= info["anchor_y"] <= info["frame_h"]) else []),
             f"palette: {meta['palette_colours']} colours + index 0 (transparent)"
             + (" · 14 and 15 left free (PAL3 with shadow/highlight)"
                if meta.get("reserve_ops") else ""),
             f"dE {v['de_mean']:.2f} · perceived {v['de_perceived']:.2f} · flicker "
             f"{100 * v['flicker']:.1f}%",
             f"tiles per frame: up to {v['tiles_max']} (all frames together: "
             f"{v['tiles_total']})", ""]
    hw = meta.get("hardware")
    if hw:
        per = hw["per_frame"]
        lines += ["## hardware sprites (Mega Drive)", "",
                  "A hardware sprite is 1 to 4 tiles a side (8 to 32 px); a SPRITE frame "
                  f"may use {hw['limit']} at most (rescomp). Estimate: rescomp cuts the "
                  "frame with its own optimiser (opt_type BALANCED by default) and may "
                  "use fewer or more.", "",
                  "| frame | sprites | sizes | worst line | VRAM |", "|---|---|---|---|---|"]
        pos = 0
        for x in anims:
            for k in range(x["frames"]):
                p_ = per[pos]
                flag = " **over the limit**" if p_["sprites"] > hw["limit"] else ""
                lines.append(f"| {x['name']} {k + 1} | {p_['sprites']}{flag} | "
                             f"{sizes_text(p_['sizes'])} | {p_['line_sprites']} / "
                             f"{p_['line_px']} px | {p_['vram_tiles']} tiles "
                             f"({p_['vram_bytes']} B) |")
                pos += 1
        lines.append("")
    lines += ["## animations (rows of the sheet)", ""]
    for i, x in enumerate(anims):
        lines.append(f"- {i}: {x['name']} ({x['frames']} frames)")
    lines += ["", "## files", "",
              f"- `{name}.png` - the sprite sheet, indexed, 16 colours: one row per "
              "animation, one column per frame; index 0 is transparent (the PNG "
              "stores it as #%02X%02X%02X). Cells after the last frame of a shorter "
              "row are left empty." % tuple(int(c) for c in pal16[0]),
              f"- `{name}.pal` - the same 16 colours (JASC-PAL).",
              f"- `{name}.res` - for rescomp: `{res_line}`. The last number is the "
              f"time of each frame in 1/60 s ({timing} = {60.0 / timing:.0f} fps).",
              "", "## C code (SGDK)", "", "```c", "#include <genesis.h>",
              f'#include "{name}.h"   // rescomp makes it from {name}.res', "",
              "int main(bool hardReset)", "{",
              "    SPR_init();",
              f"    PAL_setPalette(PAL1, {name}.palette->data, DMA);",
              "",
              "    // x, y is the top-left corner of the frame; the anchor is "
              f"({info['anchor_x']}, {info['anchor_y']}) inside it",
              f"    Sprite* spr = SPR_addSprite(&{name}, 160 - {info['anchor_x']}, "
              f"180 - {info['anchor_y']},",
              "                                TILE_ATTR(PAL1, TRUE, FALSE, FALSE));",
              f"    SPR_setAnim(spr, 0);   // row 0: {anims[0]['name']}",
              "",
              "    while (TRUE)",
              "    {",
              "        SPR_update();",
              "        SYS_doVBlankProcess();",
              "    }",
              "    return 0;",
              "}", "```", ""]
    if meta.get("reserve_ops"):
        lines += ["With shadow/highlight on, load this palette in PAL3 only if you want "
                  "colours 14 and 15 to act as operators; the sprite does not use them.", ""]
    with open(os.path.join(res_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"  {v['stem']} -> {res_dir} ({name}.png, {name}.pal, {name}.res, report.md)")
    return res_dir


def cmd_export(a):
    folder, meta = load_run(a.run)
    if not a.variant:
        print(f"variants of {folder}:")
        for i, v in enumerate(meta["variants"], 1):
            print(f"  {i:3d}  {v['stem']}")
        print("\nexport one with --variant <name or number>")
        return 0
    for item in a.variant.split(","):
        export_variant(folder, item.strip(), a.name, a.res)
    return 0


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------
def selftest():
    import tempfile
    ok = 0
    tmp = tempfile.mkdtemp(prefix="md_sprite_test_")
    try:
        src = os.path.join(tmp, "frames")
        os.makedirs(src)
        # a ball that bounces in a 64x64 canvas, two frames standing still,
        # plus a jump frame drawn on a canvas of another size
        yy, xx = np.mgrid[0:64, 0:64]
        for i, dy in enumerate((0, 0, -6, -10), 1):
            im = np.zeros((64, 64, 4), np.uint8)
            m = (xx - 32) ** 2 + (yy - (44 + dy)) ** 2 < 12 ** 2
            im[m, 0] = (120 + xx[m] * 2).clip(0, 255)
            im[m, 1] = (60 + yy[m]).clip(0, 255)
            im[m, 2] = 90
            im[m, 3] = 255
            Image.fromarray(im, "RGBA").save(os.path.join(src, f"walk_{i:02d}.png"))
        im = np.zeros((70, 61, 4), np.uint8)
        im[50:70, 20:40] = (200, 40, 40, 255)
        Image.fromarray(im, "RGBA").save(os.path.join(src, "jump1.png"))
        groups = group_frames(list_sources(src))
        assert [(n, len(f)) for n, f in groups] == [("jump", 1), ("walk", 4)], groups
        # a frame dump named by numbers only: one animation, the folder's name
        dump = os.path.join(tmp, "frame_dump")
        os.makedirs(dump)
        for i in (101, 103, 105):
            Image.fromarray(np.zeros((8, 8, 4), np.uint8), "RGBA").save(
                os.path.join(dump, f"{i:06d}.png"))
        g2 = group_frames(list_sources(dump))
        assert [(n, len(f)) for n, f in g2] == [("frame_dump", 3)], g2
        assert single_frame_note([("a", 1), ("b", 1)]) and not single_frame_note([("a", 3)])
        ok += 1
        print(f"ok {ok}: frames grouped by name: {[(n, len(f)) for n, f in groups]}; "
              f"numbers only: {[(n, len(f)) for n, f in g2]}")

        out = os.path.join(tmp, "run")
        a = argparse.Namespace(source=src, out=out, name="ball", dithering="none,ordered2,floyd",
                               dither_strength="balanced", frames="trim", anchor="bottom",
                               transparency="auto", key_color=None, alpha_threshold=128,
                               key_tolerance=3.0, edge_clean=0.0, scale=1.0,
                               scale_method="nearest", gain="none", brightness=0.0,
                               palette_file=None, lock="C80000", reserve_ops=True,
                               color0=None, color_weight=1.0, solid=0.5, grey=False,
                               fps=10, seed=0, export_all=False)
        with open(os.devnull, "w") as dn:
            old = sys.stdout
            sys.stdout = dn
            try:
                cmd_run(a)
            finally:
                sys.stdout = old
        _, meta = load_run(a.actual_out)
        info = meta["frame"]
        assert info["frame_w"] % 8 == 0 and info["frame_h"] % 8 == 0, info
        assert info["frame_w"] <= 32 and info["frame_h"] <= 48, info
        ok += 1
        print(f"ok {ok}: trimmed to {info['frame_w']}x{info['frame_h']} (canvas "
              f"{info['canvas_w']}x{info['canvas_h']}), multiples of 8")
        sheet = np.asarray(Image.open(os.path.join(a.actual_out, "variants", "none.png")))
        fw, fh = info["frame_w"], info["frame_h"]
        # bottom anchor: the canvases line up on their base. The jump frame
        # (a 61x70 canvas) is drawn down to its base, the ball stops 8 px above
        # the base of its 64x64 canvas: so the ball stands 8 px above the jump
        # frame, in both standing frames alike
        rowb = [int(np.nonzero(sheet[fh:2 * fh, c * fw:(c + 1) * fw].any(1))[0].max())
                for c in (0, 1)]
        jump_b = int(np.nonzero(sheet[0:fh, 0:fw].any(1))[0].max())
        assert jump_b == fh - 1 and rowb[0] == rowb[1] == fh - 1 - 8, (rowb, jump_b)
        ok += 1
        print(f"ok {ok}: anchor at the bottom: the canvases share their base line "
              f"(row {fh - 1})")
        pal16 = np.asarray(meta["palette"], np.uint8)
        assert [200, 0, 0] in pal16[1:].tolist() or [206, 0, 0] in pal16[1:].tolist(), pal16
        for v in meta["variants"]:
            s = np.asarray(Image.open(os.path.join(a.actual_out, v["sheet"])))
            assert s.max() <= 13, (v["stem"], "index 14/15 used with reserve_ops")
        ok += 1
        print(f"ok {ok}: {meta['palette_colours']} colours, the locked red kept, 14 and 15 "
              "never used")
        fl = {v["stem"]: v["flicker"] for v in meta["variants"]}
        assert fl["none"] == 0.0 and fl["ordered2_balanced"] == 0.0, fl
        ok += 1
        print(f"ok {ok}: no flicker where the picture stands still (none, ordered2); "
              f"floyd {100 * fl['floyd_balanced']:.1f}%")
        res_dir = export_variant(a.actual_out, "ordered2_balanced", "ball",
                                 os.path.join(tmp, "res"), log=lambda *x: None)
        res = open(os.path.join(res_dir, "ball.res"), encoding="utf-8").read().strip()
        assert res == f'SPRITE ball "ball.png" {fw // 8} {fh // 8} NONE 6', res
        im = Image.open(os.path.join(res_dir, "ball.png"))
        assert im.mode == "P" and im.size == (fw * 4, fh * 2), (im.mode, im.size)
        assert os.path.isfile(os.path.join(res_dir, "ball.pal"))
        ok += 1
        print(f"ok {ok}: export: {res}; sheet {im.size[0]}x{im.size[1]} indexed")

        # output size: valid Mega Drive sizes only, fit scales, crop cuts
        assert parse_size("auto") is None and parse_size("4x6t") == (32, 48)
        assert parse_size("32x48") == (32, 48)
        for bad in ("30x48", "256x32", "0x8"):
            try:
                parse_size(bad)
                raise AssertionError(("accepted", bad))
            except SystemExit:
                pass
        frames = [load_rgba(f) for _, fs in group_frames(list_sources(src)) for f in fs]
        masks, _, _ = opaque_masks(frames, "alpha")
        r1, m1, i1 = align_frames(frames, masks, "trim", "bottom", (16, 16), "fit")
        assert all(m.shape == (16, 16) for m in m1) and i1["fit_scale"] < 1
        assert any(m[-1].any() for m in m1), "fit: the feet stay on the bottom row"
        r2, m2, i2 = align_frames(frames, masks, "trim", "bottom", (16, 16), "crop")
        assert i2["lost_px"] > 0 and i1["lost_px"] == 0
        # hardware sprites: a full 8x12-tile frame is 2x3 sprites of 32x32;
        # a full 31x31-tile frame needs 64 and is over the rescomp limit
        full = hw_summary(hw_split(np.ones((12, 8), bool)), 12)
        assert full["sprites"] == 6 and full["sizes"] == {"32x32": 6}, full
        assert hw_summary(hw_split(np.ones((3, 3), bool)), 3)["sizes"] == {"24x24": 1}
        big = hw_summary(hw_split(np.ones((31, 31), bool)), 31)
        assert big["sprites"] > HW_PER_FRAME
        ok += 1
        print(f"ok {ok}: output size 16x16: fit x{i1['fit_scale']:.2f}, crop cuts "
              f"{i2['lost_px']} px; 64x96 full = {sizes_text(full['sizes'])}; "
              f"248x248 full = {big['sprites']} sprites (over {HW_PER_FRAME})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok}/{ok} tests passed")
    return 0


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------
def add_run_args(p):
    p.add_argument("--source", required=True,
                   help="a folder of frames, or frame files separated by ;")
    p.add_argument("--out", help="run folder (default sprite_<name> in the current folder)")
    p.add_argument("--name", help="sprite name in the C code (default: the first animation)")
    p.add_argument("--dithering", default=DEFAULT_DITHERS,
                   help=f"comma list of {', '.join(md.DITHERS)} (default {DEFAULT_DITHERS})")
    p.add_argument("--dither-strength", default="balanced",
                   help="comma list of clean, balanced, full")
    p.add_argument("--frames", choices=FRAMES, default="trim",
                   help="trim (default): one size that holds the character in every "
                        "frame; keep: the canvas, grown to multiples of 8")
    p.add_argument("--size", default="auto",
                   help="output frame size: auto (default, from --frames), WxH in pixels "
                        "(32x48) or in tiles (4x6t); multiples of 8, at most "
                        f"{FRAME_MAX_PX} px a side")
    p.add_argument("--fit", choices=FITS, default="fit",
                   help="with --size: fit (scale the character, all frames alike, to "
                        "fill the size) or crop (keep the pixels, cut what is outside)")
    p.add_argument("--anchor", choices=ANCHORS, default="bottom",
                   help="where the canvases line up: bottom (centre of the base, "
                        "default), center or top-left")
    p.add_argument("--transparency", choices=TRANSPARENCY, default="auto",
                   help="auto (alpha when the files have it, else the border colour), "
                        "alpha, key (--key-color) or border")
    p.add_argument("--key-color", help="RRGGBB of the transparent background")
    p.add_argument("--alpha-threshold", type=int, default=128,
                   help="alpha below this is transparent (1-255, default 128)")
    p.add_argument("--key-tolerance", type=float, default=3.0,
                   help="dE around the key colour that is still background")
    p.add_argument("--edge-clean", type=int, default=2,
                   help="px of the edge repaired against a key/border background "
                        "halo (default 2, 0 = off; the alpha channel needs none)")
    p.add_argument("--scale", type=float, default=1.0, help="resize every frame (1 = no)")
    p.add_argument("--scale-method", choices=md.RESIZE_METHODS, default="nearest")
    p.add_argument("--gain", default="none", help="brightness gain: none (default), "
                   "auto or a number")
    p.add_argument("--brightness", type=float, default=0.0)
    p.add_argument("--palette-file", help=".pal or .png: use these colours")
    p.add_argument("--lock", help="RRGGBB,... colours that must stay exact")
    p.add_argument("--reserve-ops", action="store_true",
                   help="leave colours 14 and 15 free (PAL3 with shadow/highlight)")
    p.add_argument("--color0", help="RRGGBB stored in index 0 of the PNG (default FF00FF)")
    p.add_argument("--color-weight", type=float, default=1.0)
    p.add_argument("--solid", type=float, default=0.5,
                   help="dots vs solid colour, 0..1 (0.5 = normal)")
    p.add_argument("--grey", action="store_true", help="grey mode")
    p.add_argument("--fps", type=float, default=10,
                   help="animation speed for the gallery and the .res (default 10)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--export-all", action="store_true",
                   help="also export the files of every variant")


def main():
    ap = argparse.ArgumentParser(description="Mega Drive sprite converter")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="convert the frames: every dithering asked, one gallery")
    add_run_args(r)
    r.set_defaults(func=cmd_run)
    e = sub.add_parser("export", help="write the SGDK files of one variant")
    e.add_argument("--run", required=True, help="the run folder (or its sprite.json)")
    e.add_argument("--variant", help="name or number; blank = list them")
    e.add_argument("--name", help="name of the files and of the C symbol")
    e.add_argument("--res", help="folder for the files (default export/<variant>/res)")
    e.set_defaults(func=cmd_export)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    try:
        if len(sys.argv) == 1 and md._interativo():
            print("MD Background Tools - sprite converter (console)\n")
            print("Drag a FOLDER of frames (or a sprite run folder, to export) into "
                  "this window and press Enter.")
            path = input("> ").strip().strip('"')
            if os.path.isfile(os.path.join(path, "sprite.json")):
                rc = cmd_export(argparse.Namespace(run=path, variant=None, name=None, res=None))
                item = input("export which? number (Enter = none): ").strip()
                if item:
                    export_variant(path, item)
            else:
                rc = ap.parse_args(["run", "--source", path]).func(
                    ap.parse_args(["run", "--source", path]))
            md._pausa()
            return rc
        a = ap.parse_args()
        if not getattr(a, "func", None):
            ap.print_help()
            return 0
        return a.func(a)
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        if exc.code not in (0, None) and md._interativo():
            md._pausa()
        raise
    except BaseException as exc:
        log = md._grava_log(exc)
        print("\n--- the tool failed ---")
        print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if log:
            print(f"log written: {log}")
        if md._interativo():
            md._pausa()
        return 1


if __name__ == "__main__":
    sys.exit(main())
