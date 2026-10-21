#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Teleham — Telegram client for PC."""

import asyncio
import random
import threading
import time
from collections import defaultdict
from datetime import datetime

import customtkinter as ctk
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import User, Chat, Channel

from config import API_ID, API_HASH, APP_NAME, SESSION_NAME

# ── theme ──────────────────────────────────────────────────────────────────
BG = "#0b1220"
PANEL = "#121a2b"
CYAN = "#2cdadc"
WHITE = "#eef5ff"
MUTED = "#97aac5"
RED = "#ff6b6b"
ORANGE = "#ffb347"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class SnowCanvas(ctk.CTkCanvas):
    """Простая анимация падающего снега."""

    def __init__(self, master, **kwargs):
        super().__init__(master, bg=BG, highlightthickness=0, **kwargs)
        self.flakes = []
        self.running = True
        self.bind("<Configure>", self._on_resize)
        self.after(50, self._tick)

    def _on_resize(self, _event=None):
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        need = 60
        while len(self.flakes) < need:
            self.flakes.append({
                "x": random.uniform(0, w),
                "y": random.uniform(0, h),
                "r": random.uniform(1.2, 3.2),
                "speed": random.uniform(0.6, 2.2),
                "drift": random.uniform(-0.4, 0.4),
                "alpha": random.randint(140, 255),
            })

    def _tick(self):
        if not self.running:
            return
        self.delete("snow")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        for f in self.flakes:
            f["y"] += f["speed"]
            f["x"] += f["drift"]
            if f["y"] > h:
                f["y"] = -5
                f["x"] = random.uniform(0, w)
            if f["x"] < 0:
                f["x"] = w
            if f["x"] > w:
                f["x"] = 0
            color = f"#{f['alpha']:02x}{f['alpha']:02x}{f['alpha']:02x}"
            self.create_oval(
                f["x"], f["y"], f["x"] + f["r"], f["y"] + f["r"],
                fill=color, outline="", tags="snow"
            )
        self.after(33, self._tick)

    def stop(self):
        self.running = False


class TelehamApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x700")
        self.minsize(900, 560)
        self.configure(fg_color=BG)

        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

        self.dialogs = []
        self.current_entity = None
        self.messages = []  # list of dicts for UI

        # deleted: (chat_id, msg_id) -> text
        self.deleted = {}
        # edited: (chat_id, msg_id) -> list of old texts
        self.edits = defaultdict(list)
        # cache of last known text for each message
        self.msg_cache = {}

        self._build_ui()
        self.after(200, self._bootstrap)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _build_ui(self):
        # header
        header = ctk.CTkFrame(self, fg_color=PANEL, height=52, corner_radius=0)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(
            header, text=APP_NAME, font=ctk.CTkFont(size=20, weight="bold"),
            text_color=WHITE
        ).pack(side="left", padx=18, pady=10)
        self.status = ctk.CTkLabel(header, text="Запуск…", text_color=MUTED)
        self.status.pack(side="right", padx=18)

        body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True)

        # left: chats
        left = ctk.CTkFrame(body, fg_color=PANEL, width=300, corner_radius=0)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ctk.CTkLabel(left, text="Чаты", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=CYAN).pack(anchor="w", padx=14, pady=(12, 6))
        self.chat_list = ctk.CTkScrollableFrame(left, fg_color=PANEL)
        self.chat_list.pack(fill="both", expand=True, padx=6, pady=6)

        # right: messages + snow
        right = ctk.CTkFrame(body, fg_color=BG, corner_radius=0)
        right.pack(side="left", fill="both", expand=True)

        self.chat_title = ctk.CTkLabel(
            right, text="Выбери чат", font=ctk.CTkFont(size=16, weight="bold"),
            text_color=WHITE
        )
        self.chat_title.pack(anchor="w", padx=16, pady=(12, 4))

        # snow layer behind messages
        msg_wrap = ctk.CTkFrame(right, fg_color=BG)
        msg_wrap.pack(fill="both", expand=True, padx=8, pady=4)

        self.snow = SnowCanvas(msg_wrap)
        self.snow.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.msg_frame = ctk.CTkScrollableFrame(msg_wrap, fg_color="transparent")
        self.msg_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        # compose
        compose = ctk.CTkFrame(right, fg_color=PANEL, height=60)
        compose.pack(fill="x", padx=8, pady=8)
        self.input = ctk.CTkEntry(
            compose, placeholder_text="Сообщение…", height=40,
            fg_color=BG, border_color="#2c3f5b", text_color=WHITE
        )
        self.input.pack(side="left", fill="x", expand=True, padx=(10, 8), pady=10)
        self.input.bind("<Return>", lambda e: self._send())
        ctk.CTkButton(
            compose, text="Отправить", width=110, fg_color=CYAN,
            text_color=BG, command=self._send
        ).pack(side="right", padx=10, pady=10)

        # login overlay
        self.login = ctk.CTkFrame(self, fg_color=BG)
        self.login.place(relx=0, rely=0, relwidth=1, relheight=1)
        box = ctk.CTkFrame(self.login, fg_color=PANEL, width=380, height=320, corner_radius=16)
        box.place(relx=0.5, rely=0.5, anchor="center")
        box.pack_propagate(False)
        ctk.CTkLabel(box, text=APP_NAME, font=ctk.CTkFont(size=26, weight="bold"),
                     text_color=WHITE).pack(pady=(28, 6))
        ctk.CTkLabel(box, text="Вход в Telegram", text_color=MUTED).pack(pady=(0, 16))
        self.phone = ctk.CTkEntry(box, placeholder_text="+79001234567", width=280, height=40)
        self.phone.pack(pady=6)
        self.code = ctk.CTkEntry(box, placeholder_text="Код из Telegram", width=280, height=40)
        self.code.pack(pady=6)
        self.password = ctk.CTkEntry(box, placeholder_text="Пароль 2FA (если есть)",
                                     show="*", width=280, height=40)
        self.password.pack(pady=6)
        ctk.CTkButton(box, text="Войти", width=280, height=40, fg_color=CYAN,
                      text_color=BG, command=self._login).pack(pady=16)
        self.login_status = ctk.CTkLabel(box, text="", text_color=MUTED)
        self.login_status.pack()

    def _set_status(self, text):
        self.after(0, lambda: self.status.configure(text=text))

    def _bootstrap(self):
        self._run(self._async_start())

    async def _async_start(self):
        try:
            await self.client.connect()
            if await self.client.is_user_authorized():
                self.after(0, self._hide_login)
                await self._register_handlers()
                await self._load_dialogs()
                self._set_status("Онлайн")
            else:
                self._set_status("Нужен вход")
        except Exception as e:
            self._set_status(f"Ошибка: {e}")

    def _hide_login(self):
        self.login.place_forget()

    def _login(self):
        phone = self.phone.get().strip()
        code = self.code.get().strip()
        password = self.password.get().strip()
        self.login_status.configure(text="Подключение…")
        self._run(self._async_login(phone, code, password))

    async def _async_login(self, phone, code, password):
        try:
            if not await self.client.is_user_authorized():
                if not hasattr(self, "_code_hash") or not code:
                    if not phone:
                        self.after(0, lambda: self.login_status.configure(text="Введи номер"))
                        return
                    result = await self.client.send_code_request(phone)
                    self._code_hash = result.phone_code_hash
                    self._phone = phone
                    self.after(0, lambda: self.login_status.configure(text="Код отправлен"))
                    return
                try:
                    await self.client.sign_in(self._phone, code, phone_code_hash=self._code_hash)
                except SessionPasswordNeededError:
                    if not password:
                        self.after(0, lambda: self.login_status.configure(text="Нужен пароль 2FA"))
                        return
                    await self.client.sign_in(password=password)
            self.after(0, self._hide_login)
            await self._register_handlers()
            await self._load_dialogs()
            self._set_status("Онлайн")
        except Exception as e:
            self.after(0, lambda: self.login_status.configure(text=str(e)))

    async def _register_handlers(self):
        @self.client.on(events.NewMessage)
        async def on_new(event):
            chat_id = event.chat_id
            msg_id = event.message.id
            text = event.message.message or ""
            self.msg_cache[(chat_id, msg_id)] = text
            if self.current_entity and getattr(self.current_entity, "id", None):
                # refresh if same chat
                try:
                    ent_id = self._entity_id(self.current_entity)
                    if ent_id == chat_id or ent_id == event.message.peer_id:
                        await self._load_messages(self.current_entity)
                except Exception:
                    pass

        @self.client.on(events.MessageDeleted)
        async def on_deleted(event):
            for msg_id in event.deleted_ids:
                # try all known chats from cache
                for (cid, mid), text in list(self.msg_cache.items()):
                    if mid == msg_id:
                        self.deleted[(cid, mid)] = text
                        break
            if self.current_entity:
                await self._load_messages(self.current_entity)

        @self.client.on(events.MessageEdited)
        async def on_edited(event):
            chat_id = event.chat_id
            msg_id = event.message.id
            old = self.msg_cache.get((chat_id, msg_id))
            new = event.message.message or ""
            if old and old != new:
                self.edits[(chat_id, msg_id)].append(old)
            self.msg_cache[(chat_id, msg_id)] = new
            if self.current_entity:
                await self._load_messages(self.current_entity)

    def _entity_id(self, entity):
        if isinstance(entity, User):
            return entity.id
        if isinstance(entity, (Chat, Channel)):
            return entity.id
        return getattr(entity, "id", None)

    async def _load_dialogs(self):
        dialogs = []
        async for d in self.client.iter_dialogs(limit=80):
            dialogs.append(d)
        self.dialogs = dialogs
        self.after(0, self._render_dialogs)

    def _render_dialogs(self):
        for w in self.chat_list.winfo_children():
            w.destroy()
        for d in self.dialogs:
            name = d.name or "Без имени"
            btn = ctk.CTkButton(
                self.chat_list, text=name, anchor="w",
                fg_color="transparent", hover_color="#1a2740",
                text_color=WHITE, height=40,
                command=lambda ent=d.entity, title=name: self._open_chat(ent, title)
            )
            btn.pack(fill="x", pady=2, padx=4)

    def _open_chat(self, entity, title):
        self.current_entity = entity
        self.chat_title.configure(text=title)
        self._set_status(f"Чат: {title}")
        self._run(self._load_messages(entity))

    async def _load_messages(self, entity):
        msgs = []
        async for m in self.client.iter_messages(entity, limit=60):
            text = m.message or ""
            if text:
                self.msg_cache[(m.chat_id, m.id)] = text
            msgs.append(m)
        msgs.reverse()
        self.messages = msgs
        self.after(0, self._render_messages)

    def _render_messages(self):
        for w in self.msg_frame.winfo_children():
            w.destroy()
        if not self.current_entity:
            return
        ent_id = self._entity_id(self.current_entity)

        # show deleted for this chat
        deleted_here = [(mid, t) for (cid, mid), t in self.deleted.items()
                        if cid == ent_id or True]
        # simpler: show all deleted related to loaded messages
        shown_ids = set()

        for m in self.messages:
            shown_ids.add(m.id)
            text = m.message or ""
            media = "[медиа] " if m.media and not text else ""
            body = media + (text or "")
            key = (m.chat_id, m.id)
            edit_hist = self.edits.get(key, [])

            frame = ctk.CTkFrame(self.msg_frame, fg_color=PANEL, corner_radius=10)
            frame.pack(fill="x", pady=3, padx=8)

            who = "Вы" if m.out else "Собеседник"
            ts = datetime.fromtimestamp(m.date.timestamp()).strftime("%H:%M") if m.date else ""
            head = f"{who}  ·  {ts}"
            if edit_hist:
                head += "  ·  изменено"
            ctk.CTkLabel(frame, text=head, text_color=CYAN,
                         font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(6, 0))

            ctk.CTkLabel(
                frame, text=body or "(пусто)", text_color=WHITE, wraplength=520,
                justify="left", anchor="w"
            ).pack(anchor="w", padx=10, pady=(2, 8))

            if edit_hist:
                prev = " ← ".join(edit_hist[-3:])
                ctk.CTkLabel(
                    frame, text=f"Было: {prev}", text_color=ORANGE,
                    wraplength=520, justify="left", anchor="w",
                    font=ctk.CTkFont(size=11)
                ).pack(anchor="w", padx=10, pady=(0, 8))

        # deleted messages not in list
        for (cid, mid), text in sorted(self.deleted.items(), key=lambda x: x[0][1]):
            if mid in shown_ids:
                continue
            # show recent deleted
            frame = ctk.CTkFrame(self.msg_frame, fg_color="#2a1520", corner_radius=10)
            frame.pack(fill="x", pady=3, padx=8)
            ctk.CTkLabel(frame, text="🗑 Удалено", text_color=RED,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=10, pady=(6, 0))
            ctk.CTkLabel(
                frame, text=text or "(без текста)", text_color=MUTED, wraplength=520,
                justify="left", anchor="w"
            ).pack(anchor="w", padx=10, pady=(2, 8))

    def _send(self):
        text = self.input.get().strip()
        if not text or not self.current_entity:
            return
        self.input.delete(0, "end")
        self._run(self._async_send(text))

    async def _async_send(self, text):
        try:
            await self.client.send_message(self.current_entity, text)
            await self._load_messages(self.current_entity)
        except Exception as e:
            self._set_status(f"Не отправлено: {e}")

    def on_close(self):
        self.snow.stop()
        try:
            fut = self._run(self.client.disconnect())
            fut.result(timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()


def main():
    app = TelehamApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
