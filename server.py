# -*- coding: utf-8 -*-

import asyncio
import json
import mimetypes
from email.parser import BytesParser
from email.policy import default
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from TikTokLive import TikTokLiveClient
from TikTokLive.events import GiftEvent, ConnectEvent, DisconnectEvent

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR
HOST_MEDIA_META = PUBLIC_DIR / "host_media.json"
HOST_MEDIA_FILE = PUBLIC_DIR / "host_media.bin"

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8899"))

TIKTOK_USERNAME = os.getenv("TIKTOK_USERNAME", "ruhali577")
TARGET_GIFT_VALUE = 500
DURATION = 135
MAX_SCORE = 499900
SLOT_COUNT = 8

slots = [
    {
        "slot": i + 1,
        "active": False,
        "username": "",
        "avatar": "",
        "started_at": 0,
    }
    for i in range(SLOT_COUNT)
]

pending_gifts = []
_next_pending_id = 1
lock = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


def clean_expired():
    current = now_ms()
    with lock:
        for slot in slots:
            if slot["active"] and current - slot["started_at"] >= DURATION * 1000:
                slot["active"] = False
                slot["username"] = ""
                slot["avatar"] = ""
                slot["started_at"] = 0


def get_state():
    clean_expired()
    with lock:
        return [dict(x) for x in slots]


def get_pending():
    with lock:
        return [dict(x) for x in pending_gifts]


def queue_gift(username, avatar, gift_name):
    global _next_pending_id
    with lock:
        item = {
            "id": _next_pending_id,
            "username": username or "Request",
            "avatar": avatar or "",
            "gift": gift_name or "Gift",
            "created_at": now_ms(),
        }
        _next_pending_id += 1
        pending_gifts.append(item)
        return dict(item)


def assign_pending(pending_id, slot_number):
    clean_expired()
    try:
        pending_id = int(pending_id)
        slot_number = int(slot_number)
    except (TypeError, ValueError):
        return None, "INVALID_ID_OR_SLOT"

    if not 1 <= slot_number <= SLOT_COUNT:
        return None, "INVALID_SLOT"

    with lock:
        item = next((x for x in pending_gifts if x["id"] == pending_id), None)
        if item is None:
            return None, "PENDING_NOT_FOUND"

        slot = next((x for x in slots if x["slot"] == slot_number), None)
        if slot is None:
            return None, "SLOT_NOT_FOUND"
        if slot["active"]:
            return None, "SLOT_BUSY"

        slot["active"] = True
        slot["username"] = item["username"]
        slot["avatar"] = item["avatar"]
        slot["started_at"] = now_ms()
        pending_gifts[:] = [x for x in pending_gifts if x["id"] != pending_id]
        return dict(slot), None


def remove_pending(pending_id):
    try:
        pending_id = int(pending_id)
    except (TypeError, ValueError):
        return False
    with lock:
        before = len(pending_gifts)
        pending_gifts[:] = [x for x in pending_gifts if x["id"] != pending_id]
        return len(pending_gifts) != before


def reset_all():
    with lock:
        for slot in slots:
            slot["active"] = False
            slot["username"] = ""
            slot["avatar"] = ""
            slot["started_at"] = 0
        pending_gifts.clear()


def send_bytes(handler, body, content_type="text/plain; charset=utf-8", status=200):
    if isinstance(body, str):
        body = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    send_bytes(handler, body, "application/json; charset=utf-8", status)


def serve_public(handler, name):
    safe = Path(name).name
    target = PUBLIC_DIR / safe
    if not target.exists() or not target.is_file():
        send_json(handler, {"ok": False, "error": "NOT_FOUND"}, 404)
        return
    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    send_bytes(handler, target.read_bytes(), mime)



def host_media_state():
    if not HOST_MEDIA_META.exists() or not HOST_MEDIA_FILE.exists():
        return {"active": False, "type": "", "url": ""}
    try:
        meta=json.loads(HOST_MEDIA_META.read_text(encoding="utf-8"))
    except Exception:
        meta={}
    return {"active": True, "type": meta.get("type", ""), "url": "/host-media-file?" + str(int(time.time()*1000))}


