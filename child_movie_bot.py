"""
Child movie bot — katta, dizaynli kino bot shabloni (server bot ishga tushiradi).

Anime shabloni (child_anime_bot.py) bilan bir xil printsip va bir xil
katta funksiyalar, faqat qismlar o'rniga bitta film bor: har bir film —
nom + poster + bitta video. Server bot bu faylni har bir kino-bot uchun
alohida jarayon qilib, shablon faylning O'ZINI ishga tushiradi (nusxa
ko'chirmaydi). Shuning uchun shablon yangilansa, mavjud token botlar
keyingi qayta ishga tushishda yangi versiyani oladi — bazasi saqlanadi,
init_db() eski bazaga yangi ustun/jadvallarni migratsiya qilib qo'shadi.
Kod-usuli botlar bu shablondan foydalanmaydi.

Muhit (env) orqali oladi:
    CHILD_BOT_TOKEN — shu nusxaning bot tokeni (faqat shu botniki)
    CHILD_DB_PATH   — shu nusxaning ALOHIDA SQLite bazasi yo'li
    OWNER_ID        — bot egasi (admin) Telegram ID si

Imkoniyatlar (Bot #1 darajasida, soddalashtirilgan):
  Foydalanuvchi:
    * /start — chiroyli salom, asosiy menyu
    * 🎬 Kinolar ro'yxati, 🆕 Yangilar, 🔥 Mashhur (ko'p ko'rilganlar)
    * 🔍 Qidiruv (nom yozish) va kod orqali topish (faqat raqam yozish)
    * Kino kartasi: poster, kod, ko'rilganlar soni, premium belgisi,
      «▶ Tomosha qilish» tugmasi
    * 🎬 Treylerlar — admin yuklagan qisqa videolar (short kabi)
    * Majburiy obuna: admin belgilagan kanallarga obuna bo'lmasa, bot
      kontent bermaydi
    * Premium kino faqat premium foydalanuvchiga ochiq
  Admin (faqat egasi, OWNER_ID — avtomatik):
    * ➕ Kino qo'shish (nom -> poster ixtiyoriy -> video)
    * ✏️ Tahrirlash: nom, poster, video, premium yoqish/o'chirish
    * 🎬 Treyler qo'shish / o'chirish
    * 🛡 Admin panel (bo'limlar): Statistika, Foydalanuvchilar,
      Tahrirlash, O'chirish, E'lonlar, Obuna kanallari, Post kanallari,
      Saqlash kanali, Baza nusxasi, Premium berish/olish
    * 📤 Post joylash — kino postini post kanallariga yuborish
    * 📦 Saqlash kanali (ko'chma baza): poster/video/treyler saqlash
      kanaliga ham nusxalanadi; file_id ishlamasa, bot kanaldan
      copy_message qilib yuboradi

  Port qilinmadi (halol cheklov): Bot #1 dagi MTProto orqali >20 MB
  video uzatish bu shablonda YO'Q — Bot API limiti (20 MB) bu yerda ham
  amal qiladi. TMDB jonli ma'lumot ham qo'shilmadi — tashqi API ga
  bog'liq bo'lib, xatolikda botni buzmasligi uchun atay qoldirildi.
  Filmni botga adminning o'zi yuklaydi — shablon tashqi saytlardan
  kontent yuklab olmaydi, pirat katalog qidirmaydi.
"""

import logging
import os
import shutil
import sqlite3
import sys
import tempfile
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


