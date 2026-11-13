# -*- coding: utf-8 -*-
"""md_fundo sweep, v2: comparison grid on top of md_fundo_v2.

v1 (md_fundo_sweep.py) carried its own 3,500-line copy of the tool, so every
fix had to be made twice and the copy had already drifted (older make_samples
and cmd_sample). This file keeps ONLY the sweep: everything else is imported
from md_fundo_v2.py.

md_fundo_v2.py is looked up, in order: the folder in MD_FUNDO_DIR, this
folder.
"""
import argparse
import contextlib
import csv
import io
import json
import html
import os
import subprocess
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
try:  # frozen .exe: md_fundo_v2 is bundled, nothing to look up
    import md_fundo_v2 as _probe  # noqa: F401
    _FOUND = True
except ImportError:
    _FOUND = False
for _d in ([] if _FOUND else (os.environ.get("MD_FUNDO_DIR"), _HERE)):
    if _d and os.path.isfile(os.path.join(_d, "md_fundo_v2.py")):
        sys.path.insert(0, _d)
        break
else:
    if not _FOUND:
        raise SystemExit("md_fundo_v2.py not found: set MD_FUNDO_DIR to its folder")

from md_fundo_v2 import *  # noqa: E402,F401,F403
from md_fundo_v2 import _grava_log, _interativo, _pausa  # noqa: E402


def _csv_list(text, allowed, name):
    """Parse \"a,b,c\" (or a single value) and validate against allowed."""
    vals = [v.strip() for v in str(text).split(",") if v.strip()]
    if not vals:
        raise SystemExit(f"{name}: empty list")
    out = []
    for v in vals:
        if allowed is not None and v not in allowed:
            raise SystemExit(f"{name}: {v!r} is not one of {allowed}")
        if v not in out:
            out.append(v)
    return out


def _parse_budgets(text, auto_max):
    """One or two per-view tile budgets.

    Blank -> [auto_max, 75% of auto_max]. One value -> [value, 75% of auto_max].
    Values below 8 are raised to 8; duplicates are removed.
    """
    vals = []
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part or part.lower() in ("auto", "none", "-"):
            continue
        try:
            v = int(part)
        except ValueError:
            raise SystemExit(f"--max-tiles: {part!r} is not an integer")
        if v > 0:
            vals.append(max(8, v))
    auto_max = max(8, int(auto_max))
    auto_75 = max(8, int(round(auto_max * 0.75)))
    if not vals:
        vals = [auto_max, auto_75]
    elif len(vals) == 1 and vals[0] != auto_75:
        vals.append(auto_75)
    out = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out[:2], auto_max, auto_75


def _parse_crop(text):
    if not text:
        return None
    p = [int(v) for v in text.replace(" ", "").split(",")]
    if len(p) != 4:
        raise SystemExit("--crop must be x,y,w,h")
    return tuple(p)


def estimate_sweep_seconds(width, height, samples, info=None, budgets=None,
                           n_palettes=1):
    """Rough estimate measured on v2.3 (numba, per-view reduction).

    - palette optimization, cached once per palette count: ~10 s per MP;
    - dither + paint + metrics, every sample: ~2 s per MP;
    - tile reduction, only for samples whose budget is below the worst view
      of the source: ~3e-5 s x unique source tiles x tiles to cut.
    Without the source analysis (info) it falls back to the paint cost only.
    """
    mp = float(width) * float(height) / 1_000_000.0
    t = 10.0 * mp * max(1, int(n_palettes)) + 2.0 * mp * float(samples)
    if info and budgets:
        peak = float(info.get("pior_tela", 0))
        uniq = float(info.get("tiles_unicos", info.get("tiles", 0)))
        per_budget = float(samples) / len(budgets)
        for b in budgets:
            cut = max(0.0, peak - float(b))
            t += per_budget * 3e-5 * uniq * cut
    return t


def shadow_hl_html(v):
    """Shadow tiles and the (not exported) highlight estimate of one cell."""
    out = ""
    if v.get("export"):
        out += (f'<br><a href="{html.escape(v["export"])}" target="_blank" '
                f'rel="noopener">files (.res, .pal, map)</a>')
    if v.get("shadow_tiles"):
        out += f'<br>shadow {v["shadow_tiles"]} tiles'
    hs = v.get("hl_sprites")
    if hs is not None:
        if not hs["sprites"]:
            out += "<br>highlight: no gain"
        else:
            ok = "fits" if hs["fits"] else "<b>does not fit</b>"
            link = (f' <a href="{html.escape(v["hl_thumb"])}" target="_blank" '
                    f'rel="noopener">preview</a>' if v.get("hl_thumb") else "")
            out += (f'<br>highlight (not exported): dE {v["de_hl"]:.2f}{link}<br>'
                    f'{hs["sprites"]} sprites · view {hs["view_max"]}/80 · '
                    f'line {hs["line_max"]}/20, {hs["line_px_max"]}/320 px · {ok}')
    return out


