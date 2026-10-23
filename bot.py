"""Семейный бот-помощник по задачам для Telegram.

Работает на python-telegram-bot (v21+) и SQLite. Внутри нет ИИ: бот реагирует
только на команды, кнопки и ответы на свои сообщения, в обычную переписку
группы не вмешивается.
"""
import calendar
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, time as dtime
from html import escape
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup as Markup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("family-bot")

# ----------------------------------------------------------------- настройки
TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "family_tasks.db")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "9"))  # утренняя сводка
# Необязательно: список id чатов через запятую, где бот имеет право работать
ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}
HTML = ParseMode.HTML

# ------------------------------------------------------------------ база
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE IF NOT EXISTS members (
    chat_id INTEGER, user_id INTEGER, name TEXT,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open | taken | done
    priority INTEGER NOT NULL DEFAULT 0,      -- 0 обычная, 1 срочная
    due_ts INTEGER,
    assignee_id INTEGER, assignee_name TEXT,
    creator_id INTEGER, creator_name TEXT,
    created_ts INTEGER, taken_ts INTEGER,
    done_ts INTEGER, done_by_id INTEGER, done_by_name TEXT,
    repeat TEXT NOT NULL DEFAULT 'none',      -- none | daily | weekly | monthly
    soon_sent INTEGER NOT NULL DEFAULT 0,
    overdue_sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks (chat_id, status);
"""
)


def fetch(sql, args=()):
    return db.execute(sql, args).fetchall()


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def get_task(tid):
    r = fetch("SELECT * FROM tasks WHERE id=?", (tid,))
    return r[0] if r else None


# --------------------------------------------------------------- утилиты
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
REPEAT_NAMES = {
    "none": "без повтора",
    "daily": "каждый день",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
}


def now_ts():
    return int(time.time())


def at(dt, hour):
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def fmt(ts):
    d = datetime.fromtimestamp(ts, TZ)
    return f"{WD[d.weekday()]} {d:%d.%m %H:%M}"


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{escape(name or "—")}</a>'


def plural(n, forms):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return forms[1]
    return forms[2]


def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def add_month(d):
    y, m = (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def next_due(due_ts, repeat):
    """Срок следующего повтора (строго в будущем)."""
    now = datetime.now(TZ)
    d = datetime.fromtimestamp(due_ts, TZ) if due_ts else at(now, 18)
    step = {
        "daily": lambda x: x + timedelta(days=1),
        "weekly": lambda x: x + timedelta(days=7),
        "monthly": add_month,
    }[repeat]
    d = step(d)
    while d <= now:
        d = step(d)
    return int(d.timestamp())


def preset_due(key):
    now = datetime.now(TZ)
    if key == "2h":
        d = now + timedelta(hours=2)
    elif key == "eve":
        d = at(now, 20)
        if d <= now:
            d += timedelta(days=1)
    elif key == "tom":
        d = at(now + timedelta(days=1), 18)
    elif key == "3d":
        d = at(now + timedelta(days=3), 18)
    else:  # "7d"
        d = at(now + timedelta(days=7), 18)
    return int(d.timestamp())


def parse_due(text):
    """'25.10 18:30', '25.10', '25.10.2026 9:00', '19:00' -> timestamp или None."""
    s = text.strip().lower()
    now = datetime.now(TZ)
    m = re.fullmatch(
        r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?:\s+(\d{1,2})[:.](\d{2}))?", s
    )
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        hh = int(m.group(4)) if m.group(4) else 18
        mm = int(m.group(5)) if m.group(5) else 0
        try:
            d = datetime(year, mon, day, hh, mm, tzinfo=TZ)
            if not m.group(3) and d < now - timedelta(days=1):
                d = d.replace(year=year + 1)
            return int(d.timestamp())
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
    if m:
        try:
            d = now.replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
            )
        except ValueError:
            return None
        if d <= now:
            d += timedelta(days=1)
        return int(d.timestamp())
    return None


async def edit(q, text, markup=None):
    try:
        await q.edit_message_text(
            text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def say(context, chat_id, text, markup=None):
    try:
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode=HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        log.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)


# ----------------------------------------------------------------- задачи
def create_task(
    chat_id,
    creator_id,
    creator_name,
    title,
    priority=0,
    due=None,
    repeat="none",
    assignee_id=None,
    assignee_name=None,
):
    ts = now_ts()
    if repeat != "none" and due is None:  # повтору нужна точка отсчёта
        d = at(datetime.now(TZ), 18)
        if int(d.timestamp()) <= ts:
            d += timedelta(days=1)
        due = int(d.timestamp())
    soon = 1 if due and due - ts <= 3600 else 0
    over = 1 if due and due <= ts else 0
    status = "taken" if assignee_id else "open"
    return run(
        """INSERT INTO tasks (chat_id, title, status, priority, due_ts, assignee_id,
        assignee_name, creator_id, creator_name, created_ts, taken_ts, repeat,
        soon_sent, overdue_sent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            chat_id, title, status, priority, due, assignee_id, assignee_name,
            creator_id, creator_name, ts, ts if assignee_id else None, repeat,
            soon, over,
        ),
    )


