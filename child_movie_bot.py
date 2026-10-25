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

    def is_owner(update) -> bool:
        return update.effective_user and update.effective_user.id == OWNER_ID

    def menu_for(update):
        if is_owner(update):
            return ReplyKeyboardMarkup([[BTN_LIST], [BTN_ADD]], resize_keyboard=True)
        return ReplyKeyboardMarkup([[BTN_LIST]], resize_keyboard=True)

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
