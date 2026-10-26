"""
Child anime bot — katta, dizaynli anime bot shabloni (server bot ishga tushiradi).

Server bot bu faylni har bir token-usuli boti uchun alohida jarayon qilib,
shablon faylning O'ZINI ishga tushiradi (nusxa ko'chirmaydi). Shuning uchun
shablon yangilansa, mavjud token botlar keyingi qayta ishga tushishda
yangi versiyani oladi — lekin ularning bazasi saqlanib qoladi, init_db()
eski bazaga yangi ustun/jadvallarni migratsiya qilib qo'shadi, ma'lumot
o'chmaydi. Kod-usuli botlar (masalan, Bot #1) bu shablondan foydalanmaydi,
ularga yangilanish tegmaydi.

Muhit (env) orqali oladi:
    CHILD_BOT_TOKEN — shu nusxaning bot tokeni (faqat shu botniki)
    CHILD_DB_PATH   — shu nusxaning ALOHIDA SQLite bazasi yo'li
    OWNER_ID        — bot egasi (admin) Telegram ID si

Imkoniyatlar (Bot #1 — Uzbek animelar olami boti darajasida, soddalashtirilgan):
  Foydalanuvchi:
    * /start — chiroyli salom, asosiy menyu
    * 📋 Anime ro'yxati, 🆕 Yangilar, 🔥 Mashhur (ko'p ko'rilganlar)
    * 🔍 Qidiruv (nom yozish) va kod orqali topish (faqat raqam yozish)
    * Anime kartasi: poster, kod, qismlar soni, ko'rilganlar soni, premium belgisi
    * Qismlar ro'yxati -> video tomosha qilish (ko'rilganlar hisoblanadi)
    * 🎞 Shortlar — admin yuklagan qisqa videolar
    * Majburiy obuna: admin belgilagan kanallarga obuna bo'lmasa, bot
      kontent bermaydi (kanalga qo'shilish tugmalari bilan ko'rsatadi)
    * Premium anime faqat premium foydalanuvchiga ochiq
  Admin (faqat egasi, OWNER_ID — avtomatik):
    * ➕ Anime qo'shish (nom -> poster ixtiyoriy -> qismlar)
    * 🎞 Qism qo'shish, qism almashtirish (eski videoni yangisiga almashtirish)
    * ✏️ Tahrirlash: nom, poster, premium yoqish/o'chirish
    * 🎞 Short qo'shish / o'chirish
    * 🛡 Admin panel (bo'limlar): Statistika, Foydalanuvchilar,
      Tahrirlash, O'chirish, E'lonlar, Obuna kanallari, Post kanallari,
      Saqlash kanali, Baza nusxasi, Premium berish/olish
    * 📤 Post joylash — anime postini post kanallariga yuborish
    * 📦 Saqlash kanali (ko'chma baza): poster/qism/short saqlash
      kanaliga ham nusxalanadi; file_id ishlamasa, bot kanaldan
      copy_message qilib yuboradi

  Port qilinmadi (halol cheklov): Bot #1 dagi MTProto orqali >20 MB
  video uzatish bu shablonda YO'Q — Bot API limiti (20 MB) bu yerda ham
  amal qiladi, chunki shablon faqat python-telegram-bot (Bot API) bilan
  ishlaydi. AniList jonli ma'lumot ham qo'shilmadi — u tashqi API ga
  bog'liq bo'lib, oflayn/xatolikda botni buzmasligi uchun atay qoldirildi.
  Filmni botga adminning o'zi yuklaydi — shablon tashqi saytlardan
  kontent yuklab olmaydi.
"""

import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

