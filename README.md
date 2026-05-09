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

4. Postgres metadata tracking (`tiles` table with `updated_ms` and `version`) for tile invalidation and change detection.
5. Redis for ephemeral cursor presence (`cursor:<client_id>` with TTL).
6. Weighted multi-zoom propagation on edits: drawing on zoom `z` updates `z` plus ancestor zoom layers (`z-1 ... 0`) with reduced brush size and alpha.
7. Client tile cache with per-tile modification times; viewport tile requests send `known_mtime` so server only returns changed tiles.
8. Throughput-oriented sync behavior:
   - local-first stroke preview with single commit on stroke end,
   - throttled viewport tile requests,
   - and capped propagation depth (`MAX_PROPAGATION_DEPTH`) for map-like responsiveness.

## Architecture

- Backend: Flask + `flask-sock`
- WebSocket protocol:
  - `stroke`: streamed edit segments (tool, size, color, zoom, points)
  - `request_tiles`: request visible tiles with known mtimes
  - `cursor`: ephemeral pointer updates
  - `stroke_result` / `tiles_response` / `tiles_changed`: server responses
- Storage:
  - PNG tile files on disk (`./tiles` mounted into app container)
  - Postgres for tile metadata and versioning
  - Redis for short-lived runtime presence data

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

## Notes on feasibility and limits

- This implementation supports unbounded tile coordinates and sparse tile creation (only touched tiles are created), which is the practical way to emulate “infinite” canvases.
- Weighted propagation is implemented as a heuristic (reduced alpha + reduced brush size on ancestor zoom levels). It works and is stable, but it is not a physically perfect mipmap reconstructor.
- Performance tuning:
  - Increase `MAX_PROPAGATION_DEPTH` for stronger cross-zoom detail persistence.
  - Decrease it for faster edits under heavy drawing load.
- If you need mathematically strict multi-resolution reconstruction or massive concurrent collaboration, we should add:
  - server-side job queue for pyramid recomputation,
  - conflict-resolution logic,
  - binary diff/patch protocol for strokes,
  - and stronger batching/locking strategies.

## Local development (optional)

If you run without Docker, you still need Postgres + Redis available:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://acedraw:acedraw@localhost:5432/acedraw'
export REDIS_URL='redis://localhost:6379/0'
python -m app.app
```