def sort_key(t):
    return (t["due_ts"] is None, t["due_ts"] or 0, -t["priority"], t["id"])


def open_tasks(chat_id, user_id=None):
    sql = "SELECT * FROM tasks WHERE chat_id=? AND status IN ('open','taken')"
    args = [chat_id]
    if user_id:
        sql += " AND assignee_id=?"
        args.append(user_id)
    return sorted(fetch(sql, args), key=sort_key)


def card_text(t):
    lines = [f"<b>#{t['id']} {escape(t['title'])}</b>"]
    if t["status"] == "done":
        lines.append(f"✅ Выполнил(а): {escape(t['done_by_name'] or '—')}, {fmt(t['done_ts'])}")
    elif t["status"] == "taken":
        lines.append(f"🙋 Взял(а): {mention(t['assignee_id'], t['assignee_name'])}")
    else:
        lines.append("🆓 Свободна, кто возьмёт?")
    if t["due_ts"]:
        late = t["status"] != "done" and t["due_ts"] < now_ts()
        lines.append(f"{'⚠️ Просрочено, срок' if late else '⏰ Срок'}: {fmt(t['due_ts'])}")
    if t["priority"]:
        lines.append("🔥 Срочно")
    if t["repeat"] != "none":
        lines.append(f"🔁 Повтор: {REPEAT_NAMES[t['repeat']]}")
    lines.append(f"<i>Добавил(а): {escape(t['creator_name'] or '—')}, {fmt(t['created_ts'])}</i>")
    return "\n".join(lines)


def card_markup(t):
    i = t["id"]
    rows = []
    if t["status"] == "open":
        rows.append([B("✋ Беру", f"t:{i}:take"), B("✅ Сделано", f"t:{i}:done")])
    elif t["status"] == "taken":
        rows.append([B("✅ Сделано", f"t:{i}:done"), B("↩️ Отказаться", f"t:{i}:release")])
        rows.append([B("✋ Забрать себе", f"t:{i}:take")])
    if t["status"] != "done":
        rows.append([B("👤 Назначить", f"t:{i}:asklist")])
    rows.append([B("🗑 Удалить", f"t:{i}:del"), B("⬅️ К списку", "m:open")])
    return Markup(rows)


def btn_label(t, show_name=True):
    if t["due_ts"] and t["due_ts"] < now_ts():
        icon = "⚠️"
    elif t["priority"]:
        icon = "🔥"
    elif t["status"] == "taken":
        icon = "🙋"
    else:
        icon = "🆓"
    label = f"{icon} {t['title']}"
    if show_name and t["status"] == "taken" and t["assignee_name"]:
        label += f" — {t['assignee_name']}"
    if t["due_ts"]:
        label += f" · {fmt(t['due_ts'])}"
    return label if len(label) <= 60 else label[:59] + "…"


