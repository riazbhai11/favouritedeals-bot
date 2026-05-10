import os
import logging
import pg8000.native
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import threading
import asyncio
import nest_asyncio
import requests as req
from urllib.parse import urlparse
import json
import re

nest_asyncio.apply()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get("BOT_TOKEN")
CHAT_ID      = os.environ.get("CHAT_ID")
MAIN_CHAT_ID = os.environ.get("CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WC_KEY       = os.environ.get("WC_KEY")
WC_SECRET    = os.environ.get("WC_SECRET")
WP_URL       = os.environ.get("WP_URL", "https://favouritedeals.online")
WP_PAYLATER_SECRET = os.environ.get("WP_PAYLATER_SECRET", "")

FONNTE_TOKEN     = "8oSaMqEDoyw8Bk94Ctbv"
FD_MAIN_WHATSAPP = "01781678471"
FD_WEBSITE       = "favouritedeals.online"

app       = Flask(__name__)
main_loop = None

# =============================================
# WHATSAPP
# =============================================

def send_fonnte_wa(phone, message):
    digits = re.sub(r"[^0-9]", "", phone)
    if digits.startswith("880"): digits = "0" + digits[3:]
    if digits.startswith("88"):  digits = "0" + digits[2:]
    if not digits.startswith("0"): digits = "0" + digits
    try:
        resp = req.post(
            "https://api.fonnte.com/send",
            headers={"Authorization": FONNTE_TOKEN},
            data={"target": digits, "message": message, "countryCode": "880"},
            timeout=15
        )
        logger.info(f"Fonnte: {digits} -> {resp.text}")
    except Exception as e:
        logger.error(f"Fonnte error: {e}")


def wa_footer():
    return (
        f"\n\n─────────────────\n"
        f"📞 যোগাযোগ: *{FD_MAIN_WHATSAPP}*\n"
        f"⚠️ এই নম্বরে reply করবেন না।\n"
        f"🌐 {FD_WEBSITE}"
    )

# =============================================
# DATABASE
# =============================================

def get_db():
    url = urlparse(DATABASE_URL)
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:], user=url.username,
        password=url.password, ssl_context=ctx
    )


def setup_db():
    conn = get_db()
    try:
        conn.run("""CREATE TABLE IF NOT EXISTS sub_payment_due (
            id SERIAL PRIMARY KEY, sub_id BIGINT UNIQUE,
            customer_name VARCHAR(200), customer_email VARCHAR(200),
            customer_phone VARCHAR(50), item_names TEXT,
            next_payment_at TIMESTAMP, reminder_count INTEGER DEFAULT 0,
            last_reminded_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW(),
            cleared_at TIMESTAMP)""")

        conn.run("""CREATE TABLE IF NOT EXISTS order_payment_due (
            id SERIAL PRIMARY KEY, woo_order_id VARCHAR(50) UNIQUE,
            customer_name VARCHAR(200), customer_phone VARCHAR(50),
            item_names TEXT, amount DECIMAL(10,2),
            reminder_count INTEGER DEFAULT 0, last_reminded_at TIMESTAMP,
            cleared_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW())""")
    finally:
        conn.close()
    logger.info("✅ Database setup complete!")

# =============================================
# SUB PAYMENT DUE
# =============================================

def upsert_sub_payment_due(sub_id, customer_name, customer_email, customer_phone, item_names, next_payment_at):
    conn = get_db()
    try:
        conn.run("""
            INSERT INTO sub_payment_due
            (sub_id, customer_name, customer_email, customer_phone, item_names,
             next_payment_at, reminder_count, last_reminded_at, cleared_at)
            VALUES (:sid, :n, :e, :p, :items, :nxt, 0, NOW(), NULL)
            ON CONFLICT (sub_id) DO UPDATE SET
                customer_name=:n, customer_email=:e, customer_phone=:p,
                item_names=:items, next_payment_at=:nxt,
                reminder_count=0, last_reminded_at=NOW(), cleared_at=NULL
        """, sid=sub_id, n=customer_name, e=customer_email,
             p=customer_phone, items=item_names, nxt=next_payment_at)
    finally:
        conn.close()


def clear_sub_payment_due(sub_id):
    conn = get_db()
    try:
        conn.run("UPDATE sub_payment_due SET cleared_at=NOW() WHERE sub_id=:sid", sid=sub_id)
    finally:
        conn.close()


def get_pending_sub_payment_due():
    conn = get_db()
    try:
        rows = conn.run("""SELECT sub_id, customer_name, customer_email, customer_phone,
                   item_names, next_payment_at, reminder_count, last_reminded_at
            FROM sub_payment_due WHERE cleared_at IS NULL""")
    finally:
        conn.close()
    return [{"sub_id": r[0], "customer_name": r[1], "customer_email": r[2],
             "customer_phone": r[3], "item_names": r[4], "next_payment_at": r[5],
             "reminder_count": r[6] or 0, "last_reminded_at": r[7]} for r in rows]


