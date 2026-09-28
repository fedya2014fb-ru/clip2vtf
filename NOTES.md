# Technical notes

Things that cost real debugging time. Read the relevant part before changing that area.

## VTF writing

- **Always VTF 7.2.** srctools 2.7 defaults to 7.5, which the TF2 engine rejects ("invalid minor
  version"), and its 7.0/7.1 headers are broken (78 bytes instead of 64) while its own reader accepts
  them. `build_vtf()` refuses to return anything but 7.2.
- **Never verify an encoder with its own decoder.** Every result is decoded with Valve's `vtf2tga.exe`
  (from the TF2 `bin` folder) and compared with the expected image (PSNR, < 30 dB = bad).
- **srctools' DXT1 turns pixels with alpha < 128 black** (1-bit alpha). Before encoding to a format
  without alpha (DXT1, BGR888) the image gets `putalpha(255)`; without it half the picture came out black
  (9 dB).
- Pass `ref=` (average linear colour) to `VTF()`: the default `(0,0,0)` blows out HDR lighting.
- DXT encoding is split into row bands (multiples of 4) and run on a thread pool
  (`_ParallelFormatFuncs` replaces `srctools.vtf._format_funcs`); srctools' Cython libsquish releases the
  GIL. The output is byte-identical to the single-threaded one. The Cython module is imported inside a
  try/except in srctools, so the PyInstaller spec lists it as a hidden import; without it DXT silently
  falls back to pure Python.
- Background removal and colour bleeding use numpy only (`border_connected`, `dilate4`, `bleed_colors`).
  The background mask is bit-identical to the old `scipy.ndimage.label` version; scipy was dropped because
  it made up 69 MB of a 150 MB bundle.

## Hammer++ automation

Hammer has no plugin API. Everything goes through window messages from a background thread.

- **Never send a common-control message that takes a pointer (`SB_*`, `LVM_*`, `TB_GETBUTTON`, …) with a
  buffer from our own process.** Windows only marshals classic messages (`WM_GETTEXT`, `WM_SETTEXT`,
  `CB_*`, `LB_*`). Doing it once crashed Hammer (`EXCEPTION_ACCESS_VIOLATION` in COMCTL32). Such buffers
  are allocated inside Hammer (`RemoteMem`: `VirtualAllocEx` / `ReadProcessMemory`).
- **Never talk to Hammer from the Tk thread.** Hammer answers slowly (60–570 ms per query while it
  renders); a watcher thread fills a state dict that the UI only reads.
- Tool ids (from the stock `hammer_dll.dll` string table): 32813 Selection, 32913 Texture application,
  33008 Apply decals, 33107 Apply overlays. Edit → Properties 32819, Tools → Transform 113, Undo 57643.
- **Texture browser**: "Browse..." is id 1010 on the `Textures` bar; post `WM_COMMAND(BN_CLICKED)`
  (`BM_CLICK` silently fails while the user holds a mouse button). The filter (combo 1269 → edit 1001) only
  reacts to `CBN_EDITCHANGE`, about 1.5 s later. Tiles are listed in **material load order, not
  alphabetically**, so every visible tile is clicked until the "selected" static (1267) shows the wanted
  name; a short name like `i` matches dozens of materials. A material created after the browser was last
  opened is not in that browser window: it is opened and cancelled once first ("priming").
- **Object Properties**: page "Class Info", list 1024, the `texture` row shows the full material path in
  edit id 1; `WM_SETTEXT` + "Apply" (12321) changes it (one undo step). Hammer does not re-project a placed
  decal after its material changes, so it is nudged +1/64 and back through Tools → Transform (radio 1194
  Move, edits 114/115/116, IDOK). 1/64 is exact in floating point, so the position comes back bit-exact.
- **Face Edit Sheet** (Texture application tool), page with Apply 1015 and Fit 1406. A click in
  "Lift+Select" mode selects the face *and* lifts its texture into the current one, so the new texture is
  chosen after the click, then Apply, then Fit.
- **Sign mode (a brush flush against the wall)**, all measured live:
  - the wall point and normal come from a probe `info_overlay`: Hammer fills its read-only
    "Overlay Basis Origin / Normal" keys (Object Properties → Class Info); the probe is undone;
  - the sign is written as a prefab into `PrefabDir` (from `hammerplusplus_gameconfig.txt`) under a
    `clip2vtf` sub-folder. Hammer++ rescans that folder **only when its window gets activated**, so
    after writing the file Hammer is deactivated and activated again;
  - Entity tool (32816) + category/object picked in the "New Objects" bar (combos 1010 / 1007) + a click
    in the 3D view puts the prefab's origin at the clicked point. With Snap to Grid (menu id **32863**,
    not 32853) x and y are rounded to 32 units at grid 64; without it to whole units, z untouched.
    Snap is switched off for the click and restored, and the prefab is built relative to
    `(round(x), round(y), z)`;
  - textures move with the brush on insertion regardless of Texture Lock (32956), so the front-face
    UV is computed in the prefab's local coordinates;
  - Transform → Teleport did not move the selection when driven from outside, and "Insert original
    prefab" (1219) inserts at the marker, not at the file's coordinates - neither is used;
  - plane winding: `(b - a) × (c - a)` points into the brush.
- **No flicker**: an out-of-context `SetWinEventHook` (`EVENT_OBJECT_CREATE..SHOW`) makes every top-level
  window Hammer creates during an operation layered + alpha 0 + click-through, and restores the original
  styles afterwards. Restoring is required: Hammer reuses its dialogs.
- **Real clicks**: one `SendInput` with MOVE+DOWN+UP in `ABSOLUTE|VIRTUALDESK` coordinates, so the user's
  mouse movement cannot slip in between. The process is per-monitor DPI aware, otherwise cursor, window
  rects and `SendInput` disagree at 125 %. A second click at the same point waits `GetDoubleClickTime()`,
  because a double click opens Object Properties.
- **Drop zone only for real drags**: OLE drag & drop shows ole32's own cursors, while a mouse button held
  in another app (dragging a window, selecting text) shows standard system cursors; the zone appears only
  when `GetCursorInfo` reports a non-standard cursor.
- Audit placed decals after the fact by grepping Hammer's autosaves (`bin\x64\Autosaves`) for
  `"texture" "..."`.

## Tk on Windows

- Every Tk widget is its own HWND and repaints separately (~1.6 ms each). The settings pages, the side
  panel and the status bar are drawn on single canvases; only entries/combos are real widgets. Hidden
  pages are `grid_remove`d.
- Minimize is faked (the window becomes transparent and click-through at the bottom of the z-order), so
  restoring it repaints nothing. `WS_EX_COMPOSITED` on a Tk window makes Tk repaint forever — don't.
- tkinterdnd2's methods exist only on `BaseWidget`, not on `tk.Tk`; register the drop target on a frame.
  The registration order of types is the priority order.
- Tk's clipboard is lost when the program exits; text meant to be pasted later is written with WinAPI.
- Hotkeys are dispatched by virtual-key code, not keysym, so they work on non-Latin keyboard layouts.

## Translations

The Russian source text is the key (`tr("...")`); `lang/<code>.json` maps it to a translation, English is
the fallback. Setting values (fit, format, …) are stored in their Russian form and only translated for
display, so saved settings survive a language change. The language is fixed at start-up (all labels are
built at import), so switching restarts the program and hands the loaded picture to the new instance.
Segoe UI has no CJK glyphs, so Chinese uses Microsoft YaHei UI everywhere; otherwise Windows picks a
different fallback font per glyph.
