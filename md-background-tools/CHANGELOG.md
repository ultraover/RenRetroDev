# Changelog

## 1.2 (2026-11-13)

- Sweep: export the files of just the variant you picked. The Sweep window
  has an "Export one sample" box (the variants of the last sweep, or of an
  older sweep folder); the console takes `export --sweep <folder> --variant
  <name or number>`, or a sweep folder dragged into the window. The variant is
  computed again and checked pixel by pixel against the gallery; each picture
  in sweep.html now shows its name, and sweep.json keeps every option of the
  run. Sweeps made by older versions can still be exported; the check says
  when the result differs from their gallery.
- Shadow: a flat shape with a sharp outline (a door, a window) is no longer
  split between normal and shadow tiles. With one palette and dither, the
  middle of a dark door could go normal while its rim stayed shadow, and the
  arch lost its outline. Dark shapes now go shadow as a whole (contour before
  colour); a tile that also holds bright detail keeps its state. On by
  default; it can be turned off in every tool: a "keep contours" checkbox in
  the Background and Sweep windows, a question in the Background console
  (`--shadow-contours off`) and `--shadow-free-contours` in the sweep console.
- Shadow: flat dark areas ringed by bright detail (a pit next to rock) go
  normal on one solid colour, so their border no longer shows a one-tile
  dotted halo. On by default; it can be turned off in every tool: a checkbox
  in the Background and Sweep windows, a question in the Background console
  (`--shadow-edges dotted`) and `--shadow-dotted-edges` in the sweep console.
- Shadow: the choice between a normal and a shadow tile now also counts the
  dithering grain, so a dotted normal tile no longer beats a smooth, faithful
  shadow tile.
- The report and the generated C code explain the priority bit in the game
  (sprite priority, both planes, PAL3 colours 14 and 15).
- Fixed: the sweep crashed while writing sweep.csv when the modes had
  different columns (normal and highlight).

## 1.1 (2026-11-12)

- The sweep computes palettes and variants in parallel on several CPU cores
  (`--workers`, and "CPU processes" in the window); results are identical.
- Closing the sweep window during a run asks first and stops cleanly.

## 1.0 (2026-11-12)

First public release.

- Start screen with four tools: Background (window and console), Sweep
  (window and console).
- Palettes on the MD 512-colour grid, chosen per tile; mirror-aware tile dedup;
  tile budget for the whole image or for every 320×224 / 320×240 view.
- Nine dithering methods with three strengths; palettes optimized with the
  dithering they are shown with.
- VDP shadow planned together with the palettes, with no 8×8 mosaic on smooth
  areas and softened borders between shadow and normal regions; highlight
  measured with a sprite-count estimate.
- Real backdrop detection: an edge that is part of the art is not made
  transparent.
- SGDK export: indexed PNG, `.pal`, `.res`, map JSON, preview and sample C code;
  the sweep can export every variant.