MENU_TEXT = "🏠 <b>Семейные задачи</b>\nВыберите действие:"
MENU = Markup(
    [
        [B("➕ Новая задача", "m:new")],
        [B("📋 Открытые", "m:open"), B("🙋 Мои", "m:my")],
        [B("✅ За неделю", "m:week"), B("🏆 Рейтинг", "m:top")],
    ]
)
BACK_ROW = [B("⬅️ Меню", "m:menu")]


def build_list(chat_id, user_id=None):
    tasks = open_tasks(chat_id, user_id)
    head = "🙋 <b>Мои задачи</b>" if user_id else "📋 <b>Открытые задачи</b>"
    if not tasks:
        text = head + "\n\nПока пусто 🎉"
    else:
        text = (
            f"{head} ({len(tasks)})\n"
            "🆓 свободна · 🙋 в работе · 🔥 срочно · ⚠️ просрочена\n"
            "Нажмите на задачу, чтобы взять её или отметить выполненной."
        )
    rows = [[B(btn_label(t, show_name=not user_id), f"t:{t['id']}")] for t in tasks[:30]]
    if len(tasks) > 30:
        text += f"\n…показаны первые 30 из {len(tasks)}"
    rows.append([B("➕ Новая задача", "m:new"), BACK_ROW[0]])
    return text, Markup(rows)


def build_week(chat_id):
    since = now_ts() - 7 * 86400
    done = fetch(
        "SELECT * FROM tasks WHERE chat_id=? AND status='done' AND done_ts>=? "
        "ORDER BY done_ts DESC LIMIT 40",
        (chat_id, since),
    )
    lines = ["✅ <b>Сделано за 7 дней</b>", ""]
    if not done:
        lines.append("Пока ничего не выполнено.")
    for t in done:
        lines.append(
            f"• {escape(t['title'])}: <b>{escape(t['done_by_name'] or '—')}</b>, {fmt(t['done_ts'])}"
        )
    return "\n".join(lines), Markup([BACK_ROW])


def build_top(chat_id):
    start = datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    res = fetch(
        "SELECT done_by_id, MAX(done_by_name) AS name, COUNT(*) AS c FROM tasks "
        "WHERE chat_id=? AND status='done' AND done_ts>=? "
        "GROUP BY done_by_id ORDER BY c DESC",
        (chat_id, int(start.timestamp())),
    )
    lines = ["🏆 <b>Рейтинг месяца</b>", ""]
    if not res:
        lines.append("В этом месяце задач пока не выполнено.")
    medals = ["🥇", "🥈", "🥉"]
    for n, r in enumerate(res):
        mark = medals[n] if n < 3 else "▫️"
        lines.append(
            f"{mark} {escape(r['name'] or '—')}: {r['c']} {plural(r['c'], ('задача', 'задачи', 'задач'))}"
        )
    return "\n".join(lines), Markup([BACK_ROW])


def build_digest(chat_id):
    tasks = open_tasks(chat_id)
    if not tasks:
        return None
    now = now_ts()
    end = int(datetime.now(TZ).replace(hour=23, minute=59, second=59).timestamp())
    overdue = [t for t in tasks if t["due_ts"] and t["due_ts"] < now]
    today = [t for t in tasks if t["due_ts"] and now <= t["due_ts"] <= end]
    seen = {t["id"] for t in overdue + today}
    free = [t for t in tasks if t["id"] not in seen and t["status"] == "open"]
    busy = [t for t in tasks if t["id"] not in seen and t["status"] == "taken"]

    def line(t):
        who = mention(t["assignee_id"], t["assignee_name"]) if t["status"] == "taken" else "свободна"
        due = f" · {fmt(t['due_ts'])}" if t["due_ts"] else ""
        fire = "🔥 " if t["priority"] else ""
        return f"• {fire}{escape(t['title'])}: {who}{due}"

    parts = ["☀️ <b>Доброе утро! Сводка по семейным задачам</b>"]
    for head, items in (
        ("⚠️ <b>Просрочено</b>", overdue),
        ("📅 <b>На сегодня</b>", today),
        ("🆓 <b>Свободные, берите!</b>", free),
        ("🙋 <b>В работе</b>", busy),
    ):
        if items:
            body = [line(t) for t in items[:10]]
            if len(items) > 10:
                body.append(f"…и ещё {len(items) - 10}")
            parts.append(head + "\n" + "\n".join(body))
    return "\n\n".join(parts)


