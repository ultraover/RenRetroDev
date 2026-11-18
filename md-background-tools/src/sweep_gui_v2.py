#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sweep GUI: palettes, modes, dithering, strengths and two tile budgets.

Run:
    python sweep_gui.py [image]

Everything runs in a worker thread; the log is shown in the window. The
checkboxes are the axes of the grid. If every checkbox is selected, the window
warns about the number of samples and the estimated time before starting.
"""
import argparse
import contextlib
import os
import sys
import traceback

from PIL import Image
from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QFileDialog, QGridLayout, QGroupBox,
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget, QSpinBox, QDoubleSpinBox,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import md_fundo_sweep_v2 as md  # noqa: E402

PALETTES = (1, 2, 3, 4)
MODES = ("normal", "shadow", "highlight", "both")
DITHERS = ("none", "ordered2", "ordered4", "ordered8", "floyd", "atkinson", "stucki",
           "halftone", "bluenoise", "riemersma")
# new dithers start unticked, so the usual sweep keeps its size
DITHERS_OFF = ("ordered2",)
DITHER_TIPS = {"ordered2": "Bayer 2x2 held at 50%: each colour mixes only with its "
                           "neighbour in lightness, as a fine checkerboard. N colours "
                           "give 2N-1 tones. Made for palettes that climb in lightness "
                           "(ladder palettes, grey mode)."}
STRENGTHS = ("clean", "balanced", "full")


class ResizeBox(QWidget):
    """v2.12: resize of the source. Width and height in px (0 = auto); with
    "keep aspect ratio" only one side is typed and the other follows it.
    value() is the core's text ('none', '400x', 'x300', '400x300')."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.src_w = self.src_h = 0
        self._busy = False
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        self.ck_on = QCheckBox("resize the image")
        g.addWidget(self.ck_on, 0, 0, 1, 2)
        self.sp_w = QSpinBox()
        self.sp_h = QSpinBox()
        for sp, tip in ((self.sp_w, "width in pixels"), (self.sp_h, "height in pixels")):
            sp.setRange(0, md.RESIZE_MAX)
            sp.setSingleStep(8)
            sp.setSpecialValueText("auto")
            sp.setSuffix(" px")
            sp.setToolTip(tip + " (auto = follows the aspect ratio)")
        g.addWidget(QLabel("width"), 1, 0)
        g.addWidget(self.sp_w, 1, 1)
        g.addWidget(QLabel("height"), 2, 0)
        g.addWidget(self.sp_h, 2, 1)
        self.ck_keep = QCheckBox("keep aspect ratio")
        self.ck_keep.setToolTip("Type only the width or only the height; "
                                "the other side follows the picture.")
        self.ck_keep.setChecked(True)
        g.addWidget(self.ck_keep, 3, 0, 1, 2)
        g.addWidget(QLabel("method"), 4, 0)
        self.cb_method = QComboBox()
        for text, key, tip in (
                ("nearest neighbor", "nearest",
                 "Copies the nearest pixel: sharp, adds no colour (pixel art)."),
                ("bilinear", "bilinear", "Averages 2x2 pixels: smooth."),
                ("bicubic", "bicubic", "Smooth, a little sharper than bilinear."),
                ("lanczos", "lanczos", "The sharpest of the smooth ones."),
                ("area (box)", "area", "Averages every pixel it covers: "
                                       "best to shrink a photo or a painting.")):
            self.cb_method.addItem(text, key)
            self.cb_method.setItemData(self.cb_method.count() - 1, tip, Qt.ToolTipRole)
        self.cb_method.setToolTip("nearest adds no colour; the others are smoother "
                                  "but add in-between colours the palette has to absorb")
        g.addWidget(self.cb_method, 4, 1)
        g.setColumnStretch(1, 1)
        self.lb_size = QLabel("")
        self.lb_size.setStyleSheet("color:#8a8")
        g.addWidget(self.lb_size, 5, 0, 1, 2)
        self.ck_on.toggled.connect(self._update)
        self.ck_keep.toggled.connect(self._keep_toggled)
        self.sp_w.valueChanged.connect(lambda v: self._side(self.sp_h, v))
        self.sp_h.valueChanged.connect(lambda v: self._side(self.sp_w, v))
        self.cb_method.currentIndexChanged.connect(self._update)
        self._update()

    def _side(self, other, v):
        # keep aspect ratio: a typed side makes the other one "auto"
        if not self._busy and v and self.ck_keep.isChecked() and other.value():
            self._busy = True
            other.setValue(0)
            self._busy = False
        self._update()

    def _keep_toggled(self, on):
        if on and self.sp_w.value() and self.sp_h.value():
            self._busy = True
            self.sp_h.setValue(0)
            self._busy = False
        self._update()

    def set_size(self, w, h, on=True):
        self._busy = True
        self.ck_on.setChecked(on)
        if w and h:
            self.ck_keep.setChecked(False)
        self.sp_w.setValue(w or 0)
        self.sp_h.setValue(h or 0)
        self._busy = False
        self._update()

    def set_source_size(self, w, h):
        # only the label changes: no "changed" signal (the sweep window would
        # measure the picture again and again)
        self.src_w, self.src_h = w or 0, h or 0
        busy, self._busy = self._busy, True
        self._update()
        self._busy = busy

    MIN_SIDE = 8

    def _sides(self):
        # a side under 8 px is not a size yet (the first digit of "320"):
        # it counts as auto until the number is complete
        w, h = self.sp_w.value(), self.sp_h.value()
        return (w if w >= self.MIN_SIDE else 0), (h if h >= self.MIN_SIDE else 0)

    def value(self):
        w, h = self._sides()
        if not self.ck_on.isChecked() or (not w and not h):
            return "none"
        return f"{w or ''}x{h or ''}"

    def method(self):
        return self.cb_method.currentData() or "nearest"

    def set_method(self, m):
        i = self.cb_method.findData(m)
        if i >= 0:
            self.cb_method.setCurrentIndex(i)

    def _update(self, *_):
        on = self.ck_on.isChecked()
        for wdg in (self.sp_w, self.sp_h, self.ck_keep, self.cb_method):
            wdg.setEnabled(on)
        v = self.value()
        short = any(0 < s.value() < self.MIN_SIDE for s in (self.sp_w, self.sp_h))
        try:
            if not on:
                self.lb_size.setText("the image keeps its size")
            elif short:
                self.lb_size.setText(f"each side needs at least {self.MIN_SIDE} px")
            elif v == "none":
                self.lb_size.setText("type a width or a height")
            elif self.src_w and self.src_h:
                nw, nh = md.resize_size(self.src_w, self.src_h, v)
                self.lb_size.setText(f"{self.src_w}x{self.src_h} -> {nw}x{nh} px")
            else:
                self.lb_size.setText("")
        except BaseException as exc:      # never close the window over a label
            self.lb_size.setText(f"invalid size: {exc}")
        if not self._busy:
            self.changed.emit()