def mark_sub_due_reminded(sub_id):
    conn = get_db()
    try:
        conn.run("UPDATE sub_payment_due SET reminder_count=reminder_count+1, last_reminded_at=NOW() WHERE sub_id=:sid", sid=sub_id)
    finally:
        conn.close()

# =============================================
# WOOCOMMERCE API
# =============================================

def wc_get(endpoint, params=None):
    try:
        return req.get(f"{WP_URL}/wp-json/wc/v3/{endpoint}",
                       auth=(WC_KEY, WC_SECRET), params=params or {}, timeout=15).json()
    except Exception as e:
        logger.error(f"WC GET [{endpoint}]: {e}")
        return None


def wc_put(endpoint, data):
    try:
        return req.put(f"{WP_URL}/wp-json/wc/v3/{endpoint}",
                       auth=(WC_KEY, WC_SECRET), json=data, timeout=15).json()
    except Exception as e:
        logger.error(f"WC PUT [{endpoint}]: {e}")
        return None

# =============================================
# SUBSCRIPTION HELPERS
# =============================================

SUB_STATUS_EMOJI = {"active": "✅", "on-hold": "⏸️", "cancelled": "❌", "expired": "⌛", "pending": "🕐"}
SUB_STATUS_LABEL = {"active": "Active — চালু আছে", "on-hold": "Paused — বন্ধ আছে",
                    "cancelled": "Cancelled", "expired": "Expired", "pending": "Pending"}


def get_subscriptions_by_email(email):
    try:
        subs = req.get(f"{WP_URL}/wp-json/wc/v3/subscriptions",
                       auth=(WC_KEY, WC_SECRET),
                       params={"search": email, "per_page": 20}, timeout=15).json()
        if isinstance(subs, list):
            return [s for s in subs if s.get("billing", {}).get("email", "").lower() == email.lower()]
        return []
    except Exception as e:
        logger.error(f"Subscription fetch: {e}")
        return []


def wc_set_bot_controlled(sub_id, enabled=True):
    sub = wc_get(f"subscriptions/{sub_id}")
    if not sub:
        return None
    meta_payload = []
    found = False
    for meta in sub.get("meta_data", []):
        if meta.get("key") == "_fd_bot_controlled":
            item = {"key": "_fd_bot_controlled", "value": "yes" if enabled else "no"}
            if meta.get("id"):
                item["id"] = meta["id"]
            meta_payload.append(item)
            found = True
            break
    if not found:
        meta_payload.append({"key": "_fd_bot_controlled", "value": "yes" if enabled else "no"})
    return wc_put(f"subscriptions/{sub_id}", {"meta_data": meta_payload})


def wp_subscription_renew(sub_id, next_dt, status="active"):
    try:
        return req.post(
            f"{WP_URL}/wp-json/fdbot/v1/subscription-renew",
            headers={"X-FD-Secret": WP_PAYLATER_SECRET or "changeme123"},
            json={"sub_id": sub_id,
                  "next_payment_gmt": next_dt.strftime("%Y-%m-%d %H:%M:%S"),
                  "status": status},
            timeout=20
        ).json()
    except Exception as e:
        logger.error(f"wp_subscription_renew: {e}")
        return {"success": False, "message": str(e)}


def format_subscription_text(sub):
    sub_id     = sub.get("id", "?")
    status     = sub.get("status", "unknown")
    item_names = ", ".join([i.get("name", "?") for i in sub.get("line_items", [])])
    next_date  = sub.get("next_payment_date_gmt", "")
    text  = f"{SUB_STATUS_EMOJI.get(status,'❓')} *Subscription #{sub_id}*\n"
    text += f"   📦 {item_names}\n"
    text += f"   💵 ৳{sub.get('total','0')}\n"
    text += f"   Status: {SUB_STATUS_LABEL.get(status, status)}\n"
    if next_date:
        text += f"   📅 Next Payment: {next_date[:10]}\n"
    return text

# =============================================
# DATE HELPERS
# =============================================

def tail_int(data):
    return int(data.split("_")[-1])


def parse_wc_dt(value):
    if not value:
        return datetime.utcnow()
    raw = value.replace("T", " ").replace("Z", "")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass
    return datetime.utcnow()


def add_one_month(dt):
    year, month = dt.year, dt.month + 1
    if month > 12:
        month, year = 1, year + 1
    month_days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                  31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.replace(year=year, month=month, day=min(dt.day, month_days[month - 1]))

# =============================================
# SUBSCRIPTION RENEW
# =============================================

def send_subscription_due_wa(phone, name, item_names, next_show, count=None):
    if not phone:
        return
    count_text = f"\n🔁 Reminder #{count}" if count else ""
    send_fonnte_wa(phone,
        f"━━━━━━━━━━━━━━━━━━\n💳 *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*,\n\nআপনার subscription renew করা হয়েছে, কিন্তু payment এখনো বাকি আছে।{count_text}\n\n"
        f"📦 Service: *{item_names}*\n📅 Next Renewal: *{next_show}*\n\n"
        f"দয়া করে payment complete করুন।" + wa_footer()
    )