# -------------------------------------------------------- учёт участников
async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user or user.is_bot:
        return
    if ALLOWED and chat.id not in ALLOWED:
        raise ApplicationHandlerStop
    run(
        "INSERT INTO chats (chat_id, title) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
        (chat.id, chat.title or "личный чат"),
    )
    run(
        "INSERT INTO members (chat_id, user_id, name) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name",
        (chat.id, user.id, user.first_name or user.username or str(user.id)),
    )


def member_rows(chat_id, prefix):
    members = fetch("SELECT user_id, name FROM members WHERE chat_id=? ORDER BY name", (chat_id,))
    rows, row = [], []
    for m in members:
        row.append(B(f"👤 {m['name']}", f"{prefix}{m['user_id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


# --------------------------------------------------------------- команды
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я веду список семейных задач.\n\n"
        "• ➕ добавьте задачу, укажите срок и приоритет\n"
        "• ✋ любой может взять задачу себе, а ✅ отметить выполненной\n"
        "• ⏰ я напомню о сроках и каждое утро пришлю сводку\n\n"
        "В обычную переписку я не вмешиваюсь: отвечаю только на команды и кнопки.\n"
        "Чтобы я мог назначать задачи на вас, нажмите любую кнопку ниже."
    )
    await update.message.reply_text(text, reply_markup=MENU)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, parse_mode=HTML, reply_markup=MENU)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, mk = build_list(update.effective_chat.id)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=mk)


async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, mk = build_list(update.effective_chat.id, update.effective_user.id)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=mk)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, mk = build_week(update.effective_chat.id)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=mk)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, mk = build_top(update.effective_chat.id)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=mk)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID этого чата: {update.effective_chat.id}")


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
    chat_id = q.message.chat_id
    if action == "menu":
        await edit(q, MENU_TEXT, MENU)
    elif action == "open":
        await edit(q, *build_list(chat_id))
    elif action == "my":
        await edit(q, *build_list(chat_id, q.from_user.id))
    elif action == "week":
        await edit(q, *build_week(chat_id))
    elif action == "top":
        await edit(q, *build_top(chat_id))


