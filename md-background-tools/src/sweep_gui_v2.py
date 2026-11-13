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
from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QGridLayout, QGroupBox,
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget, QSpinBox,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import md_fundo_sweep_v2 as md  # noqa: E402

PALETTES = (1, 2, 3, 4)
MODES = ("normal", "shadow", "highlight", "both")
DITHERS = ("none", "ordered4", "ordered8", "floyd", "atkinson", "stucki",
           "halftone", "bluenoise", "riemersma")
STRENGTHS = ("clean", "balanced", "full")


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
        self.resize(980, 760)
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

        axes = QGridLayout()
        self.cb_palettes = self._group("Palettes", [str(p) for p in PALETTES],
                                       axes, 0, 0)
        self.cb_modes = self._group("Modes", MODES, axes, 0, 1)
        self.cb_dithers = self._group("Dithering", DITHERS, axes, 1, 0)
        self.cb_strengths = self._group("Dither strength", STRENGTHS, axes, 1, 1)
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
        root.addWidget(budget)

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
        root.addLayout(pl)

        self.ck_full = QCheckBox(
            "Save full-size PNGs (needed for scrollable full-size previews)")
        self.ck_full.setChecked(True)
        self.ck_full.stateChanged.connect(self.update_projection)
        root.addWidget(self.ck_full)
        self.ck_export = QCheckBox(
            "Export the palette (.pal), map (JSON), indexed PNG and .res of EVERY "
            "variant now (to export just one later, use the box at the bottom)")
        root.addWidget(self.ck_export)
        self.ck_clean = QCheckBox(
            "Shadow: clean the border of flat dark areas (one solid colour, no dotted halo)")
        self.ck_clean.setChecked(True)
        root.addWidget(self.ck_clean)
        self.ck_contour = QCheckBox(
            "Shadow: keep contours (doors, windows are not split between normal and "
            "shadow; contour before colour)")
        self.ck_contour.setChecked(True)
        root.addWidget(self.ck_contour)

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
        er.addWidget(self.ed_sweep, 1)
        bt_sw = QPushButton("Browse…")
        bt_sw.clicked.connect(self.browse_sweep)
        er.addWidget(bt_sw)
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

    def _group(self, title, values, layout, row, col):
        box = QGroupBox(title)
        inner = QVBoxLayout(box)
        cbs = {}
        for v in values:
            cb = QCheckBox(v)
            cb.setChecked(True)
            cb.stateChanged.connect(self.update_projection)
            inner.addWidget(cb)
            cbs[v] = cb
        layout.addWidget(box, row, col)
        return cbs

    def browse_source(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Source image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*)")
        if p:
            self.ed_source.setText(p)

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
        self.src_info = None
        if self.width and self.height:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                md.set_view_height(240 if self.cb_view.currentIndex() == 1 else 224)
                rgb, _ = md.pad8(md.load(p))
                self.src_info = md.preflight(rgb)
            except Exception as exc:
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
        modes = self.selected(self.cb_modes)
        diths = self.selected(self.cb_dithers)
        strengths = self.selected(self.cb_strengths)
        specs = 0
        for d in diths:
            specs += 1 if d == "none" else len(strengths)
        budgets = self.budget_count()
        samples = len(pals) * len(modes) * specs * budgets
        all_checked = (len(pals) == len(PALETTES) and len(modes) == len(MODES)
                       and len(diths) == len(DITHERS)
                       and len(strengths) == len(STRENGTHS))
        return pals, modes, diths, strengths, specs, budgets, samples, all_checked

    def update_projection(self):
        pals, modes, diths, strengths, specs, budgets, samples, all_checked = \
            self.projection()
        if not self.width or not self.height:
            self.lb_info.setText("Select an image to see the projected samples.")
            self.lb_warn.setText("")
            return
        pan = self.width > 320 or self.height > 224
        est = md.estimate_sweep_seconds(self.width, self.height, samples, getattr(self, 'src_info', None), self.budget_values(), len(pals) * (len(diths) if self.paldith() == 'same' else 1))
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
            f"columns × {budgets} budgets = {samples} samples · "
            f"rough estimate {est / 60.0:.1f} min · "
            f"full PNGs ~{full_mb:.1f} MB")
        msgs = []
        if all_checked:
            msgs.append(f"WARNING: every option is checked: {samples} samples "
                        f"(~{est / 60.0:.1f} min rough estimate, "
                        f"~{full_mb:.1f} MB of full PNGs).")
        if self.width > 512 or self.height > 512:
            msgs.append(f"WARNING: image is above 512x512; no resize and no split, "
                        f"the plane limit is ignored.")
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
        for text in (self.ed_t1.text().strip(), self.ed_t2.text().strip()):
            if text:
                try:
                    int(text)
                except ValueError:
                    QMessageBox.warning(self, "Max tiles",
                                        "The two max-tile fields must be integers or blank.")
                    return
        if all_checked:
            est = md.estimate_sweep_seconds(self.width, self.height, samples, getattr(self, 'src_info', None), self.budget_values(), len(pals) * (len(diths) if self.paldith() == 'same' else 1))
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
            cap_scope="auto", crop=None, full=self.ck_full.isChecked(), gain="auto",
            brightness=0.0, bg_color=None, no_pad=False, pad_color=None,
            backdrop="border", backdrop_color=None, seed=0,
            view_height=240 if self.cb_view.currentIndex() == 1 else 224,
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
        if ok and out:
            self.bt_open.setEnabled(True)
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

        def job():
            exp_dir, same = md.export_one(folder, stem)
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
        if self.export_dir and os.path.isdir(self.export_dir):
            res = os.path.join(self.export_dir, "res")
            QDesktopServices.openUrl(QUrl.fromLocalFile(res if os.path.isdir(res)
                                                        else self.export_dir))

    def open_gallery(self):
        if not self.actual_out:
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
