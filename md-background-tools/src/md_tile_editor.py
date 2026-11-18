#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tile editor: move, copy, delete and mirror the 8x8 tiles of an export,
see where a tile is used, edit its pixels, change the palette a tile uses and
edit the palettes themselves (windows of their own).

    python md_tile_editor.py [export folder | name16.png | name.res]

Works on what the Background and Sweep tools export (the res/ folder):
name16.png is the image rescomp turns into tiles and map, and every pixel of
it carries the colour (bits 0-3), the palette (bits 4-5) and the priority
(bit 7) of its tile. So copying a tile copies its drawing with its palette
and priority, and the map follows when the project is built again. Saving
rewrites name16.png (the first save keeps name16.orig.png), name_map.json and
name_preview.png; the name_palN.pal files are rewritten only when a palette
colour was changed (the first time keeps name_palN.orig.pal). The .res does
not change.
"""
import argparse
import json
import os
import shutil
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from md_fundo_v2 import LEVELS, rgb_to_lab, shadow_palette  # noqa: E402

UNDO_MAX = 200
ZOOMS = (1, 2, 3, 4, 6, 8, 12, 16)

# the 512 Mega Drive colours, level r + 8 g + 64 b (levels 0..7 per channel)
MD_LEVEL_RGB = np.array([[LEVELS[r], LEVELS[g], LEVELS[b]]
                         for b in range(8) for g in range(8) for r in range(8)],
                        np.uint8)
MD_SHADOW_RGB = np.asarray(shadow_palette(MD_LEVEL_RGB), np.uint8)


def md_levels(rgb):
    """The 3-bit levels (r, g, b) of a colour, each channel snapped."""
    return tuple(int(np.abs(LEVELS - float(v)).argmin()) for v in rgb[:3])


def snap_level_rgb(rgb):
    """A colour snapped to the 512-colour grid, channel by channel."""
    return np.array([LEVELS[n] for n in md_levels(rgb)], np.uint8)


def read_pal(path):
    """The colours of a .pal (JASC-PAL or plain 'R G B' lines)."""
    cols = []
    with open(path, encoding="ascii", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                cols.append([int(p) for p in parts])
    return cols


# --------------------------------------------------------------------------
# the document: no Qt here, so it can be tested without a window
# --------------------------------------------------------------------------
def find_png(path):
    """The name16.png of an export folder, a .res, or the png itself."""
    path = os.path.abspath(path.strip().strip('"'))
    if os.path.isfile(path):
        if path.lower().endswith("16.png"):
            return path
        if path.lower().endswith(".res"):
            folder = os.path.dirname(path)
            name = os.path.splitext(os.path.basename(path))[0]
            cand = os.path.join(folder, name + "16.png")
            if os.path.isfile(cand):
                return cand
            path = folder
        else:
            path = os.path.dirname(path)
    for folder in (path, os.path.join(path, "res")):
        if os.path.isdir(folder):
            pngs = sorted(f for f in os.listdir(folder)
                          if f.lower().endswith("16.png") and ".orig." not in f.lower())
            if pngs:
                return os.path.join(folder, pngs[0])
    raise FileNotFoundError(f"no exported image (name16.png) in {path}")


def export_answers(folder):
    """The options the export was made with (samples.json next to res/ or in
    it), or {}: the editor starts with the same view height and margin."""
    for f in (os.path.join(folder, "samples.json"),
              os.path.join(os.path.dirname(folder), "samples.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                return json.load(fh).get("answers", {}) or {}
        except (OSError, ValueError):
            continue
    return {}


class TileDoc:
    """The indexed image of an export and the edits made to it."""

    def __init__(self, png_path):
        self.path = os.path.abspath(png_path)
        self.folder = os.path.dirname(self.path)
        base = os.path.basename(self.path)
        self.name = base[:-len("16.png")]
        im = Image.open(self.path)
        if im.mode != "P":
            raise ValueError(f"{base} is not an indexed PNG (mode {im.mode})")
        self.raw = np.array(im, np.uint8)
        h, w = self.raw.shape
        if h % 8 or w % 8:
            raise ValueError(f"{base}: sides must be multiples of 8 ({w}x{h})")
        flat = np.zeros(768, np.uint8)
        pal = im.getpalette() or []
        flat[:len(pal)] = pal[:768]
        self.flat = flat.reshape(256, 3)
        self.png_palette = list(flat)
        self.shadow_lut = np.asarray(shadow_palette(self.flat), np.uint8)
        self.nh, self.nw = h // 8, w // 8
        self.map_path = os.path.join(self.folder, f"{self.name}_map.json")
        self.map = {}
        if os.path.isfile(self.map_path):
            with open(self.map_path, encoding="utf-8") as f:
                self.map = json.load(f)
        prio_low = (self.raw & 0x80) == 0
        # the game turns shadow/highlight on when the export used shadow
        self.shadow_mode = bool(prio_low.any()) or "1" in str(self.map.get("shadow", ""))
        # the palettes the game loads: name_pal0.pal, name_pal1.pal, ...
        self.pal_files = []
        while len(self.pal_files) < 4:
            f = os.path.join(self.folder, f"{self.name}_pal{len(self.pal_files)}.pal")
            if not os.path.isfile(f):
                break
            self.pal_files.append(f)
        used = int(((self.raw >> 4) & 3).max()) + 1
        self.pal_count = max(len(self.pal_files), used)
        self.pal_saved = self.flat.copy()       # to know what the .pal files hold
        self.view_h = 28
        self.margin = 0          # 1: views one tile wider and taller (scroll)
        self.version = 0
        self.undo, self.redo = [], []
        self.dirty = False
        self.clipboard = None
        self.start_counts = self.counts()

    # ---------------- geometry
    def tile_block(self, x0, y0, x1, y1):
        """Pixel slices of the tile rectangle [x0, x1) x [y0, y1)."""
        return slice(y0 * 8, y1 * 8), slice(x0 * 8, x1 * 8)

    def tile_info(self, tx, ty):
        t = self.raw[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8]
        pal = int(np.bincount(((t >> 4) & 3).reshape(-1), minlength=4).argmax())
        high = bool((t & 0x80).any())
        empty = not (t & 0x0F).any()
        return pal, high, empty

    # ---------------- what the Mega Drive shows
    def render(self, shadow=None):
        shadow = self.shadow_mode if shadow is None else shadow
        rgb = self.flat[self.raw].copy()
        if shadow:
            low = (self.raw & 0x80) == 0
            rgb[low] = self.shadow_lut[self.raw[low]]
        return rgb

    def priority_tiles(self):
        """(nh, nw) True where the tile has high priority."""
        t = self.raw.reshape(self.nh, 8, self.nw, 8) & 0x80
        return t.any(axis=(1, 3))

    # ---------------- tile counts, as rescomp sees them
    def tile_ids(self):
        """Same id for tiles with the same drawing, mirrors included
        (palette and priority live in the map, not in the tile)."""
        if getattr(self, "_ids_version", None) == self.version:
            return self._ids
        self._ids_version, self._ids = self.version, self._tile_ids()
        return self._ids

    def _tile_ids(self):
        t = (self.raw & 0x0F).reshape(self.nh, 8, self.nw, 8).transpose(0, 2, 1, 3)
        t = t.reshape(-1, 8, 8)
        variants = np.stack([t, t[:, :, ::-1], t[:, ::-1, :], t[:, ::-1, ::-1]], 1)
        flat = variants.reshape(len(t), 4, 64)
        keys = {}
        ids = np.empty(len(t), np.int64)
        for i in range(len(t)):
            k = min(bytes(flat[i, j]) for j in range(4))
            ids[i] = keys.setdefault(k, len(keys))
        return ids.reshape(self.nh, self.nw), len(keys)

    def counts(self):
        key = (self.version, self.view_h, self.margin)
        if getattr(self, "_counts_key", None) == key:
            return self._counts
        self._counts_key, self._counts = key, self._count()
        return self._counts

    def _count(self):
        ids, total = self.tile_ids()
        vh = min(self.view_h + self.margin, self.nh)
        vw = min(40 + self.margin, self.nw)
        peak = 0
        for y in range(self.nh - vh + 1):
            for x in range(self.nw - vw + 1):
                peak = max(peak, len(np.unique(ids[y:y + vh, x:x + vw])))
        return total, peak

    # ---------------- edits
    def _state(self):
        return self.raw.copy(), self.flat.copy()

    def _restore(self, state):
        raw, flat = state
        self.raw = raw
        if not np.array_equal(flat, self.flat):
            self.flat = flat
            self.shadow_lut = np.asarray(shadow_palette(self.flat), np.uint8)
        self.dirty = True
        self.version += 1

    def _push(self):
        self.undo.append(self._state())
        if len(self.undo) > UNDO_MAX:
            self.undo.pop(0)
        self.redo.clear()
        self.dirty = True
        self.version += 1

    def undo_step(self):
        if not self.undo:
            return False
        self.redo.append(self._state())
        self._restore(self.undo.pop())
        return True

    def redo_step(self):
        if not self.redo:
            return False
        self.undo.append(self._state())
        self._restore(self.redo.pop())
        return True

    def copy(self, sel):
        x0, y0, x1, y1 = sel
        ys, xs = self.tile_block(x0, y0, x1, y1)
        self.clipboard = self.raw[ys, xs].copy()

    def paste(self, sel):
        """Paste the clipboard at the top-left of the selection; one copied
        tile fills every tile of a bigger selection. Returns the pasted area."""
        if self.clipboard is None:
            return None
        x0, y0, x1, y1 = sel
        ch, cw = self.clipboard.shape[0] // 8, self.clipboard.shape[1] // 8
        self._push()
        if ch == 1 and cw == 1 and (x1 - x0 > 1 or y1 - y0 > 1):
            ys, xs = self.tile_block(x0, y0, x1, y1)
            self.raw[ys, xs] = np.tile(self.clipboard, (y1 - y0, x1 - x0))
            return sel
        x1, y1 = min(self.nw, x0 + cw), min(self.nh, y0 + ch)
        ys, xs = self.tile_block(x0, y0, x1, y1)
        self.raw[ys, xs] = self.clipboard[:(y1 - y0) * 8, :(x1 - x0) * 8]
        return (x0, y0, x1, y1)

    def delete(self, sel):
        """Empty tiles: backdrop colour (index 0), priority kept."""
        ys, xs = self.tile_block(*sel)
        self._push()
        self.raw[ys, xs] &= 0x80

    def flip(self, sel, horizontal=True):
        """Mirror the selection (the Mega Drive mirrors tiles for free)."""
        ys, xs = self.tile_block(*sel)
        self._push()
        block = self.raw[ys, xs]
        self.raw[ys, xs] = block[:, ::-1] if horizontal else block[::-1, :]

    def toggle_priority(self, sel):
        """All high -> all low; otherwise all high. Returns the new state."""
        ys, xs = self.tile_block(*sel)
        block = self.raw[ys, xs]
        x0, y0, x1, y1 = sel
        tiles = block.reshape(y1 - y0, 8, x1 - x0, 8)
        all_high = bool((tiles & 0x80).any(axis=(1, 3)).all())
        self._push()
        if all_high:
            self.raw[ys, xs] = block & 0x7F
        else:
            self.raw[ys, xs] = block | 0x80
        return not all_high

    # ---------------- where a tile is used, and its pixels
    def occurrences(self, tx, ty):
        """Every place that shows the drawing of tile (tx, ty) - the same VRAM
        tile, whatever the palette: [(x, y, flip)], flip "" (as it is), "h",
        "v" or "hv" (how that place mirrors the drawing of (tx, ty))."""
        ids, _ = self.tile_ids()
        me = self.raw[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] & 0x0F
        forms = (("", me), ("h", me[:, ::-1]), ("v", me[::-1, :]), ("hv", me[::-1, ::-1]))
        out = []
        for y, x in zip(*np.nonzero(ids == ids[ty, tx])):
            t = self.raw[y * 8:y * 8 + 8, x * 8:x * 8 + 8] & 0x0F
            for flip, f in forms:
                if np.array_equal(t, f):
                    out.append((int(x), int(y), flip))
                    break
        return out

    def tile_rgb(self, tx, ty):
        """The 8x8 pixels of a tile as the Mega Drive shows them."""
        raw = self.raw[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8]
        rgb = self.flat[raw].copy()
        if self.shadow_mode:
            low = (raw & 0x80) == 0
            rgb[low] = self.shadow_lut[raw[low]]
        return rgb

    def palette_rgb(self, tx, ty):
        """The 16 colours the tile (tx, ty) can use, as it shows them."""
        pal, high, _ = self.tile_info(tx, ty)
        idx = np.arange(16) | (pal << 4) | (0x80 if high else 0)
        rgb = self.flat[idx].copy()
        if self.shadow_mode and not high:
            rgb = self.shadow_lut[idx]
        return rgb

    def begin_pixels(self, tx, ty, every_use=True):
        """Start a pixel stroke on tile (tx, ty) (one undo step). every_use:
        the drawing changes in every place that uses the tile, mirrored where
        the place is mirrored; otherwise only here (this place becomes a tile
        of its own). Returns how many places change."""
        self._stroke = self.occurrences(tx, ty) if every_use else [(tx, ty, "")]
        self._stroke_dirty = self.dirty
        self._stroke_changed = False
        self._push()
        return len(self._stroke)

    def end_pixels(self):
        """End the stroke; a stroke that changed nothing leaves no undo step."""
        if getattr(self, "_stroke", None) is None:
            return
        if not self._stroke_changed and self.undo:
            self.undo.pop()
            self.dirty = self._stroke_dirty
        self._stroke = None

    def paint_pixel(self, px, py, colour):
        """Colour index 0..15 at pixel (px, py) of the stroke's tile; the
        palette and priority of every pixel stay."""
        changed = False
        for x, y, flip in self._stroke:
            qx = 7 - px if "h" in flip else px
            qy = 7 - py if "v" in flip else py
            v = self.raw[y * 8 + qy, x * 8 + qx]
            new = (int(v) & 0xF0) | (int(colour) & 0x0F)
            if new != v:
                self.raw[y * 8 + qy, x * 8 + qx] = new
                changed = True
        if changed:
            self.version += 1
            self._stroke_changed = True
        return changed

    # ---------------- palettes
    def set_tile_palette(self, sel, pal, keep_look=False):
        """Make the tiles of the selection use palette pal (0..3). The colour
        numbers stay, so the tiles take the colours of the new palette; with
        keep_look every pixel takes instead the closest colour of the new
        palette (index 0, the backdrop, stays 0). Returns how many tiles
        changed."""
        pal = int(pal) & 3
        ys, xs = self.tile_block(*sel)
        block = self.raw[ys, xs]
        if keep_look:
            new = block.copy()
            cand = self.flat[16 * pal + 1:16 * pal + 16].astype(np.float64)
            cand_lab = rgb_to_lab(cand)
            keys = np.unique(block & 0x3F)
            for k in keys:
                c = int(k) & 0x0F
                if c == 0:
                    to = 0
                else:
                    lab = rgb_to_lab(self.flat[int(k)].astype(np.float64)[None])[0]
                    to = 1 + int(((cand_lab - lab) ** 2).sum(1).argmin())
                m = (block & 0x3F) == k
                new[m] = (block[m] & 0x80) | (pal << 4) | to
        else:
            new = (block & 0xCF) | (pal << 4)
        if np.array_equal(new, block):
            return 0
        x0, y0, x1, y1 = sel
        before = block.reshape(y1 - y0, 8, x1 - x0, 8)
        after = new.reshape(y1 - y0, 8, x1 - x0, 8)
        n = int((before != after).any(axis=(1, 3)).sum())
        self._push()
        self.raw[ys, xs] = new
        return n

    def palette_colour(self, pal, i):
        return self.flat[16 * pal + i].copy()

    def set_palette_colour(self, pal, i, rgb):
        """Change colour i of palette pal (snapped to the 512 colours). Index 0
        is the backdrop: it changes in every palette. Returns False when the
        colour was already that."""
        c = snap_level_rgb(rgb)
        slots = ([16 * p for p in range(4)] if i == 0 else [16 * pal + i])
        slots = slots + [128 + s for s in slots]
        if all(np.array_equal(self.flat[s], c) for s in slots):
            return False
        self._push()
        for s in slots:
            self.flat[s] = c
        self.shadow_lut = np.asarray(shadow_palette(self.flat), np.uint8)
        return True

    def palette_changed(self):
        """True when a palette colour differs from the .pal files."""
        return not np.array_equal(self.flat[:64], self.pal_saved[:64])

    def colour_stats(self):
        """One row per colour of the palettes the export has: palette, index
        (index 0 of every palette is one row: the backdrop), colour, pixels,
        pixels on shadow tiles (low priority) and tile places that use it."""
        if getattr(self, "_stats_version", None) == self.version:
            return self._stats
        key = (self.raw & 0x3F).astype(np.int64)
        low = (self.raw & 0x80) == 0
        px = np.bincount(key.ravel(), minlength=64)
        px_low = np.bincount(key[low], minlength=64) if low.any() else np.zeros(64, int)
        t = key.reshape(self.nh, 8, self.nw, 8).transpose(0, 2, 1, 3).reshape(-1, 64)
        pres = np.zeros((len(t), 64), bool)
        pres[np.arange(len(t))[:, None], t] = True
        tiles = pres.sum(0)
        back = (t & 0x0F) == 0
        rows = [{"pal": None, "i": 0, "rgb": self.flat[0].copy(),
                 "px": int(px[0::16].sum()), "px_shadow": int(px_low[0::16].sum()),
                 "tiles": int(back.any(1).sum())}]
        for p in range(self.pal_count):
            for i in range(1, 16):
                k = 16 * p + i
                rows.append({"pal": p, "i": i, "rgb": self.flat[k].copy(),
                             "px": int(px[k]), "px_shadow": int(px_low[k]),
                             "tiles": int(tiles[k])})
        self._stats_version, self._stats = self.version, rows
        return rows

    def colour_mask(self, pal, i):
        """(h, w) True on the pixels that show colour i of palette pal (index
        0: the backdrop, in any palette)."""
        if i == 0:
            return (self.raw & 0x0F) == 0
        return (self.raw & 0x3F) == (16 * pal + i)

    def tiles_with(self, mask):
        """(nh, nw) True where a tile has a pixel of the mask."""
        return mask.reshape(self.nh, 8, self.nw, 8).any(axis=(1, 3))

    def palette_tiles(self):
        """How many tile places use each palette."""
        return np.bincount(self.tile_palettes().reshape(-1), minlength=4)

    def tile_palettes(self):
        """(nh, nw) the palette of every tile (the one most of its pixels use)."""
        p = ((self.raw >> 4) & 3).reshape(self.nh, 8, self.nw, 8).transpose(0, 2, 1, 3)
        p = p.reshape(self.nh, self.nw, 64)
        votes = np.stack([(p == k).sum(2) for k in range(4)], 2)
        return votes.argmax(2)

    # ---------------- save
    def save(self):
        orig = os.path.join(self.folder, f"{self.name}16.orig.png")
        if not os.path.exists(orig):
            shutil.copyfile(self.path, orig)
        im = Image.fromarray(self.raw, "P")
        self.png_palette = [int(v) for v in self.flat.reshape(-1)]
        im.putpalette(self.png_palette)
        im.save(self.path)
        if self.palette_changed():
            self._save_pals()
        high = self.priority_tiles()
        pal = self.tile_palettes()
        mapa = dict(self.map)
        if "backdrop_rgb" in mapa or self.palette_changed():
            c0 = self.flat[0]
            lv = md_levels(c0)
            mapa["backdrop_rgb"] = "#%02X%02X%02X" % tuple(int(v) for v in c0)
            mapa["backdrop_cram"] = "0x%04X" % ((lv[0] << 1) | (lv[1] << 5) | (lv[2] << 9))
        self.pal_saved = self.flat.copy()
        mapa.update({
            "image": self.map.get("image", self.name),
            "tile_w": self.nw, "tile_h": self.nh,
            "pal": "".join(str(int(v)) for v in pal.reshape(-1)),
            "shadow": "".join("1" if (self.shadow_mode and not v) else "0"
                              for v in high.reshape(-1)),
            "priority": "".join("1" if v else "0" for v in high.reshape(-1)),
            "edited": "tile editor: positions, palettes and priorities as saved",
        })
        mapa.setdefault("note", "row-major: index = y * tile_w + x.")
        with open(self.map_path, "w", encoding="utf-8") as f:
            json.dump(mapa, f, ensure_ascii=False, indent=1)
        self.map = mapa
        Image.fromarray(self.render()).save(
            os.path.join(self.folder, f"{self.name}_preview.png"))
        self.dirty = False
        return orig

    def _save_pals(self):
        """Rewrite name_palN.pal with the palettes as they are now (the first
        time keeps name_palN.orig.pal); the number of colours of each file
        stays."""
        for p in range(self.pal_count):
            path = os.path.join(self.folder, f"{self.name}_pal{p}.pal")
            n = 16
            if os.path.isfile(path):
                n = len(read_pal(path)) or 16
                orig = os.path.join(self.folder, f"{self.name}_pal{p}.orig.pal")
                if not os.path.exists(orig):
                    shutil.copyfile(path, orig)
            with open(path, "w", encoding="ascii", newline="") as f:
                f.write("JASC-PAL\r\n0100\r\n%d\r\n" % n)
                for c in self.flat[16 * p:16 * p + n]:
                    f.write(f"{int(c[0])} {int(c[1])} {int(c[2])}\r\n")


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------
def build_window_classes():
    from PySide6.QtCore import QRect, Qt, Signal
    from PySide6.QtGui import (QAction, QColor, QIcon, QImage, QKeySequence, QPainter,
                               QPen, QPixmap, QShortcut)
    from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                                   QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
                                   QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox,
                                   QPushButton, QRadioButton, QScrollArea, QTableWidget,
                                   QTableWidgetItem, QVBoxLayout, QWidget)

    USE_SAME = QColor(0, 220, 255)       # another place with the same drawing
    USE_MIRROR = QColor(255, 90, 230)    # ... shown mirrored
    USE_COLOUR = QColor(255, 150, 0)     # a tile with the colour picked in the palettes

    def hexc(c):
        return "#%02X%02X%02X" % tuple(int(v) for v in c[:3])

    def ink_of(c):
        r, g, b = (int(v) for v in c[:3])
        return "#000" if 0.299 * r + 0.587 * g + 0.114 * b > 128 else "#fff"

    def palette_icon(rgb16):
        pm = QPixmap(16 * 10, 12)
        p = QPainter(pm)
        for i, c in enumerate(rgb16):
            p.fillRect(i * 10, 0, 10, 12, QColor(*(int(v) for v in c[:3])))
        p.end()
        return QIcon(pm)

    class Canvas(QWidget):
        hovered = Signal(int, int)
        selected = Signal()
        opened = Signal()

        def __init__(self, win):
            super().__init__()
            self.win = win
            self.zoom = 4
            self.image = None
            self.sel = None          # (x0, y0, x1, y1) in tiles, x1/y1 exclusive
            self.anchor = None
            self.show_prio = False
            self.uses = []           # places of the selected tile: (x, y, flip)
            self.show_uses = True
            self.colour_tiles = None     # (nh, nw) tiles with the picked colour
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.StrongFocus)

        def set_doc_image(self, rgb):
            h, w = rgb.shape[:2]
            buf = np.ascontiguousarray(rgb)
            self.image = QImage(buf.data, w, h, 3 * w, QImage.Format_RGB888).copy()
            self.setFixedSize(w * self.zoom, h * self.zoom)
            self.update()

        def set_zoom(self, z):
            self.zoom = z
            if self.image is not None:
                self.setFixedSize(self.image.width() * z, self.image.height() * z)
            self.update()

        def tile_at(self, pos):
            doc = self.win.doc
            tx = int(pos.x() // (8 * self.zoom))
            ty = int(pos.y() // (8 * self.zoom))
            return (min(max(tx, 0), doc.nw - 1), min(max(ty, 0), doc.nh - 1))

        def _set_sel(self, a, b):
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            self.sel = (x0, y0, x1 + 1, y1 + 1)
            self.update()
            self.selected.emit()

        def mousePressEvent(self, e):
            if self.win.doc is None or e.button() != Qt.LeftButton:
                return
            t = self.tile_at(e.position())
            if e.modifiers() & Qt.ShiftModifier and self.anchor is not None:
                self._set_sel(self.anchor, t)
            else:
                self.anchor = t
                self._set_sel(t, t)

        def mouseDoubleClickEvent(self, e):
            if self.win.doc is None or e.button() != Qt.LeftButton:
                return
            t = self.tile_at(e.position())
            self.anchor = t
            self._set_sel(t, t)
            self.opened.emit()

        def mouseMoveEvent(self, e):
            if self.win.doc is None:
                return
            t = self.tile_at(e.position())
            self.hovered.emit(*t)
            if e.buttons() & Qt.LeftButton and self.anchor is not None:
                self._set_sel(self.anchor, t)

        def wheelEvent(self, e):
            if e.modifiers() & Qt.ControlModifier:
                self.win.step_zoom(1 if e.angleDelta().y() > 0 else -1)
                e.accept()
            else:
                e.ignore()

        def paintEvent(self, e):
            if self.image is None:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            z = self.zoom
            r = e.rect()
            sx0, sy0 = r.left() // z, r.top() // z
            sx1 = min(self.image.width(), r.right() // z + 1)
            sy1 = min(self.image.height(), r.bottom() // z + 1)
            p.drawImage(QRect(sx0 * z, sy0 * z, (sx1 - sx0) * z, (sy1 - sy0) * z),
                        self.image, QRect(sx0, sy0, sx1 - sx0, sy1 - sy0))
            doc = self.win.doc
            tx0, ty0 = sx0 // 8, sy0 // 8
            tx1, ty1 = min(doc.nw, sx1 // 8 + 1), min(doc.nh, sy1 // 8 + 1)
            if self.show_prio:
                high = doc.priority_tiles()
                fill = QColor(255, 40, 40, 110)
                for ty in range(ty0, ty1):
                    for tx in range(tx0, tx1):
                        if not high[ty, tx]:
                            p.fillRect(tx * 8 * z, ty * 8 * z, 8 * z, 8 * z, fill)
            if z >= 3:
                p.setPen(QPen(QColor(120, 140, 255, 90), 1))
                for tx in range(tx0, tx1 + 1):
                    p.drawLine(tx * 8 * z, ty0 * 8 * z, tx * 8 * z, ty1 * 8 * z)
                for ty in range(ty0, ty1 + 1):
                    p.drawLine(tx0 * 8 * z, ty * 8 * z, tx1 * 8 * z, ty * 8 * z)
            if self.colour_tiles is not None:
                p.setPen(QPen(USE_COLOUR, 1))
                ct = self.colour_tiles
                for ty in range(ty0, ty1):
                    for tx in range(tx0, tx1):
                        if ct[ty, tx]:
                            p.drawRect(tx * 8 * z, ty * 8 * z, 8 * z - 1, 8 * z - 1)
            if self.show_uses and len(self.uses) > 1:
                w = 2 if z >= 2 else 1
                for x, y, flip in self.uses:
                    if not (tx0 <= x < tx1 and ty0 <= y < ty1):
                        continue
                    p.setPen(QPen(USE_MIRROR if flip else USE_SAME, w))
                    p.drawRect(x * 8 * z + w, y * 8 * z + w, 8 * z - 2 * w - 1,
                               8 * z - 2 * w - 1)
            if self.sel is not None:
                x0, y0, x1, y1 = self.sel
                p.setPen(QPen(QColor(255, 220, 0), 2))
                p.drawRect(x0 * 8 * z, y0 * 8 * z, (x1 - x0) * 8 * z - 1, (y1 - y0) * 8 * z - 1)
            p.end()

    class PixelGrid(QWidget):
        """The 8x8 pixels of one tile, big: left click paints, right picks."""
        CELL = 36

        def __init__(self, pw):
            super().__init__()
            self.pw = pw
            self.setFixedSize(8 * self.CELL + 1, 8 * self.CELL + 1)
            self.setMouseTracking(True)
            self.painting = False

        def pixel_at(self, pos):
            px, py = int(pos.x() // self.CELL), int(pos.y() // self.CELL)
            if 0 <= px < 8 and 0 <= py < 8:
                return px, py
            return None

        def mousePressEvent(self, e):
            q = self.pixel_at(e.position())
            if q is None or self.pw.tile is None:
                return
            if e.button() == Qt.RightButton:
                self.pw.pick(*q)
            elif e.button() == Qt.LeftButton:
                self.painting = True
                self.pw.begin()
                self.pw.paint(*q)

        def mouseMoveEvent(self, e):
            q = self.pixel_at(e.position())
            self.pw.hover(q)
            if self.painting and q is not None and e.buttons() & Qt.LeftButton:
                self.pw.paint(*q)

        def mouseReleaseEvent(self, e):
            if self.painting and e.button() == Qt.LeftButton:
                self.painting = False
                self.pw.end()

        def paintEvent(self, e):
            p = QPainter(self)
            c = self.CELL
            doc, tile = self.pw.win.doc, self.pw.tile
            if doc is None or tile is None:
                p.fillRect(self.rect(), QColor(40, 40, 40))
                p.setPen(QColor(200, 200, 200))
                p.drawText(self.rect(), Qt.AlignCenter, "select one tile")
                p.end()
                return
            rgb = doc.tile_rgb(*tile)
            for py in range(8):
                for px in range(8):
                    r, g, b = (int(v) for v in rgb[py, px])
                    p.fillRect(px * c, py * c, c, c, QColor(r, g, b))
            p.setPen(QPen(QColor(128, 128, 128, 140), 1))
            for i in range(9):
                p.drawLine(i * c, 0, i * c, 8 * c)
                p.drawLine(0, i * c, 8 * c, i * c)
            p.end()

    class PixelWindow(QWidget):
        """Separate window: edit the pixels of the tile selected in the
        editor. It follows the selection while it is open."""

        def __init__(self, win):
            super().__init__(win, Qt.Window)
            self.win = win
            self.tile = None
            self.colour = 1
            self.setWindowTitle("Tile pixels")
            lay = QVBoxLayout(self)
            self.lb_tile = QLabel("")
            lay.addWidget(self.lb_tile)
            self.grid = PixelGrid(self)
            lay.addWidget(self.grid, 0, Qt.AlignHCenter)
            lay.addWidget(QLabel("Colour (right click on a pixel picks its colour):"))
            sw = QGridLayout()
            sw.setSpacing(2)
            self.group = QButtonGroup(self)
            self.group.setExclusive(True)
            self.swatches = []
            for i in range(16):
                b = QPushButton(str(i))
                b.setCheckable(True)
                b.setFixedSize(34, 28)
                b.setToolTip("colour 0 = backdrop (transparent)" if i == 0
                             else f"colour {i}")
                self.group.addButton(b, i)
                sw.addWidget(b, i // 8, i % 8)
                self.swatches.append(b)
            self.group.idClicked.connect(self.set_colour)
            lay.addLayout(sw)
            self.rb_all = QRadioButton("")
            self.rb_one = QRadioButton("only this place (it becomes a tile of its own)")
            self.rb_all.setChecked(True)
            lay.addWidget(self.rb_all)
            lay.addWidget(self.rb_one)
            self.lb_info = QLabel("")
            self.lb_info.setWordWrap(True)
            lay.addWidget(self.lb_info)
            hint = QLabel("Left click or drag: paint · right click: pick · "
                          "Ctrl+Z / Ctrl+Y: undo / redo · Save in the editor window")
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#888")
            lay.addWidget(hint)
            for seq, slot in (("Ctrl+Z", win.undo), ("Ctrl+Y", win.redo),
                              ("Ctrl+Shift+Z", win.redo), ("Ctrl+S", win.save)):
                QShortcut(QKeySequence(seq), self, activated=slot)
            self.set_colour(1)

        def follow(self):
            """Take the tile selected in the editor (one tile) and redraw."""
            doc, sel = self.win.doc, self.win.canvas.sel
            if doc is None or sel is None or sel[2] - sel[0] != 1 or sel[3] - sel[1] != 1:
                self.tile = None
            else:
                self.tile = (sel[0], sel[1])
            self.refresh()

        def refresh(self):
            doc, tile = self.win.doc, self.tile
            on = doc is not None and tile is not None
            for b in self.swatches + [self.rb_all, self.rb_one]:
                b.setEnabled(on)
            if not on:
                self.lb_tile.setText("Select one tile in the editor window.")
                self.rb_all.setText("every place that uses this tile")
                self.lb_info.setText("")
                self.grid.update()
                return
            tx, ty = tile
            pal, high, _ = doc.tile_info(tx, ty)
            self.lb_tile.setText(f"tile {tx},{ty} · palette {pal} · priority "
                                 f"{'high' if high else 'low'}")
            for i, (b, c) in enumerate(zip(self.swatches, doc.palette_rgb(tx, ty))):
                r, g, bl = (int(v) for v in c)
                ink = "#000" if 0.299 * r + 0.587 * g + 0.114 * bl > 128 else "#fff"
                border = "3px solid #ffd800" if i == self.colour else "1px solid #555"
                b.setStyleSheet(f"QPushButton{{background:rgb({r},{g},{bl});color:{ink};"
                                f"border:{border}}}")
            uses = doc.occurrences(tx, ty)
            mirrored = sum(1 for u in uses if u[2])
            n = len(uses)
            self.rb_all.setText(f"every place that uses this tile ({n})"
                                + (f", {mirrored} of them mirrored" if mirrored else ""))
            self.lb_info.setText("The places are marked in the editor (blue: same "
                                 "drawing, pink: mirrored)." if n > 1 else
                                 "This tile is used only here.")
            self.grid.update()

        def set_colour(self, i):
            self.colour = int(i)
            self.swatches[self.colour].setChecked(True)
            self.refresh()

        def pick(self, px, py):
            tx, ty = self.tile
            self.set_colour(int(self.win.doc.raw[ty * 8 + py, tx * 8 + px]) & 0x0F)

        def hover(self, q):
            if q is None or self.tile is None:
                return
            tx, ty = self.tile
            v = int(self.win.doc.raw[ty * 8 + q[1], tx * 8 + q[0]]) & 0x0F
            self.grid.setToolTip(f"pixel {q[0]},{q[1]} · colour {v}")

        def begin(self):
            self.win.doc.begin_pixels(*self.tile, every_use=self.rb_all.isChecked())

        def paint(self, px, py):
            if self.win.doc.paint_pixel(px, py, self.colour):
                self.grid.update()
                self.win.refresh(keep_pixels=True)

        def end(self):
            self.win.doc.end_pixels()
            self.win.refresh()

    class ColourGrid(QWidget):
        """The 512 Mega Drive colours: 8 blocks (blue level), each 8x8 (red
        across, green down). Normal or as a shadow tile shows them."""
        CELL = 16
        GAP = 6
        picked = Signal(int)

        def __init__(self):
            super().__init__()
            self.shadow = False
            self.current = None          # level index of the colour being edited
            self.in_palette = set()      # level indices the palette already has
            c, g = self.CELL, self.GAP
            self.setFixedSize(4 * 8 * c + 3 * g + 1, 2 * 8 * c + g + 1)
            self.setMouseTracking(True)

        def cell_rect(self, k):
            r, gl, b = k % 8, (k // 8) % 8, k // 64
            c, g = self.CELL, self.GAP
            bx, by = (b % 4) * (8 * c + g), (b // 4) * (8 * c + g)
            return QRect(bx + r * c, by + gl * c, c, c)

        def index_at(self, pos):
            c, g = self.CELL, self.GAP
            x, y = int(pos.x()), int(pos.y())
            bx, rx = divmod(x, 8 * c + g)
            by, ry = divmod(y, 8 * c + g)
            if bx > 3 or by > 1 or rx >= 8 * c or ry >= 8 * c:
                return None
            return int(rx // c) + 8 * int(ry // c) + 64 * (bx + 4 * by)

        def mouseMoveEvent(self, e):
            k = self.index_at(e.position())
            if k is None:
                self.setToolTip("")
                return
            r, gl, b = k % 8, (k // 8) % 8, k // 64
            self.setToolTip(f"levels R{r} G{gl} B{b} · normal {hexc(MD_LEVEL_RGB[k])}"
                            f" · on a shadow tile {hexc(MD_SHADOW_RGB[k])}")

        def mousePressEvent(self, e):
            k = self.index_at(e.position())
            if k is not None and e.button() == Qt.LeftButton:
                self.picked.emit(k)

        def paintEvent(self, e):
            p = QPainter(self)
            cols = MD_SHADOW_RGB if self.shadow else MD_LEVEL_RGB
            for k in range(512):
                p.fillRect(self.cell_rect(k), QColor(*(int(v) for v in cols[k])))
            for k in self.in_palette:
                r = self.cell_rect(k)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ink_of(cols[k])))
                p.drawEllipse(r.center(), 2, 2)
            if self.current is not None:
                r = self.cell_rect(self.current)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 216, 0), 3))
                p.drawRect(r.adjusted(1, 1, -1, -1))
            p.end()

    class NumItem(QTableWidgetItem):
        """A table cell that sorts by its number, not by its text."""

        def __init__(self, text, value):
            super().__init__(text)
            self.setData(Qt.UserRole, value)
            self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def __lt__(self, other):
            return (self.data(Qt.UserRole) or 0) < (other.data(Qt.UserRole) or 0)

    class PaletteWindow(QWidget):
        """Separate window: the palettes of the export, how much and where
        each colour is used, and the 512-colour grid to replace one."""

        def __init__(self, win):
            super().__init__(win, Qt.Window)
            self.win = win
            self.sel = None              # (pal, i); i 0 is the backdrop
            self.setWindowTitle("Palettes")
            root = QHBoxLayout(self)
            left = QVBoxLayout()
            root.addLayout(left)

            view = QHBoxLayout()
            view.addWidget(QLabel("Show colours as:"))
            self.rb_normal = QRadioButton("normal")
            self.rb_shadow = QRadioButton("shadow")
            self.rb_shadow.setToolTip("How a low priority tile shows them when the game "
                                      "uses shadow/highlight (half brightness). Picking "
                                      "a colour in the grid still sets the normal colour "
                                      "behind it.")
            self.rb_normal.setChecked(True)
            self.rb_normal.toggled.connect(lambda _on: self.refresh())
            view.addWidget(self.rb_normal)
            view.addWidget(self.rb_shadow)
            view.addStretch(1)
            left.addLayout(view)

            self.sw_grid = QGridLayout()
            self.sw_grid.setSpacing(2)
            self.pal_labels, self.swatches = [], []
            for p in range(4):
                lb = QLabel(f"Palette {p}")
                self.sw_grid.addWidget(lb, p, 0)
                self.pal_labels.append(lb)
                row = []
                for i in range(16):
                    b = QPushButton(str(i))
                    b.setFixedSize(30, 26)
                    b.clicked.connect(lambda _c=False, p_=p, i_=i: self.select(p_, i_))
                    self.sw_grid.addWidget(b, p, 1 + i)
                    row.append(b)
                self.swatches.append(row)
            self.sw_grid.setColumnStretch(17, 1)
            left.addLayout(self.sw_grid)

            self.lb_info = QLabel("Click a colour to see how much and where it is used.")
            self.lb_info.setWordWrap(True)
            self.lb_info.setTextFormat(Qt.RichText)
            self.lb_info.setMinimumHeight(70)
            left.addWidget(self.lb_info)
            self.ck_where = QCheckBox("mark where it is used in the editor (the rest of "
                                      "the image is dimmed)")
            self.ck_where.setChecked(True)
            self.ck_where.toggled.connect(lambda _on: self.win.refresh(keep_pixels=True))
            left.addWidget(self.ck_where)

            box = QGroupBox("Replace the selected colour (the 512 Mega Drive colours)")
            bl = QVBoxLayout(box)
            self.grid = ColourGrid()
            self.grid.picked.connect(self.replace)
            bl.addWidget(self.grid, 0, Qt.AlignHCenter)
            hint = QLabel("Each block is one blue level; red grows to the right and green "
                          "down. Yellow frame: the colour now. Dot: colours this palette "
                          "already has. Click a colour to use it (Ctrl+Z undoes).")
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#888")
            bl.addWidget(hint)
            left.addWidget(box)
            left.addStretch(1)

            right = QVBoxLayout()
            root.addLayout(right, 1)
            self.lb_summary = QLabel("")
            self.lb_summary.setWordWrap(True)
            self.lb_summary.setTextFormat(Qt.RichText)
            right.addWidget(self.lb_summary)
            self.table = QTableWidget(0, 8)
            self.table.setHorizontalHeaderLabels(
                ["", "Palette", "Colour", "RGB", "Pixels", "% of image",
                 "On shadow tiles", "Tile places"])
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(22)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SingleSelection)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            self.table.setSortingEnabled(True)
            self.table.sortItems(4, Qt.DescendingOrder)
            self.table.cellClicked.connect(self.table_clicked)
            self.table.setMinimumWidth(520)
            right.addWidget(self.table, 1)
            tip = QLabel("Click a column title to sort (most or least used). Colour 0 is "
                         "the backdrop: every palette shows it, so it is one row.")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#888")
            right.addWidget(tip)

            for seq, slot in (("Ctrl+Z", win.undo), ("Ctrl+Y", win.redo),
                              ("Ctrl+Shift+Z", win.redo), ("Ctrl+S", win.save)):
                QShortcut(QKeySequence(seq), self, activated=slot)
            self.resize(1180, 640)

        # ---------------- helpers
        def shown(self, rgb):
            """A colour as the toggle shows it (normal or shadow)."""
            if self.rb_shadow.isChecked():
                return np.asarray(shadow_palette(np.asarray(rgb, np.uint8)[None]),
                                  np.uint8)[0]
            return np.asarray(rgb, np.uint8)

        def mask(self):
            """The pixels of the selected colour, for the editor, or None."""
            doc = self.win.doc
            if (doc is None or self.sel is None or not self.isVisible()
                    or not self.ck_where.isChecked()):
                return None
            return doc.colour_mask(*self.sel)

        def select(self, pal, i):
            self.sel = (pal, i)
            self.refresh()
            self.win.refresh(keep_pixels=True)

        def table_clicked(self, row, _col):
            it = self.table.item(row, 1)
            if it is not None:
                self.select(*it.data(Qt.UserRole + 1))

        def replace(self, k):
            doc = self.win.doc
            if doc is None or self.sel is None:
                return
            pal, i = self.sel
            if doc.set_palette_colour(pal, i, MD_LEVEL_RGB[k]):
                self.win.refresh(keep_pixels=True)
                what = "backdrop (colour 0 of every palette)" if i == 0 else \
                    f"palette {pal} colour {i}"
                self.win.lb_hover.setText(f"{what} is now {hexc(MD_LEVEL_RGB[k])}")

        # ---------------- drawing
        def refresh(self):
            doc = self.win.doc
            if doc is None:
                return
            stats = doc.colour_stats()
            total_px = doc.raw.size
            by_key = {(r["pal"] if r["pal"] is not None else 0, r["i"]): r for r in stats}
            ptiles = doc.palette_tiles()
            for p in range(4):
                on = p < doc.pal_count
                self.pal_labels[p].setVisible(on)
                self.pal_labels[p].setText(f"Palette {p} ({int(ptiles[p])} tiles)")
                for i, b in enumerate(self.swatches[p]):
                    b.setVisible(on)
                    if not on:
                        continue
                    c = self.shown(doc.palette_colour(p, i))
                    r = by_key[(0, 0)] if i == 0 else by_key[(p, i)]
                    sel = self.sel is not None and (self.sel == (p, i) or
                                                     (i == 0 and self.sel[1] == 0))
                    border = "3px solid #ffd800" if sel else (
                        "1px dashed #f44" if r["px"] == 0 else "1px solid #555")
                    b.setStyleSheet(f"QPushButton{{background:{hexc(c)};color:{ink_of(c)};"
                                    f"border:{border}}}")
                    b.setToolTip(f"palette {p} colour {i} · {hexc(doc.palette_colour(p, i))}"
                                 f" · {r['px']} px" + (" · backdrop" if i == 0 else ""))
            self.fill_table(stats, total_px)
            self.fill_summary(stats, total_px)
            self.fill_info(by_key, total_px)

        def fill_info(self, by_key, total_px):
            doc = self.win.doc
            if self.sel is None:
                self.grid.current = None
                self.grid.in_palette = set()
                self.grid.shadow = self.rb_shadow.isChecked()
                self.grid.update()
                return
            pal, i = self.sel
            r = by_key[(0, 0)] if i == 0 else by_key[(pal, i)]
            c = doc.palette_colour(pal, i)
            lv = md_levels(c)
            name = ("colour 0 = backdrop (transparent in every palette; the backdrop "
                    "colour shows through)" if i == 0 else f"palette {pal}, colour {i}")
            pct = 100.0 * r["px"] / total_px
            if r["px"]:
                use = (f"used by <b>{r['px']}</b> pixels ({pct:.2f}% of the image) in "
                       f"<b>{r['tiles']}</b> tile places")
                if r["px_shadow"]:
                    use += (f"; {r['px_shadow']} of the pixels "
                            f"({100.0 * r['px_shadow'] / r['px']:.0f}%) are on shadow tiles")
                rank = sorted((x for x in doc.colour_stats()), key=lambda x: -x["px"])
                pos = next(n for n, x in enumerate(rank, 1)
                           if x["pal"] == r["pal"] and x["i"] == r["i"])
                use += f" · <b>{pos}º</b> most used of {len(rank)}"
            else:
                use = "<b style='color:#e44'>not used anywhere in the image</b>"
            self.lb_info.setText(
                f"<b>{name}</b> · {hexc(c)} (levels R{lv[0]} G{lv[1]} B{lv[2]}) · on a "
                f"shadow tile {hexc(shadow_palette(np.asarray(c, np.uint8)[None])[0])}"
                f"<br>{use}")
            self.grid.shadow = self.rb_shadow.isChecked()
            self.grid.current = lv[0] + 8 * lv[1] + 64 * lv[2]
            have = set()
            for j in range(16):
                q = md_levels(doc.palette_colour(pal, j))
                have.add(q[0] + 8 * q[1] + 64 * q[2])
            self.grid.in_palette = have
            self.grid.update()

        def fill_summary(self, stats, total_px):
            used = [r for r in stats if r["px"]]
            unused = [r for r in stats if not r["px"]]

            def nm(r):
                return "backdrop (0)" if r["pal"] is None else f"pal {r['pal']} · {r['i']}"

            def chip(r):
                c = self.shown(r["rgb"])
                return (f"<span style='background:{hexc(c)};color:{ink_of(c)}'>"
                        f"&nbsp;{nm(r)}&nbsp;</span> {100.0 * r['px'] / total_px:.1f}%")

            top = sorted(used, key=lambda r: -r["px"])
            txt = (f"<b>{len(used)}</b> of {len(stats)} colours in use. "
                   f"<b>Most used:</b> " + ", ".join(chip(r) for r in top[:5]))
            if len(top) > 5:
                low = top[-5:][::-1]
                txt += ("<br><b>Least used:</b> " + ", ".join(
                    chip(r) + f" ({r['px']} px)" for r in low))
            if unused:
                txt += ("<br><b style='color:#e44'>Not used:</b> "
                        + ", ".join(nm(r) for r in unused)
                        + " (free to give another colour)")
            self.lb_summary.setText(txt)

        def fill_table(self, stats, total_px):
            t = self.table
            sort_col = t.horizontalHeader().sortIndicatorSection()
            order = t.horizontalHeader().sortIndicatorOrder()
            t.setSortingEnabled(False)
            t.setRowCount(len(stats))
            sel_row = None
            for n, r in enumerate(stats):
                p = r["pal"]
                c = self.shown(r["rgb"])
                sw = QTableWidgetItem("")
                sw.setBackground(QColor(*(int(v) for v in c)))
                t.setItem(n, 0, sw)
                key = (0 if p is None else p, r["i"])
                pi = NumItem("all" if p is None else str(p), -1 if p is None else p)
                pi.setData(Qt.UserRole + 1, key)
                t.setItem(n, 1, pi)
                t.setItem(n, 2, NumItem(f"{r['i']} (backdrop)" if p is None else str(r["i"]),
                                        r["i"]))
                t.setItem(n, 3, QTableWidgetItem(hexc(r["rgb"])))
                t.setItem(n, 4, NumItem(str(r["px"]), r["px"]))
                pct = 100.0 * r["px"] / total_px
                t.setItem(n, 5, NumItem(f"{pct:.2f}%", pct))
                t.setItem(n, 6, NumItem(str(r["px_shadow"]), r["px_shadow"]))
                t.setItem(n, 7, NumItem(str(r["tiles"]), r["tiles"]))
                if not r["px"]:
                    for col in range(1, 8):
                        t.item(n, col).setForeground(QColor(230, 70, 70))
                if self.sel is not None and (key == self.sel or
                                             (r["i"] == 0 and self.sel[1] == 0)):
                    sel_row = sw
            t.setSortingEnabled(True)
            t.sortItems(sort_col, order)
            t.clearSelection()
            if sel_row is not None:
                t.selectRow(sel_row.row())

        def hideEvent(self, e):
            super().hideEvent(e)
            self.win.refresh(keep_pixels=True)

    class EditorWindow(QMainWindow):
        def __init__(self, path=""):
            super().__init__()
            self.setWindowTitle("MD Background Tools · Tile editor")
            self.resize(1180, 820)
            self.setAcceptDrops(True)
            self.doc = None
            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)

            def button(text, slot, tip=""):
                b = QPushButton(text)
                b.clicked.connect(slot)
                if tip:
                    b.setToolTip(tip)
                return b

            row = QHBoxLayout()
            row.addWidget(button("Open…", self.open_dialog, "Ctrl+O · an export folder, "
                                 "its res/ folder, a .res or a name16.png"))
            self.bt_save = button("Save", self.save, "Ctrl+S")
            row.addWidget(self.bt_save)
            row.addSpacing(12)
            self.bt_undo = button("Undo", self.undo, "Ctrl+Z")
            self.bt_redo = button("Redo", self.redo, "Ctrl+Y")
            row.addWidget(self.bt_undo)
            row.addWidget(self.bt_redo)
            row.addSpacing(12)
            self.bt_copy = button("Copy", self.copy, "Ctrl+C · the selected tile(s)")
            self.bt_paste = button("Paste", self.paste, "Ctrl+V · at the selected tile, "
                                   "replacing it (one copied tile fills a bigger selection)")
            self.bt_del = button("Delete", self.delete, "Del · empty tile (backdrop colour)")
            self.bt_fh = button("Flip H", lambda: self.flip(True), "H · mirror left-right")
            self.bt_fv = button("Flip V", lambda: self.flip(False), "V · mirror top-bottom")
            self.bt_prio = button("Priority", self.priority,
                                  "P · turn the priority of the selected tile(s) on or off")
            self.bt_pix = button("Edit pixels…", self.open_pixels,
                                 "E or double click · edit the pixels of the selected "
                                 "tile in a window of its own")
            self.bt_tpal = QPushButton("Tile palette ▾")
            self.bt_tpal.setToolTip("Keys 0-3 · make the selected tile(s) use another of "
                                    "the palettes")
            self.menu_tpal = QMenu(self)
            self.menu_tpal.aboutToShow.connect(self.fill_tile_palette_menu)
            self.bt_tpal.setMenu(self.menu_tpal)
            self.act_keep = QAction("keep the look (closest colours of the new palette)",
                                    self)
            self.act_keep.setCheckable(True)
            self.act_keep.setToolTip("Off: the colour numbers stay and the tile takes the "
                                     "colours of the new palette. On: every pixel takes "
                                     "the closest colour of the new palette, so the tile "
                                     "looks about the same (its drawing may change).")
            self.bt_pals = button("Palettes…", self.open_palettes,
                                  "C · the palettes: how much and where each colour is "
                                  "used, and replacing a colour")
            for b in (self.bt_copy, self.bt_paste, self.bt_del, self.bt_fh, self.bt_fv,
                      self.bt_prio, self.bt_pix, self.bt_tpal, self.bt_pals):
                row.addWidget(b)
            self.pixels = None
            self.palwin = None
            row.addStretch(1)
            row.addWidget(QLabel("Zoom"))
            self.cb_zoom = QComboBox()
            for z in ZOOMS:
                self.cb_zoom.addItem(f"{z}x", z)
            self.cb_zoom.setCurrentIndex(ZOOMS.index(4))
            self.cb_zoom.currentIndexChanged.connect(
                lambda i: self.set_zoom(self.cb_zoom.itemData(i)))
            row.addWidget(self.cb_zoom)
            root.addLayout(row)

            row2 = QHBoxLayout()
            self.ck_prio = QCheckBox("show priority (red = low priority)")
            self.ck_prio.toggled.connect(self.toggle_prio_view)
            row2.addWidget(self.ck_prio)
            self.ck_sh = QCheckBox("game uses shadow/highlight (low priority tiles darkened)")
            self.ck_sh.setToolTip("On when the image was exported in shadow mode. Without "
                                  "shadow/highlight the priority does not change the "
                                  "colours: it only puts the tile in front of or behind "
                                  "low-priority sprites.")
            self.ck_sh.toggled.connect(self.toggle_shadow)
            row2.addWidget(self.ck_sh)
            row2.addWidget(QLabel("view"))
            self.cb_view = QComboBox()
            self.cb_view.addItems(["320x224 (NTSC)", "320x240 (PAL)"])
            self.cb_view.currentIndexChanged.connect(self.view_changed)
            row2.addWidget(self.cb_view)
            self.ck_margin = QCheckBox("scroll margin")
            self.ck_margin.setToolTip("Count the views one tile wider and taller, as a "
                                      "pixel-by-pixel scroll needs them in VRAM.")
            self.ck_margin.toggled.connect(lambda _on: self.view_changed(
                self.cb_view.currentIndex()))
            row2.addWidget(self.ck_margin)
            self.ck_uses = QCheckBox("mark where the selected tile is used")
            self.ck_uses.setToolTip("Blue: the same drawing. Pink: the same drawing "
                                    "mirrored. Any palette: it is the same tile in VRAM.")
            self.ck_uses.setChecked(True)
            self.ck_uses.toggled.connect(self.toggle_uses)
            row2.addWidget(self.ck_uses)
            row2.addStretch(1)
            root.addLayout(row2)

            self.canvas = Canvas(self)
            self.canvas.hovered.connect(self.show_hover)
            self.canvas.selected.connect(self.update_status)
            self.canvas.opened.connect(self.open_pixels)
            self.scroll = QScrollArea()
            self.scroll.setWidget(self.canvas)
            self.scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            self.scroll.setStyleSheet("QScrollArea{background:#202020}")
            root.addWidget(self.scroll, 1)

            self.lb_counts = QLabel("Open an export (Background or Sweep) to edit its tiles.")
            self.lb_hover = QLabel("")
            st = QHBoxLayout()
            st.addWidget(self.lb_counts, 1)
            st.addWidget(self.lb_hover)
            root.addLayout(st)

            for seq, slot in (("Ctrl+O", self.open_dialog), ("Ctrl+S", self.save),
                              ("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo),
                              ("Ctrl+Shift+Z", self.redo), ("Ctrl+C", self.copy),
                              ("Ctrl+V", self.paste), ("Delete", self.delete),
                              ("P", self.priority), ("H", lambda: self.flip(True)),
                              ("V", lambda: self.flip(False)), ("E", self.open_pixels),
                              ("C", self.open_palettes),
                              ("0", lambda: self.tile_palette(0)),
                              ("1", lambda: self.tile_palette(1)),
                              ("2", lambda: self.tile_palette(2)),
                              ("3", lambda: self.tile_palette(3)),
                              ("Ctrl++", lambda: self.step_zoom(1)),
                              ("Ctrl+=", lambda: self.step_zoom(1)),
                              ("Ctrl+-", lambda: self.step_zoom(-1))):
                QShortcut(QKeySequence(seq), self, activated=slot)
            self.update_buttons()
            if path:
                self.open_path(path)

        # ---------------- files
        def open_dialog(self):
            if not self.confirm_discard():
                return
            p = QFileDialog.getExistingDirectory(self, "Export folder (with res/ or name16.png)")
            if p:
                self.open_path(p)

        def open_path(self, path):
            try:
                doc = TileDoc(find_png(path))
            except Exception as exc:
                QMessageBox.warning(self, "Open", str(exc))
                return
            self.doc = doc
            ans = export_answers(doc.folder)
            if ans:
                for w_, v_ in ((self.cb_view, 1 if ans.get("view_height") == 240 else 0),
                               (self.ck_margin, bool(ans.get("scroll_margin")))):
                    w_.blockSignals(True)
                    (w_.setCurrentIndex if w_ is self.cb_view else w_.setChecked)(v_)
                    w_.blockSignals(False)
            self.doc.view_h = 30 if self.cb_view.currentIndex() == 1 else 28
            self.doc.margin = 1 if self.ck_margin.isChecked() else 0
            self.doc.start_counts = self.doc.counts()
            self.ck_sh.blockSignals(True)
            self.ck_sh.setChecked(doc.shadow_mode)
            self.ck_sh.blockSignals(False)
            self.canvas.sel = None
            self.canvas.anchor = None
            if self.palwin is not None:
                self.palwin.sel = None
            self.refresh()
            self.setWindowTitle(f"MD Background Tools · Tile editor · {doc.name}16.png")

        def save(self):
            if self.doc is None:
                return
            try:
                orig = self.doc.save()
            except Exception as exc:
                QMessageBox.warning(self, "Save", f"Could not save: {exc}")
                return
            self.update_status()
            self.lb_hover.setText(f"saved · original kept as {os.path.basename(orig)}")
            self.update_title()

        def confirm_discard(self):
            if self.doc is None or not self.doc.dirty:
                return True
            r = QMessageBox.question(
                self, "Unsaved changes", "Save the changes first?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if r == QMessageBox.Save:
                self.save()
                return not self.doc.dirty
            return r == QMessageBox.Discard

        def closeEvent(self, e):
            if self.confirm_discard():
                for w_ in (self.pixels, self.palwin):
                    if w_ is not None:
                        w_.close()
                e.accept()
            else:
                e.ignore()

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            urls = e.mimeData().urls()
            if urls and self.confirm_discard():
                self.open_path(urls[0].toLocalFile())

        # ---------------- edits
        def _sel(self):
            if self.doc is None or self.canvas.sel is None:
                return None
            return self.canvas.sel

        def copy(self):
            sel = self._sel()
            if sel:
                self.doc.copy(sel)
                x0, y0, x1, y1 = sel
                self.lb_hover.setText(f"copied {x1 - x0}x{y1 - y0} tile(s)")
                self.update_buttons()

        def paste(self):
            sel = self._sel()
            if sel and self.doc.clipboard is not None:
                area = self.doc.paste(sel)
                if area:
                    self.canvas.sel = area
                self.refresh()

        def delete(self):
            sel = self._sel()
            if sel:
                self.doc.delete(sel)
                self.refresh()

        def flip(self, horizontal):
            sel = self._sel()
            if sel:
                self.doc.flip(sel, horizontal)
                self.refresh()

        def priority(self):
            sel = self._sel()
            if sel:
                high = self.doc.toggle_priority(sel)
                self.refresh()
                note = "high" if high else "low"
                if self.doc.shadow_mode and high:
                    note += " - normal colours: lighter than the converter chose for a shadow tile"
                self.lb_hover.setText(f"priority {note}")

        def fill_tile_palette_menu(self):
            m = self.menu_tpal
            m.clear()
            doc = self.doc
            if doc is None:
                return
            sel = self._sel()
            cur = None
            if sel:
                x0, y0, x1, y1 = sel
                pals = set(doc.tile_palettes()[y0:y1, x0:x1].reshape(-1).tolist())
                cur = pals.pop() if len(pals) == 1 else None
            for p in range(doc.pal_count):
                rgb = doc.flat[16 * p:16 * p + 16]
                if doc.shadow_mode and sel and not doc.priority_tiles()[
                        sel[1]:sel[3], sel[0]:sel[2]].any():
                    rgb = doc.shadow_lut[16 * p:16 * p + 16]
                a = m.addAction(palette_icon(rgb), f"Palette {p}"
                                + ("  (now)" if p == cur else "") + f"\tkey {p}")
                a.setEnabled(bool(sel))
                a.triggered.connect(lambda _c=False, p_=p: self.tile_palette(p_))
            if doc.pal_count < 2:
                info = m.addAction("this export has only one palette")
                info.setEnabled(False)
            m.addSeparator()
            m.addAction(self.act_keep)

        def tile_palette(self, pal):
            sel = self._sel()
            if not sel:
                return
            if pal >= self.doc.pal_count:
                self.lb_hover.setText(f"this export has no palette {pal} "
                                      f"(palettes 0-{self.doc.pal_count - 1})")
                return
            t0 = self.doc.counts()[0]
            n = self.doc.set_tile_palette(sel, pal, keep_look=self.act_keep.isChecked())
            self.refresh()
            if not n:
                self.lb_hover.setText(f"the selection already uses palette {pal}")
                return
            msg = f"{n} tile{'s' if n != 1 else ''} now use{'s' if n == 1 else ''} palette {pal}"
            if self.act_keep.isChecked():
                t1 = self.doc.counts()[0]
                if t1 != t0:
                    msg += f" (closest colours: unique tiles {t0} → {t1})"
                else:
                    msg += " (closest colours)"
            self.lb_hover.setText(msg)

        def open_palettes(self):
            if self.doc is None:
                return
            if self.palwin is None:
                self.palwin = PaletteWindow(self)
            self.palwin.show()
            self.palwin.raise_()
            self.palwin.activateWindow()
            self.palwin.refresh()
            self.refresh(keep_pixels=True)

        def undo(self):
            if self.doc is not None and self.doc.undo_step():
                self.refresh()

        def redo(self):
            if self.doc is not None and self.doc.redo_step():
                self.refresh()

        # ---------------- view
        def set_zoom(self, z):
            old = self.canvas.zoom
            hs, vs = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
            cx = (hs.value() + self.scroll.viewport().width() / 2) / old
            cy = (vs.value() + self.scroll.viewport().height() / 2) / old
            self.canvas.set_zoom(z)
            hs.setValue(int(cx * z - self.scroll.viewport().width() / 2))
            vs.setValue(int(cy * z - self.scroll.viewport().height() / 2))

        def step_zoom(self, d):
            i = min(max(self.cb_zoom.currentIndex() + d, 0), len(ZOOMS) - 1)
            self.cb_zoom.setCurrentIndex(i)

        def open_pixels(self):
            if self.doc is None:
                return
            if self.pixels is None:
                self.pixels = PixelWindow(self)
            self.pixels.follow()
            self.pixels.show()
            self.pixels.raise_()
            self.pixels.activateWindow()

        def toggle_uses(self, on):
            self.canvas.show_uses = on
            self.canvas.update()

        def toggle_prio_view(self, on):
            self.canvas.show_prio = on
            self.canvas.update()

        def toggle_shadow(self, on):
            if self.doc is not None:
                self.doc.shadow_mode = on
                self.doc.dirty = True
                self.refresh()

        def view_changed(self, i):
            if self.doc is not None:
                self.doc.view_h = 30 if i == 1 else 28
                self.doc.margin = 1 if self.ck_margin.isChecked() else 0
                self.doc.start_counts = self.doc.counts()
                self.update_status()

        def refresh(self, keep_pixels=False):
            if self.doc is None:
                return
            rgb = self.doc.render()
            mask = self.palwin.mask() if self.palwin is not None else None
            self.canvas.colour_tiles = None
            if mask is not None:
                rest = ~mask
                rgb[rest] = (rgb[rest].astype(np.uint16) // 4 + 24).astype(np.uint8)
                self.canvas.colour_tiles = self.doc.tiles_with(mask)
            self.canvas.set_doc_image(rgb)
            self.update_status(follow=not keep_pixels)
            self.update_title()
            if self.palwin is not None and self.palwin.isVisible():
                self.palwin.refresh()
            if keep_pixels and self.pixels is not None and self.pixels.isVisible():
                self.pixels.refresh()

        def update_title(self):
            if self.doc is not None:
                star = " *" if self.doc.dirty else ""
                self.setWindowTitle(f"MD Background Tools · Tile editor · "
                                    f"{self.doc.name}16.png{star}")
            self.update_buttons()

        def update_buttons(self):
            has = self.doc is not None
            sel = has and self.canvas.sel is not None
            for b in (self.bt_copy, self.bt_del, self.bt_fh, self.bt_fv, self.bt_prio,
                      self.bt_tpal):
                b.setEnabled(bool(sel))
            self.bt_pals.setEnabled(has)
            s = self.canvas.sel
            self.bt_pix.setEnabled(bool(sel and s[2] - s[0] == 1 and s[3] - s[1] == 1))
            self.bt_paste.setEnabled(bool(sel and self.doc.clipboard is not None))
            self.bt_save.setEnabled(has)
            self.bt_undo.setEnabled(bool(has and self.doc.undo))
            self.bt_redo.setEnabled(bool(has and self.doc.redo))

        def update_status(self, follow=True):
            if self.doc is None:
                return
            total, peak = self.doc.counts()
            t0, p0 = self.doc.start_counts
            vh = self.doc.view_h * 8
            view = (f"{320 + 8}x{vh + 8} (scroll margin)" if self.doc.margin
                    else f"320x{vh}")
            txt = (f"{self.doc.nw}x{self.doc.nh} tiles · unique tiles {total} (opened: {t0})"
                   f" · worst {view} view {peak} (opened: {p0})")
            self.canvas.uses = []
            if self.canvas.sel is not None:
                x0, y0, x1, y1 = self.canvas.sel
                if x1 - x0 == 1 and y1 - y0 == 1:
                    uses = self.doc.occurrences(x0, y0)
                    self.canvas.uses = uses
                    n = len(uses)
                    mirrored = sum(1 for u in uses if u[2])
                    txt += (f" · tile {x0},{y0}: used {n} time{'s' if n != 1 else ''} "
                            "in the image")
                    if mirrored:
                        txt += f" ({mirrored} mirrored)"
                else:
                    txt += f" · selection {x1 - x0}x{y1 - y0} at {x0},{y0}"
            self.canvas.update()
            self.lb_counts.setText(txt)
            self.update_buttons()
            if follow and self.pixels is not None and self.pixels.isVisible():
                self.pixels.follow()

        def show_hover(self, tx, ty):
            if self.doc is None:
                return
            pal, high, empty = self.doc.tile_info(tx, ty)
            self.lb_hover.setText(f"tile {tx},{ty} · palette {pal} · priority "
                                  f"{'high' if high else 'low'}{' · empty' if empty else ''}")

    return EditorWindow


def main():
    ap = argparse.ArgumentParser(description="MD tile editor")
    ap.add_argument("path", nargs="?", default="")
    a = ap.parse_args()
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([sys.argv[0]])
    EditorWindow = build_window_classes()
    w = EditorWindow(a.path)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
