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
                                image_data BYTEA,
                                PRIMARY KEY (z, x, y)
                            )
                            """
                        )
                        # Forward-compatible migration for older schemas that
                        # predate binary image storage.
                        cur.execute(
                            """
                            ALTER TABLE tiles
                            ADD COLUMN IF NOT EXISTS image_data BYTEA
                            """
                        )
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(delay_seconds)
        raise RuntimeError(f"Could not initialize database: {last_error}") from last_error

    def upsert_tiles(
        self,
        tile_updates: Iterable[Tuple[int, int, int, int, bytes | None]],
    ) -> List[Dict]:
        updates = list(tile_updates)
        if not updates:
            return []

        placeholders = ", ".join(["(%s, %s, %s, %s, %s, 1)"] * len(updates))
        params: List = []
        for z, x, y, updated_ms, image_data in updates:
            params.extend([z, x, y, updated_ms, image_data])

        query = f"""
            INSERT INTO tiles (z, x, y, updated_ms, image_data, version)
            VALUES {placeholders}
            ON CONFLICT (z, x, y)
            DO UPDATE SET
                updated_ms = EXCLUDED.updated_ms,
                image_data = COALESCE(EXCLUDED.image_data, tiles.image_data),
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

    def get_tile_image(
        self,
        z: int,
        x: int,
        y: int,
    ) -> bytes | None:
        query = """
            SELECT image_data
            FROM tiles
            WHERE z = %s AND x = %s AND y = %s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                try:
                    cur.execute(query, [z, x, y])
                    row = cur.fetchone()
                except psycopg.errors.UndefinedColumn:
                    return None
        return row["image_data"] if row else None

    def get_tile_storage_stats(self, z: int, x: int, y: int) -> Dict | None:
        query = """
            SELECT z, x, y, updated_ms, version, OCTET_LENGTH(image_data) AS byte_len
            FROM tiles
            WHERE z = %s AND x = %s AND y = %s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                try:
                    cur.execute(query, [z, x, y])
                    row = cur.fetchone()
                except psycopg.errors.UndefinedColumn:
                    return None
        return row

    def init_cursors_table(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cursors (
                        client_id TEXT PRIMARY KEY,
                        z INTEGER NOT NULL,
                        x FLOAT NOT NULL,
                        y FLOAT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

    def upsert_cursor(self, client_id: str, z: int, x: float, y: float) -> None:
        query = """
            INSERT INTO cursors (client_id, z, x, y, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (client_id)
            DO UPDATE SET z = EXCLUDED.z, x = EXCLUDED.x, y = EXCLUDED.y, updated_at = CURRENT_TIMESTAMP
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, [client_id, z, x, y])

    def delete_cursor(self, client_id: str) -> None:
        query = "DELETE FROM cursors WHERE client_id = %s"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, [client_id])
