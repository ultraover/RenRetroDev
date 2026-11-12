"""Cores do Mega Drive: grade de 512 cores, conversão para Lab e encaixe na grade.

Níveis de saída do MD por canal (3 bits): 0, 52, 87, 116, 144, 172, 206, 255. São os que o
driver "genesis" do MAME 0.289 usa (conferido em 2026-10-13); BlastEm aprovado visualmente
pelo autor. Usado por md_palettes.py, md_reduce.py e md_scene_rom.py.
"""

import numpy as np

MD_LEVELS = np.array([0, 52, 87, 116, 144, 172, 206, 255], dtype=np.float64)


def rgb_to_lab(rgb):
    # sRGB is defined on 0..1. Clamping here is not cosmetic: np.where evaluates
    # both branches, so a negative channel would run ((c + 0.055) / 1.055) ** 2.4
    # on a negative base -> NaN + "invalid value encountered in power", even
    # though the other branch is the one selected.
    c = np.clip(np.asarray(rgb, dtype=np.float64) / 255.0, 0.0, 1.0)
    lin = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16,
                     500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


# índice da grade = r*64 + g*8 + b (r, g, b de 0 a 7)
MD_GRID_RGB = np.array([(r, g, b) for r in MD_LEVELS for g in MD_LEVELS for b in MD_LEVELS])
MD_GRID_LAB = rgb_to_lab(MD_GRID_RGB)


def snap_to_md(lab_points):
    """Índices da grade do MD mais próximos (em Lab) de cada ponto, sem repetição."""
    d = ((lab_points[:, None, :] - MD_GRID_LAB[None, :, :]) ** 2).sum(axis=2)
    return np.unique(d.argmin(axis=1))


_nearest_cache = {}


def nearest_md(rgb):
    """Índice da grade do MD mais próximo de uma cor RGB (com cache)."""
    rgb = tuple(int(c) for c in rgb)
    if rgb not in _nearest_cache:
        lab = rgb_to_lab(np.array(rgb, dtype=np.float64))
        _nearest_cache[rgb] = int(((MD_GRID_LAB - lab) ** 2).sum(axis=1).argmin())
    return _nearest_cache[rgb]
