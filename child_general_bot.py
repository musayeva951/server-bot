"""
Umumiy bot shabloni — har qanaqa kontent boti uchun (server bot ishga tushiradi).

Server bot bu faylni har bir token-usuli umumiy bot uchun alohida jarayon
qilib ishga tushiradi. Shablon yangilansa, mavjud umumiy botlar keyingi
qayta ishga tushishda yangi versiyani oladi, bazasi saqlanib qoladi.

Muhit (env) orqali oladi:
    CHILD_BOT_TOKEN — shu nusxaning bot tokeni
    CHILD_DB_PATH   — shu nusxaning ALOHIDA SQLite bazasi yo'li
    OWNER_ID        — bot egasi (admin) Telegram ID si

Nima qiladi:
  Foydalanuvchi:
    * /start — egasi yozgan salom matni + bo'limlar menyusi
    * Bo'lim tugmasi — shu bo'limdagi kontent (matn, rasm, video) ketma-ket
    * Majburiy obuna: egasi belgilagan kanallarga obuna bo'lmasa, kontent
      berilmaydi (kanalga qo'shilish tugmalari + tekshirish tugmasi)
    * Premium bo'lim/kontent faqat premium foydalanuvchiga ochiq
  Admin (faqat egasi, OWNER_ID):
    * ➕ Bo'lim qo'shish (nom + premiummi, yo'qmi)
    * Bo'limni boshqarish: kontent qo'shish (matn/rasm/video yuborish),
      premium yoqish/o'chirish, bo'limni o'chirish
    * 📺 Majburiy obuna kanallarini qo'shish/o'chirish
    * 👥 Premium berish/olish (Telegram ID bo'yicha, qo'lda)
    * 📢 Elon — barcha foydalanuvchilarga matn yuborish
    * ✏️ Salom matnini o'zgartirish, 📊 Statistika

  Nima qilmaydi (halol cheklov): murakkab maxsus logika — o'yin,
  murakkab to'lov, buyurtma tizimi, tashqi saytdan avtomatik kontent —
  bu shablon bilan avtomatik yasab bo'lmaydi. Bunday bot uchun kod
  yuborish usuli bor (server botda «📁 Bot qo'shish (kod)»).
  Bu shablon ichida to'lov qabul qilinmaydi — server oyligi server
  botda, bola bot premiumi egasi tomonidan qo'lda beriladi.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

TOKEN = os.environ.get("CHILD_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
DB_PATH = Path(os.environ.get("CHILD_DB_PATH", "child.db"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("child_general_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
                         user_id INTEGER PRIMARY KEY,
                         username TEXT DEFAULT '',
                         first_seen TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS sections(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         title TEXT NOT NULL,
                         is_premium INTEGER DEFAULT 0,
                         order_no INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS contents(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         section_id INTEGER NOT NULL,
                         kind TEXT NOT NULL,
                         file_id TEXT DEFAULT '',
                         caption TEXT DEFAULT '',
                         order_no INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS premium_users(
                         user_id INTEGER PRIMARY KEY,
                         granted_at TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS sub_channels(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         username TEXT NOT NULL UNIQUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
                         key TEXT PRIMARY KEY,
                         value TEXT DEFAULT '')""")
        row = c.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()
        if not row:
            c.execute(
                "INSERT INTO settings(key, value) VALUES('welcome', ?)",
                ("Assalomu alaykum! 👋\nBo'limni tanlang 👇",),
            )


def add_user(user_id, username=""):
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
            (user_id, username or "", datetime.now().isoformat(timespec="seconds")),
        )


def get_setting(key):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""


