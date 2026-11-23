"""Telegram-бот «Приветствие и согласие» (aiogram 3.x).

Демо по мотивам заказа с биржи: приветственный бот, в котором человек сам
подтверждает согласие на сообщения, плюс отслеживание источников перехода и
админ-панель (статистика, экспорт CSV, рассылка только согласившимся).

Запуск:
    pip install "aiogram>=3.7" python-dotenv
    # в .env: BOT_TOKEN=...  и  ADMIN_ID=ваш_telegram_id
    python bot.py
"""
import asyncio
import csv
import html
import io
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv необязателен
    pass

DB_PATH = os.getenv("DB_PATH", "subscribers.db")


def load_admin_id():
    raw = os.getenv("ADMIN_ID", "").strip()
    return int(raw) if raw.isdigit() else None


ADMIN_ID = load_admin_id()

# ============================================================
# НАСТРОЙКИ ДЕМО-ЗАВЕДЕНИЯ (меняются под любого клиента)
# ============================================================
BRAND = "Зерно"
CURRENCY = "BYN"
ADDRESS = "г. Минск, ул. Примерная, 10"
HOURS = "ежедневно, 08:00–22:00"
PHONE = "+375 29 000-00-00"
DIV = "━━━━━━━━━━━━━━━━━━"

# метка в ссылке -> (эмодзи, короткое имя, строка приветствия, промокод)
SOURCES = {
    "site": ("🌐", "Сайт", "Рады видеть вас на нашем сайте! 🌐", "SITE10"),
    "instagram": ("📸", "Instagram", "Рады видеть вас из Instagram! 📸", "INSTA10"),
    "ads": ("📣", "Реклама", "Вы пришли по нашей рекламе — спасибо за интерес! 📣", "ADS10"),
    "qr": ("🔳", "QR-код", "Вы отсканировали QR-код в нашей кофейне — добро пожаловать! 🔳", "QR10"),
    "partner": ("🤝", "Партнёр", "Нас порекомендовал партнёр — приятно познакомиться! 🤝", "FRIEND10"),
    "direct": ("➡️", "Прямой", "Рады видеть вас! 👋", "HELLO10"),
}

# категория, эмодзи, [(название ≤ 22 символов, цена)]
MENU = [
    ("Кофе", "☕", [("Эспрессо", 3.50), ("Капучино", 5.50), ("Раф", 6.50)]),
    ("Десерты", "🍰", [("Чизкейк", 7.00), ("Круассан", 4.50), ("Маффин", 4.00)]),
    ("Холодное", "🧊", [("Айс-латте", 6.50), ("Лимонад", 5.00), ("Смузи", 7.50)]),
]

BTN_MENU = "☕ Меню"
BTN_CONTACTS = "📍 Контакты"
BTN_PROMO = "🎁 Промокод"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "🛠 Админ-панель"
BTN_SKIP = "⏭ Пропустить"
BTN_SHARE = "📱 Поделиться номером"

# ============================================================
# ТЕКСТЫ
# ============================================================
PRIVACY = (
    "🔒 <b>Какие данные мы храним</b>\n"
    "<blockquote expandable>"
    "• Telegram ID, имя и username\n"
    "• источник перехода (откуда вы пришли)\n"
    "• дату вашего согласия\n"
    "• номер телефона — только если вы сами им поделились\n\n"
    "Зачем: чтобы присылать вам сообщения, на которые вы согласились. "
    "Мы не передаём данные третьим лицам.\n\n"
    "Отписаться: /stop или ⚙️ Настройки.\n"
    "Удалить все данные: ⚙️ Настройки → «Удалить мои данные»."
    "</blockquote>"
)

PHONE_ASK = (
    "📱 <b>Хотите персональные предложения?</b>\n"
    "Поделитесь номером телефона. Это <i>необязательно</i>, можно пропустить."
)


