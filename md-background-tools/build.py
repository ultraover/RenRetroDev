# -*- coding: utf-8 -*-
"""Build dist/MD_Background_Tools.exe from src/ and write SHA256SUMS.txt.

    pip install -r requirements.txt pyinstaller
    python build.py
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
DIST = os.path.join(HERE, "dist")
NAME = "MD_Background_Tools"


def main():
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
           "--name", NAME, "--paths", SRC, "--distpath", DIST,
           "--workpath", os.path.join(HERE, "build"), "--specpath", os.path.join(HERE, "build")]
    for mod in ("md_color", "md_fundo_v2", "md_fundo_gui_v2", "md_fundo_sweep_v2",
                "sweep_gui_v2", "md_tile_editor", "md_sprite", "sprite_gui",
                "scipy.ndimage"):
        cmd += ["--hidden-import", mod]
    # big packages the tools never use: SciPy can reach for them when they are
    # installed, and PyInstaller would bundle them (PyTorch alone is ~390 MB)
    for mod in ("torch", "torchvision", "torchaudio", "torchgen", "functorch",
                "onnxruntime", "cv2", "tensorflow", "jax", "simple_lama_inpainting"):
        cmd += ["--exclude-module", mod]
    cmd.append(os.path.join(SRC, "md_tools.py"))
    subprocess.check_call(cmd)
    exe = os.path.join(DIST, NAME + (".exe" if os.name == "nt" else ""))
    h = hashlib.sha256(open(exe, "rb").read()).hexdigest()
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="ascii", newline="\n") as f:
        f.write(f"{h}  {os.path.basename(exe)}\n")
    print(f"\n{exe}\nsha256 {h}")


if __name__ == "__main__":
    main()