def _table_cols(c, table):
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_col(c, table, col, ddl):
    if col not in _table_cols(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS movies(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         title TEXT NOT NULL,
                         file_id TEXT NOT NULL,
                         poster_file_id TEXT DEFAULT '',
                         poster_msg_id INTEGER DEFAULT 0,
                         storage_msg_id INTEGER DEFAULT 0,
                         is_premium INTEGER DEFAULT 0,
                         views INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS trailers(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         movie_id INTEGER NOT NULL REFERENCES movies(id),
                         title TEXT DEFAULT '',
                         file_id TEXT NOT NULL,
                         storage_msg_id INTEGER DEFAULT 0,
                         views INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
                         user_id INTEGER PRIMARY KEY,
                         username TEXT DEFAULT '',
                         first_seen TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS premium_users(
                         user_id INTEGER PRIMARY KEY,
                         username TEXT DEFAULT '')""")
        c.execute("CREATE TABLE IF NOT EXISTS channels(username TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE IF NOT EXISTS post_channels(username TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT DEFAULT '')")
        for col, ddl in [("poster_file_id", "TEXT DEFAULT ''"), ("poster_msg_id", "INTEGER DEFAULT 0"),
                         ("storage_msg_id", "INTEGER DEFAULT 0"), ("is_premium", "INTEGER DEFAULT 0"),
                         ("views", "INTEGER DEFAULT 0")]:
            _ensure_col(c, "movies", col, ddl)


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


def is_premium_user(user_id: int) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM premium_users WHERE user_id=?", (user_id,)).fetchone() is not None


def give_premium(user_id: int, username: str = ""):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO premium_users(user_id, username) VALUES(?,?)", (user_id, username or ""))


def revoke_premium(user_id: int):
    with conn() as c:
        c.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))


def list_premium(limit: int = 20):
    with conn() as c:
        return c.execute("SELECT * FROM premium_users ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()


def get_channels():
    with conn() as c:
        return [r[0] for r in c.execute("SELECT username FROM channels ORDER BY username").fetchall()]


def add_channel(username: str):
    username = username.strip().lstrip("@")
    if not username:
        return False
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO channels(username) VALUES(?)", (username,))
    return True


def del_channel(username: str):
    with conn() as c:
        c.execute("DELETE FROM channels WHERE username=?", (username.strip().lstrip("@"),))


def get_post_channels():
    with conn() as c:
        return [r[0] for r in c.execute("SELECT username FROM post_channels ORDER BY username").fetchall()]


def add_post_channel(username: str):
    username = username.strip().lstrip("@")
    if not username:
        return False
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO post_channels(username) VALUES(?)", (username,))
    return True


def del_post_channel(username: str):
    with conn() as c:
        c.execute("DELETE FROM post_channels WHERE username=?", (username.strip().lstrip("@"),))


def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))


def stats_text() -> str:
    with conn() as c:
        n_movie = c.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        n_tr = c.execute("SELECT COUNT(*) FROM trailers").fetchone()[0]
        n_prem = c.execute("SELECT COUNT(*) FROM premium_users").fetchone()[0]
        n_ch = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    return (
        f"📊 Statistika\n\n"
        f"👥 Foydalanuvchilar: {count_users()} ta\n"
        f"⭐ Premium foydalanuvchilar: {n_prem} ta\n"
        f"🎬 Kinolar: {n_movie} ta\n"
        f"🎬 Treylerlar: {n_tr} ta\n"
        f"📺 Obuna kanallari: {n_ch} ta\n"
        f"🛡 Egasi ID: {OWNER_ID}"
    )


def list_movies(query: str = "", order: str = "id"):
    order_sql = {"id": "id", "new": "id DESC", "top": "views DESC, id DESC"}.get(order, "id")
    with conn() as c:
        if query:
            return c.execute(
                f"SELECT * FROM movies WHERE title LIKE ? ORDER BY {order_sql}",
                (f"%{query}%",),
            ).fetchall()
        return c.execute(f"SELECT * FROM movies ORDER BY {order_sql}").fetchall()


def newest_movies(limit: int = 10):
    with conn() as c:
        return c.execute("SELECT * FROM movies ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def top_movies(limit: int = 10):
    with conn() as c:
        return c.execute("SELECT * FROM movies ORDER BY views DESC, id DESC LIMIT ?", (limit,)).fetchall()


def get_movie(movie_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()


def add_movie(title: str, file_id: str, poster_file_id: str = "", poster_msg_id: int = 0,
              storage_msg_id: int = 0, is_premium: int = 0) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO movies(title, file_id, poster_file_id, poster_msg_id, storage_msg_id, is_premium) "
            "VALUES(?,?,?,?,?,?)",
            (title.strip(), file_id, poster_file_id or "", int(poster_msg_id), int(storage_msg_id), int(is_premium)),
        )
        return cur.lastrowid


def update_movie(movie_id: int, title=None, file_id=None, poster_file_id=None, poster_msg_id=None,
                 storage_msg_id=None, is_premium=None):
    with conn() as c:
        if title is not None:
            c.execute("UPDATE movies SET title=? WHERE id=?", (title.strip(), movie_id))
        if file_id is not None:
            c.execute("UPDATE movies SET file_id=? WHERE id=?", (file_id, movie_id))
        if poster_file_id is not None:
            c.execute("UPDATE movies SET poster_file_id=? WHERE id=?", (poster_file_id, movie_id))
        if poster_msg_id is not None:
            c.execute("UPDATE movies SET poster_msg_id=? WHERE id=?", (int(poster_msg_id), movie_id))
        if storage_msg_id is not None:
            c.execute("UPDATE movies SET storage_msg_id=? WHERE id=?", (int(storage_msg_id), movie_id))
        if is_premium is not None:
            c.execute("UPDATE movies SET is_premium=? WHERE id=?", (int(is_premium), movie_id))


def delete_movie(movie_id: int):
    with conn() as c:
        c.execute("DELETE FROM trailers WHERE movie_id=?", (movie_id,))
        c.execute("DELETE FROM movies WHERE id=?", (movie_id,))


def inc_movie_views(movie_id: int):
    with conn() as c:
        c.execute("UPDATE movies SET views = COALESCE(views,0)+1 WHERE id=?", (movie_id,))


def list_trailers(movie_id=None):
    with conn() as c:
        if movie_id:
            return c.execute("SELECT * FROM trailers WHERE movie_id=? ORDER BY id DESC", (movie_id,)).fetchall()
        return c.execute("SELECT * FROM trailers ORDER BY id DESC LIMIT 30").fetchall()


def get_trailer(trailer_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM trailers WHERE id=?", (trailer_id,)).fetchone()


def add_trailer(movie_id: int, file_id: str, title: str = "", storage_msg_id: int = 0) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO trailers(movie_id, title, file_id, storage_msg_id) VALUES(?,?,?,?)",
            (movie_id, title.strip(), file_id, int(storage_msg_id)),
        )
        return cur.lastrowid


def delete_trailer(trailer_id: int):
    with conn() as c:
        c.execute("DELETE FROM trailers WHERE id=?", (trailer_id,))


def inc_trailer_views(trailer_id: int):
    with conn() as c:
        c.execute("UPDATE trailers SET views = COALESCE(views,0)+1 WHERE id=?", (trailer_id,))


def movie_card(movie) -> str:
    prem = " ⭐ Premium" if movie["is_premium"] else ""
    return (
        f"🎬 {movie['title']}{prem}\n"
        f"🔢 Kod: {movie['id']}\n"
        f"👁 Ko'rilgan: {movie['views'] or 0} marta"
    )


# ---------------------------------------------------------------- bot

def build_app():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    BTN_LIST = "🎬 Kinolar ro'yxati"
    BTN_NEW = "🆕 Yangilar"
    BTN_TOP = "🔥 Mashhur"
    BTN_TRAILERS = "🎬 Treylerlar"
    BTN_ADD = "➕ Kino qo'shish"
    BTN_ADD_TR = "🎬 Treyler qo'shish"
    BTN_ADMIN = "🛡 Admin panel"

    def is_owner(update) -> bool:
        return update.effective_user and update.effective_user.id == OWNER_ID

    def menu_for(update):
        if is_owner(update):
            return ReplyKeyboardMarkup(
                [[BTN_LIST, BTN_NEW], [BTN_TOP, BTN_TRAILERS], [BTN_ADD, BTN_ADD_TR], [BTN_ADMIN]],
                resize_keyboard=True,
            )
        return ReplyKeyboardMarkup(
            [[BTN_LIST, BTN_NEW], [BTN_TOP, BTN_TRAILERS]], resize_keyboard=True
        )

    def admin_keyboard():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistika", callback_data="admin:stats"),
             InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin:users")],
            [InlineKeyboardButton("✏️ Tahrirlash", callback_data="admin:edit"),
             InlineKeyboardButton("🗑 O'chirish", callback_data="admin:delete")],
            [InlineKeyboardButton("📢 E'lon yuborish", callback_data="admin:broadcast"),
             InlineKeyboardButton("📤 Post joylash", callback_data="admin:post")],
            [InlineKeyboardButton("📺 Obuna kanallari", callback_data="admin:channels"),
             InlineKeyboardButton("📦 Saqlash kanali", callback_data="admin:storage")],
            [InlineKeyboardButton("⭐ Premium berish/olish", callback_data="admin:premium"),
             InlineKeyboardButton("💾 Baza nusxasi", callback_data="admin:backup")],
        ])

    async def check_subscription(update, context) -> bool:
        if is_owner(update):
            return True
        channels = get_channels()
        if not channels:
            return True
        uid = update.effective_user.id
        missing = []
        for ch in channels:
            try:
                m = await context.bot.get_chat_member(chat_id="@" + ch, user_id=uid)
                if m.status in ("member", "administrator", "creator", "restricted"):
                    continue
                missing.append(ch)
            except Exception:
                continue
        if not missing:
            return True
        kb = [[InlineKeyboardButton(f"📺 @{ch}", url=f"https://t.me/{ch}")] for ch in missing]
        kb.append([InlineKeyboardButton("✅ Obuna bo'ldim, tekshirish", callback_data="back:list")])
        text = ("❌ Botdan foydalanish uchun avval quyidagi kanallarga obuna bo'ling:\n\n"
                + "\n".join(f"• @{ch}" for ch in missing)
                + "\n\nObuna bo'lgach, «✅ Obuna bo'ldim» ni bosing.")
        try:
            if update.callback_query:
                await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
            else:
                await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return False

    def premium_blocked(update, movie) -> bool:
        if not movie["is_premium"]:
            return False
        if is_owner(update):
            return False
        return not is_premium_user(update.effective_user.id)

    async def send_via_storage_or_file(context, chat_id, file_id, storage_msg_id, caption=None, is_photo=False):
        storage = get_setting("storage_channel", "")
        if storage and storage_msg_id:
            try:
                sid = int(storage) if storage.lstrip("-").isdigit() else storage
                await context.bot.copy_message(chat_id=chat_id, from_chat_id=sid,
                                               message_id=int(storage_msg_id), caption=caption)
                return True
            except Exception as e:
                log.warning("Saqlash kanalidan yuborilmadi, file_id ishlatiladi: %s", e)
        if is_photo:
            await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
        else:
            await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption)
        return True

    async def storage_copy(context, update, message_id: int) -> int:
        storage = get_setting("storage_channel", "")
        if not storage:
            return 0
        try:
            sid = int(storage) if storage.lstrip("-").isdigit() else storage
            msg = await context.bot.copy_message(chat_id=sid, from_chat_id=update.effective_chat.id,
                                                 message_id=message_id)
            return int(msg.message_id)
        except Exception as e:
            log.warning("Saqlash kanaliga nusxalab bo'lmadi: %s", e)
            return 0

    def movies_keyboard(rows, prefix="movie"):
        kb = []
        for r in rows:
            label = f"{'⭐ ' if r['is_premium'] else ''}{r['title']} (kod {r['id']})"
            kb.append([InlineKeyboardButton(label, callback_data=f"{prefix}:{r['id']}")])
        kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="back:list")])
        return InlineKeyboardMarkup(kb)

    async def send_movie_list(update, context, query="", order="id", title="Kinolar ro'yxati:"):
        if order == "new":
            rows = newest_movies()
        elif order == "top":
            rows = top_movies()
        else:
            rows = list_movies(query, order=order)
        chat = update.effective_chat
        if not rows:
            text = "Hech narsa topilmadi." if query else "Hozircha kino yo'q."
            if update.message:
                await update.message.reply_text(text)
            else:
                await chat.send_message(text)
            return
        kb = movies_keyboard(rows)
        if update.message:
            await update.message.reply_text(title, reply_markup=kb)
        else:
            await chat.send_message(title, reply_markup=kb)

    async def show_movie(update, context, movie_id: int):
        movie = get_movie(movie_id)
        if not movie:
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶ Tomosha qilish", callback_data=f"watch:{movie_id}")],
            [InlineKeyboardButton("⬅️ Ro'yxatga qaytish", callback_data="back:list")],
        ])
        q = update.callback_query
        if movie["poster_file_id"]:
            try:
                await send_via_storage_or_file(context, update.effective_chat.id,
                                               movie["poster_file_id"], movie["poster_msg_id"],
                                               caption=movie_card(movie), is_photo=True)
                await update.effective_chat.send_message("Tomosha qilish uchun tugmani bosing:", reply_markup=kb)
                if q:
                    try:
                        await q.message.delete()
                    except Exception:
                        pass
                return
            except Exception as e:
                log.warning("Poster yuborilmadi: %s", e)
        if q:
            await q.edit_message_text(movie_card(movie), reply_markup=kb)
        else:
            await update.effective_chat.send_message(movie_card(movie), reply_markup=kb)

    async def cmd_start(update, context):
        track_user(update.effective_user.id, update.effective_user.username or "")
        if not await check_subscription(update, context):
            await update.message.reply_text(
                "Assalomu alaykum! 👋 Obuna tekshiruvidan o'tgach, bot to'liq ishlaydi.",
                reply_markup=menu_for(update),
            )
            return
        await update.message.reply_text(
            "Assalomu alaykum! 👋\n\n🎬 Kino tanlang, nomini yozib qidiring yoki kodini yuboring.\n"
            "Yangi kinolar «🆕 Yangilar» da, eng ko'p ko'rilganlar «🔥 Mashhur» da.",
            reply_markup=menu_for(update),
        )
        await send_movie_list(update, context)

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
            lines = "\n".join(f"• {r['user_id']}" + (f" (@{r['username']})" if r["username"] else "") for r in rows)
            await q.edit_message_text(f"👥 Oxirgi foydalanuvchilar:\n{lines}", reply_markup=admin_keyboard())
        elif action == "delete":
            rows = list_movies()
            if not rows:
                await q.edit_message_text("O'chiriladigan kino yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {r['title']}", callback_data=f"delmovie:{r['id']}")] for r in rows])
            await q.edit_message_text("Qaysi kino o'chirilsin? (treylerlari ham o'chadi)", reply_markup=kb)
        elif action == "edit":
            rows = list_movies()
            if not rows:
                await q.edit_message_text("Tahrirlanadigan kino yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✏️ {r['title']}", callback_data=f"editmovie:{r['id']}")] for r in rows])
            await q.edit_message_text("Qaysi kino tahrirlanadi?", reply_markup=kb)
        elif action == "broadcast":
            context.user_data["await"] = "broadcast"
            await q.message.reply_text("📢 Barcha foydalanuvchilarga yuboriladigan e'lon matnini yozing:")
        elif action == "post":
            rows = list_movies()
            if not rows:
                await q.edit_message_text("Post qilinadigan kino yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"📤 {r['title']}", callback_data=f"postmovie:{r['id']}")] for r in rows])
            await q.edit_message_text("Qaysi kino post qilinsin?", reply_markup=kb)
        elif action == "channels":
            chs = get_channels()
            pchs = get_post_channels()
            txt = ("📺 Obuna kanallari:\n" + ("\n".join(f"• @{c}" for c in chs) or "—")
                   + "\n\n📤 Post kanallari:\n" + ("\n".join(f"• @{c}" for c in pchs) or "—"))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Obuna kanal qo'shish", callback_data="admin:chadd"),
                 InlineKeyboardButton("➖ Obuna kanal o'chirish", callback_data="admin:chdel")],
                [InlineKeyboardButton("➕ Post kanal qo'shish", callback_data="admin:pchadd"),
                 InlineKeyboardButton("➖ Post kanal o'chirish", callback_data="admin:pchdel")],
                [InlineKeyboardButton("⬅️ Admin panel", callback_data="admin:back")],
            ])
            await q.edit_message_text(txt, reply_markup=kb)
        elif action == "chadd":
            context.user_data["await"] = "channel_add"
            await q.message.reply_text("✍️ Obuna kanal username'ini yozing (@ bilan yoki @ siz):")
        elif action == "chdel":
            chs = get_channels()
            if not chs:
                await q.edit_message_text("O'chiriladigan kanal yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ @{c}", callback_data=f"delchan:{c}")] for c in chs])
            await q.edit_message_text("Qaysi obuna kanal o'chirilsin?", reply_markup=kb)
        elif action == "pchadd":
            context.user_data["await"] = "post_channel_add"
            await q.message.reply_text("✍️ Post kanal username'ini yozing (@ bilan yoki @ siz).\n⚠️ Avval botni shu kanalga admin qilib qo'shing!")
        elif action == "pchdel":
            chs = get_post_channels()
            if not chs:
                await q.edit_message_text("O'chiriladigan post kanal yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ @{c}", callback_data=f"delpchan:{c}")] for c in chs])
            await q.edit_message_text("Qaysi post kanal o'chirilsin?", reply_markup=kb)
        elif action == "storage":
            cur = get_setting("storage_channel", "") or "o'rnatilmagan"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Saqlash kanalini o'rnatish", callback_data="admin:storage_set")],
                [InlineKeyboardButton("⬅️ Admin panel", callback_data="admin:back")],
            ])
            await q.edit_message_text(
                f"📦 Saqlash kanali (ko'chma baza)\n\nHozirgi: {cur}\n\n"
                "Bu kanalga yuklangan har bir poster/video/treyler nusxalanadi. "
                "Bazani boshqa botga ko'chirsangiz, o'sha bot shu kanalga admin bo'lsa — fayllar ishlaydi.\n\n"
                "1️⃣ Maxfiy kanal oching va botni unga admin qiling\n"
                "2️⃣ Kanal ID sini yuboring (masalan: -1001234567890) yoki @username",
                reply_markup=kb)
        elif action == "storage_set":
            context.user_data["await"] = "storage_set"
            await q.message.reply_text("📦 Saqlash kanali ID sini yoki @username'ini yozing (masalan: -1001234567890):")
        elif action == "premium":
            rows = list_premium()
            txt = "⭐ Premium foydalanuvchilar:\n" + ("\n".join(f"• {r['user_id']}" + (f" (@{r['username']})" if r["username"] else "") for r in rows) or "—")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Premium berish", callback_data="admin:prem_give"),
                 InlineKeyboardButton("➖ Premium olish", callback_data="admin:prem_revoke")],
                [InlineKeyboardButton("⬅️ Admin panel", callback_data="admin:back")],
            ])
            await q.edit_message_text(txt, reply_markup=kb)
        elif action == "prem_give":
            context.user_data["await"] = "prem_give"
            await q.message.reply_text("⭐ Premium beriladigan foydalanuvchi ID sini yozing:")
        elif action == "prem_revoke":
            context.user_data["await"] = "prem_revoke"
            await q.message.reply_text("⭐ Premium olinadigan foydalanuvchi ID sini yozing:")
        elif action == "backup":
            try:
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                    tmp_path = tmp.name
                shutil.copy(DB_PATH, tmp_path)
                await q.message.reply_document(document=open(tmp_path, "rb"), filename="baza.db",
                                               caption="💾 Baza nusxasi — bu faylni xavfsiz joyda saqlang.")
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            except Exception as e:
                await q.message.reply_text(f"❌ Baza nusxasini olib bo'lmadi: {e}")
        elif action == "back":
            await q.edit_message_text("🛡 Admin panel\nEgasi ID avtomatik o'rnatilgan — qo'lda kiritish shart emas.",
                                      reply_markup=admin_keyboard())

    async def on_delete_callback(update, context):
        q = update.callback_query
        await q.answer()
        if not is_owner(update):
            return
        kind, _, raw = (q.data or "").partition(":")
        if kind == "delmovie":
            try:
                mid = int(raw)
            except ValueError:
                return
            movie = get_movie(mid)
            delete_movie(mid)
            await q.edit_message_text(f"🗑 O'chirildi: {movie['title'] if movie else mid}")
        elif kind == "delchan":
            del_channel(raw)
            await q.edit_message_text(f"❌ Obuna kanal o'chirildi: @{raw}")
        elif kind == "delpchan":
            del_post_channel(raw)
            await q.edit_message_text(f"❌ Post kanal o'chirildi: @{raw}")
        elif kind == "deltrailer":
            try:
                tid = int(raw)
            except ValueError:
                return
            delete_trailer(tid)
            await q.edit_message_text("🗑 Treyler o'chirildi.")


    async def on_callback(update, context):
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        if data == "back:list":
            if not await check_subscription(update, context):
                return
            await send_movie_list(update, context)
            return
        kind, _, raw = data.partition(":")
        if kind == "movie":
            try:
                mid = int(raw)
            except ValueError:
                return
            if not await check_subscription(update, context):
                return
            movie = get_movie(mid)
            if movie and premium_blocked(update, movie):
                await q.message.reply_text("⭐ Bu kino premium — faqat premium foydalanuvchilar ko'radi.")
                return
            await show_movie(update, context, mid)
        elif kind == "watch":
            try:
                mid = int(raw)
            except ValueError:
                return
            if not await check_subscription(update, context):
                return
            movie = get_movie(mid)
            if not movie:
                await q.edit_message_text("Kino topilmadi.")
                return
            if premium_blocked(update, movie):
                await q.message.reply_text("⭐ Bu kino premium — faqat premium foydalanuvchilar ko'radi.")
                return
            inc_movie_views(mid)
            await send_via_storage_or_file(context, update.effective_chat.id, movie["file_id"],
                                           movie["storage_msg_id"], caption=f"🎬 {movie['title']}")
        elif kind == "traileradd":
            if not is_owner(update):
                return
            try:
                mid = int(raw)
            except ValueError:
                return
            context.user_data["await"] = "trailer_video"
            context.user_data["trailer_movie_id"] = mid
            await q.edit_message_text("🎬 Treyler uchun video yuboring.")
        elif kind == "trailer":
            try:
                tid = int(raw)
            except ValueError:
                return
            if not await check_subscription(update, context):
                return
            tr = get_trailer(tid)
            if not tr:
                await q.edit_message_text("Treyler topilmadi.")
                return
            movie = get_movie(tr["movie_id"])
            if movie and premium_blocked(update, movie):
                await q.message.reply_text("⭐ Bu kino premium — faqat premium foydalanuvchilar ko'radi.")
                return
            inc_trailer_views(tid)
            await send_via_storage_or_file(context, update.effective_chat.id, tr["file_id"],
                                           tr["storage_msg_id"], caption=f"🎬 {tr['title'] or (movie['title'] if movie else '')}")
        elif kind == "editmovie":
            if not is_owner(update):
                return
            try:
                mid = int(raw)
            except ValueError:
                return
            movie = get_movie(mid)
            if not movie:
                return
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Nomini o'zgartirish", callback_data=f"edittitle:{mid}"),
                 InlineKeyboardButton("🖼 Poster almashtirish", callback_data=f"editposter:{mid}")],
                [InlineKeyboardButton("🎬 Video almashtirish", callback_data=f"editvideo:{mid}"),
                 InlineKeyboardButton("⭐ Premium: " + ("O'chirish" if movie["is_premium"] else "Yoqish"), callback_data=f"editprem:{mid}")],
                [InlineKeyboardButton("🎬 Treyler o'chirish", callback_data=f"editdeltrailer:{mid}")],
                [InlineKeyboardButton("⬅️ Admin panel", callback_data="admin:back")],
            ])
            await q.edit_message_text(f"✏️ Tahrirlash: {movie['title']}\n{movie_card(movie)}", reply_markup=kb)
        elif kind == "edittitle":
            if not is_owner(update):
                return
            context.user_data["await"] = "edit_title"
            context.user_data["edit_id"] = int(raw)
            await q.message.reply_text("✏️ Yangi nomni yozing:")
        elif kind == "editposter":
            if not is_owner(update):
                return
            context.user_data["await"] = "edit_poster"
            context.user_data["edit_id"] = int(raw)
            await q.message.reply_text("🖼 Yangi poster rasmini yuboring (rasm qilib):")
        elif kind == "editvideo":
            if not is_owner(update):
                return
            context.user_data["await"] = "edit_video"
            context.user_data["edit_id"] = int(raw)
            await q.message.reply_text("🎬 Yangi video yuboring (eski video o'rniga qo'yiladi):")
        elif kind == "editprem":
            if not is_owner(update):
                return
            mid = int(raw)
            movie = get_movie(mid)
            if movie:
                update_movie(mid, is_premium=0 if movie["is_premium"] else 1)
                await q.edit_message_text(f"⭐ Premium {'o\'chirildi' if movie['is_premium'] else 'yoqildi'}: {movie['title']}")
        elif kind == "editdeltrailer":
            if not is_owner(update):
                return
            trs = list_trailers(int(raw))
            if not trs:
                await q.edit_message_text("O'chiriladigan treyler yo'q.")
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {t['title'] or t['id']}", callback_data=f"deltrailer:{t['id']}")] for t in trs])
            await q.edit_message_text("Qaysi treyler o'chirilsin?", reply_markup=kb)
        elif kind == "postmovie":
            if not is_owner(update):
                return
            mid = int(raw)
            movie = get_movie(mid)
            targets = get_post_channels()
            if not targets:
                await q.edit_message_text("❌ Avval «📺 Obuna kanallari» dan post kanal qo'shing.")
                return
            caption = f"🎬 {movie['title']}\n🔢 Kod: {movie['id']}"
            sent, failed = [], []
            for ch in targets:
                try:
                    if movie["poster_file_id"]:
                        await send_via_storage_or_file(context, "@" + ch, movie["poster_file_id"],
                                                       movie["poster_msg_id"], caption=caption, is_photo=True)
                    else:
                        await context.bot.send_message(chat_id="@" + ch, text=caption)
                    sent.append(ch)
                except Exception as e:
                    failed.append(f"@{ch}: {e}")
            await q.edit_message_text(f"📤 Post yuborildi: {len(sent)} ta kanalga yetdi.\n" + ("\n".join(failed) if failed else "Xato yo'q — bot har bir kanalda admin bo'lishi kerak."))

    async def handle_text(update, context):
        text = (update.message.text or "").strip()
        state = context.user_data.get("await")
        track_user(update.effective_user.id, update.effective_user.username or "")
        if is_owner(update) and text == BTN_ADMIN:
            context.user_data.pop("await", None)
            await update.message.reply_text("🛡 Admin panel\nEgasi ID avtomatik o'rnatilgan — qo'lda kiritish shart emas.",
                                            reply_markup=admin_keyboard())
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
        if is_owner(update) and state == "channel_add":
            context.user_data.pop("await", None)
            await update.message.reply_text("✅ Obuna kanal qo'shildi." if add_channel(text) else "❌ Kanal nomi noto'g'ri.")
            return
        if is_owner(update) and state == "post_channel_add":
            context.user_data.pop("await", None)
            await update.message.reply_text("✅ Post kanal qo'shildi." if add_post_channel(text) else "❌ Kanal nomi noto'g'ri.")
            return
        if is_owner(update) and state == "storage_set":
            context.user_data.pop("await", None)
            set_setting("storage_channel", text.strip())
            await update.message.reply_text(f"✅ Saqlash kanali o'rnatildi: {text.strip()}\nBot shu kanalda admin ekanini tekshiring.")
            return
        if is_owner(update) and state == "prem_give":
            context.user_data.pop("await", None)
            try:
                give_premium(int(text))
                await update.message.reply_text(f"✅ Premium berildi: {text}")
            except ValueError:
                await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
            return
        if is_owner(update) and state == "prem_revoke":
            context.user_data.pop("await", None)
            try:
                revoke_premium(int(text))
                await update.message.reply_text(f"✅ Premium olindi: {text}")
            except ValueError:
                await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
            return
        if is_owner(update) and state == "edit_title":
            mid = context.user_data.pop("edit_id", None)
            context.user_data.pop("await", None)
            if mid:
                update_movie(mid, title=text)
                await update.message.reply_text(f"✅ Nom o'zgartirildi: {text}")
            return
        if is_owner(update) and state == "trailer_title":
            mid = context.user_data.pop("trailer_movie_id", None)
            fid = context.user_data.pop("trailer_file_id", None)
            smsg = context.user_data.pop("trailer_storage_msg", 0)
            context.user_data.pop("await", None)
            if mid and fid:
                title = "" if text.strip() == "-" else text
                new_id = add_trailer(mid, fid, title, smsg)
                await update.message.reply_text(f"✅ Treyler saqlandi (#{new_id}).")
            return
        if text == BTN_LIST:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            return await send_movie_list(update, context)
        if text == BTN_NEW:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            return await send_movie_list(update, context, order="new", title="🆕 Yangi qo'shilgan kinolar:")
        if text == BTN_TOP:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            return await send_movie_list(update, context, order="top", title="🔥 Eng ko'p ko'rilgan kinolar:")
        if text == BTN_TRAILERS:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            trs = list_trailers()
            if not trs:
                await update.message.reply_text("Hozircha treyler yo'q.")
                return
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎬 {t['title'] or 'Treyler'}", callback_data=f"trailer:{t['id']}")] for t in trs])
            await update.message.reply_text("🎬 Treylerlar:", reply_markup=kb)
            return
        if is_owner(update) and text == BTN_ADD:
            context.user_data["await"] = "movie_title"
            await update.message.reply_text("Yangi kino nomini yozing:")
            return
        if is_owner(update) and text == BTN_ADD_TR:
            rows = list_movies()
            if not rows:
                await update.message.reply_text("Avval kino qo'shing — ro'yxat bo'sh.")
                return
            await update.message.reply_text("Qaysi kinoga treyler qo'shiladi?",
                                            reply_markup=movies_keyboard(rows, prefix="traileradd"))
            return
        if is_owner(update) and state == "movie_title":
            context.user_data["await"] = "movie_poster"
            context.user_data["movie_title"] = text
            await update.message.reply_text(
                "🖼 Poster rasmini yuboring (rasm qilib).\nPoster kerak bo'lmasa, «o'tkazib yuborish» deb yozing:")
            return
        if is_owner(update) and state == "movie_poster":
            context.user_data["await"] = "movie_video"
            await update.message.reply_text(f"🎬 {context.user_data.get('movie_title','')} uchun video yuboring (video qilib, fayl sifatida emas).")
            return
        if is_owner(update) and state == "movie_video":
            await update.message.reply_text("Video kutyapman — video fayl yuboring.")
            return
        if is_owner(update) and state in ("trailer_video", "edit_video"):
            await update.message.reply_text("Video kutyapman — video fayl yuboring.")
            return
        if is_owner(update) and state == "edit_poster":
            await update.message.reply_text("Poster rasmi kutyapman — rasm yuboring.")
            return
        if not await check_subscription(update, context):
            return
        if text.isdigit():
            movie = get_movie(int(text))
            if movie:
                if premium_blocked(update, movie):
                    await update.message.reply_text("⭐ Bu kino premium — faqat premium foydalanuvchilar ko'radi.")
                    return
                await show_movie(update, context, int(text))
                return
            await update.message.reply_text("Bu kodda kino topilmadi.")
            return
        await send_movie_list(update, context, query=text)

    async def handle_photo(update, context):
        if not is_owner(update):
            return
        state = context.user_data.get("await")
        photo = update.message.photo[-1] if update.message.photo else None
        if photo is None:
            return
        if state == "movie_poster":
            context.user_data["await"] = "movie_video"
            context.user_data["movie_poster_id"] = photo.file_id
            context.user_data["movie_poster_msg"] = await storage_copy(context, update, update.message.message_id)
            await update.message.reply_text(f"🎬 {context.user_data.get('movie_title','')} uchun video yuboring (video qilib, fayl sifatida emas).")
            return
        if state == "edit_poster":
            mid = context.user_data.pop("edit_id", None)
            context.user_data.pop("await", None)
            if mid:
                smsg = await storage_copy(context, update, update.message.message_id)
                update_movie(mid, poster_file_id=photo.file_id, poster_msg_id=smsg)
                await update.message.reply_text("✅ Poster almashtirildi.")
            return
        await update.message.reply_text("Poster qabul qilinmadi — avval kino qo'shish yoki tahrirlashni boshlang.")

    async def handle_video(update, context):
        if not is_owner(update):
            return
        state = context.user_data.get("await")
        video = update.message.video or (update.message.document if update.message.document else None)
        if video is None:
            await update.message.reply_text("Video topilmadi, qayta yuboring.")
            return
        if state == "movie_video":
            title = context.user_data.pop("movie_title", "")
            poster_id = context.user_data.pop("movie_poster_id", "")
            poster_msg = context.user_data.pop("movie_poster_msg", 0)
            context.user_data.pop("await", None)
            smsg = await storage_copy(context, update, update.message.message_id)
            new_id = add_movie(title, video.file_id, poster_file_id=poster_id,
                               poster_msg_id=poster_msg, storage_msg_id=smsg)
            await update.message.reply_text(f"✅ Kino qo'shildi (kod {new_id}): {title}")
            return
        if state == "trailer_video":
            mid = context.user_data.get("trailer_movie_id")
            smsg = await storage_copy(context, update, update.message.message_id)
            context.user_data["await"] = "trailer_title"
            context.user_data["trailer_file_id"] = video.file_id
            context.user_data["trailer_storage_msg"] = smsg
            await update.message.reply_text("🎬 Treyler sarlavhasini yozing (kerak bo'lmasa «-» yozing):")
            return
        if state == "edit_video":
            mid = context.user_data.pop("edit_id", None)
            context.user_data.pop("await", None)
            if mid:
                smsg = await storage_copy(context, update, update.message.message_id)
                update_movie(mid, file_id=video.file_id, storage_msg_id=smsg)
                await update.message.reply_text("✅ Video almashtirildi.")
            return
        await update.message.reply_text("Avval «➕ Kino qo'shish» ni bosib kino nomini yozing.")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^(delmovie|delchan|delpchan|deltrailer):"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(movie|watch|trailer|traileradd|editmovie|edittitle|editposter|editvideo|editprem|editdeltrailer|postmovie|back):"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main():
    if not TOKEN:
        print("XATO: CHILD_BOT_TOKEN env yo'q — bu faylni server bot ishga tushiradi.")
        raise SystemExit(1)
    if not OWNER_ID:
        print("OGOHLANTIRISH: OWNER_ID yo'q — admin funksiyalari hech kimga ochilmaydi.")
    init_db()
    app = build_app()
    log.info("Child movie bot ishga tushdi (egasi=%s, baza=%s)", OWNER_ID, DB_PATH)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
