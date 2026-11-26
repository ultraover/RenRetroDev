# Changelog

## 2.1 (2026-11-26)

- Sweep: **normal/shadow uniformity**. With shadow, one piece of the drawing
  that crossed normal and shadow tiles came out in two colours, one on each
  side. The new option (0 to 100%, 0 = as before) gives up some fidelity for
  one colour on both sides: every source colour shown in both states takes the
  colour of the state that shows most of it, the tiles move whole to that
  state when they can, and the palettes are fitted to it. Works with every
  dither. A list (`0, 50, 100`) makes one column each; each card shows the
  normal/shadow jump. `--uniform 0,50,100`.
- Fix: a picture with a horizontal stretch that is all backdrop (the
  transparent bottom of a strip) stopped the conversion with "float division
  by zero". The palette of that stretch now starts from the art of the whole
  picture.
- Sweep window: **First seed**, next to Variations. Pick the seed to start
  from: 9 with 1 variation runs seed 9 alone (the console already had
  `--seed`).
- New tool, **Sprite converter** (window and console): convert the frames of a
  character in one go. Frames are grouped into animations by name (one row of
  the sheet each), share one palette made from all of them (or a palette
  file, with locked colours and 14-15 left free for PAL3 with
  shadow/highlight), lose their background (alpha, key colour or border, with
  a halo cleanup along the edge) and are cut to one size in multiples of 8,
  lined up on an anchor (bottom centre by default). Every dither asked is
  painted and shown animated in a gallery, with its colour error, flicker
  between frames, tiles and hardware sprites per frame. Export one variant
  as an SGDK sprite sheet: indexed `.png`, `.pal`, `.res` (`SPRITE` line) and
  a report with C code.
- Sprite converter: **output size**. Pick the size of every frame (a list of
  sizes, or tiles; multiples of 8 up to 248 px a side, as rescomp takes) and
  fit the character to it (scaled alike in every frame) or crop it around the
  anchor. The window shows, before converting, the frame size and its
  **hardware sprites** (1-4 tiles a side): how many per frame and their sizes,
  the worst line and the VRAM, in red when a frame needs more than the 16
  hardware sprites rescomp takes per frame. The report lists every frame.
  `--size 32x48` or `--size 4x6t`, `--fit fit|crop`.
- Sprite converter: frames named by numbers only (`0001.png`) were each an
  animation of their own; they are now one animation named after their folder.
- Build: big packages the tools never use (PyTorch, OpenCV, onnxruntime...)
  are left out of the `.exe` even when they are installed.

## 2.0 (2026-11-17)

- **The Sweep does everything now; the Background tool is off the start
  screen** (it still runs with `--run background-gui` or
  `--run background-console`). The Sweep window gained what only the
  Background had:
  - **Backdrop (index 0):** from the border or a fixed colour.
  - **Cap applies to:** auto, the whole image or each screen view.
  - **Over 512 px:** keep, shrink to fit or split. Split cuts the image into
    the fewest pieces that fit and runs one sweep per piece, with the gain,
    backdrop and transparency of the whole image; `split.html` and
    `split.json` list them.
  - **Padding:** pad to multiples of 8 with a colour (or untick it), and the
    colour behind transparent pixels.
  - **Export one sample:** a **Name** and a **res folder**, to write
    `<name>.res` and its files straight into your project.
  - On the command line: `sweep --oversize none|resize|split`,
    `export --name <name> --res <folder>`.

- New dithering **`ordered2`** (Bayer 2×2, 2N−1 tones): each colour of the
  palette, in order of lightness, mixes only with its neighbour, half and
  half, as a checkerboard, so N colours give 2N−1 tones. For palettes that
  climb in lightness (ladder palettes, grey mode). In the Background, the
  Sweep (unticked by default) and on the command line (`--dithering ordered2`).
- Fix: `ordered4`, `ordered8`, `halftone` and `bluenoise` chose each pair of
  colours by its mean only, so on `full` a flat area with no close colour in
  the palette could be drawn with a dark colour and 10-25% of light dots
  (a "drizzle" over the image; perceived error around 17 against 5 now on a
  city background). The grain of the pattern now counts in the choice, and a
  pair that costs more than the solid colour loses to it. The grain dropped
  30-45% on that background. Old sweeps with these dithers no longer match a
  new export of the same variant.
- Less grain in `ordered4`, `ordered8`, `halftone` and `bluenoise`: they mix
  only neighbour colours now (a pair with another palette colour between them,
  such as black and olive with a grey between, drew salt and pepper and dark
  dots inside a light moon), and where the source is smooth they dither its
  3×3 mean, so the brush texture of a painting no longer makes the pattern
  break into noise. On a 320×1520 painted city: grain 10-15% lower, same
  colour fidelity. Old sweeps with these dithers no longer match a new export.
- New option **Solid vs dots** (0-100%, 50% = normal) in the Background, the
  Sweep and on the command line (`--solid 0..1`): a preference between dots and
  solid colour for every dither. Higher, a dither must win by more before it
  draws dots and the diffusions (Floyd, Atkinson, Stucki, Riemersma) spread
  less error; lower, it dithers more readily. The palette of each tile follows
  the same preference. At 50% nothing changes.
- New tool, **Tile editor**: open an export and rearrange its 8×8 tiles
  (copy, paste, delete, mirror), turn each tile's priority on or off, with zoom
  from 1× to 16×, undo and live tile counts. Saving rewrites the indexed image
  (keeping the original once), the map and the preview.
