# ImPRESS Print Works — brand assets

The live logo is derived directly from the preserved original artwork at
`archive/logo-original.jpg`. That archived file is the source of truth and must not be
overwritten or removed. The original `PRINTING SERVICES` tagline baked into the artwork is
intentionally kept.

## Asset map

| File | Use |
| --- | --- |
| `logo-horizontal.jpg` | Customer navigation, auth pages, footer, and small/mobile brand slots. Cropped from the original so the `ImPRESS` wordmark and `PRINTING SERVICES` tagline remain visible. |
| `logo-icon.jpg` | Admin sidebar, admin login marks, and loading states where a compact square slot is required. The original wordmark crop is centred on a square black canvas made from the artwork's own background. |
| `favicon.png`, `favicon.ico` | Browser tab and apple-touch icons generated from the original wordmark on its native black background. |

There are no separate light/dark variants: the artwork keeps its original black background
and light lettering in every theme. Theme switching in `frontend/assets/theme-toggle.js`
only keeps `data-brand-logo` images pointed at these original-derived files.

## Brand rules

- Official name in text/titles: **ImPRESS Print Works** (exact capitalization and spacing).
- The image itself retains its original `ImPRESS` wordmark and `PRINTING SERVICES` tagline.
- Do not stretch, recolour, redraw, replace the lettering, or place the logo on a busy
  background. Use the supplied crops and let CSS size them with `object-fit: contain`.
- Keep `archive/logo-original.jpg` untouched as the backup/source artwork.
