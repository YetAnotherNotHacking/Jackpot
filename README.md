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

## Run with Docker

```bash
docker compose up --build
```

Open: `http://localhost:5000`

## Controls

- Left click: draw
- Right click or middle click drag: pan
- Mouse wheel / zoom slider: zoom
- Toolbar: pen/eraser, pen color, brush size