import asyncio
import logging
import os
import random
from urllib.parse import quote

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, ChatMemberUpdated,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("razdacha")

# ---------- КОНФИГ (всё через переменные окружения) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])              # id чата, вида -100123...
CHAT_LINK = os.environ.get("CHAT_LINK", "")       # публичная ссылка на чат (кнопка «Открыть чат»)
def _ids(v):
    return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]


# можно указать несколько id через запятую: бот выберет случайный
GIFT_IDS = _ids(os.environ.get("GIFT_ID", ""))                    # подарки за друзей
RAFFLE_GIFT_IDS = _ids(os.environ.get("RAFFLE_GIFT_ID", "")) or GIFT_IDS  # подарки в розыгрыше
_emoji_cache = {}
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SPONSOR = os.environ.get("SPONSOR", "@your_username")   # «от @...» в сообщении о выигрыше
PROMO_TEXT = os.environ.get("PROMO_TEXT", "")           # рекламный блок под поздравлением (HTML)
RAFFLE_EVERY = int(os.environ.get("RAFFLE_EVERY", "30"))
FRIENDS_PER_GIFT = int(os.environ.get("FRIENDS_PER_GIFT", "5"))
DB_PATH = os.environ.get("DB_PATH", "/data/bot.db")

router = Router()
db: aiosqlite.Connection
raffle_lock = asyncio.Lock()


# ---------- БАЗА ----------
async def init_db():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
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


def display(username, first_name, uid):
    if username:
        return f"@{username}"
    return first_name or str(uid)


async def counted(uid):
    r = await q1("SELECT COUNT(*) FROM refs WHERE inviter_id=? AND messaged=1 AND in_chat=1", (uid,))
    return r[0]


# ---------- ТЕКСТЫ ----------
LINE = "━━━━━━━━━━━━━━━━━━"


def box(title):
    return f"┌{'─' * 18}\n│ {title}\n└{'─' * 18}"


def start_text(name):
    return f"👋 <b>Привет, {name}!</b>\n\n👥 <b>Приглашения — подарок за друзей</b>"


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
    left = FRIENDS_PER_GIFT - prog
    target = (f"Первый подарок — за {FRIENDS_PER_GIFT} друзей" if gifts == 0 and c == 0
              else f"Следующий подарок — ещё {left} друзей")
    return (
        f"{LINE}\n👋 <b>Привет, {name}!</b>\n{LINE}\n\n"
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


# ---------- КЛАВИАТУРЫ ----------
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пригласить друзей", callback_data="panel")]])


def kb_panel(link):
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('Залетай в чат, тут раздают мишек 🧸')}"
    rows = [[InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share)]]
    if CHAT_LINK:
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


# ---------- ССЫЛКИ ----------
async def get_link(bot: Bot, uid):
    r = await q1("SELECT link FROM users WHERE id=?", (uid,))
    if r and r[0]:
        return r[0]
    inv = await bot.create_chat_invite_link(CHAT_ID, name=f"ref{uid}")
    await db.execute("UPDATE users SET link=? WHERE id=?", (inv.invite_link, uid))
    await db.commit()
    return inv.invite_link


