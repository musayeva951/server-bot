"""
Child anime bot — tayyor anime bot shabloni (server bot ishga tushiradigan nusxa).

Server bot bu faylni har bir token-usuli boti uchun alohida jarayon qilib
ishga tushiradi. Muhit (env) orqali oladi:
    CHILD_BOT_TOKEN — shu nusxaning bot tokeni (faqat shu botniki)
    CHILD_DB_PATH   — shu nusxaning ALOHIDA SQLite bazasi yo'li
    OWNER_ID        — bot egasi (admin) Telegram ID si

Imkoniyatlar (MVP, ixcham):
  Foydalanuvchi:
    * /start — salom + anime ro'yxati tugmasi
    * Anime ro'yxati (inline tugmalar) -> qismlar ro'yxati -> video
    * Qidiruv: shunchaki nom yozsa, nom bo'yicha qidiradi
  Admin (faqat egasi, OWNER_ID):
    * "Anime qo'shish" — nom so'raydi, bazaga qo'shadi
    * "Qism qo'shish" — avval anime tanlaydi (inline), keyin video qabul
      qiladi va file_id ni saqlaydi. Video aynan shu botga yuborilgani
      uchun file_id shu botda ishlaydi (boshqa botdan ko'chirilgan
      file_id ishlamasligi mumkin — shuning uchun har bot o'z bazasi bilan).
    * "🛡 Admin panel" — faqat egasiga ochiq: statistika, foydalanuvchilar
      ro'yxati, anime o'chirish, barcha foydalanuvchilarga e'lon yuborish.

  Egasi ID si avtomatik: server bot botni yaratgan odamning Telegram
  ID sini o'zi aniqlab, OWNER_ID qilib botga beradi — egasi ID ni qo'lda
  kiritishi shart emas. Botga kirgan har bir foydalanuvchi users jadvaliga
  yoziladi, admin panel shundan foydalanuvchilarni ko'rsatadi.

MVP ga KIRMAYDI (atay qoldirilgan): majburiy obuna, premium/to'lov,
post kanallari, shorts, sayt API. Kerak bo'lsa keyingi versiyada qo'shiladi.
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path

TOKEN = os.environ.get("CHILD_BOT_TOKEN", "")
DB_PATH = Path(os.environ.get("CHILD_DB_PATH", "child.db"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("child_anime_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- baza

def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS animes(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         title TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS episodes(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         anime_id INTEGER NOT NULL REFERENCES animes(id),
                         ep_no INTEGER NOT NULL,
                         title TEXT DEFAULT '',
                         file_id TEXT NOT NULL)""")
        # Admin panel uchun: botga kirgan foydalanuvchilar (ID avtomatik).
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
        return c.execute(
            "SELECT * FROM users ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()


def stats_text() -> str:
    with conn() as c:
        n_anime = c.execute("SELECT COUNT(*) FROM animes").fetchone()[0]
        n_ep = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    return (
        f"📊 Statistika\n\n"
        f"👥 Foydalanuvchilar: {count_users()} ta\n"
        f"🎬 Animelar: {n_anime} ta\n"
        f"🎞 Qismlar: {n_ep} ta\n"
        f"🛡 Egasi ID: {OWNER_ID}"
    )


def delete_anime(anime_id: int):
    with conn() as c:
        c.execute("DELETE FROM episodes WHERE anime_id=?", (anime_id,))
        c.execute("DELETE FROM animes WHERE id=?", (anime_id,))


def list_animes(query: str = ""):
    with conn() as c:
        if query:
            return c.execute(
                "SELECT * FROM animes WHERE title LIKE ? ORDER BY id",
                (f"%{query}%",),
            ).fetchall()
        return c.execute("SELECT * FROM animes ORDER BY id").fetchall()


def get_anime(anime_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM animes WHERE id=?", (anime_id,)).fetchone()


def list_episodes(anime_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM episodes WHERE anime_id=? ORDER BY ep_no, id", (anime_id,)
        ).fetchall()


def add_anime(title: str) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO animes(title) VALUES(?)", (title.strip(),))
        return cur.lastrowid


def add_episode(anime_id: int, file_id: str, title: str = "") -> int:
    with conn() as c:
        nxt = c.execute(
            "SELECT COALESCE(MAX(ep_no),0)+1 FROM episodes WHERE anime_id=?", (anime_id,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO episodes(anime_id, ep_no, title, file_id) VALUES(?,?,?,?)",
            (anime_id, nxt, title, file_id),
        )
        return nxt


def get_episode(ep_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()


# ---------------------------------------------------------------- bot

def build_app():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    BTN_LIST = "📋 Anime ro'yxati"
    BTN_ADD_ANIME = "➕ Anime qo'shish"
    BTN_ADD_EP = "🎞 Qism qo'shish"
    BTN_ADMIN = "🛡 Admin panel"

    def is_owner(update) -> bool:
        return update.effective_user and update.effective_user.id == OWNER_ID

    def menu_for(update):
        if is_owner(update):
            return ReplyKeyboardMarkup(
                [[BTN_LIST], [BTN_ADD_ANIME, BTN_ADD_EP], [BTN_ADMIN]], resize_keyboard=True
            )
        return ReplyKeyboardMarkup([[BTN_LIST]], resize_keyboard=True)

    def admin_keyboard():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistika", callback_data="admin:stats"),
             InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin:users")],
            [InlineKeyboardButton("🗑 Anime o'chirish", callback_data="admin:delete"),
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
            rows = list_animes()
            if not rows:
                await q.edit_message_text("O'chiriladigan anime yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"🗑 {r['title']}", callback_data=f"delanime:{r['id']}")] for r in rows]
            )
            await q.edit_message_text("Qaysi anime o'chirilsin? (qismlari ham o'chadi)", reply_markup=kb)
        elif action == "broadcast":
            context.user_data["await"] = "broadcast"
            await q.message.reply_text("📢 Barcha foydalanuvchilarga yuboriladigan e'lon matnini yozing:")

    async def on_delete_callback(update, context):
        q = update.callback_query
        await q.answer()
        if not is_owner(update):
            return
        try:
            anime_id = int((q.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            return
        anime = get_anime(anime_id)
        delete_anime(anime_id)
        await q.edit_message_text(f"🗑 O'chirildi: {anime['title'] if anime else anime_id}")

    def animes_keyboard(rows, prefix="anime"):
        kb = [[InlineKeyboardButton(r["title"], callback_data=f"{prefix}:{r['id']}")] for r in rows]
        return InlineKeyboardMarkup(kb) if kb else None

    async def send_anime_list(update, context, query=""):
        rows = list_animes(query)
        chat = update.effective_chat
        if not rows:
            text = "Hech narsa topilmadi." if query else "Hozircha anime yo'q."
            if update.message:
                await update.message.reply_text(text)
            else:
                await chat.send_message(text)
            return
        kb = animes_keyboard(rows)
        text = "Topilgan animelar:" if query else "Animelar ro'yxati:"
        if update.message:
            await update.message.reply_text(text, reply_markup=kb)
        else:
            await chat.send_message(text, reply_markup=kb)

    async def cmd_start(update, context):
        track_user(update.effective_user.id, update.effective_user.username or "")
        await update.message.reply_text(
            "Assalomu alaykum! 👋 Anime tanlang yoki nomini yozib qidiring.",
            reply_markup=menu_for(update),
        )
        await send_anime_list(update, context)

    async def on_callback(update, context):
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        kind, _, raw = data.partition(":")
        try:
            obj_id = int(raw)
        except ValueError:
            return
        if kind == "anime":
            anime = get_anime(obj_id)
            if not anime:
                await q.edit_message_text("Anime topilmadi.")
                return
            eps = list_episodes(obj_id)
            if not eps:
                await q.edit_message_text(f"🎬 {anime['title']}\nHozircha qism yo'q.")
                return
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"{e['ep_no']}-qism", callback_data=f"ep:{e['id']}")] for e in eps]
            )
            await q.edit_message_text(f"🎬 {anime['title']} — qismni tanlang:", reply_markup=kb)
        elif kind == "ep":
            ep = get_episode(obj_id)
            if not ep:
                await q.edit_message_text("Qism topilmadi.")
                return
            anime = get_anime(ep["anime_id"])
            caption = f"{anime['title']} — {ep['ep_no']}-qism" if anime else f"{ep['ep_no']}-qism"
            await q.message.reply_video(video=ep["file_id"], caption=caption)
        elif kind == "epadd":
            if not is_owner(update):
                return
            context.user_data["await"] = "video"
            context.user_data["ep_anime_id"] = obj_id
            anime = get_anime(obj_id)
            await q.edit_message_text(
                f"🎬 {anime['title'] if anime else ''} uchun video yuboring (fayl sifatida emas, video qilib)."
            )

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
            return await send_anime_list(update, context)
        if is_owner(update) and text == BTN_ADD_ANIME:
            context.user_data["await"] = "anime_title"
            await update.message.reply_text("Yangi anime nomini yozing:")
            return
        if is_owner(update) and text == BTN_ADD_EP:
            rows = list_animes()
            if not rows:
                await update.message.reply_text("Avval anime qo'shing — ro'yxat bo'sh.")
                return
            await update.message.reply_text(
                "Qaysi animega qism qo'shiladi?",
                reply_markup=animes_keyboard(rows, prefix="epadd"),
            )
            return
        if is_owner(update) and state == "anime_title":
            context.user_data.pop("await", None)
            new_id = add_anime(text)
            await update.message.reply_text(f"✅ Anime qo'shildi (#{new_id}): {text}")
            return
        if is_owner(update) and state == "video":
            await update.message.reply_text("Video kutyapman — video fayl yuboring.")
            return
        # Oddiy foydalanuvchi (va egasi bo'sh vaqtda) — qidiruv
        await send_anime_list(update, context, query=text)

    async def handle_video(update, context):
        if not is_owner(update):
            return
        if context.user_data.get("await") != "video":
            await update.message.reply_text("Avval «🎞 Qism qo'shish» ni bosib anime tanlang.")
            return
        anime_id = context.user_data.get("ep_anime_id")
        video = update.message.video or (update.message.document if update.message.document else None)
        if video is None:
            await update.message.reply_text("Video topilmadi, qayta yuboring.")
            return
        context.user_data.pop("await", None)
        ep_no = add_episode(anime_id, video.file_id, getattr(video, "file_name", "") or "")
        anime = get_anime(anime_id)
        await update.message.reply_text(
            f"✅ {anime['title'] if anime else ''} — {ep_no}-qism saqlandi."
        )

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^delanime:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(anime|ep|epadd):"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main():
    if not TOKEN:
        print("XATO: CHILD_BOT_TOKEN env yo'q — bu faylni server bot ishga tushiradi.")
        sys.exit(1)
    if not OWNER_ID:
        print("OGOHLANTIRISH: OWNER_ID yo'q — admin funksiyalari hech kimga ochilmaydi.")
    init_db()
    app = build_app()
    log.info("Child anime bot ishga tushdi (egasi=%s, baza=%s)", OWNER_ID, DB_PATH)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
