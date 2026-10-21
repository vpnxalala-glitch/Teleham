#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Teleham Web — Telegram-like client for PC."""

import asyncio
import json
import mimetypes
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telethon import TelegramClient, events, types
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.types import User, Chat, Channel, MessageMediaPhoto, MessageMediaDocument

from config import API_ID, API_HASH, APP_NAME, SESSION_NAME, HOST, PORT

ROOT = Path(__file__).parent
AVATAR_DIR = ROOT / "static" / "avatars"
MEDIA_DIR = ROOT / "static" / "media"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
login_state = {"phone": None, "phone_code_hash": None, "need_password": False}
msg_cache = {}
deleted_msgs = {}
edits = defaultdict(list)
ws_clients = set()
_handlers_ready = False


def sid(value):
    """String id — JS cannot safely hold large Telegram ids as numbers."""
    return str(value)


def entity_title(entity):
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        return " ".join(p for p in parts if p).strip() or (entity.username or str(entity.id))
    if isinstance(entity, (Chat, Channel)):
        return entity.title or str(entity.id)
    return str(getattr(entity, "id", "?"))


def entity_letter(name):
    name = (name or "?").strip()
    return name[0].upper() if name else "?"


def avatar_url_if_cached(entity):
    eid = getattr(entity, "id", None)
    if eid is None:
        return None
    path = AVATAR_DIR / f"{eid}.jpg"
    if path.exists() and path.stat().st_size > 0:
        return f"/static/avatars/{eid}.jpg"
    return None


async def download_avatar(entity):
    try:
        eid = getattr(entity, "id", None)
        if eid is None:
            return None
        path = AVATAR_DIR / f"{eid}.jpg"
        if path.exists() and path.stat().st_size > 0:
            return f"/static/avatars/{eid}.jpg"
        result = await client.download_profile_photo(entity, file=str(path))
        if result and path.exists() and path.stat().st_size > 0:
            return f"/static/avatars/{eid}.jpg"
    except Exception:
        pass
    return None


async def ensure_media(message):
    try:
        if not message.media:
            return None
        mid = message.id
        cid = message.chat_id
        if isinstance(message.media, MessageMediaPhoto) or message.photo:
            path = MEDIA_DIR / f"{cid}_{mid}.jpg"
            if path.exists() and path.stat().st_size > 0:
                return {"type": "photo", "url": f"/static/media/{cid}_{mid}.jpg"}
            await client.download_media(message, file=str(path))
            if path.exists() and path.stat().st_size > 0:
                return {"type": "photo", "url": f"/static/media/{cid}_{mid}.jpg"}
        if isinstance(message.media, MessageMediaDocument) and message.document:
            mime = getattr(message.document, "mime_type", "") or ""
            if mime.startswith("image/"):
                ext = mimetypes.guess_extension(mime) or ".bin"
                path = MEDIA_DIR / f"{cid}_{mid}{ext}"
                if not path.exists() or path.stat().st_size == 0:
                    await client.download_media(message, file=str(path))
                if path.exists() and path.stat().st_size > 0:
                    return {"type": "photo", "url": f"/static/media/{path.name}"}
            name = None
            for a in message.document.attributes or []:
                if hasattr(a, "file_name"):
                    name = a.file_name
            return {"type": "file", "name": name or "файл", "size": message.document.size}
    except Exception:
        pass
    return None


def user_status_text(entity):
    if not isinstance(entity, User):
        if isinstance(entity, Channel):
            return "канал" if getattr(entity, "broadcast", False) else "группа"
        if isinstance(entity, Chat):
            return "группа"
        return ""
    st = entity.status
    if isinstance(st, types.UserStatusOnline):
        return "в сети"
    if isinstance(st, types.UserStatusOffline) and st.was_online:
        return "был(а) " + st.was_online.strftime("%d.%m %H:%M")
    if isinstance(st, types.UserStatusRecently):
        return "был(а) недавно"
    if isinstance(st, types.UserStatusLastWeek):
        return "был(а) на этой неделе"
    if isinstance(st, types.UserStatusLastMonth):
        return "был(а) в этом месяце"
    return ""


async def serialize_message(m, chat_id):
    key = (chat_id, m.id)
    text = m.message or ""
    if text:
        msg_cache[key] = text
    media_info = await ensure_media(m)
    edit_hist = edits.get(key, [])
    reply = None
    if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
        rid = m.reply_to.reply_to_msg_id
        reply = {"id": rid, "text": msg_cache.get((chat_id, rid), "")[:120]}
    sender_name = None
    try:
        if m.sender:
            sender_name = entity_title(m.sender)
    except Exception:
        pass
    return {
        "id": m.id,
        "out": bool(m.out),
        "text": text,
        "media": media_info,
        "date": m.date.timestamp() if m.date else None,
        "edited": bool(edit_hist) or bool(m.edit_date),
        "edit_history": edit_hist[-5:],
        "deleted": False,
        "reply": reply,
        "sender": sender_name,
        "views": getattr(m, "views", None),
        "fwd": bool(m.fwd_from),
    }


