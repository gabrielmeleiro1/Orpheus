# Icon sources

The app icon is an amphora drawn from lines of text. It is exported from these
vector files; do not edit the PNG, `.ico` or `.icns` files by hand.

Two tile geometries:

- `orpheus-mac-*`: an 824 px tile on a 1024 px canvas (Apple's icon grid), used for `icon.icns`.
- `orpheus-win-*`: the tile fills the canvas, used for `icon.ico`, the PNGs and `public/favicon.png`.

Four levels of detail, chosen by output size so the lines never fall below a pixel:

| File suffix | Output sizes |
| --- | --- |
| `full` | 128 px and larger |
| `medium` | 48 and 64 px |
| `small` | 24 and 32 px |
| `tiny` | 16 px |

`icon.ico` holds 32, 16, 24, 48, 64 and 256 px frames, in that order: Tauri uses
the first frame for the development window, and `tests/test_desktop_branding.py`
checks the set.
