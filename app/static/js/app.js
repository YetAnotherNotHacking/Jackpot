(() => {
  const cfg = window.ACEDRAW_CONFIG;
  const tileSize = cfg.tileSize;
  const maxZoom = cfg.maxZoom;

  const TILE_REQUEST_DEBOUNCE_MS = 90;
  const CURSOR_SEND_INTERVAL_MS = 60;
  const STROKE_FLUSH_MS = 45;
  const MIN_STROKE_STEP_PX = 1.5;
  const MAX_TILE_CACHE = 1400;

  const canvas = document.getElementById("drawCanvas");
  const ctx = canvas.getContext("2d");

  const penTool = document.getElementById("penTool");
  const eraserTool = document.getElementById("eraserTool");
  const penColor = document.getElementById("penColor");
  const brushSize = document.getElementById("brushSize");
  const brushSizeLabel = document.getElementById("brushSizeLabel");
  const zoomSlider = document.getElementById("zoomLevel");
  const zoomLabel = document.getElementById("zoomLabel");

  let tool = "pen";
  let zoom = parseInt(zoomSlider.value, 10);
  let color = penColor.value;
  let size = parseInt(brushSize.value, 10);

  let isDrawing = false;
  let isPanning = false;
  let panStart = null;

  let ws = null;
  let wsOpen = false;

  let requestSeq = 0;
  let renderQueued = false;
  let tileRequestTimer = null;
  let pendingTileRequest = false;
  let queuedTileRequest = false;
  let queuedTileRequestForce = false;
  let lastViewportSignature = "";

  let lastCursorSentAt = 0;
  let strokeBuffer = [];
  let strokeFlushTimer = null;

  const camera = { x: 0, y: 0 };

  const tileStore = new Map();
  const dirtyKeys = new Set();

  function keyFor(z, x, y) {
    return `${z}:${x}:${y}`;
  }

  function scaleForZoom(z) {
    return 2 ** z;
  }

  function tileWorldSpan(z) {
    return tileSize / (2 ** z);
  }

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      render();
    });
  }

  function ensureCanvasSize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    queueRender();
    scheduleTileRequest(true, true);
  }

  function screenToWorld(sx, sy) {
    const rect = canvas.getBoundingClientRect();
    const px = sx - rect.left;
    const py = sy - rect.top;
    const scale = scaleForZoom(zoom);
    return {
      x: camera.x + (px - rect.width / 2) / scale,
      y: camera.y + (py - rect.height / 2) / scale,
    };
  }

  function worldToScreen(wx, wy) {
    const rect = canvas.getBoundingClientRect();
    const scale = scaleForZoom(zoom);
    return {
      x: (wx - camera.x) * scale + rect.width / 2,
      y: (wy - camera.y) * scale + rect.height / 2,
    };
  }

  function getVisibleBounds(z) {
    const rect = canvas.getBoundingClientRect();
    const scale = scaleForZoom(z);
    const span = tileWorldSpan(z);

    const minWorldX = camera.x + (0 - rect.width / 2) / scale;
    const maxWorldX = camera.x + (rect.width - rect.width / 2) / scale;
    const minWorldY = camera.y + (0 - rect.height / 2) / scale;
    const maxWorldY = camera.y + (rect.height - rect.height / 2) / scale;

    return {
      minX: Math.floor(minWorldX / span) - 1,
      maxX: Math.floor(maxWorldX / span) + 1,
      minY: Math.floor(minWorldY / span) - 1,
      maxY: Math.floor(maxWorldY / span) + 1,
    };
  }

  function visibleTiles(z) {
    const b = getVisibleBounds(z);
    const tiles = [];
    for (let x = b.minX; x <= b.maxX; x += 1) {
      for (let y = b.minY; y <= b.maxY; y += 1) {
        tiles.push({ x, y });
      }
    }
    return tiles;
  }

  function hasDirtyVisibleTiles() {
    const b = getVisibleBounds(zoom);
    for (let x = b.minX; x <= b.maxX; x += 1) {
      for (let y = b.minY; y <= b.maxY; y += 1) {
        if (dirtyKeys.has(keyFor(zoom, x, y))) {
          return true;
        }
      }
    }
    return false;
  }

  function render() {
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);

    const tiles = visibleTiles(zoom);
    const screenSpan = tileWorldSpan(zoom) * scaleForZoom(zoom);
    for (const t of tiles) {
      const key = keyFor(zoom, t.x, t.y);
      const tile = tileStore.get(key);
      if (!tile || !tile.image) continue;

      const span = tileWorldSpan(zoom);
      const worldX = t.x * span;
      const worldY = t.y * span;
      const p = worldToScreen(worldX, worldY);
      ctx.drawImage(tile.image, p.x, p.y, screenSpan, screenSpan);
    }
  }

  function pruneTileCache() {
    if (tileStore.size <= MAX_TILE_CACHE) return;
    const nowBounds = getVisibleBounds(zoom);

    for (const [key, tile] of tileStore.entries()) {
      const keepZoom = Math.abs(tile.z - zoom) <= 1;
      const keepX = tile.x >= nowBounds.minX - 2 && tile.x <= nowBounds.maxX + 2;
      const keepY = tile.y >= nowBounds.minY - 2 && tile.y <= nowBounds.maxY + 2;

      if (!(keepZoom && keepX && keepY)) {
        tileStore.delete(key);
        dirtyKeys.delete(key);
      }

      if (tileStore.size <= MAX_TILE_CACHE) {
        return;
      }
    }
  }

  function installTile(tile) {
    const key = keyFor(tile.z, tile.x, tile.y);
    const existing = tileStore.get(key);
    if (existing && existing.mtime === tile.mtime && existing.image) {
      dirtyKeys.delete(key);
      return;
    }

    const image = new Image();
    image.onload = () => {
      tileStore.set(key, {
        z: tile.z,
        x: tile.x,
        y: tile.y,
        mtime: tile.mtime,
        image,
      });
      dirtyKeys.delete(key);
      pruneTileCache();
      queueRender();
    };

    image.onerror = () => {
      tileStore.delete(key);
    };

    image.src = tile.url;
  }

  function markInvalidated(tiles) {
    let needsVisibleRefresh = false;
    const b = getVisibleBounds(zoom);

    for (const tile of tiles) {
      const key = keyFor(tile.z, tile.x, tile.y);
      dirtyKeys.add(key);

      const existing = tileStore.get(key);
      if (existing) {
        existing.mtime = tile.mtime;
        existing.image = null;
        tileStore.set(key, existing);
      } else {
        tileStore.set(key, {
          z: tile.z,
          x: tile.x,
          y: tile.y,
          mtime: tile.mtime,
          image: null,
        });
      }

      if (
        tile.z === zoom &&
        tile.x >= b.minX &&
        tile.x <= b.maxX &&
        tile.y >= b.minY &&
        tile.y <= b.maxY
      ) {
        needsVisibleRefresh = true;
      }
    }

    if (needsVisibleRefresh) {
      scheduleTileRequest(true, false);
    }
  }

  function sendWs(payload) {
    if (!wsOpen || !ws) return;
    ws.send(JSON.stringify(payload));
  }

  function requestVisibleTiles(force = false) {
    if (!wsOpen || pendingTileRequest) return;

    const b = getVisibleBounds(zoom);
    const signature = `${zoom}:${b.minX}:${b.maxX}:${b.minY}:${b.maxY}`;
    if (!force && signature === lastViewportSignature && !hasDirtyVisibleTiles()) {
      return;
    }

    const tiles = [];
    for (let x = b.minX; x <= b.maxX; x += 1) {
      for (let y = b.minY; y <= b.maxY; y += 1) {
        const k = keyFor(zoom, x, y);
        const existing = tileStore.get(k);
        const knownMtime = dirtyKeys.has(k) ? 0 : (existing ? existing.mtime : 0);
        tiles.push({ x, y, known_mtime: knownMtime });
      }
    }

    lastViewportSignature = signature;
    pendingTileRequest = true;
    requestSeq += 1;
    sendWs({
      type: "request_tiles",
      request_id: `tiles-${requestSeq}`,
      z: zoom,
      tiles,
    });
  }

  function scheduleTileRequest(force = false, immediate = false) {
    if (force) {
      queuedTileRequestForce = true;
    }

    if (pendingTileRequest) {
      queuedTileRequest = true;
      return;
    }

    if (immediate) {
      if (tileRequestTimer) {
        clearTimeout(tileRequestTimer);
        tileRequestTimer = null;
      }
      requestVisibleTiles(force || queuedTileRequestForce);
      queuedTileRequestForce = false;
      return;
    }

    if (tileRequestTimer) return;

    tileRequestTimer = setTimeout(() => {
      tileRequestTimer = null;
      requestVisibleTiles(force || queuedTileRequestForce);
      queuedTileRequestForce = false;
    }, TILE_REQUEST_DEBOUNCE_MS);
  }

  function flushStrokeBuffer() {
    if (!wsOpen || strokeBuffer.length < 2) return;

    const points = strokeBuffer.slice();
    strokeBuffer = [strokeBuffer[strokeBuffer.length - 1]];

    requestSeq += 1;
    sendWs({
      type: "stroke",
      request_id: `stroke-${requestSeq}`,
      tool,
      color,
      size,
      z: zoom,
      points,
    });
  }

  function connectWs() {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${window.location.host}/ws`);

    ws.addEventListener("open", () => {
      wsOpen = true;
      pendingTileRequest = false;
      queuedTileRequest = false;
      queuedTileRequestForce = false;
      lastViewportSignature = "";
      scheduleTileRequest(true, true);
    });

    ws.addEventListener("close", () => {
      wsOpen = false;
      pendingTileRequest = false;
      setTimeout(connectWs, 1000);
    });

    ws.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }

      if (msg.type === "tiles_response") {
        pendingTileRequest = false;
        for (const tile of msg.tiles || []) {
          installTile(tile);
        }

        if (queuedTileRequest) {
          const force = queuedTileRequestForce;
          queuedTileRequest = false;
          queuedTileRequestForce = false;
          scheduleTileRequest(force, false);
        }
      }

      if (msg.type === "stroke_result" || msg.type === "tiles_changed") {
        for (const tile of msg.updated || []) {
          installTile(tile);
        }
        markInvalidated(msg.invalidated || []);
      }
    });
  }

  penTool.addEventListener("click", () => {
    tool = "pen";
    penTool.classList.add("active");
    eraserTool.classList.remove("active");
  });

  eraserTool.addEventListener("click", () => {
    tool = "eraser";
    eraserTool.classList.add("active");
    penTool.classList.remove("active");
  });

  penColor.addEventListener("input", () => {
    color = penColor.value;
  });

  brushSize.addEventListener("input", () => {
    size = parseInt(brushSize.value, 10);
    brushSizeLabel.textContent = `${size}px`;
  });

  zoomSlider.addEventListener("input", () => {
    zoom = parseInt(zoomSlider.value, 10);
    zoomLabel.textContent = `z${zoom}`;
    queueRender();
    scheduleTileRequest(true, true);
  });

  canvas.addEventListener("mousedown", (event) => {
    if (event.button === 2 || event.button === 1) {
      isPanning = true;
      panStart = { x: event.clientX, y: event.clientY };
      return;
    }

    if (event.button !== 0) return;

    isDrawing = true;
    strokeBuffer = [screenToWorld(event.clientX, event.clientY)];
    if (!strokeFlushTimer) {
      strokeFlushTimer = setInterval(flushStrokeBuffer, STROKE_FLUSH_MS);
    }
  });

  canvas.addEventListener("mousemove", (event) => {
    const world = screenToWorld(event.clientX, event.clientY);

    const now = performance.now();
    if (now - lastCursorSentAt >= CURSOR_SEND_INTERVAL_MS) {
      lastCursorSentAt = now;
      sendWs({
        type: "cursor",
        cursor: { z: zoom, x: world.x, y: world.y },
      });
    }

    if (isPanning && panStart) {
      const dx = event.clientX - panStart.x;
      const dy = event.clientY - panStart.y;
      panStart = { x: event.clientX, y: event.clientY };
      const scale = scaleForZoom(zoom);
      camera.x -= dx / scale;
      camera.y -= dy / scale;
      queueRender();
      scheduleTileRequest(false, false);
      return;
    }

    if (!isDrawing || strokeBuffer.length === 0) return;

    const last = strokeBuffer[strokeBuffer.length - 1];
    const minWorldStep = MIN_STROKE_STEP_PX / scaleForZoom(zoom);
    const dx = world.x - last.x;
    const dy = world.y - last.y;
    if ((dx * dx) + (dy * dy) < (minWorldStep * minWorldStep)) {
      return;
    }

    strokeBuffer.push(world);
  });

  function stopInteractions() {
    if (isDrawing) {
      flushStrokeBuffer();
    }
    isDrawing = false;
    isPanning = false;
    panStart = null;
    strokeBuffer = [];

    if (strokeFlushTimer) {
      clearInterval(strokeFlushTimer);
      strokeFlushTimer = null;
    }
  }

  canvas.addEventListener("mouseup", stopInteractions);
  canvas.addEventListener("mouseleave", stopInteractions);
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const delta = event.deltaY > 0 ? -1 : 1;
      const prevZoom = zoom;
      zoom = Math.max(0, Math.min(maxZoom, zoom + delta));
      if (zoom === prevZoom) return;

      zoomSlider.value = String(zoom);
      zoomLabel.textContent = `z${zoom}`;
      queueRender();
      scheduleTileRequest(true, true);
    },
    { passive: false },
  );

  window.addEventListener("resize", ensureCanvasSize);

  ensureCanvasSize();
  connectWs();
})();
