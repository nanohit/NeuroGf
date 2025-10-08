from __future__ import annotations

import logging
from typing import Dict, Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram_retry import send_message_with_retry

logger = logging.getLogger(__name__)

ADMIN_IDS = [811818035]


class NyxAudit:
    def __init__(self, audit_bot_token: str, audit_chat_id: int, database_manager):
        self.audit_bot_token = audit_bot_token
        self.audit_chat_id = audit_chat_id
        self.db = database_manager
        self._bot: Optional[Bot] = None
        self._app: Optional[Application] = None
        self.takeover_sessions: Dict[int, int] = {}

    async def _get_bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self.audit_bot_token)
        return self._bot

    async def start(self):
        if self._app is not None:
            return
        self._app = Application.builder().token(self.audit_bot_token).build()
        self._app.add_handler(CommandHandler("panel", self.admin_panel))
        self._app.add_handler(CommandHandler("chats", self.list_chats))
        self._app.add_handler(CommandHandler("release", self.release_command))
        self._app.add_handler(CallbackQueryHandler(self.handle_callback))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_admin_message)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self):
        if self._app is None:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        self._app = None

    def _is_admin(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        return bool(user_id and user_id in ADMIN_IDS)

    async def admin_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        keyboard = [
            [InlineKeyboardButton("📋 Active Chats", callback_data="list_chats")],
            [InlineKeyboardButton("⏸️ Manage Paused", callback_data="manage_paused")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("🔧 Admin Panel", reply_markup=reply_markup)

    async def list_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        control_rows = await self.db.list_user_controls()
        buttons = []
        for row in control_rows[:15]:
            label = f"User {row['user_id']}"
            if row.get("is_paused"):
                label += " ⏸"
            if row.get("takeover_by"):
                label += " 🎯"
            buttons.append([InlineKeyboardButton(label, callback_data=f"manage_{row['user_id']}")])
        if not buttons:
            buttons.append([InlineKeyboardButton("No users", callback_data="noop")])
        reply_markup = InlineKeyboardMarkup(buttons)
        message: Message = update.message or update.callback_query.message
        await message.reply_text("📋 User Controls", reply_markup=reply_markup)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        if query.from_user.id not in ADMIN_IDS:
            await query.answer()
            return
        data = query.data or ""
        if data == "list_chats":
            await query.answer()
            await self.list_chats(update, context)
            return
        if data == "manage_paused":
            await query.answer()
            await self.show_paused_users(query)
            return
        if data.startswith("manage_"):
            await query.answer()
            user_id = int(data.replace("manage_", ""))
            await self.show_user_controls(query, user_id)
        elif data.startswith("pause_"):
            await query.answer()
            user_id = int(data.replace("pause_", ""))
            await self.db.set_user_pause(user_id, True)
            await self.show_user_controls(query, user_id)
        elif data.startswith("unpause_"):
            await query.answer()
            user_id = int(data.replace("unpause_", ""))
            await self.db.set_user_pause(user_id, False)
            await self.show_user_controls(query, user_id)
        elif data.startswith("takeover_"):
            await query.answer()
            user_id = int(data.replace("takeover_", ""))
            await self.start_takeover(query, user_id)
        elif data.startswith("release_"):
            await query.answer()
            user_id = int(data.replace("release_", ""))
            await self.end_takeover(query, user_id)
        else:
            await query.answer()

    async def show_user_controls(self, query, user_id: int):
        control = await self.db.get_user_control(user_id)
        is_paused = control.get("is_paused", False)
        takeover_by = control.get("takeover_by")
        status_lines = [f"👤 User {user_id}"]
        status_lines.append("🔴 PAUSED" if is_paused else "🟢 Active")
        if takeover_by:
            status_lines.append(f"🎯 Taken over by {takeover_by}")
        keyboard = []
        if is_paused:
            keyboard.append([InlineKeyboardButton("▶️ Unpause", callback_data=f"unpause_{user_id}")])
        else:
            keyboard.append([InlineKeyboardButton("⏸️ Pause", callback_data=f"pause_{user_id}")])
        if takeover_by:
            keyboard.append([InlineKeyboardButton("🔓 Release", callback_data=f"release_{user_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🎯 Take Over", callback_data=f"takeover_{user_id}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("\n".join(status_lines), reply_markup=reply_markup)

    async def show_paused_users(self, query):
        try:
            control_rows = await self.db.list_user_controls()
        except Exception as exc:
            await query.edit_message_text(f"Ошибка загрузки списка: {exc}")
            return
        paused = [row for row in control_rows if row.get("is_paused")]
        if not paused:
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="list_chats")]]
            await query.edit_message_text("Нет пользователей на паузе", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        keyboard = []
        for row in paused[:15]:
            user_id = row["user_id"]
            label = f"User {user_id} ⏸"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"manage_{user_id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="list_chats")])
        await query.edit_message_text("⏸️ Пауза включена:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def start_takeover(self, query, user_id: int):
        admin_id = query.from_user.id
        await self.db.set_user_takeover(user_id, admin_id)
        self.takeover_sessions[admin_id] = user_id
        await query.edit_message_text(
            f"🎯 Taking over chat with user {user_id}.\nВы можете писать сообщения, и они придут пользователю от имени Nyx.\nИспользуйте /release чтобы вернуть управление LLM."
        )

    async def end_takeover(self, query, user_id: int):
        admin_id = query.from_user.id
        await self.db.set_user_takeover(user_id, None)
        self.takeover_sessions.pop(admin_id, None)
        await query.edit_message_text(f"🔓 Released control of user {user_id}")

    async def release_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        admin_id = update.effective_user.id
        target = self.takeover_sessions.get(admin_id)
        if target is None:
            await update.message.reply_text("Нет активного захвата.")
            return
        await self.db.set_user_takeover(target, None)
        self.takeover_sessions.pop(admin_id, None)
        await update.message.reply_text(f"🔓 Управление пользователем {target} возвращено Nyx.")

    async def handle_admin_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        admin_id = update.effective_user.id
        if admin_id not in ADMIN_IDS:
            return
        target_user_id = self.takeover_sessions.get(admin_id)
        if not target_user_id:
            return
        message_text = update.message.text
        await self.db.queue_admin_message(target_user_id, admin_id, message_text)
        await update.message.reply_text(f"✅ Отправлено пользователю {target_user_id}")

    async def log_user_message(self, update: Update):
        try:
            if update.effective_user and update.effective_user.id in ADMIN_IDS:
                return
            bot = await self._get_bot()
            username = update.effective_user.username or "no_username"
            first_name = update.effective_user.first_name or "Unknown"
            user_id = update.effective_user.id
            if update.message.text:
                content = update.message.text
            elif update.message.photo:
                content = f"[Photo] {update.message.caption or ''}"
            elif update.message.voice:
                content = "[Voice message]"
            else:
                content = f"[{update.message.content_type}]"
            message = f"👤 @{username} ({first_name}) [ID:{user_id}]\n{content}"
            await send_message_with_retry(
                bot,
                self.audit_chat_id,
                text=message,
                log_context={"source": "audit_user"},
            )
        except Exception as exc:
            logger.error(f"Audit failed (user message): {exc}")

    async def log_bot_response(self, user_id: int, response_text: str):
        try:
            if user_id in ADMIN_IDS:
                return
            bot = await self._get_bot()
            message = f"🤖 → [ID:{user_id}]\n{response_text}"
            await send_message_with_retry(
                bot,
                self.audit_chat_id,
                text=message,
                log_context={"source": "audit_bot", "user_id": user_id},
            )
        except Exception as exc:
            logger.error(f"Audit failed (bot response): {exc}")
