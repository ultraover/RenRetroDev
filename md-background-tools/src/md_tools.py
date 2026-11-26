# -*- coding: utf-8 -*-
"""MD Background Tools: one start screen for the tools.

    python tools/md_tools.py            start screen
    python tools/md_tools.py --run X    run one tool directly
                                        (X = sweep-gui, sweep-console, sprite-gui,
                                         sprite-console, tile-editor;
                                         background-gui and background-console
                                         still run, but are off the start
                                         screen: the sweep does what they did)

Each choice starts the tool in its own process (its own window or console),
so the start screen stays open and several tools can run side by side. In
the frozen .exe the process re-runs this same .exe with --run; the console
tools get a console of their own (the .exe itself has none).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP_DIR = os.environ.get("MD_SWEEP_DIR", HERE)
for _d in (HERE, SWEEP_DIR):
    if _d and os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)

TITLE = "MD Background Tools"
VERSION = "2.1"

TOOLS = [
    ("sweep-gui", "Sweep · window",
     "Convert one image: try many combinations at once, compare them in one "
     "HTML page and export the one you pick (tiles, palettes, map, .res)."),
    ("sweep-console", "Sweep · console",
     "The sweep from the command line: every option as a flag, or drag an "
     "image into the window for the default grid."),
    ("sprite-gui", "Sprite · window",
     "Convert the frames of a character in one go: one shared palette, the "
     "dithers compared animated, and an SGDK sprite sheet (.png, .pal, .res)."),
    ("sprite-console", "Sprite · console",
     "The sprite converter from the command line, or drag a folder of frames "
     "into the window."),
    ("tile-editor", "Tile editor",
     "Open an export and rearrange its 8×8 tiles: copy, paste, delete, mirror "
     "and turn the priority of each tile on or off. See where a tile is used "
     "and edit its pixels, switch tile palettes and edit the palettes. Saving updates the image, the map and the preview "
     "for your project."),
]


# --------------------------------------------------------------------------
# console for the frozen windowed .exe
# --------------------------------------------------------------------------
def _attach_console():
    """Give this process a console of its own and route stdio to it."""
    if sys.platform != "win32":
        return
    import ctypes
    k = ctypes.windll.kernel32
    if not k.GetConsoleWindow():
        k.AllocConsole()
        k.SetConsoleTitleW(TITLE)
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        pass
    try:
        k.SetConsoleOutputCP(65001)
    except Exception:
        pass


def run_tool(name, extra):
    """Run one tool in THIS process."""
    if name in ("background-console", "sweep-console", "sprite-console"):
        _attach_console()
        os.environ["MD_TOOLS_NO_HANDOFF"] = "1"
        sys.argv = [sys.argv[0]] + list(extra)
        if name == "background-console":
            import md_fundo_v2 as tool
        elif name == "sprite-console":
            import md_sprite as tool
        else:
            import md_fundo_sweep_v2 as tool
        try:
            rc = tool.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 0
        return rc or 0
    sys.argv = [sys.argv[0]] + list(extra)
    if name == "background-gui":
        import md_fundo_gui_v2 as gui
        return gui.main()
    if name == "sweep-gui":
        import sweep_gui_v2 as gui
        return gui.main()
    if name == "sprite-gui":
        import sprite_gui as gui
        return gui.main()
    if name == "tile-editor":
        import md_tile_editor as gui
        return gui.main()
    raise SystemExit(f"unknown tool: {name}")


def launch(name):
    """Start one tool in a new process."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--run", name]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--run", name]
    flags = 0
    if sys.platform == "win32" and name.endswith("console") and not getattr(sys, "frozen", False):
        flags = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen(cmd, creationflags=flags, cwd=os.getcwd())


# --------------------------------------------------------------------------
# start screen
# --------------------------------------------------------------------------
STYLE = """
QWidget { background:#17191e; color:#d8dbe2; font-size:10pt; }
QLabel#title { font-size:20pt; font-weight:bold; color:#ffffff; }
QLabel#sub { color:#8a93a6; }
QPushButton { background:#22262f; border:1px solid #343a46; border-radius:8px;
              padding:14px; text-align:left; }
QPushButton:hover { background:#2b3140; border-color:#4b7fd6; }
QLabel#name { font-size:13pt; font-weight:bold; color:#7fb4ff; background:transparent; }
QLabel#desc { color:#b8bfcc; background:transparent; }
"""


def start_screen():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QApplication, QGridLayout, QLabel, QPushButton,
                                   QVBoxLayout, QWidget)

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    w = QWidget()
    w.setWindowTitle(TITLE)
    root = QVBoxLayout(w)
    root.setContentsMargins(24, 20, 24, 20)
    root.setSpacing(10)
    t = QLabel(TITLE)
    t.setObjectName("title")
    root.addWidget(t)
    s = QLabel(f"Mega Drive / Genesis background converter · {VERSION} · "
               "choose a tool (each one opens in its own window)")
    s.setObjectName("sub")
    s.setWordWrap(True)
    root.addWidget(s)
    grid = QGridLayout()
    grid.setSpacing(12)
    for i, (key, name, desc) in enumerate(TOOLS):
        b = QPushButton()
        b.setMinimumSize(330, 120)
        b.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(b)
        n = QLabel(name)
        n.setObjectName("name")
        d = QLabel(desc)
        d.setObjectName("desc")
        d.setWordWrap(True)
        for lb in (n, d):
            lb.setAttribute(Qt.WA_TransparentForMouseEvents)
            lay.addWidget(lb)
        lay.addStretch(1)
        b.clicked.connect(lambda _=False, k=key: launch(k))
        if i == len(TOOLS) - 1 and len(TOOLS) % 2:
            grid.addWidget(b, i // 2, 0, 1, 2)     # the odd last one spans both
        else:
            grid.addWidget(b, i // 2, i % 2)
    root.addLayout(grid)
    w.resize(720, 470)
    w.show()
    return app.exec()


def main():
    # the sweep runs its variants in worker processes; in the frozen .exe each
    # worker re-runs this .exe, and freeze_support hands it to multiprocessing
    import multiprocessing
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--run":
        return run_tool(args[1], args[2:])
    return start_screen()


if __name__ == "__main__":
    sys.exit(main())