- Tile editor: **Tile palette** (keys 0-3) makes the selected tiles use another
  of the export's palettes, keeping the colour numbers or, with "keep the
  look", taking the closest colours of the new palette. **Palettes…** (C)
  opens a window of its own: click a colour to see how many pixels and tile
  places use it (and how many are on shadow tiles), with the image dimmed
  around it and its tiles framed; a sortable table of every colour, with the
  most used, least used and unused ones; and the 512 Mega Drive colours, shown
  normal or as a shadow tile shows them, to replace the selected colour. A
  changed palette is saved to the `.pal` files (the first time keeps
  `name_palN.orig.pal`).
- Tile editor: select one tile and it says how many times the image uses it,
  with every place marked on the grid (blue: same drawing, pink: mirrored).
  "Edit pixels…" (E, or double click) opens a window of its own to paint the
  pixels of the tile with the colours of its palette, in every place that
  uses it (mirrored where the place is mirrored) or only in this one.
- Resize with a size of your own, in the Background and in the Sweep (the
  Sweep had no resize before). Type only the width or only the height and the
  aspect ratio is kept, or untick "keep aspect ratio" and give both sides.
  The method is a choice too: nearest neighbor (the default, adds no colour),
  bilinear, bicubic, lanczos or area. The console asks it in question 7b;
  `--resize 400x`, `--resize x300`, `--resize 400x300` and `--resize-method`
  work in both tools. The old answers `width` (320 wide) and `height` (224
  tall) still work. A sweep keeps the resize in sweep.json, so exporting one
  variant later gives the same picture.
- Sweep: palettes from a file. A `.pal` or `.png` gives the colours and no
  palette is made; every 16 colours make one palette (colour 0 is the backdrop
  slot), up to 4, and several files can be given with `;`. The window reads
  the file at once and says how many palettes it found, or why it could not
  read it. The colours are kept in sweep.json, so exporting one variant later
  uses them even if the file changed. Command line: `--palette-file`.
- Sweep: the brightness gain is no longer always automatic. The window has
  "auto" (and shows the value it would use), a gain of your own (1.00 = no
  change) and a brightness offset; the command line takes `--gain none` too.
  The gallery page shows the gain used.
- Colour weight, in the Sweep and in the Background window: the weight of the
  colour against the lightness in the colour choices of the conversion (100% =
  as before, 0% = lightness only). Lower lets slightly tinted colours of
  the right lightness in, and the leftover colour error, passed on whole, makes
  the neighbours cancel the tint. On a grey cave plane with 4 palettes of
  near-grey pairs at 40%: 27 tones instead of 12 and about 23% less grain.
  Kept in sweep.json and samples.json; `--color-weight` on the command line.
- Grey mode, in the Sweep and in the Background: for a game seen through a
  monochrome filter. Every colour counts only as the grey the filter shows
  (the MD levels as BlastEm outputs them, weighted by Rec.601), in every
  choice of the conversion; any colour of the right grey will do. Kept in
  sweep.json and samples.json; `--grey` in the Sweep, `--grey on` in the
  Background.
- Colour weight 0% no longer breaks the picture. The weight only changed the
  colour of each pixel, and the palette of each tile was still chosen by the
  full colour: at 0% nothing held the colour back, and the tile choice gave
  up the lightness to save it (a dark floor turned into a light green). The
  weight now goes into every colour comparison of the conversion, and the
  numbers it reports.
- The gain takes `none` (= 1.0) in the Background command line too.
- Sweep window: resize and palette side by side, and the four option boxes in
  one row, so the window fits a 1080p screen (it was taller than the screen).
- Sweep: variations. "Variations" in the window (`--variations N`) runs every
  combination N times, each with its own seed (0, 1, 2...): another palette
  search start and another tile merge order, so another take on the same
  task. The gallery shows one row per seed; variations 1 and up are named
  `..._s1`, `..._s2` and export like any variant. The same seed always gives
  the same picture.
- Scroll margin, in the Sweep, the Background and the Tile editor: the
  per-view tile cap counts one more tile column and row, the ones a
  pixel-by-pixel scroll shows at once (41 × 29 tiles in NTSC, 41 × 31 in
  PAL), only on an axis longer than the view. Kept in sweep.json and
  samples.json; the Tile editor opens an export with the view and margin it
  was made with. `--scroll-margin` in the Sweep, `--scroll-margin on` in the
  Background.
- Fixed: exporting one variant of a sweep again could differ from its
  gallery picture when the sweep's first dithering strength was not
  "balanced": the sweep made the palettes with that strength and the first
  tile budget, the export made them without. The check said "differs".
- Fixed: the Sweep ignored the 320×240 (PAL) view for the tile cap. It kept the
  view height it had when it started (224) and passed that to the processes
  that compute the variants, so "max tiles per view" was held on 320×224
  windows only: a 1080 cap gave up to 1160 tiles in a 320×240 view. sweep.json
  also said 224. Sweeps made with PAL before this fix should be run again.
- Tile cap: no more grid of black dots on flat areas. When the per-view cap
  merged two tiles, it could replace a clean tile used many times by a dotted
  one used a few times, step after step, until one dotted tile was repeated
  all over a flat grey and its dots lined up every 8 px. The merge now
  replaces the tile whose loss does less damage in total (cost per use times
  the number of uses). On a distant grey plane, the clean tiles went from 0 to
  140 and the error went down a little; colour images measured the same or
  slightly better.

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
