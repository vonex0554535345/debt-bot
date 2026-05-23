#!/usr/bin/env python3
"""Debt tracking Telegram bot — inline keyboard UI, tenge currency."""

import os
import html as HTML
import math
import sqlite3
import logging
import asyncio

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kbd
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS: set[int] = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH   = os.getenv("DB_PATH", "debts.db")
PG        = 5

# Conversation states
ASK_AMOUNT, ASK_DESC, ASK_PARTIAL = range(3)
ADM_SEL_LENDER, ADM_ASK_DATE, ADM_ASK_AMT, ADM_ASK_DESC = range(3, 7)


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT NOT NULL,
                rating     REAL NOT NULL DEFAULT 5.0,
                joined_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS debts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                borrower_id  INTEGER NOT NULL REFERENCES users(user_id),
                lender_id    INTEGER NOT NULL REFERENCES users(user_id),
                amount       REAL    NOT NULL,
                paid_amount  REAL    NOT NULL DEFAULT 0,
                description  TEXT    NOT NULL DEFAULT 'Без описания',
                status       TEXT    NOT NULL DEFAULT 'pending',
                initiated_by TEXT    NOT NULL DEFAULT 'lender',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                confirmed_at TEXT,
                repaid_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_b ON debts(borrower_id);
            CREATE INDEX IF NOT EXISTS idx_l ON debts(lender_id);
        """)
        # Migrations for existing DBs
        for col, dflt in [("paid_amount", "0"), ("initiated_by", "'lender'")]:
            try:
                c.execute(f"ALTER TABLE debts ADD COLUMN {col} REAL NOT NULL DEFAULT {dflt}")
            except Exception:
                pass
        try:
            c.execute("ALTER TABLE debts ADD COLUMN initiated_by TEXT NOT NULL DEFAULT 'lender'")
        except Exception:
            pass


def upsert_user(uid, uname, fname):
    with get_db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)", (uid, uname, fname))
        c.execute("UPDATE users SET username=?,first_name=? WHERE user_id=?", (uname, fname, uid))


def get_user(uid):
    with get_db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


# ── HELPERS ───────────────────────────────────────────────────────────────────

def calc_rating(uid) -> float:
    with get_db() as c:
        r = c.execute("""
            SELECT
                COUNT(CASE WHEN status='repaid'               THEN 1 END) repaid,
                COUNT(CASE WHEN status='active'               THEN 1 END) active,
                COUNT(CASE WHEN status IN ('active','repaid') THEN 1 END) accepted,
                AVG(CASE WHEN status='repaid'
                         AND repaid_at IS NOT NULL AND confirmed_at IS NOT NULL
                    THEN CAST((julianday(repaid_at) - julianday(confirmed_at)) AS REAL)
                    END) avg_days
            FROM debts WHERE borrower_id=?
        """, (uid,)).fetchone()

    repaid   = r["repaid"]   or 0
    active   = r["active"]   or 0
    accepted = r["accepted"] or 0
    avg_days = r["avg_days"]

    if accepted == 0:
        return 5.0

    completion = repaid / accepted
    base = 1.0 + completion * 3.5

    if avg_days is None:   speed =  0.0
    elif avg_days <= 1:    speed =  0.5
    elif avg_days <= 7:    speed =  0.3
    elif avg_days <= 14:   speed =  0.1
    elif avg_days <= 30:   speed =  0.0
    elif avg_days <= 90:   speed = -0.3
    else:                  speed = -0.7

    burden = min((active / accepted) * 0.6, 0.6)
    raw    = max(1.0, min(5.0, base + speed - burden))
    K      = 3
    conf   = accepted / (accepted + K)
    return round(max(1.0, min(5.0, raw * conf + 5.0 * (1.0 - conf))), 1)


def rating_details(uid) -> dict:
    with get_db() as c:
        r = c.execute("""
            SELECT
                COUNT(CASE WHEN status='repaid'               THEN 1 END) repaid,
                COUNT(CASE WHEN status='active'               THEN 1 END) active,
                COUNT(CASE WHEN status IN ('active','repaid') THEN 1 END) accepted,
                AVG(CASE WHEN status='repaid'
                         AND repaid_at IS NOT NULL AND confirmed_at IS NOT NULL
                    THEN CAST((julianday(repaid_at) - julianday(confirmed_at)) AS REAL) END) avg_days
            FROM debts WHERE borrower_id=?
        """, (uid,)).fetchone()
    return {"repaid": r["repaid"] or 0, "active": r["active"] or 0,
            "accepted": r["accepted"] or 0, "avg_days": r["avg_days"]}


def refresh_rating(uid) -> float:
    v = calc_rating(uid)
    with get_db() as c:
        c.execute("UPDATE users SET rating=? WHERE user_id=?", (v, uid))
    return v


def stars(v) -> str:
    f = max(0, min(5, round(v)))
    return "★" * f + "☆" * (5 - f)


def tg(v: float) -> str:
    return f"{int(v):,}".replace(",", " ") + " ₸"


def e(text) -> str:
    return HTML.escape(str(text))


def display(row) -> str:
    return f"@{row['username']}" if row["username"] else row["first_name"]


def paginate(items, page, size=PG):
    pages = max(1, math.ceil(len(items) / size))
    page  = max(0, min(page, pages - 1))
    return items[page * size:(page + 1) * size], page, pages


def nav_row(page, pages, prefix):
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(Btn("◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(Btn(f"  {page + 1}/{pages}  ", callback_data="noop"))
    if page < pages - 1:
        row.append(Btn("▶️", callback_data=f"{prefix}:{page + 1}"))
    return row


def fmt_days(avg_days) -> str:
    if avg_days is None:
        return "—"
    if avg_days < 1:
        return "< суток"
    if avg_days < 7:
        return f"{avg_days:.1f} дн."
    return f"{avg_days:.0f} дн."


SEMOJI = {"pending": "🟡", "active": "🔴", "repaid": "✅", "cancelled": "⚫"}
STEXT  = {"pending": "Ожидает", "active": "Активен", "repaid": "Погашен", "cancelled": "Отменён"}


async def edit_msg(q, text, kb):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


# ── UI BUILDERS ───────────────────────────────────────────────────────────────

def build_menu(uid):
    rat = calc_rating(uid)
    u   = get_user(uid)
    with get_db() as c:
        s = c.execute("""
            SELECT
                COUNT(CASE WHEN status='active'  AND borrower_id=? THEN 1 END) ma,
                COUNT(CASE WHEN status='pending' AND borrower_id=? THEN 1 END) mp,
                COUNT(CASE WHEN status='active'  AND lender_id=?   THEN 1 END) oa,
                COUNT(CASE WHEN status='pending' AND lender_id=?   THEN 1 END) op,
                COALESCE(SUM(CASE WHEN status='active' AND borrower_id=? THEN amount - paid_amount END),0) ms,
                COALESCE(SUM(CASE WHEN status='active' AND lender_id=?   THEN amount - paid_amount END),0) os
            FROM debts
        """, (uid, uid, uid, uid, uid, uid)).fetchone()

    fname = e(u["first_name"]) if u else "—"
    my_n  = s["ma"] + s["mp"]
    ow_n  = s["oa"] + s["op"]

    lines = [
        "<b>💰 Бот учёта долгов</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>{fname}</b>   {stars(rat)} <b>{rat}</b>",
        "",
    ]
    if s["ma"]:  lines.append(f"🔴 Я должен: <b>{tg(s['ms'])}</b> ({s['ma']} шт.)")
    if s["mp"]:  lines.append(f"🟡 Ожидают моего ответа: <b>{s['mp']}</b>")
    if s["oa"]:  lines.append(f"💵 Мне должны: <b>{tg(s['os'])}</b> ({s['oa']} шт.)")
    if s["op"]:  lines.append(f"🟡 Мои запросы к другим: <b>{s['op']}</b>")
    if not any([s["ma"], s["mp"], s["oa"], s["op"]]):
        lines.append("✅ Нет активных долгов")

    ml = f"💸 Мои долги ({my_n})" if my_n else "💸 Мои долги"
    ol = f"💰 Мне должны ({ow_n})" if ow_n else "💰 Мне должны"

    rows = [
        [Btn(ml, callback_data="mydebt:0"), Btn(ol, callback_data="owed:0")],
        [Btn("➕ Дать в долг",    callback_data="newdebt"),
         Btn("🙏 Попросить в долг", callback_data="reqdebt")],
        [Btn("📊 Моя история",  callback_data=f"hist:{uid}"),
         Btn("🏆 Топ должников", callback_data="top:cur")],
    ]
    if uid in ADMIN_IDS:
        rows.append([Btn("⚙️ Панель администратора", callback_data="admin")])

    return "\n".join(lines), Kbd(rows)


def build_user_select(caller_uid, page, flow):
    """Show paginated list of users for debt creation or request."""
    with get_db() as c:
        all_u = c.execute(
            "SELECT * FROM users WHERE user_id != ? ORDER BY first_name", (caller_uid,)
        ).fetchall()

    if flow == "nd":
        title, subtitle = "➕ <b>Дать в долг</b>", "Выбери <b>должника</b>:"
    else:
        title, subtitle = "🙏 <b>Попросить в долг</b>", "Выбери у кого <b>попросить</b>:"

    if not all_u:
        return (
            f"{title}\n\n❌ Нет других пользователей.\nПопроси их написать /start боту.",
            Kbd([[Btn("◀️ Меню", callback_data="menu")]])
        )

    chunk, page, pages = paginate(list(all_u), page, 6)
    lines = [title, "━━━━━━━━━━━━━━━━━━━━", subtitle, ""]
    btns  = []

    for u in chunk:
        rat      = calc_rating(u["user_id"])
        raw_name = display(u)
        lines.append(f"• <b>{e(raw_name)}</b>  {stars(rat)} {rat}")
        btns.append([Btn(f"👤 {raw_name[:28]}", callback_data=f"sel_{flow}:{u['user_id']}")])

    nav = nav_row(page, pages, f"usersel_{flow}")
    if nav:
        btns.append(nav)
    btns.append([Btn("◀️ Меню", callback_data="menu")])
    return "\n".join(lines), Kbd(btns)


def build_mydebt(uid, page):
    """Debts where current user is the BORROWER."""
    with get_db() as c:
        all_ = c.execute("""
            SELECT d.*, lu.username lu, lu.first_name ln
            FROM debts d JOIN users lu ON d.lender_id = lu.user_id
            WHERE d.borrower_id = ? AND d.status IN ('pending','active')
            ORDER BY d.status = 'active' DESC, d.created_at DESC
        """, (uid,)).fetchall()

    if not all_:
        return (
            "💸 <b>Мои долги</b>\n\n✅ Нет активных долгов!",
            Kbd([[Btn("◀️ Меню", callback_data="menu")]])
        )

    chunk, page, pages = paginate(list(all_), page)
    total = sum(r["amount"] - r["paid_amount"] for r in all_ if r["status"] == "active")

    lines = ["💸 <b>Мои долги</b>\n"]
    btns  = []

    for r in chunk:
        lender_raw = display({"username": r["lu"], "first_name": r["ln"]})
        em   = SEMOJI[r["status"]]
        paid = r["paid_amount"]
        rem  = r["amount"] - paid

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{em} <b>#{r['id']}</b> — <b>{tg(r['amount'])}</b>")
        if paid > 0:
            lines.append(f"   💳 Оплачено: <b>{tg(paid)}</b> / Осталось: <b>{tg(rem)}</b>")
        lines.append(f"👤 {e(lender_raw)}   📅 {r['created_at'][:10]}")
        lines.append(f"📝 <i>{e(r['description'])}</i>")

        if r["status"] == "pending":
            if r["initiated_by"] == "lender":
                # Lender gave me a debt — I can accept or reject
                btns.append([
                    Btn(f"✅ Принять #{r['id']}",  callback_data=f"confirm:{r['id']}"),
                    Btn(f"❌ Отклонить #{r['id']}", callback_data=f"reject:{r['id']}"),
                ])
            else:
                # I requested debt from lender — waiting for their approval
                lines[-1] = lines[-1]  # keep desc
                lines.append(f"   <i>⏳ Ожидает одобрения кредитора</i>")
                btns.append([Btn(f"⚫ Отозвать запрос #{r['id']}", callback_data=f"breqcancel:{r['id']}")])
        else:  # active
            btns.append([
                Btn(f"💸 Полностью #{r['id']}", callback_data=f"repay:{r['id']}"),
                Btn(f"💳 Частями #{r['id']}",   callback_data=f"partpay:{r['id']}"),
            ])

    if total:
        lines += ["━━━━━━━━━━━━━━━━━━━━", f"💰 Итого осталось: <b>{tg(total)}</b>"]

    nav = nav_row(page, pages, "mydebt")
    if nav:
        btns.append(nav)
    btns.append([Btn("◀️ Меню", callback_data="menu")])
    return "\n".join(lines), Kbd(btns)


def build_owed(uid, page):
    """Debts where current user is the LENDER."""
    with get_db() as c:
        all_ = c.execute("""
            SELECT d.*, bu.username bu, bu.first_name bn
            FROM debts d JOIN users bu ON d.borrower_id = bu.user_id
            WHERE d.lender_id = ? AND d.status IN ('pending','active')
            ORDER BY d.status = 'active' DESC, d.created_at DESC
        """, (uid,)).fetchall()

    if not all_:
        return (
            "💰 <b>Мне должны</b>\n\n💤 Никто вам не должен.",
            Kbd([[Btn("◀️ Меню", callback_data="menu")]])
        )

    chunk, page, pages = paginate(list(all_), page)
    total = sum(r["amount"] - r["paid_amount"] for r in all_ if r["status"] == "active")

    lines = ["💰 <b>Мне должны</b>\n"]
    btns  = []

    for r in chunk:
        borrower_raw = display({"username": r["bu"], "first_name": r["bn"]})
        em   = SEMOJI[r["status"]]
        paid = r["paid_amount"]
        rem  = r["amount"] - paid

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{em} <b>#{r['id']}</b> — <b>{tg(r['amount'])}</b>")
        if paid > 0:
            lines.append(f"   💳 Получено: <b>{tg(paid)}</b> / Осталось: <b>{tg(rem)}</b>")
        lines.append(f"👤 {e(borrower_raw)}   📅 {r['created_at'][:10]}")
        lines.append(f"📝 <i>{e(r['description'])}</i>")

        if r["status"] == "pending":
            if r["initiated_by"] == "lender":
                # I gave debt, waiting for borrower to confirm
                lines.append(f"   <i>⏳ Ожидает подтверждения от {e(borrower_raw)}</i>")
                btns.append([Btn(f"⚫ Отозвать #{r['id']}", callback_data=f"dcancel:{r['id']}")])
            else:
                # Borrower requested from me — I approve or reject
                lines.append(f"   <i>🙏 {e(borrower_raw)} просит в долг</i>")
                btns.append([
                    Btn(f"✅ Одобрить #{r['id']}",  callback_data=f"lapprove:{r['id']}"),
                    Btn(f"❌ Отказать #{r['id']}",  callback_data=f"lreject:{r['id']}"),
                ])
        else:  # active
            btns.append([
                Btn(f"✅ Закрыть #{r['id']}",      callback_data=f"repaid:{r['id']}"),
                Btn(f"⚫ Аннулировать #{r['id']}", callback_data=f"dcancel:{r['id']}"),
            ])

    if total:
        lines += ["━━━━━━━━━━━━━━━━━━━━", f"💵 Итого ожидается: <b>{tg(total)}</b>"]

    nav = nav_row(page, pages, "owed")
    if nav:
        btns.append(nav)
    btns.append([Btn("◀️ Меню", callback_data="menu")])
    return "\n".join(lines), Kbd(btns)


def build_history(uid):
    u = get_user(uid)
    if not u:
        return "❌ Пользователь не найден.", Kbd([[Btn("◀️ Меню", callback_data="menu")]])

    with get_db() as c:
        agg = c.execute("""
            SELECT
                COUNT(CASE WHEN status IN ('active','repaid') THEN 1 END) t,
                COUNT(CASE WHEN status='repaid' THEN 1 END) rp,
                COUNT(CASE WHEN status='active' THEN 1 END) ac,
                COALESCE(SUM(CASE WHEN status IN ('active','repaid') THEN amount END),0) ts,
                COALESCE(SUM(CASE WHEN status='repaid' THEN amount END),0) rs,
                COALESCE(SUM(CASE WHEN status='active' THEN amount - paid_amount END),0) as_
            FROM debts WHERE borrower_id=?
        """, (uid,)).fetchone()
        recent = c.execute("""
            SELECT d.*, lu.username lu, lu.first_name ln
            FROM debts d JOIN users lu ON d.lender_id = lu.user_id
            WHERE d.borrower_id = ? AND d.status != 'cancelled'
            ORDER BY d.created_at DESC LIMIT 12
        """, (uid,)).fetchall()

    det = rating_details(uid)
    rat = calc_rating(uid)
    t   = agg["t"] or 0
    pct = int(agg["rp"] / t * 100) if t else 100

    badge = (
        "🏆 Идеальная" if rat >= 4.5 else
        "👍 Хорошая"   if rat >= 3.5 else
        "⚠️ Средняя"   if rat >= 2.5 else
        "⛔ Плохая"    if rat >= 1.5 else "🚫 Критически плохая"
    )

    lines = [
        f"📊 <b>Кредитная история: {e(display(u))}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{stars(rat)} <b>{rat} / 5.0</b>  {badge}",
        f"📈 Процент возврата: <b>{pct}%</b>",
        f"⏱ Среднее время возврата: <b>{fmt_days(det['avg_days'])}</b>",
        "",
        f"💰 Взято всего:   <b>{tg(agg['ts'])}</b>",
        f"✅ Возвращено:    <b>{tg(agg['rs'])}</b>",
        f"🔴 Активный долг: <b>{tg(agg['as_'])}</b> ({agg['ac']} шт.)",
        "",
        "<b>— Последние операции —</b>",
    ]
    for r in recent:
        lr     = display({"username": r["lu"], "first_name": r["ln"]})
        paid   = r["paid_amount"]
        suffix = f" (оплач. {tg(paid)})" if paid > 0 and r["status"] == "active" else ""
        lines.append(f"{SEMOJI[r['status']]} #{r['id']} {tg(r['amount'])}{suffix} ← {e(lr)} ({r['created_at'][:10]})")

    return "\n".join(lines), Kbd([[Btn("◀️ Меню", callback_data="menu")]])


def build_user_profile(uid):
    u = get_user(uid)
    if not u:
        return "❌ Пользователь не найден.", Kbd([[Btn("◀️ Топ", callback_data="top:cur")]])

    det  = rating_details(uid)
    rat  = calc_rating(uid)
    pct  = int(det["repaid"] / det["accepted"] * 100) if det["accepted"] else 100

    with get_db() as c:
        owed_sum = c.execute(
            "SELECT COALESCE(SUM(amount - paid_amount),0) FROM debts WHERE borrower_id=? AND status='active'",
            (uid,)
        ).fetchone()[0]

    badge = (
        "🏆 Идеальная" if rat >= 4.5 else
        "👍 Хорошая"   if rat >= 3.5 else
        "⚠️ Средняя"   if rat >= 2.5 else
        "⛔ Плохая"    if rat >= 1.5 else "🚫 Критически плохая"
    )

    lines = [
        f"👤 <b>{e(display(u))}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{stars(rat)} <b>{rat} / 5.0</b>  {badge}",
        "",
        f"📊 Взял долгов:  <b>{det['accepted']}</b>",
        f"✅ Погашено:     <b>{det['repaid']}</b> ({pct}%)",
        f"🔴 Активных:     <b>{det['active']}</b>",
        f"💰 Активный долг: <b>{tg(owed_sum)}</b>",
        f"⏱ Среднее время возврата: <b>{fmt_days(det['avg_days'])}</b>",
        f"📅 В боте с: <b>{u['joined_at'][:10]}</b>",
    ]
    return "\n".join(lines), Kbd([
        [Btn("📋 История", callback_data=f"hist:{uid}")],
        [Btn("◀️ Топ", callback_data="top:cur"), Btn("◀️ Меню", callback_data="menu")],
    ])


def build_top(tab):
    with get_db() as c:
        if tab == "cur":
            rows = c.execute("""
                SELECT u.user_id, u.username, u.first_name,
                       SUM(d.amount - d.paid_amount) total, COUNT(d.id) cnt
                FROM debts d JOIN users u ON d.borrower_id = u.user_id
                WHERE d.status = 'active'
                GROUP BY u.user_id ORDER BY total DESC LIMIT 10
            """).fetchall()
        else:
            rows = c.execute("""
                SELECT u.user_id, u.username, u.first_name,
                       SUM(d.amount) total, COUNT(d.id) cnt
                FROM debts d JOIN users u ON d.borrower_id = u.user_id
                WHERE d.status IN ('active','repaid')
                GROUP BY u.user_id ORDER BY total DESC LIMIT 10
            """).fetchall()

    MEDALS = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    title  = "🔴 Активные долги сейчас" if tab == "cur" else "📈 За всё время"
    lines  = ["🏆 <b>Топ должников</b>", f"<i>{title}</i>", "━━━━━━━━━━━━━━━━━━━━"]
    btns   = [[Btn("🔴 Сейчас", callback_data="top:cur"),
               Btn("📈 За все времена", callback_data="top:all")]]

    if rows:
        for i, r in enumerate(rows):
            rat = calc_rating(r["user_id"])
            raw = display(r)
            lines.append(f"{MEDALS[i]} {e(raw)} — <b>{tg(r['total'])}</b> ({r['cnt']} шт.) {stars(rat)}{rat}")
            btns.append([Btn(f"👤 {raw[:28]}", callback_data=f"profile:{r['user_id']}")])
    else:
        lines.append("<i>Нет данных</i>")

    btns.append([Btn("◀️ Меню", callback_data="menu")])
    return "\n".join(lines), Kbd(btns)


def build_adm_sel_user(exclude_uid, page, cb_prefix, nav_prefix, title, subtitle):
    """Paginated user list for admin debt creation."""
    with get_db() as c:
        all_u = c.execute(
            "SELECT * FROM users WHERE user_id != ? ORDER BY first_name", (exclude_uid,)
        ).fetchall()

    if not all_u:
        return (
            f"{title}\n\n❌ Нет других пользователей.",
            Kbd([[Btn("◀️ Отмена", callback_data="adm_conv_cancel")]])
        )

    chunk, page, pages = paginate(list(all_u), page, 6)
    lines = [title, "━━━━━━━━━━━━━━━━━━━━", subtitle, ""]
    btns  = []

    for u in chunk:
        rat = calc_rating(u["user_id"])
        raw = display(u)
        lines.append(f"• <b>{e(raw)}</b>  {stars(rat)} {rat}")
        btns.append([Btn(f"👤 {raw[:28]}", callback_data=f"{cb_prefix}:{u['user_id']}")])

    nav = nav_row(page, pages, nav_prefix)
    if nav:
        btns.append(nav)
    btns.append([Btn("❌ Отмена", callback_data="adm_conv_cancel")])
    return "\n".join(lines), Kbd(btns)


def build_admin():
    with get_db() as c:
        g = c.execute("""
            SELECT (SELECT COUNT(*) FROM users) users,
                   COUNT(CASE WHEN status='active'  THEN 1 END) active,
                   COUNT(CASE WHEN status='pending' THEN 1 END) pending,
                   COUNT(CASE WHEN status='repaid'  THEN 1 END) repaid,
                   COALESCE(SUM(CASE WHEN status='active' THEN amount - paid_amount END),0) asum
            FROM debts
        """).fetchone()

    text = (
        "⚙️ <b>Панель администратора</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: <b>{g['users']}</b>\n"
        f"🔴 Активных долгов: <b>{g['active']}</b> ({tg(g['asum'])})\n"
        f"🟡 Ожидают: <b>{g['pending']}</b>\n"
        f"✅ Погашено всего: <b>{g['repaid']}</b>"
    )
    return text, Kbd([
        [Btn("👥 Пользователи",   callback_data="adm_u:0"),
         Btn("📋 Активные долги", callback_data="adm_d:0")],
        [Btn("➕ Начислить долг", callback_data="adm_newdebt")],
        [Btn("◀️ Меню", callback_data="menu")],
    ])


def build_adm_users(page):
    with get_db() as c:
        all_u = c.execute("""
            SELECT u.*,
                   COUNT(CASE WHEN d.status='active' THEN 1 END) ac,
                   COALESCE(SUM(CASE WHEN d.status='active' THEN d.amount - d.paid_amount END),0) asum
            FROM users u LEFT JOIN debts d ON u.user_id = d.borrower_id
            GROUP BY u.user_id ORDER BY asum DESC
        """).fetchall()

    chunk, page, pages = paginate(list(all_u), page, 6)
    lines = [f"👥 <b>Пользователи</b> — {page+1}/{pages}\n"]
    btns  = []

    for u in chunk:
        rat = calc_rating(u["user_id"])
        raw = display(u)
        lines.append(f"• <b>{e(raw)}</b> (<code>{u['user_id']}</code>)\n  {stars(rat)}{rat}   🔴 {u['ac']} / {tg(u['asum'])}")
        btns.append([Btn(f"📋 {raw[:22]}", callback_data=f"adm_ud:{u['user_id']}:0")])

    nav = nav_row(page, pages, "adm_u")
    if nav:
        btns.append(nav)
    btns.append([Btn("◀️ Админ", callback_data="admin")])
    return "\n".join(lines), Kbd(btns)


def build_adm_debts(page):
    with get_db() as c:
        all_d = c.execute("""
            SELECT d.*, bu.username bu, bu.first_name bn, lu.username lu, lu.first_name ln
            FROM debts d
            JOIN users bu ON d.borrower_id = bu.user_id
            JOIN users lu ON d.lender_id   = lu.user_id
            WHERE d.status = 'active' ORDER BY d.created_at DESC
        """).fetchall()

    if not all_d:
        return ("📋 <b>Активные долги</b>\n\n<i>Нет активных долгов</i>",
                Kbd([[Btn("◀️ Админ", callback_data="admin")]]))

    chunk, page, pages = paginate(list(all_d), page)
    lines = [f"📋 <b>Активные долги</b> — {page+1}/{pages}\n"]
    btns  = []

    for r in chunk:
        br  = display({"username": r["bu"], "first_name": r["bn"]})
        lr  = display({"username": r["lu"], "first_name": r["ln"]})
        rem = r["amount"] - r["paid_amount"]
        lines.append(f"🔴 <b>#{r['id']}</b>  {tg(rem)}/{tg(r['amount'])}\n   {e(br)} → {e(lr)}  ({r['created_at'][:10]})")
        btns.append([Btn(f"🔍 #{r['id']} — {br[:16]}", callback_data=f"adm_di:{r['id']}")])

    nav = nav_row(page, pages, "adm_d")
    if nav:
        btns.append(nav)
    btns.append([Btn("◀️ Админ", callback_data="admin")])
    return "\n".join(lines), Kbd(btns)


def build_adm_debt(debt_id):
    with get_db() as c:
        d = c.execute("""
            SELECT d.*, bu.username bu, bu.first_name bn, lu.username lu, lu.first_name ln
            FROM debts d
            JOIN users bu ON d.borrower_id = bu.user_id
            JOIN users lu ON d.lender_id   = lu.user_id
            WHERE d.id = ?
        """, (debt_id,)).fetchone()

    if not d:
        return f"❌ Долг #{debt_id} не найден.", Kbd([[Btn("◀️ Список", callback_data="adm_d:0")]])

    br   = display({"username": d["bu"], "first_name": d["bn"]})
    lr   = display({"username": d["lu"], "first_name": d["ln"]})
    rem  = d["amount"] - d["paid_amount"]
    paid_line = f"\n💳 Оплачено: {tg(d['paid_amount'])} / Осталось: {tg(rem)}" if d["paid_amount"] > 0 else ""

    text = (
        f"🔍 <b>Долг #{debt_id}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Сумма: <b>{tg(d['amount'])}</b>{paid_line}\n"
        f"📝 {e(d['description'])}\n"
        f"📊 Статус: {SEMOJI.get(d['status'],'❓')} <b>{STEXT.get(d['status'], d['status'])}</b>\n\n"
        f"👤 Кредитор: {e(lr)} (<code>{d['lender_id']}</code>)\n"
        f"👤 Должник:  {e(br)} (<code>{d['borrower_id']}</code>)\n\n"
        f"📅 Создан: {d['created_at']}\n"
        f"📅 Подтверждён: {d['confirmed_at'] or '—'}\n"
        f"📅 Погашен: {d['repaid_at'] or '—'}"
    )
    btns = []
    if d["status"] in ("pending", "active"):
        btns.append([Btn("✅ Закрыть долг", callback_data=f"adm_cl:{debt_id}"),
                     Btn("🗑 Удалить",       callback_data=f"adm_rm:{debt_id}")])
    btns.append([Btn(f"📋 Долги {br[:14]}", callback_data=f"adm_ud:{d['borrower_id']}:0"),
                 Btn("◀️ Список",           callback_data="adm_d:0")])
    return text, Kbd(btns)


def build_adm_user_debts(uid, page):
    u = get_user(uid)
    if not u:
        return "❌ Не найден.", Kbd([[Btn("◀️ Пользователи", callback_data="adm_u:0")]])

    with get_db() as c:
        all_d = c.execute("""
            SELECT d.*, lu.username lu, lu.first_name ln
            FROM debts d JOIN users lu ON d.lender_id = lu.user_id
            WHERE d.borrower_id = ? ORDER BY d.created_at DESC
        """, (uid,)).fetchall()

    rat   = calc_rating(uid)
    raw   = display(u)
    chunk, page, pages = paginate(list(all_d), page, 6)
    lines = [f"👤 <b>{e(raw)}</b> (<code>{uid}</code>)", f"{stars(rat)} <b>{rat}</b>", "━━━━━━━━━━━━━━━━━━━━"]
    btns  = []

    for r in chunk:
        lr     = display({"username": r["lu"], "first_name": r["ln"]})
        suffix = f" (оплач. {tg(r['paid_amount'])})" if r["paid_amount"] > 0 else ""
        lines.append(f"{SEMOJI.get(r['status'],'❓')} #{r['id']} — {tg(r['amount'])}{suffix} ← {e(lr)} ({r['created_at'][:10]})")
        if r["status"] in ("pending", "active"):
            btns.append([Btn(f"🔍 #{r['id']}", callback_data=f"adm_di:{r['id']}")])

    nav = nav_row(page, pages, f"adm_ud:{uid}")
    if nav:
        btns.append(nav)
    btns.append([Btn("◀️ Пользователи", callback_data="adm_u:0")])
    return "\n".join(lines), Kbd(btns)


# ── COMMANDS ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username, u.first_name)
    text, kb = build_menu(u.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username, u.first_name)
    text, kb = build_menu(u.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твой Telegram ID: <code>{update.effective_user.id}</code>", parse_mode="HTML"
    )


# ── NAVIGATION CALLBACKS ──────────────────────────────────────────────────────

async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    upsert_user(q.from_user.id, q.from_user.username, q.from_user.first_name)
    await edit_msg(q, *build_menu(q.from_user.id))


async def cb_mydebt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit_msg(q, *build_mydebt(q.from_user.id, int(q.data.split(":")[1])))


async def cb_owed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit_msg(q, *build_owed(q.from_user.id, int(q.data.split(":")[1])))


async def cb_hist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit_msg(q, *build_history(int(q.data.split(":")[1])))


async def cb_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit_msg(q, *build_top(q.data.split(":")[1]))


async def cb_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit_msg(q, *build_user_profile(int(q.data.split(":")[1])))


async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌ Нет прав.", show_alert=True); return
    await q.answer()
    await edit_msg(q, *build_admin())


async def cb_adm_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer(); return
    await q.answer()
    await edit_msg(q, *build_adm_users(int(q.data.split(":")[1])))


async def cb_adm_debts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer(); return
    await q.answer()
    await edit_msg(q, *build_adm_debts(int(q.data.split(":")[1])))


async def cb_adm_debt_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer(); return
    await q.answer()
    await edit_msg(q, *build_adm_debt(int(q.data.split(":")[1])))


async def cb_adm_user_debts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer(); return
    await q.answer()
    _, uid_s, pg_s = q.data.split(":")
    await edit_msg(q, *build_adm_user_debts(int(uid_s), int(pg_s)))


async def cb_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── USER SELECTION FLOW ───────────────────────────────────────────────────────

async def cb_newdebt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show user list to pick borrower."""
    q = update.callback_query
    await q.answer()
    upsert_user(q.from_user.id, q.from_user.username, q.from_user.first_name)
    await edit_msg(q, *build_user_select(q.from_user.id, 0, "nd"))


