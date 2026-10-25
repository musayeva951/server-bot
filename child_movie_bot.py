"""
Child movie bot — tayyor kino bot shabloni (server bot ishga tushiradigan nusxa).

Anime shabloni (child_anime_bot.py) bilan bir xil printsip, faqat
qismlar o'rniga bitta film bor: har bir film — nom + bitta video.
Server bot bu faylni har bir kino-bot uchun alohida jarayon qilib
ishga tushiradi. Muhit (env) orqali oladi:
    CHILD_BOT_TOKEN — shu nusxaning bot tokeni (faqat shu botniki)
    CHILD_DB_PATH   — shu nusxaning ALOHIDA SQLite bazasi yo'li
    OWNER_ID        — bot egasi (admin) Telegram ID si

Imkoniyatlar:
  Foydalanuvchi:
    * /start — salom + kinolar ro'yxati
    * Kino ro'yxati (inline tugmalar) -> video tomosha qilish
    * Qidiruv: shunchaki nom yozsa, nom bo'yicha qidiradi
  Admin (faqat egasi, OWNER_ID):
    * "Kino qo'shish" — nom so'raydi, keyin video qabul qiladi va
      file_id ni saqlaydi. Video aynan shu botga yuborilgani uchun
      file_id shu botda ishlaydi.
    * "🛡 Admin panel" — faqat egasiga ochiq: statistika, foydalanuvchilar
      ro'yxati, kino o'chirish, barcha foydalanuvchilarga e'lon yuborish.

  Egasi ID si avtomatik: server bot botni yaratgan odamning Telegram
  ID sini o'zi aniqlab, OWNER_ID qilib botga beradi — egasi ID ni qo'lda
  kiritishi shart emas. Botga kirgan har bir foydalanuvchi users jadvaliga
  yoziladi, admin panel shundan foydalanuvchilarni ko'rsatadi.

Eslatma: filmni botga adminning o'zi yuklaydi — yuklangan kontent
uchun admin javobgar. Bu shablon tashqi saytlardan kino yuklab
olmaydi, pirat katalog qidirmaydi.
"""

import logging
import os
import sqlite3
from pathlib import Path