def greeting_text(source):
    line = src_meta(source)[2]
    return (
        f"☕ <b>Добро пожаловать в «{BRAND}»!</b>\n"
        f"{line}\n\n"
        f"{DIV}\n"
        "<blockquote>Мы пишем редко и по делу:\n"
        "🎁 скидки и промокоды\n"
        "🆕 новинки меню\n"
        "📍 события в наших кофейнях</blockquote>\n"
        f"{DIV}\n\n"
        "Чтобы присылать вам сообщения, нужно ваше <b>согласие</b>.\n"
        "Отписаться можно в любой момент."
    )


def promo_text(source):
    code = src_meta(source)[3]
    return (
        "🎁 <b>Ваш подарок</b>\n\n"
        f"<blockquote>Скидка 10% на первый заказ\nПромокод: <code>{code}</code></blockquote>\n"
        "Нажмите на код, чтобы скопировать, и назовите его на кассе."
    )


def menu_text():
    parts = [f"☕ <b>МЕНЮ · «{BRAND}»</b>\n{DIV}"]
    for cat, emoji, items in MENU:
        rows = "\n".join(f"{html.escape(n):<22}{p:>7.2f}" for n, p in items)
        parts.append(f"{emoji} <b>{cat}</b>\n<pre>{rows}</pre>")
    parts.append(f"<i>Цены в {CURRENCY}</i>")
    return "\n\n".join(parts)


def contacts_text():
    return (
        f"📍 <b>«{BRAND}» · контакты</b>\n{DIV}\n"
        f"🏠 {ADDRESS}\n"
        f"🕗 {HOURS}\n"
        f"📞 {PHONE}\n\n"
        "<i>Это демонстрационные данные.</i>"
    )


def settings_text(user):
    on = bool(user["consent"])
    return (
        f"⚙️ <b>Настройки</b>\n{DIV}\n"
        f"🔔 Сообщения: <b>{'включены ✅' if on else 'выключены ❌'}</b>\n"
        f"📱 Телефон: <b>{'указан' if user['phone'] else 'не указан'}</b>"
    )


# ============================================================
# БАЗА ДАННЫХ (SQLite)
# ============================================================
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return closing(con)


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                first_name TEXT,
                username   TEXT,
                source     TEXT NOT NULL DEFAULT 'direct',
                phone      TEXT,
                consent    INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                unsub_at   TEXT,
                blocked    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consent_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action  TEXT NOT NULL,
                source  TEXT,
                at      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcasts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                audience   TEXT NOT NULL,
                text       TEXT,
                has_photo  INTEGER NOT NULL DEFAULT 0,
                sent       INTEGER NOT NULL DEFAULT 0,
                failed     INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        con.commit()


def get_user(user_id):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def add_user(user_id, first_name, username, source):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, username, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, first_name, username, source, now()),
        )
        con.commit()


def touch_user(user_id, first_name, username, source=None):
    """Обновляет имя, снимает отметку блокировки; источник меняет, только если человек ещё не соглашался."""
    with db() as con:
        con.execute(
            "UPDATE users SET first_name = ?, username = ?, blocked = 0 WHERE user_id = ?",
            (first_name, username, user_id),
        )
        if source:
            con.execute(
                "UPDATE users SET source = ? WHERE user_id = ? AND consent_at IS NULL",
                (source, user_id),
            )
        con.commit()


def ensure_user(tg_user):
    user = get_user(tg_user.id)
    if user is None:
        add_user(tg_user.id, tg_user.first_name, tg_user.username, "direct")
        user = get_user(tg_user.id)
    return user


def set_consent(user_id, agree):
    with db() as con:
        row = con.execute("SELECT source FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return
        if agree:
            con.execute(
                "UPDATE users SET consent = 1, consent_at = ?, unsub_at = NULL WHERE user_id = ?",
                (now(), user_id),
            )
        else:
            con.execute(
                "UPDATE users SET consent = 0, unsub_at = ? WHERE user_id = ?",
                (now(), user_id),
            )
        con.execute(
            "INSERT INTO consent_log (user_id, action, source, at) VALUES (?, ?, ?, ?)",
            (user_id, "agree" if agree else "revoke", row["source"], now()),
        )
        con.commit()


def save_phone(user_id, phone):
    with db() as con:
        con.execute("UPDATE users SET phone = ? WHERE user_id = ?", (phone, user_id))
        con.commit()


def mark_blocked(user_id, blocked):
    with db() as con:
        con.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id))
        con.commit()