async def cb_reqdebt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show user list to pick lender to request from."""
    q = update.callback_query
    await q.answer()
    upsert_user(q.from_user.id, q.from_user.username, q.from_user.first_name)
    await edit_msg(q, *build_user_select(q.from_user.id, 0, "rd"))


async def cb_usersel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Paginate user selection list. Data: usersel_nd:2 or usersel_rd:0"""
    q = update.callback_query
    await q.answer()
    raw      = q.data           # "usersel_nd:2"
    parts    = raw.split(":")
    page     = int(parts[1])
    flow     = parts[0].split("_")[1]   # "nd" or "rd"
    await edit_msg(q, *build_user_select(q.from_user.id, page, flow))


# ── DEBT CONVERSATION (entry via user-selection buttons) ──────────────────────

async def cb_sel_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Conversation entry: user selected from list. Data: sel_nd:{uid} or sel_rd:{uid}"""
    q    = update.callback_query
    uid  = q.from_user.id
    # q.data = "sel_nd:12345" or "sel_rd:12345"
    parts      = q.data.split(":")   # ["sel_nd", "12345"]
    flow_key   = parts[0].split("_")[1]   # "nd" or "rd"
    target_uid = int(parts[1])

    target = get_user(target_uid)
    if not target:
        await q.answer("❌ Пользователь не найден.", show_alert=True); return

    target_name = display(target)
    ctx.user_data["flow"]       = flow_key
    ctx.user_data["target_uid"] = target_uid
    ctx.user_data["target_name"] = target_name
    ctx.user_data["cid"]        = q.message.chat_id
    ctx.user_data["mid"]        = q.message.message_id

    if flow_key == "nd":
        header = f"➕ <b>Дать в долг</b>\n\n✅ Должник: <b>{e(target_name)}</b>"
    else:
        header = f"🙏 <b>Попросить в долг</b>\n\nКредитор: <b>{e(target_name)}</b>"

    await q.answer()
    await q.edit_message_text(
        f"{header}\n\nВведи <b>сумму</b> (₸):",
        parse_mode="HTML",
        reply_markup=Kbd([[Btn("❌ Отмена", callback_data="conv_cancel")]]),
    )
    return ASK_AMOUNT


async def conv_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("❌ Отменено.")
    ctx.user_data.clear()
    await edit_msg(q, *build_menu(q.from_user.id))
    return ConversationHandler.END


async def _edit_conv(ctx, text, kb):
    try:
        await ctx.bot.edit_message_text(
            chat_id=ctx.user_data["cid"], message_id=ctx.user_data["mid"],
            text=text, parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        pass


async def conv_get_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    flow        = ctx.user_data.get("flow", "nd")
    target_name = ctx.user_data.get("target_name", "")

    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        label = "Должник" if flow == "nd" else "Кредитор"
        await _edit_conv(ctx,
            f"{'➕' if flow=='nd' else '🙏'} <b>{'Дать в долг' if flow=='nd' else 'Попросить в долг'}</b>\n\n"
            f"{label}: <b>{e(target_name)}</b>\n\n"
            "❌ Неверная сумма. Введи положительное число (например: <code>50000</code>):",
            Kbd([[Btn("❌ Отмена", callback_data="conv_cancel")]]))
        return ASK_AMOUNT

    ctx.user_data["amt"] = amount
    label = "Должник" if flow == "nd" else "Кредитор"
    await _edit_conv(ctx,
        f"{'➕' if flow=='nd' else '🙏'} <b>{'Дать в долг' if flow=='nd' else 'Попросить в долг'}</b>\n\n"
        f"✅ {label}: <b>{e(target_name)}</b>\n"
        f"✅ Сумма: <b>{tg(amount)}</b>\n\n"
        "Введи <b>описание</b> или нажми «Пропустить»:",
        Kbd([[Btn("⏩ Пропустить", callback_data="conv_skip"),
              Btn("❌ Отмена",     callback_data="conv_cancel")]]))
    return ASK_DESC


async def conv_skip_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["desc"] = "Без описания"
    return await _finalize(update.effective_user.id, ctx, q=q)


async def conv_get_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    ctx.user_data["desc"] = update.message.text.strip()[:200]
    return await _finalize(update.effective_user.id, ctx)


async def _finalize(acting_uid, ctx, q=None):
    flow        = ctx.user_data.get("flow", "nd")
    target_uid  = ctx.user_data["target_uid"]
    target_name = ctx.user_data["target_name"]
    amount      = ctx.user_data["amt"]
    desc        = ctx.user_data.get("desc", "Без описания")

    if flow == "nd":
        # Acting user is lender, target is borrower
        lender_id    = acting_uid
        borrower_id  = target_uid
        initiated_by = "lender"
    else:
        # Acting user is borrower, target is lender
        lender_id    = target_uid
        borrower_id  = acting_uid
        initiated_by = "borrower"

    with get_db() as c:
        cur = c.execute(
            "INSERT INTO debts(borrower_id,lender_id,amount,description,initiated_by) VALUES(?,?,?,?,?)",
            (borrower_id, lender_id, amount, desc, initiated_by),
        )
        did = cur.lastrowid

    me = get_user(acting_uid)
    me_name = display(me) if me else "Пользователь"

    if initiated_by == "lender":
        # Notify borrower
        try:
            await ctx.bot.send_message(
                borrower_id,
                f"⚠️ <b>Запрос о долге</b>\n\n"
                f"👤 Кредитор: <b>{e(me_name)}</b>\n"
                f"💵 Сумма: <b>{tg(amount)}</b>\n"
                f"📝 {e(desc)}\n🆔 #{did}\n\n"
                "Подтверди, что берёшь этот долг:",
                parse_mode="HTML",
                reply_markup=Kbd([[
                    Btn("✅ Подтверждаю", callback_data=f"confirm:{did}"),
                    Btn("❌ Отклоняю",    callback_data=f"reject:{did}"),
                ]]),
            )
            note = "✅ Уведомление отправлено должнику."
        except Exception:
            note = "⚠️ Не удалось уведомить должника (пусть напишет /start)."
        result_text = (
            f"✅ <b>Долг создан!</b>\n🆔 #{did}\n"
            f"👤 Должник: <b>{e(target_name)}</b>\n"
            f"💵 {tg(amount)}\n📝 {e(desc)}\n\n{note}"
        )
    else:
        # Notify lender
        try:
            await ctx.bot.send_message(
                lender_id,
                f"🙏 <b>Запрос на займ</b>\n\n"
                f"👤 <b>{e(me_name)}</b> просит одолжить:\n"
                f"💵 <b>{tg(amount)}</b>\n"
                f"📝 {e(desc)}\n🆔 #{did}\n\n"
                "Одобрить запрос?",
                parse_mode="HTML",
                reply_markup=Kbd([[
                    Btn("✅ Одобрить", callback_data=f"lapprove:{did}"),
                    Btn("❌ Отказать", callback_data=f"lreject:{did}"),
                ]]),
            )
            note = "✅ Запрос отправлен кредитору."
        except Exception:
            note = "⚠️ Не удалось уведомить кредитора."
        result_text = (
            f"🙏 <b>Запрос отправлен!</b>\n🆔 #{did}\n"
            f"👤 Кредитор: <b>{e(target_name)}</b>\n"
            f"💵 {tg(amount)}\n📝 {e(desc)}\n\n{note}"
        )

    result_kb = Kbd([[Btn("💸 Мои долги", callback_data="mydebt:0"),
                      Btn("◀️ Меню",      callback_data="menu")]])
    try:
        await ctx.bot.edit_message_text(
            chat_id=ctx.user_data["cid"], message_id=ctx.user_data["mid"],
            text=result_text, parse_mode="HTML", reply_markup=result_kb,
        )
    except Exception:
        if q:
            await edit_msg(q, result_text, result_kb)

    ctx.user_data.clear()
    return ConversationHandler.END


# ── DEBT ACTION CALLBACKS ─────────────────────────────────────────────────────

async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Borrower confirms lender-initiated debt."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["borrower_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "pending":
        await q.answer("❌ Уже обработан.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='active', confirmed_at=datetime('now') WHERE id=?", (did,))
    refresh_rating(uid)
    await q.answer("✅ Долг подтверждён!")

    me = e(f"@{q.from_user.username}" if q.from_user.username else q.from_user.first_name)
    try:
        await ctx.bot.send_message(
            debt["lender_id"],
            f"✅ <b>{me}</b> подтвердил долг <b>#{did}</b> на <b>{tg(debt['amount'])}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await edit_msg(q, *build_mydebt(uid, 0))


async def cb_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Borrower rejects lender-initiated debt."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["borrower_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "pending":
        await q.answer("❌ Уже обработан.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='cancelled' WHERE id=?", (did,))
    await q.answer("❌ Отклонено.")

    me = e(f"@{q.from_user.username}" if q.from_user.username else q.from_user.first_name)
    try:
        await ctx.bot.send_message(
            debt["lender_id"],
            f"❌ <b>{me}</b> отклонил запрос долга <b>#{did}</b>.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await edit_msg(q, *build_mydebt(uid, 0))


async def cb_lapprove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Lender approves borrower-initiated debt request."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["lender_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "pending":
        await q.answer("❌ Уже обработан.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='active', confirmed_at=datetime('now') WHERE id=?", (did,))
    refresh_rating(debt["borrower_id"])
    await q.answer("✅ Одобрено!")

    me = e(f"@{q.from_user.username}" if q.from_user.username else q.from_user.first_name)
    try:
        await ctx.bot.send_message(
            debt["borrower_id"],
            f"✅ <b>{me}</b> одобрил твой запрос на займ <b>#{did}</b> — <b>{tg(debt['amount'])}</b>.\n"
            "Долг активен!",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await edit_msg(q, *build_owed(uid, 0))


async def cb_lreject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Lender rejects borrower-initiated debt request."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["lender_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "pending":
        await q.answer("❌ Уже обработан.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='cancelled' WHERE id=?", (did,))
    await q.answer("❌ Отказано.")

    me = e(f"@{q.from_user.username}" if q.from_user.username else q.from_user.first_name)
    try:
        await ctx.bot.send_message(
            debt["borrower_id"],
            f"❌ <b>{me}</b> отказал в займе <b>#{did}</b> ({tg(debt['amount'])}).",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await edit_msg(q, *build_owed(uid, 0))


async def cb_breqcancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Borrower cancels their own pending borrow request."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["borrower_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "pending":
        await q.answer("❌ Нельзя отозвать.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='cancelled' WHERE id=?", (did,))
    await q.answer("⚫ Запрос отозван.")

    try:
        await ctx.bot.send_message(
            debt["lender_id"],
            f"⚫ Запрос на займ <b>#{did}</b> ({tg(debt['amount'])}) был отозван.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await edit_msg(q, *build_mydebt(uid, 0))


async def cb_repay_req(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Borrower requests full repayment confirmation from lender."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["borrower_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "active":
        await q.answer("❌ Не активен.", show_alert=True); return

    rem = debt["amount"] - debt["paid_amount"]
    me  = e(f"@{q.from_user.username}" if q.from_user.username else q.from_user.first_name)
    try:
        await ctx.bot.send_message(
            debt["lender_id"],
            f"💸 <b>Заявка на закрытие долга #{did}</b>\n\n"
            f"<b>{me}</b> сообщает, что вернул:\n"
            f"💵 <b>{tg(rem)}</b>\n📝 {e(debt['description'])}\n\n"
            "Подтверди получение:",
            parse_mode="HTML",
            reply_markup=Kbd([[
                Btn("✅ Получил", callback_data=f"repaid:{did}"),
                Btn("❌ Не получал", callback_data=f"notrepaid:{did}"),
            ]]),
        )
        await q.answer("✅ Запрос отправлен кредитору!")
    except Exception:
        await q.answer("❌ Не удалось уведомить кредитора.", show_alert=True)


async def cb_repaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt:
        await q.answer("❌ Не найден.", show_alert=True); return
    if debt["lender_id"] != uid and uid not in ADMIN_IDS:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "active":
        await q.answer("❌ Не активен.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='repaid', paid_amount=amount, repaid_at=datetime('now') WHERE id=?", (did,))
    new_rat = refresh_rating(debt["borrower_id"])
    await q.answer("✅ Долг закрыт!")

    try:
        await ctx.bot.send_message(
            debt["borrower_id"],
            f"✅ <b>Долг #{did} закрыт!</b>\n💵 {tg(debt['amount'])}\n"
            f"⭐ Ваш рейтинг: {stars(new_rat)} <b>{new_rat}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await edit_msg(q,
        f"✅ <b>Долг #{did} погашен!</b>\n💵 {tg(debt['amount'])}\n"
        f"⭐ Рейтинг должника: {stars(new_rat)} <b>{new_rat}</b>\n\n<i>Должник уведомлён.</i>",
        Kbd([[Btn("💰 Мне должны", callback_data="owed:0"), Btn("◀️ Меню", callback_data="menu")]]),
    )


async def cb_notrepaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["lender_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return

    await q.answer("❌ Погашение отклонено.")
    await edit_msg(q,
        f"❌ <b>Погашение долга #{did} отклонено.</b>\nДолг остаётся активным.",
        Kbd([[Btn("◀️ Меню", callback_data="menu")]]),
    )
    try:
        await ctx.bot.send_message(debt["borrower_id"],
            f"❌ Кредитор не подтвердил погашение долга <b>#{did}</b>. Свяжитесь с ним.",
            parse_mode="HTML")
    except Exception:
        pass


async def cb_dcancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Lender cancels/revokes a debt (any pending or active)."""
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt:
        await q.answer("❌ Не найден.", show_alert=True); return
    if debt["lender_id"] != uid:
        await q.answer("❌ Только кредитор может отменить долг.", show_alert=True); return
    if debt["status"] not in ("pending", "active"):
        await q.answer("❌ Нельзя отменить.", show_alert=True); return

    with get_db() as c:
        c.execute("UPDATE debts SET status='cancelled' WHERE id=?", (did,))
    refresh_rating(debt["borrower_id"])
    await q.answer("⚫ Аннулирован.")

    if debt["status"] == "active":
        try:
            await ctx.bot.send_message(
                debt["borrower_id"],
                f"⚫ Кредитор аннулировал долг <b>#{did}</b> ({tg(debt['amount'])}).\nДолг закрыт без погашения.",
                parse_mode="HTML")
        except Exception:
            pass

    await edit_msg(q, *build_owed(uid, 0))