async def broadcast(event_type, data):
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    dead = []
    for ws in list(ws_clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


async def register_handlers():
    global _handlers_ready
    if _handlers_ready:
        return
    _handlers_ready = True

    @client.on(events.NewMessage)
    async def on_new(event):
        chat_id = event.chat_id
        text = event.message.message or ""
        msg_cache[(chat_id, event.message.id)] = text
        msg = await serialize_message(event.message, chat_id)
        await broadcast("new_message", {"chat_id": sid(chat_id), "message": msg})

    @client.on(events.MessageDeleted)
    async def on_deleted(event):
        for msg_id in event.deleted_ids:
            for (cid, mid), text in list(msg_cache.items()):
                if mid == msg_id:
                    deleted_msgs[(cid, mid)] = {"text": text, "date": datetime.now().timestamp()}
                    await broadcast("deleted_message", {
                        "chat_id": sid(cid), "message_id": mid, "text": text
                    })
                    break

    @client.on(events.MessageEdited)
    async def on_edited(event):
        chat_id = event.chat_id
        msg_id = event.message.id
        key = (chat_id, msg_id)
        old = msg_cache.get(key)
        new = event.message.message or ""
        if old is not None and old != new:
            edits[key].append(old)
        msg_cache[key] = new
        msg = await serialize_message(event.message, chat_id)
        await broadcast("edited_message", {"chat_id": sid(chat_id), "message": msg})


async def api_status(request):
    try:
        authorized = await client.is_user_authorized()
    except Exception:
        authorized = False
    me = None
    if authorized:
        try:
            u = await client.get_me()
            av = avatar_url_if_cached(u) or await download_avatar(u)
            me = {
                "id": sid(u.id),
                "name": entity_title(u),
                "username": u.username,
                "avatar": av,
                "letter": entity_letter(entity_title(u)),
                "phone": u.phone,
            }
        except Exception:
            pass
    return web.json_response({
        "app": APP_NAME,
        "authorized": authorized,
        "need_password": login_state["need_password"],
        "me": me,
    })


async def api_send_code(request):
    try:
        data = await request.json()
        phone = (data.get("phone") or "").strip().replace(" ", "")
        if not phone:
            return web.json_response({"ok": False, "error": "Введи номер"}, status=400)
        if not phone.startswith("+"):
            return web.json_response({"ok": False, "error": "Номер с +"}, status=400)
        if not client.is_connected():
            await client.connect()
        result = await client.send_code_request(phone)
        login_state["phone"] = phone
        login_state["phone_code_hash"] = result.phone_code_hash
        login_state["need_password"] = False
        return web.json_response({"ok": True, "message": "Код отправлен — смотри Telegram / SMS"})
    except FloodWaitError as e:
        return web.json_response({"ok": False, "error": f"Подожди {e.seconds} сек."}, status=429)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_sign_in(request):
    try:
        data = await request.json()
        code = (data.get("code") or "").strip()
        password = (data.get("password") or "").strip()
        if not client.is_connected():
            await client.connect()
        if login_state["need_password"] or password:
            if not password:
                return web.json_response({"ok": False, "error": "Пароль 2FA", "need_password": True}, status=400)
            await client.sign_in(password=password)
            login_state["need_password"] = False
            await register_handlers()
            return web.json_response({"ok": True})
        if not login_state["phone"] or not login_state["phone_code_hash"]:
            return web.json_response({"ok": False, "error": "Сначала запроси код"}, status=400)
        if not code:
            return web.json_response({"ok": False, "error": "Введи код"}, status=400)
        try:
            await client.sign_in(login_state["phone"], code, phone_code_hash=login_state["phone_code_hash"])
        except SessionPasswordNeededError:
            login_state["need_password"] = True
            return web.json_response({"ok": False, "error": "Нужен пароль 2FA", "need_password": True})
        except PhoneCodeInvalidError:
            return web.json_response({"ok": False, "error": "Неверный код"}, status=400)
        await register_handlers()
        return web.json_response({"ok": True})
    except FloodWaitError as e:
        return web.json_response({"ok": False, "error": f"Подожди {e.seconds} сек."}, status=429)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_dialogs(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    items = []
    try:
        async for d in client.iter_dialogs(limit=200):
            try:
                name = d.name or entity_title(d.entity)
                last = ""
                last_date = None
                if d.message:
                    last_date = d.message.date.timestamp() if d.message.date else None
                    if d.message.message:
                        last = d.message.message[:90]
                    elif d.message.photo:
                        last = "📷 Фото"
                    elif d.message.media:
                        last = "📎 Файл"
                kind = "user"
                if isinstance(d.entity, Channel):
                    kind = "channel" if getattr(d.entity, "broadcast", False) else "group"
                elif isinstance(d.entity, Chat):
                    kind = "group"
                # only cached avatars here — fast list
                av = avatar_url_if_cached(d.entity)
                items.append({
                    "id": sid(d.id),
                    "name": name,
                    "letter": entity_letter(name),
                    "avatar": av,
                    "unread": d.unread_count or 0,
                    "last": last,
                    "last_date": last_date,
                    "pinned": bool(d.pinned),
                    "kind": kind,
                    "status": user_status_text(d.entity),
                })
            except Exception:
                continue
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e), "dialogs": []}, status=500)

    # background: fetch missing avatars (non-blocking for response)
    async def fill_avatars():
        try:
            async for d in client.iter_dialogs(limit=40):
                if not avatar_url_if_cached(d.entity):
                    await download_avatar(d.entity)
        except Exception:
            pass

    asyncio.create_task(fill_avatars())
    return web.json_response({"ok": True, "dialogs": items, "count": len(items)})


