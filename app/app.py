import json
import os
import shutil
import threading
import uuid
import zipfile
from typing import Dict, List

from flask import Flask, Response, jsonify, render_template, send_file, request, flash, redirect
from werkzeug.utils import secure_filename
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
    app.secret_key = "super-secret-key-for-flash"

    repo = TileRepository(app.config["DATABASE_URL"])
    repo.init_db()

    redis_client = Redis.from_url(app.config["REDIS_URL"], decode_responses=True)
    tile_engine = TileEngine(
        tile_root=app.config["TILE_ROOT"],
        tile_size=app.config["TILE_SIZE"],
        max_zoom=app.config["MAX_ZOOM"],
        max_descendant_depth=app.config["MAX_DESCENDANT_DEPTH"],
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

    @app.route("/fixup", methods=["GET", "POST"])
    def fixup():
        if request.method == "POST":
            if "backup_zip" not in request.files:
                flash("No file part")
                return redirect(request.url)
            file = request.files["backup_zip"]
            if file.filename == "":
                flash("No selected file")
                return redirect(request.url)
            if file and file.filename.endswith(".zip"):
                filename = secure_filename(file.filename)
                upload_path = os.path.join(app.config["TILE_ROOT"], filename)
                file.save(upload_path)
                
                try:
                    with zipfile.ZipFile(upload_path, 'r') as zip_ref:
                        # Extract directly into TILE_ROOT.
                        # Some zips might have a root folder (like 'tiles/'), we'll extract everything.
                        # The sync_tiles_to_db method looks for 'z[level]' directories recursively
                        # Wait, the sync_tiles_to_db method uses os.walk to find 'z[level]' folders,
                        # but it's better to extract and move if there's a parent folder, 
                        # or just extract and let sync_tiles_to_db find them anywhere in tile_root.
                        # Actually sync_tiles_to_db walks the whole tile_root and finds any folder starting with 'z'
                        zip_ref.extractall(app.config["TILE_ROOT"])
                    
                    updated_count = tile_engine.sync_tiles_to_db()
                    broadcast({"type": "tiles_cleared"}) # force clients to reload
                    flash(f"Successfully processed {updated_count} tiles from backup.")
                except Exception as e:
                    flash(f"Error processing zip: {str(e)}")
                finally:
                    if os.path.exists(upload_path):
                        os.remove(upload_path)
                        
                return redirect(request.url)
            else:
                flash("Must be a .zip file")
                return redirect(request.url)
                
        return render_template("fixup.html")

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
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.route("/clear", methods=["POST"])
    def clear_drawings() -> Response:
        tile_engine.clear_tiles()
        broadcast({"type": "tiles_cleared"})
        return jsonify({"ok": True})

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
                redis_client.delete(f"cursor:{client_id}")
            except Exception:  # noqa: BLE001
                pass

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=False)
