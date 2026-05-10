(() => {
  const cfg = window.ACEDRAW_CONFIG;
  const tileSize = cfg.tileSize;
  const maxZoom = cfg.maxZoom;

  const TILE_REQUEST_DEBOUNCE_MS = 90;
  const CURSOR_SEND_INTERVAL_MS = 60;
  const MIN_STROKE_STEP_PX = 1.5;
  const MAX_TILE_CACHE = 1400;

  const canvas = document.getElementById("drawCanvas");
  const ctx = canvas.getContext("2d");

  const penTool = document.getElementById("penTool");
  const eraserTool = document.getElementById("eraserTool");
  const fillTool = document.getElementById("fillTool");
  const lineTool = document.getElementById("lineTool");
  const squareTool = document.getElementById("squareTool");
  const clearButton = document.getElementById("clearButton");
  const penColor = document.getElementById("penColor");
  const brushSize = document.getElementById("brushSize");
  const brushSizeLabel = document.getElementById("brushSizeLabel");
  const zoomSlider = document.getElementById("zoomLevel");
  const zoomLabel = document.getElementById("zoomLabel");

  let tool = "pen";
  let zoom = parseFloat(zoomSlider.value);
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

  const camera = { x: 0, y: 0 };

  const tileStore = new Map();
  const dirtyKeys = new Set();
  const pendingOverlays = new Map();
  let activeOverlay = null;

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
    const tileZ = Math.floor(z);
    const span = tileWorldSpan(tileZ);

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
    const tileZ = Math.floor(zoom);
    for (let x = b.minX; x <= b.maxX; x += 1) {
      for (let y = b.minY; y <= b.maxY; y += 1) {
        if (dirtyKeys.has(keyFor(tileZ, x, y))) {
          return true;
        }
      }
    }
    return false;
  }

  function drawFallbackFromAncestor(z, x, y, dx, dy, dw, dh) {
    for (let ancestorZ = z - 1; ancestorZ >= 0; ancestorZ -= 1) {
      const factor = 2 ** (z - ancestorZ);
      const ancestorX = Math.floor(x / factor);
      const ancestorY = Math.floor(y / factor);
      const ancestor = tileStore.get(keyFor(ancestorZ, ancestorX, ancestorY));
      if (!ancestor || !ancestor.image) {
        continue;
      }

      const localX = x - (ancestorX * factor);
      const localY = y - (ancestorY * factor);
      const srcSpan = tileSize / factor;
      const sx = localX * srcSpan;
      const sy = localY * srcSpan;
      
      ctx.save();
      ctx.beginPath();
      ctx.rect(dx, dy, dw, dh);
      ctx.clip();
      
      const scaleX = dw / srcSpan;
      const scaleY = dh / srcSpan;
      const drawX = dx - (sx * scaleX);
      const drawY = dy - (sy * scaleY);
      const drawW = tileSize * scaleX;
      const drawH = tileSize * scaleY;
      
      ctx.drawImage(ancestor.image, drawX, drawY, drawW, drawH);
      ctx.restore();
      return true;
    }

    return false;
  }

  function drawOverlay(overlay) {
    if (!overlay || Math.floor(overlay.z) !== Math.floor(zoom) || overlay.points.length === 0) {
      return;
    }

    ctx.save();
    ctx.lineWidth = overlay.size;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    if (overlay.tool === "eraser") {
      ctx.globalCompositeOperation = "destination-out";
      ctx.strokeStyle = "rgba(0,0,0,1)";
    } else {
      ctx.globalCompositeOperation = "source-over";
      ctx.strokeStyle = overlay.color;
    }

    if (overlay.points.length === 1) {
      const p = worldToScreen(overlay.points[0].x, overlay.points[0].y);
      ctx.beginPath();
      ctx.arc(p.x, p.y, overlay.size / 2, 0, Math.PI * 2);
      if (overlay.tool === "eraser") {
        ctx.fillStyle = "rgba(0,0,0,1)";
      } else {
        ctx.fillStyle = overlay.color;
      }
      ctx.fill();
      ctx.restore();
      return;
    }

    ctx.beginPath();

    if (overlay.tool === "square" && overlay.points.length === 2) {
      // Points is just [start, current] for UI overlay. 
      // We draw it as a square here.
      const start = worldToScreen(overlay.points[0].x, overlay.points[0].y);
      const end = worldToScreen(overlay.points[1].x, overlay.points[1].y);
      ctx.moveTo(start.x, start.y);
      ctx.lineTo(end.x, start.y);
      ctx.lineTo(end.x, end.y);
      ctx.lineTo(start.x, end.y);
      ctx.closePath();
    } else {
      // Normal stroke or line tool
      const start = worldToScreen(overlay.points[0].x, overlay.points[0].y);
      ctx.moveTo(start.x, start.y);

      for (let i = 1; i < overlay.points.length; i += 1) {
        const p = worldToScreen(overlay.points[i].x, overlay.points[i].y);
        ctx.lineTo(p.x, p.y);
      }
    }

    ctx.stroke();
    ctx.restore();
  }

  function render() {
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);

    const tileZ = Math.floor(zoom);
    const tiles = visibleTiles(zoom);
    const screenSpan = tileWorldSpan(tileZ) * scaleForZoom(zoom);
    for (const t of tiles) {
      const key = keyFor(tileZ, t.x, t.y);
      const tile = tileStore.get(key);
      const span = tileWorldSpan(tileZ);
      const worldX = t.x * span;
      const worldY = t.y * span;
      const p = worldToScreen(worldX, worldY);

      if (tile && tile.image) {
        ctx.drawImage(tile.image, p.x, p.y, screenSpan, screenSpan);
      } else {
        drawFallbackFromAncestor(tileZ, t.x, t.y, p.x, p.y, screenSpan, screenSpan);
      }
    }

    for (const overlay of pendingOverlays.values()) {
      drawOverlay(overlay);
    }
    drawOverlay(activeOverlay);
  }

  function pruneTileCache() {
    if (tileStore.size <= MAX_TILE_CACHE) return;
    const nowBounds = getVisibleBounds(zoom);

    for (const [key, tile] of tileStore.entries()) {
      const keepZoom = Math.abs(tile.z - Math.floor(zoom)) <= 2;
      const keepX = tile.x >= nowBounds.minX - 3 && tile.x <= nowBounds.maxX + 3;
      const keepY = tile.y >= nowBounds.minY - 3 && tile.y <= nowBounds.maxY + 3;

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
    if (
      existing &&
      existing.mtime === tile.mtime &&
      existing.version === tile.version &&
      existing.image
    ) {
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
        version: tile.version || 0,
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
    if (!tiles || tiles.length === 0) {
      return;
    }

    let needsVisibleRefresh = false;
    const b = getVisibleBounds(zoom);

    for (const tile of tiles) {
      const key = keyFor(tile.z, tile.x, tile.y);
      dirtyKeys.add(key);

      const existing = tileStore.get(key);
      if (existing) {
        existing.mtime = tile.mtime;
        existing.version = tile.version || existing.version || 0;
        existing.image = null;
        tileStore.set(key, existing);
      } else {
        tileStore.set(key, {
          z: tile.z,
          x: tile.x,
          y: tile.y,
          mtime: tile.mtime,
          version: tile.version || 0,
          image: null,
        });
      }

      if (
        tile.z === Math.floor(zoom) &&
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
    const tileZ = Math.floor(zoom);
    const signature = `${tileZ}:${b.minX}:${b.maxX}:${b.minY}:${b.maxY}`;
    if (!force && signature === lastViewportSignature && !hasDirtyVisibleTiles()) {
      return;
    }

    const tiles = [];
    for (let x = b.minX; x <= b.maxX; x += 1) {
      for (let y = b.minY; y <= b.maxY; y += 1) {
        const k = keyFor(tileZ, x, y);
        const existing = tileStore.get(k);
        const knownMtime = dirtyKeys.has(k) ? 0 : (existing ? existing.mtime : 0);
        const knownVersion = dirtyKeys.has(k) ? 0 : (existing ? existing.version || 0 : 0);
        tiles.push({ x, y, known_mtime: knownMtime, known_version: knownVersion });
      }
    }

    lastViewportSignature = signature;
    pendingTileRequest = true;
    requestSeq += 1;
    sendWs({
      type: "request_tiles",
      request_id: `tiles-${requestSeq}`,
      z: tileZ,
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

  function finalizeActiveStroke() {
    if (!activeOverlay) return;

    const points = activeOverlay.points.slice();
    const overlay = activeOverlay;
    activeOverlay = null;
    isDrawing = false;

    if (points.length < 2) {
      queueRender();
      return;
    }

    let finalPoints = points;
    if (overlay.tool === "square" && points.length === 2) {
      // Expand into 5 points for the backend to trace the square outline
      const start = points[0];
      const end = points[1];
      finalPoints = [
        start,
        { x: end.x, y: start.y },
        end,
        { x: start.x, y: end.y },
        start
      ];
    }

    requestSeq += 1;
    const requestId = `stroke-${requestSeq}`;
    const pending = {
      ...overlay,
      points: finalPoints,
      requestId,
    };
    pendingOverlays.set(requestId, pending);

    sendWs({
      type: "stroke",
      request_id: requestId,
      tool: pending.tool,
      color: pending.color,
      size: pending.size,
      z: pending.z,
      points: pending.points,
    });

    queueRender();
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

      if (msg.type === "stroke_result") {
        for (const tile of msg.updated || []) {
          installTile(tile);
        }
        markInvalidated(msg.invalidated || []);

        const requestId = msg.request_id;
        if (requestId && pendingOverlays.has(requestId)) {
          pendingOverlays.delete(requestId);
          scheduleTileRequest(true, true);
          queueRender();
        }
      }

      if (msg.type === "fill_result") {
        for (const tile of msg.updated || []) {
          installTile(tile);
        }
        markInvalidated(msg.invalidated || []);
        scheduleTileRequest(true, true);
        queueRender();
      }

      if (msg.type === "tiles_changed") {
        for (const tile of msg.updated || []) {
          installTile(tile);
        }
        markInvalidated(msg.invalidated || []);
      }

      if (msg.type === "tiles_cleared") {
        clearAllTiles();
      }
    });
  }

  function setTool(newTool) {
    tool = newTool;
    penTool.classList.toggle("active", tool === "pen");
    eraserTool.classList.toggle("active", tool === "eraser");
    fillTool.classList.toggle("active", tool === "fill");
    lineTool.classList.toggle("active", tool === "line");
    squareTool.classList.toggle("active", tool === "square");
  }

  penTool.addEventListener("click", () => setTool("pen"));
  eraserTool.addEventListener("click", () => setTool("eraser"));
  fillTool.addEventListener("click", () => setTool("fill"));
  lineTool.addEventListener("click", () => setTool("line"));
  squareTool.addEventListener("click", () => setTool("square"));

  penColor.addEventListener("input", () => {
    color = penColor.value;
  });

  brushSize.addEventListener("input", () => {
    size = parseInt(brushSize.value, 10);
    brushSizeLabel.textContent = `${size} px`;
  });

  zoomSlider.addEventListener("input", () => {
    zoom = parseFloat(zoomSlider.value);
    zoomLabel.textContent = `Layer ${zoom.toFixed(1)}`;
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

    const world = screenToWorld(event.clientX, event.clientY);

    if (tool === "fill") {
      // Handle fill as a single click
      requestSeq += 1;
      const requestId = `fill-${requestSeq}`;
      sendWs({
        type: "fill",
        request_id: requestId,
        color: color,
        z: Math.floor(zoom),
        x: world.x,
        y: world.y,
      });
      return;
    }

    isDrawing = true;
    activeOverlay = {
      tool,
      color,
      size,
      z: Math.floor(zoom),
      points: [world],
    };
    queueRender();
  });

  canvas.addEventListener("mousemove", (event) => {
    const world = screenToWorld(event.clientX, event.clientY);

    const now = performance.now();
    if (now - lastCursorSentAt >= CURSOR_SEND_INTERVAL_MS) {
      lastCursorSentAt = now;
      sendWs({
        type: "cursor",
        cursor: { z: Math.floor(zoom), x: world.x, y: world.y },
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

    if (!isDrawing || !activeOverlay) return;

    if (activeOverlay.tool === "line" || activeOverlay.tool === "square") {
      // For line and square tools, only keep the start and current point
      if (activeOverlay.points.length === 1) {
        activeOverlay.points.push(world);
      } else {
        activeOverlay.points[1] = world;
      }
      queueRender();
      return;
    }

    const last = activeOverlay.points[activeOverlay.points.length - 1];
    const minWorldStep = MIN_STROKE_STEP_PX / scaleForZoom(zoom);
    const dx = world.x - last.x;
    const dy = world.y - last.y;
    if ((dx * dx) + (dy * dy) < (minWorldStep * minWorldStep)) {
      return;
    }

    activeOverlay.points.push(world);
    queueRender();
  });

  function clearAllTiles() {
    tileStore.clear();
    dirtyKeys.clear();
    pendingOverlays.clear();
    activeOverlay = null;
    lastViewportSignature = "";
    queueRender();
    scheduleTileRequest(true, true);
  }

  function stopInteractions() {
    if (isDrawing) {
      finalizeActiveStroke();
    }

    isPanning = false;
    panStart = null;
  }

  clearButton.addEventListener("click", async () => {
    clearButton.disabled = true;
    try {
      const response = await fetch("/clear", { method: "POST" });
      if (!response.ok) {
        throw new Error(`Clear failed: ${response.status}`);
      }
      clearAllTiles();
    } catch (error) {
      console.error("Unable to clear tiles", error);
    } finally {
      clearButton.disabled = false;
    }
  });

  canvas.addEventListener("mouseup", stopInteractions);
  canvas.addEventListener("mouseleave", stopInteractions);
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      
      const oldWorld = screenToWorld(event.clientX, event.clientY);
      
      // Adjust zoom speed
      const delta = event.deltaY > 0 ? -0.2 : 0.2;
      const prevZoom = zoom;
      zoom = Math.max(0, Math.min(maxZoom, zoom + delta));
      if (zoom === prevZoom) return;

      const newWorld = screenToWorld(event.clientX, event.clientY);
      camera.x -= (newWorld.x - oldWorld.x);
      camera.y -= (newWorld.y - oldWorld.y);

      zoomSlider.value = zoom.toFixed(1);
      zoomLabel.textContent = `Layer ${zoom.toFixed(1)}`;
      queueRender();
      scheduleTileRequest(true, true);
    },
    { passive: false },
  );

  const keysPressed = new Set();
  let wasdTimer = null;
  let velX = 0;
  let velY = 0;

  function updateWASD() {
    let ax = 0;
    let ay = 0;
    
    if (keysPressed.has('w')) ay -= 1;
    if (keysPressed.has('s')) ay += 1;
    if (keysPressed.has('a')) ax -= 1;
    if (keysPressed.has('d')) ax += 1;

    // Normalize diagonal movement
    if (ax !== 0 && ay !== 0) {
      const len = Math.sqrt(ax * ax + ay * ay);
      ax /= len;
      ay /= len;
    }

    const scale = scaleForZoom(zoom);
    const accelSpeed = 2 / scale;
    
    velX += ax * accelSpeed;
    velY += ay * accelSpeed;

    // Friction
    velX *= 0.82;
    velY *= 0.82;

    // Stop micro-movements
    const stopThreshold = 0.05 / scale;
    if (Math.abs(velX) < stopThreshold) velX = 0;
    if (Math.abs(velY) < stopThreshold) velY = 0;

    let moved = false;
    if (velX !== 0 || velY !== 0) {
      camera.x += velX;
      camera.y += velY;
      moved = true;
    }

    if (moved) {
      queueRender();
      scheduleTileRequest(false, false);
    }

    if (keysPressed.size > 0 || velX !== 0 || velY !== 0) {
      wasdTimer = requestAnimationFrame(updateWASD);
    } else {
      wasdTimer = null;
    }
  }

  window.addEventListener("keydown", (e) => {
    const key = e.key.toLowerCase();
    if (['w', 'a', 's', 'd'].includes(key)) {
      keysPressed.add(key);
      if (!wasdTimer) {
        wasdTimer = requestAnimationFrame(updateWASD);
      }
    }
  });

  window.addEventListener("keyup", (e) => {
    keysPressed.delete(e.key.toLowerCase());
  });

  window.addEventListener("resize", ensureCanvasSize);

  ensureCanvasSize();
  connectWs();
})();