def delete_user(user_id):
    with db() as con:
        con.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM consent_log WHERE user_id = ?", (user_id,))
        con.commit()


def recipients(audience):
    """ID получателей: только согласившиеся и не заблокировавшие бота."""
    sql = "SELECT user_id FROM users WHERE consent = 1 AND blocked = 0"
    args = ()
    if audience != "all":
        sql += " AND source = ?"
        args = (audience,)
    with db() as con:
        return [r["user_id"] for r in con.execute(sql, args).fetchall()]


def get_stats():
    with db() as con:
        one = lambda sql: con.execute(sql).fetchone()[0]
        s = {
            "total": one("SELECT COUNT(*) FROM users"),
            "agreed": one("SELECT COUNT(*) FROM users WHERE consent = 1"),
            "unsub": one("SELECT COUNT(*) FROM users WHERE consent = 0 AND consent_at IS NOT NULL"),
            "silent": one("SELECT COUNT(*) FROM users WHERE consent = 0 AND consent_at IS NULL"),
            "blocked": one("SELECT COUNT(*) FROM users WHERE blocked = 1"),
            "phones": one("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL"),
        }
        s["by_source"] = [
            (r["source"], r["total"], r["agreed"] or 0)
            for r in con.execute(
                "SELECT source, COUNT(*) AS total, SUM(consent) AS agreed "
                "FROM users GROUP BY source ORDER BY total DESC"
            ).fetchall()
        ]
    return s


def audience_counts():
    with db() as con:
        rows = con.execute(
            "SELECT source, COUNT(*) AS n FROM users WHERE consent = 1 AND blocked = 0 "
            "GROUP BY source ORDER BY n DESC"
        ).fetchall()
    return [(r["source"], r["n"]) for r in rows]


def export_csv():
    with db() as con:
        rows = con.execute(
            "SELECT user_id, first_name, username, source, phone, consent, consent_at, "
            "unsub_at, blocked, created_at FROM users ORDER BY created_at"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "имя", "username", "источник", "телефон", "согласие",
                "дата согласия", "дата отписки", "заблокировал бота", "создан"])
    for r in rows:
        w.writerow([r["user_id"], r["first_name"] or "", r["username"] or "", r["source"],
                    r["phone"] or "", "да" if r["consent"] else "нет", r["consent_at"] or "",
                    r["unsub_at"] or "", "да" if r["blocked"] else "нет", r["created_at"]])
    return buf.getvalue().encode("utf-8-sig")  # utf-8-sig, чтобы Excel видел кириллицу


def add_broadcast(audience, text, has_photo):
    with db() as con:
        cur = con.execute(
            "INSERT INTO broadcasts (created_at, audience, text, has_photo) VALUES (?, ?, ?, ?)",
            (now(), audience, text, 1 if has_photo else 0),
        )
        con.commit()
        return cur.lastrowid


def finish_broadcast(bid, sent, failed):
    with db() as con:
        con.execute("UPDATE broadcasts SET sent = ?, failed = ? WHERE id = ?", (sent, failed, bid))
        con.commit()


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================
def normalize_source(arg):
    """Метка из ссылки t.me/бот?start=метка: латиница, цифры и _, до 32 символов."""
    if not arg:
        return "direct"
    tag = re.sub(r"[^a-z0-9_]", "", arg.lower())[:32]
    return tag or "direct"


def src_meta(tag):
    if tag in SOURCES:
        return SOURCES[tag]
    base = SOURCES["direct"]
    return ("🔗", tag[:13], base[2], base[3])


def is_admin(user_id):
    return ADMIN_ID is not None and user_id == ADMIN_ID


