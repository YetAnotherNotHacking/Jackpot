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
        self.max_descendant_depth = max(10, max_descendant_depth)
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
        with Image.open(path) as loaded:
            return loaded.convert("RGBA")

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
        max_level = min(self.max_zoom, z + self.max_descendant_depth)
        min_level = 0

        with self._lock:
            for level in range(min_level, max_level + 1):
                if level >= z:
                    level_scale = 2 ** (level - z)
                    level_size = max(1.0, size * level_scale)
                else:
                    level_scale = 2 ** (z - level)
                    level_size = max(1.0, size / level_scale)

                radius_px = level_size / 2.0
                color = self._parse_color(color_hex, 255)
                erase_strength = 255
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
                                if level > z:
                                    path = self._tile_path(level, tx, ty, ensure_dir=False)
                                    if not os.path.exists(path):
                                        # Skip materializing descendants that do not exist yet.
                                        # They will inherit the antialiased stroke from ancestors when requested.
                                        continue

                                # Erasing should start from a rebuilt ancestor state
                                # so deep descendant tiles correctly inherit transparency.
                                if tool == "eraser" and level > z:
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

        span = self.tile_world_span(z)
        tx = math.floor(wx / span)
        ty = math.floor(wy / span)
        local_world_x = wx - (tx * span)
        local_world_y = wy - (ty * span)
        px = int((local_world_x / span) * self.tile_size)
        py = int((local_world_y / span) * self.tile_size)

        # Get the target color at the clicked position
        target_tile = self._load_or_create(z, tx, ty)
        target_color = None
        
        if px >= 0 and px < self.tile_size and py >= 0 and py < self.tile_size:
            target_color = target_tile.getpixel((px, py))
        else:
            # Clicked outside tile bounds
            return {"updated": [], "invalidated": []}

        # Skip if already the target color
        fill_color = self._parse_color(color_hex, 255)
        if target_color[:3] == fill_color[:3]:  # Compare RGB, ignore alpha
            return {"updated": [], "invalidated": []}

        touched: Set[TileCoord] = set()
        tile_cache: Dict[TileCoord, Image.Image] = {}

        with self._lock:
            # Perform flood fill starting from the clicked tile
            fill_queue = [(z, tx, ty, px, py)]
            visited_tiles = set()
            
            while fill_queue:
                current_z, current_tx, current_ty, current_px, current_py = fill_queue.pop(0)
                tile_key = (current_z, current_tx, current_ty)
                
                if tile_key in visited_tiles:
                    continue
                visited_tiles.add(tile_key)
                
                coord = TileCoord(current_z, current_tx, current_ty)
                if coord not in tile_cache:
                    tile_cache[coord] = self._load_or_create(current_z, current_tx, current_ty)
                
                # Perform flood fill on this tile and get boundary pixels
                boundary_pixels = self._flood_fill_tile(
                    tile_cache[coord], 
                    current_px, 
                    current_py, 
                    target_color, 
                    fill_color
                )
                
                if boundary_pixels:
                    touched.add(coord)
                    
                    # Check neighboring tiles for pixels that match the target color
                    span_current = self.tile_world_span(current_z)
                    
                    # Check all boundary pixels for potential spread to adjacent tiles
                    for bx, by in boundary_pixels:
                        # Convert pixel coordinates back to world coordinates
                        local_wx = (bx / self.tile_size) * span_current
                        local_wy = (by / self.tile_size) * span_current
                        world_x = current_tx * span_current + local_wx
                        world_y = current_ty * span_current + local_wy
                        
                        # Check adjacent tiles
                        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            adj_tx = current_tx + dx
                            adj_ty = current_ty + dy
                            
                            # Calculate pixel position in adjacent tile
                            adj_local_x = world_x - (adj_tx * span_current)
                            adj_local_y = world_y - (adj_ty * span_current)
                            adj_px = int((adj_local_x / span_current) * self.tile_size)
                            adj_py = int((adj_local_y / span_current) * self.tile_size)
                            
                            # Check bounds
                            if (adj_px >= 0 and adj_px < self.tile_size and 
                                adj_py >= 0 and adj_py < self.tile_size):
                                
                                adj_coord = TileCoord(current_z, adj_tx, adj_ty)
                                if adj_coord not in tile_cache:
                                    tile_cache[adj_coord] = self._load_or_create(current_z, adj_tx, adj_ty)
                                
                                # Check if adjacent pixel matches target color
                                try:
                                    adj_pixel = tile_cache[adj_coord].getpixel((adj_px, adj_py))
                                    if adj_pixel[:3] == target_color[:3]:
                                        fill_queue.append((current_z, adj_tx, adj_ty, adj_px, adj_py))
                                except (IndexError, OSError):
                                    pass

            now_ms = int(time.time() * 1000)
            updates: List[Tuple[int, int, int, int]] = []
            for coord in touched:
                mtime = self._save_tile(coord.z, coord.x, coord.y, tile_cache[coord])
                updates.append((coord.z, coord.x, coord.y, max(mtime, now_ms)))

            rows = self.repo.upsert_tiles(updates)

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

    def _flood_fill_tile(
        self, 
        image: Image.Image, 
        start_x: int, 
        start_y: int, 
        target_color: Tuple[int, int, int, int], 
        fill_color: Tuple[int, int, int, int]
    ) -> List[Tuple[int, int]]:
        """Perform flood fill on a single tile. Returns list of boundary pixels that touch tile edges."""
        if start_x < 0 or start_x >= self.tile_size or start_y < 0 or start_y >= self.tile_size:
            return []

        # Check if start pixel matches target color
        start_pixel = image.getpixel((start_x, start_y))
        if start_pixel[:3] != target_color[:3]:  # Compare RGB only
            return []

        # Use a stack-based flood fill algorithm
        stack = [(start_x, start_y)]
        visited = set()
        boundary_pixels = []

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

            # Check if this is a boundary pixel (touches tile edge)
            if x == 0 or x == self.tile_size - 1 or y == 0 or y == self.tile_size - 1:
                boundary_pixels.append((x, y))

            # Add neighbors
            stack.extend([
                (x + 1, y),
                (x - 1, y), 
                (x, y + 1),
                (x, y - 1)
            ])

        return boundary_pixels

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
