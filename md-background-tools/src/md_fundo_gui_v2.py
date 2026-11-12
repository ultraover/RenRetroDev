# -*- coding: utf-8 -*-
"""Desktop GUI (PySide6) for md_fundo_v2.py (v2: corrected dithers,
reduction and metrics).

    python md_fundo_gui_v2.py [image]

Two windows:
  - Compare (main): big, pixel-exact views of the original and the four samples.
    Single view, side by side or 2x2 grid; zoom and pan are shared by all panes.
  - Options (tool window): source image, preflight, every conversion option,
    Generate, export and the log.

Calls md_fundo directly: preflight, cmd_sample (with the window's answers in
place of the terminal questions), cmd_choose and do_emit.

Keys (compare window):
  1..4  sample in the active pane    0  original in the active pane
  Space (hold) show the original in every pane (flip test)
  Tab   switch active pane           G  8x8 grid      F  fit
  Ctrl+O options window              wheel = zoom, drag = pan
"""
import argparse
import contextlib
import io
import json
import os
import sys
import traceback

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QTextCursor)
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QButtonGroup, QCheckBox, QColorDialog,
                               QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QToolBar,
                               QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import md_fundo_v2 as md  # noqa: E402

NAMES = ["Original", "1 · A fidelity", "2 · B worst case", "3 · C balance worst", "4 · D balance"]
SHORT = ["Original", "1 · A", "2 · B", "3 · C", "4 · D"]

STYLE = """
QWidget { background:#17191e; color:#d8dbe2; font-size:10pt; }
QToolBar { background:#1e2127; border:none; border-bottom:1px solid #2b2f38; spacing:6px; padding:4px; }
QToolBar QToolButton { padding:4px 9px; border-radius:4px; }
QToolBar QToolButton:checked { background:#2f6fd6; color:white; }
QToolBar QToolButton:hover { background:#2c313c; }
QGroupBox { border:1px solid #2b2f38; border-radius:6px; margin-top:14px; padding:8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin:margin; left:8px; color:#7fb4ff; font-weight:bold; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit {
  background:#22252d; border:1px solid #353a46; border-radius:4px; padding:3px 5px; }
QPushButton { background:#2a2f39; border:1px solid #3a404e; border-radius:5px; padding:6px 12px; }
QPushButton:hover { background:#343a47; }
QPushButton:disabled { color:#666; }
QPushButton#primary { background:#2f6fd6; border-color:#3d7fe8; color:white; font-weight:bold; }
QPushButton#sample { text-align:left; padding:6px 10px; }
QPushButton#sample:checked { background:#24406e; border-color:#3d7fe8; }
QLabel#muted { color:#8d94a1; font-size:9pt; }
QLabel#badge { background:rgba(10,11,14,190); color:#f0f2f6; padding:4px 8px;
  border-radius:4px; font-weight:bold; }
QLabel#badge[active="true"] { background:rgba(47,111,214,220); }
QScrollArea { border:none; }
QSplitter::handle { background:#2b2f38; }
"""


def qimg(arr):
    arr = np.ascontiguousarray(arr[..., :3].astype(np.uint8))
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888).copy()


# --------------------------------------------------------------------------
# background work
# --------------------------------------------------------------------------
class _Out(io.TextIOBase):
    def __init__(self, signal):
        self.signal = signal

    def write(self, s):
        if s:
            self.signal.emit(s)
        return len(s)


