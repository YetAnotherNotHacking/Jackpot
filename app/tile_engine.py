import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from PIL import Image, ImageDraw

from .database import TileRepository


@dataclass(frozen=True)
class TileCoord:
    z: int
    x: int
    y: int


class TileEngine:
    def __init__(
        self,
        tile_root: str,
        tile_size: int,
        max_zoom: int,
        max_descendant_depth: int,
        repo: TileRepository,
    ):
        self.tile_root = tile_root
        self.tile_size = tile_size
        self.max_zoom = max_zoom
        self.max_descendant_depth = max(0, max_descendant_depth)
        self.repo = repo
        self._lock = threading.RLock()
        os.makedirs(self.tile_root, exist_ok=True)

    def tile_world_span(self, z: int) -> float:
        return self.tile_size / (2 ** z)

    def _tile_path(self, z: int, x: int, y: int, ensure_dir: bool = True) -> str:
        z_dir = os.path.join(self.tile_root, f"z{z}")
        if ensure_dir:
            os.makedirs(z_dir, exist_ok=True)
        return os.path.join(z_dir, f"{x}-{y}.png")

    def _load_or_create(self, z: int, x: int, y: int) -> Image.Image:
        existing = self._load_existing_tile(z, x, y)
        if existing is not None:
            return existing
        return self._build_tile_from_ancestors(z, x, y)

    def _load_existing_tile(self, z: int, x: int, y: int) -> Optional[Image.Image]:
        path = self._tile_path(z, x, y, ensure_dir=False)
        if not os.path.exists(path):
            return None
        try:
            with Image.open(path) as loaded:
                return loaded.convert("RGBA")
        except (OSError, IOError):
            # Corrupted or invalid image file, remove it
            try:
                os.remove(path)
            except OSError:
                pass
            return None

    def _build_tile_from_ancestors(self, z: int, x: int, y: int) -> Image.Image:
        # New high-detail tiles inherit their nearest existing ancestor so zooming
        # into unexplored regions does not appear to "erase" lower-detail content.
        for ancestor_z in range(z - 1, -1, -1):
            factor = 2 ** (z - ancestor_z)
            ancestor_x = math.floor(x / factor)
            ancestor_y = math.floor(y / factor)
            ancestor = self._load_existing_tile(ancestor_z, ancestor_x, ancestor_y)
            if ancestor is None:
                continue

            local_x = x - (ancestor_x * factor)
            local_y = y - (ancestor_y * factor)
            src_span = self.tile_size // factor
            sx = int(local_x * src_span)
            sy = int(local_y * src_span)
            crop = ancestor.crop((sx, sy, sx + src_span, sy + src_span))
            return crop.resize(
                (self.tile_size, self.tile_size),
                Image.Resampling.LANCZOS,
            )

        return Image.new("RGBA", (self.tile_size, self.tile_size), (0, 0, 0, 0))

    def _save_tile(self, z: int, x: int, y: int, image: Image.Image) -> int:
        path = self._tile_path(z, x, y)
        image.save(path, format="PNG")
        return int(time.time() * 1000)

    def clear_tiles(self) -> None:
        with self._lock:
            for root, _, files in os.walk(self.tile_root, topdown=False):
                for filename in files:
                    if filename.endswith(".png"):
                        try:
                            os.remove(os.path.join(root, filename))
                        except FileNotFoundError:
                            pass
                if root != self.tile_root and not os.listdir(root):
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
            self.repo.clear_tiles()

    def _clear_tiles_above_level(self, min_z: int) -> None:
        self.repo.clear_tiles_above_level(min_z)
        # Delete the directories
        for level in range(min_z + 1, self.max_zoom + 1):
            level_dir = os.path.join(self.tile_root, f"z{level}")
            if os.path.exists(level_dir):
                import shutil
                shutil.rmtree(level_dir)

    def _world_to_tile(self, wx: float, wy: float, z: int) -> Tuple[int, int, float, float]:
        span = self.tile_world_span(z)
        tx = math.floor(wx / span)
        ty = math.floor(wy / span)
        local_world_x = wx - (tx * span)
        local_world_y = wy - (ty * span)
        px = (local_world_x / span) * self.tile_size
        py = (local_world_y / span) * self.tile_size
        return tx, ty, px, py

    def _sample_points(self, points: List[Dict], step_world: float) -> Iterable[Tuple[float, float]]:
        if not points:
            return

        if len(points) == 1:
            p = points[0]
            yield float(p["x"]), float(p["y"])
            return

        for idx in range(len(points) - 1):
            p0 = points[idx]
            p1 = points[idx + 1]
            x0, y0 = float(p0["x"]), float(p0["y"])
            x1, y1 = float(p1["x"]), float(p1["y"])
            dist = math.hypot(x1 - x0, y1 - y0)
            samples = max(1, int(dist / max(step_world, 1e-9)))
            for i in range(samples + 1):
                t = i / samples
                yield (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    def _parse_color(self, color_hex: str, alpha: int) -> Tuple[int, int, int, int]:
        value = color_hex.strip().lstrip("#")
        if len(value) != 6:
            value = "000000"
        r = int(value[0:2], 16)
        g = int(value[2:4], 16)
        b = int(value[4:6], 16)
        return (r, g, b, alpha)

    def _draw_brush(
        self,
        image: Image.Image,
        px: float,
        py: float,
        radius_px: float,
        tool: str,
        color: Tuple[int, int, int, int],
        erase_strength: int,
    ) -> None:
        if radius_px <= 0.0:
            return

        bbox = [
            px - radius_px,
            py - radius_px,
            px + radius_px,
            py + radius_px,
        ]

        if tool == "eraser":
            mask = Image.new("L", image.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse(bbox, fill=max(1, min(255, erase_strength)))
            transparent = Image.new("RGBA", image.size, (0, 0, 0, 0))
            image.paste(transparent, (0, 0), mask)
        else:
            draw = ImageDraw.Draw(image)
            draw.ellipse(bbox, fill=color)

    def apply_stroke(self, payload: Dict) -> Dict:
        z = int(payload.get("z", 0))
        z = max(0, min(self.max_zoom, z))
        points = payload.get("points", [])
        tool = payload.get("tool", "pen")
        size = max(1.0, float(payload.get("size", 12)))
        color_hex = payload.get("color", "#111111")

        if not points:
            return {"updated": [], "invalidated": []}

        span_edit = self.tile_world_span(z)
        world_radius = (size / 2.0) * (span_edit / self.tile_size)
        step_world = max((span_edit / self.tile_size) * 1.5, world_radius / 3.0)
        sampled_points = list(self._sample_points(points, step_world))
        if not sampled_points:
            return {"updated": [], "invalidated": []}

        touched: Set[TileCoord] = set()
        tile_cache: Dict[TileCoord, Image.Image] = {}
        color = self._parse_color(color_hex, 255)
        erase_strength = 255

        with self._lock:
            # Only update the drawn zoom level and ancestors (levels below)
            # Descendants inherit on demand - don't materialize them
            for level in range(0, z + 1):
                if level == z:
                    level_size = size
                else:
                    level_scale = 2 ** (z - level)
                    level_size = max(1.0, size / level_scale)

                radius_px = level_size / 2.0
                span = self.tile_world_span(level)
                for wx, wy in sampled_points:
                    min_tx = math.floor((wx - world_radius) / span)
                    max_tx = math.floor((wx + world_radius) / span)
                    min_ty = math.floor((wy - world_radius) / span)
                    max_ty = math.floor((wy + world_radius) / span)

                    for tx in range(min_tx, max_tx + 1):
                        for ty in range(min_ty, max_ty + 1):
                            local_world_x = wx - (tx * span)
                            local_world_y = wy - (ty * span)
                            px = (local_world_x / span) * self.tile_size
                            py = (local_world_y / span) * self.tile_size

                            coord = TileCoord(level, tx, ty)
                            if coord not in tile_cache:
                                # Erasing should start from a rebuilt ancestor state
                                # so deep descendant tiles correctly inherit transparency.
                                if tool == "eraser":
                                    tile_cache[coord] = self._build_tile_from_ancestors(level, tx, ty)
                                else:
                                    tile_cache[coord] = self._load_or_create(level, tx, ty)
                            
                            self._draw_brush(
                                tile_cache[coord],
                                px,
                                py,
                                radius_px,
                                tool,
                                color,
                                erase_strength,
                            )
                            touched.add(coord)

            now_ms = int(time.time() * 1000)
            updates: List[Tuple[int, int, int, int]] = []
            for coord in touched:
                mtime = self._save_tile(coord.z, coord.x, coord.y, tile_cache[coord])
                updates.append((coord.z, coord.x, coord.y, max(mtime, now_ms)))

            rows = self.repo.upsert_tiles(updates)

            # Clear all higher zoom levels to ensure descendants are rebuilt from updated ancestors
            self._clear_tiles_above_level(z)

        result_updates = []
        for row in rows:
            result_updates.append(
                {
                    "z": row["z"],
                    "x": row["x"],
                    "y": row["y"],
                    "mtime": row["updated_ms"],
                    "version": row["version"],
                    "url": f"/tile/{row['z']}/{row['x']}/{row['y']}.png?t={row['updated_ms']}",
                }
            )

        edited_level = [u for u in result_updates if u["z"] == z]
        invalidated = [u for u in result_updates if u["z"] > z]
        return {"updated": edited_level, "invalidated": invalidated}

    def apply_fill(self, payload: Dict) -> Dict:
        z = int(payload.get("z", 0))
        z = max(0, min(self.max_zoom, z))
        wx = float(payload.get("x", 0.0))
        wy = float(payload.get("y", 0.0))
        color_hex = payload.get("color", "#111111")

        fill_color = self._parse_color(color_hex, 255)
        touched: Set[TileCoord] = set()
        tile_cache: Dict[TileCoord, Image.Image] = {}

        with self._lock:
            # Step 1: Get target color at the clicked position at zoom z
            span_z = self.tile_world_span(z)
            tx_z = math.floor(wx / span_z)
            ty_z = math.floor(wy / span_z)
            local_world_x = wx - (tx_z * span_z)
            local_world_y = wy - (ty_z * span_z)
            px_z = int((local_world_x / span_z) * self.tile_size)
            py_z = int((local_world_y / span_z) * self.tile_size)

            target_tile = self._load_or_create(z, tx_z, ty_z)
            if px_z < 0 or px_z >= self.tile_size or py_z < 0 or py_z >= self.tile_size:
                return {"updated": [], "invalidated": []}

            target_color = target_tile.getpixel((px_z, py_z))
            
            # Skip if already the target color
            if target_color[:3] == fill_color[:3]:
                return {"updated": [], "invalidated": []}

            # Step 2: Apply fill at all levels from 0 to z (ancestors and clicked level)
            # This matches how the stroke tool works
            for level in range(0, z + 1):
                if level == z:
                    # At clicked level, use exact coordinates
                    level_span = span_z
                    level_tx = tx_z
                    level_ty = ty_z
                    level_px = px_z
                    level_py = py_z
                    level_target_color = target_color
                else:
                    # At ancestor levels, scale down coordinates
                    level_scale = 2 ** (z - level)
                    level_span = self.tile_world_span(level)
                    level_tx = math.floor(wx / level_span)
                    level_ty = math.floor(wy / level_span)
                    level_local_x = wx - (level_tx * level_span)
                    level_local_y = wy - (level_ty * level_span)
                    level_px = int((level_local_x / level_span) * self.tile_size)
                    level_py = int((level_local_y / level_span) * self.tile_size)
                    
                    # Get target color at this level
                    level_coord = TileCoord(level, level_tx, level_ty)
                    if level_coord not in tile_cache:
                        tile_cache[level_coord] = self._load_or_create(level, level_tx, level_ty)
                    level_tile = tile_cache[level_coord]
                    
                    if (level_px < 0 or level_px >= self.tile_size or 
                        level_py < 0 or level_py >= self.tile_size):
                        continue
                    
                    level_target_color = level_tile.getpixel((level_px, level_py))

                # Skip if already the target color at this level
                if level_target_color[:3] == fill_color[:3]:
                    continue

                # Flood fill at this level across potentially multiple tiles
                filled_tiles = self._flood_fill_across_tiles(
                    level,
                    level_tx,
                    level_ty,
                    level_px,
                    level_py,
                    level_target_color,
                    fill_color,
                    tile_cache
                )
                
                touched.update(TileCoord(level, tx, ty) for tx, ty, _, _ in filled_tiles)

            now_ms = int(time.time() * 1000)
            updates: List[Tuple[int, int, int, int]] = []
            for coord in touched:
                mtime = self._save_tile(coord.z, coord.x, coord.y, tile_cache[coord])
                updates.append((coord.z, coord.x, coord.y, max(mtime, now_ms)))

            rows = self.repo.upsert_tiles(updates)

            # Clear all higher zoom levels to ensure descendants are rebuilt from updated ancestors
            self._clear_tiles_above_level(z)

        result_updates = []
        for row in rows:
            result_updates.append(
                {
                    "z": row["z"],
                    "x": row["x"],
                    "y": row["y"],
                    "mtime": row["updated_ms"],
                    "version": row["version"],
                    "url": f"/tile/{row['z']}/{row['x']}/{row['y']}.png?t={row['updated_ms']}",
                }
            )

        edited_level = [u for u in result_updates if u["z"] == z]
        invalidated = [u for u in result_updates if u["z"] > z]
        return {"updated": edited_level, "invalidated": invalidated}

    def _flood_fill_tile_simple(
        self, 
        image: Image.Image, 
        start_x: int, 
        start_y: int, 
        target_color: Tuple[int, int, int, int], 
        fill_color: Tuple[int, int, int, int]
    ) -> bool:
        """Perform flood fill on a single tile. Returns True if any pixels were changed."""
        if start_x < 0 or start_x >= self.tile_size or start_y < 0 or start_y >= self.tile_size:
            return False

        # Check if start pixel matches target color
        start_pixel = image.getpixel((start_x, start_y))
        if start_pixel[:3] != target_color[:3]:  # Compare RGB only
            return False

        # Use a stack-based flood fill algorithm
        stack = [(start_x, start_y)]
        visited = set()
        changed = False

        while stack:
            x, y = stack.pop()
            if (x, y) in visited:
                continue
            visited.add((x, y))

            if x < 0 or x >= self.tile_size or y < 0 or y >= self.tile_size:
                continue

            pixel = image.getpixel((x, y))
            if pixel[:3] != target_color[:3]:
                continue

            # Fill this pixel
            image.putpixel((x, y), fill_color)
            changed = True

            # Add neighbors
            stack.extend([
                (x + 1, y),
                (x - 1, y), 
                (x, y + 1),
                (x, y - 1)
            ])

        return changed

    def _flood_fill_and_track(
        self,
        image: Image.Image,
        start_x: int,
        start_y: int,
        target_color: Tuple[int, int, int, int],
        fill_color: Tuple[int, int, int, int]
    ) -> Set[Tuple[int, int]]:
        """Perform flood fill and return set of all filled pixel coordinates."""
        if start_x < 0 or start_x >= self.tile_size or start_y < 0 or start_y >= self.tile_size:
            return set()

        # Check if start pixel matches target color
        start_pixel = image.getpixel((start_x, start_y))
        if start_pixel[:3] != target_color[:3]:  # Compare RGB only
            return set()

        # Use a stack-based flood fill algorithm
        stack = [(start_x, start_y)]
        visited = set()
        filled = set()

        while stack:
            x, y = stack.pop()
            if (x, y) in visited:
                continue
            visited.add((x, y))

            if x < 0 or x >= self.tile_size or y < 0 or y >= self.tile_size:
                continue

            pixel = image.getpixel((x, y))
            if pixel[:3] != target_color[:3]:
                continue

            # Fill this pixel
            image.putpixel((x, y), fill_color)
            filled.add((x, y))

            # Add neighbors
            stack.extend([
                (x + 1, y),
                (x - 1, y), 
                (x, y + 1),
                (x, y - 1)
            ])

        return filled

    def _flood_fill_across_tiles(
        self,
        z: int,
        start_tx: int,
        start_ty: int,
        start_px: int,
        start_py: int,
        target_color: Tuple[int, int, int, int],
        fill_color: Tuple[int, int, int, int],
        tile_cache: Dict
    ) -> Set[Tuple[int, int, int, int]]:
        """Flood fill that can span across multiple tiles at the same zoom level.
        Returns set of (tx, ty, px, py) of all filled pixels."""
        span = self.tile_world_span(z)
        
        # Use a queue to process pixels, tracking which tile they're in
        # Queue contains: (tile_x, tile_y, pixel_x, pixel_y)
        queue = [(start_tx, start_ty, start_px, start_py)]
        visited = set()
        filled_pixels = set()

        while queue:
            tx, ty, px, py = queue.pop(0)
            
            if (tx, ty, px, py) in visited:
                continue
            visited.add((tx, ty, px, py))

            # Load tile if not in cache
            coord = TileCoord(z, tx, ty)
            if coord not in tile_cache:
                tile_cache[coord] = self._load_or_create(z, tx, ty)
            
            tile = tile_cache[coord]
            
            # Check bounds within tile
            if px < 0 or px >= self.tile_size or py < 0 or py >= self.tile_size:
                continue
            
            # Get pixel color
            try:
                pixel = tile.getpixel((px, py))
            except (IndexError, OSError):
                continue
            
            # Check if matches target color
            if pixel[:3] != target_color[:3]:
                continue
            
            # Fill the pixel
            tile.putpixel((px, py), fill_color)
            filled_pixels.add((tx, ty, px, py))
            
            # Add neighbors (may cross tile boundaries)
            neighbors = [
                (tx, ty, px + 1, py),
                (tx, ty, px - 1, py),
                (tx, ty, px, py + 1),
                (tx, ty, px, py - 1)
            ]
            
            # Check if neighbors cross tile boundaries
            for nx, ny, npx, npy in neighbors:
                # Handle tile wrapping at boundaries
                if npx >= self.tile_size:
                    nx += 1
                    npx -= self.tile_size
                elif npx < 0:
                    nx -= 1
                    npx += self.tile_size
                
                if npy >= self.tile_size:
                    ny += 1
                    npy -= self.tile_size
                elif npy < 0:
                    ny -= 1
                    npy += self.tile_size
                
                if (nx, ny, npx, npy) not in visited:
                    queue.append((nx, ny, npx, npy))
        
        return filled_pixels

    def diff_visible_tiles(self, z: int, requested: List[Dict]) -> List[Dict]:
        z = max(0, min(self.max_zoom, int(z)))
        coords = [(int(item["x"]), int(item["y"])) for item in requested]
        known = {
            (int(item["x"]), int(item["y"])): int(item.get("known_mtime") or 0)
            for item in requested
        }

        meta = self.repo.get_tile_meta_many(z, coords)
        changed = []

        for x, y in coords:
            row = meta.get((x, y))
            if not row:
                continue
            if row["updated_ms"] == known.get((x, y), 0):
                continue
            changed.append(
                {
                    "z": z,
                    "x": x,
                    "y": y,
                    "mtime": row["updated_ms"],
                    "version": row["version"],
                    "url": f"/tile/{z}/{x}/{y}.png?t={row['updated_ms']}",
                }
            )
        return changed

    def tile_file_path(self, z: int, x: int, y: int) -> str:
        return self._tile_path(z, x, y, ensure_dir=False)