# ------------------------------------------------------ действия с задачей
async def task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    tid = int(parts[1])
    act = parts[2] if len(parts) > 2 else "card"
    chat_id = q.message.chat_id
    t = get_task(tid)
    if not t or t["chat_id"] != chat_id:
        await q.answer("Эта задача уже удалена", show_alert=True)
        return
    user = q.from_user
    uname = user.first_name or user.username or "Кто-то"
    me = mention(user.id, uname)
    title = escape(t["title"])

    if act == "card":
        await q.answer()
        await edit(q, card_text(t), card_markup(t))
        return

    if act == "asklist":
        await q.answer()
        rows = member_rows(chat_id, f"t:{tid}:as:")
        rows.append([B("⬅️ Назад", f"t:{tid}")])
        await edit(q, f"Кому назначить «{title}»?", Markup(rows))
        return

    if act in ("take", "done", "release", "as") and t["status"] == "done":
        await q.answer("Эта задача уже выполнена ✅", show_alert=True)
        return

    if act == "take":
        if t["assignee_id"] == user.id:
            await q.answer("Эта задача уже у вас")
            return
        prev = t["assignee_name"]
        run(
            "UPDATE tasks SET status='taken', assignee_id=?, assignee_name=?, taken_ts=? WHERE id=?",
            (user.id, uname, now_ts(), tid),
        )
        await q.answer("Задача ваша!")
        text = f"✋ {me} взял(а) задачу «{title}»"
        if prev:
            text += f" (раньше: {escape(prev)})"
        await say(context, chat_id, text)

    elif act == "release":
        if t["assignee_id"] != user.id:
            await q.answer("Отказаться может только исполнитель", show_alert=True)
            return
        run(
            "UPDATE tasks SET status='open', assignee_id=NULL, assignee_name=NULL, taken_ts=NULL WHERE id=?",
            (tid,),
        )
        await q.answer("Готово")
        await say(context, chat_id, f"↩️ {me} отказался(ась) от задачи «{title}». Она снова свободна.")

    elif act == "done":
        ts = now_ts()
        run(
            "UPDATE tasks SET status='done', done_ts=?, done_by_id=?, done_by_name=? WHERE id=?",
            (ts, user.id, uname, tid),
        )
        await q.answer("Отлично! ✅")
        text = f"✅ {me} выполнил(а) задачу «{title}»\n🕒 {fmt(ts)}"
        if t["assignee_id"] and t["assignee_id"] != user.id:
            text += f"\n(была на {escape(t['assignee_name'] or '')})"
        if t["repeat"] != "none":
            due = next_due(t["due_ts"], t["repeat"])
            create_task(
                chat_id, t["creator_id"], t["creator_name"], t["title"],
                t["priority"], due, t["repeat"],
            )
            text += f"\n🔁 Следующая: {fmt(due)}"
        await say(context, chat_id, text)

    elif act == "as":
        uid = int(parts[3])
        m = fetch("SELECT name FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid))
        if not m:
            await q.answer("Не знаю такого участника", show_alert=True)
            return
        run(
            "UPDATE tasks SET status='taken', assignee_id=?, assignee_name=?, taken_ts=? WHERE id=?",
            (uid, m[0]["name"], now_ts(), tid),
        )
        await q.answer("Назначено")
        await say(context, chat_id, f"👉 {me} назначил(а) задачу «{title}» на {mention(uid, m[0]['name'])}")

    elif act == "del":
        if user.id != t["creator_id"]:
            await q.answer(
                f"Удалить может только тот, кто добавил задачу ({t['creator_name']})",
                show_alert=True,
            )
            return
        await q.answer()
        await edit(
            q,
            f"Удалить задачу «{title}»?",
            Markup([[B("🗑 Да, удалить", f"t:{tid}:delok"), B("Нет", f"t:{tid}")]]),
        )
        return

    elif act == "delok":
        if user.id != t["creator_id"]:
            await q.answer("Удалить может только автор задачи", show_alert=True)
            return
        run("DELETE FROM tasks WHERE id=?", (tid,))
        await q.answer("Удалено")
        await edit(q, "🗑 Задача удалена.", Markup([[B("⬅️ К списку", "m:open")]]))
        await say(context, chat_id, f"🗑 {me} удалил(а) задачу «{title}»")
        return

    t = get_task(tid)
    await edit(q, card_text(t), card_markup(t))


# ------------------------------------------------------- создание задачи
TITLE, DUE, CUSTOM, PRIO, ASSIGN, REPEAT = range(6)


def due_kb():
    return Markup(
        [
            [B("⏱ Через 2 часа", "n:due:2h"), B("🌙 Сегодня 20:00", "n:due:eve")],
            [B("🌅 Завтра 18:00", "n:due:tom"), B("📆 Через 3 дня", "n:due:3d")],
            [B("🗓 Через неделю", "n:due:7d"), B("♾ Без срока", "n:due:none")],
            [B("✏️ Свой срок", "n:due:custom")],
            [B("❌ Отмена", "n:cancel")],
        ]
    )


PRIO_KB = Markup(
    [
        [B("Обычная", "n:pr:0"), B("🔥 Срочная", "n:pr:1")],
        [B("❌ Отмена", "n:cancel")],
    ]
)
REPEAT_KB = Markup(
    [
        [B("Один раз", "n:rp:none")],
        [B("🔁 Каждый день", "n:rp:daily"), B("🔁 Каждую неделю", "n:rp:weekly")],
        [B("🔁 Каждый месяц", "n:rp:monthly")],
        [B("❌ Отмена", "n:cancel")],
    ]
)


def is_my_reply(update, context):
    m = update.message
    if m.chat.type == "private":
        return True
    r = m.reply_to_message
    return bool(r and r.message_id == context.user_data.get("prompt_id"))


async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"] = {}
    if update.callback_query:
        await update.callback_query.answer()
        target = update.callback_query.message
    else:
        target = update.message
    sent = await target.reply_text(
        "✍️ Напишите, что нужно сделать.\n"
        "<i>Ответьте (Reply) на это сообщение, так я пойму, что текст для меня.</i>",
        parse_mode=HTML,
    )
    context.user_data["prompt_id"] = sent.message_id
    return TITLE


async def got_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_my_reply(update, context):
        return None  # обычная переписка, молчим
    context.user_data["new"] = {"title": update.message.text.strip()[:200]}
    await update.message.reply_text("⏰ Когда нужно сделать?", reply_markup=due_kb())
    return DUE


async def got_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":")[2]
    if key == "custom":
        await edit(
            q,
            "✏️ Напишите срок ответом на это сообщение.\n"
            "Примеры: <code>25.10 18:30</code>, <code>25.10</code>, <code>19:00</code>",
        )
        context.user_data["prompt_id"] = q.message.message_id
        return CUSTOM
    context.user_data["new"]["due"] = None if key == "none" else preset_due(key)
    await edit(q, "🔥 Насколько срочно?", PRIO_KB)
    return PRIO


