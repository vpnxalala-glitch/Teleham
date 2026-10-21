#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Teleham Web — local Telegram web client."""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.types import User, Chat, Channel

from config import API_ID, API_HASH, APP_NAME, SESSION_NAME, HOST, PORT

ROOT = Path(__file__).parent

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
login_state = {
    "phone": None,
    "phone_code_hash": None,
    "need_password": False,
}

msg_cache = {}
deleted_msgs = {}
edits = defaultdict(list)
ws_clients = set()
_handlers_ready = False


def entity_title(entity):
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        return " ".join(p for p in parts if p).strip() or (entity.username or str(entity.id))
    if isinstance(entity, (Chat, Channel)):
        return entity.title or str(entity.id)
    return str(getattr(entity, "id", "?"))


def serialize_message(m, chat_id):
    key = (chat_id, m.id)
    text = m.message or ""
    if text:
        msg_cache[key] = text
    media = None
    if m.media and not text:
        media = "[медиа]"
    edit_hist = edits.get(key, [])
    return {
        "id": m.id,
        "out": bool(m.out),
        "text": text,
        "media": media,
        "date": m.date.timestamp() if m.date else None,
        "edited": bool(edit_hist),
        "edit_history": edit_hist[-5:],
        "deleted": False,
    }


async def broadcast(event_type, data):
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    dead = []
    for ws in ws_clients:
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
        await broadcast("new_message", {
            "chat_id": chat_id,
            "message": serialize_message(event.message, chat_id),
        })

    @client.on(events.MessageDeleted)
    async def on_deleted(event):
        for msg_id in event.deleted_ids:
            for (cid, mid), text in list(msg_cache.items()):
                if mid == msg_id:
                    deleted_msgs[(cid, mid)] = {
                        "text": text,
                        "date": datetime.now().timestamp(),
                    }
                    await broadcast("deleted_message", {
                        "chat_id": cid,
                        "message_id": mid,
                        "text": text,
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
        await broadcast("edited_message", {
            "chat_id": chat_id,
            "message": serialize_message(event.message, chat_id),
        })


async def api_status(request):
    authorized = await client.is_user_authorized()
    return web.json_response({
        "app": APP_NAME,
        "authorized": authorized,
        "need_password": login_state["need_password"],
    })


async def api_send_code(request):
    try:
        data = await request.json()
        phone = (data.get("phone") or "").strip().replace(" ", "")
        if not phone:
            return web.json_response({"ok": False, "error": "Введи номер телефона"}, status=400)
        if not phone.startswith("+"):
            return web.json_response(
                {"ok": False, "error": "Номер должен начинаться с + (например +79001234567)"},
                status=400,
            )

        if not client.is_connected():
            await client.connect()

        result = await client.send_code_request(phone)
        login_state["phone"] = phone
        login_state["phone_code_hash"] = result.phone_code_hash
        login_state["need_password"] = False

        sent = getattr(result, "type", None)
        hint = "Код отправлен. Открой Telegram на телефоне — код придёт туда (или SMS)."
        if sent:
            hint = f"Код отправлен ({sent.__class__.__name__}). Смотри в Telegram / SMS."

        return web.json_response({"ok": True, "message": hint})
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
                return web.json_response(
                    {"ok": False, "error": "Введи пароль 2FA", "need_password": True},
                    status=400,
                )
            await client.sign_in(password=password)
            login_state["need_password"] = False
            await register_handlers()
            return web.json_response({"ok": True, "message": "Вход выполнен"})

        if not login_state["phone"] or not login_state["phone_code_hash"]:
            return web.json_response({"ok": False, "error": "Сначала запроси код"}, status=400)
        if not code:
            return web.json_response({"ok": False, "error": "Введи код"}, status=400)

        try:
            await client.sign_in(
                login_state["phone"],
                code,
                phone_code_hash=login_state["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            login_state["need_password"] = True
            return web.json_response(
                {"ok": False, "error": "Нужен пароль двухэтапной проверки", "need_password": True}
            )
        except PhoneCodeInvalidError:
            return web.json_response({"ok": False, "error": "Неверный код"}, status=400)

        await register_handlers()
        return web.json_response({"ok": True, "message": "Вход выполнен"})
    except FloodWaitError as e:
        return web.json_response({"ok": False, "error": f"Подожди {e.seconds} сек."}, status=429)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_dialogs(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    items = []
    async for d in client.iter_dialogs(limit=100):
        items.append({
            "id": d.id,
            "name": d.name or entity_title(d.entity),
            "unread": d.unread_count,
        })
    return web.json_response({"ok": True, "dialogs": items})


async def api_messages(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"ok": False, "error": "bad chat id"}, status=400)

    msgs = []
    async for m in client.iter_messages(chat_id, limit=80):
        msgs.append(serialize_message(m, m.chat_id))
    msgs.reverse()

    for (cid, mid), info in deleted_msgs.items():
        if cid == chat_id:
            msgs.append({
                "id": mid,
                "out": False,
                "text": info.get("text") or "",
                "media": None,
                "date": info.get("date"),
                "edited": False,
                "edit_history": [],
                "deleted": True,
            })

    return web.json_response({"ok": True, "messages": msgs})


async def api_send(request):
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "Не авторизован"}, status=401)
    data = await request.json()
    chat_id = data.get("chat_id")
    text = (data.get("text") or "").strip()
    if not chat_id or not text:
        return web.json_response({"ok": False, "error": "Пустое сообщение"}, status=400)
    m = await client.send_message(int(chat_id), text)
    return web.json_response({"ok": True, "message": serialize_message(m, m.chat_id)})


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
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_static("/static/", ROOT / "static", show_index=False)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/send_code", api_send_code)
    app.router.add_post("/api/sign_in", api_sign_in)
    app.router.add_get("/api/dialogs", api_dialogs)
    app.router.add_get("/api/messages/{chat_id}", api_messages)
    app.router.add_post("/api/send", api_send)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print(f"{APP_NAME} Web → http://{HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