def pct(a, b):
    return f"{round(100 * a / b)}%" if b else "—"


def stats_text():
    s = get_stats()
    rows = [f"{'Источник':<13}{'Всего':>6}{'Согл.':>6}{'%':>6}"]
    for tag, total, agreed in s["by_source"]:
        rows.append(f"{src_meta(tag)[1][:12]:<13}{total:>6}{agreed:>6}{pct(agreed, total):>6}")
    table = "\n".join(rows) if s["by_source"] else "Пока нет данных"
    return (
        f"📊 <b>СТАТИСТИКА</b>\n{DIV}\n"
        f"👥 Всего пользователей: <b>{s['total']}</b>\n"
        f"✅ Согласились: <b>{s['agreed']}</b> ({pct(s['agreed'], s['total'])})\n"
        f"🔕 Отписались: <b>{s['unsub']}</b>\n"
        f"⏳ Не ответили или отказались: <b>{s['silent']}</b>\n"
        f"🚫 Заблокировали бота: <b>{s['blocked']}</b>\n"
        f"📱 Оставили телефон: <b>{s['phones']}</b>\n\n"
        f"🧭 <b>По источникам</b>\n<pre>{table}</pre>"
    )


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows]
    )


def menu_kb(admin=False):
    rows = [
        [KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_CONTACTS)],
        [KeyboardButton(text=BTN_PROMO), KeyboardButton(text=BTN_SETTINGS)],
    ]
    if admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SHARE, request_contact=True)], [KeyboardButton(text=BTN_SKIP)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def consent_kb():
    return kb([
        [("✅ Да, согласен(на)", "c:yes")],
        [("ℹ️ Какие данные вы храните?", "c:info")],
        [("Нет, спасибо", "c:no")],
    ])


def settings_kb(user):
    return kb([
        [("🔕 Отписаться", "s:unsub")] if user["consent"] else [("🔔 Подписаться", "s:sub")],
        [("📱 Обновить номер" if user["phone"] else "📱 Добавить номер", "s:phone")],
        [("ℹ️ Какие данные вы храните?", "s:info")],
        [("🗑 Удалить мои данные", "s:del")],
    ])


def admin_kb():
    return kb([
        [("📊 Статистика", "a:stats"), ("📤 Рассылка", "a:bc")],
        [("📥 Экспорт CSV", "a:csv"), ("🔗 Ссылки-источники", "a:links")],
    ])


def unsub_kb():
    return kb([[("🔕 Отписаться от рассылки", "u:stop")]])


async def safe_edit(cb, text, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


# ============================================================
# ОБРАБОТЧИКИ
# ============================================================
router = Router()
router.message.filter(F.chat.type == "private")  # бот работает только в личных чатах
router.callback_query.filter(F.message.chat.type == "private")


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


BACKGROUND_TASKS = set()


async def send_greeting(message: Message, source):
    await message.answer(greeting_text(source), reply_markup=consent_kb())


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    u = message.from_user
    tag = normalize_source(command.args)
    if get_user(u.id) is None:
        add_user(u.id, u.first_name, u.username, tag)
    else:
        touch_user(u.id, u.first_name, u.username, tag)
    user = get_user(u.id)
    if user["consent"]:
        name = html.escape(u.first_name or "друг")
        await message.answer(
            f"С возвращением, {name}! ☕\nВыберите действие в меню ниже.",
            reply_markup=menu_kb(is_admin(u.id)),
        )
        return
    await send_greeting(message, tag)


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    await state.clear()
    user = ensure_user(message.from_user)
    set_consent(user["user_id"], False)
    await message.answer(
        "🔕 <b>Вы отписались.</b> Больше сообщений от нас не будет.\n"
        "Передумаете — ⚙️ Настройки → «Подписаться».",
        reply_markup=menu_kb(is_admin(message.from_user.id)),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=menu_kb(is_admin(message.from_user.id)))


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await fallback(message)
        return
    await state.clear()
    await message.answer(f"🛠 <b>Админ-панель · «{BRAND}»</b>\n{DIV}\nВыберите действие:", reply_markup=admin_kb())


# ---------- содержимое рассылки (состояние админа) ----------
@router.message(Broadcast.content)
async def got_broadcast_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await fallback(message)
        return
    photo = message.photo[-1].file_id if message.photo else None
    body = message.html_text if (message.text or message.caption) else ""
    if not photo and not body:
        await message.answer("Пришлите текст или фото с подписью. Отмена: /cancel")
        return
    limit = 1024 if photo else 4096
    if len(body) > limit:
        await message.answer(f"Слишком длинно: максимум {limit} символов, у вас {len(body)}. Сократите и отправьте снова.")
        return
    data = await state.get_data()
    ids = recipients(data.get("audience", "all"))
    await state.update_data(text=body, photo=photo)
    await state.set_state(Broadcast.confirm)
    # предпросмотр ровно в том виде, в каком увидят подписчики
    if photo:
        await message.bot.send_photo(message.chat.id, photo, caption=body or None, reply_markup=unsub_kb())
    else:
        await message.bot.send_message(message.chat.id, body, reply_markup=unsub_kb())
    await message.answer(
        f"👆 Так увидят подписчики.\n👥 Получателей: <b>{len(ids)}</b>",
        reply_markup=kb([[(f"🚀 Отправить ({len(ids)})", "bc:go")], [("✖️ Отмена", "bc:x")]]),
    )


# ---------- кнопки меню ----------
@router.message(F.text == BTN_MENU)
async def menu_show(message: Message):
    ensure_user(message.from_user)
    await message.answer(menu_text())


@router.message(F.text == BTN_CONTACTS)
async def contacts_show(message: Message):
    ensure_user(message.from_user)
    await message.answer(contacts_text())


@router.message(F.text == BTN_PROMO)
async def promo_show(message: Message):
    user = ensure_user(message.from_user)
    if user["consent"]:
        await message.answer(promo_text(user["source"]))
    else:
        await message.answer("🎁 Промокод откроется после согласия на сообщения.", reply_markup=consent_kb())


@router.message(F.text == BTN_SETTINGS)
async def settings_show(message: Message):
    user = ensure_user(message.from_user)
    await message.answer(settings_text(user), reply_markup=settings_kb(user))


@router.message(F.text == BTN_ADMIN)
async def admin_button(message: Message, state: FSMContext):
    await cmd_admin(message, state)


@router.message(F.text == BTN_SKIP)
async def phone_skip(message: Message):
    await message.answer("Хорошо, без номера 👌", reply_markup=menu_kb(is_admin(message.from_user.id)))


@router.message(F.contact)
async def got_contact(message: Message):
    ensure_user(message.from_user)
    if message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, поделитесь <b>своим</b> номером кнопкой ниже.", reply_markup=phone_kb())
        return
    save_phone(message.from_user.id, message.contact.phone_number)
    await message.answer("Спасибо! 📱 Номер сохранён.", reply_markup=menu_kb(is_admin(message.from_user.id)))


# ---------- согласие ----------
@router.callback_query(F.data == "c:yes")
async def cb_yes(cb: CallbackQuery):
    await cb.answer("Спасибо! 🎉")
    user = ensure_user(cb.from_user)
    set_consent(user["user_id"], True)
    await safe_edit(cb, f"✅ <b>Готово!</b> Вы подписаны на сообщения «{BRAND}».\nОтписаться можно в любой момент: /stop")
    await cb.message.answer(promo_text(user["source"]), reply_markup=menu_kb(is_admin(cb.from_user.id)))
    await cb.message.answer(PHONE_ASK, reply_markup=phone_kb())


@router.callback_query(F.data == "c:no")
async def cb_no(cb: CallbackQuery):
    await cb.answer()
    ensure_user(cb.from_user)
    await safe_edit(cb, "Хорошо, мы вас не побеспокоим 🙂\nЕсли передумаете, просто нажмите /start.")
    await cb.message.answer("Меню и контакты доступны в любой момент.", reply_markup=menu_kb(is_admin(cb.from_user.id)))


@router.callback_query(F.data.in_({"c:info", "s:info"}))
async def cb_info(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(PRIVACY)


@router.callback_query(F.data == "u:stop")
async def cb_unsub(cb: CallbackQuery):
    await cb.answer("Вы отписались")
    user = ensure_user(cb.from_user)
    set_consent(user["user_id"], False)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer("🔕 <b>Вы отписались.</b> Больше сообщений от нас не будет.")


# ---------- настройки ----------
async def refresh_settings(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if user:
        await safe_edit(cb, settings_text(user), settings_kb(user))


@router.callback_query(F.data == "s:sub")
async def cb_sub(cb: CallbackQuery):
    await cb.answer("Подписка включена ✅")
    set_consent(ensure_user(cb.from_user)["user_id"], True)
    await refresh_settings(cb)


@router.callback_query(F.data == "s:unsub")
async def cb_unsub_settings(cb: CallbackQuery):
    await cb.answer("Вы отписались")
    set_consent(ensure_user(cb.from_user)["user_id"], False)
    await refresh_settings(cb)


@router.callback_query(F.data == "s:phone")
async def cb_phone(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(PHONE_ASK, reply_markup=phone_kb())


@router.callback_query(F.data == "s:del")
async def cb_delete_ask(cb: CallbackQuery):
    await cb.answer()
    await safe_edit(
        cb,
        "🗑 <b>Удалить все ваши данные?</b>\nМы сотрём профиль, согласие и номер телефона. Это нельзя отменить.",
        kb([[("Да, удалить", "s:del:yes")], [("Нет, оставить", "s:del:no")]]),
    )


@router.callback_query(F.data == "s:del:no")
async def cb_delete_no(cb: CallbackQuery):
    await cb.answer("Ничего не удалено")
    await refresh_settings(cb)


@router.callback_query(F.data == "s:del:yes")
async def cb_delete_yes(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    delete_user(cb.from_user.id)
    await safe_edit(cb, "🗑 <b>Данные удалены.</b>")
    await cb.message.answer("Если захотите вернуться, нажмите /start.", reply_markup=ReplyKeyboardRemove())


# ---------- админ-панель ----------
async def admin_guard(cb: CallbackQuery):
    if is_admin(cb.from_user.id):
        return True
    await cb.answer("Недоступно", show_alert=True)
    return False


@router.callback_query(F.data == "a:stats")
async def cb_stats(cb: CallbackQuery):
    if not await admin_guard(cb):
        return
    await cb.answer()
    await cb.message.answer(stats_text(), reply_markup=admin_kb())


@router.callback_query(F.data == "a:links")
async def cb_links(cb: CallbackQuery):
    if not await admin_guard(cb):
        return
    await cb.answer()
    me = await cb.bot.me()
    lines = [f"🔗 <b>Ссылки-источники</b>\n{DIV}"]
    for tag, (emoji, name, _, _) in SOURCES.items():
        if tag == "direct":
            continue
        lines.append(f"{emoji} {name}\n<code>https://t.me/{me.username}?start={tag}</code>")
    lines.append("Своя метка: <code>?start=любая_метка</code> (латиница, цифры, _). В статистике появится сама.")
    await cb.message.answer("\n\n".join(lines), reply_markup=admin_kb())


@router.callback_query(F.data == "a:csv")
async def cb_csv(cb: CallbackQuery):
    if not await admin_guard(cb):
        return
    await cb.answer()
    data = export_csv()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    await cb.message.answer_document(
        BufferedInputFile(data, filename=f"subscribers_{stamp}.csv"),
        caption="📥 Выгрузка пользователей",
    )


@router.callback_query(F.data == "a:bc")
async def cb_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    await cb.answer()
    counts = audience_counts()
    total = sum(n for _, n in counts)
    if total == 0:
        await cb.message.answer("Пока нет подписчиков, которым можно отправить рассылку.", reply_markup=admin_kb())
        return
    rows = [[(f"👥 Все подписчики ({total})", "bc:a:all")]]
    for tag, n in counts:
        emoji, name, _, _ = src_meta(tag)
        rows.append([(f"{emoji} {name} ({n})", f"bc:a:s:{tag}")])
    rows.append([("✖️ Отмена", "bc:x")])
    await state.clear()
    await cb.message.answer("📤 <b>Рассылка</b>\nКому отправляем? Получат только согласившиеся.", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("bc:a:"))
async def cb_broadcast_audience(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    await cb.answer()
    arg = cb.data[len("bc:a:"):]
    audience = "all" if arg == "all" else arg.removeprefix("s:")
    await state.set_state(Broadcast.content)
    await state.update_data(audience=audience)
    await safe_edit(cb, "📤 <b>Рассылка</b>\nАудитория выбрана ✅")
    await cb.message.answer(
        "✍️ Отправьте текст сообщения или <b>фото с подписью</b>.\n"
        "Форматирование (жирный, курсив, ссылки) сохранится. Отмена: /cancel"
    )


@router.callback_query(F.data == "bc:x")
async def cb_broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    await cb.answer("Отменено")
    await state.clear()
    await cb.message.answer("Рассылка отменена.", reply_markup=admin_kb())


async def send_one(bot, uid, text, photo):
    if photo:
        await bot.send_photo(uid, photo, caption=text or None, reply_markup=unsub_kb())
    else:
        await bot.send_message(uid, text, reply_markup=unsub_kb())


async def run_broadcast(bot, admin_chat, bid, ids, text, photo):
    sent = failed = 0
    for uid in ids:
        try:
            await send_one(bot, uid, text, photo)
            sent += 1
        except TelegramRetryAfter as e:  # упёрлись в лимит Telegram: ждём и пробуем ещё раз
            await asyncio.sleep(e.retry_after + 1)
            try:
                await send_one(bot, uid, text, photo)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:  # человек заблокировал бота
            mark_blocked(uid, True)
            failed += 1
        except Exception:
            logging.exception("Не удалось отправить пользователю %s", uid)
            failed += 1
        await asyncio.sleep(0.05)  # ≈20 сообщений в секунду, ниже лимита Telegram
    finish_broadcast(bid, sent, failed)
    await bot.send_message(
        admin_chat,
        f"✅ <b>Рассылка завершена</b>\n{DIV}\n📨 Доставлено: <b>{sent}</b>\n⚠️ Не доставлено: <b>{failed}</b>",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "bc:go")
async def cb_broadcast_go(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    await cb.answer()
    data = await state.get_data()
    if "audience" not in data or ("text" not in data and "photo" not in data):
        await state.clear()
        await cb.message.answer("Данные рассылки потерялись, начните заново.", reply_markup=admin_kb())
        return
    ids = recipients(data["audience"])
    text, photo = data.get("text", ""), data.get("photo")
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if not ids:
        await cb.message.answer("Получателей нет, рассылка не отправлена.", reply_markup=admin_kb())
        return
    bid = add_broadcast(data["audience"], text, bool(photo))
    await cb.message.answer(f"🚀 Рассылка запущена: {len(ids)} получателей. Сообщу, когда закончу.")
    task = asyncio.create_task(run_broadcast(cb.bot, cb.message.chat.id, bid, ids, text, photo))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


# ---------- блокировка бота пользователем ----------
@router.my_chat_member()
async def on_member_update(event: ChatMemberUpdated):
    if event.chat.type != "private":
        return
    status = event.new_chat_member.status
    if status == "kicked":
        mark_blocked(event.from_user.id, True)
    elif status == "member":
        mark_blocked(event.from_user.id, False)


@router.message()
async def fallback(message: Message):
    await message.answer("Воспользуйтесь меню ниже 👇", reply_markup=menu_kb(is_admin(message.from_user.id)))


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    if ADMIN_ID is None:
        logging.warning("ADMIN_ID не задан: админ-панель отключена")
    init_db()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
