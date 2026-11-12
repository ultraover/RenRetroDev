# Changelog

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
