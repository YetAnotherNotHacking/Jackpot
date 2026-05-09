import json
import os
import threading
import uuid
from typing import Dict, List

from flask import Flask, Response, jsonify, render_template, send_file
from flask_sock import Sock
from redis import Redis

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

    redis_client = Redis.from_url(app.config["REDIS_URL"], decode_responses=True)
    tile_engine = TileEngine(
        tile_root=app.config["TILE_ROOT"],
        tile_size=app.config["TILE_SIZE"],
        max_zoom=app.config["MAX_ZOOM"],
        repo=repo,
    )

    os.makedirs(app.config["TILE_ROOT"], exist_ok=True)

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

        path = tile_engine.tile_file_path(z, x_int, y_int)
        if not os.path.exists(path):
            return Response(status=404)
        response = send_file(path, mimetype="image/png")
        response.headers["Cache-Control"] = "public, max-age=60"
        return response

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
                    redis_client.hset(
                        f"cursor:{client_id}",
                        mapping={
                            "z": int(cursor.get("z", 0)),
                            "x": float(cursor.get("x", 0.0)),
                            "y": float(cursor.get("y", 0.0)),
                        },
                    )
                    redis_client.expire(f"cursor:{client_id}", 20)

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
                redis_client.delete(f"cursor:{client_id}")
            except Exception:  # noqa: BLE001
                pass

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=False)
