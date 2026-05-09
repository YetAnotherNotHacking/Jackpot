# AceDraw

AceDraw is a Flask + WebSocket drawing app with tile-based infinite-style zoom editing.

## What this implementation includes

1. Flask backend with WebSocket edit streaming (`/ws`) and HTTP tile serving (`/tile/<z>/<x>/<y>.png`).
2. Pen + eraser tools, adjustable size, adjustable pen color, left toolbar UI, and large drawing area.
3. Sparse PNG tile storage under a z/x-y style hierarchy:

```text
tiles/
  z0/
    0-0.png
  z1/
    0-0.png
```
4. Local-first drawing UX:
   - strokes render immediately in the browser,
   - server commit happens on stroke end,
   - tiles refresh after commit.
5. Map-style zoom behavior:
   - smooth fractional zooming,
   - overzoom while moving between levels,
   - auto-upgrade to sharper tiles when higher LoD is available.
6. Parent/child inheritance:
   - missing child tiles are derived from nearest ancestor tile,
   - edits at a zoom level propagate to deeper zoom levels (full color, no weighting).

## Run with Docker

```bash
docker compose up --build
```

Open: `http://localhost:5000`

### Performance knobs

- `MAX_DESCENDANT_DEPTH` (default `2`): how many deeper zoom levels are updated when drawing.
- `MAX_ZOOM` (default `10`): deepest zoom level.

## Controls

- Left click: draw
- Right click or middle click drag: pan
- Mouse wheel / zoom slider: zoom
- Toolbar: pen/eraser, pen color, brush size