def send_subscription_renewed_wa(phone, name, item_names, next_show):
    if not phone:
        return
    send_fonnte_wa(phone,
        f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*! 🎉\n\nআপনার subscription সফলভাবে *renewed* হয়েছে!\n\n"
        f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_show}*\n\n🙏 ধন্যবাদ!" + wa_footer()
    )


async def process_subscription_renew_action(sub_id, paid, custom_dt=None):
    sub = wc_get(f"subscriptions/{sub_id}")
    if not sub or "id" not in sub:
        return False, "❌ Subscription পাওয়া যায়নি।"

    billing      = sub.get("billing", {})
    client_name  = billing.get("first_name", "প্রিয় গ্রাহক")
    client_email = billing.get("email", "")
    client_phone = billing.get("phone", "")
    item_names   = ", ".join([i.get("name", "?") for i in sub.get("line_items", [])]) or "Subscription"

    base_dt = parse_wc_dt(sub.get("next_payment_date_gmt", ""))
    if base_dt < datetime.utcnow():
        base_dt = datetime.utcnow()

    next_dt   = custom_dt if custom_dt else add_one_month(base_dt)
    next_show = next_dt.strftime("%d/%m/%Y")

    result = wp_subscription_renew(sub_id, next_dt, status="active")
    if not result or not result.get("success"):
        return False, f"❌ Renew হয়নি। {result.get('message', 'Unknown error')}"
    if result.get("status") != "active":
        return False, "❌ Subscription active হয়নি।"

    if result.get("next_payment_gmt"):
        try:
            next_dt   = parse_wc_dt(result["next_payment_gmt"])
            next_show = next_dt.strftime("%d/%m/%Y")
        except Exception:
            pass

    clear_sub_payment_due(sub_id)

    if paid:
        send_subscription_renewed_wa(client_phone, client_name, item_names, next_show)
        return True, (
            f"✅ *Subscription #{sub_id} Renew হয়েছে!*\n\n"
            f"📦 {item_names}\n📅 Next Renewal: {next_show}\n\n"
            f"{'✅ WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}"
        )

    upsert_sub_payment_due(sub_id, client_name, client_email, client_phone, item_names, next_dt)
    send_subscription_due_wa(client_phone, client_name, item_names, next_show)

    from telegram import Bot
    kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{sub_id}")]]
    await Bot(token=BOT_TOKEN).send_message(
        chat_id=MAIN_CHAT_ID,
        text=(f"💰 *Subscription Due*\n\n#{sub_id}\n👤 {client_name}\n📧 {client_email}\n"
              f"📦 {item_names}\n📅 Next: {next_show}\n\nPayment পেলে press করো।"),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return True, (
        f"✅ *Subscription #{sub_id} Active হয়েছে*\n\n"
        f"📅 Next Renewal: {next_show}\n💳 Payment due reminder চালু\n\n"
        f"{'✅ Client WA গেছে।' if client_phone else '⚠️ Phone নেই।'}"
    )

# =============================================
# BACKGROUND TASKS
# =============================================

async def send_subscription_due_reminders():
    while True:
        await asyncio.sleep(60 * 60)
        try:
            rows = get_pending_sub_payment_due()
            now  = datetime.utcnow()
            for row in rows:
                last = row["last_reminded_at"]
                if last and (now - last) < timedelta(hours=12):
                    continue
                next_show  = row["next_payment_at"].strftime("%d/%m/%Y") if row["next_payment_at"] else "N/A"
                next_count = (row["reminder_count"] or 0) + 1
                send_subscription_due_wa(
                    row["customer_phone"], row["customer_name"],
                    row["item_names"], next_show, count=next_count
                )
                mark_sub_due_reminded(row["sub_id"])
                from telegram import Bot
                kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{row['sub_id']}")]]
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=(f"⏰ *Sub Due Reminder #{next_count}*\n\n"
                          f"#{row['sub_id']} — {row['customer_name']}\n📦 {row['item_names']}"),
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Sub due reminder: {e}")


async def send_order_payment_due_reminders():
    while True:
        await asyncio.sleep(60 * 60)
        try:
            conn = get_db()
            try:
                rows = conn.run("""SELECT woo_order_id, customer_name, customer_phone,
                           item_names, amount, reminder_count, last_reminded_at
                    FROM order_payment_due WHERE cleared_at IS NULL""")
            finally:
                conn.close()
            now = datetime.utcnow()
            for row in rows:
                last = row[6]
                if last and (now - last) < timedelta(hours=12):
                    continue
                count = (row[5] or 0) + 1
                send_fonnte_wa(row[2],
                    f"━━━━━━━━━━━━━━━━━━\n💳 *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                    f"হ্যালো *{row[1]}*,\n\nআপনার order এর payment এখনো বাকি আছে।\n🔁 Reminder #{count}\n\n"
                    f"📦 Product: *{row[3]}*\n💵 Amount: *৳{row[4]}*\n\n"
                    f"দয়া করে payment complete করুন।" + wa_footer()
                )
                from telegram import Bot
                kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"order_due_paid_{row[0]}")]]
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=(f"⏰ *Order Due Reminder #{count}*\n\n"
                          f"Order #{row[0]} — {row[1]}\n📦 {row[3]}\n💵 ৳{row[4]} বাকি"),
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
                conn2 = get_db()
                try:
                    conn2.run("UPDATE order_payment_due SET reminder_count=reminder_count+1, last_reminded_at=NOW() WHERE woo_order_id=:oid", oid=row[0])
                finally:
                    conn2.close()
        except Exception as e:
            logger.error(f"Order due reminder: {e}")

