"""раздача от депчика — бот розыгрышей и рефералки для чата. Версия 8."""
import asyncio
import html
import logging
import os
import random
import sys
from urllib.parse import quote

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ContentType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, ChatMemberUpdated, ErrorEvent,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           ReplyParameters)

VERSION = "v8"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("razdacha")


# ---------------- КОНФИГ ----------------
def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_int(name, default=0):
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.error("Переменная %s должна быть числом, а там: %r. Беру %s", name, raw, default)
        return default


BOT_TOKEN = env("BOT_TOKEN")
CHAT_ID = env_int("CHAT_ID")
CHAT_LINK = env("CHAT_LINK")
ADMIN_ID = env_int("ADMIN_ID")
SPONSOR = env("SPONSOR", "@your_username")
PROMO_TEXT = env("PROMO_TEXT")
WIN_NOTE = env("WIN_NOTE", "✅ Подарок отправлен.")
RAFFLE_EVERY = max(1, env_int("RAFFLE_EVERY", 50))
FRIENDS_PER_GIFT = max(1, env_int("FRIENDS_PER_GIFT", 5))
DB_PATH = env("DB_PATH", "/data/bot.db")

missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("CHAT_ID", CHAT_ID)) if not v]
if missing:
    log.error("НЕ ЗАДАНЫ ПЕРЕМЕННЫЕ: %s. Добавь их в Railway -> Variables.", ", ".join(missing))
    sys.exit(1)

ANON_ADMIN_ID = 1087968824  # «анонимный админ» Telegram
COUNTED_TYPES = {
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.STICKER,
    ContentType.VOICE, ContentType.VIDEO_NOTE, ContentType.ANIMATION,
    ContentType.AUDIO, ContentType.DOCUMENT,
}

router = Router()
db: aiosqlite.Connection
raffle_lock = asyncio.Lock()