TOKEN = os.environ.get("CHILD_BOT_TOKEN", "")
DB_PATH = Path(os.environ.get("CHILD_DB_PATH", "child_movie.db"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("child_movie_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)


def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS movies(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         title TEXT NOT NULL,
                         file_id TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
                         user_id INTEGER PRIMARY KEY,
                         username TEXT DEFAULT '',
                         first_seen TEXT DEFAULT '')""")


def track_user(user_id: int, username: str = ""):
    import datetime as _dt
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
            (user_id, username or "", _dt.datetime.now().isoformat(timespec="seconds")),
        )


def count_users() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users(limit: int = 20):
    with conn() as c:
        return c.execute("SELECT * FROM users ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()


def stats_text() -> str:
    with conn() as c:
        n_movie = c.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    return (
        f"📊 Statistika\n\n"
        f"👥 Foydalanuvchilar: {count_users()} ta\n"
        f"🎬 Kinolar: {n_movie} ta\n"
        f"🛡 Egasi ID: {OWNER_ID}"
    )


def delete_movie(movie_id: int):
    with conn() as c:
        c.execute("DELETE FROM movies WHERE id=?", (movie_id,))


def list_movies(query: str = ""):
    with conn() as c:
        if query:
            return c.execute(
                "SELECT * FROM movies WHERE title LIKE ? ORDER BY id",
                (f"%{query}%",),
            ).fetchall()
        return c.execute("SELECT * FROM movies ORDER BY id").fetchall()


def get_movie(movie_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()


def add_movie(title: str, file_id: str) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO movies(title, file_id) VALUES(?,?)", (title.strip(), file_id))
        return cur.lastrowid


def build_app():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    BTN_LIST = "🎬 Kinolar ro'yxati"
    BTN_ADD = "➕ Kino qo'shish"
    BTN_ADMIN = "🛡 Admin panel"

    def is_owner(update) -> bool:
        return update.effective_user and update.effective_user.id == OWNER_ID

    def menu_for(update):
        if is_owner(update):
            return ReplyKeyboardMarkup([[BTN_LIST], [BTN_ADD], [BTN_ADMIN]], resize_keyboard=True)
        return ReplyKeyboardMarkup([[BTN_LIST]], resize_keyboard=True)

    def admin_keyboard():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistika", callback_data="admin:stats"),
             InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin:users")],
            [InlineKeyboardButton("🗑 Kino o'chirish", callback_data="admin:delete"),
             InlineKeyboardButton("📢 E'lon yuborish", callback_data="admin:broadcast")],
        ])

    async def on_admin_callback(update, context):
        q = update.callback_query
        await q.answer()
        if not is_owner(update):
            return
        action = (q.data or "").split(":", 1)[1] if ":" in (q.data or "") else ""
        if action == "stats":
            await q.edit_message_text(stats_text(), reply_markup=admin_keyboard())
        elif action == "users":
            rows = list_users()
            if not rows:
                await q.edit_message_text("Hozircha foydalanuvchi yo'q.", reply_markup=admin_keyboard())
                return
            lines = "\n".join(
                f"• {r['user_id']}" + (f" (@{r['username']})" if r["username"] else "")
                for r in rows
            )
            await q.edit_message_text(f"👥 Oxirgi foydalanuvchilar:\n{lines}", reply_markup=admin_keyboard())
        elif action == "delete":
            rows = list_movies()
            if not rows:
                await q.edit_message_text("O'chiriladigan kino yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"🗑 {r['title']}", callback_data=f"delmovie:{r['id']}")] for r in rows]
            )
            await q.edit_message_text("Qaysi kino o'chirilsin?", reply_markup=kb)
        elif action == "broadcast":
            context.user_data["await"] = "broadcast"
            await q.message.reply_text("📢 Barcha foydalanuvchilarga yuboriladigan e'lon matnini yozing:")

    async def on_delete_callback(update, context):
        q = update.callback_query
        await q.answer()
        if not is_owner(update):
            return
        try:
            movie_id = int((q.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            return
        movie = get_movie(movie_id)
        delete_movie(movie_id)
        await q.edit_message_text(f"🗑 O'chirildi: {movie['title'] if movie else movie_id}")

    async def send_movie_list(update, context, query=""):
        rows = list_movies(query)
        chat = update.effective_chat
        if not rows:
            text = "Hech narsa topilmadi." if query else "Hozircha kino yo'q."
            if update.message:
                await update.message.reply_text(text)
            else:
                await chat.send_message(text)
            return
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(r["title"], callback_data=f"movie:{r['id']}")] for r in rows]
        )
        text = "Topilgan kinolar:" if query else "Kinolar ro'yxati:"
        if update.message:
            await update.message.reply_text(text, reply_markup=kb)
        else:
            await chat.send_message(text, reply_markup=kb)

    async def cmd_start(update, context):
        track_user(update.effective_user.id, update.effective_user.username or "")
        await update.message.reply_text(
            "Assalomu alaykum! 👋 Kino tanlang yoki nomini yozib qidiring.",
            reply_markup=menu_for(update),
        )
        await send_movie_list(update, context)

    async def on_callback(update, context):
        q = update.callback_query
        await q.answer()
        kind, _, raw = (q.data or "").partition(":")
        if kind != "movie":
            return
        try:
            movie_id = int(raw)
        except ValueError:
            return
        movie = get_movie(movie_id)
        if not movie:
            await q.edit_message_text("Kino topilmadi.")
            return
        await q.message.reply_video(video=movie["file_id"], caption=f"🎬 {movie['title']}")

    async def handle_text(update, context):
        text = (update.message.text or "").strip()
        state = context.user_data.get("await")
        track_user(update.effective_user.id, update.effective_user.username or "")
        if is_owner(update) and text == BTN_ADMIN:
            context.user_data.pop("await", None)
            await update.message.reply_text(
                "🛡 Admin panel\nEgasi ID avtomatik o'rnatilgan — qo'lda kiritish shart emas.",
                reply_markup=admin_keyboard(),
            )
            return
        if is_owner(update) and state == "broadcast":
            context.user_data.pop("await", None)
            rows = list_users()
            sent = failed = 0
            for r in rows:
                if int(r["user_id"]) == OWNER_ID:
                    continue
                try:
                    await context.bot.send_message(chat_id=int(r["user_id"]), text=text)
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(f"📢 E'lon yuborildi: {sent} ta yetdi, {failed} ta yetmadi.")
            return
        if text == BTN_LIST:
            context.user_data.pop("await", None)
            return await send_movie_list(update, context)
        if is_owner(update) and text == BTN_ADD:
            context.user_data["await"] = "movie_title"
            await update.message.reply_text("Yangi kino nomini yozing:")
            return
        if is_owner(update) and state == "movie_title":
            context.user_data["await"] = "movie_video"
            context.user_data["movie_title"] = text
            await update.message.reply_text(f"🎬 {text} uchun video yuboring (video qilib, fayl sifatida emas).")
            return
        if is_owner(update) and state == "movie_video":
            await update.message.reply_text("Video kutyapman — video fayl yuboring.")
            return
        await send_movie_list(update, context, query=text)

    async def handle_video(update, context):
        if not is_owner(update):
            return
        if context.user_data.get("await") != "movie_video":
            await update.message.reply_text("Avval «➕ Kino qo'shish» ni bosib kino nomini yozing.")
            return
        title = context.user_data.get("movie_title", "")
        video = update.message.video or (update.message.document if update.message.document else None)
        if video is None:
            await update.message.reply_text("Video topilmadi, qayta yuboring.")
            return
        context.user_data.pop("await", None)
        context.user_data.pop("movie_title", None)
        new_id = add_movie(title, video.file_id)
        await update.message.reply_text(f"✅ Kino qo'shildi (#{new_id}): {title}")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^delmovie:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^movie:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main():
    if not TOKEN:
        print("XATO: CHILD_BOT_TOKEN env yo'q — bu faylni server bot ishga tushiradi.")
        raise SystemExit(1)
    init_db()
    app = build_app()
    log.info("Child movie bot ishga tushdi (egasi=%s, baza=%s)", OWNER_ID, DB_PATH)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