def _write_sweep_html(out, meta, rgb):
    """Static gallery: one table per tile budget, palettes/modes x dither."""
    specs = meta.get("dither_specs")
    if not specs:
        specs = [{"dither": d, "strength": meta.get("strength", "full"),
                  "column": d} for d in meta["dithering"]]
    columns = [s["column"] for s in specs]
    mt = meta.get("max_tiles")
    if meta.get("budgets"):
        budgets = meta["budgets"]
    elif isinstance(mt, (list, tuple)):
        budgets = list(mt)
    elif mt:
        budgets = [mt]
    else:
        budgets = [None]
    for v in meta["variants"]:
        v.setdefault("column", v.get("dithering"))
        mt0 = (mt[0] if isinstance(mt, (list, tuple)) and mt else
               (mt if mt else None))
        v.setdefault("max_tiles", mt0)
    pan = meta.get("pan", meta.get("width", 0) > 320 or meta.get("height", 0) > 224)
    modes = meta["modes"]
    palettes = meta["palettes"]
    crop = meta["crop"]
    cap_scope = meta.get("cap_scope", "view")
    unit = "total" if cap_scope == "image" else "peak"
    tables = []
    for vq in budgets:
        by_key = {(v["palettes"], v.get("max_tiles"), v["column"], v["mode"]): v
                  for v in meta["variants"] if v.get("max_tiles") == vq}
        rows = []
        for n in palettes:
            for mi, mode in enumerate(modes):
                cells = []
                for col in columns:
                    v = by_key[(n, vq, col, mode)]
                    href = v["full"] or v["thumb"]
                    src = href
                    cap = " CAP!" if not v["cap_ok"] else ""
                    perc = v.get("de_perceived", v["de_mean"])
                    kb = v.get("kb_total")
                    if kb is None:
                        kb = (v["bytes_tiles"] + v["bytes_map"]
                              + 32 * v["palettes"]) / 1024.0
                    cells.append(
                        f'<td><div class="view"><a href="{html.escape(href)}" '
                        f'target="_blank" rel="noopener">'
                        f'<img src="{html.escape(src)}" alt="{html.escape(col)} '
                        f'{html.escape(mode)}" loading="lazy" draggable="false" '
                        f'style="cursor:zoom-in"></a></div>'
                        f'<div class="m"><b>{v["tiles"]}</b> tiles · '
                        f'<b>{kb:.1f} KB</b> total<br>'
                        f'dE mean {v["de_mean"]:.2f} · <b>perc {perc:.2f}</b>'
                        f' · p95 {v["de_p95"]:.1f}<br>'
                        f'{unit} {v["peak"]}{cap}'
                        f'{shadow_hl_html(v)}'
                        f'<br><span class="nm">{html.escape(variant_stem(v))}</span>'
                        f'</div></td>')
                label = (f'<th class="pal" rowspan="{len(modes)}">{n} palette'
                         f'{"s" if n > 1 else ""}</th>' if mi == 0 else "")
                rows.append(f'<tr>{label}<th class="mode">{html.escape(mode)}</th>'
                            + "".join(cells) + "</tr>")
        header = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
        unit_label = ("max tiles total" if cap_scope == "image"
                      else "max tiles/view")
        tables.append(
            f"<h2>{unit_label}: {vq if vq is not None else 'no cap'}</h2>\n"
            f"<table><tr><th>palettes</th><th>mode</th>{header}</tr>\n"
            + "".join(rows) + "\n</table>")
    css = """
    body{background:#16161a;color:#ddd;font:13px/1.4 system-ui,sans-serif;margin:16px}
    h1{font-size:18px} h2{font-size:15px;margin-top:24px}
    table{border-collapse:collapse;margin:8px 0 18px}
    th,td{border:1px solid #333;padding:4px;vertical-align:top;text-align:center}
    th{background:#222;position:sticky;top:0;z-index:2}
    th.pal{background:#2a2a33;min-width:52px}
    th.mode{background:#202028;min-width:70px}
    img{image-rendering:pixelated;display:block}
    .view{width:320px;height:240px;overflow:auto;border:1px solid #333;
          background:#000;display:block}
    .view img{max-width:none;max-height:none;width:auto;height:auto}
    .m{font-size:10px;color:#9ab;white-space:nowrap;line-height:1.45}
    .warn{color:#f88}
    .nm{color:#678;user-select:all}
    details{margin-top:18px} summary{cursor:pointer;color:#8cf}
    """
    summary = (f"{meta['width']}x{meta['height']} px · {len(meta['variants'])} "
               f"variants · {meta['seconds']}s · crop x={crop[0]} y={crop[1]} "
               f"{crop[2]}x{crop[3]} · "
               f"{'pan background' if pan else 'static image'}")
    warn = ""
    if meta["width"] > 512 or meta["height"] > 512:
        warn = ("<p class=\"warn\">WARNING: the image is above 512x512. "
                "No resize and no split: the plane limit is ignored.</p>")
    ref_full = os.path.join(out, "reference_full.png")
    ref_src = "reference_full.png" if os.path.exists(ref_full) else "reference.png"
    full_note = ("" if meta.get("full_images") else
                 " Full-size PNGs were not generated; the windows show the "
                 "thumbnail. Use --full (CLI) or the GUI checkbox.")
    doc = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>sweep - {html.escape(os.path.basename(meta['source']))}</title>