async def got_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_my_reply(update, context):
        return None
    ts = parse_due(update.message.text)
    if ts is None:
        await update.message.reply_text(
            "Не понял срок 🤔 Попробуйте так: 25.10 18:30, 25.10 или 19:00 (ответом на моё сообщение)."
        )
        return None
    context.user_data["new"]["due"] = ts
    await update.message.reply_text("🔥 Насколько срочно?", reply_markup=PRIO_KB)
    return PRIO


async def got_prio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["new"]["priority"] = int(q.data.split(":")[2])
    rows = [[B("🆓 Пусть возьмёт кто-нибудь", "n:as:0")]]
    rows += member_rows(q.message.chat_id, "n:as:")
    rows.append([B("❌ Отмена", "n:cancel")])
    await edit(q, "👤 Кому назначить?", Markup(rows))
    return ASSIGN


async def got_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = int(q.data.split(":")[2])
    d = context.user_data["new"]
    if uid:
        m = fetch("SELECT name FROM members WHERE chat_id=? AND user_id=?", (q.message.chat_id, uid))
        d["assignee_id"], d["assignee_name"] = uid, (m[0]["name"] if m else "—")
    await edit(q, "🔁 Повторять задачу?", REPEAT_KB)
    return REPEAT


async def got_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data.pop("new", None)
    if not d or "title" not in d:
        await edit(q, "Что-то пошло не так, начните заново: /new")
        return ConversationHandler.END
    user = q.from_user
    uname = user.first_name or user.username or "Кто-то"
    chat_id = q.message.chat_id
    tid = create_task(
        chat_id, user.id, uname, d["title"], d.get("priority", 0), d.get("due"),
        q.data.split(":")[2], d.get("assignee_id"), d.get("assignee_name"),
    )
    t = get_task(tid)
    await edit(q, f"🆕 <b>Новая задача от {escape(uname)}</b>\n\n{card_text(t)}", card_markup(t))
    if t["assignee_id"] and t["assignee_id"] != user.id:
        await say(
            context, chat_id,
            f"👉 {mention(t['assignee_id'], t['assignee_name'])}, тебе назначена задача «{escape(t['title'])}»",
        )
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new", None)
    if update.callback_query:
        await update.callback_query.answer()
        await edit(update.callback_query, "Отменено. Меню: /menu")
    else:
        await update.message.reply_text("Отменено. Меню: /menu")
    return ConversationHandler.END