class Job(QObject):
    text = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        out = _Out(self.text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                r = self.fn()
            self.done.emit(r)
        except SystemExit as e:
            self.failed.emit(str(e.code))
        except BaseException:
            self.failed.emit(traceback.format_exc())


# --------------------------------------------------------------------------
# one comparison pane
# --------------------------------------------------------------------------
class Pane(QGraphicsView):
    hovered = Signal(int, int, object)
    activated = Signal(object)

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.FastTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QColor("#0e0f12"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setFocusPolicy(Qt.NoFocus)
        self.slot = 0          # 0 = original, 1..4 = sample
        self.arr = None
        self.tile_pal = None
        self._sync = False
        self.badge = QLabel(self.viewport())
        self.badge.setObjectName("badge")
        self.badge.move(10, 10)

    def show_array(self, arr, tile_pal, title):
        self.arr, self.tile_pal = arr, tile_pal
        if arr is None:
            self.item.setPixmap(QPixmap())
        else:
            self.item.setPixmap(QPixmap.fromImage(qimg(arr)))
            self.scene().setSceneRect(QRectF(0, 0, arr.shape[1], arr.shape[0]))
        self.badge.setText(title)
        self.badge.adjustSize()

    def set_active(self, on):
        self.badge.setProperty("active", "true" if on else "false")
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    def zoom(self):
        return self.transform().m11()

    def apply(self, z, h, v):
        self._sync = True
        self.resetTransform()
        self.scale(z, z)
        self.horizontalScrollBar().setValue(h)
        self.verticalScrollBar().setValue(v)
        self._sync = False
        self.viewport().update()

    def broadcast(self):
        if self._sync:
            return
        z, h, v = self.zoom(), self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        for o in self.hub.panes:
            if o is not self:
                o.apply(z, h, v)
        self.hub.zoom_changed(z)

    def wheelEvent(self, e):
        z = self.zoom()
        nz = max(0.25, min(32.0, z * 2 if e.angleDelta().y() > 0 else z / 2))
        self.scale(nz / z, nz / z)
        self.broadcast()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.broadcast()

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        p = self.mapToScene(e.position().toPoint())
        self.hovered.emit(int(p.x()), int(p.y()), self)

    def mousePressEvent(self, e):
        self.activated.emit(self)
        super().mousePressEvent(e)

    def drawForeground(self, painter, rect):
        if not self.hub.grid or self.arr is None or self.zoom() < 2:
            return
        h, w = self.arr.shape[:2]
        pen = QPen(QColor(255, 255, 255, 55))
        pen.setCosmetic(True)
        painter.setPen(pen)
        for x in range(0, w + 1, 8):
            painter.drawLine(QPointF(x, 0), QPointF(x, h))
        for y in range(0, h + 1, 8):
            painter.drawLine(QPointF(0, y), QPointF(w, y))


class PaletteStrip(QWidget):
    def __init__(self):
        super().__init__()
        self.pals = []
        self.setFixedHeight(30)

    def set(self, pals):
        self.pals = pals or []
        self.setFixedHeight(max(30, 22 * len(self.pals) + 6))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        s = 18
        for r, pal in enumerate(self.pals):
            y = 3 + r * 22
            p.setPen(QColor("#8d94a1"))
            p.drawText(0, y + 14, f"PAL{r}")
            for i, c in enumerate(pal):
                x = 40 + i * (s + 2)
                p.fillRect(x, y, s, s, QColor(*[int(v) for v in c[:3]]))
                p.setPen(QColor("#3d7fe8") if i == 0 else QColor("#000"))
                p.drawRect(x, y, s, s)
        p.end()


# --------------------------------------------------------------------------
# options window
# --------------------------------------------------------------------------
class OptionsWindow(QWidget):
    def __init__(self, main):
        super().__init__(main, Qt.Tool)
        self.main = main
        self.setWindowTitle("MD Background Tools · Background · Options")
        self.resize(400, 900)
        outer = QVBoxLayout(self)
        box = QWidget()
        col = QVBoxLayout(box)

        g = QGroupBox("Source")
        f = QVBoxLayout(g)
        row = QHBoxLayout()
        self.ed_source = QLineEdit()
        self.ed_source.setPlaceholderText("drop an image here")
        self.ed_source.setReadOnly(True)
        b = QPushButton("Browse…")
        b.clicked.connect(main.open_image)
        row.addWidget(self.ed_source)
        row.addWidget(b)
        f.addLayout(row)
        self.lb_pre = QLabel("—")
        self.lb_pre.setWordWrap(True)
        self.lb_pre.setObjectName("muted")
        f.addWidget(self.lb_pre)
        col.addWidget(g)

        g = QGroupBox("Palette")
        f = QFormLayout(g)
        self.cb_npal = QComboBox()
        self.cb_npal.addItems(["1", "2", "3", "4"])
        f.addRow("Palettes", self.cb_npal)
        self.cb_source = QComboBox()
        self.cb_source.addItems(["own", "from file"])
        self.cb_source.currentIndexChanged.connect(self._pal_source)
        f.addRow("Source", self.cb_source)
        self.ed_palfile = QLineEdit()
        self.ed_palfile.setPlaceholderText(".pal or .png")
        self.ed_palfile.setEnabled(False)
        f.addRow("File", self.ed_palfile)
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(md.MODES)
        f.addRow("Mode", self.cb_mode)
        self.cb_backdrop = QComboBox()
        self.cb_backdrop.addItems(["from border", "fixed color"])
        f.addRow("Backdrop (index 0)", self.cb_backdrop)
        self.bt_backdrop = self._color_button("101820")
        self.bt_backdrop.setEnabled(False)
        self.cb_backdrop.currentIndexChanged.connect(lambda i: self.bt_backdrop.setEnabled(i == 1))
        f.addRow("", self.bt_backdrop)
        col.addWidget(g)

        g = QGroupBox("Dithering")
        f = QFormLayout(g)
        self.cb_dith = QComboBox()
        self.cb_dith.addItems(md.DITHERS)
        f.addRow("Type", self.cb_dith)
        self.cb_dst = QComboBox()
        self.cb_dst.addItems(list(md.DITHER_STRENGTHS))
        self.cb_dst.setCurrentText(md.DITHER_DEFAULT)
        self.cb_dst.setEnabled(False)
        self.cb_dith.currentTextChanged.connect(lambda t: self.cb_dst.setEnabled(t != "none"))
        f.addRow("Strength", self.cb_dst)
        col.addWidget(g)

        g = QGroupBox("Image")
        f = QFormLayout(g)
        self.ck_scroll = QCheckBox("scrolling background")
        self.ck_scroll.toggled.connect(
            lambda s: self.cb_resize.setCurrentText("height" if s else "width"))
        f.addRow("Scroll", self.ck_scroll)
        self.cb_resize = QComboBox()
        self.cb_resize.addItems(["width", "height", "none"])
        f.addRow("Resize", self.cb_resize)
        self.cb_over = QComboBox()
        self.cb_over.addItems(["resize", "split", "none"])
        f.addRow("Over 512 px", self.cb_over)
        self.ck_gain = QCheckBox("auto")
        self.ck_gain.setChecked(True)
        self.sp_gain = QDoubleSpinBox()
        self.sp_gain.setRange(0.2, 4.0)
        self.sp_gain.setSingleStep(0.05)
        self.sp_gain.setValue(1.0)
        self.sp_gain.setEnabled(False)
        self.ck_gain.toggled.connect(lambda s: self.sp_gain.setEnabled(not s))
        h = QHBoxLayout()
        h.addWidget(self.ck_gain)
        h.addWidget(self.sp_gain)
        f.addRow("Gain", h)
        self.sp_bright = QDoubleSpinBox()
        self.sp_bright.setRange(-100, 100)
        f.addRow("Brightness", self.sp_bright)
        self.ck_pad = QCheckBox("pad to multiples of 8")
        self.ck_pad.setChecked(True)
        f.addRow("", self.ck_pad)
        self.bt_pad = self._color_button("FFFFFF")
        f.addRow("Pad color", self.bt_pad)
        self.bt_bg = self._color_button("FFFFFF")
        f.addRow("Behind alpha", self.bt_bg)
        col.addWidget(g)

        g = QGroupBox("Tiles")
        f = QFormLayout(g)
        self.sp_max = QSpinBox()
        self.sp_max.setRange(0, 4096)
        self.sp_max.setSpecialValueText("no limit")
        f.addRow("Max unique tiles", self.sp_max)
        self.cb_scope = QComboBox()
        self.cb_scope.addItems(["whole image", "each screen view"])
        f.addRow("Cap applies to", self.cb_scope)
        self.cb_view = QComboBox()
        self.cb_view.addItems(["320×224 (NTSC)", "320×240 (PAL)"])
        f.addRow("Screen view", self.cb_view)
        self.sp_seed = QSpinBox()
        self.sp_seed.setRange(0, 9999)
        f.addRow("Seed", self.sp_seed)
        self.ed_out = QLineEdit()
        f.addRow("Samples folder", self.ed_out)
        col.addWidget(g)

        self.bt_generate = QPushButton("Generate samples")
        self.bt_generate.setObjectName("primary")
        self.bt_generate.setEnabled(False)
        self.bt_generate.clicked.connect(main.generate)
        col.addWidget(self.bt_generate)

        g = QGroupBox("Export")
        f = QFormLayout(g)
        self.ed_name = QLineEdit("parede")
        f.addRow("Name", self.ed_name)
        self.ed_res = QLineEdit(os.path.join(md.ROOT, "res"))
        f.addRow("res/ folder", self.ed_res)
        self.ck_profile = QCheckBox("remember this choice (profile.json)")
        self.ck_profile.setChecked(True)
        f.addRow("", self.ck_profile)
        col.addWidget(g)

        g = QGroupBox("Log")
        f = QVBoxLayout(g)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        self.log.setStyleSheet("font-family:Consolas; font-size:9pt;")
        f.addWidget(self.log)
        col.addWidget(g, 1)

        scroll = QScrollArea()
        scroll.setWidget(box)
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

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

    def _pal_source(self, i):
        self.ed_palfile.setEnabled(i == 1)
        if i == 1 and not self.ed_palfile.text():
            p, _ = QFileDialog.getOpenFileName(self, "Palette", "", "Palette (*.pal *.png)")
            if p:
                self.ed_palfile.setText(p)
            else:
                self.cb_source.setCurrentIndex(0)

    def view_height(self):
        return 240 if self.cb_view.currentIndex() == 1 else 224

    def answers(self):
        mx = self.sp_max.value()
        fixed = self.cb_backdrop.currentIndex() == 1
        return {
            "palettes": int(self.cb_npal.currentText()),
            "palette_file": self.ed_palfile.text() if self.cb_source.currentIndex() == 1 else None,
            "mode": self.cb_mode.currentText(),
            "transparency": True,
            "dithering": self.cb_dith.currentText(),
            "dither_strength": self.cb_dst.currentText(),
            "scroll": self.ck_scroll.isChecked(),
            "resize": self.cb_resize.currentText(),
            "max_tiles": None if mx == 0 else max(8, mx),
            "cap_scope": "view" if self.cb_scope.currentIndex() == 1 else "image",
            "backdrop": "color" if fixed else "border",
            "backdrop_rgb": self.bt_backdrop.property("hex") if fixed else None,
        }

    def dragEnterEvent(self, e):
        self.main.dragEnterEvent(e)

    def dropEvent(self, e):
        self.main.dropEvent(e)


# --------------------------------------------------------------------------
# compare window (main)
# --------------------------------------------------------------------------
class CompareWindow(QMainWindow):
    def __init__(self, initial=None):
        super().__init__()
        self.setWindowTitle("MD Background Tools · Background · Compare")
        self.resize(1600, 960)
        self.setAcceptDrops(True)
        self.source = None
        self.folder = None
        self.data = None
        self.images = [None] * 5       # 0 = original, 1..4 = samples
        self.tile_pals = [None] * 5
        self.grid = False
        self.flip = False
        self.thread = None
        self.layout_mode = "side"

        self.opts = OptionsWindow(self)
        self.opts.setAcceptDrops(True)

        self.panes = [Pane(self) for _ in range(4)]
        for p in self.panes:
            p.hovered.connect(self.on_hover)
            p.activated.connect(self.set_active)
        self.panes[0].slot, self.panes[1].slot = 0, 4
        self.panes[2].slot, self.panes[3].slot = 1, 3
        self.active = self.panes[1]

        self.area = QWidget()
        self.grid_lay = QGridLayout(self.area)
        self.grid_lay.setContentsMargins(0, 0, 0, 0)
        self.grid_lay.setSpacing(2)

        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(8, 6, 8, 6)
        self.sample_group = QButtonGroup(self)
        self.sample_buttons = []
        for i in range(5):
            b = QPushButton(SHORT[i])
            b.setObjectName("sample")
            b.setCheckable(True)
            b.setMinimumWidth(120)
            b.clicked.connect(lambda _=False, n=i: self.put(n))
            self.sample_group.addButton(b, i)
            self.sample_buttons.append(b)
            bl.addWidget(b)
        bl.addSpacing(12)
        self.palette = PaletteStrip()
        self.palette.setMinimumWidth(40 + 16 * 20)
        bl.addWidget(self.palette, 1)
        self.bt_emit = QPushButton("Export active sample")
        self.bt_emit.setObjectName("primary")
        self.bt_emit.setEnabled(False)
        self.bt_emit.clicked.connect(self.emit_sample)
        bl.addWidget(self.bt_emit)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.area)
        split.addWidget(bottom)
        split.setStretchFactor(0, 1)
        split.setCollapsible(0, False)
        self.setCentralWidget(split)

        self._toolbar()
        self.statusBar().showMessage("Drop an image anywhere, or open Options (Ctrl+O).")
        self.apply_layout("side")
        self._route_drops_to_window()
        if initial:
            self.load_image(initial)

    # ---------------- toolbar
    def _toolbar(self):
        tb = QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addAction(QAction("Options", self, shortcut="Ctrl+O", triggered=self.show_options))
        tb.addAction(QAction("Open image", self, triggered=self.open_image))
        tb.addAction(QAction("Open samples", self, triggered=self.open_samples))
        tb.addSeparator()
        grp = QActionGroup(self)
        for key, label, sc in (("single", "Single", "Ctrl+1"), ("side", "Side by side", "Ctrl+2"),
                               ("grid", "Grid 2×2", "Ctrl+3")):
            a = QAction(label, self, checkable=True, shortcut=sc,
                        triggered=lambda _=False, k=key: self.apply_layout(k))
            a.setChecked(key == "side")
            grp.addAction(a)
            tb.addAction(a)
        tb.addSeparator()
        self.act_grid = QAction("8×8 grid", self, checkable=True, shortcut="G",
                                triggered=self.toggle_grid)
        tb.addAction(self.act_grid)
        tb.addAction(QAction("Fit", self, shortcut="F", triggered=self.fit))
        for z in (1, 2, 4, 8):
            tb.addAction(QAction(f"{z}×", self, triggered=lambda _=False, z=z: self.set_zoom(z)))
        self.lb_zoom = QLabel("")
        self.lb_zoom.setObjectName("muted")
        tb.addWidget(self.lb_zoom)
        tb.addSeparator()
        self.cb_seg = QComboBox()
        self.cb_seg.currentIndexChanged.connect(self._segment)
        self.act_seg = tb.addWidget(self.cb_seg)
        self.act_seg.setVisible(False)
        for n in range(5):
            self.addAction(QAction(self, shortcut=str(n), triggered=lambda _=False, n=n: self.put(n)))
        self.addAction(QAction(self, shortcut="Tab", triggered=self.next_pane))

    def show_options(self):
        if not self.opts.isVisible():
            g = self.geometry()
            self.opts.move(max(0, g.x() - self.opts.width() - 8), g.y())
        self.opts.show()
        self.opts.raise_()
        self.opts.activateWindow()

    # ---------------- layout
    def visible_panes(self):
        return {"single": 1, "side": 2, "grid": 4}[self.layout_mode]

    def apply_layout(self, mode):
        self.layout_mode = mode
        for p in self.panes:
            self.grid_lay.removeWidget(p)
            p.hide()
        pos = {"single": [(0, 0)], "side": [(0, 0), (0, 1)],
               "grid": [(0, 0), (0, 1), (1, 0), (1, 1)]}[mode]
        for p, (r, c) in zip(self.panes, pos):
            self.grid_lay.addWidget(p, r, c)
            p.show()
        if self.active not in self.panes[:len(pos)]:
            self.active = self.panes[len(pos) - 1]
        if mode == "single":
            self.active = self.panes[0]
        self.refresh()
        self.fit()

    def set_active(self, pane):
        self.active = pane
        self.refresh_badges()
        self.update_bottom()

    def next_pane(self):
        vis = self.panes[:self.visible_panes()]
        self.set_active(vis[(vis.index(self.active) + 1) % len(vis)])

    def put(self, n):
        """Show slot n (0 = original) in the active pane."""
        if self.images[n] is None:
            return
        self.active.slot = n
        self.refresh()

    # ---------------- drawing
    def title_of(self, n):
        t = NAMES[n]
        if n and self.data:
            s = self.data["samples"][n - 1]
            t += f"   {s['tiles']} tiles · ΔE {s['de_mean']:.2f}"
        return t

    def refresh(self):
        for p in self.panes[:self.visible_panes()]:
            n = 0 if self.flip else p.slot
            if self.images[n] is None:
                n = 0
            label = self.title_of(n) + ("   [original — release Space]" if self.flip else "")
            p.show_array(self.images[n], self.tile_pals[n], label)
        self.refresh_badges()
        self.update_bottom()

    def refresh_badges(self):
        for p in self.panes:
            p.set_active(p is self.active and self.visible_panes() > 1)

    def update_bottom(self):
        n = self.active.slot
        for i, b in enumerate(self.sample_buttons):
            b.setEnabled(self.images[i] is not None)
            if i and self.data:
                s = self.data["samples"][i - 1]
                b.setText(f"{SHORT[i]}   {s['tiles']} tiles · ΔE {s['de_mean']:.2f}")
            else:
                b.setText(SHORT[i])
        self.sample_buttons[n].setChecked(True)
        if n and self.data:
            self.palette.set(self.data["samples"][n - 1]["palettes"])
            self.bt_emit.setEnabled(True)
            self.bt_emit.setText(f"Export {SHORT[n]}")
        else:
            self.palette.set([])
            self.bt_emit.setEnabled(False)
            self.bt_emit.setText("Export active sample")

    def toggle_grid(self, on):
        self.grid = on
        for p in self.panes:
            p.viewport().update()

    def fit(self):
        p = self.panes[0]
        if p.arr is None:
            return
        p.fitInView(p.scene().sceneRect(), Qt.KeepAspectRatio)
        z = p.zoom()
        z = 2 ** int(np.floor(np.log2(z))) if z >= 1 else z
        self.set_zoom(z)

    def set_zoom(self, z):
        p = self.panes[0]
        p.apply(z, p.horizontalScrollBar().value(), p.verticalScrollBar().value())
        p.broadcast()
        self.zoom_changed(z)

    def zoom_changed(self, z):
        self.lb_zoom.setText(f"  zoom {z:g}×")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self.flip = True
            self.refresh()
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self.flip = False
            self.refresh()
        else:
            super().keyReleaseEvent(e)

    def on_hover(self, x, y, pane):
        a = pane.arr
        if a is None or not (0 <= x < a.shape[1] and 0 <= y < a.shape[0]):
            return
        c = a[y, x]
        txt = f"x {x}  y {y}   ·   tile ({x // 8}, {y // 8})   ·   #{c[0]:02X}{c[1]:02X}{c[2]:02X}"
        tp = pane.tile_pal
        if tp is not None and tp.ndim == 2 and y // 8 < tp.shape[0] and x // 8 < tp.shape[1]:
            txt += f"   ·   PAL{int(tp[y // 8, x // 8])}"
        o = self.images[0]
        if o is not None and o.shape == a.shape and pane.slot:
            oc = o[y, x]
            txt += f"   ·   original #{oc[0]:02X}{oc[1]:02X}{oc[2]:02X}"
        self.statusBar().showMessage(txt)

    # ---------------- files
    def _route_drops_to_window(self):
        """Make file drops land on the window instead of inside a child.

        QGraphicsView (the panes), QLineEdit and QPlainTextEdit accept drops by
        default. Qt stops at the first child that accepts them, so a drop over a
        pane or a text field never reached CompareWindow.dropEvent().
        """
        seen = set()
        for root in (self, self.opts):
            for w in [root] + root.findChildren(QWidget):
                if w is not root:
                    if w.isWindow() or id(w) in seen:
                        continue
                    seen.add(id(w))
                    w.setAcceptDrops(False)
                if isinstance(w, QAbstractScrollArea):
                    v = w.viewport()
                    if v is not None:
                        v.setAcceptDrops(False)

    def _dropped_path(self, mime):
        if not mime.hasUrls():
            return ""
        for url in mime.urls():
            p = url.toLocalFile()
            if p and os.path.exists(p):
                return p
        return ""

    def dragEnterEvent(self, e):
        if self._dropped_path(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._dropped_path(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        p = self._dropped_path(e.mimeData())
        if not p:
            e.ignore()
            return
        if os.path.isdir(p) or p.endswith("samples.json"):
            self.load_samples(p)
        else:
            self.load_image(p)
        e.acceptProposedAction()

    def open_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "Image", "",
                                           "Images (*.png *.bmp *.gif *.jpg *.jpeg *.webp)")
        if p:
            self.load_image(p)

    def open_samples(self):
        p = QFileDialog.getExistingDirectory(self, "Folder with samples.json")
        if p:
            self.load_samples(p)

    def load_image(self, path):
        o = self.opts
        try:
            rgb = md.load(path, md.parse_fill(o.bt_bg.property("hex")))
        except Exception as ex:
            QMessageBox.warning(self, "Image", str(ex))
            return
        self.source = path
        self.data = None
        o.ed_source.setText(path)
        base = os.path.splitext(os.path.basename(path))[0]
        o.ed_out.setText(os.path.join(os.path.dirname(path), base))
        o.ed_name.setText(md.c_name(base))
        o.bt_generate.setEnabled(True)
        self.images = [rgb, None, None, None, None]
        self.tile_pals = [None] * 5
        self.setWindowTitle(f"MD Background Tools · Background · {os.path.basename(path)}")
        o.lb_pre.setText("measuring colors and tiles…")
        self.refresh()
        self.fit()
        self.show_options()
        vh = o.view_height()

        def pre():
            md.set_view_height(vh)
            return md.preflight(rgb)

        self.run(pre, self._preflight_done)

    def _preflight_done(self, i):
        self.opts.lb_pre.setText(
            f"<b>{i['w']}×{i['h']} px</b><br>"
            f"Colors: {i['cores']} in the file · <b>{i['cores_md']}</b> on the MD grid<br>"
            f"Tiles: {i['tiles']} slots · <b>{i.get('tiles_unicos', '?')}</b> unique in the image<br>"
            f"Worst {md.view_label()} view: <b>{i['pior_tela']}</b> unique<br>"
            f"<span style='color:#666'>peak at x={i['pior_x0'] * 8}..{i['pior_x1'] * 8} "
            f"y={i['pior_y0'] * 8}..{i['pior_y1'] * 8}</span>")

    def load_samples(self, folder):
        if folder.endswith("samples.json"):
            folder = os.path.dirname(folder)
        split = os.path.join(folder, "split.json")
        self.cb_seg.blockSignals(True)
        self.cb_seg.clear()
        if os.path.exists(split):
            for s in json.load(open(split, encoding="utf-8"))["segments"]:
                self.cb_seg.addItem(f"segment {s['name']} at ({s['x']},{s['y']})", s["folder"])
            self.act_seg.setVisible(True)
            self.cb_seg.blockSignals(False)
            self._segment(0)
            return
        self.act_seg.setVisible(False)
        self.cb_seg.blockSignals(False)
        self._show_folder(folder)

    def _segment(self, i):
        if i >= 0:
            self._show_folder(self.cb_seg.itemData(i), segment=True)

    def _show_folder(self, folder, segment=False):
        p = os.path.join(folder, "samples.json")
        if not os.path.exists(p):
            QMessageBox.warning(self, "Samples", f"No samples.json in\n{folder}")
            return
        self.folder = folder
        self.data = json.load(open(p, encoding="utf-8"))
        for i, s in enumerate(self.data["samples"], 1):
            self.images[i] = np.array(md.Image.open(os.path.join(folder, s["image"])).convert("RGB"))
            self.tile_pals[i] = np.array(s["tile_pal"])
        ref = self.images[1]
        # the original pane must match the samples pixel for pixel: rebuild it
        # the way the tool prepared it (resize + pad; segments are cropped)
        try:
            src = md.load(self.data["source"], md.parse_fill(self.opts.bt_bg.property("hex")))
            ans = self.data.get("answers", {})
            if ans.get("resize") in ("width", "height"):
                src = md.resize_to(src, ans["resize"])
            if segment:
                seg = next(s for s in json.load(open(os.path.join(os.path.dirname(folder),
                                                                   "split.json"),
                                                      encoding="utf-8"))["segments"]
                           if os.path.normpath(s["folder"]) == os.path.normpath(folder))
                full = np.zeros((max(src.shape[0], seg["y"] + seg["height"]),
                                 max(src.shape[1], seg["x"] + seg["width"]), 3), np.uint8)
                full[:src.shape[0], :src.shape[1]] = src
                src = full[seg["y"]:seg["y"] + seg["height"], seg["x"]:seg["x"] + seg["width"]]
            if src.shape[:2] != ref.shape[:2]:
                canvas = np.full(ref.shape, 255, np.uint8)
                h, w = min(src.shape[0], ref.shape[0]), min(src.shape[1], ref.shape[1])
                canvas[:h, :w] = src[:h, :w]
                src = canvas
            self.images[0] = src
            if self.source is None:
                self.source = self.data["source"]
                self.opts.ed_source.setText(self.source)
                self.opts.bt_generate.setEnabled(True)
        except Exception:
            pass
        self.refresh()
        self.fit()

    # ---------------- generate / export
    def generate(self):
        if not self.source:
            return
        o = self.opts
        resp = o.answers()
        if resp["palette_file"] and not os.path.exists(resp["palette_file"]):
            QMessageBox.warning(self, "Palette", "Palette file not found.")
            return
        out = o.ed_out.text().strip() or os.path.splitext(self.source)[0]
        a = argparse.Namespace(
            cmd="sample", source=self.source, out=out, seed=o.sp_seed.value(), no_ask=True,
            gain="auto" if o.ck_gain.isChecked() else str(o.sp_gain.value()),
            brightness=o.sp_bright.value(), no_pad=not o.ck_pad.isChecked(),
            pad_color=o.bt_pad.property("hex"), bg_color=o.bt_bg.property("hex"),
            oversize=o.cb_over.currentText(), samples=None, name=None, res=None, n=None,
            view_height=o.view_height())

        def work():
            old = md.ask
            md.ask = lambda args, profile: dict(resp)
            try:
                md.cmd_sample(a)
            finally:
                md.ask = old
            return getattr(a, "actual_out", out)

        o.log.clear()
        o.bt_generate.setText("Generating…")
        self.run(work, self._generated)

    def _generated(self, out):
        self.opts.ed_out.setText(out)
        self.load_samples(out)
        self.raise_()
        self.activateWindow()

    def emit_sample(self):
        n = self.active.slot
        if not self.data or not n:
            return
        o = self.opts
        name = md.c_name(o.ed_name.text().strip() or "parede")
        res = os.path.join(o.ed_res.text().strip(), f"{name}.res")
        folder, resp, remember = self.folder, dict(self.data["answers"]), o.ck_profile.isChecked()

        def work():
            if remember:
                md.cmd_choose(argparse.Namespace(choice=str(n), samples=folder))
            md.do_emit(os.path.join(folder, "samples.json"), name, res, resp, str(n))
            return res

        self.run(work, lambda r: QMessageBox.information(
            self, "Exported", f"Sample {SHORT[n]} exported:\n{r}"))

    def run(self, fn, ok):
        if self.thread and self.thread.isRunning():
            self.statusBar().showMessage("Busy: wait for the current job.")
            return
        self.opts.bt_generate.setEnabled(False)
        self.bt_emit.setEnabled(False)
        self.thread = QThread()
        self.job = Job(fn)
        self.job.moveToThread(self.thread)
        self.thread.started.connect(self.job.run)
        self.job.text.connect(self._log)
        self.job.done.connect(ok)
        self.job.failed.connect(self._failed)
        for s in (self.job.done, self.job.failed):
            s.connect(self.thread.quit)
        self.thread.finished.connect(self._idle)
        self.thread.start()

    def _idle(self):
        self.opts.bt_generate.setText("Generate samples")
        self.opts.bt_generate.setEnabled(bool(self.source))
        self.update_bottom()

    def _log(self, s):
        log = self.opts.log
        log.moveCursor(QTextCursor.MoveOperation.End)
        log.insertPlainText(s)
        last = s.strip().splitlines()
        if last:
            self.statusBar().showMessage(last[-1][:180])

    def _failed(self, msg):
        self.opts.log.appendPlainText("\n--- failed ---\n" + msg)
        self.show_options()
        QMessageBox.warning(self, "md_fundo", msg.strip().splitlines()[-1] if msg.strip() else "error")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    w = CompareWindow(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    if not w.source:
        w.show_options()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
