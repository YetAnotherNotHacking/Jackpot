import time
from contextlib import contextmanager
from typing import Dict, Iterable, List, Tuple

import psycopg
from psycopg.rows import dict_row


class TileRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def _conn(self):
        conn = psycopg.connect(self.database_url, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self, retries: int = 25, delay_seconds: float = 1.0) -> None:
        last_error = None
        for _ in range(retries):
            try:
                with self._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS tiles (
                                z INTEGER NOT NULL,
                                x INTEGER NOT NULL,
                                y INTEGER NOT NULL,
                                updated_ms BIGINT NOT NULL,
                                version BIGINT NOT NULL DEFAULT 1,
                                PRIMARY KEY (z, x, y)
                            )
                            """
                        )
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(delay_seconds)
        raise RuntimeError(f"Could not initialize database: {last_error}") from last_error

    def upsert_tiles(
        self,
        tile_updates: Iterable[Tuple[int, int, int, int]],
    ) -> List[Dict]:
        updates = list(tile_updates)
        if not updates:
            return []

        placeholders = ", ".join(["(%s, %s, %s, %s, 1)"] * len(updates))
        params: List[int] = []
        for z, x, y, updated_ms in updates:
            params.extend([z, x, y, updated_ms])

        query = f"""
            INSERT INTO tiles (z, x, y, updated_ms, version)
            VALUES {placeholders}
            ON CONFLICT (z, x, y)
            DO UPDATE SET
                updated_ms = EXCLUDED.updated_ms,
                version = tiles.version + 1
            RETURNING z, x, y, updated_ms, version
        """

        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        return rows

    def clear_tiles(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tiles")

    def get_tile_meta_many(
        self,
        z: int,
        coords: Iterable[Tuple[int, int]],
    ) -> Dict[Tuple[int, int], Dict]:
        coords_list = list(coords)
        if not coords_list:
            return {}

        placeholders = ", ".join(["(%s, %s)"] * len(coords_list))
        params: List[int] = []
        for x, y in coords_list:
            params.extend([x, y])

        query = f"""
            SELECT x, y, updated_ms, version
            FROM tiles
            WHERE z = %s
              AND (x, y) IN ({placeholders})
        """

        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, [z, *params])
                rows = cur.fetchall()
        return {(row["x"], row["y"]): row for row in rows}