async def api_messages(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    raw = request.match_info["chat_id"]
    try:
        chat_id = int(raw)
    except ValueError:
        return web.json_response({"ok": False, "error": "bad id"}, status=400)

    try:
        await client.send_read_acknowledge(chat_id)
    except Exception:
        pass

    q = request.query.get("q", "").strip()
    msgs = []
    try:
        if q:
            it = client.iter_messages(chat_id, search=q, limit=50)
        else:
            it = client.iter_messages(chat_id, limit=80)
        async for m in it:
            msgs.append(await serialize_message(m, m.chat_id))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    msgs.reverse()

    for (cid, mid), info in deleted_msgs.items():
        if cid == chat_id:
            msgs.append({
                "id": mid, "out": False, "text": info.get("text") or "",
                "media": None, "date": info.get("date"), "edited": False,
                "edit_history": [], "deleted": True, "reply": None,
                "sender": None, "views": None, "fwd": False,
            })
    return web.json_response({"ok": True, "messages": msgs})


async def api_chat_info(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False}, status=401)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"ok": False}, status=400)
    entity = await client.get_entity(chat_id)
    av = avatar_url_if_cached(entity) or await download_avatar(entity)
    info = {
        "id": sid(chat_id),
        "name": entity_title(entity),
        "letter": entity_letter(entity_title(entity)),
        "avatar": av,
        "status": user_status_text(entity),
        "username": getattr(entity, "username", None),
        "about": getattr(entity, "about", None) or "",
        "kind": "user",
        "members": None,
    }
    if isinstance(entity, Channel):
        info["kind"] = "channel" if entity.broadcast else "group"
        info["members"] = getattr(entity, "participants_count", None)
    elif isinstance(entity, Chat):
        info["kind"] = "group"
        info["members"] = getattr(entity, "participants_count", None)
    elif isinstance(entity, User):
        info["phone"] = entity.phone
    return web.json_response({"ok": True, "info": info})


async def api_send(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    data = await request.json()
    chat_id = data.get("chat_id")
    text = (data.get("text") or "").strip()
    reply_to = data.get("reply_to")
    if chat_id is None or not text:
        return web.json_response({"ok": False, "error": "Пусто"}, status=400)
    kwargs = {}
    if reply_to:
        kwargs["reply_to"] = int(reply_to)
    m = await client.send_message(int(chat_id), text, **kwargs)
    return web.json_response({"ok": True, "message": await serialize_message(m, m.chat_id)})


async def api_send_file(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    reader = await request.multipart()
    chat_id = None
    file_path = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "chat_id":
            chat_id = (await part.text()).strip()
        elif part.name == "file":
            data = await part.read(decode=False)
            filename = part.filename or "file.bin"
            tmp = MEDIA_DIR / f"upload_{filename}"
            tmp.write_bytes(data)
            file_path = tmp
    if not chat_id or not file_path:
        return web.json_response({"ok": False, "error": "Нет файла"}, status=400)
    m = await client.send_file(int(chat_id), str(file_path))
    return web.json_response({"ok": True, "message": await serialize_message(m, m.chat_id)})


async def api_typing(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False}, status=401)
    data = await request.json()
    chat_id = data.get("chat_id")
    if chat_id is not None:
        try:
            await client.action(int(chat_id), "typing")
        except Exception:
            pass
    return web.json_response({"ok": True})


async def api_logout(request):
    try:
        await client.log_out()
    except Exception:
        pass
    return web.json_response({"ok": True})


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ws_clients.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        ws_clients.discard(ws)
    return ws


async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html")


async def on_startup(app):
    await client.connect()
    if await client.is_user_authorized():
        await register_handlers()


async def on_cleanup(app):
    await client.disconnect()


def main():
    app = web.Application(client_max_size=40 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_static("/static/", ROOT / "static", show_index=False)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/send_code", api_send_code)
    app.router.add_post("/api/sign_in", api_sign_in)
    app.router.add_get("/api/dialogs", api_dialogs)
    app.router.add_get("/api/messages/{chat_id}", api_messages)
    app.router.add_get("/api/chat/{chat_id}", api_chat_info)
    app.router.add_post("/api/send", api_send)
    app.router.add_post("/api/send_file", api_send_file)
    app.router.add_post("/api/typing", api_typing)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"{APP_NAME} Web → http://{HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