# =============================================
# MENU
# =============================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Active Orders",       callback_data="active_orders")],
        [InlineKeyboardButton("📋 Subscription Check", callback_data="sub_check")],
    ])

# =============================================
# HANDLERS
# =============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛍️ *Favourite Deals Assistant*\n\nAssalamualaikum bhai! 👋",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    if context.user_data.get("state") == "waiting_custom_renew_date":
        text = update.message.text.strip()
        try:
            custom_dt = datetime.strptime(text, "%Y-%m-%d")
        except Exception:
            await update.message.reply_text("❌ Date format: `YYYY-MM-DD`", parse_mode="Markdown")
            return
        sub_id  = context.user_data.get("custom_renew_sub_id")
        paid    = context.user_data.get("custom_renew_paid", False)
        ok, msg = await process_subscription_renew_action(sub_id, paid, custom_dt=custom_dt)
        context.user_data["state"] = None
        context.user_data.pop("custom_renew_sub_id", None)
        context.user_data.pop("custom_renew_paid", None)
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return
    await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    try:
        # ── Menu ──────────────────────────────────────────────────────
        if data == "menu":
            await query.edit_message_text(
                "🛍️ *FD Assistant*",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )

        # ── Active Orders ─────────────────────────────────────────────
        elif data == "active_orders":
            await show_active_orders(query)

        # ── Subscription Check ────────────────────────────────────────
        elif data == "sub_check":
            await query.edit_message_text(
                "📋 *Subscription Check*\n\nClient এর email দাও:\n`/sub email@gmail.com`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        # ── Sub: pause ────────────────────────────────────────────────
        elif data.startswith("sub_pause_confirm_"):
            sub_id = tail_int(data)
            result = wc_put(f"subscriptions/{sub_id}", {"status": "on-hold"})
            if result and result.get("status") == "on-hold":
                client_phone = result.get("billing", {}).get("phone", "")
                client_name  = result.get("billing", {}).get("first_name", "প্রিয় গ্রাহক")
                plist        = ", ".join([i.get("name", "?") for i in result.get("line_items", [])])
                if client_phone:
                    send_fonnte_wa(client_phone,
                        f"━━━━━━━━━━━━━━━━━━\n⏸️ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                        f"হ্যালো *{client_name}*,\n\nআপনার subscription *pause* হয়ে গেছে।\n\n"
                        f"📦 Service: *{plist}*\n\n🔄 Renew করলেই আবার চালু হবে!" + wa_footer()
                    )
                await query.edit_message_text(
                    f"⏸️ *Subscription #{sub_id} Paused!*\n\n"
                    f"{'✅ Client WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Pause হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data.startswith("sub_pause_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"⏸️ *Subscription #{sub_id} Pause করবে?*",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏸️ হ্যাঁ Pause করো", callback_data=f"sub_pause_confirm_{sub_id}"),
                    InlineKeyboardButton("❌ না", callback_data="menu")
                ]]),
                parse_mode="Markdown"
            )

        # ── Sub: resume ───────────────────────────────────────────────
        elif data.startswith("sub_resume_confirm_"):
            sub_id = tail_int(data)
            await query.edit_message_text(f"⏳ #{sub_id} resume করছি...")
            try:
                wc_set_bot_controlled(sub_id, True)
                result = wc_put(f"subscriptions/{sub_id}", {"status": "active"})
            finally:
                wc_set_bot_controlled(sub_id, False)
            if result and result.get("status") == "active":
                next_date    = result.get("next_payment_date_gmt", "")
                next_show    = next_date[:10] if next_date else "N/A"
                item_names   = ", ".join([i.get("name", "?") for i in result.get("line_items", [])])
                client_phone = result.get("billing", {}).get("phone", "")
                client_name  = result.get("billing", {}).get("first_name", "প্রিয় গ্রাহক")
                client_email = result.get("billing", {}).get("email", "")
                if client_phone:
                    send_fonnte_wa(client_phone,
                        f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                        f"হ্যালো *{client_name}*! 🎉\n\nআপনার subscription সফলভাবে *চালু* হয়েছে!\n\n"
                        f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_show}*\n\n🙏 ধন্যবাদ!" + wa_footer()
                    )
                await query.edit_message_text(
                    f"✅ *Subscription #{sub_id} Resume হয়েছে!*\n\n"
                    f"📦 {item_names}\n📧 {client_email}\n📅 Next Renewal: {next_show}\n\n"
                    f"{'✅ WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Resume হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data.startswith("sub_resume_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"▶️ *Subscription #{sub_id} Resume*\n\nClient payment দিয়েছে?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ টাকা পেয়েছি — Resume", callback_data=f"sub_resume_confirm_{sub_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="menu")
                ]]),
                parse_mode="Markdown"
            )

        # ── Sub: renew ────────────────────────────────────────────────
        elif data.startswith("sub_renew_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"🔄 *Subscription #{sub_id} Renew*\n\nটাকা পেয়েছো?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ হ্যাঁ পেয়েছি", callback_data=f"sub_paid_yes_{sub_id}")],
                    [InlineKeyboardButton("❌ না পাইনি",       callback_data=f"sub_paid_no_{sub_id}")],
                    [InlineKeyboardButton("🔙 Back",            callback_data="menu")]
                ]),
                parse_mode="Markdown"
            )

        elif data.startswith("sub_paid_yes_") or data.startswith("sub_paid_no_"):
            sub_id = tail_int(data)
            paid   = data.startswith("sub_paid_yes_")
            mode   = "paid" if paid else "due"
            await query.edit_message_text(
                f"📆 *Subscription #{sub_id}*\n\nNext date কতদিন বাড়াবে?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📅 1 Month after", callback_data=f"sub_extend1m_{mode}_{sub_id}")],
                    [InlineKeyboardButton("✍️ Custom Date",   callback_data=f"sub_extendcustom_{mode}_{sub_id}")],
                    [InlineKeyboardButton("🔙 Back",           callback_data=f"sub_renew_{sub_id}")]
                ]),
                parse_mode="Markdown"
            )

        elif data.startswith("sub_extend1m_"):
            parts   = data.split("_")
            paid    = parts[2] == "paid"
            sub_id  = int(parts[3])
            ok, msg = await process_subscription_renew_action(sub_id, paid)
            await query.edit_message_text(msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")

        elif data.startswith("sub_extendcustom_"):
            parts  = data.split("_")
            paid   = parts[2] == "paid"
            sub_id = int(parts[3])
            context.user_data["state"]               = "waiting_custom_renew_date"
            context.user_data["custom_renew_sub_id"] = sub_id
            context.user_data["custom_renew_paid"]   = paid
            await query.edit_message_text(
                f"📅 *Subscription #{sub_id}*\n\nCustom date দাও:\n`YYYY-MM-DD`\n\nExample: `2026-06-15`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        elif data.startswith("sub_due_paid_"):
            sub_id = tail_int(data)
            clear_sub_payment_due(sub_id)
            await query.edit_message_text(
                f"✅ *Subscription #{sub_id}*\n\nPayment due reminder বন্ধ।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        # ── Sub: activate ─────────────────────────────────────────────
        elif data.startswith("sub_activate_wc_"):
            sub_id = tail_int(data)
            await query.edit_message_text(f"⏳ Subscription #{sub_id} activate করছি...")
            result = wc_put(f"subscriptions/{sub_id}", {"status": "active"})
            if result and result.get("status") == "active":
                next_date    = result.get("next_payment_date_gmt", "")[:10] or "N/A"
                item_names   = ", ".join([i.get("name", "?") for i in result.get("line_items", [])])
                client_phone = result.get("billing", {}).get("phone", "")
                client_name  = result.get("billing", {}).get("first_name", "প্রিয় গ্রাহক")
                if client_phone:
                    send_fonnte_wa(client_phone,
                        f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                        f"হ্যালো *{client_name}*! 🎉\n\nআপনার subscription সফলভাবে *চালু* হয়েছে!\n\n"
                        f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_date}*\n\n🙏 ধন্যবাদ!" + wa_footer()
                    )
                await query.edit_message_text(
                    f"✅ *Subscription #{sub_id} Active হয়েছে!*\n\n"
                    f"📦 {item_names}\n📅 Next Renewal: {next_date}\n\n"
                    f"{'✅ Client WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Activate হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        # ── Sub: cancel ───────────────────────────────────────────────
        elif data.startswith("sub_cancel_confirm_"):
            sub_id = tail_int(data)
            result = wc_put(f"subscriptions/{sub_id}", {"status": "cancelled"})
            if result and result.get("status") == "cancelled":
                await query.edit_message_text(
                    f"✅ *Subscription #{sub_id} Cancel হয়েছে!*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Cancel হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data.startswith("sub_cancel_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"⚠️ *Subscription #{sub_id} Cancel করবে?*",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ হ্যাঁ Cancel করো", callback_data=f"sub_cancel_confirm_{sub_id}"),
                    InlineKeyboardButton("❌ না", callback_data="menu")
                ]]),
                parse_mode="Markdown"
            )

        # ── WooCommerce order status ──────────────────────────────────
        elif data.startswith("wc_order_status_"):
            order_id = int(data.split("_")[3])
            await query.edit_message_text(
                f"✏️ Order #{order_id} এর নতুন status:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏳ Processing",      callback_data=f"wc_setstatus_{order_id}_processing")],
                    [InlineKeyboardButton("✅ Completed",        callback_data=f"wc_setstatus_{order_id}_completed")],
                    [InlineKeyboardButton("💳 Payment Pending", callback_data=f"wc_setstatus_{order_id}_pending")],
                    [InlineKeyboardButton("❌ Cancelled",        callback_data=f"wc_setstatus_{order_id}_cancelled")],
                    [InlineKeyboardButton("🔙 Back",             callback_data="active_orders")]
                ])
            )

        elif data.startswith("wc_paid_yes_"):
            order_id = tail_int(data)
            result   = wc_put(f"orders/{order_id}", {"status": "completed"})
            if result and result.get("status") == "completed":
                order   = wc_get(f"orders/{order_id}") or {}
                billing = order.get("billing", {})
                email   = billing.get("email", "")
                name    = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip() or "প্রিয় গ্রাহক"
                phone   = billing.get("phone", "")
                subs    = get_subscriptions_by_email(email) if email else []
                if subs:
                    sub        = subs[0]
                    sub_id     = sub.get("id")
                    item_names = ", ".join([i.get("name", "?") for i in sub.get("line_items", [])]) or "Subscription"
                    sub_b      = sub.get("billing", {})
                    s_name     = f"{sub_b.get('first_name','')} {sub_b.get('last_name','')}".strip() or name
                    s_phone    = sub_b.get("phone", "") or phone
                    next_dt    = add_one_month(parse_wc_dt(sub.get("next_payment_date_gmt", "")))
                    wp_subscription_renew(sub_id, next_dt, status="active")
                    if s_phone:
                        send_fonnte_wa(s_phone,
                            f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                            f"হ্যালো *{s_name}*! 🎉\n\nআপনার subscription সফলভাবে *চালু* হয়েছে!\n\n"
                            f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_dt.strftime('%d/%m/%Y')}*\n\n🙏 ধন্যবাদ!" + wa_footer()
                        )
                await query.edit_message_text(
                    f"✅ Order #{order_id} complete!{chr(10) + 'Subscription active। WA পাঠানো হয়েছে।' if subs else ''}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
            else:
                await query.edit_message_text("❌ Update হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data.startswith("wc_paid_no_"):
            order_id = tail_int(data)
            result   = wc_put(f"orders/{order_id}", {"status": "completed"})
            if result and result.get("status") == "completed":
                order   = wc_get(f"orders/{order_id}") or {}
                billing = order.get("billing", {})
                email   = billing.get("email", "")
                name    = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip() or "প্রিয় গ্রাহক"
                phone   = billing.get("phone", "")
                subs    = get_subscriptions_by_email(email) if email else []
                if subs:
                    sub        = subs[0]
                    sub_id     = sub.get("id")
                    item_names = ", ".join([i.get("name", "?") for i in sub.get("line_items", [])]) or "Subscription"
                    sub_b      = sub.get("billing", {})
                    s_name     = f"{sub_b.get('first_name','')} {sub_b.get('last_name','')}".strip() or name
                    s_phone    = sub_b.get("phone", "") or phone
                    next_dt    = add_one_month(parse_wc_dt(sub.get("next_payment_date_gmt", "")))
                    wp_subscription_renew(sub_id, next_dt, status="active")
                    upsert_sub_payment_due(sub_id, s_name, email, s_phone, item_names, next_dt)
                    if s_phone:
                        send_fonnte_wa(s_phone,
                            f"━━━━━━━━━━━━━━━━━━\n⚠️ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                            f"হ্যালো *{s_name}*!\n\nআপনার subscription *চালু* হয়েছে!\n\n"
                            f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_dt.strftime('%d/%m/%Y')}*\n\n"
                            f"💳 Payment এখনো বাকি আছে।" + wa_footer()
                        )
                    kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{sub_id}")]]
                    await query.edit_message_text(
                        f"💰 Subscription #{sub_id} active কিন্তু payment বাকি।\n"
                        f"👤 {s_name}\n📦 {item_names}\n📅 Next: {next_dt.strftime('%d/%m/%Y')}",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                else:
                    await query.edit_message_text("✅ Order complete! (Subscription পাওয়া যায়নি)",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            else:
                await query.edit_message_text("❌ Update হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data.startswith("wc_setstatus_"):
            parts      = data.split("_")
            order_id   = int(parts[2])
            new_status = parts[3]
            if new_status == "completed":
                await query.edit_message_text(
                    f"💳 Order #{order_id} — টাকা পেয়েছো?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ টাকা পেয়েছি",  callback_data=f"wc_paid_yes_{order_id}"),
                        InlineKeyboardButton("❌ এখনো পাইনি", callback_data=f"wc_paid_no_{order_id}")
                    ]])
                )
                return
            result = wc_put(f"orders/{order_id}", {"status": new_status})
            if result and result.get("status") == new_status:
                if new_status == "pending":
                    order = wc_get(f"orders/{order_id}")
                    if order:
                        billing    = order.get("billing", {})
                        phone      = billing.get("phone", "")
                        name       = billing.get("first_name", "প্রিয় গ্রাহক")
                        item_names = ", ".join([i.get("name", "?") for i in order.get("line_items", [])])
                        total      = order.get("total", "0")
                        if phone:
                            send_fonnte_wa(phone,
                                f"━━━━━━━━━━━━━━━━━━\n💳 *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                                f"হ্যালো *{name}*,\n\nআপনার order এর payment এখনো বাকি আছে।\n\n"
                                f"📦 Product: *{item_names}*\n💵 Amount: *৳{total}*\n\n"
                                f"দয়া করে payment complete করুন।" + wa_footer()
                            )
                            conn = get_db()
                            try:
                                conn.run("""INSERT INTO order_payment_due
                                    (woo_order_id, customer_name, customer_phone, item_names, amount, reminder_count, last_reminded_at)
                                    VALUES (:oid, :n, :p, :items, :amt, 0, NOW())
                                    ON CONFLICT (woo_order_id) DO UPDATE SET
                                        reminder_count=0, last_reminded_at=NOW(), cleared_at=NULL
                                """, oid=str(order_id), n=name, p=phone, items=item_names, amt=float(total))
                            finally:
                                conn.close()
                            from telegram import Bot
                            kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"order_due_paid_{order_id}")]]
                            await Bot(token=BOT_TOKEN).send_message(
                                chat_id=MAIN_CHAT_ID,
                                text=(f"💰 *Order Payment Due*\n\nOrder #{order_id}\n"
                                      f"👤 {name}\n📦 {item_names}\n💵 ৳{total}"),
                                reply_markup=InlineKeyboardMarkup(kb),
                                parse_mode="Markdown"
                            )
                await query.edit_message_text(
                    f"✅ Order #{order_id} → *{new_status}*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Update হয়নি.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data.startswith("order_due_paid_"):
            order_id = data.split("_")[3]
            conn = get_db()
            try:
                conn.run("UPDATE order_payment_due SET cleared_at=NOW() WHERE woo_order_id=:oid", oid=str(order_id))
            finally:
                conn.close()
            await query.edit_message_text(
                f"✅ Order #{order_id} — Payment due বন্ধ।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.exception(f"button_handler error [{data}]: {e}")
        try:
            await query.edit_message_text(
                f"❌ Error:\n`{str(e)[:300]}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        except Exception:
            pass

# =============================================
# SHOW: ACTIVE ORDERS
# =============================================

async def show_active_orders(query):
    all_orders = []
    for status in ["pending", "processing", "on-hold"]:
        orders = wc_get("orders", {"status": status, "per_page": 20})
        if orders and isinstance(orders, list):
            all_orders.extend(orders)
    all_orders.sort(key=lambda x: x.get("date_created", ""), reverse=True)

    if not all_orders:
        await query.edit_message_text(
            "📋 কোনো active order নেই.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
        )
        return

    status_emoji = {"pending": "🕐", "processing": "⏳", "on-hold": "⏸️"}
    text         = f"📋 *Active Orders ({len(all_orders)}টা):*\n\n"
    keyboard     = []
    for o in all_orders[:15]:
        billing  = o.get("billing", {})
        name     = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip() or "Unknown"
        order_id = o.get("id")
        status   = o.get("status", "")
        text    += f"{status_emoji.get(status,'❓')} #{order_id} — {name}\n   💵 ৳{o.get('total','0')} | {status}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{order_id} status", callback_data=f"wc_order_status_{order_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# =============================================
# COMMANDS
# =============================================

async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📋 Format: `/sub email@gmail.com`", parse_mode="Markdown")
        return
    email = context.args[0].lower().strip()
    await update.message.reply_text(f"🔍 `{email}` খুঁজছি...", parse_mode="Markdown")
    subs  = get_subscriptions_by_email(email)
    if not subs:
        await update.message.reply_text(f"❌ `{email}` এ কোনো subscription নেই।", parse_mode="Markdown")
        return
    text     = f"📋 *{email} এর Subscriptions ({len(subs)}টা):*\n\n"
    keyboard = []
    for sub in subs:
        sub_id = sub.get("id")
        status = sub.get("status", "unknown")
        text  += format_subscription_text(sub) + "\n"
        row    = []
        if status == "pending":
            row += [InlineKeyboardButton(f"✅ #{sub_id} Activate", callback_data=f"sub_activate_wc_{sub_id}"),
                    InlineKeyboardButton(f"❌ #{sub_id} Cancel",   callback_data=f"sub_cancel_{sub_id}")]
        elif status == "on-hold":
            row += [InlineKeyboardButton(f"▶️ #{sub_id} Resume",  callback_data=f"sub_resume_{sub_id}"),
                    InlineKeyboardButton(f"❌ #{sub_id} Cancel",   callback_data=f"sub_cancel_{sub_id}")]
        elif status == "active":
            row += [InlineKeyboardButton(f"⏸️ #{sub_id} Pause",   callback_data=f"sub_pause_{sub_id}"),
                    InlineKeyboardButton(f"🔄 #{sub_id} Renew",   callback_data=f"sub_renew_{sub_id}")]
        elif status in ["cancelled", "expired"]:
            row += [InlineKeyboardButton(f"▶️ #{sub_id} Reactivate", callback_data=f"sub_resume_{sub_id}")]
        if row:
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


def _wp_paylater_api(method, endpoint, email=None):
    url     = f"{WP_URL}/wp-json/fdbot/v1/paylater/{endpoint}"
    headers = {"X-FD-Secret": WP_PAYLATER_SECRET}
    try:
        resp = req.get(url, headers=headers, timeout=10) if method == "GET" \
               else req.post(url, headers=headers, json={"email": email}, timeout=10)
        return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


async def paylater_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text(
            "📋 *Pay Later:*\n\n`/paylater add email`\n`/paylater remove email`\n`/paylater list`",
            parse_mode="Markdown"
        )
        return
    sub          = context.args[0].lower()
    context.args = context.args[1:]
    if sub == "add":
        if not context.args:
            await update.message.reply_text("Format: `/paylater add email`", parse_mode="Markdown")
            return
        email  = context.args[0].lower().strip()
        result = _wp_paylater_api("POST", "add", email)
        await update.message.reply_text(
            f"✅ Pay Later চালু!\n📧 `{email}`" if result.get("success") else f"❌ Error: {result.get('message')}",
            parse_mode="Markdown"
        )
    elif sub == "remove":
        if not context.args:
            await update.message.reply_text("Format: `/paylater remove email`", parse_mode="Markdown")
            return
        email  = context.args[0].lower().strip()
        result = _wp_paylater_api("POST", "remove", email)
        await update.message.reply_text(
            f"🗑️ Pay Later বাদ!\n📧 `{email}`" if result.get("success") else f"❌ `{email}` পাওয়া যায়নি।",
            parse_mode="Markdown"
        )
    elif sub == "list":
        result = _wp_paylater_api("GET", "list")
        if not result.get("success"):
            await update.message.reply_text(f"❌ Error: {result.get('message')}")
            return
        emails = result.get("emails", [])
        if not emails:
            await update.message.reply_text("📋 কোনো approved email নেই।")
            return
        text = f"📋 *Approved ({len(emails)}টা):*\n\n"
        for i, e in enumerate(emails, 1):
            text += f"{i}. `{e}`\n"
        await update.message.reply_text(text, parse_mode="Markdown")

# =============================================
# FLASK WEBHOOK
# =============================================

@app.route("/webhook/woocommerce", methods=["POST"])
def woocommerce_webhook():
    try:
        raw = request.data
        if not raw:
            return jsonify({"status": "ok"}), 200
        try:
            data = json.loads(raw)
        except Exception:
            return jsonify({"status": "ok"}), 200
        if not data:
            return jsonify({"status": "ok"}), 200

        order_id       = str(data.get("id", "N/A"))
        customer       = data.get("billing", {})
        customer_name  = f"{customer.get('first_name','')} {customer.get('last_name','')}".strip() or "Unknown"
        customer_email = customer.get("email", "")
        total          = float(data.get("total", 0))
        status         = data.get("status", "pending")
        items_text     = ", ".join([f"{i['name']} x{i['quantity']}" for i in data.get("line_items", [])])

        msg = (f"🛍️ *নতুন WooCommerce Order!*\n\n"
               f"📋 #{order_id}\n👤 {customer_name}\n📧 {customer_email}\n"
               f"📦 {items_text}\n💵 ৳{total}\n📊 {status}")
        asyncio.run_coroutine_threadsafe(send_telegram_message(msg), main_loop)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"}), 200


async def send_telegram_message(message):
    from telegram import Bot
    await Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# =============================================
# MAIN
# =============================================

async def main():
    global main_loop
    main_loop = asyncio.get_event_loop()
    setup_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start",    start))
    bot_app.add_handler(CommandHandler("sub",      subscription_command))
    bot_app.add_handler(CommandHandler("paylater", paylater_command))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ FD Admin Bot started!")

    async with bot_app:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        asyncio.create_task(send_subscription_due_reminders())
        asyncio.create_task(send_order_payment_due_reminders())
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