<style>{css}</style>
<h1>sweep - {html.escape(os.path.basename(meta['source']))}</h1>
<p>{html.escape(summary)}<br>
palettes: {palettes} · dither: {meta['dithering']} · strengths: {meta.get('strengths')} · modes: {modes}<br>
budgets: {budgets} · cap scope: {cap_scope} · auto max {meta.get('auto_max')} / 75% {meta.get('auto_75')}<br>
palette optimized with dither <b>{'each variant' + chr(39) + 's own' if meta['palette_dither'] == 'same' else meta['palette_dither']}</b>; highlight/both are
measured previews (sprite PAL3 index 14), not tile attributes.<br>
<b>tiles</b> = unique 8x8 tiles in the whole image · <b>KB total</b> = tiles +
map + palettes (raw, before compression) · <b>dE mean</b> = per-pixel ·
<b>dE perc</b> = 4x4 block average (what the eye blends).</p>
{warn}
<p>Each window is a 320x240 viewport over the full-size image.
Scroll or drag any window to pan all of them together; click an image to open
the full PNG in a new browser tab. <label><input type="checkbox" id="sync"
checked> synchronize scroll</label>{html.escape(full_note)}</p>
<p><b>reference</b>:</p>
<div class="view"><a href="{ref_src}" target="_blank" rel="noopener">
<img src="{ref_src}" alt="reference" loading="lazy" draggable="false" style="cursor:zoom-in"></a></div>
{''.join(tables)}
<script>
const sync = document.getElementById('sync');
const views = Array.from(document.querySelectorAll('.view'));
let lock = false;
for (const v of views) {{
  v.addEventListener('scroll', () => {{
    if (!sync.checked || lock) return;
    lock = true;
    const x = v.scrollLeft, y = v.scrollTop;
    for (const o of views) if (o !== v) {{ o.scrollLeft = x; o.scrollTop = y; }}
    requestAnimationFrame(() => {{ lock = false; }});
  }});
}}
</script>
<details><summary>full metrics (JSON/CSV in the folder)</summary>
<pre>{html.escape(json.dumps(meta, ensure_ascii=False, indent=2)[:20000])}</pre>
</details>
</html>"""
    open(os.path.join(out, "sweep.html"), "w", encoding="utf-8").write(doc)


def _sweep_inputs(a):
    """The prepared source and the shared answers of a sweep.

    Used by the sweep and by the export of one variant, so a variant exported
    later is computed from exactly the same picture, backdrop and options.
    """
    bg = parse_fill(a.bg_color)
    rgb0 = load(a.source, bg)
    rgb0, _ = apply_pad(rgb0, a)
    if rgb0.shape[0] % 8 or rgb0.shape[1] % 8:
        raise SystemExit(f"image sides must be multiples of 8: "
                         f"{rgb0.shape[1]}x{rgb0.shape[0]}")
    gain = auto_gain(rgb0) if a.gain in (None, "auto") else float(a.gain)
    rgb = prepare(rgb0, gain, float(a.brightness or 0))
    w, h = rgb.shape[1], rgb.shape[0]
    pan = (w > 320 or h > 224)
    cap_scope = a.cap_scope if a.cap_scope != "auto" else ("view" if pan else "image")
    if a.backdrop == "color" and a.backdrop_color:
        chave = parse_fill(a.backdrop_color)
    else:
        chave = border_key(rgb)
    key_src = [int(v) for v in np.asarray(chave).reshape(-1)[:3]]
    chave = snap_md(np.asarray(chave, np.float64).reshape(1, 1, 3))[0, 0]
    key = [int(v) for v in chave]
    if a.backdrop == "color" and a.backdrop_color:
        key_holes, why = True, "colour chosen by the author"
    else:
        key_holes, _, why = backdrop_is_real(rgb, key_src, a.source)
    resp = {
        "palettes": None, "palette_file": None, "mode": None,
        "transparency": True, "dithering": None,
        "dither_strength": None, "scroll": pan, "resize": "none",
        "max_tiles": None, "cap_scope": cap_scope,
        "backdrop": a.backdrop, "backdrop_rgb": a.backdrop_color,
        "oversize": "none", "gain": gain, "brightness": float(a.brightness or 0),
        "key": key, "key_src": key_src, "view_height": WIN_H * 8,
        "key_holes": bool(key_holes),
    }
    resp["sprite"] = transparent_fraction(a.source) > 0.05
    # v2.10: flat dark shadow regions ringed by bright detail go normal, on
    # one solid colour, instead of a dotted halo (--shadow-dotted-edges = old)
    resp["shadow_clean"] = not bool(getattr(a, "shadow_dotted_edges", False))
    # v2.11: a flat shape with a sharp outline (a door) is never split between
    # normal and shadow tiles (--shadow-free-contours = old, each tile alone)
    resp["shadow_contour"] = not bool(getattr(a, "shadow_free_contours", False))
    return dict(rgb=rgb, gain=gain, pan=pan, cap_scope=cap_scope, key=key,
                key_src=key_src, key_holes=key_holes, why=why, resp=resp)


def build_base(rgb, pals, resp, vq=None, seed=0):
    """Tile base shared by the modes of one group: no preview effects.

    The expensive part (palette assignment, dedup, cap reduction, rebuild) is
    done once per (palettes, dither, vq, shadow planned). With shadow planned
    (resp mode shadow/both) the shadow tiles are chosen here, with the dither
    and before the reduction; normal/highlight share a base without it.
    """
    h, w = rgb.shape[:2]
    idx, tile_pal, _ = assign_palettes(rgb, pals, resp)
    eff = eff_pals(pals, resp)
    idx, tile_pal, uniq, tilemap, rec, peak = reduce_for_cap(
        idx, eff, tile_pal, resp, vq, seed)
    rows = np.repeat(np.repeat(tile_pal, 8, 0), 8, 1)
    painted = np.asarray(eff, np.uint8)[rows, rec]
    tile_pal, shadow_mask = split_tile_pal(tile_pal, len(pals))
    if shadow_on(resp) and shadow_mask is None:
        shadow_mask = np.zeros(tile_pal.shape, bool)
    painted[rec == 0] = backdrop_shown(pals, rec, shadow_mask)
    return {"idx": idx, "tile_pal": tile_pal, "uniq": uniq, "tilemap": tilemap,
            "rec": rec, "painted": painted, "tiles": len(uniq),
            "peak": peak, "bytes_tiles": int(len(uniq)) * 32,
            "bytes_map": int((h // 8) * (w // 8) * 2),
            "shadow_mask": shadow_mask,
            "palette_usage": np.bincount(tile_pal.reshape(-1),
                                         minlength=len(pals)).tolist()}


def export_variant(folder, name, base, var, pals, resp, stem):
    """Write the files of one sweep variant, the same set the md_fundo emit
    step writes: indexed PNG (palette and priority bits), .pal files, .res,
    map JSON, preview and report.md (with the C code).

    The variant is packed as a one-sample samples.json and handed to do_emit,
    so the files are exactly the ones md_fundo exports.
    """
    os.makedirs(folder, exist_ok=True)
    sm = var.get("shadow_mask")
    write_indexed_png(base["rec"], pals, os.path.join(folder, "1_idx.png"),
                      base["tile_pal"], sm)
    Image.fromarray(var["rgb"]).save(os.path.join(folder, "1.png"))
    sample = {
        "n": 1, "letter": "A", "label": stem, "recipe": "sweep",
        "palettes": [np.asarray(p, np.uint8).tolist() for p in pals],
        "tile_pal": np.asarray(base["tile_pal"]).tolist(),
        "palette_usage": base["palette_usage"], "vq": resp.get("max_tiles"),
        "tiles": base["tiles"], "bytes_tiles": base["bytes_tiles"],
        "bytes_map": base["bytes_map"],
        "tilemap": np.asarray(base["tilemap"]).tolist(),
        "shadow_rows": (["".join("1" if v else "0" for v in row) for row in sm]
                        if sm is not None and np.any(sm) else None),
        "dither": resp.get("dithering"), "de_mean": var["de_mean"],
        "de_p95": var["de_p95"], "de_max": var["de_max"],
        "de_perceived": var["de_perceived"], "image": "1.png", "indexed": "1_idx.png",
    }
    answers = {k: v for k, v in resp.items() if isinstance(v, (str, int, float, bool, list, type(None)))}
    path = os.path.join(folder, "samples.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"source": resp.get("source", ""), "answers": answers,
                   "samples": [sample]}, f)
    res = os.path.join(folder, "res", f"{name}.res")
    with contextlib.redirect_stdout(io.StringIO()):
        do_emit(path, name, res, resp, "1")
    return res


def render_variant(rgb, base, pals, resp, mode):
    """One mode from a shared base: normal, shadow, highlight or both."""
    rec = base["rec"]
    tile_pal = base["tile_pal"]
    painted = base["painted"].copy()

    shadow_mask, hl_tiles, hl_gain, hl_img, hl_win = None, 0, 0.0, None, None
    planned = base.get("shadow_mask")
    if planned is not None and mode in ("shadow", "both"):
        # shadow already chosen with the palettes; only highlight is measured
        new, shadow_mask = painted, planned
        if mode == "both":
            _, _, hl_tiles, hl_gain, hl_img, hl_win = vdp_effects(
                rgb, rec, tile_pal, pals, painted, "highlight", planned)
    elif mode in ("shadow", "highlight", "both"):
        new, shadow_mask, hl_tiles, hl_gain, hl_img, hl_win = vdp_effects(
            rgb, rec, tile_pal, pals, painted, mode)
    else:
        new = painted

    # v2.4 and earlier measured "highlight" and "both" on the best-case
    # highlight overlay, which a plane cannot export. The dE is now always the
    # exported image; the highlight overlay gets de_hl and the sprite count.
    prev = new.copy()
    prev[rec == 0] = backdrop_shown(pals, rec, shadow_mask)
    # backdrop taken from the SOURCE (v1 dropped every pixel that came out as
    # index 0, hiding art lost to the backdrop); perceived on the 2-D image
    # (v1 passed a flat pixel list, so it always equalled the mean)
    keep = metric_keep(rgb, resp, shadow_mask)
    m = metrics(rgb[keep], prev[keep]) if keep.any() else {
        "de_mean": 0.0, "de_p95": 0.0, "de_max": 0.0}
    if resp.get("dithering", "none") == "none":
        seen = m["de_mean"]
    else:
        seen = percebido(rgb, prev, keep) if keep.any() else 0.0
    hl_sprites, de_hl, rgb_hl = None, None, None
    if hl_img is not None:
        hl_sprites = highlight_sprites(hl_win)
        rgb_hl = hl_img
        if keep.any():
            de_hl = metrics(rgb[keep], hl_img[keep])["de_mean"]
    return {"rgb": prev, "de_mean": m["de_mean"], "de_p95": m["de_p95"],
            "de_max": m["de_max"], "de_perceived": seen,
            "shadow_mask": shadow_mask, "hl_tiles": hl_tiles,
            "hl_gain": hl_gain, "hl_sprites": hl_sprites, "de_hl": de_hl,
            "rgb_hl": rgb_hl}


# --------------------------------------------------------------------------
# parallel workers (v2.9)
# --------------------------------------------------------------------------
# Every variant (one palette count x dither x budget x shadow group) is
# independent, and the slow part - the tile reduction - runs on one core. So
# the sweep spreads the palettes and then the variants over worker processes.
# Each worker gets the source picture once (initializer) and writes its own
# thumbnails, full previews and exports; the parent only collects the records
# and prints the progress. Results are identical to the one-process run.
_STATE = {}
_ACTIVE_POOL = None
WORKER_RAM_GB = 1.6      # peak memory of one worker per megapixel (measured: 1.66 GB at 1.07 MP)


def _set_active_pool(p):
    global _ACTIVE_POOL
    _ACTIVE_POOL = p


def stop_sweep():
    """Kill the worker processes of a running sweep (used when the window closes)."""
    p = _ACTIVE_POOL
    if p is None:
        return
    for proc in list(getattr(p, "_processes", {}).values()):
        try:
            proc.terminate()
        except Exception:
            pass


def _free_ram_gb():
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1024 ** 3
    except Exception:
        return 8.0


def sweep_workers(asked, n_jobs, rgb):
    """How many worker processes: the user's number, or auto.

    Auto = physical cores minus one, but no more than the free memory allows
    (WORKER_RAM_GB each, scaled by the picture size) and never more than the
    number of variants."""
    try:
        asked = int(asked or 0)
    except (TypeError, ValueError):
        asked = 0
    if asked > 0:
        sweep_workers.why = "chosen by hand"
        return max(1, min(asked, n_jobs))
    cpus = os.cpu_count() or 2
    cores = max(1, cpus // 2 if cpus >= 4 else cpus)
    mp_ = rgb.shape[0] * rgb.shape[1] / 1e6
    per = max(0.4, WORKER_RAM_GB * mp_)
    free = _free_ram_gb()
    by_ram = max(1, int((free - 0.5) / per))
    n = max(1, min(max(1, cores - 1), by_ram, n_jobs))
    sweep_workers.why = (f"{cores} cores, {free:.1f} GB of memory free, about "
                         f"{per:.1f} GB per process for this image")
    if n < min(cores - 1, n_jobs) and by_ram == n:
        sweep_workers.why += " - limited by memory: close other programs for more"
    return n


def _start_pool(workers, state):
    import concurrent.futures as cf
    import multiprocessing as mp_mod
    ctx = mp_mod.get_context("spawn")
    pool = cf.ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                  initializer=_sweep_init, initargs=(state,))
    _set_active_pool(pool)
    return pool


def _as_done(pool, fn, items):
    import concurrent.futures as cf
    futs = {pool.submit(fn, it): it for it in items}
    for f in cf.as_completed(futs):
        yield futs[f], f.result()


def _sweep_init(state):
    _STATE.clear()
    _STATE.update(state)
    set_view_height(int(state.get("view_height") or 224))


def _sweep_palette_job(key_):
    n, plan, pd = key_
    st = _STATE
    r = dict(st["resp"])
    r["palettes"] = n
    r["dithering"] = pd
    r["mode"] = "shadow" if plan else "normal"
    t = time.time()
    pals, _, _ = optimize_palettes(st["rgb"], r, seed=st["seed"], rounds=st["rounds"])
    return [np.asarray(p, np.uint8) for p in pals], time.time() - t


def _sweep_variant_job(j):
    """One base (palettes x dither x budget x shadow group) and its modes."""
    st = _STATE
    rgb, out = st["rgb"], st["out"]
    n, dith, strength, column, vq, plan = (j["n"], j["dith"], j["strength"],
                                           j["column"], j["vq"], j["plan"])
    pals = j["pals"]
    r = dict(st["resp"])
    r.update(palettes=n, dithering=dith, dither_strength=strength,
             mode="shadow" if plan else "normal", max_tiles=vq)
    t = time.time()
    base = build_base(rgb, pals, r, vq=vq, seed=st["seed"])
    lines = [f"  base {n}p/{column}/t{vq}{' shadow' if plan else ''}: "
             f"{time.time() - t:.1f}s  {base['tiles']} tiles  peak {base['peak'][0]}"]
    recs = []
    x, y, cw, ch = st["crop"]
    x = max(0, min(x, rgb.shape[1] - 1))
    y = max(0, min(y, rgb.shape[0] - 1))
    cw = min(cw, rgb.shape[1] - x)
    ch = min(ch, rgb.shape[0] - y)
    for mode in j["modes"]:
        rr = dict(r)
        rr["mode"] = mode
        var = render_variant(rgb, base, pals, rr, mode)
        stem = f"p{n}_{dith}_{strength}_t{vq}_{mode}"
        thumb = os.path.join(st["thumbs"], stem + ".png")
        Image.fromarray(var["rgb"][y:y + ch, x:x + cw]).save(thumb)
        hl_thumb = None
        if var.get("rgb_hl") is not None:
            hl_thumb = os.path.join(st["thumbs"], stem + "_hl.png")
            Image.fromarray(var["rgb_hl"][y:y + ch, x:x + cw]).save(hl_thumb)
        full = None
        if st["full"]:
            full = os.path.join(st["full_dir"], stem + ".png")
            Image.fromarray(var["rgb"]).save(full)
        p = base["peak"]
        cap_ok = (base["tiles"] <= vq if st["cap_scope"] == "image" else p[0] <= vq)
        kb = (base["bytes_tiles"] + base["bytes_map"] + 32 * n) / 1024.0
        rec = {
            "palettes": n, "dithering": dith, "strength": strength,
            "column": column, "max_tiles": vq, "mode": mode,
            "tiles": base["tiles"], "bytes_tiles": base["bytes_tiles"],
            "bytes_map": base["bytes_map"],
            "peak": p[0], "peak_x0": p[1] * 8, "peak_y0": p[2] * 8,
            "peak_x1": p[3] * 8, "peak_y1": p[4] * 8,
            "de_mean": var["de_mean"], "de_p95": var["de_p95"],
            "de_max": var["de_max"], "de_perceived": var["de_perceived"],
            "shadow_tiles": (int(var["shadow_mask"].sum())
                             if var["shadow_mask"] is not None else 0),
            "hl_tiles": var["hl_tiles"], "hl_gain": var["hl_gain"],
            "de_hl": var.get("de_hl"), "hl_sprites": var.get("hl_sprites"),
            "hl_thumb": (os.path.relpath(hl_thumb, out).replace("\\", "/")
                         if hl_thumb else None),
            "palette_usage": base["palette_usage"],
            "bytes_total": base["bytes_tiles"] + base["bytes_map"] + 32 * n,
            "kb_total": round(kb, 1), "cap_ok": cap_ok,
            "thumb": os.path.relpath(thumb, out).replace("\\", "/"),
            "full": os.path.relpath(full, out).replace("\\", "/") if full else None,
        }
        if st["export"]:
            exp_dir = os.path.join(out, "export", stem)
            export_variant(exp_dir, c_name(st["base_name"]), base, var, pals,
                           dict(rr, source=st["source"]), stem)
            rec["export"] = os.path.relpath(os.path.join(exp_dir, "res"),
                                            out).replace("\\", "/")
        if var.get("hl_sprites") is not None:
            rec["hl_note"] = (f"{highlight_note(var['hl_sprites'])}; dE with it "
                              f"{var['de_hl']:.2f} (not exported)")
        recs.append(rec)
        lines.append(f"{stem}: dE {var['de_mean']:.2f} / perc "
                     f"{var['de_perceived']:.2f} · {kb:.1f} KB total")
    return recs, lines


def cmd_sweep(a):
    """Generate a comparison grid of setup answers, without asking per cell."""
    set_view_height(int(getattr(a, "view_height", None) or 224))
    palettes = [int(v) for v in _csv_list(a.palettes, None, "--palettes")]
    if any(p < 1 or p > 4 for p in palettes):
        raise SystemExit("--palettes must be between 1 and 4")
    dithers = _csv_list(a.dithering, DITHERS, "--dithering")
    modes = _csv_list(a.modes, MODES, "--modes")
    strengths = _csv_list(a.dither_strength, tuple(DITHER_STRENGTHS),
                          "--dither-strength")

    # "none" and "floyd" ignore the strength value; keep one column for them
    # so the grid does not contain identical variants.
    dither_specs = []
    for d in dithers:
        if d == "none":
            dither_specs.append((d, "full", d))
        else:
            for s in strengths:
                dither_specs.append((d, s, f"{d}/{s}"))

    inp = _sweep_inputs(a)
    rgb, gain, pan, cap_scope = inp["rgb"], inp["gain"], inp["pan"], inp["cap_scope"]
    key, key_holes, why, resp = inp["key"], inp["key_holes"], inp["why"], inp["resp"]
    w, h = rgb.shape[1], rgb.shape[0]

    if w > 512 or h > 512:
        print(f"\nWARNING: {w}x{h} is above 512x512. No resize and no split: "
              "the plane limit is ignored, the ROM must handle it.")
    print(f"\nmode: {'pan background' if pan else 'static image'} "
          f"({w}x{h}) · cap scope: {cap_scope} · no resize, no split")

    info = preflight(rgb)
    print_preflight(info)
    auto_max = info["pior_tela"]
    budgets, auto_max, auto_75 = _parse_budgets(a.max_tiles, auto_max)
    total = len(palettes) * len(modes) * len(dither_specs) * len(budgets)
    n_opt = len(palettes) * (len(dithers) if (getattr(a, "palette_dither", None)
                                             or "same") == "same" else 1)
    est = estimate_sweep_seconds(w, h, total, info, budgets, n_opt)
    all_checked = (len(palettes) == 4 and len(modes) == 4
                   and len(dithers) == len(DITHERS)
                   and len(strengths) == len(DITHER_STRENGTHS))
    print(f"  budgets per view: {budgets}  (auto max {auto_max}, 75% {auto_75})")
    print(f"  samples: {total}  ·  estimate {est / 60.0:.1f} min")
    if all_checked:
        print(f"\nWARNING: every option is checked -> {total} samples "
              f"(~{est / 60.0:.1f} min rough estimate).")
    if w * h > 8_000_000 and min(budgets) < auto_max:
        raise SystemExit(
            f"tile reduction refused: {w}x{h} is {w * h / 1_000_000:.1f} MP "
            f"and the budget ({min(budgets)}) is below the auto view max "
            f"({auto_max}). A pairwise merge would be enormous. Resize or split "
            "the image, raise both budgets to the auto max, or use "
            "cap_scope=image.")

    resp.update(palettes=palettes[0], mode=modes[0], dithering=dithers[0],
                dither_strength=strengths[0], max_tiles=budgets[0])

    base_name = os.path.splitext(os.path.basename(a.source))[0]
    raw_out = os.path.normpath(a.out or os.path.join(os.getcwd(), f"sweep_{base_name}"))
    out = (raw_out if os.path.abspath(raw_out) == os.getcwd()
           else make_unique_dir(raw_out))
    setattr(a, "actual_out", out)   # the GUI reads this after cmd_sweep
    thumbs = os.path.join(out, "thumbs")
    full_dir = os.path.join(out, "full")
    os.makedirs(thumbs, exist_ok=True)
    if a.full:
        os.makedirs(full_dir, exist_ok=True)

    print(f"\nsweep: {rgb.shape[1]}x{rgb.shape[0]}  gain x{gain:.2f}")
    print(f"  palettes: {palettes}")
    print(f"  dithering: {dithers}  strengths {strengths}")
    print(f"  modes: {modes}")
    print(f"  backdrop: #{key[0]:02X}{key[1]:02X}{key[2]:02X}  "
          f"budgets {budgets} ({cap_scope})")
    print(f"  transparent backdrop holes: {'yes' if key_holes else 'NO'} ({why})")
    if any(m in ("shadow", "both") for m in modes):
        print("  shadow borders: " + ("clean (flat dark areas ringed by bright detail "
              "go normal on one solid colour)" if resp["shadow_clean"]
              else "dotted (--shadow-dotted-edges)"))
        print("  shadow contours: " + ("kept (flat shapes with an outline are not split "
              "between normal and shadow)" if resp["shadow_contour"]
              else "free (--shadow-free-contours)"))
    pd_mode = getattr(a, "palette_dither", None) or "same"
    if pd_mode == "same":
        print("  palettes: optimized with each variant's own dithering (one per "
              "palette count x dither; shadow/both get their own, shadow planned in)")
    else:
        print(f"  palettes: one optimization per palette count, with dither "
              f"{pd_mode} (shared by every dither); shadow/both get their own")

    # ---- palettes: the only expensive axis, once per palette count ----
    # shadow/both plan the shadow with the palettes (v2.4); normal and
    # highlight share the plain palettes. --shadow-late keeps the old way.
    late = bool(getattr(a, "shadow_late", False))
    groups = []
    plain = [m for m in modes if late or m not in ("shadow", "both")]
    planned = [m for m in modes if not late and m in ("shadow", "both")]
    if plain:
        groups.append((False, plain))
    if planned:
        groups.append((True, planned))
    # Palettes are optimized with the dithering they will be shown with
    # ("same", default since v2.7): an undithered palette has no colours that
    # MIX into a tone it lacks, so a dark gradient sky came out flat lilac in
    # normal and as dark shadow blocks in shadow. --palette-dither <name>
    # keeps one shared palette per count, optimized with that dither.
    def pal_key(n, plan, dith):
        return (n, plan, dith if pd_mode == "same" else pd_mode)

    variants = []
    crop = _parse_crop(a.crop)
    if crop is not None:
        x, y, cw, ch = crop
        x = max(0, min(x, rgb.shape[1] - 1))
        y = max(0, min(y, rgb.shape[0] - 1))
        crop = (x, y, min(cw, rgb.shape[1] - x), min(ch, rgb.shape[0] - y))
    else:
        # thumbnails show the busiest view of the source (known before any
        # variant runs, so every worker crops the same place)
        crop = (info["pior_x0"] * 8, info["pior_y0"] * 8,
                (info["pior_x1"] - info["pior_x0"]) * 8,
                (info["pior_y1"] - info["pior_y0"]) * 8)

    jobs = []
    for n in palettes:
        for dith, strength, column in dither_specs:
            for vq in budgets:
                for plan, gmodes in groups:
                    jobs.append(dict(i=len(jobs), n=n, dith=dith, strength=strength,
                                     column=column, vq=vq, plan=plan, modes=gmodes))
    keys = []
    for j in jobs:
        k_ = pal_key(j["n"], j["plan"], j["dith"])
        if k_ not in keys:
            keys.append(k_)

    workers = sweep_workers(getattr(a, "workers", 0), len(jobs), rgb)
    state = dict(rgb=rgb, resp=resp, seed=a.seed, rounds=a.palette_rounds,
                 full=bool(a.full), export=bool(getattr(a, "export", False)),
                 source=a.source, out=out, thumbs=thumbs, full_dir=full_dir,
                 crop=crop, base_name=base_name, cap_scope=cap_scope,
                 view_height=WIN_H * 8, total=total)
    print(f"  workers: {workers} process{'es' if workers > 1 else ''} "
          f"({getattr(sweep_workers, 'why', '')}; --workers to change)")
    t_all = time.time()
    pal_cache = {}
    done = 0
    pool = None
    try:
        if workers > 1:
            pool = _start_pool(workers, state)
            run = lambda fn, items: _as_done(pool, fn, items)   # noqa: E731
        else:
            _sweep_init(state)
            run = lambda fn, items: ((it, fn(it)) for it in items)   # noqa: E731
        for k_, (pals, secs) in run(_sweep_palette_job, keys):
            pal_cache[k_] = pals
            n, plan, pd = k_
            print(f"  palettes {n}{' (shadow planned)' if plan else ''}, "
                  f"optimized with {pd}: {secs:.1f}s", flush=True)
        for j in jobs:
            j["pals"] = pal_cache[pal_key(j["n"], j["plan"], j["dith"])]
        results = {}
        for j, (recs, lines) in run(_sweep_variant_job, jobs):
            results[j["i"]] = recs
            print(lines[0], flush=True)
            for rec, extra in zip(recs, lines[1:]):
                done += 1
                print(f"    [{done}/{total}] {extra}", flush=True)
                if rec.get("hl_note"):
                    print(f"        {rec['hl_note']}", flush=True)
        for i in sorted(results):
            variants.extend(results[i])
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
            _set_active_pool(None)

    dt = time.time() - t_all
    ref_path = os.path.join(out, "reference.png")
    x, y, cw, ch = crop
    Image.fromarray(rgb[y:y + ch, x:x + cw]).save(ref_path)
    Image.fromarray(rgb).save(os.path.join(out, "reference_full.png"))
    meta = {
        "source": os.path.abspath(a.source), "width": rgb.shape[1],
        "height": rgb.shape[0], "gain": gain, "key": key,
        "view_height": WIN_H * 8, "tool": "md_fundo_v2",
        "palettes": palettes, "dithering": dithers, "strengths": strengths,
        "dither_specs": [{"dither": d, "strength": s, "column": c}
                         for d, s, c in dither_specs],
        "modes": modes, "palette_dither": a.palette_dither,
        "max_tiles": budgets, "budgets": budgets, "cap_scope": cap_scope,
        "pan": pan, "auto_max": auto_max, "auto_75": auto_75,
        "sprite": bool(resp.get("sprite")),
        "estimated_seconds": round(est, 1), "full_images": bool(a.full),
        "crop": list(crop), "seconds": round(dt, 1),
        "args": sweep_args(a), "variants": variants,
    }
    json.dump(meta, open(os.path.join(out, "sweep.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    with open(os.path.join(out, "sweep.csv"), "w", encoding="utf-8",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for v in variants for k in v)))
        w.writeheader()
        w.writerows(variants)
    _write_sweep_html(out, meta, rgb)
    print(f"\nsweep done: {total} variants in {dt:.1f}s")
    print(f"  {os.path.join(out, 'sweep.html')}")
    print(f"  {os.path.join(out, 'sweep.json')}")
    show_link("sweep", os.path.join(out, "sweep.html"))
    return 0


# --------------------------------------------------------------------------
# export of ONE variant, after the sweep (v2.11)
# --------------------------------------------------------------------------
# --export writes the files of every variant. Usually only one is wanted, the
# one picked in the gallery, so a sweep folder can be handed back with the
# name of a variant: it is computed again from the options kept in sweep.json
# (same picture, seed, palette rounds, shadow rules) and checked pixel by
# pixel against its thumbnail and full-size preview.
ARG_KEYS = ("source", "palettes", "dithering", "modes", "dither_strength",
            "palette_dither", "palette_rounds", "max_tiles", "cap_scope",
            "view_height", "crop", "gain", "brightness", "bg_color", "no_pad",
            "pad_color", "backdrop", "backdrop_color", "seed", "shadow_dotted_edges",
            "shadow_free_contours", "shadow_late")
# sweeps written before v2.11 kept no options: these are the window's
# defaults, and the contour rule did not exist yet
OLD_SWEEP_ARGS = dict(bg_color=None, no_pad=False, pad_color=None, gain="auto",
                      brightness=0.0, backdrop="border", backdrop_color=None, seed=0,
                      palette_rounds=2, palette_dither="same", shadow_late=False,
                      shadow_dotted_edges=False, shadow_free_contours=True,
                      view_height=224, cap_scope="auto", crop=None)


def sweep_args(a):
    out = {}
    for k in ARG_KEYS:
        v = getattr(a, k, None)
        if isinstance(v, (str, int, float, bool, type(None))):
            out[k] = v
    return out


def variant_stem(v):
    return os.path.splitext(os.path.basename(v["thumb"]))[0]


def variant_label(v):
    n = v["palettes"]
    kb = v.get("kb_total")
    kb = f" · {kb:.1f} KB" if kb is not None else ""
    return (f'{n} palette{"s" if n > 1 else ""} · {v["column"]} · '
            f'{v["max_tiles"]} tiles · {v["mode"]} - dE {v["de_mean"]:.2f} · '
            f'perc {v.get("de_perceived", v["de_mean"]):.2f}{kb}')


def load_sweep(folder):
    """(folder, meta) of a sweep folder or of its sweep.json."""
    folder = os.path.abspath(folder.strip().strip('"'))
    if os.path.isfile(folder):
        folder = os.path.dirname(folder)
    path = os.path.join(folder, "sweep.json")
    if not os.path.isfile(path):
        raise SystemExit(f"no sweep.json in {folder}")
    with open(path, encoding="utf-8") as f:
        return folder, json.load(f)


def export_one(folder, stem, log=print):
    """Compute one variant of a sweep again and write its files.

    Returns (export folder, identical): identical is True when the variant
    came out pixel for pixel as the sweep drew it (thumbnail and, when it
    exists, the full-size preview)."""
    folder, meta = load_sweep(folder)
    found = [v for v in meta["variants"] if variant_stem(v) == stem]
    if not found:
        raise SystemExit(f"variant not found in this sweep: {stem}")
    v = found[0]
    saved = meta.get("args")
    d = dict(OLD_SWEEP_ARGS)
    if saved:
        d.update(saved)
    else:
        log("  note: this sweep was made before v2.11 and kept no options; "
            "using the window defaults")
    d["source"] = meta["source"]
    d["gain"] = meta["gain"]
    d["view_height"] = int(meta.get("view_height") or d.get("view_height") or 224)
    a = argparse.Namespace(**d)
    if not os.path.isfile(a.source):
        raise SystemExit(f"the source image of this sweep is gone: {a.source}")
    set_view_height(a.view_height)
    t = time.time()
    inp = _sweep_inputs(a)
    rgb, resp = inp["rgb"], inp["resp"]
    resp["cap_scope"] = meta.get("cap_scope", resp["cap_scope"])
    n, dith, strength = v["palettes"], v["dithering"], v["strength"]
    vq, mode = v["max_tiles"], v["mode"]
    plan = (not a.shadow_late) and mode in ("shadow", "both")
    pd_mode = meta.get("palette_dither") or "same"
    r = dict(resp, palettes=n, dithering=dith if pd_mode == "same" else pd_mode,
             mode="shadow" if plan else "normal")
    log(f"  {stem}: palettes...")
    pals, _, _ = optimize_palettes(rgb, r, seed=a.seed, rounds=a.palette_rounds)
    pals = [np.asarray(p, np.uint8) for p in pals]
    r2 = dict(resp, palettes=n, dithering=dith, dither_strength=strength,
              mode="shadow" if plan else "normal", max_tiles=vq)
    log(f"  {stem}: tiles...")
    base = build_base(rgb, pals, r2, vq=vq, seed=a.seed)
    rr = dict(r2, mode=mode)
    var = render_variant(rgb, base, pals, rr, mode)
    # the check: what the sweep drew
    x, y, cw, ch = meta["crop"]
    x = max(0, min(x, rgb.shape[1] - 1))
    y = max(0, min(y, rgb.shape[0] - 1))
    cw = min(cw, rgb.shape[1] - x)
    ch = min(ch, rgb.shape[0] - y)
    diffs = []
    for rel, img in ((v.get("thumb"), var["rgb"][y:y + ch, x:x + cw]),
                     (v.get("full"), var["rgb"])):
        if rel and os.path.isfile(os.path.join(folder, rel)):
            ref = np.asarray(Image.open(os.path.join(folder, rel)).convert("RGB"))
            if ref.shape != img.shape:
                diffs.append(ref.shape[0] * ref.shape[1])
            else:
                diffs.append(int((ref != img).any(2).sum()))
    identical = bool(diffs) and max(diffs) == 0
    exp_dir = os.path.join(folder, "export", stem)
    base_name = os.path.splitext(os.path.basename(a.source))[0]
    res = export_variant(exp_dir, c_name(base_name), base, var, pals,
                         dict(rr, source=a.source), stem)
    v["export"] = os.path.relpath(os.path.dirname(res), folder).replace(os.sep, "/")
    with open(os.path.join(folder, "sweep.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    _write_sweep_html(folder, meta, rgb)
    if identical:
        log(f"  {stem}: identical to the sweep preview ({time.time() - t:.0f}s)")
    elif diffs:
        log(f"  {stem}: WARNING - differs from the sweep preview in "
            f"{max(diffs)} pixels (the sweep was made with another version or "
            f"other options); the files match export/{stem}/1.png, not the gallery")
    log(f"  files: {exp_dir}")
    return exp_dir, identical


def cmd_export(a):
    folder, meta = load_sweep(a.sweep)
    stems = [variant_stem(v) for v in meta["variants"]]
    if not a.variant:
        print(f"variants of {folder}:")
        for i, v in enumerate(meta["variants"], 1):
            print(f"  {i:3d}  {variant_stem(v)}   ({variant_label(v)})")
        print("")
        print("export one with --variant <name or number>")
        return 0
    picked = []
    for item in a.variant.split(","):
        item = item.strip()
        if item.isdigit() and 1 <= int(item) <= len(stems):
            item = stems[int(item) - 1]
        picked.append(item)
    ok = True
    for stem in picked:
        _, same = export_one(folder, stem)
        ok = ok and same
    return 0 if ok else 2


def _export_interactive(folder):
    """Double-clicked console, a sweep folder given: pick variants by number."""
    folder, meta = load_sweep(folder)
    stems = [variant_stem(v) for v in meta["variants"]]
    print("")
    print(f"sweep: {folder}")
    for i, v in enumerate(meta["variants"], 1):
        print(f"  {i:3d}  {variant_label(v)}")
    print("")
    try:
        ans = input("export which? number(s), comma separated (Enter = none): ").strip()
    except EOFError:
        ans = ""
    for item in [t.strip() for t in ans.split(",") if t.strip()]:
        if not item.isdigit() or not 1 <= int(item) <= len(stems):
            print(f"  not a number from the list: {item}")
            continue
        export_one(folder, stems[int(item) - 1])
    return 0


def main():
    ap = argparse.ArgumentParser(description="Mega Drive background: measure, sample, choose, emit.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("measure", aliases=["medir"], help="rebuild the texture and find the period")
    m.add_argument("--photos", required=True)
    m.add_argument("--grid", required=True)
    m.add_argument("--scale", default="auto")
    m.add_argument("--step", type=int, default=4)
    m.add_argument("--out")
    m.add_argument("--no-pad", action="store_true")
    m.add_argument("--pad-color")
    m.set_defaults(func=cmd_measure)

    for name, fn in (("sample", cmd_sample), ("emit", cmd_emit)):
        p = sub.add_parser(name, aliases=["amostra"] if name == "sample" else ["emitir"])
        p.add_argument("--source")
        p.add_argument("--samples")
        p.add_argument("--out")
        p.add_argument("--name", default="parede")
        p.add_argument("--res")
        p.add_argument("--n")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--no-ask", action="store_true")
        p.add_argument("--palettes", type=int, choices=(1, 2, 3, 4))
        p.add_argument("--palette", choices=("own", "file"))
        p.add_argument("--palette-file")
        p.add_argument("--mode", choices=MODES)
        p.add_argument("--transparency", choices=("yes", "no"),
                       help="deprecated: index 0 is always the backdrop")
        p.add_argument("--backdrop", choices=("border", "color"),
                       help="where the backdrop colour (index 0 of every palette) comes from")
        p.add_argument("--backdrop-color", help="RRGGBB when --backdrop color")
        p.add_argument("--dithering", choices=DITHERS)
        p.add_argument("--dither-strength", choices=list(DITHER_STRENGTHS))
        p.add_argument("--gain", default="auto")
        p.add_argument("--brightness", type=float, default=0.0)
        p.add_argument("--no-pad", action="store_true",
                       help="do not pad the image to multiples of 8")
        p.add_argument("--pad-color", help="RRGGBB of the padded border (default FFFFFF)")
        p.add_argument("--scroll", choices=("yes", "no"))
        p.add_argument("--resize", choices=("width", "height", "none"))
        p.add_argument("--bg-color", help="RRGGBB behind the alpha channel (default FFFFFF)")
        p.add_argument("--oversize", choices=("split", "resize", "none"),
                       help="what to do when a side is over the 512 px plane limit")
        p.add_argument("--max-tiles", type=int,
                       help="cap the unique tiles of every sample (0 = no limit)")
        p.add_argument("--view-height", type=int, choices=(224, 240), default=224)
        p.add_argument("--cap-scope", choices=("image", "view"),
                       help="does the tile cap apply to the whole image"
                            " or to each 320x240 view")
        p.set_defaults(func=fn, cmd=name)

    c = sub.add_parser("choose", aliases=["escolher"])
    c.add_argument("choice", help="1..4 or A..D")
    c.add_argument("--samples", required=True)
    c.set_defaults(func=cmd_choose)

    s = sub.add_parser("sweep", aliases=["grade"],
                       help="comparison grid of palettes x dither x mode")
    s.add_argument("--source", required=True)
    s.add_argument("--out")
    s.add_argument("--palettes", default="1,2,3,4")
    s.add_argument("--dithering", default="none,ordered4,ordered8,floyd")
    s.add_argument("--modes", default="normal,shadow,highlight,both")
    s.add_argument("--dither-strength", default="clean,balanced,full",
                   help="comma list: clean,balanced,full (ignored by none/floyd)")
    s.add_argument("--palette-dither", default="same", choices=("same",) + tuple(DITHERS),
                   help="dithering used to optimize the palettes: 'same' (default) "
                        "= each variant's own dithering; a name = one shared palette "
                        "per count optimized with it (faster, v2.6 used 'none')")
    s.add_argument("--palette-rounds", type=int, default=2,
                   help="refinement rounds of the cached palette (1-3; default 2)")
    s.add_argument("--max-tiles", default="",
                   help="one or two per-view budgets, comma separated; blank = "
                        "auto max view and 75%% of it")
    s.add_argument("--cap-scope", choices=("auto", "image", "view"), default="auto",
                   help="auto: view for pan backgrounds, image for static")
    s.add_argument("--view-height", type=int, choices=(224, 240), default=224,
                   help="one view for the per-view cap: 224 NTSC, 240 PAL")
    s.add_argument("--crop", help="thumbnail crop x,y,w,h (default: peak view)")
    s.add_argument("--full", action="store_true",
                   help="also write full-size RGB previews for every variant")
    s.add_argument("--gain", default="auto")
    s.add_argument("--brightness", type=float, default=0.0)
    s.add_argument("--bg-color")
    s.add_argument("--no-pad", action="store_true")
    s.add_argument("--pad-color")
    s.add_argument("--backdrop", choices=("border", "color"), default="border")
    s.add_argument("--backdrop-color")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--workers", type=int, default=0,
                   help="worker processes (0 = auto: cores - 1, limited by free memory)")
    s.add_argument("--export", action="store_true",
                   help="also write the palette (.pal), map (JSON), indexed PNG, "
                        ".res and report for every variant, in export/<variant>/")
    s.add_argument("--shadow-dotted-edges", action="store_true",
                   help="keep the shadow in flat dark regions ringed by bright "
                        "detail (old way: a dotted halo on their border); by "
                        "default they go normal on one solid colour")
    s.add_argument("--shadow-free-contours", action="store_true",
                   help="let each tile of a flat shape with a sharp outline (a door) "
                        "pick normal or shadow on its own (old way); by default the "
                        "shape takes one state, dark ones shadow - contour before colour")
    s.add_argument("--shadow-late", action="store_true",
                   help="old shadow: decide it per finished tile, after the "
                        "palettes, dither and reduction (default: plan it "
                        "with the palettes)")
    s.set_defaults(func=cmd_sweep, cmd="sweep")

    e = sub.add_parser("export", aliases=["exportar"],
                       help="write the palette, map and .res of ONE variant of a sweep")
    e.add_argument("--sweep", required=True, help="the sweep folder (or its sweep.json)")
    e.add_argument("--variant", help="variant name as shown under each picture in "
                   "sweep.html (or its number); comma list for several; "
                   "blank = list them")
    e.set_defaults(func=cmd_export, cmd="export")

    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())

    try:
        # an image dragged onto the exe arrives as the only argument
        handoff = not os.environ.get("MD_TOOLS_NO_HANDOFF")
        if (handoff and getattr(sys, "frozen", False) and len(sys.argv) == 2
                and os.path.isfile(sys.argv[1])):
            gui = os.path.join(os.path.dirname(sys.executable),
                               "md_fundo_sweep_gui_v2.exe")
            if os.path.isfile(gui):
                subprocess.Popen([gui, sys.argv[1]])
                return 0
            a = ap.parse_args(["sweep", "--source", sys.argv[1]])
            rc = a.func(a)
            _pausa()
            return rc

        if getattr(sys, "frozen", False) and len(sys.argv) == 1:
            gui = os.path.join(os.path.dirname(sys.executable),
                               "md_fundo_sweep_gui_v2.exe")
            if handoff and os.path.isfile(gui):
                subprocess.Popen([gui])
                return 0
            # double click without the GUI: ask for the image
            print("md_fundo - Mega Drive background tool")
            print("")
            print("type the path of the image (drag it into this window),")
            print("or of a sweep folder to export one of its variants,")
            print("or press Enter to see the command line help.")
            print("")
            try:
                caminho = input("image: ").strip().strip('"')
            except EOFError:
                caminho = ""
            if not caminho:
                ap.print_help()
                _pausa()
                return 0
            if not os.path.exists(caminho):
                print(f"not found: {caminho}")
                _pausa()
                return 1
            if (os.path.isdir(caminho) or
                    os.path.basename(caminho).lower() == "sweep.json"):
                rc = _export_interactive(caminho)
                _pausa()
                return rc
            a = ap.parse_args(["sweep", "--source", caminho])
            rc = a.func(a)
            _pausa()
            return rc

        a = ap.parse_args()
        return a.func(a)
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        # argparse and the tool's own "raise SystemExit(...)" land here
        if exc.code not in (0, None) and _interativo():
            _pausa()
        raise
    except BaseException as exc:
        log = _grava_log(exc)
        print("\n--- the tool failed ---")
        print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if log:
            print(f"log written: {log}")
        else:
            print("could not write the log file (check the folder permissions)")
        if _interativo():
            _pausa()
        return 1


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