TOKEN = os.environ.get("CHILD_BOT_TOKEN", "")
DB_PATH = Path(os.environ.get("CHILD_DB_PATH", "child.db"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
# Server bot yaratish so'rovnomasidan keladi: qaysi funksiyalar yoqilgan,
# panel qanaqa uslubda (colorful/simple/minimal). Env bo'lmasa — hammasi
# yoqilgan, oddiy ko'rinish (eski botlar o'zgarmaydi).
FEATURES = {x.strip() for x in os.environ.get("CHILD_FEATURES", "shorts,premium,sub,post,storage").split(",") if x.strip()}
PANEL_STYLE = os.environ.get("PANEL_STYLE", "simple") or "simple"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("child_anime_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- baza

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
        c.execute("""CREATE TABLE IF NOT EXISTS animes(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         title TEXT NOT NULL,
                         poster_file_id TEXT DEFAULT '',
                         poster_msg_id INTEGER DEFAULT 0,
                         is_premium INTEGER DEFAULT 0,
                         views INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS episodes(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         anime_id INTEGER NOT NULL REFERENCES animes(id),
                         ep_no INTEGER NOT NULL,
                         title TEXT DEFAULT '',
                         file_id TEXT NOT NULL,
                         storage_msg_id INTEGER DEFAULT 0,
                         views INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS shorts(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         anime_id INTEGER NOT NULL REFERENCES animes(id),
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
        # Eski bazalar migratsiyasi (ma'lumot o'chmaydi):
        for col, ddl in [("poster_file_id", "TEXT DEFAULT ''"), ("poster_msg_id", "INTEGER DEFAULT 0"),
                         ("is_premium", "INTEGER DEFAULT 0"), ("views", "INTEGER DEFAULT 0")]:
            _ensure_col(c, "animes", col, ddl)
        for col, ddl in [("storage_msg_id", "INTEGER DEFAULT 0"), ("views", "INTEGER DEFAULT 0")]:
            _ensure_col(c, "episodes", col, ddl)


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
        n_anime = c.execute("SELECT COUNT(*) FROM animes").fetchone()[0]
        n_ep = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        n_short = c.execute("SELECT COUNT(*) FROM shorts").fetchone()[0]
        n_prem = c.execute("SELECT COUNT(*) FROM premium_users").fetchone()[0]
        n_ch = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    return (
        f"📊 Statistika\n\n"
        f"👥 Foydalanuvchilar: {count_users()} ta\n"
        f"⭐ Premium foydalanuvchilar: {n_prem} ta\n"
        f"🎬 Animelar: {n_anime} ta\n"
        f"🎞 Qismlar: {n_ep} ta\n"
        f"📱 Shortlar: {n_short} ta\n"
        f"📺 Obuna kanallari: {n_ch} ta\n"
        f"🛡 Egasi ID: {OWNER_ID}"
    )


def list_animes(query: str = "", order: str = "id"):
    order_sql = {"id": "id", "new": "id DESC", "top": "views DESC, id DESC"}.get(order, "id")
    with conn() as c:
        if query:
            return c.execute(
                f"SELECT * FROM animes WHERE title LIKE ? ORDER BY {order_sql}",
                (f"%{query}%",),
            ).fetchall()
        return c.execute(f"SELECT * FROM animes ORDER BY {order_sql}").fetchall()


def newest_animes(limit: int = 10):
    with conn() as c:
        return c.execute("SELECT * FROM animes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def top_animes(limit: int = 10):
    with conn() as c:
        return c.execute("SELECT * FROM animes ORDER BY views DESC, id DESC LIMIT ?", (limit,)).fetchall()


def get_anime(anime_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM animes WHERE id=?", (anime_id,)).fetchone()


def count_episodes(anime_id: int) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM episodes WHERE anime_id=?", (anime_id,)).fetchone()[0]


def list_episodes(anime_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM episodes WHERE anime_id=? ORDER BY ep_no, id", (anime_id,)
        ).fetchall()


def get_episode(ep_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()


def add_anime(title: str, poster_file_id: str = "", is_premium: int = 0) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO animes(title, poster_file_id, is_premium) VALUES(?,?,?)",
            (title.strip(), poster_file_id or "", int(is_premium)),
        )
        return cur.lastrowid


def update_anime(anime_id: int, title=None, poster_file_id=None, poster_msg_id=None, is_premium=None):
    with conn() as c:
        if title is not None:
            c.execute("UPDATE animes SET title=? WHERE id=?", (title.strip(), anime_id))
        if poster_file_id is not None:
            c.execute("UPDATE animes SET poster_file_id=? WHERE id=?", (poster_file_id, anime_id))
        if poster_msg_id is not None:
            c.execute("UPDATE animes SET poster_msg_id=? WHERE id=?", (int(poster_msg_id), anime_id))
        if is_premium is not None:
            c.execute("UPDATE animes SET is_premium=? WHERE id=?", (int(is_premium), anime_id))


def delete_anime(anime_id: int):
    with conn() as c:
        c.execute("DELETE FROM shorts WHERE anime_id=?", (anime_id,))
        c.execute("DELETE FROM episodes WHERE anime_id=?", (anime_id,))
        c.execute("DELETE FROM animes WHERE id=?", (anime_id,))


def add_episode(anime_id: int, file_id: str, title: str = "", storage_msg_id: int = 0) -> int:
    with conn() as c:
        nxt = c.execute(
            "SELECT COALESCE(MAX(ep_no),0)+1 FROM episodes WHERE anime_id=?", (anime_id,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO episodes(anime_id, ep_no, title, file_id, storage_msg_id) VALUES(?,?,?,?,?)",
            (anime_id, nxt, title, file_id, int(storage_msg_id)),
        )
        return nxt


def replace_episode(ep_id: int, file_id: str, storage_msg_id: int = 0):
    with conn() as c:
        c.execute(
            "UPDATE episodes SET file_id=?, storage_msg_id=? WHERE id=?",
            (file_id, int(storage_msg_id), ep_id),
        )


def delete_episode(ep_id: int):
    with conn() as c:
        c.execute("DELETE FROM episodes WHERE id=?", (ep_id,))


def inc_anime_views(anime_id: int):
    with conn() as c:
        c.execute("UPDATE animes SET views = COALESCE(views,0)+1 WHERE id=?", (anime_id,))


def inc_episode_views(ep_id: int):
    with conn() as c:
        c.execute("UPDATE episodes SET views = COALESCE(views,0)+1 WHERE id=?", (ep_id,))


def list_shorts(anime_id=None):
    with conn() as c:
        if anime_id:
            return c.execute("SELECT * FROM shorts WHERE anime_id=? ORDER BY id DESC", (anime_id,)).fetchall()
        return c.execute("SELECT * FROM shorts ORDER BY id DESC LIMIT 30").fetchall()


def get_short(short_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM shorts WHERE id=?", (short_id,)).fetchone()


def add_short(anime_id: int, file_id: str, title: str = "", storage_msg_id: int = 0) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO shorts(anime_id, title, file_id, storage_msg_id) VALUES(?,?,?,?)",
            (anime_id, title.strip(), file_id, int(storage_msg_id)),
        )
        return cur.lastrowid


def delete_short(short_id: int):
    with conn() as c:
        c.execute("DELETE FROM shorts WHERE id=?", (short_id,))


def inc_short_views(short_id: int):
    with conn() as c:
        c.execute("UPDATE shorts SET views = COALESCE(views,0)+1 WHERE id=?", (short_id,))


def anime_card(anime) -> str:
    prem = " ⭐ Premium" if anime["is_premium"] else ""
    return (
        f"🎬 {anime['title']}{prem}\n"
        f"🔢 Kod: {anime['id']}\n"
        f"🎞 Qismlar: {count_episodes(anime['id'])} ta\n"
        f"👁 Ko'rilgan: {anime['views'] or 0} marta"
    )


# ---------------------------------------------------------------- bot

def build_app():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    if PANEL_STYLE == "minimal":
        BTN_LIST, BTN_NEW, BTN_TOP, BTN_SHORTS = "Anime ro'yxati", "Yangilar", "Mashhur", "Shortlar"
        BTN_ADD_ANIME, BTN_ADD_EP, BTN_ADD_SHORT = "Anime qo'shish", "Qism qo'shish", "Short qo'shish"
        BTN_ADMIN, BTN_BACK_MAIN = "Admin panel", "Asosiy menyu"
    else:
        BTN_LIST = "📋 Anime ro'yxati"
        BTN_NEW = "🆕 Yangilar"
        BTN_TOP = "🔥 Mashhur"
        BTN_SHORTS = "🎞 Shortlar"
        BTN_ADD_ANIME = "➕ Anime qo'shish"
        BTN_ADD_EP = "🎞 Qism qo'shish"
        BTN_ADD_SHORT = "📱 Short qo'shish"
        BTN_ADMIN = "🛡 Admin panel"
        BTN_BACK_MAIN = "⬅️ Asosiy menyu"

    def is_owner(update) -> bool:
        return update.effective_user and update.effective_user.id == OWNER_ID

    def _reply_style(text):
        # Haqiqiy rangli tugma (Bot API 9.4, PTB 22.7+): primary=ko'k,
        # success=yashil, danger=qizil. Oddiy/minimal uslubda rang berilmaydi.
        if PANEL_STYLE != "colorful":
            return None
        if text == BTN_SHORTS:
            return "danger"
        if text == BTN_NEW:
            return "success"
        return "primary"

    def _wrap_reply(rows):
        if PANEL_STYLE != "colorful":
            return rows
        out = []
        for row in rows:
            out.append([KeyboardButton(cell, style=_reply_style(cell)) if _reply_style(cell) else cell for cell in row])
        return out

    def _ibtn(text, cb=None, callback_data=None, url=None):
        cb = cb or callback_data
        if PANEL_STYLE != "colorful":
            if url is not None:
                return InlineKeyboardButton(text, url=url)
            return InlineKeyboardButton(text, callback_data=cb)
        low_cb = (cb or "").lower()
        low_text = str(text).lower()
        if ("del" in low_cb or "delete" in low_cb or "close" in low_cb or "reject" in low_cb
                or "o'chirish" in low_text or "yopish" in low_text or "rad etish" in low_text
                or low_text.startswith("🗑") or low_text.startswith("❌")):
            style = "danger"
        elif ("prem" in low_cb or "accept" in low_cb or "give" in low_cb
                or "premium" in low_text or "tasdiqlash" in low_text or "yuklash" in low_text
                or "tekshirish" in low_text or "qo'shish" in low_text):
            style = "success"
        else:
            style = "primary"
        if url is not None:
            return InlineKeyboardButton(text, url=url, style=style)
        return InlineKeyboardButton(text, callback_data=cb, style=style)

    def menu_for(update):
        if is_owner(update):
            # Foydalanuvchi paneli alohida: asosiy menyuda faqat foydalanuvchi
            # tugmalari + bitta "Admin panel" kirish tugmasi. Qo'shish
            # tugmalari faqat Admin panel ichida ko'rinadi.
            rows = [[BTN_LIST, BTN_NEW], [BTN_TOP, BTN_SHORTS] if "shorts" in FEATURES else [BTN_TOP],
                    [BTN_ADMIN]]
            return ReplyKeyboardMarkup(_wrap_reply(rows), resize_keyboard=True)
        rows = [[BTN_LIST, BTN_NEW], [BTN_TOP, BTN_SHORTS] if "shorts" in FEATURES else [BTN_TOP]]
        return ReplyKeyboardMarkup(_wrap_reply(rows), resize_keyboard=True)

    def admin_keyboard():
        rows = [
            [_ibtn("➕ Anime qo'shish", "admin:add_anime"),
             _ibtn("🎞 Qism qo'shish", "admin:add_ep")],
        ]
        if "shorts" in FEATURES:
            rows.append([_ibtn("📱 Short qo'shish", "admin:add_short")])
        rows += [
            [_ibtn("📊 Statistika", "admin:stats"),
             _ibtn("👥 Foydalanuvchilar", "admin:users")],
            [_ibtn("✏️ Tahrirlash", "admin:edit"),
             _ibtn("🗑 O'chirish", "admin:delete")],
            [_ibtn("📢 E'lon yuborish", "admin:broadcast")],
        ]
        if "post" in FEATURES:
            rows[-1].append(_ibtn("📤 Post joylash", "admin:post"))
        chan_row = []
        if "sub" in FEATURES:
            chan_row.append(_ibtn("📺 Obuna kanallari", "admin:channels"))
        if "storage" in FEATURES:
            chan_row.append(_ibtn("📦 Saqlash kanali", "admin:storage"))
        if chan_row:
            rows.append(chan_row)
        prem_row = []
        if "premium" in FEATURES:
            prem_row.append(_ibtn("⭐ Premium berish/olish", "admin:premium"))
        prem_row.append(_ibtn("💾 Baza nusxasi", "admin:backup"))
        rows.append(prem_row)
        return InlineKeyboardMarkup(rows)

    def back_kb():
        return InlineKeyboardMarkup([[_ibtn("⬅️ Orqaga", callback_data="back:list")]])

    async def check_subscription(update, context) -> bool:
        if "sub" not in FEATURES:
            return True
        """Majburiy obuna: kanallar bo'lmasa True. Xatolikda botni buzmaslik
        uchun o'sha kanal o'tkazib yuboriladi (admin xato yozgan bo'lishi mumkin)."""
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
        kb = [[_ibtn(f"📺 @{ch}", url=f"https://t.me/{ch}")] for ch in missing]
        kb.append([_ibtn("✅ Obuna bo'ldim, tekshirish", callback_data="back:list")])
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

    def premium_blocked(update, anime) -> bool:
        if "premium" not in FEATURES:
            return False
        if not anime["is_premium"]:
            return False
        if is_owner(update):
            return False
        return not is_premium_user(update.effective_user.id)

    async def send_via_storage_or_file(context, chat_id, file_id, storage_msg_id, caption=None, is_photo=False):
        """Avval saqlash kanalidan copy, bo'lmasa file_id bilan yuborish."""
        storage = get_setting("storage_channel", "")
        if storage and storage_msg_id:
            try:
                sid = int(storage) if storage.lstrip("-").isdigit() else storage
                if is_photo:
                    await context.bot.copy_message(chat_id=chat_id, from_chat_id=sid,
                                                   message_id=int(storage_msg_id), caption=caption)
                else:
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
        if "storage" not in FEATURES:
            return 0
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

    def animes_keyboard(rows, prefix="anime"):
        kb = []
        for r in rows:
            label = f"{'⭐ ' if r['is_premium'] else ''}{r['title']} (kod {r['id']})"
            kb.append([_ibtn(label, callback_data=f"{prefix}:{r['id']}")])
        kb.append([_ibtn("⬅️ Orqaga", callback_data="back:list")])
        return InlineKeyboardMarkup(kb) if kb else None

    async def send_anime_list(update, context, query="", order="id", title="Animelar ro'yxati:"):
        rows = list_animes(query, order=order) if order != "new" else newest_animes()
        if order == "top":
            rows = top_animes()
        chat = update.effective_chat
        if not rows:
            text = "Hech narsa topilmadi." if query else "Hozircha anime yo'q."
            if update.message:
                await update.message.reply_text(text)
            else:
                await chat.send_message(text)
            return
        kb = animes_keyboard(rows)
        if update.message:
            await update.message.reply_text(title, reply_markup=kb)
        else:
            await chat.send_message(title, reply_markup=kb)

    async def show_anime(update, context, anime_id: int):
        anime = get_anime(anime_id)
        if not anime:
            return
        kb_rows = []
        eps = list_episodes(anime_id)
        for e in eps:
            kb_rows.append([_ibtn(f"{e['ep_no']}-qism", callback_data=f"ep:{e['id']}")])
        kb_rows.append([_ibtn("⬅️ Ro'yxatga qaytish", callback_data="back:list")])
        kb = InlineKeyboardMarkup(kb_rows)
        q = update.callback_query
        if anime["poster_file_id"]:
            try:
                await send_via_storage_or_file(context, update.effective_chat.id,
                                               anime["poster_file_id"], anime["poster_msg_id"],
                                               caption=anime_card(anime), is_photo=True)
                await update.effective_chat.send_message("Qismni tanlang:", reply_markup=kb)
                if q:
                    try:
                        await q.message.delete()
                    except Exception:
                        pass
                return
            except Exception as e:
                log.warning("Poster yuborilmadi: %s", e)
        if q:
            await q.edit_message_text(anime_card(anime) + ("\n\nHozircha qism yo'q." if not eps else "\n\nQismni tanlang:"),
                                      reply_markup=kb)
        else:
            await update.effective_chat.send_message(anime_card(anime), reply_markup=kb)

    async def cmd_start(update, context):
        track_user(update.effective_user.id, update.effective_user.username or "")
        if not await check_subscription(update, context):
            await update.message.reply_text(
                "Assalomu alaykum! 👋 Obuna tekshiruvidan o'tgach, bot to'liq ishlaydi.",
                reply_markup=menu_for(update),
            )
            return
        await update.message.reply_text(
            ("🌸 Assalomu alaykum! 🌸\n\n✨🎬 Anime tanlang, nomini yozib qidiring yoki kodini yuboring.\n" if PANEL_STYLE == "colorful" else "Assalomu alaykum! 👋\n\n🎬 Anime tanlang, nomini yozib qidiring yoki kodini yuboring.\n")
            + "Yangi animelar «🆕 Yangilar» da, eng ko'p ko'rilganlar «🔥 Mashhur» da.",
            reply_markup=menu_for(update),
        )
        await send_anime_list(update, context)

    # ---- admin callbacks ----
    async def on_admin_callback(update, context):
        q = update.callback_query
        await q.answer()
        if not is_owner(update):
            return
        action = (q.data or "").split(":", 1)[1] if ":" in (q.data or "") else ""
        if action == "add_anime":
            context.user_data["await"] = "anime_title"
            await q.message.reply_text("Yangi anime nomini yozing:")
        elif action == "add_ep":
            rows = list_animes()
            if not rows:
                await q.message.reply_text("Avval anime qo'shing — ro'yxat bo'sh.")
                return
            await q.message.reply_text("Qaysi animega qism qo'shiladi?",
                                       reply_markup=animes_keyboard(rows, prefix="epadd"))
        elif action == "add_short":
            if "shorts" not in FEATURES:
                await q.message.reply_text("Bu funksiya bu botda o'chirilgan.")
                return
            rows = list_animes()
            if not rows:
                await q.message.reply_text("Avval anime qo'shing — ro'yxat bo'sh.")
                return
            await q.message.reply_text("Qaysi animega short qo'shiladi?",
                                       reply_markup=animes_keyboard(rows, prefix="shortadd"))
        elif action == "stats":
            await q.edit_message_text(stats_text(), reply_markup=admin_keyboard())
        elif action == "users":
            rows = list_users()
            if not rows:
                await q.edit_message_text("Hozircha foydalanuvchi yo'q.", reply_markup=admin_keyboard())
                return
            lines = "\n".join(f"• {r['user_id']}" + (f" (@{r['username']})" if r["username"] else "") for r in rows)
            await q.edit_message_text(f"👥 Oxirgi foydalanuvchilar:\n{lines}", reply_markup=admin_keyboard())
        elif action == "delete":
            rows = list_animes()
            if not rows:
                await q.edit_message_text("O'chiriladigan anime yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"🗑 {r['title']}", callback_data=f"delanime:{r['id']}")] for r in rows])
            await q.edit_message_text("Qaysi anime o'chirilsin? (qismlari va shortlari ham o'chadi)", reply_markup=kb)
        elif action == "edit":
            rows = list_animes()
            if not rows:
                await q.edit_message_text("Tahrirlanadigan anime yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"✏️ {r['title']}", callback_data=f"editanime:{r['id']}")] for r in rows])
            await q.edit_message_text("Qaysi anime tahrirlanadi?", reply_markup=kb)
        elif action == "broadcast":
            context.user_data["await"] = "broadcast"
            await q.message.reply_text("📢 Barcha foydalanuvchilarga yuboriladigan e'lon matnini yozing:")
        elif action == "post":
            rows = list_animes()
            if not rows:
                await q.edit_message_text("Post qilinadigan anime yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"📤 {r['title']}", callback_data=f"postanime:{r['id']}")] for r in rows])
            await q.edit_message_text("Qaysi anime post qilinsin?", reply_markup=kb)
        elif action == "channels":
            chs = get_channels()
            pchs = get_post_channels()
            txt = ("📺 Obuna kanallari:\n" + ("\n".join(f"• @{c}" for c in chs) or "—")
                   + "\n\n📤 Post kanallari:\n" + ("\n".join(f"• @{c}" for c in pchs) or "—"))
            kb = InlineKeyboardMarkup([
                [_ibtn("➕ Obuna kanal qo'shish", callback_data="admin:chadd"),
                 _ibtn("➖ Obuna kanal o'chirish", callback_data="admin:chdel")],
                [_ibtn("➕ Post kanal qo'shish", callback_data="admin:pchadd"),
                 _ibtn("➖ Post kanal o'chirish", callback_data="admin:pchdel")],
                [_ibtn("⬅️ Admin panel", callback_data="admin:back")],
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
            kb = InlineKeyboardMarkup([[_ibtn(f"❌ @{c}", callback_data=f"delchan:{c}")] for c in chs])
            await q.edit_message_text("Qaysi obuna kanal o'chirilsin?", reply_markup=kb)
        elif action == "pchadd":
            context.user_data["await"] = "post_channel_add"
            await q.message.reply_text("✍️ Post kanal username'ini yozing (@ bilan yoki @ siz).\n⚠️ Avval botni shu kanalga admin qilib qo'shing!")
        elif action == "pchdel":
            chs = get_post_channels()
            if not chs:
                await q.edit_message_text("O'chiriladigan post kanal yo'q.", reply_markup=admin_keyboard())
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"❌ @{c}", callback_data=f"delpchan:{c}")] for c in chs])
            await q.edit_message_text("Qaysi post kanal o'chirilsin?", reply_markup=kb)
        elif action == "storage":
            cur = get_setting("storage_channel", "") or "o'rnatilmagan"
            kb = InlineKeyboardMarkup([
                [_ibtn("✏️ Saqlash kanalini o'rnatish", callback_data="admin:storage_set")],
                [_ibtn("⬅️ Admin panel", callback_data="admin:back")],
            ])
            await q.edit_message_text(
                f"📦 Saqlash kanali (ko'chma baza)\n\nHozirgi: {cur}\n\n"
                "Bu kanalga yuklangan har bir poster/qism/short nusxalanadi. "
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
                [_ibtn("➕ Premium berish", callback_data="admin:prem_give"),
                 _ibtn("➖ Premium olish", callback_data="admin:prem_revoke")],
                [_ibtn("⬅️ Admin panel", callback_data="admin:back")],
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
        data = q.data or ""
        kind, _, raw = data.partition(":")
        if kind == "delanime":
            try:
                anime_id = int(raw)
            except ValueError:
                return
            anime = get_anime(anime_id)
            delete_anime(anime_id)
            await q.edit_message_text(f"🗑 O'chirildi: {anime['title'] if anime else anime_id}")
        elif kind == "delchan":
            del_channel(raw)
            await q.edit_message_text(f"❌ Obuna kanal o'chirildi: @{raw}")
        elif kind == "delpchan":
            del_post_channel(raw)
            await q.edit_message_text(f"❌ Post kanal o'chirildi: @{raw}")
        elif kind == "delshort":
            try:
                sid = int(raw)
            except ValueError:
                return
            delete_short(sid)
            await q.edit_message_text("🗑 Short o'chirildi.")
        elif kind == "delep":
            try:
                ep_id = int(raw)
            except ValueError:
                return
            delete_episode(ep_id)
            await q.edit_message_text("🗑 Qism o'chirildi.")


    async def on_callback(update, context):
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        if data == "back:list":
            if not await check_subscription(update, context):
                return
            await send_anime_list(update, context)
            return
        kind, _, raw = data.partition(":")
        if kind == "anime":
            try:
                anime_id = int(raw)
            except ValueError:
                return
            if not await check_subscription(update, context):
                return
            anime = get_anime(anime_id)
            if anime and premium_blocked(update, anime):
                await q.message.reply_text("⭐ Bu anime premium — faqat premium foydalanuvchilar ko'radi.")
                return
            await show_anime(update, context, anime_id)
        elif kind == "ep":
            try:
                ep_id = int(raw)
            except ValueError:
                return
            if not await check_subscription(update, context):
                return
            ep = get_episode(ep_id)
            if not ep:
                await q.edit_message_text("Qism topilmadi.")
                return
            anime = get_anime(ep["anime_id"])
            if anime and premium_blocked(update, anime):
                await q.message.reply_text("⭐ Bu anime premium — faqat premium foydalanuvchilar ko'radi.")
                return
            inc_episode_views(ep_id)
            if anime:
                inc_anime_views(anime["id"])
            caption = f"{anime['title']} — {ep['ep_no']}-qism" if anime else f"{ep['ep_no']}-qism"
            await send_via_storage_or_file(context, update.effective_chat.id, ep["file_id"],
                                           ep["storage_msg_id"], caption=caption)
        elif kind == "epadd":
            if not is_owner(update):
                return
            try:
                anime_id = int(raw)
            except ValueError:
                return
            context.user_data["await"] = "video"
            context.user_data["ep_anime_id"] = anime_id
            anime = get_anime(anime_id)
            await q.edit_message_text(f"🎬 {anime['title'] if anime else ''} uchun video yuboring (video qilib, fayl sifatida emas).")
        elif kind == "shortadd":
            if not is_owner(update):
                return
            try:
                anime_id = int(raw)
            except ValueError:
                return
            context.user_data["await"] = "short_video"
            context.user_data["short_anime_id"] = anime_id
            await q.edit_message_text("📱 Short uchun video yuboring.")
        elif kind == "short":
            try:
                sid = int(raw)
            except ValueError:
                return
            if not await check_subscription(update, context):
                return
            sh = get_short(sid)
            if not sh:
                await q.edit_message_text("Short topilmadi.")
                return
            anime = get_anime(sh["anime_id"])
            if anime and premium_blocked(update, anime):
                await q.message.reply_text("⭐ Bu anime premium — faqat premium foydalanuvchilar ko'radi.")
                return
            inc_short_views(sid)
            await send_via_storage_or_file(context, update.effective_chat.id, sh["file_id"],
                                           sh["storage_msg_id"], caption=f"📱 {sh['title'] or (anime['title'] if anime else '')}")
        elif kind == "editanime":
            if not is_owner(update):
                return
            try:
                anime_id = int(raw)
            except ValueError:
                return
            anime = get_anime(anime_id)
            if not anime:
                return
            kb = InlineKeyboardMarkup([
                [_ibtn("✏️ Nomini o'zgartirish", callback_data=f"edittitle:{anime_id}"),
                 _ibtn("🖼 Poster almashtirish", callback_data=f"editposter:{anime_id}")],
                [_ibtn("⭐ Premium: " + ("O'chirish" if anime["is_premium"] else "Yoqish"), callback_data=f"editprem:{anime_id}")],
                [_ibtn("🎞 Qism almashtirish", callback_data=f"editrepep:{anime_id}"),
                 _ibtn("🗑 Qism o'chirish", callback_data=f"editdelep:{anime_id}")],
                [_ibtn("📱 Short o'chirish", callback_data=f"editdelshort:{anime_id}")],
                [_ibtn("⬅️ Admin panel", callback_data="admin:back")],
            ])
            await q.edit_message_text(f"✏️ Tahrirlash: {anime['title']}\n{anime_card(anime)}", reply_markup=kb)
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
        elif kind == "editprem":
            if not is_owner(update):
                return
            anime_id = int(raw)
            anime = get_anime(anime_id)
            if anime:
                update_anime(anime_id, is_premium=0 if anime["is_premium"] else 1)
                prem_word = "o'chirildi" if anime["is_premium"] else "yoqildi"
                await q.edit_message_text(f"⭐ Premium {prem_word}: {anime['title']}")
        elif kind == "editrepep":
            if not is_owner(update):
                return
            eps = list_episodes(int(raw))
            if not eps:
                await q.edit_message_text("Almashtiriladigan qism yo'q.")
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"🔄 {e['ep_no']}-qism", callback_data=f"repep:{e['id']}")] for e in eps])
            await q.edit_message_text("Qaysi qism almashtirilsin?", reply_markup=kb)
        elif kind == "editdelep":
            if not is_owner(update):
                return
            eps = list_episodes(int(raw))
            if not eps:
                await q.edit_message_text("O'chiriladigan qism yo'q.")
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"🗑 {e['ep_no']}-qism", callback_data=f"delep:{e['id']}")] for e in eps])
            await q.edit_message_text("Qaysi qism o'chirilsin?", reply_markup=kb)
        elif kind == "editdelshort":
            if not is_owner(update):
                return
            shorts = list_shorts(int(raw))
            if not shorts:
                await q.edit_message_text("O'chiriladigan short yo'q.")
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"🗑 {s['title'] or s['id']}", callback_data=f"delshort:{s['id']}")] for s in shorts])
            await q.edit_message_text("Qaysi short o'chirilsin?", reply_markup=kb)
        elif kind == "repep":
            if not is_owner(update):
                return
            context.user_data["await"] = "ep_replace_video"
            context.user_data["ep_replace_id"] = int(raw)
            await q.edit_message_text("🔄 Yangi video yuboring (eski qism o'rniga qo'yiladi):")
        elif kind == "postanime":
            if not is_owner(update):
                return
            anime_id = int(raw)
            anime = get_anime(anime_id)
            targets = get_post_channels()
            if not targets:
                await q.edit_message_text("❌ Avval «📺 Obuna kanallari» dan post kanal qo'shing.")
                return
            caption = f"🎬 {anime['title']}\n🔢 Kod: {anime['id']}\n🎞 Qismlar: {count_episodes(anime_id)} ta"
            sent, failed = [], []
            for ch in targets:
                try:
                    if anime["poster_file_id"]:
                        await send_via_storage_or_file(context, "@" + ch, anime["poster_file_id"],
                                                       anime["poster_msg_id"], caption=caption, is_photo=True)
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
        if text == BTN_BACK_MAIN:
            context.user_data.pop("await", None)
            await update.message.reply_text("Asosiy menyu:", reply_markup=menu_for(update))
            return
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
            aid = context.user_data.pop("edit_id", None)
            context.user_data.pop("await", None)
            if aid:
                update_anime(aid, title=text)
                await update.message.reply_text(f"✅ Nom o'zgartirildi: {text}")
            return
        if is_owner(update) and state == "short_title":
            sid_anime = context.user_data.pop("short_anime_id", None)
            fid = context.user_data.pop("short_file_id", None)
            smsg = context.user_data.pop("short_storage_msg", 0)
            context.user_data.pop("await", None)
            if sid_anime and fid:
                title = "" if text.strip() == "-" else text
                new_id = add_short(sid_anime, fid, title, smsg)
                await update.message.reply_text(f"✅ Short saqlandi (#{new_id}).")
            return
        if text == BTN_LIST:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            return await send_anime_list(update, context)
        if text == BTN_NEW:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            return await send_anime_list(update, context, order="new", title="🆕 Yangi qo'shilgan animelar:")
        if text == BTN_TOP:
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            return await send_anime_list(update, context, order="top", title="🔥 Eng ko'p ko'rilgan animelar:")
        if text == BTN_SHORTS:
            if "shorts" not in FEATURES:
                await update.message.reply_text("Bu funksiya bu botda o'chirilgan.")
                return
            context.user_data.pop("await", None)
            if not await check_subscription(update, context):
                return
            shorts = list_shorts()
            if not shorts:
                await update.message.reply_text("Hozircha short yo'q.")
                return
            kb = InlineKeyboardMarkup([[_ibtn(f"📱 {s['title'] or 'Short'}", callback_data=f"short:{s['id']}")] for s in shorts])
            await update.message.reply_text("🎞 Shortlar:", reply_markup=kb)
            return
        if is_owner(update) and text == BTN_ADD_ANIME:
            context.user_data["await"] = "anime_title"
            await update.message.reply_text("Yangi anime nomini yozing:")
            return
        if is_owner(update) and text == BTN_ADD_EP:
            rows = list_animes()
            if not rows:
                await update.message.reply_text("Avval anime qo'shing — ro'yxat bo'sh.")
                return
            await update.message.reply_text("Qaysi animega qism qo'shiladi?",
                                            reply_markup=animes_keyboard(rows, prefix="epadd"))
            return
        if is_owner(update) and text == BTN_ADD_SHORT:
            rows = list_animes()
            if not rows:
                await update.message.reply_text("Avval anime qo'shing — ro'yxat bo'sh.")
                return
            await update.message.reply_text("Qaysi animega short qo'shiladi?",
                                            reply_markup=animes_keyboard(rows, prefix="shortadd"))
            return
        if is_owner(update) and state == "anime_title":
            context.user_data["await"] = "anime_poster"
            context.user_data["anime_title"] = text
            await update.message.reply_text(
                "🖼 Poster rasmini yuboring (rasm qilib).\nPoster kerak bo'lmasa, «o'tkazib yuborish» deb yozing:")
            return
        if is_owner(update) and state == "anime_poster":
            # Poster o'rniga matn kelsa — o'tkazib yuborish
            context.user_data.pop("await", None)
            title = context.user_data.pop("anime_title", "")
            new_id = add_anime(title)
            await update.message.reply_text(f"✅ Anime qo'shildi (kod {new_id}): {title}\nEndi «🎞 Qism qo'shish» dan qismlarni qo'shing.")
            return
        if is_owner(update) and state in ("video", "short_video", "ep_replace_video"):
            await update.message.reply_text("Video kutyapman — video fayl yuboring.")
            return
        if is_owner(update) and state in ("edit_poster",):
            await update.message.reply_text("Poster rasmi kutyapman — rasm yuboring.")
            return
        # Oddiy foydalanuvchi: kod (faqat raqam) yoki qidiruv
        if not await check_subscription(update, context):
            return
        if text.isdigit():
            anime = get_anime(int(text))
            if anime:
                if premium_blocked(update, anime):
                    await update.message.reply_text("⭐ Bu anime premium — faqat premium foydalanuvchilar ko'radi.")
                    return
                await show_anime(update, context, int(text))
                return
            await update.message.reply_text("Bu kodda anime topilmadi.")
            return
        await send_anime_list(update, context, query=text)

    async def handle_photo(update, context):
        if not is_owner(update):
            return
        state = context.user_data.get("await")
        photo = update.message.photo[-1] if update.message.photo else None
        if photo is None:
            return
        if state == "anime_poster":
            context.user_data.pop("await", None)
            title = context.user_data.pop("anime_title", "")
            smsg = await storage_copy(context, update, update.message.message_id)
            new_id = add_anime(title, poster_file_id=photo.file_id)
            if smsg:
                update_anime(new_id, poster_msg_id=smsg)
            await update.message.reply_text(f"✅ Anime qo'shildi (kod {new_id}): {title}\nEndi «🎞 Qism qo'shish» dan qismlarni qo'shing.")
            return
        if state == "edit_poster":
            aid = context.user_data.pop("edit_id", None)
            context.user_data.pop("await", None)
            if aid:
                smsg = await storage_copy(context, update, update.message.message_id)
                update_anime(aid, poster_file_id=photo.file_id, poster_msg_id=smsg)
                await update.message.reply_text("✅ Poster almashtirildi.")
            return
        await update.message.reply_text("Poster qabul qilinmadi — avval anime qo'shish yoki tahrirlashni boshlang.")

    async def handle_video(update, context):
        if not is_owner(update):
            return
        state = context.user_data.get("await")
        video = update.message.video or (update.message.document if update.message.document else None)
        if video is None:
            await update.message.reply_text("Video topilmadi, qayta yuboring.")
            return
        if state == "video":
            anime_id = context.user_data.get("ep_anime_id")
            smsg = await storage_copy(context, update, update.message.message_id)
            context.user_data.pop("await", None)
            ep_no = add_episode(anime_id, video.file_id, getattr(video, "file_name", "") or "", smsg)
            anime = get_anime(anime_id)
            await update.message.reply_text(f"✅ {anime['title'] if anime else ''} — {ep_no}-qism saqlandi.")
            return
        if state == "short_video":
            anime_id = context.user_data.get("short_anime_id")
            smsg = await storage_copy(context, update, update.message.message_id)
            context.user_data["await"] = "short_title"
            context.user_data["short_file_id"] = video.file_id
            context.user_data["short_storage_msg"] = smsg
            await update.message.reply_text("📱 Short sarlavhasini yozing (kerak bo'lmasa «-» yozing):")
            return
        if state == "ep_replace_video":
            ep_id = context.user_data.pop("ep_replace_id", None)
            context.user_data.pop("await", None)
            if ep_id:
                smsg = await storage_copy(context, update, update.message.message_id)
                replace_episode(ep_id, video.file_id, smsg)
                await update.message.reply_text("✅ Qism almashtirildi.")
            return
        await update.message.reply_text("Avval «🎞 Qism qo'shish» yoki «📱 Short qo'shish» ni bosib anime tanlang.")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^(delanime|delchan|delpchan|delshort|delep):"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(anime|ep|epadd|short|shortadd|editanime|edittitle|editposter|editprem|editrepep|editdelep|editdelshort|repep|postanime|back):"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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
