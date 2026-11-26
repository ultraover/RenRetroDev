# -*- coding: utf-8 -*-
"""Window of the sprite converter (md_sprite.py).

    python sprite_gui.py [folder of frames]

Drop a folder of frames (or several frame files) on the window, tick the
dithers to try, press Convert, compare them animated in the gallery and
export the one you like for SGDK.
"""
import argparse
import contextlib
import os
import sys
import traceback

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import md_sprite as ms  # noqa: E402
import numpy as np  # noqa: E402

md = ms.md
DITHERS = md.DITHERS
DITHERS_ON = ("none", "ordered2", "ordered4")
STRENGTHS = ("clean", "balanced", "full")
DITHER_TIPS = {
    "none": "Solid colours only: no dots, no flicker.",
    "ordered2": "Bayer 2x2 held at 50%: a fine checkerboard between neighbour colours. "
                "Tied to the frame, so it stays put while the character stands still.",
    "ordered4": "Bayer 4x4: more tones, a coarser pattern; also tied to the frame.",
    "floyd": "Error diffusion: the smoothest still image, but the dots change from "
             "frame to frame (see the flicker number).",
}


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

    def __init__(self, job):
        super().__init__()
        self.job = job

    def run(self):
        stream = _Stream()
        stream.text.connect(self.log)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                out = self.job()
            self.done.emit(True, out or "")
        except BaseException as exc:
            self.log.emit("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
            self.done.emit(False, str(exc))


class SpriteWindow(QWidget):
    def __init__(self, source=""):
        super().__init__()
        self.setWindowTitle("MD Background Tools · Sprite")
        self.resize(1040, 900)
        self.setAcceptDrops(True)
        self.worker = None
        self.run_dir = ""
        self.res_dir = None
        self.export_dir = ""
        root = QVBoxLayout(self)

        # ---- frames
        r = QHBoxLayout()
        r.addWidget(QLabel("Frames:"))
        self.ed_source = QLineEdit()
        self.ed_source.setPlaceholderText(
            "drop a folder of frames (walk_01.png, walk_02.png, jump_01.png...) or the files")
        self.ed_source.setAcceptDrops(False)
        self.ed_source.textChanged.connect(self.source_changed)
        r.addWidget(self.ed_source, 1)
        b = QPushButton("Folder…")
        b.clicked.connect(self.browse_folder)
        r.addWidget(b)
        b = QPushButton("Files…")
        b.clicked.connect(self.browse_files)
        r.addWidget(b)
        root.addLayout(r)
        self.lb_frames = QLabel("Each animation is one row of the sheet: walk_01, walk_02... "
                                "-> walk; jump1, jump2... -> jump.")
        self.lb_frames.setWordWrap(True)
        root.addWidget(self.lb_frames)
        r = QHBoxLayout()
        r.addWidget(QLabel("Output folder:"))
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("blank = sprite_<name> next to the frames folder")
        self.ed_out.setAcceptDrops(False)
        r.addWidget(self.ed_out, 2)
        r.addWidget(QLabel("Sprite name:"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("blank = first animation")
        self.ed_name.setToolTip("Name of the files and of the symbol in the C code.")
        r.addWidget(self.ed_name, 1)
        root.addLayout(r)

        top = QHBoxLayout()
        # ---- frame size
        g = QGroupBox("Frame size")
        f = QGridLayout(g)
        f.addWidget(QLabel("Frames:"), 0, 0)
        self.cb_frames = QComboBox()
        self.cb_frames.addItem("trim to fit (one size for all)", "trim")
        self.cb_frames.addItem("keep the canvas", "keep")
        self.cb_frames.setToolTip(
            "trim: the smallest box that holds the character in EVERY frame, grown to\n"
            "multiples of 8 around the anchor (SGDK wants one size per sheet).\n"
            "keep: the canvas you drew, only grown to multiples of 8.")
        f.addWidget(self.cb_frames, 0, 1)
        f.addWidget(QLabel("Anchor:"), 1, 0)
        self.cb_anchor = QComboBox()
        self.cb_anchor.addItem("bottom centre (feet)", "bottom")
        self.cb_anchor.addItem("centre", "center")
        self.cb_anchor.addItem("top-left corner", "top-left")
        self.cb_anchor.setToolTip(
            "How canvases of different sizes line up, and the point the report gives\n"
            "for placing the sprite. Bottom centre keeps the feet on one line.")
        f.addWidget(self.cb_anchor, 1, 1)
        f.addWidget(QLabel("Scale:"), 2, 0)
        sr = QHBoxLayout()
        self.sp_scale = QSpinBox()
        self.sp_scale.setRange(5, 800)
        self.sp_scale.setValue(100)
        self.sp_scale.setSuffix(" %")
        sr.addWidget(self.sp_scale)
        self.cb_method = QComboBox()
        self.cb_method.addItems(md.RESIZE_METHODS)
        self.cb_method.setToolTip("nearest: sharp, adds no colour (pixel art).\n"
                                  "area/lanczos: best to shrink a render or a painting.")
        sr.addWidget(self.cb_method)
        f.addLayout(sr, 2, 1)
        # output size: auto (from Frames) or a size the Mega Drive takes
        f.addWidget(QLabel("Output size:"), 3, 0)
        zr = QHBoxLayout()
        self.cb_size = QComboBox()
        self.cb_size.addItem("auto (from Frames)", "auto")
        for w_, h_ in ((16, 16), (16, 24), (24, 24), (24, 32), (32, 32), (32, 48),
                       (48, 48), (48, 64), (64, 64), (64, 96), (96, 96), (128, 128)):
            n_ = len(ms.hw_split(np.ones((h_ // 8, w_ // 8), bool)))
            self.cb_size.addItem(f"{w_}x{h_} px ({w_ // 8}x{h_ // 8} tiles, "
                                 f"{n_} sprite{'s' if n_ > 1 else ''} if full)",
                                 f"{w_}x{h_}")
        self.cb_size.addItem("custom (tiles)", "custom")
        self.cb_size.setToolTip(
            "The size of every frame of the sheet. A hardware sprite is 1 to 4 tiles a\n"
            "side (8 to 32 px); a bigger frame is made of several (rescomp takes 16\n"
            f"per frame at most, and {ms.FRAME_MAX_PX} px a side at most).")
        zr.addWidget(self.cb_size, 1)
        self.sp_tw = QSpinBox()
        self.sp_tw.setRange(1, ms.FRAME_MAX_PX // 8)
        self.sp_tw.setValue(4)
        self.sp_th = QSpinBox()
        self.sp_th.setRange(1, ms.FRAME_MAX_PX // 8)
        self.sp_th.setValue(4)
        for sp_ in (self.sp_tw, self.sp_th):
            sp_.setSuffix(" t")
            sp_.setToolTip("tiles (8 px each)")
            sp_.setEnabled(False)
        zr.addWidget(self.sp_tw)
        zr.addWidget(QLabel("x"))
        zr.addWidget(self.sp_th)
        f.addLayout(zr, 3, 1)
        f.addWidget(QLabel("Fit:"), 4, 0)
        self.cb_fit = QComboBox()
        self.cb_fit.addItem("scale the character to the size", "fit")
        self.cb_fit.addItem("crop (keep the pixels)", "crop")
        self.cb_fit.setToolTip(
            "scale: every frame is scaled alike (aspect kept, with the Scale method)\n"
            "so the character fills the size. crop: no scaling, what is outside the\n"
            "size around the anchor is cut.")
        self.cb_fit.setEnabled(False)
        f.addWidget(self.cb_fit, 4, 1)
        self.cb_size.currentIndexChanged.connect(self._size_changed)
        top.addWidget(g)

        # ---- transparency
        g = QGroupBox("Transparency (index 0)")
        f = QGridLayout(g)
        f.addWidget(QLabel("From:"), 0, 0)
        self.cb_transp = QComboBox()
        self.cb_transp.addItem("auto (alpha, else border colour)", "auto")
        self.cb_transp.addItem("alpha channel", "alpha")
        self.cb_transp.addItem("key colour", "key")
        self.cb_transp.addItem("border colour", "border")
        self.cb_transp.currentIndexChanged.connect(self._transp_changed)
        # the key colour button is wired to the output line once it exists
        f.addWidget(self.cb_transp, 0, 1)
        self.bt_key = self._color_button("FF00FF")
        self.bt_key.setEnabled(False)
        f.addWidget(self.bt_key, 0, 2)
        f.addWidget(QLabel("Alpha below:"), 1, 0)
        self.sp_alpha = QSpinBox()
        self.sp_alpha.setRange(1, 255)
        self.sp_alpha.setValue(128)
        self.sp_alpha.setToolTip("Pixels with less alpha than this are transparent.")
        f.addWidget(self.sp_alpha, 1, 1)
        f.addWidget(QLabel("Key tolerance:"), 2, 0)
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0, 40)
        self.sp_tol.setValue(3.0)
        self.sp_tol.setSuffix(" dE")
        self.sp_tol.setToolTip("How far from the key colour a pixel is still background.")
        f.addWidget(self.sp_tol, 2, 1)
        f.addWidget(QLabel("Clean edge:"), 3, 0)
        self.sp_edge = QSpinBox()
        self.sp_edge.setRange(0, 8)
        self.sp_edge.setValue(2)
        self.sp_edge.setSuffix(" px")
        self.sp_edge.setToolTip(
            "Key or border colour only: the band of N px along the edge is repaired, so\n"
            "no halo of the old background is left (0 = off). Raise it if a halo remains.")
        f.addWidget(self.sp_edge, 3, 1)
        top.addWidget(g)

        # ---- palette
        g = QGroupBox("Palette (one for every frame)")
        f = QGridLayout(g)
        f.addWidget(QLabel("Palette file:"), 0, 0)
        pr = QHBoxLayout()
        self.ed_pal = QLineEdit()
        self.ed_pal.setPlaceholderText("blank = made from all the frames")
        self.ed_pal.setAcceptDrops(False)
        pr.addWidget(self.ed_pal, 1)
        b = QPushButton("…")
        b.setFixedWidth(28)
        b.clicked.connect(self.browse_pal)
        pr.addWidget(b)
        f.addLayout(pr, 0, 1)
        f.addWidget(QLabel("Lock colours:"), 1, 0)
        self.ed_lock = QLineEdit()
        self.ed_lock.setPlaceholderText("RRGGBB, RRGGBB… kept exact")
        self.ed_lock.setToolTip("Colours that must be in the palette exactly (eyes, skin,\n"
                                "a colour shared with another character).")
        f.addWidget(self.ed_lock, 1, 1)
        self.ck_ops = QCheckBox("leave 14 and 15 free (PAL3 with shadow/highlight)")
        self.ck_ops.setToolTip("With shadow/highlight on, colours 14 and 15 of PAL3 are\n"
                               "operators on sprites. The palette then has 13 colours.")
        f.addWidget(self.ck_ops, 2, 0, 1, 2)
        gr = QHBoxLayout()
        self.ck_gain = QCheckBox("auto gain")
        self.ck_gain.setToolTip("Brighten until the brightest pixels reach about 230.")
        gr.addWidget(self.ck_gain)
        gr.addWidget(QLabel("offset"))
        self.sp_bright = QSpinBox()
        self.sp_bright.setRange(-100, 100)
        gr.addWidget(self.sp_bright)
        gr.addStretch(1)
        f.addLayout(gr, 3, 0, 1, 2)
        top.addWidget(g, 1)
        root.addLayout(top)
        self.lb_output = QLabel("Output: drop the frames to see the frame size and its "
                                "hardware sprites.")
        self.lb_output.setWordWrap(True)
        self.lb_output.setStyleSheet("color:#2a7a2a;font-weight:bold")
        self.lb_output.setToolTip(
            "Mega Drive: a hardware sprite is 1 to 4 tiles a side (8 to 32 px). The\n"
            "frame is cut into the fewest pieces (an estimate: rescomp cuts with its own\n"
            "optimiser). rescomp takes 16 hardware sprites per frame at most.")
        root.addWidget(self.lb_output)
        self._plan_timer = QTimer(self)
        self._plan_timer.setSingleShot(True)
        self._plan_timer.setInterval(450)
        self._plan_timer.timeout.connect(self.update_output)
        for w_ in (self.cb_frames, self.cb_anchor, self.cb_method, self.cb_transp,
                   self.cb_size, self.cb_fit):
            w_.currentIndexChanged.connect(self._plan_timer.start)
        for w_ in (self.sp_scale, self.sp_alpha, self.sp_edge, self.sp_tw, self.sp_th):
            w_.valueChanged.connect(self._plan_timer.start)
        self.sp_tol.valueChanged.connect(self._plan_timer.start)
        self.ed_source.textChanged.connect(self._plan_timer.start)

        # ---- dithering
        axes = QHBoxLayout()
        g = QGroupBox("Dithering")
        gl = QGridLayout(g)
        self.cb_dithers = {}
        for i, d in enumerate(DITHERS):
            cb = QCheckBox(d)
            cb.setChecked(d in DITHERS_ON)
            if d in DITHER_TIPS:
                cb.setToolTip(DITHER_TIPS[d])
            gl.addWidget(cb, i % 4, i // 4)
            self.cb_dithers[d] = cb
        axes.addWidget(g, 1)
        g = QGroupBox("Dither strength")
        gl = QVBoxLayout(g)
        self.cb_strengths = {}
        for s in STRENGTHS:
            cb = QCheckBox(s)
            cb.setChecked(s == "balanced")
            gl.addWidget(cb)
            self.cb_strengths[s] = cb
        axes.addWidget(g)
        g = QGroupBox("Colour")
        gl = QGridLayout(g)
        gl.addWidget(QLabel("Solid vs dots:"), 0, 0)
        self.sp_solid = QSpinBox()
        self.sp_solid.setRange(0, 100)
        self.sp_solid.setSingleStep(5)
        self.sp_solid.setValue(50)
        self.sp_solid.setSuffix(" %")
        self.sp_solid.setToolTip("50% = normal. Higher: fewer dots, flatter areas. "
                                 "Lower: more tones, more grain.")
        gl.addWidget(self.sp_solid, 0, 1)
        gl.addWidget(QLabel("Colour weight:"), 1, 0)
        self.sp_cweight = QSpinBox()
        self.sp_cweight.setRange(0, 100)
        self.sp_cweight.setSingleStep(5)
        self.sp_cweight.setValue(100)
        self.sp_cweight.setSuffix(" %")
        self.sp_cweight.setToolTip("Colour against lightness (100% = normal).")
        gl.addWidget(self.sp_cweight, 1, 1)
        self.ck_grey = QCheckBox("grey mode")
        self.ck_grey.toggled.connect(lambda s: self.sp_cweight.setEnabled(not s))
        gl.addWidget(self.ck_grey, 2, 0, 1, 2)
        gl.addWidget(QLabel("Speed:"), 3, 0)
        self.sp_fps = QSpinBox()
        self.sp_fps.setRange(1, 60)
        self.sp_fps.setValue(10)
        self.sp_fps.setSuffix(" fps")
        self.sp_fps.setToolTip("Animation speed in the gallery and in the .res\n"
                               "(SGDK frame time = 60 / fps).")
        gl.addWidget(self.sp_fps, 3, 1)
        axes.addWidget(g)
        root.addLayout(axes)

        br = QHBoxLayout()
        self.bt_run = QPushButton("Convert")
        self.bt_run.setDefault(True)
        self.bt_run.clicked.connect(self.convert)
        br.addWidget(self.bt_run)
        self.bt_open = QPushButton("Open gallery")
        self.bt_open.setEnabled(False)
        self.bt_open.clicked.connect(self.open_gallery)
        br.addWidget(self.bt_open)
        br.addStretch(1)
        root.addLayout(br)

        # ---- export
        g = QGroupBox("Export one variant for SGDK (sprite sheet .png, .pal, .res, report)")
        el = QVBoxLayout(g)
        er = QHBoxLayout()
        er.addWidget(QLabel("Run folder:"))
        self.ed_run = QLineEdit()
        self.ed_run.setPlaceholderText("filled after a conversion, or browse an older one")
        self.ed_run.setAcceptDrops(False)
        self.ed_run.textChanged.connect(self.load_variants)
        er.addWidget(self.ed_run, 2)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_run)
        er.addWidget(b)
        er.addSpacing(12)
        er.addWidget(QLabel("res folder:"))
        self.ed_res = QLineEdit()
        self.ed_res.setPlaceholderText("blank = inside the run folder")
        self.ed_res.setToolTip("Where the files go, such as the res folder of your\n"
                               "project. Files of the same name there are replaced.")
        self.ed_res.setAcceptDrops(False)
        er.addWidget(self.ed_res, 2)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_res)
        er.addWidget(b)
        el.addLayout(er)
        vr = QHBoxLayout()
        self.cb_variant = QComboBox()
        vr.addWidget(self.cb_variant, 1)
        self.bt_export = QPushButton("Export this one")
        self.bt_export.setEnabled(False)
        self.bt_export.clicked.connect(self.export_one)
        vr.addWidget(self.bt_export)
        self.bt_files = QPushButton("Open its files")
        self.bt_files.setEnabled(False)
        self.bt_files.clicked.connect(self.open_files)
        vr.addWidget(self.bt_files)
        el.addLayout(vr)
        root.addWidget(g)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family:Consolas,monospace")
        root.addWidget(self.log, 1)
        if source:
            self.ed_source.setText(source)

    # ---------------- helpers
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

    def _log(self, s):
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(s)
        self.log.moveCursor(QTextCursor.End)

    def _transp_changed(self):
        self.bt_key.setEnabled(self.cb_transp.currentData() == "key")

    def _size_changed(self):
        data = self.cb_size.currentData()
        for sp_ in (self.sp_tw, self.sp_th):
            sp_.setEnabled(data == "custom")
        self.cb_fit.setEnabled(data != "auto")
        self.cb_frames.setEnabled(data == "auto")

    def size_text(self):
        data = self.cb_size.currentData()
        if data == "custom":
            return f"{self.sp_tw.value()}x{self.sp_th.value()}t"
        return data

    def update_output(self):
        src = self.ed_source.text().strip()
        if not src:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            txt = ms.plan_output(
                src, self.cb_frames.currentData(), self.cb_anchor.currentData(),
                self.size_text(), self.cb_fit.currentData(), self.sp_scale.value() / 100.0,
                self.cb_method.currentText(), self.cb_transp.currentData(),
                (ms.parse_colors(self.bt_key.property("hex"))[0]
                 if self.cb_transp.currentData() == "key" else None),
                self.sp_alpha.value(), self.sp_tol.value(), self.sp_edge.value())
            over = "over " + str(ms.HW_PER_FRAME) in txt or "WARNING" in txt
            self.lb_output.setStyleSheet("color:%s;font-weight:bold"
                                         % ("#c33" if over or "crop cuts" in txt
                                            else "#2a7a2a"))
            self.lb_output.setText("Output: " + txt.replace("output frame ", "frame "))
        except SystemExit as exc:
            self.lb_output.setStyleSheet("color:#c33;font-weight:bold")
            self.lb_output.setText(f"Output: {exc}")
        except Exception as exc:          # a frame that cannot be read, etc.
            self.lb_output.setStyleSheet("color:#c33;font-weight:bold")
            self.lb_output.setText(f"Output: could not read the frames ({exc})")
        finally:
            QApplication.restoreOverrideCursor()

    def source_changed(self):
        text = self.ed_source.text().strip()
        if not text:
            return
        try:
            groups = ms.group_frames(ms.list_sources(text))
        except SystemExit as exc:
            self.lb_frames.setText(str(exc))
            return
        n = sum(len(fs) for _, fs in groups)
        text = (f"{n} frames in {len(groups)} animation{'s' if len(groups) > 1 else ''} "
                "(one row each): " + " · ".join(f"{a} ({len(fs)})" for a, fs in groups[:12])
                + (" · …" if len(groups) > 12 else ""))
        note = ms.single_frame_note([(a, len(fs)) for a, fs in groups])
        self.lb_frames.setText(text + ("\n" + note if note else ""))

    def browse_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Folder of frames")
        if p:
            self.ed_source.setText(p)

    def browse_files(self):
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Frames", "", "Images (*.png *.bmp *.gif *.tga *.webp);;All files (*)")
        if ps:
            self.ed_source.setText(";".join(ps))

    def browse_pal(self):
        p, _ = QFileDialog.getOpenFileName(self, "Palette file", "",
                                           "Palettes (*.pal *.png);;All files (*)")
        if p:
            self.ed_pal.setText(p)

    def browse_run(self):
        p = QFileDialog.getExistingDirectory(self, "Sprite run folder")
        if p:
            self.ed_run.setText(p)

    def browse_res(self):
        p = QFileDialog.getExistingDirectory(self, "res folder", self.ed_res.text().strip())
        if p:
            self.ed_res.setText(p)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if len(paths) == 1 and os.path.isfile(os.path.join(paths[0], "sprite.json")):
            self.ed_run.setText(paths[0])
        else:
            self.ed_source.setText(";".join(paths))

    # ---------------- convert
    def args(self):
        src = self.ed_source.text().strip()
        diths = [d for d, cb in self.cb_dithers.items() if cb.isChecked()]
        strengths = [s for s, cb in self.cb_strengths.items() if cb.isChecked()]
        if not diths or not strengths:
            raise SystemExit("tick at least one dithering and one strength")
        out = self.ed_out.text().strip() or None
        if out is None:
            first = src.split(";")[0]
            parent = os.path.dirname(os.path.normpath(first))
            name = ms.md.c_name(self.ed_name.text().strip() or
                                ms.anim_name(ms.list_sources(src)[0]))
            out = os.path.join(parent, f"sprite_{name}")
        return argparse.Namespace(
            source=src, out=out, name=self.ed_name.text().strip() or None,
            dithering=",".join(diths), dither_strength=",".join(strengths),
            frames=self.cb_frames.currentData(), anchor=self.cb_anchor.currentData(),
            size=self.size_text(), fit=self.cb_fit.currentData(),
            transparency=self.cb_transp.currentData(),
            key_color=self.bt_key.property("hex") if self.cb_transp.currentData() == "key"
            else None,
            alpha_threshold=self.sp_alpha.value(), key_tolerance=self.sp_tol.value(),
            edge_clean=self.sp_edge.value(), scale=self.sp_scale.value() / 100.0,
            scale_method=self.cb_method.currentText(),
            gain="auto" if self.ck_gain.isChecked() else "none",
            brightness=float(self.sp_bright.value()),
            palette_file=self.ed_pal.text().strip() or None,
            lock=self.ed_lock.text().strip() or None, reserve_ops=self.ck_ops.isChecked(),
            color0=None, color_weight=self.sp_cweight.value() / 100.0,
            solid=self.sp_solid.value() / 100.0, grey=self.ck_grey.isChecked(),
            fps=float(self.sp_fps.value()), seed=0, export_all=False)

    def convert(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.ed_source.text().strip():
            QMessageBox.information(self, "Frames", "Drop a folder of frames first.")
            return
        try:
            a = self.args()
        except SystemExit as exc:
            QMessageBox.warning(self, "Sprite", str(exc))
            return
        self.log.clear()
        self.bt_run.setEnabled(False)

        def job():
            ms.cmd_run(a)
            return getattr(a, "actual_out", "")

        self.worker = _Worker(job)
        self.worker.log.connect(self._log)
        self.worker.done.connect(self._converted)
        self.worker.start()

    def _converted(self, ok, out):
        self.bt_run.setEnabled(True)
        if ok and out:
            self.run_dir = out
            self.bt_open.setEnabled(True)
            self.ed_run.setText(out)
            self._log(f"\nwritten to: {out}\n")
        else:
            self._log(f"\nconversion failed: {out}\n")

    def open_gallery(self):
        p = os.path.join(self.run_dir or self.ed_run.text().strip(), "index.html")
        if os.path.isfile(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    # ---------------- export
    def load_variants(self):
        self.cb_variant.clear()
        self.bt_export.setEnabled(False)
        folder = self.ed_run.text().strip()
        if not folder:
            return
        try:
            folder, meta = ms.load_run(folder)
        except SystemExit:
            return
        for v in meta["variants"]:
            self.cb_variant.addItem(
                f"{v['stem']}  ·  dE {v['de_mean']:.2f} · flicker {100 * v['flicker']:.1f}% · "
                f"tiles/frame {v['tiles_max']} · sprites/frame {v['sprites_max']}", v["stem"])
        self.run_dir = folder
        self.bt_open.setEnabled(True)
        self.bt_export.setEnabled(self.cb_variant.count() > 0)

    def export_one(self):
        folder = self.ed_run.text().strip()
        stem = self.cb_variant.currentData()
        if not folder or not stem:
            return
        name = self.ed_name.text().strip() or None
        res = self.ed_res.text().strip() or None
        try:
            out = ms.export_variant(folder, stem, name, res,
                                    log=lambda s: self._log(s + "\n"))
        except SystemExit as exc:
            QMessageBox.warning(self, "Export", str(exc))
            return
        self.export_dir = out
        self.bt_files.setEnabled(True)

    def open_files(self):
        if self.export_dir and os.path.isdir(self.export_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.export_dir))


def main():
    ap = argparse.ArgumentParser(description="sprite converter window")
    ap.add_argument("source", nargs="?", default="")
    a = ap.parse_args()
    app = QApplication([sys.argv[0]])
    w = SpriteWindow(a.source)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
