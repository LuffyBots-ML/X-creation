# File: main.py
import asyncio
import html
import logging
from logging.handlers import RotatingFileHandler
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from config import AppConfig
from database import DatabaseManager
from verifier import XReadOnlyVerifier

# =====================================================================
# LOGGING SETUP
# =====================================================================
def setup_logging(log_path: str) -> logging.Logger:
    from pathlib import Path
    path_obj = Path(log_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("HoloweenBot")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        path_obj, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger


# =====================================================================
# ADMIN MIDDLEWARE
# =====================================================================
class SingleAdminMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int, logger_instance: logging.Logger) -> None:
        self.admin_id = admin_id
        self.logger = logger_instance

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.admin_id:
            uid = user.id if user else "Unknown"
            self.logger.warning("Unauthorized access blocked from user_id=%s", uid)
            if isinstance(event, Message):
                await event.answer("⛔ Access denied. Restricted to the administrator.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Access denied.", show_alert=True)
            return None
        return await handler(event, data)


# =====================================================================
# KEYBOARDS & HELPERS
# =====================================================================
def build_main_menu_keyboard(active_username: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🔢 Next Username", callback_data="cmd:next"),
            InlineKeyboardButton(text="📊 Stats", callback_data="cmd:stats"),
        ],
        [
            InlineKeyboardButton(text="▶️ Start Session", callback_data="cmd:session_start"),
            InlineKeyboardButton(text="📋 Session Status", callback_data="cmd:session_status"),
        ],
        [
            InlineKeyboardButton(text="✖️ Cancel Session", callback_data="cmd:session_cancel"),
            InlineKeyboardButton(text="✅ Confirmed Accounts", callback_data="cmd:accounts"),
        ],
        [
            InlineKeyboardButton(text="🩺 Health", callback_data="cmd:health"),
            InlineKeyboardButton(text="📜 Recent Logs", callback_data="cmd:logs"),
        ],
    ]
    if active_username:
        rows.insert(
            2,
            [
                InlineKeyboardButton(
                    text=f"🔍 Verify Active (@{active_username})",
                    callback_data=f"verify:{active_username}",
                )
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def read_recent_logs(log_path: str, max_lines: int = 25) -> str:
    from pathlib import Path
    p = Path(log_path)
    if not p.exists():
        return "No log entries found yet."
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail) if tail else "Log file is empty."
    except OSError as exc:
        return f"Error reading log file: {exc}"


# =====================================================================
# ROUTER & HANDLER BUILDER
# =====================================================================
def build_router(
    config: AppConfig,
    db: DatabaseManager,
    verifier: XReadOnlyVerifier,
    logger_instance: logging.Logger,
    start_monotonic: float,
) -> Router:
    router = Router(name="holoween_router")

    async def render_next_text() -> Tuple[str, InlineKeyboardMarkup]:
        seq, next_uname, sess = await asyncio.to_thread(db.peek_next_sequence)
        text = (
            "<b>🔢 Sequential Username Generator</b>\n\n"
            f"• <b>Next Available Sequence:</b> <code>{seq:05d}</code>\n"
            f"• <b>Candidate Handle:</b> <code>{html.escape(next_uname)}</code>\n"
        )
        if sess:
            text += (
                "\n⚠️ <b>Active Manual Session In Progress:</b>\n"
                f"• <b>Reserved Handle:</b> <code>{html.escape(sess['username'])}</code>\n"
                f"• <b>Started At:</b> <code>{html.escape(sess['started_at'])}</code>\n"
                "Use /verify once registration on X is finished."
            )
            kb = build_main_menu_keyboard(active_username=sess["username"])
        else:
            text += "\nUse /session_start to lock this handle for manual signup."
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="▶️ Start Session", callback_data="cmd:session_start"),
                        InlineKeyboardButton(
                            text=f"🔍 Check @{next_uname}",
                            callback_data=f"verify:{next_uname}",
                        ),
                    ],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="cmd:menu")],
                ]
            )
        return text, kb

    async def render_session_start() -> Tuple[str, InlineKeyboardMarkup]:
        created, sess = await asyncio.to_thread(db.start_manual_session)
        uname = sess["username"]
        if created:
            logger_instance.info("Manual signup session started for %s (seq=%d)", uname, sess["seq_num"])
            text = (
                "<b>▶️ Manual Signup Session Started</b>\n\n"
                f"• <b>Sequence Number:</b> <code>{sess['seq_num']:05d}</code>\n"
                f"• <b>Target Username:</b> <code>{html.escape(uname)}</code>\n"
                f"• <b>Started At:</b> <code>{html.escape(sess['started_at'])}</code>\n\n"
                "<b>Instructions:</b>\n"
                "1. Register the account manually on X.\n"
                f"2. Set the handle to <code>{html.escape(uname)}</code>.\n"
                f"3. Run <code>/verify {html.escape(uname)}</code> to confirm public availability."
            )
        else:
            text = (
                "<b>ℹ️ Session Already Active</b>\n\n"
                f"• <b>Reserved Username:</b> <code>{html.escape(uname)}</code>\n"
                f"• <b>Sequence Number:</b> <code>{sess['seq_num']:05d}</code>\n"
                f"• <b>Started At:</b> <code>{html.escape(sess['started_at'])}</code>"
            )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🔍 Verify @{uname}", callback_data=f"verify:{uname}")],
                [
                    InlineKeyboardButton(text="✖️ Cancel Session", callback_data="cmd:session_cancel"),
                    InlineKeyboardButton(text="🏠 Main Menu", callback_data="cmd:menu"),
                ],
            ]
        )
        return text, kb

    async def render_session_status() -> Tuple[str, InlineKeyboardMarkup]:
        sess = await asyncio.to_thread(db.get_active_session)
        if not sess:
            return (
                "<b>📋 Session Status</b>\n\nNo manual signup session is currently active.\n"
                "Use /session_start to reserve the next sequential username.",
                build_main_menu_keyboard(),
            )
        uname = sess["username"]
        text = (
            "<b>📋 Active Manual Signup Session</b>\n\n"
            f"• <b>Sequence Number:</b> <code>{sess['seq_num']:05d}</code>\n"
            f"• <b>Target Username:</b> <code>{html.escape(uname)}</code>\n"
            f"• <b>Started At:</b> <code>{html.escape(sess['started_at'])}</code>"
        )
        return text, build_main_menu_keyboard(active_username=uname)

    async def render_session_cancel() -> Tuple[str, InlineKeyboardMarkup]:
        cancelled = await asyncio.to_thread(db.cancel_active_session)
        if not cancelled:
            return (
                "<b>ℹ️ No Active Session</b>\n\nThere is no active session to cancel.",
                build_main_menu_keyboard(),
            )
        logger_instance.info("Cancelled session for %s (seq=%d)", cancelled["username"], cancelled["seq_num"])
        text = (
            "<b>✖️ Session Cancelled</b>\n\n"
            f"• <b>Released Handle:</b> <code>{html.escape(cancelled['username'])}</code>\n"
            f"• <b>Sequence Number:</b> <code>{cancelled['seq_num']:05d}</code> (preserved)"
        )
        return text, build_main_menu_keyboard()

    async def render_stats() -> Tuple[str, InlineKeyboardMarkup]:
        st = await asyncio.to_thread(db.get_stats)
        latest_str = (
            f"<code>@{html.escape(st['latest_account']['username'])}</code> ({html.escape(st['latest_account']['confirmed_at'])})"
            if st["latest_account"]
            else "None yet"
        )
        sess_str = (
            f"<code>@{html.escape(st['active_session']['username'])}</code>"
            if st["active_session"]
            else "Idle"
        )
        text = (
            "<b>📊 Holoween Statistics</b>\n\n"
            f"• <b>Total Confirmed Accounts:</b> <code>{st['total_confirmed']}</code>\n"
            f"• <b>Start Sequence:</b> <code>{st['start_sequence']:05d}</code>\n"
            f"• <b>Next Sequence Number:</b> <code>{st['next_seq']:05d}</code>\n"
            f"• <b>Next Handle:</b> <code>{html.escape(st['next_username'])}</code>\n"
            f"• <b>Active Session:</b> {sess_str}\n"
            f"• <b>Latest Confirmed:</b> {latest_str}"
        )
        active_uname = st["active_session"]["username"] if st["active_session"] else None
        return text, build_main_menu_keyboard(active_username=active_uname)

    async def render_accounts() -> Tuple[str, InlineKeyboardMarkup]:
        records = await asyncio.to_thread(db.list_confirmed_accounts, 20, 0)
        if not records:
            return (
                "<b>✅ Confirmed Account Records</b>\n\nNo confirmed accounts stored yet.\n"
                "Use <code>/verify USERNAME</code> after completing manual signup.",
                build_main_menu_keyboard(),
            )
        lines = ["<b>✅ Confirmed Account Records (Latest 20)</b>\n"]
        for rec in records:
            seq_tag = f"#{rec['seq_num']:05d}" if rec["seq_num"] is not None else "Custom"
            lines.append(
                f"• <b>{seq_tag}</b> — <code>@{html.escape(rec['username'])}</code> "
                f"(ID: <code>{html.escape(rec['x_user_id'])}</code>) — {html.escape(rec['confirmed_at'])}"
            )
        lines.append("\nInspect specific record with <code>/account USERNAME</code>.")
        return "\n".join(lines), build_main_menu_keyboard()

    async def render_health() -> Tuple[str, InlineKeyboardMarkup]:
        db_ok, db_msg = await asyncio.to_thread(db.check_health)
        uptime_sec = int(time.monotonic() - start_monotonic)
        hours, rem = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(rem, 60)
        status_icon = "🟢" if db_ok else "🔴"
        text = (
            f"<b>{status_icon} System Health</b>\n\n"
            f"• <b>Bot Status:</b> Online\n"
            f"• <b>Uptime:</b> <code>{hours}h {minutes}m {seconds}s</code>\n"
            f"• <b>Database:</b> <code>{html.escape(db_msg)}</code>\n"
            f"• <b>X API Mode:</b> Read-Only v2 Lookup"
        )
        return text, build_main_menu_keyboard()

    async def execute_verification(
        target_username: str, confirm_ownership: bool = False
    ) -> Tuple[str, InlineKeyboardMarkup]:
        clean_uname = target_username.lstrip("@").strip()
        logger_instance.info("Running X API read-only verification for %s (confirm=%s)", clean_uname, confirm_ownership)
        result = await verifier.verify_username(clean_uname)

        if result.api_error:
            return (
                "<b>⚠️ X API Verification Error</b>\n\n"
                f"• <b>Target Handle:</b> <code>@{html.escape(clean_uname)}</code>\n"
                f"• <b>Status:</b> <code>{html.escape(result.status_label)}</code>\n"
                f"• <b>Details:</b> {html.escape(result.detail)}",
                build_main_menu_keyboard(),
            )

        if not result.discoverable:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"🔄 Re-Check @{clean_uname}", callback_data=f"verify:{clean_uname}")],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="cmd:menu")],
                ]
            )
            return (
                "<b>🔍 Official X API Lookup Result</b>\n\n"
                f"• <b>Username:</b> <code>@{html.escape(clean_uname)}</code>\n"
                f"• <b>Status:</b> <code>{html.escape(result.status_label)}</code>\n"
                f"• <b>Detail:</b> {html.escape(result.detail)}\n\n"
                "ℹ️ <i>This handle is not publicly active on X right now.</i>",
                kb,
            )

        existing_rec = await asyncio.to_thread(db.get_account_by_username, result.username)
        if not confirm_ownership and not existing_rec:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Mark Manual Signup Complete",
                            callback_data=f"confirm:{result.username}",
                        )
                    ],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="cmd:menu")],
                ]
            )
            return (
                "<b>🔎 Public Profile Found on X</b>\n\n"
                f"• <b>Username:</b> <code>@{html.escape(result.username)}</code>\n"
                f"• <b>X User ID:</b> <code>{html.escape(result.x_user_id or 'N/A')}</code>\n"
                f"• <b>Display Name:</b> {html.escape(result.x_name or 'N/A')}\n"
                f"• <b>Created At:</b> <code>{html.escape(result.x_created_at or 'N/A')}</code>\n\n"
                "⚠️ <b>Important Distinction:</b>\n"
                "The official X API confirms that this profile is <b>publicly discoverable</b>, "
                "but this API lookup does not prove account ownership.\n\n"
                "👉 Tap <b>Mark Manual Signup Complete</b> below to record your completion, "
                "close any matching active session, and advance the sequence atomically.",
                kb,
            )

        is_new, saved_rec = await asyncio.to_thread(
            db.record_confirmed_account,
            result.username,
            result.x_user_id or "UNKNOWN",
            result.x_name or result.username,
            result.x_created_at,
        )
        seq_display = f"{saved_rec['seq_num']:05d}" if saved_rec.get("seq_num") is not None else "Custom"
        header = "✅ Manual Signup Completion Recorded" if is_new else "ℹ️ Already Recorded in Database"

        return (
            f"<b>{header}</b>\n\n"
            f"• <b>Username:</b> <code>@{html.escape(saved_rec['username'])}</code>\n"
            f"• <b>Sequence Number:</b> <code>{seq_display}</code>\n"
            f"• <b>X User ID:</b> <code>{html.escape(saved_rec['x_user_id'])}</code>\n"
            f"• <b>Display Name:</b> {html.escape(saved_rec['x_name'])}\n"
            f"• <b>Confirmed At:</b> <code>{html.escape(saved_rec['confirmed_at'])}</code>",
            build_main_menu_keyboard(),
        )

    # Command handlers
    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        sess = await asyncio.to_thread(db.get_active_session)
        active_uname = sess["username"] if sess else None
        text = (
            "<b>🎃 Holoween Sequential Account Manager</b>\n\n"
            "Welcome. Manage persistent sequential handles, track manual X signups, "
            "and verify public profiles via read-only official X API v2.\n\n"
            "Use /help to view commands."
        )
        await message.answer(text, reply_markup=build_main_menu_keyboard(active_uname))

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        text = (
            "<b>📖 Available Commands</b>\n\n"
            "• /start — Welcome menu\n"
            "• /help — Show help\n"
            "• /menu — Inline keyboard panel\n"
            "• /next — Next sequential username\n"
            "• /session_start — Start manual session\n"
            "• /session_status — Active session details\n"
            "• /session_cancel — Cancel active session\n"
            "• <code>/verify USERNAME</code> — X API v2 profile lookup\n"
            "• /accounts — List confirmed accounts\n"
            "• <code>/account USERNAME</code> — Show specific account details\n"
            "• /stats — Statistics & sequence counters\n"
            "• /health — System health check\n"
            "• /logs — View recent log lines"
        )
        await message.answer(text, reply_markup=build_main_menu_keyboard())

    @router.message(Command("menu"))
    async def cmd_menu(message: Message) -> None:
        sess = await asyncio.to_thread(db.get_active_session)
        active_uname = sess["username"] if sess else None
        await message.answer("<b>🎛 Control Menu</b>", reply_markup=build_main_menu_keyboard(active_uname))

    @router.message(Command("next"))
    async def cmd_next(message: Message) -> None:
        text, kb = await render_next_text()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("session_start"))
    async def cmd_session_start(message: Message) -> None:
        text, kb = await render_session_start()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("session_status"))
    async def cmd_session_status(message: Message) -> None:
        text, kb = await render_session_status()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("session_cancel"))
    async def cmd_session_cancel(message: Message) -> None:
        text, kb = await render_session_cancel()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("verify"))
    async def cmd_verify(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if not arg:
            await message.answer("⚠️ Please specify a username. Example: <code>/verify Holoween00111</code>")
            return
        text, kb = await execute_verification(arg, confirm_ownership=False)
        await message.answer(text, reply_markup=kb)

    @router.message(Command("accounts"))
    async def cmd_accounts(message: Message) -> None:
        text, kb = await render_accounts()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("account"))
    async def cmd_account_detail(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lstrip("@")
        if not arg:
            await message.answer("⚠️ Provide a username. Example: <code>/account Holoween00111</code>")
            return
        rec = await asyncio.to_thread(db.get_account_by_username, arg)
        if not rec:
            await message.answer(f"ℹ️ No local record for <code>@{html.escape(arg)}</code>.")
            return
        seq_str = f"{rec['seq_num']:05d}" if rec['seq_num'] is not None else "Custom"
        text = (
            "<b>🗂 Account Record</b>\n\n"
            f"• <b>Username:</b> <code>@{html.escape(rec['username'])}</code>\n"
            f"• <b>Sequence:</b> <code>{seq_str}</code>\n"
            f"• <b>X ID:</b> <code>{html.escape(rec['x_user_id'])}</code>\n"
            f"• <b>Name:</b> {html.escape(rec['x_name'])}\n"
            f"• <b>Confirmed At:</b> <code>{html.escape(rec['confirmed_at'])}</code>"
        )
        await message.answer(text, reply_markup=build_main_menu_keyboard())

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        text, kb = await render_stats()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("health"))
    async def cmd_health(message: Message) -> None:
        text, kb = await render_health()
        await message.answer(text, reply_markup=kb)

    @router.message(Command("logs"))
    async def cmd_logs(message: Message) -> None:
        raw_logs = await asyncio.to_thread(read_recent_logs, config.log_path, 25)
        escaped = html.escape(raw_logs[-3500:])
        text = f"<b>📜 Recent Logs</b>\n<pre>{escaped}</pre>"
        await message.answer(text, reply_markup=build_main_menu_keyboard())

    # Callbacks
    @router.callback_query(F.data.startswith("cmd:"))
    async def handle_menu_callbacks(callback: CallbackQuery) -> None:
        action = (callback.data or "").split(":", 1)[1]
        if action == "next":
            text, kb = await render_next_text()
        elif action == "session_start":
            text, kb = await render_session_start()
        elif action == "session_status":
            text, kb = await render_session_status()
        elif action == "session_cancel":
            text, kb = await render_session_cancel()
        elif action == "accounts":
            text, kb = await render_accounts()
        elif action == "stats":
            text, kb = await render_stats()
        elif action == "health":
            text, kb = await render_health()
        elif action == "logs":
            raw_logs = await asyncio.to_thread(read_recent_logs, config.log_path, 20)
            text = f"<b>📜 Recent Logs</b>\n<pre>{html.escape(raw_logs[-3500:])}</pre>"
            kb = build_main_menu_keyboard()
        else:
            sess = await asyncio.to_thread(db.get_active_session)
            active_uname = sess["username"] if sess else None
            text = "<b>🎛 Control Menu</b>"
            kb = build_main_menu_keyboard(active_uname)

        if callback.message:
            await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    @router.callback_query(F.data.startswith("verify:"))
    async def handle_verify_callback(callback: CallbackQuery) -> None:
        uname = (callback.data or "").split(":", 1)[1]
        await callback.answer(f"Querying X API for @{uname}...")
        text, kb = await execute_verification(uname, confirm_ownership=False)
        if callback.message:
            await callback.message.answer(text, reply_markup=kb)

    @router.callback_query(F.data.startswith("confirm:"))
    async def handle_confirm_callback(callback: CallbackQuery) -> None:
        uname = (callback.data or "").split(":", 1)[1]
        await callback.answer(f"Recording completion for @{uname}...")
        text, kb = await execute_verification(uname, confirm_ownership=True)
        if callback.message:
            await callback.message.answer(text, reply_markup=kb)

    @router.errors()
    async def handle_global_error(event: ErrorEvent) -> None:
        logger_instance.exception("Unhandled exception: %s", event.exception)
        if event.update.message:
            await event.update.message.answer("⚠️ Internal error occurred. Check /logs.")
        elif event.update.callback_query:
            await event.update.callback_query.answer("⚠️ Internal error.", show_alert=True)

    return router


async def main() -> None:
    try:
        config = AppConfig()
    except ValueError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    logger_instance = setup_logging(str(config.log_path))
    logger_instance.info("Initializing Holoween Bot...")

    db = DatabaseManager(db_path=config.db_path, prefix="Holoween", start_seq=111)
    verifier = XReadOnlyVerifier(bearer_token=config.x_bearer_token)
    start_monotonic = time.monotonic()

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    admin_mw = SingleAdminMiddleware(admin_id=config.admin_id, logger_instance=logger_instance)
    dp.message.middleware(admin_mw)
    dp.callback_query.middleware(admin_mw)

    router = build_router(
        config=config,
        db=db,
        verifier=verifier,
        logger_instance=logger_instance,
        start_monotonic=start_monotonic,
    )
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger_instance.info("Starting long-polling loop...")
        await dp.start_polling(bot)
    finally:
        logger_instance.info("Shutting down bot session cleanly.")
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