# ---------------- БАЗА ----------------
async def init_db():
    global db
    path = DB_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        db = await aiosqlite.connect(path)
    except Exception as e:
        log.warning("Не могу открыть базу %s (%s). Использую ./bot.db — после перезапуска данные пропадут. "
                    "Добавь Volume с путём /data.", path, e)
        db = await aiosqlite.connect("bot.db")
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            link TEXT, gifts_given INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS refs(
            invited_id INTEGER PRIMARY KEY, inviter_id INTEGER, invited_name TEXT,
            messaged INTEGER DEFAULT 0, in_chat INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS recent(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, msg_id INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER);
        """
    )
    await db.commit()


async def q1(sql, args=()):
    cur = await db.execute(sql, args)
    return await cur.fetchone()


async def qa(sql, args=()):
    cur = await db.execute(sql, args)
    return await cur.fetchall()


def esc(x):
    return html.escape(str(x))


def display(username, first_name, uid):
    return f"@{username}" if username else (first_name or str(uid))


def mention(uid, username, first_name):
    return f'<a href="tg://user?id={uid}">{esc(display(username, first_name, uid))}</a> (id <code>{uid}</code>)'


async def counted(uid):
    r = await q1("SELECT COUNT(*) FROM refs WHERE inviter_id=? AND messaged=1 AND in_chat=1", (uid,))
    return r[0]


async def get_counter():
    r = await q1("SELECT v FROM meta WHERE k='cnt'")
    return r[0] if r else 0


async def notify_admin(bot: Bot, text):
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан, уведомление не отправлено: %s", text)
        return
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        log.error("Не смог написать админу (нажми /start у бота в личке): %s", e)


# ---------------- ТЕКСТЫ ----------------
LINE = "━━━━━━━━━━━━━━━━━━"


def box(title):
    return f"┌{'─' * 18}\n│ {title}\n└{'─' * 18}"


def start_text(name):
    return f"👋 <b>Привет, {esc(name)}!</b>\n\n👥 <b>Приглашения — подарок за друзей</b>"


def how_text():
    return (
        "❓ <b>Как получить подарок</b>\n\n"
        "1️⃣ Скопируй свою ссылку в меню\n"
        "2️⃣ Отправь её друзьям\n"
        f"3️⃣ Как только {FRIENDS_PER_GIFT} друзей зайдут по ней в чат — тебе прилетит подарок 🧸\n\n"
        f"{'─' * 24}\n\n"
        "<b>Важно:</b>\n"
        "• считаются только переходы по <b>твоей</b> ссылке\n"
        "• друг должен <b>написать в чат</b> — молчуны не считаются\n"
        "• друг должен <b>остаться</b> в чате — вышел, и он не в счёт\n"
        "• один человек засчитывается один раз, повторные заходы не считаются\n"
        f"• подарков можно получить сколько угодно: каждые {FRIENDS_PER_GIFT} друзей — новый"
    )


async def panel_text(uid, name, link):
    c = await counted(uid)
    row = await q1("SELECT gifts_given FROM users WHERE id=?", (uid,))
    gifts = row[0] if row else 0
    prog = c % FRIENDS_PER_GIFT
    filled = round(prog / FRIENDS_PER_GIFT * 10)
    bar = "▓" * filled + "░" * (10 - filled)
    target = (f"Первый подарок — за {FRIENDS_PER_GIFT} друзей" if gifts == 0 and c == 0
              else f"Следующий подарок — ещё {FRIENDS_PER_GIFT - prog} друзей")
    return (
        f"{LINE}\n👋 <b>Привет, {esc(name)}!</b>\n{LINE}\n\n"
        "👥 Приглашай друзей в чат!\n\n"
        f"За каждые {FRIENDS_PER_GIFT} друзей — подарок 🧸\n\n"
        "Друг должен перейти по твоей ссылке и остаться в чате.\n\n"
        f"{box('📊 ТВОЙ ПРОГРЕСС')}\n"
        f"{bar}  {prog}/{FRIENDS_PER_GIFT}\n\n"
        f"👥 Друзей засчитано: {c}\n"
        f"🧸 Подарков получено: {gifts}\n"
        f"🎯 {target}\n\n"
        f"{box('🛡 КАКИХ ДРУЗЕЙ ЗАСЧИТАЕМ')}\n"
        "Чтобы никто не накручивал ботами, друг засчитается, только если:\n\n"
        "💬 он написал хотя бы 1 сообщение в чат\n\n"
        "<i>Остальные могут зайти в чат, но в твой счёт не пойдут.</i>\n\n"
        f"{box('🔗 ТВОЯ ССЫЛКА')}\n"
        f"<code>{link}</code>\n\n"
        "<i>Нажми на ссылку, чтобы скопировать. Считаются только те, кто зашёл "
        "именно по ней и остался в чате.</i>"
    )


# ---------------- КЛАВИАТУРЫ ----------------
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пригласить друзей", callback_data="panel")]])


def kb_panel(link):
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('Залетай в чат, тут раздают мишек 🧸')}"
    rows = [[InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share)]]
    if CHAT_LINK.startswith("http"):
        rows.append([InlineKeyboardButton(text="🔗 Открыть чат", url=CHAT_LINK)])
    rows += [
        [InlineKeyboardButton(text="👥 Мои друзья", callback_data="friends"),
         InlineKeyboardButton(text="🏆 Топ", callback_data="top")],
        [InlineKeyboardButton(text="❓ Как это работает", callback_data="how")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh"),
         InlineKeyboardButton(text="◀️ Главное", callback_data="main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В меню", callback_data="panel")]])


# ---------------- ЛИЧКА ----------------
async def upsert_user(u):
    await db.execute(
        "INSERT INTO users(id,username,first_name) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (u.id, u.username, u.first_name))
    await db.commit()


async def get_link(bot: Bot, uid):
    r = await q1("SELECT link FROM users WHERE id=?", (uid,))
    if r and r[0]:
        return r[0]
    inv = await bot.create_chat_invite_link(CHAT_ID, name=f"ref{uid}")
    await db.execute("UPDATE users SET link=? WHERE id=?", (inv.invite_link, uid))
    await db.commit()
    return inv.invite_link


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(m: Message):
    await upsert_user(m.from_user)
    await m.answer(start_text(m.from_user.first_name), reply_markup=kb_start())


async def send_panel(call: CallbackQuery, edit=False):
    u = call.from_user
    await upsert_user(u)
    try:
        link = await get_link(call.bot, u.id)
    except Exception as e:
        log.error("Не смог создать ссылку: %s", e)
        await call.message.answer("😕 Не получилось создать ссылку. Попробуй позже.")
        await notify_admin(call.bot, "⚠️ Не могу создать пригласительную ссылку. Бот должен быть АДМИНОМ чата "
                                     f"с правом «Приглашать пользователей».\n<code>{esc(e)}</code>")
        return
    text = await panel_text(u.id, u.first_name, link)
    if edit:
        try:
            await call.message.edit_text(text, reply_markup=kb_panel(link))
        except Exception:
            pass
    else:
        await call.message.answer(text, reply_markup=kb_panel(link))


@router.callback_query(F.data == "panel")
async def cb_panel(call: CallbackQuery):
    await call.answer()
    await send_panel(call)


@router.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery):
    await call.answer("Обновлено")
    await send_panel(call, edit=True)


@router.callback_query(F.data == "main")
async def cb_main(call: CallbackQuery):
    await call.answer()
    await call.message.answer(start_text(call.from_user.first_name), reply_markup=kb_start())


@router.callback_query(F.data == "how")
async def cb_how(call: CallbackQuery):
    await call.answer()
    await call.message.answer(how_text(), reply_markup=kb_menu())


@router.callback_query(F.data == "friends")
async def cb_friends(call: CallbackQuery):
    await call.answer()
    rows = await qa("SELECT invited_name, messaged, in_chat FROM refs WHERE inviter_id=?", (call.from_user.id,))
    if not rows:
        text = ("👥 <b>Мои друзья</b>\n\nОтправь свою ссылку друзьям — "
                "как только кто-то зайдёт по ней, он появится здесь.")
    else:
        lines = []
        for name, msg, inchat in rows:
            if not inchat:
                lines.append(f"🚪 {esc(name)} — вышел(ла)")
            elif msg:
                lines.append(f"✅ {esc(name)} — засчитан")
            else:
                lines.append(f"⏳ {esc(name)} — ещё не писал(а)")
        c = await counted(call.from_user.id)
        text = f"👥 <b>Мои друзья</b> ({c} засчитано)\n\n" + "\n".join(lines)
    await call.message.answer(text, reply_markup=kb_menu())


@router.callback_query(F.data == "top")
async def cb_top(call: CallbackQuery):
    await call.answer()
    rows = await qa(
        "SELECT r.inviter_id, COUNT(*) c, u.username, u.first_name FROM refs r "
        "LEFT JOIN users u ON u.id=r.inviter_id WHERE r.messaged=1 AND r.in_chat=1 "
        "GROUP BY r.inviter_id ORDER BY c DESC LIMIT 10")
    medals = ["🥇", "🥈", "🥉"]
    lines, ids = [], []
    for i, (uid, c, un, fn) in enumerate(rows):
        ids.append(uid)
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {esc(display(un, fn, uid))} — {c} 👥")
    me = await counted(call.from_user.id)
    tail = (f"Ты: {me} 👥 — ты в топе! 🔥" if call.from_user.id in ids
            else f"Ты: {me} 👥 — до топа ещё чуть-чуть!")
    body = "\n".join(lines) if lines else "Пока никого — будь первым!"
    await call.message.answer(f"🏆 <b>Топ приглашающих</b>\n\n{body}\n\n<i>{tail}</i>", reply_markup=kb_menu())


# ---------------- ДИАГНОСТИКА ----------------
@router.message(Command("id"))
async def cmd_id(m: Message):
    ok = "✅ совпадает с CHAT_ID" if m.chat.id == CHAT_ID else f"❌ НЕ совпадает с CHAT_ID ({CHAT_ID})"
    await m.reply(f"id этого чата: <code>{m.chat.id}</code>\n{ok}\nтвой id: <code>{m.from_user.id}</code>")


@router.message(Command("count"))
async def cmd_count(m: Message):
    await m.reply(f"Счётчик розыгрыша: {await get_counter()} из {RAFFLE_EVERY}\n"
                  f"Версия бота: {VERSION}")


@router.message(Command("raffle"))
async def cmd_raffle(m: Message, bot: Bot):
    if m.from_user.id != ADMIN_ID:
        await m.reply(f"Команда только для админа. Твой id: <code>{m.from_user.id}</code>, "
                      f"ADMIN_ID в Railway: <code>{ADMIN_ID}</code>")
        return
    async with raffle_lock:
        await db.execute("INSERT INTO recent(user_id,msg_id) VALUES(?,?)", (m.from_user.id, m.message_id))
        await db.commit()
        try:
            await run_raffle(bot)
        except Exception as e:
            log.exception("тест розыгрыша")
            await m.reply(f"⚠️ Ошибка розыгрыша: <code>{esc(repr(e))}</code>")


# ---------------- ЧАТ ----------------
@router.chat_member(F.chat.id == CHAT_ID)
async def on_member(ev: ChatMemberUpdated, bot: Bot):
    u = ev.new_chat_member.user
    if u.is_bot:
        return
    st = ev.new_chat_member.status
    if st == ChatMemberStatus.MEMBER:
        link = ev.invite_link
        if link and link.creator.id == bot.id and (link.name or "").startswith("ref"):
            try:
                inviter = int(link.name[3:])
            except ValueError:
                return
            if inviter == u.id:
                return
            await db.execute("INSERT OR IGNORE INTO refs(invited_id,inviter_id,invited_name) VALUES(?,?,?)",
                             (u.id, inviter, display(u.username, u.first_name, u.id)))
            await db.execute("UPDATE refs SET in_chat=1 WHERE invited_id=?", (u.id,))
            await db.commit()
            log.info("Новый реферал %s от %s", u.id, inviter)
    elif st in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.execute("UPDATE refs SET in_chat=0 WHERE invited_id=?", (u.id,))
        await db.commit()


async def award(bot: Bot, inviter):
    row = await q1("SELECT gifts_given, username, first_name FROM users WHERE id=?", (inviter,))
    if not row:
        return
    given, un, fn = row
    due = (await counted(inviter)) // FRIENDS_PER_GIFT - given
    for _ in range(max(0, due)):
        given += 1
        await db.execute("UPDATE users SET gifts_given=gifts_given+1 WHERE id=?", (inviter,))
        await db.commit()
        await notify_admin(bot, f"🎁 За друзей: {mention(inviter, un, fn)} — подарок №{given}. Отправь вручную.")
        try:
            await bot.send_message(inviter, f"🎁 <b>Ты пригласил {FRIENDS_PER_GIFT} друзей!</b> 🧸\n"
                                            "Подарок скоро прилетит. Спасибо, что зовёшь друзей!")
        except Exception:
            pass


async def run_raffle(bot: Bot):
    rows = await qa("SELECT user_id, msg_id FROM recent")
    if not rows:
        return
    uid, msg_id = random.choice(rows)  # больше сообщений — выше шанс
    await db.execute("DELETE FROM recent")
    await db.execute("INSERT INTO meta(k,v) VALUES('cnt',0) ON CONFLICT(k) DO UPDATE SET v=0")
    await db.commit()
    try:
        u = (await bot.get_chat_member(CHAT_ID, uid)).user
        who = mention(uid, u.username, u.first_name)
    except Exception:
        who = f"id <code>{uid}</code>"
    await notify_admin(bot, f"🎉 Победитель розыгрыша: {who}. Отправь подарок вручную.")
    text = (f"🎉 <b>Поздравляю!</b>\n"
            f"📨 Ты выиграл мишку 🧸 от {esc(SPONSOR)}\n"
            f"{esc(WIN_NOTE)}")
    if PROMO_TEXT:
        text += f"\n\n{PROMO_TEXT}"
    try:
        await bot.send_message(CHAT_ID, text,
                               reply_parameters=ReplyParameters(message_id=msg_id,
                                                                allow_sending_without_reply=True))
    except Exception as e1:
        log.error("Поздравление с ответом не ушло: %s. Пробую без ответа и без разметки", e1)
        try:
            await bot.send_message(CHAT_ID, f"🎉 Поздравляю! Ты выиграл мишку 🧸 от {SPONSOR}",
                                   parse_mode=None)
        except Exception as e2:
            await notify_admin(bot, f"⚠️ Не могу написать в чат: <code>{esc(e2)}</code>")
            raise


@router.message(F.chat.id == CHAT_ID)
async def on_chat_message(m: Message, bot: Bot):
    if m.content_type not in COUNTED_TYPES:
        return
    if not m.from_user or m.from_user.is_bot or m.from_user.id == ANON_ADMIN_ID or m.sender_chat:
        return
    uid = m.from_user.id
    cur = await db.execute("UPDATE refs SET messaged=1 WHERE invited_id=? AND messaged=0 AND in_chat=1", (uid,))
    await db.commit()
    if cur.rowcount:
        r = await q1("SELECT inviter_id FROM refs WHERE invited_id=?", (uid,))
        if r:
            await award(bot, r[0])
    async with raffle_lock:
        await db.execute("INSERT INTO recent(user_id,msg_id) VALUES(?,?)", (uid, m.message_id))
        await db.execute("INSERT INTO meta(k,v) VALUES('cnt',1) ON CONFLICT(k) DO UPDATE SET v=v+1")
        await db.commit()
        cnt = await get_counter()
        log.info("Сообщение от %s, счётчик %s/%s", uid, cnt, RAFFLE_EVERY)
        if cnt >= RAFFLE_EVERY:
            await run_raffle(bot)


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot):
    log.error("Ошибка обработчика", exc_info=event.exception)
    await notify_admin(bot, f"⚠️ Ошибка бота: <code>{esc(repr(event.exception))}</code>")
    return True


# ---------------- ЗАПУСК ----------------
async def main():
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("Запущен @%s, версия %s, чат %s, розыгрыш каждые %s", me.username, VERSION, CHAT_ID, RAFFLE_EVERY)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await notify_admin(bot, f"✅ Бот запущен ({VERSION}). Розыгрыш каждые {RAFFLE_EVERY} сообщений.")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