# ── PARTIAL REPAYMENT ─────────────────────────────────────────────────────────

async def cb_partpay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["borrower_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "active":
        await q.answer("❌ Не активен.", show_alert=True); return

    rem = debt["amount"] - debt["paid_amount"]
    ctx.user_data.update(pp_did=did, pp_remaining=rem,
                         cid=q.message.chat_id, mid=q.message.message_id)
    await q.answer()
    await q.edit_message_text(
        f"💳 <b>Частичное погашение долга #{did}</b>\n\n"
        f"💰 Осталось: <b>{tg(rem)}</b>\n\nВведи сумму:",
        parse_mode="HTML",
        reply_markup=Kbd([[Btn("❌ Отмена", callback_data="pp_cancel")]]),
    )
    return ASK_PARTIAL


async def conv_pp_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("❌ Отменено.")
    ctx.user_data.clear()
    await edit_msg(q, *build_mydebt(q.from_user.id, 0))
    return ConversationHandler.END


async def conv_get_partial_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    did       = ctx.user_data.get("pp_did")
    remaining = ctx.user_data.get("pp_remaining", 0)
    if not did:
        return ConversationHandler.END

    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await _edit_conv(ctx,
            f"💳 <b>Частичное погашение долга #{did}</b>\n\n"
            f"💰 Осталось: <b>{tg(remaining)}</b>\n\n❌ Неверная сумма:",
            Kbd([[Btn("❌ Отмена", callback_data="pp_cancel")]]))
        return ASK_PARTIAL

    if amount > remaining + 0.01:
        await _edit_conv(ctx,
            f"💳 <b>Частичное погашение долга #{did}</b>\n\n"
            f"💰 Осталось: <b>{tg(remaining)}</b>\n\n❌ Сумма больше остатка:",
            Kbd([[Btn("❌ Отмена", callback_data="pp_cancel")]]))
        return ASK_PARTIAL

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()
    if not debt:
        return ConversationHandler.END

    me        = e(f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name)
    after_rem = remaining - amount
    try:
        await ctx.bot.send_message(
            debt["lender_id"],
            f"💳 <b>Частичная оплата долга #{did}</b>\n\n"
            f"<b>{me}</b> сообщает об оплате: <b>{tg(amount)}</b>\n"
            f"💰 Останется: <b>{tg(after_rem)}</b>\n📝 {e(debt['description'])}\n\n"
            "Подтверди получение:",
            parse_mode="HTML",
            reply_markup=Kbd([[
                Btn("✅ Получил", callback_data=f"pconfirm:{did}:{round(amount)}"),
                Btn("❌ Не получал", callback_data=f"preject:{did}"),
            ]]),
        )
        result = f"✅ Запрос отправлен!\nДолг #{did} — частичная оплата: <b>{tg(amount)}</b>"
    except Exception:
        result = "❌ Не удалось уведомить кредитора."

    try:
        await ctx.bot.edit_message_text(
            chat_id=ctx.user_data["cid"], message_id=ctx.user_data["mid"],
            text=result, parse_mode="HTML",
            reply_markup=Kbd([[Btn("◀️ Мои долги", callback_data="mydebt:0")]]),
        )
    except Exception:
        pass
    ctx.user_data.clear()
    return ConversationHandler.END


async def cb_pconfirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    uid    = q.from_user.id
    parts  = q.data.split(":")
    did, amount = int(parts[1]), float(parts[2])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["lender_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return
    if debt["status"] != "active":
        await q.answer("❌ Долг не активен.", show_alert=True); return

    new_paid  = debt["paid_amount"] + amount
    remaining = debt["amount"] - new_paid

    if remaining <= 0.01:
        with get_db() as c:
            c.execute("UPDATE debts SET paid_amount=?, status='repaid', repaid_at=datetime('now') WHERE id=?", (new_paid, did))
        new_rat = refresh_rating(debt["borrower_id"])
        await q.answer("✅ Долг полностью погашен!")
        lt = f"✅ <b>Долг #{did} полностью погашен!</b>\n💵 {tg(debt['amount'])}\n⭐ Рейтинг должника: {stars(new_rat)} <b>{new_rat}</b>"
        bt = f"✅ <b>Долг #{did} полностью погашен!</b>\n💵 {tg(debt['amount'])}\n⭐ Ваш рейтинг: {stars(new_rat)} <b>{new_rat}</b>"
    else:
        with get_db() as c:
            c.execute("UPDATE debts SET paid_amount=? WHERE id=?", (new_paid, did))
        await q.answer(f"✅ Принято: {tg(amount)}")
        lt = f"✅ <b>Частичная оплата принята!</b>\nДолг #{did}: получено <b>{tg(amount)}</b>, осталось <b>{tg(remaining)}</b>"
        bt = f"✅ <b>Частичная оплата подтверждена!</b>\nДолг #{did}: оплачено <b>{tg(amount)}</b>, осталось <b>{tg(remaining)}</b>"

    try:
        await ctx.bot.send_message(debt["borrower_id"], bt, parse_mode="HTML")
    except Exception:
        pass
    await edit_msg(q, lt, Kbd([[Btn("💰 Мне должны", callback_data="owed:0"), Btn("◀️ Меню", callback_data="menu")]]))


async def cb_preject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()

    if not debt or debt["lender_id"] != uid:
        await q.answer("❌ Нет прав.", show_alert=True); return

    await q.answer("❌ Оплата отклонена.")
    await edit_msg(q,
        f"❌ <b>Частичная оплата долга #{did} отклонена.</b>",
        Kbd([[Btn("◀️ Меню", callback_data="menu")]]),
    )
    try:
        await ctx.bot.send_message(debt["borrower_id"],
            f"❌ Кредитор не подтвердил частичную оплату долга <b>#{did}</b>.",
            parse_mode="HTML")
    except Exception:
        pass


# ── ADMIN DEBT CREATION CONVERSATION ─────────────────────────────────────────

async def cb_adm_newdebt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin clicks '➕ Начислить долг' — show borrower selection."""
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌ Нет прав.", show_alert=True); return
    await q.answer()
    ctx.user_data["adm_cid"] = q.message.chat_id
    ctx.user_data["adm_mid"] = q.message.message_id
    await edit_msg(q, *build_adm_sel_user(
        exclude_uid=-1, page=0,
        cb_prefix="adm_sel_b", nav_prefix="adm_usel_b",
        title="➕ <b>Начислить долг</b>",
        subtitle="Шаг 1/5 — Выбери <b>должника</b>:",
    ))


async def cb_adm_usel_b(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Paginate borrower selection (before conversation starts)."""
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer(); return
    await q.answer()
    page = int(q.data.split(":")[1])
    await edit_msg(q, *build_adm_sel_user(
        exclude_uid=-1, page=page,
        cb_prefix="adm_sel_b", nav_prefix="adm_usel_b",
        title="➕ <b>Начислить долг</b>",
        subtitle="Шаг 1/5 — Выбери <b>должника</b>:",
    ))


async def cb_adm_sel_b(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Borrower selected — store and show lender selection. Conv entry point."""
    q   = update.callback_query
    uid = q.from_user.id
    if uid not in ADMIN_IDS:
        await q.answer("❌ Нет прав.", show_alert=True); return

    borrower_uid = int(q.data.split(":")[1])
    borrower     = get_user(borrower_uid)
    if not borrower:
        await q.answer("❌ Пользователь не найден.", show_alert=True); return

    ctx.user_data["adm_borrower_uid"]  = borrower_uid
    ctx.user_data["adm_borrower_name"] = display(borrower)
    ctx.user_data["adm_cid"] = q.message.chat_id
    ctx.user_data["adm_mid"] = q.message.message_id
    await q.answer()
    await edit_msg(q, *build_adm_sel_user(
        exclude_uid=borrower_uid, page=0,
        cb_prefix="adm_sel_l", nav_prefix="adm_usel_l",
        title="➕ <b>Начислить долг</b>\n"
              f"✅ Должник: <b>{e(display(borrower))}</b>",
        subtitle="Шаг 2/5 — Выбери <b>кредитора</b> (кто дал деньги):",
    ))
    return ADM_SEL_LENDER


async def conv_adm_usel_l(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Paginate lender selection inside conversation."""
    q = update.callback_query
    await q.answer()
    page         = int(q.data.split(":")[1])
    borrower_uid = ctx.user_data.get("adm_borrower_uid", -1)
    bname        = ctx.user_data.get("adm_borrower_name", "")
    await edit_msg(q, *build_adm_sel_user(
        exclude_uid=borrower_uid, page=page,
        cb_prefix="adm_sel_l", nav_prefix="adm_usel_l",
        title=f"➕ <b>Начислить долг</b>\n✅ Должник: <b>{e(bname)}</b>",
        subtitle="Шаг 2/5 — Выбери <b>кредитора</b>:",
    ))
    return ADM_SEL_LENDER


async def conv_adm_sel_l(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Lender selected inside conversation — ask for date."""
    q          = update.callback_query
    lender_uid = int(q.data.split(":")[1])
    lender     = get_user(lender_uid)
    if not lender:
        await q.answer("❌ Не найден.", show_alert=True)
        return ADM_SEL_LENDER

    ctx.user_data["adm_lender_uid"]  = lender_uid
    ctx.user_data["adm_lender_name"] = display(lender)
    bname = ctx.user_data.get("adm_borrower_name", "")
    lname = display(lender)
    await q.answer()
    await edit_msg(q,
        f"➕ <b>Начислить долг</b>\n"
        f"✅ Должник:  <b>{e(bname)}</b>\n"
        f"✅ Кредитор: <b>{e(lname)}</b>\n\n"
        "Шаг 3/5 — Введи <b>дату</b> долга (например: <code>2024-03-15</code> или <code>15.03.2024</code>):",
        Kbd([[Btn("❌ Отмена", callback_data="adm_conv_cancel")]]),
    )
    return ADM_ASK_DATE


async def conv_adm_get_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    raw = update.message.text.strip()
    # Accept formats: YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY
    import re
    date_str = None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        date_str = raw
    m2 = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$", raw)
    if m2:
        date_str = f"{m2.group(3)}-{int(m2.group(2)):02d}-{int(m2.group(1)):02d}"

    bname = ctx.user_data.get("adm_borrower_name", "")
    lname = ctx.user_data.get("adm_lender_name", "")

    if not date_str:
        await _adm_edit(ctx,
            f"➕ <b>Начислить долг</b>\n"
            f"✅ Должник: <b>{e(bname)}</b>  Кредитор: <b>{e(lname)}</b>\n\n"
            "❌ Неверный формат. Введи дату: <code>2024-03-15</code> или <code>15.03.2024</code>:",
            Kbd([[Btn("❌ Отмена", callback_data="adm_conv_cancel")]]))
        return ADM_ASK_DATE

    ctx.user_data["adm_date"] = date_str
    await _adm_edit(ctx,
        f"➕ <b>Начислить долг</b>\n"
        f"✅ Должник: <b>{e(bname)}</b>\n"
        f"✅ Кредитор: <b>{e(lname)}</b>\n"
        f"✅ Дата: <b>{date_str}</b>\n\n"
        "Шаг 4/5 — Введи <b>сумму</b> долга (₸):",
        Kbd([[Btn("❌ Отмена", callback_data="adm_conv_cancel")]]))
    return ADM_ASK_AMT


async def conv_adm_get_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    bname = ctx.user_data.get("adm_borrower_name", "")
    lname = ctx.user_data.get("adm_lender_name", "")
    date  = ctx.user_data.get("adm_date", "")

    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await _adm_edit(ctx,
            f"➕ <b>Начислить долг</b>\n"
            f"✅ Должник: <b>{e(bname)}</b>  Кредитор: <b>{e(lname)}</b>\n"
            f"✅ Дата: <b>{date}</b>\n\n"
            "❌ Неверная сумма. Введи число (например: <code>50000</code>):",
            Kbd([[Btn("❌ Отмена", callback_data="adm_conv_cancel")]]))
        return ADM_ASK_AMT

    ctx.user_data["adm_amt"] = amount
    await _adm_edit(ctx,
        f"➕ <b>Начислить долг</b>\n"
        f"✅ Должник: <b>{e(bname)}</b>\n"
        f"✅ Кредитор: <b>{e(lname)}</b>\n"
        f"✅ Дата: <b>{date}</b>\n"
        f"✅ Сумма: <b>{tg(amount)}</b>\n\n"
        "Шаг 5/5 — Введи <b>описание</b> или нажми «Пропустить»:",
        Kbd([[Btn("⏩ Пропустить", callback_data="adm_conv_skip"),
              Btn("❌ Отмена",     callback_data="adm_conv_cancel")]]))
    return ADM_ASK_DESC


async def conv_adm_skip_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["adm_desc"] = "Без описания"
    return await _adm_finalize(q.from_user.id, ctx, q=q)


async def conv_adm_get_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    ctx.user_data["adm_desc"] = update.message.text.strip()[:200]
    return await _adm_finalize(update.effective_user.id, ctx)


async def conv_adm_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("❌ Отменено.")
    ctx.user_data.clear()
    await edit_msg(q, *build_admin())
    return ConversationHandler.END


async def _adm_edit(ctx, text, kb):
    try:
        await ctx.bot.edit_message_text(
            chat_id=ctx.user_data["adm_cid"], message_id=ctx.user_data["adm_mid"],
            text=text, parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        pass


async def _adm_finalize(admin_uid, ctx, q=None):
    borrower_uid = ctx.user_data["adm_borrower_uid"]
    lender_uid   = ctx.user_data["adm_lender_uid"]
    bname        = ctx.user_data["adm_borrower_name"]
    lname        = ctx.user_data["adm_lender_name"]
    date         = ctx.user_data["adm_date"]
    amount       = ctx.user_data["adm_amt"]
    desc         = ctx.user_data.get("adm_desc", "Без описания")

    with get_db() as c:
        cur = c.execute("""
            INSERT INTO debts(borrower_id, lender_id, amount, description,
                              status, initiated_by, created_at, confirmed_at)
            VALUES (?, ?, ?, ?, 'active', 'admin', ?, ?)
        """, (borrower_uid, lender_uid, amount, desc, date + " 00:00:00", date + " 00:00:00"))
        did = cur.lastrowid

    refresh_rating(borrower_uid)

    result = (
        f"✅ <b>Долг начислен администратором!</b>\n"
        f"🆔 #{did}\n"
        f"👤 Должник: <b>{e(bname)}</b>\n"
        f"👤 Кредитор: <b>{e(lname)}</b>\n"
        f"📅 Дата: <b>{date}</b>\n"
        f"💵 Сумма: <b>{tg(amount)}</b>\n"
        f"📝 {e(desc)}"
    )
    result_kb = Kbd([[Btn("⚙️ Админ-панель", callback_data="admin")]])

    # Notify both parties
    for uid, role in [(borrower_uid, "должник"), (lender_uid, "кредитор")]:
        try:
            other = lname if uid == borrower_uid else bname
            await ctx.bot.send_message(
                uid,
                f"🔧 <b>Администратор начислил долг #{did}</b>\n"
                f"👤 {'Кредитор' if uid == borrower_uid else 'Должник'}: <b>{e(other)}</b>\n"
                f"📅 Дата: <b>{date}</b>\n"
                f"💵 <b>{tg(amount)}</b>\n"
                f"📝 {e(desc)}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    try:
        await ctx.bot.edit_message_text(
            chat_id=ctx.user_data["adm_cid"], message_id=ctx.user_data["adm_mid"],
            text=result, parse_mode="HTML", reply_markup=result_kb,
        )
    except Exception:
        if q:
            await edit_msg(q, result, result_kb)

    ctx.user_data.clear()
    return ConversationHandler.END


# ── ADMIN ACTION CALLBACKS ────────────────────────────────────────────────────

async def cb_adm_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌", show_alert=True); return
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()
        if not debt:
            await q.answer("❌ Не найден.", show_alert=True); return
        c.execute("UPDATE debts SET status='repaid', paid_amount=amount, repaid_at=datetime('now') WHERE id=?", (did,))

    new_rat = refresh_rating(debt["borrower_id"])
    await q.answer("✅ Закрыт!")
    for cid in {debt["borrower_id"], debt["lender_id"]}:
        try:
            await ctx.bot.send_message(cid, f"🔧 Долг <b>#{did}</b> закрыт администратором.", parse_mode="HTML")
        except Exception:
            pass
    await edit_msg(q, *build_adm_debt(did))


async def cb_adm_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌", show_alert=True); return
    did = int(q.data.split(":")[1])

    with get_db() as c:
        debt = c.execute("SELECT * FROM debts WHERE id=?", (did,)).fetchone()
        if not debt:
            await q.answer("❌ Не найден.", show_alert=True); return
        c.execute("DELETE FROM debts WHERE id=?", (did,))

    refresh_rating(debt["borrower_id"])
    await q.answer("🗑 Удалён!")
    await edit_msg(q, *build_adm_debts(0))


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    init_db()
    logger.info("DB ready. Admins: %s", ADMIN_IDS)

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation: create/request debt (entry via user-selection button)
    conv_debt = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_sel_user, pattern=r"^sel_(nd|rd):\d+$")],
        states={
            ASK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_get_amount),
                CallbackQueryHandler(conv_cancel_cb, pattern=r"^conv_cancel$"),
            ],
            ASK_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_get_desc),
                CallbackQueryHandler(conv_skip_desc,  pattern=r"^conv_skip$"),
                CallbackQueryHandler(conv_cancel_cb,  pattern=r"^conv_cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(conv_cancel_cb, pattern=r"^conv_cancel$")],
        per_message=False,
        allow_reentry=True,
    )

    # Conversation: partial payment
    conv_partial = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_partpay, pattern=r"^partpay:\d+$")],
        states={
            ASK_PARTIAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_get_partial_amount),
                CallbackQueryHandler(conv_pp_cancel, pattern=r"^pp_cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(conv_pp_cancel, pattern=r"^pp_cancel$")],
        per_message=False,
        allow_reentry=True,
    )

    # Conversation: admin manual debt creation
    conv_admin = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_adm_sel_b, pattern=r"^adm_sel_b:\d+$")],
        states={
            ADM_SEL_LENDER: [
                CallbackQueryHandler(conv_adm_sel_l,  pattern=r"^adm_sel_l:\d+$"),
                CallbackQueryHandler(conv_adm_usel_l, pattern=r"^adm_usel_l:\d+$"),
                CallbackQueryHandler(conv_adm_cancel, pattern=r"^adm_conv_cancel$"),
            ],
            ADM_ASK_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_adm_get_date),
                CallbackQueryHandler(conv_adm_cancel, pattern=r"^adm_conv_cancel$"),
            ],
            ADM_ASK_AMT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_adm_get_amt),
                CallbackQueryHandler(conv_adm_cancel, pattern=r"^adm_conv_cancel$"),
            ],
            ADM_ASK_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_adm_get_desc),
                CallbackQueryHandler(conv_adm_skip_desc, pattern=r"^adm_conv_skip$"),
                CallbackQueryHandler(conv_adm_cancel,    pattern=r"^adm_conv_cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(conv_adm_cancel, pattern=r"^adm_conv_cancel$")],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv_debt)
    app.add_handler(conv_partial)
    app.add_handler(conv_admin)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("id",    cmd_id))

    for pattern, handler in [
        (r"^menu$",                   cb_menu),
        (r"^mydebt:\d+$",             cb_mydebt),
        (r"^owed:\d+$",               cb_owed),
        (r"^hist:\d+$",               cb_hist),
        (r"^top:(cur|all)$",          cb_top),
        (r"^profile:\d+$",            cb_profile),
        (r"^admin$",                  cb_admin),
        (r"^adm_u:\d+$",              cb_adm_users),
        (r"^adm_d:\d+$",              cb_adm_debts),
        (r"^adm_di:\d+$",             cb_adm_debt_info),
        (r"^adm_ud:\d+:\d+$",         cb_adm_user_debts),
        # User selection navigation
        (r"^newdebt$",                cb_newdebt),
        (r"^reqdebt$",                cb_reqdebt),
        (r"^usersel_(nd|rd):\d+$",    cb_usersel),
        # Debt actions
        (r"^confirm:\d+$",            cb_confirm),
        (r"^reject:\d+$",             cb_reject),
        (r"^lapprove:\d+$",           cb_lapprove),
        (r"^lreject:\d+$",            cb_lreject),
        (r"^breqcancel:\d+$",         cb_breqcancel),
        (r"^repay:\d+$",              cb_repay_req),
        (r"^repaid:\d+$",             cb_repaid),
        (r"^notrepaid:\d+$",          cb_notrepaid),
        (r"^dcancel:\d+$",            cb_dcancel),
        (r"^pconfirm:\d+:\d+$",       cb_pconfirm),
        (r"^preject:\d+$",            cb_preject),
        (r"^adm_cl:\d+$",             cb_adm_clear),
        (r"^adm_rm:\d+$",             cb_adm_remove),
        (r"^adm_newdebt$",            cb_adm_newdebt),
        (r"^adm_usel_b:\d+$",         cb_adm_usel_b),
        (r"^noop$",                   cb_noop),
    ]:
        app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