class _Stream(QObject):
    text = Signal(str)

    def write(self, s):
        if s:
            self.text.emit(str(s))
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class _Worker(QThread):
    log = Signal(str)
    done = Signal(bool, str)

    def __init__(self, args, job=None):
        super().__init__()
        self.args = args
        # job: a callable returning the text handed to done (default: the sweep)
        self.job = job

    def run(self):
        stream = _Stream()
        stream.text.connect(self.log)
        try:
            with contextlib.redirect_stdout(stream), \
                    contextlib.redirect_stderr(stream):
                if self.job is not None:
                    out = self.job()
                else:
                    md.cmd_sweep(self.args)
                    out = getattr(self.args, "actual_out", "")
            self.done.emit(True, out)
        except BaseException as exc:
            self.log.emit("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
            self.done.emit(False, str(exc))


class SweepWindow(QWidget):
    def __init__(self, source=""):
        super().__init__()
        self.setWindowTitle("MD Background Tools · Sweep")
        self.resize(1100, 960)
        self.setAcceptDrops(True)
        self.source = ""
        self.width = 0
        self.height = 0
        self.actual_out = ""
        self.worker = None

        root = QVBoxLayout(self)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source image:"))
        self.ed_source = QLineEdit()
        self.ed_source.setPlaceholderText("drop/select an image")
        self.ed_source.setAcceptDrops(False)
        self.ed_source.textChanged.connect(self.source_changed)
        src_row.addWidget(self.ed_source, 1)
        bt_src = QPushButton("Browse…")
        bt_src.clicked.connect(self.browse_source)
        src_row.addWidget(bt_src)
        root.addLayout(src_row)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output folder:"))
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("blank = sweep_<image> in the current folder")
        self.ed_out.setAcceptDrops(False)
        out_row.addWidget(self.ed_out, 1)
        bt_out = QPushButton("Browse…")
        bt_out.clicked.connect(self.browse_out)
        out_row.addWidget(bt_out)
        root.addLayout(out_row)

        # v2.12: optional resize of the source before the sweep
        rsg = QGroupBox("Resize the source first (optional)")
        rl = QHBoxLayout(rsg)
        self.rs = ResizeBox()
        rl.addWidget(self.rs)
        top = QHBoxLayout()   # resize | palette file and brightness, side by side
        top.addWidget(rsg)
        root.addLayout(top)
        self._rs_timer = QTimer(self)
        self._rs_timer.setSingleShot(True)
        self._rs_timer.setInterval(500)
        self._rs_timer.timeout.connect(self.source_changed)
        self.rs.changed.connect(self._rs_timer.start)

        # v2.13: fixed palettes from a file, and the brightness gain
        cg = QGroupBox("Palette file, brightness, colour weight, solid vs dots and grey "
                       "mode (optional)")
        cl = QVBoxLayout(cg)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Palette file:"))
        self.ed_palfile = QLineEdit()
        self.ed_palfile.setPlaceholderText(
            "blank = the tool makes the palettes · .pal or .png; several files with ;")
        self.ed_palfile.setAcceptDrops(False)
        self.ed_palfile.setToolTip(
            "Use these colours instead of making new palettes.\n"
            "Every 16 colours of the file make one palette (up to 4, 64 colours).\n"
            "In a block of 16, colour 0 is the backdrop slot: the sweep's backdrop\n"
            "goes there. A file with 15 colours or fewer has only colours.\n"
            "Several files (one palette each): separate them with ;")
        self.ed_palfile.textChanged.connect(self.palfile_changed)
        pr.addWidget(self.ed_palfile, 1)
        bt_pal = QPushButton("Browse…")
        bt_pal.clicked.connect(self.browse_palfile)
        pr.addWidget(bt_pal)
        bt_palx = QPushButton("Clear")
        bt_palx.clicked.connect(lambda: self.ed_palfile.setText(""))
        pr.addWidget(bt_palx)
        cl.addLayout(pr)
        self.lb_pal = QLabel("")
        self.lb_pal.setWordWrap(True)
        cl.addWidget(self.lb_pal)
        gr = QHBoxLayout()
        gr.addWidget(QLabel("Brightness gain:"))
        self.ck_gain = QCheckBox("auto")
        self.ck_gain.setChecked(True)
        self.ck_gain.setToolTip("auto: brightens the picture until its brightest "
                                "pixels reach about 230 (never darkens).\n"
                                "Untick to type the gain; 1.00 = no change.")
        gr.addWidget(self.ck_gain)
        self.sp_gain = QDoubleSpinBox()
        self.sp_gain.setRange(0.2, 4.0)
        self.sp_gain.setSingleStep(0.05)
        self.sp_gain.setDecimals(2)
        self.sp_gain.setPrefix("x")
        self.sp_gain.setValue(1.0)
        self.sp_gain.setEnabled(False)
        self.ck_gain.toggled.connect(lambda s: self.sp_gain.setEnabled(not s))
        gr.addWidget(self.sp_gain)
        self.lb_autogain = QLabel("")
        gr.addWidget(self.lb_autogain)
        gr.addStretch(1)
        cl.addLayout(gr)
        gr = QHBoxLayout()
        gr.addWidget(QLabel("Brightness offset:"))
        self.sp_bright = QDoubleSpinBox()
        self.sp_bright.setRange(-100, 100)
        self.sp_bright.setSingleStep(1)
        self.sp_bright.setDecimals(0)
        self.sp_bright.setToolTip("Added to every R, G and B value after the gain "
                                  "(0 = no change).")
        gr.addWidget(self.sp_bright)
        gr.addStretch(1)
        cl.addLayout(gr)
        gr = QHBoxLayout()
        gr.addWidget(QLabel("Colour weight vs lightness:"))
        self.sp_cweight = QSpinBox()
        self.sp_cweight.setRange(0, 100)
        self.sp_cweight.setSingleStep(5)
        self.sp_cweight.setValue(100)
        self.sp_cweight.setSuffix(" %")
        self.sp_cweight.setToolTip(
            "How much a difference of colour counts against a difference of\n"
            "lightness, in every colour comparison of the conversion.\n"
            "100% = normal, 0% = lightness only. Lower (40% is a good start)\n"
            "lets slightly tinted colours of the right lightness in: smoother\n"
            "tones, fewer dots, a little tint that the neighbours cancel.")
        gr.addWidget(self.sp_cweight)
        gr.addWidget(QLabel("(100% = normal)"))
        gr.addSpacing(16)
        gr.addWidget(QLabel("Solid vs dots:"))
        self.sp_solid = QSpinBox()
        self.sp_solid.setRange(0, 100)
        self.sp_solid.setSingleStep(5)
        self.sp_solid.setValue(50)
        self.sp_solid.setSuffix(" %")
        self.sp_solid.setToolTip(
            "Dots against solid colour, for every dither (ordered and diffusion).\n"
            "50% = normal (the strength preset as it is). Higher: a dither must win\n"
            "by more before it draws dots, and the diffusions spread less error\n"
            "(smoother, flatter). Lower: dithers more readily (more tones, more\n"
            "grain). The palette of each tile follows the same preference.")
        gr.addWidget(self.sp_solid)
        gr.addWidget(QLabel("(50% = normal)"))
        gr.addSpacing(16)
        self.ck_grey = QCheckBox("Grey mode (monochrome filter)")
        self.ck_grey.setToolTip(
            "Every colour counts only as the grey a monochrome filter shows:\n"
            "the MD levels as BlastEm outputs them, weighted by Rec.601\n"
            "(0.299 R + 0.587 G + 0.114 B). Any colour of the right grey will\n"
            "do, so the result looks odd in colour: view it through the filter.\n"
            "Pairs well with a palette file that climbs in grey.")
        self.ck_grey.toggled.connect(lambda s: self.sp_cweight.setEnabled(not s))
        gr.addWidget(self.ck_grey)
        gr.addStretch(1)
        cl.addLayout(gr)
        cl.addStretch(1)
        top.addWidget(cg, 1)
        self.fixed_n = 0

        # the four axes in one row (dithering in 3 columns) so the window
        # fits a 1080p screen
        axes = QGridLayout()
        self.cb_palettes = self._group("Palettes", [str(p) for p in PALETTES],
                                       axes, 0, 0)
        self.cb_modes = self._group("Modes", MODES, axes, 0, 1)
        self.cb_dithers = self._group("Dithering", DITHERS, axes, 0, 2, ncols=3,
                                       off=DITHERS_OFF, tips=DITHER_TIPS)
        self.cb_strengths = self._group("Dither strength", STRENGTHS, axes, 0, 3)
        axes.setColumnStretch(2, 1)
        root.addLayout(axes)

        budget = QGroupBox("Max tiles per view (static image: total tiles)")
        bl = QHBoxLayout(budget)
        bl.addWidget(QLabel("Option 1:"))
        self.ed_t1 = QLineEdit()
        self.ed_t1.setPlaceholderText("auto: max of the view")
        self.ed_t1.textChanged.connect(self.update_projection)
        bl.addWidget(self.ed_t1, 1)
        bl.addWidget(QLabel("Option 2:"))
        self.ed_t2 = QLineEdit()
        self.ed_t2.setPlaceholderText("auto: 75% of the max")
        self.ed_t2.textChanged.connect(self.update_projection)
        bl.addWidget(self.ed_t2, 1)
        bl.addWidget(QLabel("view height"))
        self.cb_view = QComboBox()
        self.cb_view.addItems(["224 (NTSC)", "240 (PAL)"])
        self.cb_view.currentIndexChanged.connect(self.source_changed)
        bl.addWidget(self.cb_view)
        self.ck_margin = QCheckBox("scroll margin")
        self.ck_margin.setToolTip(
            "Count one more tile row and column in every view: a game that\n"
            "scrolls pixel by pixel shows part of 41 columns and 29 (NTSC) or\n"
            "31 (PAL) rows at once, and all of them must be in VRAM.\n"
            "Only on an axis longer than the view (a 320 px wide image\n"
            "gets only the extra row).")
        self.ck_margin.toggled.connect(self.source_changed)
        bl.addWidget(self.ck_margin)
        # v2.16: where the cap applies (was the Background tool's option)
        bl.addWidget(QLabel("cap applies to"))
        self.cb_scope = QComboBox()
        self.cb_scope.addItem("auto", "auto")
        self.cb_scope.addItem("whole image", "image")
        self.cb_scope.addItem("each view", "view")
        self.cb_scope.setToolTip(
            "auto: each view when the image pans, the whole image otherwise.\n"
            "whole image: the tile cap counts every tile of the picture once.\n"
            "each view: no 320x224 (or 240) window at any camera position may\n"
            "pass the cap; the image as a whole may hold more (streaming).")
        bl.addWidget(self.cb_scope)
        root.addWidget(budget)

        # v2.16: the Background tool's options, so the sweep does it all
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Backdrop (index 0):"))
        self.cb_backdrop = QComboBox()
        self.cb_backdrop.addItems(["from border", "fixed colour"])
        self.cb_backdrop.setToolTip(
            "Index 0 of every palette: the colour the VDP shows behind the plane.\n"
            "from border = the colour around the picture (and its pixels become\n"
            "holes when it really is a background); fixed colour = the one you pick,\n"
            "and its pixels are always holes.")
        r1.addWidget(self.cb_backdrop)
        self.bt_backdrop = self._color_button("101820")
        self.bt_backdrop.setEnabled(False)
        self.cb_backdrop.currentIndexChanged.connect(
            lambda i: self.bt_backdrop.setEnabled(i == 1))
        r1.addWidget(self.bt_backdrop)
        r1.addSpacing(16)
        r1.addWidget(QLabel("Over 512 px:"))
        self.cb_over = QComboBox()
        self.cb_over.addItem("keep", "none")
        self.cb_over.addItem("shrink to fit", "resize")
        self.cb_over.addItem("split", "split")
        self.cb_over.setToolTip(
            "A plane holds at most 512x512 px.\n"
            "keep: the size stays, the ROM has to deal with it (streaming).\n"
            "shrink to fit: the picture is resized to fit 512.\n"
            "split: the fewest pieces that fit, as even as they can be; one sweep\n"
            "per piece, each in its own folder (split.html lists them). Gain,\n"
            "backdrop and transparency come from the whole image.")
        self.cb_over.currentIndexChanged.connect(self.update_projection)
        r1.addWidget(self.cb_over)
        r1.addSpacing(16)
        self.ck_pad = QCheckBox("pad to multiples of 8 with")
        self.ck_pad.setChecked(True)
        self.ck_pad.setToolTip("Missing rows and columns are added on the bottom and\n"
                               "right border, never inside the art. Unticked, the\n"
                               "image must already have sides in multiples of 8.")
        r1.addWidget(self.ck_pad)
        self.bt_pad = self._color_button("FFFFFF")
        self.ck_pad.toggled.connect(self.bt_pad.setEnabled)
        r1.addWidget(self.bt_pad)
        r1.addSpacing(16)
        r1.addWidget(QLabel("Behind transparent pixels:"))
        self.bt_bg = self._color_button("FFFFFF")
        self.bt_bg.setToolTip("An alpha channel is composited on this colour.")
        r1.addWidget(self.bt_bg)
        r1.addStretch(1)
        root.addLayout(r1)

        pl = QHBoxLayout()
        pl.addWidget(QLabel("Palette optimized with:"))
        self.cb_paldith = QComboBox()
        self.cb_paldith.addItem("each variant's own dithering (best, slower)", "same")
        self.cb_paldith.addItem("no dithering, one palette for all (faster, old way)", "none")
        self.cb_paldith.currentIndexChanged.connect(self.update_projection)
        pl.addWidget(self.cb_paldith, 1)
        pl.addWidget(QLabel("CPU processes:"))
        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(0, max(1, os.cpu_count() or 1))
        self.sp_workers.setSpecialValueText("auto")
        self.sp_workers.setToolTip("How many variants are computed at the same time. "
                                   "auto = cores - 1, limited by the free memory "
                                   "(about 1.5 GB per process on a big image).")
        pl.addWidget(self.sp_workers)
        pl.addSpacing(12)
        pl.addWidget(QLabel("Variations:"))
        self.sp_var = QSpinBox()
        self.sp_var.setRange(1, 50)
        self.sp_var.setValue(1)
        self.sp_var.setToolTip(
            "Run every combination this many times, each with its own seed\n"
            "(0, 1, 2, ...). The seed changes where the palette search starts\n"
            "and which tiles are merged first, so each variation is a different\n"
            "take on the same task. The same seed always gives the same result.")
        self.sp_var.valueChanged.connect(self.update_projection)
        pl.addWidget(self.sp_var)
        root.addLayout(pl)

        ck = QGridLayout()
        ck.setVerticalSpacing(2)
        self.ck_full = QCheckBox("Save full-size PNGs (for scrollable previews)")
        self.ck_full.setToolTip("Save full-size PNGs: needed for scrollable full-size "
                                "previews in the gallery.")
        self.ck_full.setChecked(True)
        self.ck_full.stateChanged.connect(self.update_projection)
        ck.addWidget(self.ck_full, 0, 0)
        self.ck_export = QCheckBox("Export EVERY variant now (.pal, map, PNG, .res)")
        self.ck_export.setToolTip(
            "Export the palette (.pal), map (JSON), indexed PNG and .res of EVERY\n"
            "variant now. To export just one later, use the box at the bottom.")
        ck.addWidget(self.ck_export, 0, 1)
        self.ck_clean = QCheckBox("Shadow: clean borders of flat dark areas")
        self.ck_clean.setToolTip("Shadow: clean the border of flat dark areas (one solid "
                                 "colour, no dotted halo).")
        self.ck_clean.setChecked(True)
        ck.addWidget(self.ck_clean, 1, 0)
        self.ck_contour = QCheckBox("Shadow: keep contours (contour before colour)")
        self.ck_contour.setToolTip(
            "Shadow: keep contours. Doors and windows are not split between normal\n"
            "and shadow tiles; contour before colour.")
        self.ck_contour.setChecked(True)
        ck.addWidget(self.ck_contour, 1, 1)
        ck.setColumnStretch(2, 1)
        root.addLayout(ck)

        self.lb_info = QLabel("Select the axes; the grid is generated in one run.")
        self.lb_info.setWordWrap(True)
        root.addWidget(self.lb_info)
        self.lb_warn = QLabel("")
        self.lb_warn.setWordWrap(True)
        self.lb_warn.setStyleSheet("color:#d44;font-weight:bold")
        root.addWidget(self.lb_warn)

        bt_row = QHBoxLayout()
        self.bt_run = QPushButton("Generate sweep")
        self.bt_run.setDefault(True)
        self.bt_run.clicked.connect(self.generate)
        bt_row.addWidget(self.bt_run)
        self.bt_open = QPushButton("Open gallery")
        self.bt_open.setEnabled(False)
        self.bt_open.clicked.connect(self.open_gallery)
        bt_row.addWidget(self.bt_open)
        bt_row.addStretch(1)
        root.addLayout(bt_row)

        # v2.11: the files (.pal, map, .res) of ONE variant, picked after the
        # sweep, instead of exporting every variant
        exp = QGroupBox("Export one sample (palette .pal, map, .res, indexed PNG)")
        el = QVBoxLayout(exp)
        er = QHBoxLayout()
        er.addWidget(QLabel("Sweep folder:"))
        self.ed_sweep = QLineEdit()
        self.ed_sweep.setPlaceholderText("filled after a sweep, or browse an older sweep folder")
        self.ed_sweep.setAcceptDrops(False)
        self.ed_sweep.textChanged.connect(self.load_variants)
        er.addWidget(self.ed_sweep, 2)
        bt_sw = QPushButton("Browse…")
        bt_sw.clicked.connect(self.browse_sweep)
        er.addWidget(bt_sw)
        nr = er
        nr.addSpacing(12)
        nr.addWidget(QLabel("Name:"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("blank = image name")
        self.ed_name.setToolTip("Name of the .res, palettes, map and indexed PNG\n"
                                "(letters, digits and _ ; used in the C code).")
        nr.addWidget(self.ed_name, 1)
        nr.addWidget(QLabel("res folder:"))
        self.ed_res = QLineEdit()
        self.ed_res.setPlaceholderText("blank = inside the sweep folder")
        self.ed_res.setToolTip("Where the .res and its files go, such as the res folder\n"
                               "of your project. Files of the same name there are\n"
                               "replaced.")
        self.ed_res.setAcceptDrops(False)
        nr.addWidget(self.ed_res, 2)
        bt_res = QPushButton("Browse…")
        bt_res.clicked.connect(self.browse_res)
        nr.addWidget(bt_res)
        el.addLayout(er)
        vr = QHBoxLayout()
        self.cb_variant = QComboBox()
        self.cb_variant.setToolTip("The name in brackets is the one written under each "
                                   "picture in the gallery (sweep.html).")
        vr.addWidget(self.cb_variant, 1)
        self.bt_export1 = QPushButton("Export this one")
        self.bt_export1.setEnabled(False)
        self.bt_export1.clicked.connect(self.export_one)
        vr.addWidget(self.bt_export1)
        self.bt_files = QPushButton("Open its files")
        self.bt_files.setEnabled(False)
        self.bt_files.clicked.connect(self.open_files)
        vr.addWidget(self.bt_files)
        el.addLayout(vr)
        root.addWidget(exp)
        self.export_dir = ""

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setAcceptDrops(False)
        self.log.viewport().setAcceptDrops(False)
        self.log.setStyleSheet("font-family:Consolas,monospace")
        root.addWidget(self.log, 1)

        if source:
            self.ed_source.setText(source)
        self.update_projection()

    def _group(self, title, values, layout, row, col, ncols=1, off=(), tips=None):
        box = QGroupBox(title)
        inner = QGridLayout(box)
        cbs = {}
        nrows = -(-len(values) // ncols)
        for i, v in enumerate(values):
            cb = QCheckBox(v)
            cb.setChecked(v not in off)
            if tips and v in tips:
                cb.setToolTip(tips[v])
            cb.stateChanged.connect(self.update_projection)
            inner.addWidget(cb, i % nrows, i // nrows)
            cbs[v] = cb
        inner.setRowStretch(nrows, 1)
        layout.addWidget(box, row, col)
        return cbs

    def browse_source(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Source image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*)")
        if p:
            self.ed_source.setText(p)

    def _color_button(self, hexv):
        b = QPushButton()
        b.setProperty("hex", hexv)

        def paint():
            hx = b.property("hex")
            light = sum(int(hx[i:i + 2], 16) for i in (0, 2, 4)) > 380
            b.setText("#" + hx)
            b.setStyleSheet(f"background:#{hx}; color:{'#000' if light else '#fff'};")

        def pick():
            c = QColorDialog.getColor(QColor("#" + b.property("hex")), self)
            if c.isValid():
                b.setProperty("hex", c.name()[1:].upper())
                paint()

        b.clicked.connect(pick)
        paint()
        return b

    def browse_res(self):
        p = QFileDialog.getExistingDirectory(self, "res folder", self.ed_res.text().strip())
        if p:
            self.ed_res.setText(p)

    def browse_palfile(self):
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Palette file(s)", "",
            "Palettes (*.pal *.png);;All files (*)")
        if ps:
            self.ed_palfile.setText(";".join(ps))

    def palfile_changed(self):
        """Read the palette file(s) now, so a wrong file shows before the run."""
        text = self.ed_palfile.text().strip()
        self.fixed_n = 0
        ok_style, bad_style = "color:#2a7", "color:#d44;font-weight:bold"
        if not text:
            self.lb_pal.setText("")
        else:
            try:
                pals, notes = md.load_palette_set(text, (0, 0, 0))
                self.fixed_n = len(pals)
                self.lb_pal.setStyleSheet(ok_style)
                self.lb_pal.setText(
                    f"{self.fixed_n} palette{'s' if self.fixed_n > 1 else ''} from the "
                    f"file - no palette is made, the Palettes boxes are ignored. "
                    + " · ".join(notes))
            except BaseException as exc:   # SystemExit too: never close the window
                self.lb_pal.setStyleSheet(bad_style)
                self.lb_pal.setText(f"palette file: {exc}")
                self.fixed_n = -1
        fixed = self.fixed_n > 0
        for cb in self.cb_palettes.values():
            cb.setEnabled(not fixed)
        self.cb_paldith.setEnabled(not fixed)
        self.update_projection()

    def browse_out(self):
        p = QFileDialog.getExistingDirectory(self, "Output folder")
        if p:
            self.ed_out.setText(p)

    # ---------------- drag and drop
    @staticmethod
    def _first_local_file(mime):
        if not mime.hasUrls():
            return ""
        for url in mime.urls():
            p = url.toLocalFile()
            if p and os.path.isfile(p):
                return p
        return ""

    def _drag_hint(self, on):
        self.ed_source.setStyleSheet("border:2px solid #2a7" if on else "")

    def dragEnterEvent(self, e):
        if self._first_local_file(e.mimeData()):
            self._drag_hint(True)
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._first_local_file(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._drag_hint(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._drag_hint(False)
        p = self._first_local_file(e.mimeData())
        if not p:
            e.ignore()
            return
        self.ed_source.setText(p)
        if not self.width or not self.height:
            self._log("not an image file: " + p)
        e.acceptProposedAction()

    def source_changed(self):
        p = self.ed_source.text().strip()
        self.source = p
        self.width = self.height = 0
        if p and os.path.isfile(p):
            try:
                with Image.open(p) as im:
                    self.width, self.height = im.size
            except Exception:
                pass
        self.rs.set_source_size(self.width, self.height)
        if self.width and self.height:
            # the sweep works on the resized picture: size and tiles follow it
            try:
                self.width, self.height = md.resize_size(self.width, self.height,
                                                         self.rs.value())
            except BaseException as exc:
                self._log(f"resize: {exc}")
        self.src_info = None
        if self.width and self.height:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                md.set_view_height(240 if self.cb_view.currentIndex() == 1 else 224)
                md.set_scroll_margin(self.ck_margin.isChecked())
                rgb, _ = md.pad8(md.resize_to(md.load(p), self.rs.value(), self.rs.method()))
                self.src_info = md.preflight(rgb)
                ag = md.auto_gain(rgb)
                self.lb_autogain.setText(
                    f"(auto for this image: x{ag:.2f}"
                    + (", no change)" if ag <= 1.0001 else ")"))
            except BaseException as exc:   # SystemExit too: never close the window
                self._log(f"source analysis failed: {exc}")
            finally:
                QApplication.restoreOverrideCursor()
        self.update_projection()

    def selected(self, cbs):
        return [v for v, cb in cbs.items() if cb.isChecked()]

    def paldith(self):
        cb = getattr(self, "cb_paldith", None)
        return (cb.currentData() if cb is not None else None) or "same"

    def budget_values(self):
        """The budgets the core will use (same rule as the sweep)."""
        si = getattr(self, "src_info", None)
        if not si:
            return None
        text = ",".join(t for t in (self.ed_t1.text().strip(),
                                    self.ed_t2.text().strip()) if t)
        try:
            return md._parse_budgets(text, si["pior_tela"])[0]
        except SystemExit:
            return None

    def budget_count(self):
        """How many distinct budgets the core will use (usually 2)."""
        vals = []
        for text in (self.ed_t1.text().strip(), self.ed_t2.text().strip()):
            if text:
                try:
                    v = int(text)
                    if v > 0:
                        vals.append(max(8, v))
                except ValueError:
                    return 2
        if len(vals) <= 1:
            return 2
        return len(set(vals))

    def projection(self):
        pals = self.selected(self.cb_palettes)
        if getattr(self, "fixed_n", 0) > 0:
            pals = [str(self.fixed_n)]   # the file decides the palette count
        modes = self.selected(self.cb_modes)
        diths = self.selected(self.cb_dithers)
        strengths = self.selected(self.cb_strengths)
        specs = 0
        for d in diths:
            specs += 1 if d == "none" else len(strengths)
        budgets = self.budget_count()
        samples = len(pals) * len(modes) * specs * budgets * self.sp_var.value()
        all_checked = (len(pals) == len(PALETTES) and len(modes) == len(MODES)
                       and len(diths) == len(DITHERS)
                       and len(strengths) == len(STRENGTHS))
        return pals, modes, diths, strengths, specs, budgets, samples, all_checked

    def n_opt(self, pals, diths):
        """How many palette optimizations the sweep will run (0 with a file)."""
        if getattr(self, "fixed_n", 0) > 0:
            return 0
        per = len(diths) if self.paldith() == "same" else 1
        return len(pals) * per * self.sp_var.value()   # one search per seed too

    def update_projection(self):
        pals, modes, diths, strengths, specs, budgets, samples, all_checked = \
            self.projection()
        if not self.width or not self.height:
            self.lb_info.setText("Select an image to see the projected samples.")
            self.lb_warn.setText("")
            return
        pan = self.width > 320 or self.height > 224
        est = md.estimate_sweep_seconds(self.width, self.height, samples, getattr(self, 'src_info', None), self.budget_values(), self.n_opt(pals, diths))
        full_mb = (0.35 * self.width * self.height * samples / 1_000_000.0
                   if self.ck_full.isChecked() else 0.0)
        src = ""
        si = getattr(self, "src_info", None)
        if si:
            src = (f"SOURCE: {si['cores']} colors ({si['cores_md']} on the MD grid) · "
                   f"{si['tiles_unicos']} unique tiles in the image · "
                   f"worst {md.view_label()} view {si['pior_tela']} unique tiles\n")
        self.lb_info.setText(
            src + f"{self.width}x{self.height} · {'pan background' if pan else 'static image'} · "
            f"{len(pals)} palettes × {len(modes)} modes × {specs} dither/strength "
            f"columns × {budgets} budgets"
            + (f" × {self.sp_var.value()} variations" if self.sp_var.value() > 1 else "")
            + f" = {samples} samples · "
            f"rough estimate {est / 60.0:.1f} min · "
            f"full PNGs ~{full_mb:.1f} MB")
        msgs = []
        if all_checked:
            msgs.append(f"WARNING: every option is checked: {samples} samples "
                        f"(~{est / 60.0:.1f} min rough estimate, "
                        f"~{full_mb:.1f} MB of full PNGs).")
        if self.width > 512 or self.height > 512:
            over = self.cb_over.currentData() if hasattr(self, "cb_over") else "none"
            if over == "split":
                n_seg = len(md.segments_of(self.height, self.width))
                msgs.append(f"Image above 512x512: split into {n_seg} segments, "
                            f"one sweep each ({n_seg} x the samples above).")
            elif over == "resize":
                msgs.append("Image above 512x512: shrunk to fit the 512 px plane.")
            else:
                msgs.append("WARNING: image is above 512x512 and kept at its size "
                            "(Over 512 px: keep); the ROM must handle the plane limit.")
        self.lb_warn.setText(" ".join(msgs))
        if not pals or not modes or not diths or not strengths:
            self.lb_warn.setText("Select at least one option in every group.")

    def generate(self):
        p = self.ed_source.text().strip()
        if not p or not os.path.isfile(p):
            QMessageBox.warning(self, "Source", "Select a valid image first.")
            return
        pals, modes, diths, strengths, specs, budgets, samples, all_checked = \
            self.projection()
        if not pals or not modes or not diths or not strengths:
            QMessageBox.warning(self, "Axes", "Select at least one option in every group.")
            return
        if self.fixed_n < 0:
            QMessageBox.warning(self, "Palette file",
                                "The palette file could not be read:\n\n"
                                + self.lb_pal.text() + "\n\nFix it or clear the field.")
            return
        for text in (self.ed_t1.text().strip(), self.ed_t2.text().strip()):
            if text:
                try:
                    int(text)
                except ValueError:
                    QMessageBox.warning(self, "Max tiles",
                                        "The two max-tile fields must be integers or blank.")
                    return
        if all_checked:
            est = md.estimate_sweep_seconds(self.width, self.height, samples, getattr(self, 'src_info', None), self.budget_values(), self.n_opt(pals, diths))
            full_mb = (0.35 * self.width * self.height * samples / 1_000_000.0
                       if self.ck_full.isChecked() else 0.0)
            r = QMessageBox.warning(
                self, "Large sweep",
                f"Every option is checked.\n\n{samples} samples will be generated "
                f"(~{est / 60.0:.1f} min rough estimate).\n"
                f"Full-size PNGs: ~{full_mb:.1f} MB.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return

        args = argparse.Namespace(
            cmd="sweep", source=p, out=self.ed_out.text().strip() or None,
            palettes=",".join(pals), dithering=",".join(diths),
            dither_strength=",".join(strengths), modes=",".join(modes),
            palette_dither=self.paldith(), palette_rounds=2,
            export=self.ck_export.isChecked(), workers=self.sp_workers.value(),
            shadow_dotted_edges=not self.ck_clean.isChecked(),
            shadow_free_contours=not self.ck_contour.isChecked(),
            max_tiles=f"{self.ed_t1.text().strip()},{self.ed_t2.text().strip()}",
            cap_scope=self.cb_scope.currentData(), crop=None, full=self.ck_full.isChecked(),
            gain="auto" if self.ck_gain.isChecked() else f"{self.sp_gain.value():.2f}",
            brightness=float(self.sp_bright.value()),
            palette_file=self.ed_palfile.text().strip() or None,
            color_weight=self.sp_cweight.value() / 100.0,
            solid=self.sp_solid.value() / 100.0,
            grey=self.ck_grey.isChecked(),
            bg_color=self.bt_bg.property("hex"), no_pad=not self.ck_pad.isChecked(),
            pad_color=self.bt_pad.property("hex"),
            backdrop="color" if self.cb_backdrop.currentIndex() == 1 else "border",
            backdrop_color=(self.bt_backdrop.property("hex")
                            if self.cb_backdrop.currentIndex() == 1 else None),
            oversize=self.cb_over.currentData(),
            seed=0, variations=self.sp_var.value(),
            view_height=240 if self.cb_view.currentIndex() == 1 else 224,
            scroll_margin=self.ck_margin.isChecked(),
            resize=self.rs.value(), resize_method=self.rs.method(),
        )
        self.log.clear()
        self.bt_run.setEnabled(False)
        self.bt_open.setEnabled(False)
        self.actual_out = ""
        self.worker = _Worker(args)
        self.worker.log.connect(self._log)
        self.worker.done.connect(self._done)
        self.worker.start()

    def closeEvent(self, e):
        """Closing during a sweep: ask, then stop the worker processes cleanly.

        Qt aborts the whole program when a window closes while its worker
        thread still runs, so the close is handled here instead."""
        if self.worker is not None and self.worker.isRunning():
            r = QMessageBox.question(
                self, "Sweep running",
                "A sweep is still running. Stop it and close?" + chr(10) + chr(10) +
                "The variants already finished stay in the output folder, "
                "but the HTML page is only written at the end.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                e.ignore()
                return
            try:
                md.stop_sweep()
            finally:
                os._exit(0)
        e.accept()

    def _log(self, text):
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.End)
        self.log.setTextCursor(cur)
        self.log.insertPlainText(text)

    def _done(self, ok, out):
        self.bt_run.setEnabled(True)
        self.actual_out = out if ok else ""
        self.split_dir = ""
        if ok and out:
            self.bt_open.setEnabled(True)
            parent = os.path.dirname(os.path.normpath(out))
            if os.path.isfile(os.path.join(parent, "split.json")):
                # v2.16: a split image - one sweep per segment; the gallery
                # button opens the list, the export box starts on segment 1
                self.split_dir = parent
                self.ed_out.setText(parent)
                self._log(f"\nsplit into segments: {parent}\n"
                          "  the export box shows segment 1; Browse... picks another "
                          "segment folder\n")
            else:
                self.ed_out.setText(out)
            self.ed_sweep.setText(out)
            self._log(f"\nsweep written to: {out}\n")
        else:
            self._log(f"\nsweep failed: {out}\n")

    # ---------------- export of one variant
    def browse_sweep(self):
        p = QFileDialog.getExistingDirectory(self, "Sweep folder")
        if p:
            self.ed_sweep.setText(p)

    def load_variants(self):
        self.cb_variant.clear()
        self.bt_export1.setEnabled(False)
        folder = self.ed_sweep.text().strip()
        if not folder or not os.path.isfile(os.path.join(folder, "sweep.json")):
            if folder:
                self.cb_variant.addItem("(no sweep.json in this folder)")
            return
        try:
            _, meta = md.load_sweep(folder)
        except BaseException as exc:
            self.cb_variant.addItem(f"(could not read sweep.json: {exc})")
            return
        for v in meta.get("variants", []):
            stem = md.variant_stem(v)
            self.cb_variant.addItem(f"{md.variant_label(v)}   [{stem}]", stem)
        self.bt_export1.setEnabled(self.cb_variant.count() > 0
                                   and not (self.worker is not None and self.worker.isRunning()))

    def export_one(self):
        folder = self.ed_sweep.text().strip()
        stem = self.cb_variant.currentData()
        if not folder or not stem:
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the running job to finish.")
            return

        name = self.ed_name.text().strip() or None
        res_dir = self.ed_res.text().strip() or None
        self.res_dir = res_dir

        def job():
            exp_dir, same = md.export_one(folder, stem, name=name, res_dir=res_dir)
            return exp_dir

        self._log(f"\nexporting {stem}...\n")
        self.bt_run.setEnabled(False)
        self.bt_export1.setEnabled(False)
        self.worker = _Worker(None, job)
        self.worker.log.connect(self._log)
        self.worker.done.connect(self._export_done)
        self.worker.start()

    def _export_done(self, ok, out):
        self.bt_run.setEnabled(True)
        self.bt_export1.setEnabled(True)
        if ok and out:
            self.export_dir = out
            self.bt_files.setEnabled(True)
            self._log(f"export written to: {out}\n")
        else:
            self._log(f"export failed: {out}\n")

    def open_files(self):
        if getattr(self, "res_dir", None) and os.path.isdir(self.res_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.res_dir))
            return
        if self.export_dir and os.path.isdir(self.export_dir):
            res = os.path.join(self.export_dir, "res")
            QDesktopServices.openUrl(QUrl.fromLocalFile(res if os.path.isdir(res)
                                                        else self.export_dir))

    def open_gallery(self):
        if not self.actual_out:
            return
        if getattr(self, "split_dir", ""):
            QDesktopServices.openUrl(QUrl.fromLocalFile(
                os.path.join(self.split_dir, "split.html")))
            return
        p = os.path.join(self.actual_out, "sweep.html")
        if os.path.exists(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))


def main():
    ap = argparse.ArgumentParser(description="md_fundo sweep GUI")
    ap.add_argument("source", nargs="?", default="")
    a = ap.parse_args()
    app = QApplication([sys.argv[0]])
    w = SweepWindow(a.source)
    w.show()
    return app.exec()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
