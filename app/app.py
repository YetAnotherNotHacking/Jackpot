import json
import os
import threading
import time
import uuid
from typing import Dict, List

from flask import Flask, Response, jsonify, render_template
from flask_sock import Sock

from .config import Config
from .database import TileRepository
from .tile_engine import TileEngine


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )
    app.config.from_object(Config)

    repo = TileRepository(app.config["DATABASE_URL"])
    repo.init_db()
    repo.init_cursors_table()

    tile_engine = TileEngine(
        tile_root=app.config["TILE_ROOT"],
        tile_size=app.config["TILE_SIZE"],
        max_zoom=app.config["MAX_ZOOM"],
        max_descendant_depth=app.config["MAX_DESCENDANT_DEPTH"],
        repo=repo,
    )

    sock = Sock(app)
    clients: Dict[str, object] = {}
    clients_lock = threading.RLock()

    def broadcast(payload: Dict, except_id: str | None = None) -> None:
        message = json.dumps(payload)
        with clients_lock:
            stale_ids: List[str] = []
            for client_id, client_ws in clients.items():
                if client_id == except_id:
                    continue
                try:
                    client_ws.send(message)
                except Exception:  # noqa: BLE001
                    stale_ids.append(client_id)
            for stale_id in stale_ids:
                clients.pop(stale_id, None)

    @app.route("/")
    def index() -> str:
        return render_template(
            "index.html",
            app_name=app.config["APP_NAME"],
            tile_size=app.config["TILE_SIZE"],
            max_zoom=app.config["MAX_ZOOM"],
        )

    @app.route("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True})

    @app.route("/tile/<int:z>/<x>/<y>.png")
    def get_tile(z: int, x: str, y: str):
        try:
            x_int = int(x)
            y_int = int(y)
        except ValueError:
            return Response(status=400)

        image_data = tile_engine.repo.get_tile_image(z, x_int, y_int)
        if image_data is None:
            # Fallback for legacy on-disk tiles while binary storage migrates.
            legacy_path = tile_engine.tile_file_path(z, x_int, y_int)
            if os.path.exists(legacy_path):
                with open(legacy_path, "rb") as legacy_tile:
                    image_data = legacy_tile.read()
                # Self-heal: once a legacy tile is served, persist it in DB.
                tile_engine.repo.upsert_tiles(
                    [
                        (
                            z,
                            x_int,
                            y_int,
                            int(time.time() * 1000),
                            image_data,
                        )
                    ]
                )
        if image_data is None:
            return Response(status=404)
        response = Response(image_data, mimetype="image/png")
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.route("/debug/tile/<int:z>/<x>/<y>")
    def debug_tile_storage(z: int, x: str, y: str):
        try:
            x_int = int(x)
            y_int = int(y)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid coords"}), 400

        stats = tile_engine.repo.get_tile_storage_stats(z, x_int, y_int)
        legacy_path = tile_engine.tile_file_path(z, x_int, y_int)
        legacy_exists = os.path.exists(legacy_path)

        return jsonify(
            {
                "ok": True,
                "z": z,
                "x": x_int,
                "y": y_int,
                "db_tile": stats,
                "legacy_file_exists": legacy_exists,
            }
        )

    @sock.route("/ws")
    def ws_handler(ws):
        client_id = str(uuid.uuid4())
        with clients_lock:
            clients[client_id] = ws

        ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "client_id": client_id,
                    "tile_size": app.config["TILE_SIZE"],
                    "max_zoom": app.config["MAX_ZOOM"],
                }
            )
        )

        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break

                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                message_type = payload.get("type")

                if message_type == "cursor":
                    cursor = payload.get("cursor", {})
                    repo.upsert_cursor(
                        client_id,
                        int(cursor.get("z", 0)),
                        float(cursor.get("x", 0.0)),
                        float(cursor.get("y", 0.0)),
                    )

                elif message_type == "stroke":
                    result = tile_engine.apply_stroke(payload)
                    response = {
                        "type": "stroke_result",
                        "request_id": payload.get("request_id"),
                        "updated": result["updated"],
                        "invalidated": result["invalidated"],
                    }
                    ws.send(json.dumps(response))
                    broadcast(
                        {
                            "type": "tiles_changed",
                            "updated": result["updated"],
                            "invalidated": result["invalidated"],
                        },
                        except_id=client_id,
                    )

                elif message_type == "fill":
                    result = tile_engine.apply_fill(payload)
                    response = {
                        "type": "fill_result",
                        "request_id": payload.get("request_id"),
                        "updated": result["updated"],
                        "invalidated": result["invalidated"],
                    }
                    ws.send(json.dumps(response))
                    broadcast(
                        {
                            "type": "tiles_changed",
                            "updated": result["updated"],
                            "invalidated": result["invalidated"],
                        },
                        except_id=client_id,
                    )

                elif message_type == "request_tiles":
                    z = int(payload.get("z", 0))
                    requested = payload.get("tiles", [])
                    changed = tile_engine.diff_visible_tiles(z, requested)
                    ws.send(
                        json.dumps(
                            {
                                "type": "tiles_response",
                                "request_id": payload.get("request_id"),
                                "z": z,
                                "tiles": changed,
                            }
                        )
                    )

                elif message_type == "ping":
                    ws.send(json.dumps({"type": "pong"}))

        finally:
            with clients_lock:
                clients.pop(client_id, None)
            try:
                repo.delete_cursor(client_id)
            except Exception:  # noqa: BLE001
                pass

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=False)