async def foreign_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "Это меню неактивно или его открыл другой человек. Нажмите «➕ Новая задача».",
        show_alert=True,
    )


# ------------------------------------------------------------ напоминания
async def check_deadlines(context: ContextTypes.DEFAULT_TYPE):
    now = now_ts()
    due = fetch(
        "SELECT * FROM tasks WHERE status IN ('open','taken') AND due_ts IS NOT NULL AND "
        "((soon_sent=0 AND due_ts - ? <= 3600) OR (overdue_sent=0 AND due_ts <= ?))",
        (now, now),
    )
    for t in due:
        who = (
            mention(t["assignee_id"], t["assignee_name"])
            if t["status"] == "taken"
            else "Задача свободна, кто возьмёт?"
        )
        title = escape(t["title"])
        if t["due_ts"] <= now:
            text = f"⚠️ <b>Срок вышел:</b> «{title}»\n{who}"
            run("UPDATE tasks SET soon_sent=1, overdue_sent=1 WHERE id=?", (t["id"],))
        else:
            mins = max(1, (t["due_ts"] - now) // 60)
            text = f"⏰ <b>Скоро срок</b> (через ~{mins} мин): «{title}»\n{who}"
            run("UPDATE tasks SET soon_sent=1 WHERE id=?", (t["id"],))
        await say(context, t["chat_id"], text, Markup([[B("Открыть задачу", f"t:{t['id']}")]]))


async def morning_digest(context: ContextTypes.DEFAULT_TYPE):
    for ch in fetch("SELECT chat_id FROM chats"):
        text = build_digest(ch["chat_id"])
        if text:
            await say(context, ch["chat_id"], text, Markup([[B("📋 Открыть список", "m:open")]]))


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("menu", "Меню задач"),
            BotCommand("new", "Новая задача"),
            BotCommand("tasks", "Открытые задачи"),
            BotCommand("my", "Мои задачи"),
            BotCommand("done", "Сделано за неделю"),
            BotCommand("top", "Рейтинг месяца"),
        ]
    )


def main():
    if not TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN (токен от @BotFather).")
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(TypeHandler(Update, track), group=-1)

    flow = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_start),
            CallbackQueryHandler(new_start, pattern=r"^m:new$"),
        ],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            DUE: [CallbackQueryHandler(got_due, pattern=r"^n:due:")],
            CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom)],
            PRIO: [CallbackQueryHandler(got_prio, pattern=r"^n:pr:")],
            ASSIGN: [CallbackQueryHandler(got_assign, pattern=r"^n:as:")],
            REPEAT: [CallbackQueryHandler(got_repeat, pattern=r"^n:rp:")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            CommandHandler("new", new_start),
            CallbackQueryHandler(cancel_flow, pattern=r"^n:cancel$"),
            CallbackQueryHandler(new_start, pattern=r"^m:new$"),
        ],
        conversation_timeout=900,
        allow_reentry=True,
    )
    app.add_handler(flow)
    app.add_handler(CallbackQueryHandler(foreign_flow, pattern=r"^n:"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^m:(menu|open|my|week|top)$"))
    app.add_handler(CallbackQueryHandler(task_cb, pattern=r"^t:"))

    for name, fn in (
        ("start", cmd_start), ("help", cmd_start), ("menu", cmd_menu),
        ("tasks", cmd_tasks), ("my", cmd_my), ("done", cmd_done),
        ("top", cmd_top), ("chatid", cmd_chatid),
    ):
        app.add_handler(CommandHandler(name, fn))

    app.job_queue.run_repeating(check_deadlines, interval=60, first=15)
    app.job_queue.run_daily(morning_digest, time=dtime(DIGEST_HOUR, 0, tzinfo=TZ))

    log.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