def set_setting(key, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_sections():
    with conn() as c:
        return c.execute("SELECT * FROM sections ORDER BY order_no, id").fetchall()


def get_section(sec_id):
    with conn() as c:
        return c.execute("SELECT * FROM sections WHERE id=?", (sec_id,)).fetchone()


def get_contents(sec_id):
    with conn() as c:
        return c.execute(
            "SELECT * FROM contents WHERE section_id=? ORDER BY order_no, id", (sec_id,)
        ).fetchall()


def get_sub_channels():
    with conn() as c:
        return c.execute("SELECT * FROM sub_channels ORDER BY id").fetchall()


def is_premium(user_id):
    if user_id == OWNER_ID:
        return True
    with conn() as c:
        return c.execute(
            "SELECT 1 FROM premium_users WHERE user_id=?", (user_id,)
        ).fetchone() is not None


def count_users():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def build_app():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    BTN_ADMIN = "🛡 Admin panel"

    def main_menu(user_id):
        rows = []
        for sec in get_sections():
            label = sec["title"] + (" ⭐" if sec["is_premium"] else "")
            rows.append([label])
        if user_id == OWNER_ID:
            rows.append([BTN_ADMIN])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True) if rows else None

    def admin_menu():
        return ReplyKeyboardMarkup([
            ["➕ Bo'lim qo'shish", "📋 Bo'limlar"],
            ["📺 Majburiy obuna", "👥 Premium"],
            ["📢 Elon", "✏️ Salom matni"],
            ["📊 Statistika", "⬅️ Orqaga"],
        ], resize_keyboard=True)

    async def check_sub(update, context):
        """Majburiy obuna tekshiruvi: True bo'lsa kontent berish mumkin."""
        channels = get_sub_channels()
        if not channels:
            return True
        user_id = update.effective_user.id
        if user_id == OWNER_ID:
            return True
        missing = []
        for ch in channels:
            try:
                member = await context.bot.get_chat_member(ch["username"], user_id)
                if member.status in ("left", "kicked"):
                    missing.append(ch["username"])
            except Exception:
                missing.append(ch["username"])
        if not missing:
            return True
        kb_rows = []
        for username in missing:
            url = username if username.startswith("http") else f"https://t.me/{username.lstrip('@')}"
            kb_rows.append([InlineKeyboardButton(f"📺 {username}", url=url)])
        kb_rows.append([InlineKeyboardButton("✅ Obuna bo'ldim, tekshirish", callback_data="sub:check")])
        text = (
            "📺 Avval quyidagi kanallarga obuna bo'ling, keyin "
            "«✅ Obuna bo'ldim, tekshirish» ni bosing:"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))
        return False

    async def cmd_start(update, context):
        user = update.effective_user
        add_user(user.id, user.username or "")
        if not await check_sub(update, context):
            return
        await update.message.reply_text(
            get_setting("welcome"), reply_markup=main_menu(user.id)
        )

    async def sub_check(update, context):
        q = update.callback_query
        await q.answer()
        if await check_sub(update, context):
            await q.message.reply_text(
                get_setting("welcome"), reply_markup=main_menu(q.from_user.id)
            )

    async def show_section(update, context, title):
        user_id = update.effective_user.id
        sec = None
        for s in get_sections():
            if s["title"] == title or s["title"] + " ⭐" == title:
                sec = s
                break
        if sec is None:
            return
        if not await check_sub(update, context):
            return
        if sec["is_premium"] and not is_premium(user_id):
            await update.message.reply_text(
                "⭐ Bu bo'lim premium — faqat premium foydalanuvchilar ko'ra oladi. "
                "Premium olish uchun bot egasiga yozing."
            )
            return
        items = get_contents(sec["id"])
        if not items:
            await update.message.reply_text(f"«{sec['title']}» bo'limi hali bo'sh.")
            return
        for item in items:
            try:
                if item["kind"] == "photo":
                    await update.message.reply_photo(item["file_id"], caption=item["caption"] or None)
                elif item["kind"] == "video":
                    await update.message.reply_video(item["file_id"], caption=item["caption"] or None)
                else:
                    await update.message.reply_text(item["caption"])
            except Exception as e:
                log.warning("Kontent yuborilmadi: %s", e)

    def section_manage_kb(sec):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Kontent qo'shish", callback_data=f"sec:add:{sec['id']}")],
            [InlineKeyboardButton(
                "⭐ Premiumni o'chirish" if sec["is_premium"] else "⭐ Premium qilish",
                callback_data=f"sec:prem:{sec['id']}")],
            [InlineKeyboardButton("🗑 Bo'limni o'chirish", callback_data=f"sec:delask:{sec['id']}")],
        ])

    async def show_sections_admin(update, context):
        secs = get_sections()
        if not secs:
            await update.message.reply_text("Hali bo'lim yo'q. «➕ Bo'lim qo'shish» ni bosing.")
            return
        for sec in secs:
            with conn() as c:
                n = c.execute(
                    "SELECT COUNT(*) FROM contents WHERE section_id=?", (sec["id"],)
                ).fetchone()[0]
            await update.message.reply_text(
                f"📁 {sec['title']} — {n} ta kontent, "
                f"{'premium ⭐' if sec['is_premium'] else 'ochiq'}",
                reply_markup=section_manage_kb(sec),
            )

    async def section_cb(update, context):
        q = update.callback_query
        await q.answer()
        if q.from_user.id != OWNER_ID:
            await q.edit_message_text("Faqat bot egasi boshqaradi.")
            return
        _, action, raw_id = q.data.split(":", 2)
        sec_id = int(raw_id)
        sec = get_section(sec_id)
        if sec is None:
            await q.edit_message_text("Bo'lim topilmadi.")
            return
        if action == "add":
            context.user_data["await"] = "content"
            context.user_data["content_sec"] = sec_id
            await q.message.reply_text(
                f"«{sec['title']}» bo'limiga kontent yuboring — matn, rasm yoki video. "
                "Rasm/videoga izoh (caption) yozsangiz, shu izoh bilan chiqadi."
            )
        elif action == "prem":
            context.user_data.pop("await", None)
            context.user_data.pop("new_sec", None)
            with conn() as c:
                c.execute(
                    "UPDATE sections SET is_premium=? WHERE id=?",
                    (0 if sec["is_premium"] else 1, sec_id),
                )
            await q.edit_message_text(
                f"«{sec['title']}» endi {'premium ⭐' if not sec['is_premium'] else 'ochiq'}."
            )
        elif action == "delask":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Ha, o'chir ✅", callback_data=f"sec:delyes:{sec_id}"),
                InlineKeyboardButton("Yo'q ❌", callback_data=f"sec:delno:{sec_id}"),
            ]])
            await q.edit_message_text(f"«{sec['title']}» bo'limi va uning kontenti o'chirilsinmi?", reply_markup=kb)
        elif action == "delyes":
            with conn() as c:
                c.execute("DELETE FROM contents WHERE section_id=?", (sec_id,))
                c.execute("DELETE FROM sections WHERE id=?", (sec_id,))
            await q.edit_message_text("Bo'lim o'chirildi 🗑")
        elif action == "delno":
            await q.edit_message_text("Bo'lim qoldirildi.")

    async def sub_cb(update, context):
        q = update.callback_query
        await q.answer()
        if q.from_user.id != OWNER_ID:
            return
        _, action, raw_id = q.data.split(":", 2)
        if action == "del":
            with conn() as c:
                c.execute("DELETE FROM sub_channels WHERE id=?", (int(raw_id),))
            await q.edit_message_text("Kanal o'chirildi.")
            return
        await q.edit_message_text("Noma'lum amal.")

    async def handle_text(update, context):
        text = (update.message.text or "").strip()
        user = update.effective_user
        add_user(user.id, user.username or "")
        if text == BTN_ADMIN:
            if user.id != OWNER_ID:
                return
            await update.message.reply_text("🛡 Admin panel", reply_markup=admin_menu())
            return
        if text == "⬅️ Orqaga":
            context.user_data.pop("await", None)
            await update.message.reply_text("Asosiy menyu 👇", reply_markup=main_menu(user.id))
            return
        if user.id == OWNER_ID:
            if text == "➕ Bo'lim qo'shish":
                context.user_data["await"] = "section_title"
                await update.message.reply_text("Yangi bo'lim nomini yuboring:")
                return
            if text == "📋 Bo'limlar":
                return await show_sections_admin(update, context)
            if text == "📺 Majburiy obuna":
                channels = get_sub_channels()
                if not channels:
                    await update.message.reply_text(
                        "Majburiy obuna kanali yo'q. Kanal @username ni yuboring "
                        "(masalan: @kanal_nomi) — yoki «⬅️ Orqaga»."
                    )
                else:
                    for ch in channels:
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🗑 O'chirish", callback_data=f"sub:del:{ch['id']}"),
                        ]])
                        await update.message.reply_text(f"📺 {ch['username']}", reply_markup=kb)
                    await update.message.reply_text("Yangi kanal qo'shish uchun @username yuboring:")
                context.user_data["await"] = "sub_add"
                return
            if text == "👥 Premium":
                context.user_data["await"] = "premium_menu"
                await update.message.reply_text(
                    "Premium berish uchun «+ID», olish uchun «-ID» yuboring "
                    "(masalan: +123456789). «⬅️ Orqaga» bilan chiqasiz."
                )
                return
            if text == "📢 Elon":
                context.user_data["await"] = "announce"
                await update.message.reply_text("📢 Elon matnini yuboring — barcha foydalanuvchilarga boradi:")
                return
            if text == "✏️ Salom matni":
                context.user_data["await"] = "welcome"
                await update.message.reply_text("Yangi salom matnini yuboring (/start da chiqadi):")
                return
            if text == "📊 Statistika":
                with conn() as c:
                    secs = c.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
                    conts = c.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
                    prems = c.execute("SELECT COUNT(*) FROM premium_users").fetchone()[0]
                await update.message.reply_text(
                    f"📊 Statistika\n👥 Foydalanuvchilar: {count_users()}\n"
                    f"📁 Bo'limlar: {secs}\n📄 Kontentlar: {conts}\n⭐ Premium: {prems}"
                )
                return
            await_state = context.user_data.get("await")
            if await_state == "section_title":
                with conn() as c:
                    cur = c.execute(
                        "INSERT INTO sections(title, order_no) VALUES(?,?)",
                        (text, c.execute("SELECT COUNT(*) FROM sections").fetchone()[0]),
                    )
                    sec_id = int(cur.lastrowid)
                context.user_data["await"] = "section_premium"
                context.user_data["new_sec"] = sec_id
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⭐ Premium", callback_data=f"sec:prem:{sec_id}"),
                    InlineKeyboardButton("Ochiq qoldirish", callback_data="sec:open"),
                ]])
                # Premium tugmasi bu yerda faqat belgilash uchun: prem callback
                # bo'limni premium qiladi, keyin admin menyuga qaytiladi.
                await update.message.reply_text(
                    f"«{text}» bo'limi yaratildi. Premium qilasizmi?", reply_markup=kb
                )
                return
            if await_state == "sub_add" and text.startswith("@"):
                with conn() as c:
                    c.execute("INSERT OR IGNORE INTO sub_channels(username) VALUES(?)", (text,))
                context.user_data.pop("await", None)
                await update.message.reply_text(f"✅ Majburiy obuna kanali qo'shildi: {text}")
                return
            if await_state == "premium_menu" and (text.startswith("+") or text.startswith("-")):
                try:
                    target = int(text[1:].strip())
                except ValueError:
                    await update.message.reply_text("ID raqam bo'lishi kerak, masalan: +123456789")
                    return
                context.user_data.pop("await", None)
                with conn() as c:
                    if text.startswith("+"):
                        c.execute(
                            "INSERT OR IGNORE INTO premium_users(user_id, granted_at) VALUES(?,?)",
                            (target, datetime.now().isoformat(timespec="seconds")),
                        )
                        msg = f"✅ {target} ga premium berildi."
                    else:
                        c.execute("DELETE FROM premium_users WHERE user_id=?", (target,))
                        msg = "✅ Premium olindi."
                await update.message.reply_text(msg)
                try:
                    await context.bot.send_message(chat_id=target, text=msg)
                except Exception:
                    pass
                return
            if await_state == "announce":
                context.user_data.pop("await", None)
                with conn() as c:
                    users = c.execute("SELECT user_id FROM users").fetchall()
                ok = 0
                for u in users:
                    try:
                        await context.bot.send_message(chat_id=u["user_id"], text=f"📣 Elon\n\n{text}")
                        ok += 1
                    except Exception:
                        pass
                await update.message.reply_text(f"📣 Elon yuborildi: {ok} / {len(users)}")
                return
            if await_state == "welcome":
                context.user_data.pop("await", None)
                set_setting("welcome", text)
                await update.message.reply_text("✅ Salom matni saqlandi.")
                return
        # Oddiy foydalanuvchi: bo'lim tugmasi
        titles = {s["title"] for s in get_sections()} | {s["title"] + " ⭐" for s in get_sections()}
        if text in titles:
            return await show_section(update, context, text)
        await update.message.reply_text("Menyudan tanlang 👇", reply_markup=main_menu(user.id))

    async def handle_media(update, context):
        user = update.effective_user
        if user.id != OWNER_ID:
            return
        if context.user_data.get("await") != "content":
            await update.message.reply_text("Avval bo'limda «➕ Kontent qo'shish» ni bosing.")
            return
        sec_id = context.user_data.get("content_sec")
        if not sec_id or get_section(sec_id) is None:
            await update.message.reply_text("Bo'lim topilmadi.")
            return
        kind, file_id, caption = "text", "", ""
        if update.message.photo:
            kind = "photo"
            file_id = update.message.photo[-1].file_id
            caption = update.message.caption or ""
        elif update.message.video:
            kind = "video"
            file_id = update.message.video.file_id
            caption = update.message.caption or ""
        elif update.message.text:
            kind = "text"
            caption = update.message.text
        else:
            await update.message.reply_text("Faqat matn, rasm yoki video yuboring.")
            return
        with conn() as c:
            order_no = c.execute(
                "SELECT COUNT(*) FROM contents WHERE section_id=?", (sec_id,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO contents(section_id, kind, file_id, caption, order_no) VALUES(?,?,?,?,?)",
                (sec_id, kind, file_id, caption, order_no),
            )
        context.user_data.pop("await", None)
        context.user_data.pop("content_sec", None)
        await update.message.reply_text("✅ Kontent bo'limga qo'shildi.")

    async def sec_open_cb(update, context):
        q = update.callback_query
        await q.answer()
        if q.from_user.id != OWNER_ID:
            return
        context.user_data.pop("await", None)
        context.user_data.pop("new_sec", None)
        await q.edit_message_text("Bo'lim ochiq qoldirildi ✅")

    async def post_init(app):
        init_db()
        log.info("Umumiy bot ishga tushdi")

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(sub_check, pattern=r"^sub:check$"))
    app.add_handler(CallbackQueryHandler(section_cb, pattern=r"^sec:(add|prem|delask|delyes|delno):"))
    app.add_handler(CallbackQueryHandler(sec_open_cb, pattern=r"^sec:open$"))
    app.add_handler(CallbackQueryHandler(sub_cb, pattern=r"^sub:del:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main():
    if not TOKEN:
        print("XATO: CHILD_BOT_TOKEN env yo'q")
        raise SystemExit(1)
    init_db()
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
