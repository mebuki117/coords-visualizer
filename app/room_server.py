import asyncio
import hashlib
import secrets
import threading
import uuid
from typing import Dict, Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
import uvicorn

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

app = FastAPI(title="Coords Visualizer Room Server")
_rooms: Dict[str, Dict[str, Any]] = {}
_sessions: Dict[str, Dict[str, str]] = {}
_rooms_lock = threading.Lock()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return salt.hex() + ":" + digest.hex()


def _password_matches(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 120_000
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class JoinRequest(BaseModel):
    room: str
    password: str
    user: str
    client_id: str


class RoomClient:
    """Background-thread client used by Coords Visualizer."""

    def __init__(self, server_url: str, on_event=None):
        self.server_url = server_url.rstrip("/")
        self.on_event = on_event
        self.token = None
        self.room = None
        self.user = None
        self.client_id = None
        self.ws = None
        self._thread = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

    @property
    def connected(self):
        return bool(self.ws and self.token)

    def _emit(self, event_type, data=None):
        if self.on_event:
            try:
                self.on_event(event_type, data)
            except Exception:
                pass

    def join(self, room, password, user, client_id):
        self.leave()
        self.room = room.strip()
        self.user = user.strip()
        self.client_id = client_id
        self._stop.clear()
        self._thread = threading.Thread(target=self._join_worker, args=(password,), daemon=True)
        self._thread.start()

    def _join_worker(self, password):
        import json
        import urllib.error
        import urllib.request

        try:
            payload = json.dumps({
                "room": self.room,
                "password": password,
                "user": self.user,
                "client_id": self.client_id,
            }).encode("utf-8")
            request = urllib.request.Request(
                self.server_url + "/rooms/join",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))

            self.token = result["token"]
            self._emit("snapshot", result.get("points", []))
            self._emit("connected", {"room": self.room})
            self._run_websocket()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            self._emit("error", detail)
        except Exception as e:
            if not self._stop.is_set():
                self._emit("error", str(e))
        finally:
            self.ws = None
            self.token = None
            if not self._stop.is_set():
                self._emit("disconnected", None)

    def _run_websocket(self):
        import json
        import websocket

        parsed = urlparse(self.server_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Server URL must look like http://HOST:8765")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{scheme}://{parsed.netloc}/rooms/ws?token={self.token}"

        self.ws = websocket.create_connection(ws_url, timeout=5)
        self.ws.settimeout(1.0)
        while not self._stop.is_set():
            try:
                raw = self.ws.recv()
                if not raw:
                    break
                message = json.loads(raw)
                self._emit(message.get("type"), message.get("data"))
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break

    def _send(self, message):
        if not self.ws or not self.token:
            return False
        import json
        try:
            with self._send_lock:
                self.ws.send(json.dumps(message))
            return True
        except Exception:
            return False

    def send_point(self, point):
        return self._send({"type": "add", "point": point})

    def reset(self):
        return self._send({"type": "reset"})

    def leave(self):
        self._stop.set()
        ws = self.ws
        self.ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self.token = None
        self.room = None
        self.user = None


async def _broadcast(room_name: str, message: dict):
    room = _rooms.get(room_name)
    if not room:
        return
    dead = []
    for websocket in list(room["clients"]):
        try:
            await websocket.send_json(message)
        except Exception:
            dead.append(websocket)
    for websocket in dead:
        room["clients"].discard(websocket)


@app.get("/rooms/status")
async def room_status():
    with _rooms_lock:
        return {"ok": True, "rooms": len(_rooms)}


@app.post("/rooms/join")
async def join_room(request: JoinRequest):
    room_name = request.room.strip()
    user = request.user.strip()
    client_id = request.client_id.strip()
    if not room_name:
        raise HTTPException(400, "Room name is required")
    if not user:
        raise HTTPException(400, "User name is required")
    if not client_id:
        raise HTTPException(400, "Client ID is required")
    if len(room_name) > 64 or len(user) > 32:
        raise HTTPException(400, "Room or user name is too long")

    with _rooms_lock:
        room = _rooms.get(room_name)
        if room is None:
            room = {"password": _password_hash(request.password), "points": [], "clients": set()}
            _rooms[room_name] = room
        elif not _password_matches(request.password, room["password"]):
            raise HTTPException(403, "Incorrect room password")

        token = secrets.token_urlsafe(32)
        _sessions[token] = {"room": room_name, "client_id": client_id, "user": user}
        snapshot = list(room["points"])
    return {"token": token, "room": room_name, "points": snapshot}


@app.websocket("/rooms/ws")
async def room_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    session = _sessions.get(token)
    if not session:
        await websocket.close(code=1008)
        return

    room_name = session["room"]
    client_id = session["client_id"]
    user = session["user"]
    room = _rooms.get(room_name)
    if room is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    room["clients"].add(websocket)
    try:
        await websocket.send_json({"type": "snapshot", "data": list(room["points"])})
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "add":
                incoming = message.get("point")
                if not isinstance(incoming, dict):
                    continue
                try:
                    point = {
                        "id": str(incoming.get("id") or uuid.uuid4().hex),
                        "owner_id": client_id,
                        "user": user,
                        "x": float(incoming["x"]),
                        "z": float(incoming["z"]),
                        "yaw": float(incoming["yaw"]),
                        "pitch": float(incoming["pitch"]),
                        "source": str(incoming.get("source", "")),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
                if any(p["id"] == point["id"] for p in room["points"]):
                    continue
                room["points"].append(point)
                await _broadcast(room_name, {"type": "point_added", "data": point})
            elif message_type == "reset":
                room["points"].clear()
                await _broadcast(room_name, {"type": "reset", "data": {"user": user}})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        room["clients"].discard(websocket)
        _sessions.pop(token, None)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    uvicorn.run(app, host=host, port=port, log_level="warning")


def start_server_in_background(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    thread = threading.Thread(target=run_server, args=(host, port), daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    run_server()