def clear_host_media():
    for p in (HOST_MEDIA_META, HOST_MEDIA_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        parsed=urlparse(self.path)
        path=parsed.path.rstrip('/') or '/'
        if path == "/host-media":
            content_type=self.headers.get("Content-Type", "")
            length=int(self.headers.get("Content-Length", "0") or 0)
            body=self.rfile.read(length)
            try:
                msg=BytesParser(policy=default).parsebytes(
                    (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode()+body
                )
                file_part=None
                for part in msg.iter_parts():
                    if part.get_content_disposition() == "form-data" and part.get_filename():
                        file_part=part
                        break
                if file_part is None:
                    send_json(self,{"ok":False,"error":"NO_FILE"},400); return
                filename=file_part.get_filename() or "media"
                ctype=(file_part.get_content_type() or "").lower()
                if not (ctype.startswith("image/") or ctype.startswith("video/")):
                    send_json(self,{"ok":False,"error":"IMAGE_OR_VIDEO_ONLY"},400); return
                data=file_part.get_payload(decode=True) or b""
                if len(data) > 60*1024*1024:
                    send_json(self,{"ok":False,"error":"FILE_TOO_LARGE_60MB"},400); return
                HOST_MEDIA_FILE.write_bytes(data)
                HOST_MEDIA_META.write_text(json.dumps({"type":ctype,"name":filename},ensure_ascii=False),encoding="utf-8")
                send_json(self,{"ok":True,"state":host_media_state()})
            except Exception as exc:
                send_json(self,{"ok":False,"error":str(exc)},400)
            return
        if path == "/host-media-clear":
            clear_host_media()
            send_json(self,{"ok":True})
            return
        send_json(self,{"ok":False,"error":"NOT_FOUND"},404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/":
            serve_public(self, "index.html")
            return
        if path == "/control":
            serve_public(self, "control.html")
            return
        if path == "/frame.png":
            serve_public(self, "frame.png")
            return
        if path == "/host-media-state":
            send_json(self, host_media_state())
            return
        if path == "/host-media-file":
            if not HOST_MEDIA_FILE.exists() or not HOST_MEDIA_META.exists():
                send_json(self, {"ok": False, "error": "NO_MEDIA"}, 404)
                return
            try:
                meta=json.loads(HOST_MEDIA_META.read_text(encoding="utf-8"))
                ctype=meta.get("type") or "application/octet-stream"
            except Exception:
                ctype="application/octet-stream"
            send_bytes(self, HOST_MEDIA_FILE.read_bytes(), ctype)
            return
        if path == "/state":
            clean_expired()
            send_json(self, {
                "ok": True,
                "slots": get_state(),
                "pending": get_pending(),
                "target_gift": TARGET_GIFT_VALUE,
                "duration": DURATION,
                "max_score": MAX_SCORE,
            })
            return
        if path == "/assign":
            assigned, error = assign_pending(query.get("pending_id", [""])[0], query.get("slot", [""])[0])
            if error:
                send_json(self, {"ok": False, "error": error}, 400)
            else:
                send_json(self, {"ok": True, "slot": assigned})
            return
        if path == "/remove-pending":
            removed = remove_pending(query.get("pending_id", [""])[0])
            send_json(self, {"ok": removed}, 200 if removed else 404)
            return
        if path == "/reset":
            reset_all()
            send_json(self, {"ok": True})
            return
        send_json(self, {"ok": False, "error": "NOT_FOUND"}, 404)


def run_http_server():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 72)
    print("TikTok Guest Bridge FINAL — manual guest placement")
    print(f"Broadcast: http://127.0.0.1:{PORT}/")
    print(f"Control  : http://127.0.0.1:{PORT}/control")
    print(f"TikTok   : @{TIKTOK_USERNAME}")
    print(f"Gift     : {TARGET_GIFT_VALUE}")
    print(f"Duration : {DURATION}s")
    print(f"Slots    : {SLOT_COUNT}")
    print("=" * 72)
    server.serve_forever()


def get_username(event):
    user = getattr(event, "user", None)
    if user is None:
        return "Request"
    for name in ("unique_id", "nickname"):
        value = getattr(user, name, None)
        if value:
            return str(value)
    return "Request"


async def get_avatar(event):
    user = getattr(event, "user", None)
    if user is None:
        return ""
    for name in ("avatar_url", "avatar", "profile_picture", "profile_picture_url"):
        value = getattr(user, name, None)
        if value:
            return str(value)
    return ""


client = TikTokLiveClient(unique_id=f"@{TIKTOK_USERNAME}")


@client.on(ConnectEvent)
async def on_connect(event: ConnectEvent):
    print(f"CONNECTED to TikTok @{TIKTOK_USERNAME}")
    print(f"Room ID: {client.room_id}")


@client.on(DisconnectEvent)
async def on_disconnect(event: DisconnectEvent):
    print("TikTok disconnected. Waiting for the next LIVE...")


@client.on(GiftEvent)
async def on_gift(event: GiftEvent):
    if getattr(event, "streaking", False):
        return

    gift = getattr(event, "gift", None)
    if gift is None:
        return

    gift_name = getattr(gift, "name", "Unknown Gift")
    try:
        diamond_count = int(getattr(gift, "diamond_count", 0) or 0)
    except Exception:
        diamond_count = 0
    try:
        repeat_count = int(getattr(event, "repeat_count", 1) or 1)
    except Exception:
        repeat_count = 1

    total_value = diamond_count * repeat_count
    username = get_username(event)
    avatar = await get_avatar(event)

    print(f"Gift: {username} -> {gift_name} x{repeat_count} = {total_value}")

    if total_value != TARGET_GIFT_VALUE:
        return

    pending = queue_gift(username, avatar, gift_name)
    print(f"Gift 500 accepted -> Pending #{pending['id']} (manual placement)")


async def tiktok_loop():
    print(f"Starting direct TikTok monitor for @{TIKTOK_USERNAME}...")
    while True:
        try:
            print(f"Connecting to @{TIKTOK_USERNAME}...")
            await client.connect(fetch_gift_info=True)
            await asyncio.sleep(5)
        except Exception as exc:
            print(f"TikTok connection error: {exc}")
            await asyncio.sleep(15)


async def main():
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()
    await tiktok_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        reset_all()
        print("\nBridge stopped.")
