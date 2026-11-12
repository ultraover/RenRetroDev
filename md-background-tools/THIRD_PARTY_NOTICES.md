# Third-party software

The source code in `src/` is MIT-licensed (see `LICENSE`). The single-file
`MD_Background_Tools.exe` also bundles the following third-party components,
each under its own license. Their full license texts ship with each project and
can be read at the links below.

| Component | Version in the release | License | Project |
|---|---|---|---|
| Python | 3.12.10 | PSF License | https://www.python.org/ |
| Qt for Python: PySide6, Shiboken6 | 6.12.0 | LGPL-3.0 | https://www.qt.io/qt-for-python |
| Qt (bundled with PySide6) | 6.12 | LGPL-3.0 | https://www.qt.io/ |
| NumPy | 2.5.2 | BSD-3-Clause | https://numpy.org/ |
| SciPy | 1.18.1 | BSD-3-Clause | https://scipy.org/ |
| Pillow | 12.3.0 | MIT-CMU (HPND) | https://python-pillow.github.io/ |
| Numba | 0.68.1 | BSD-2-Clause | https://numba.pydata.org/ |
| llvmlite / LLVM | 0.50.0 | BSD-2-Clause / Apache-2.0 with LLVM exception | https://llvmlite.readthedocs.io/ |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the bootloader exception (it allows distributing programs built with it) | https://pyinstaller.org/ |

## Qt / PySide6 (LGPL-3.0)

The program uses Qt and PySide6 unmodified, under the GNU Lesser General Public
License v3. You can get their source code from https://download.qt.io/ (Qt) and
https://code.qt.io/cgit/pyside/pyside-setup.git/ (PySide6).

The LGPL lets you replace these libraries with your own build. Because the
`.exe` packs everything into one file, the easiest way to do that is to run
the program from the source code (`pip install -r requirements.txt`, then
`python src/md_tools.py`) with the PySide6/Qt of your choice, or to rebuild
the `.exe` with `python build.py`. Everything needed is in this repository.

The full LGPL-3.0 text: https://www.gnu.org/licenses/lgpl-3.0.html