async def upsert_user(u):
    await db.execute(
        "INSERT INTO users(id,username,first_name) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (u.id, u.username, u.first_name))
    await db.commit()


async def gift_emoji(bot, gid):
    if not _emoji_cache:
        try:
            for g in (await bot.get_available_gifts()).gifts:
                _emoji_cache[g.id] = g.sticker.emoji or "🎁"
        except Exception as e:
            log.error("get_available_gifts failed: %s", e)
    return _emoji_cache.get(gid, "🎁")


# ---------- ЛИЧКА ----------
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(m: Message):
    await upsert_user(m.from_user)
    await m.answer(start_text(m.from_user.first_name), reply_markup=kb_start())


async def send_panel(call: CallbackQuery, edit=False):
    u = call.from_user
    await upsert_user(u)
    link = await get_link(call.bot, u.id)
    text = await panel_text(u.id, u.first_name, link)
    kb = kb_panel(link)
    if edit:
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    else:
        await call.message.answer(text, reply_markup=kb)


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
    rows = await qa("SELECT invited_id, invited_name, messaged, in_chat FROM refs WHERE inviter_id=?",
                    (call.from_user.id,))
    if not rows:
        text = ("👥 <b>Мои друзья</b>\n\nОтправь свою ссылку друзьям — "
                "как только кто-то зайдёт по ней, он появится здесь.")
    else:
        lines = []
        for _, name, msg, inchat in rows:
            if not inchat:
                lines.append(f"🚪 {name} — вышел(ла)")
            elif msg:
                lines.append(f"✅ {name} — засчитан")
            else:
                lines.append(f"⏳ {name} — ещё не писал(а)")
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
    lines = []
    ids = []
    for i, (uid, c, un, fn) in enumerate(rows):
        ids.append(uid)
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {display(un, fn, uid)} — {c} 👥")
    me = await counted(call.from_user.id)
    tail = (f"Ты: {me} 👥 — ты в топе! 🔥" if call.from_user.id in ids
            else f"Ты: {me} 👥 — до топа ещё чуть-чуть!")
    body = "\n".join(lines) if lines else "Пока никого — будь первым!"
    await call.message.answer(f"🏆 <b>Топ приглашающих</b>\n\n{body}\n\n<i>{tail}</i>",
                              reply_markup=kb_menu())


# ---------- АДМИН: узнать id подарков ----------
@router.message(Command("gifts"), F.chat.type == ChatType.PRIVATE)
async def cmd_gifts(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    gifts = await m.bot.get_available_gifts()
    lines = [f"{g.sticker.emoji or ''} {g.star_count}⭐ — <code>{g.id}</code>" for g in gifts.gifts]
    await m.answer("\n".join(lines[:60]) or "Пусто")


# ---------- ЧАТ: вступление / выход ----------
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
            name = display(u.username, u.first_name, u.id)
            await db.execute("INSERT OR IGNORE INTO refs(invited_id,inviter_id,invited_name) VALUES(?,?,?)",
                             (u.id, inviter, name))
            await db.execute("UPDATE refs SET in_chat=1 WHERE invited_id=? AND inviter_id=?", (u.id, inviter))
            await db.commit()
    elif st in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.execute("UPDATE refs SET in_chat=0 WHERE invited_id=?", (u.id,))
        await db.commit()


# ---------- ЧАТ: сообщения ----------
async def award(bot: Bot, inviter):
    row = await q1("SELECT gifts_given FROM users WHERE id=?", (inviter,))
    if not row:
        return
    given = row[0]
    due = (await counted(inviter)) // FRIENDS_PER_GIFT - given
    for _ in range(max(0, due)):
        try:
            gid = random.choice(GIFT_IDS)
            await bot.send_gift(user_id=inviter, gift_id=gid,
                                text=f"Подарок за {FRIENDS_PER_GIFT} друзей!")
        except Exception as e:
            log.error("send_gift failed: %s", e)
            if ADMIN_ID:
                await bot.send_message(ADMIN_ID, f"⚠️ Не смог отправить подарок {inviter}: {e}")
            return
        await db.execute("UPDATE users SET gifts_given=gifts_given+1 WHERE id=?", (inviter,))
        await db.commit()
        try:
            await bot.send_message(inviter, f"🎁 <b>Тебе прилетел подарок!</b> {await gift_emoji(bot, gid)}\nСпасибо, что зовёшь друзей!")
        except Exception:
            pass


async def run_raffle(bot: Bot):
    rows = await qa("SELECT user_id, msg_id FROM recent")
    uid, msg_id = random.choice(rows)  # чем больше сообщений — тем выше шанс
    await db.execute("DELETE FROM recent")
    await db.execute("UPDATE meta SET v=0 WHERE k='cnt'")
    await db.commit()
    try:
        gid = random.choice(RAFFLE_GIFT_IDS)
        await bot.send_gift(user_id=uid, gift_id=gid, text="Приз из раздачи!")
    except Exception as e:
        log.error("raffle gift failed: %s", e)
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"⚠️ Розыгрыш: не смог отправить подарок {uid}: {e}")
        return
    text = (f"🎉 <b>Поздравляю!</b>\n"
            f"📨 Ты выиграл подарок {await gift_emoji(bot, gid)} от {SPONSOR}\n"
            f"✅ Подарок отправлен.")
    if PROMO_TEXT:
        text += f"\n\n{PROMO_TEXT}"
    await bot.send_message(CHAT_ID, text, reply_to_message_id=msg_id,
                           allow_sending_without_reply=True)


@router.message(F.chat.id == CHAT_ID, F.from_user, ~F.from_user.is_bot)
async def on_chat_message(m: Message, bot: Bot):
    uid = m.from_user.id
    # засчёт реферала: первое сообщение приглашённого
    cur = await db.execute("UPDATE refs SET messaged=1 WHERE invited_id=? AND messaged=0 AND in_chat=1", (uid,))
    await db.commit()
    if cur.rowcount:
        r = await q1("SELECT inviter_id FROM refs WHERE invited_id=?", (uid,))
        if r:
            await award(bot, r[0])
    # счётчик розыгрыша
    async with raffle_lock:
        await db.execute("INSERT INTO recent(user_id,msg_id) VALUES(?,?)", (uid, m.message_id))
        await db.execute("INSERT INTO meta(k,v) VALUES('cnt',1) ON CONFLICT(k) DO UPDATE SET v=v+1")
        await db.commit()
        cnt = (await q1("SELECT v FROM meta WHERE k='cnt'"))[0]
        if cnt >= RAFFLE_EVERY:
            await run_raffle(bot)


async def main():
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
