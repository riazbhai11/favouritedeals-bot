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

BOT_TOKEN          = os.environ.get("BOT_TOKEN")
CHAT_ID            = os.environ.get("CHAT_ID")
MAIN_CHAT_ID       = os.environ.get("CHAT_ID")
DATABASE_URL       = os.environ.get("DATABASE_URL")
WC_KEY             = os.environ.get("WC_KEY")
WC_SECRET          = os.environ.get("WC_SECRET")
WP_URL             = os.environ.get("WP_URL", "https://favouritedeals.online")
WP_PAYLATER_SECRET = os.environ.get("WP_PAYLATER_SECRET", "fd_renew_A7kP29mQx4Lz_2026")

FONNTE_TOKEN     = "8oSaMqEDoyw8Bk94Ctbv"
FD_MAIN_WHATSAPP = "01781678471"
FD_WEBSITE       = "favouritedeals.online"

app       = Flask(__name__)
main_loop = None

# =============================================
# WHATSAPP
# =============================================

def send_fonnte_wa(phone, message):
    if not phone:
        return
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
# WHATSAPP MESSAGES
# =============================================

def wa_subscription_activated(name, product, next_date):
    """নতুন order complete হলে subscription active notification"""
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Favourite Deals*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*! 🎉\n\n"
        f"আপনার *{product}* Subscription টি সফলভাবে Active হয়েছে।\n\n"
        f"📅 আপনার Next Renew Date: *{next_date}*\n\n"
        f"যদি Subscription চলাকালীন কোনো সমস্যা face করেন অবশ্যই "
        f"*+88{FD_MAIN_WHATSAPP}* এই Number এ WhatsApp এ কল দিবেন।\n\n"
        f"ধন্যবাদ আমাদের Choose করার জন্য।"
        + wa_footer()
    )


def wa_subscription_activated_due(name, product, next_date):
    """নতুন order — subscription active কিন্তু payment বাকি"""
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Favourite Deals*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*! 🎉\n\n"
        f"আপনার *{product}* Subscription টি সফলভাবে Active হয়েছে।\n\n"
        f"📅 আপনার Next Renew Date: *{next_date}*\n\n"
        f"⚠️ আপনার Due বাকি আছে, অনুগ্রহ করে ১২ ঘন্টার মধ্যে পরিশোধ করুন।\n\n"
        f"যদি কোনো সমস্যা হয় *+88{FD_MAIN_WHATSAPP}* এ WhatsApp করুন।\n\n"
        f"ধন্যবাদ আমাদের Choose করার জন্য।"
        + wa_footer()
    )


def wa_subscription_renewed(name, product, next_date):
    """পুরনো subscription renew notification"""
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Favourite Deals*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*!\n\n"
        f"আপনার *{product}* সফলভাবে Renew হয়েছে।\n\n"
        f"📅 Next Renew Date: *{next_date}*\n\n"
        f"কোনো সমস্যা হলে জানাবেন *+88{FD_MAIN_WHATSAPP}*।\n\n"
        f"আমাদের সাথে থাকার জন্য ধন্যবাদ।"
        + wa_footer()
    )


def wa_subscription_paused(name, product):
    """Subscription pause notification"""
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏸️ *Favourite Deals*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*,\n\n"
        f"আপনার *{product}* Subscription টি Paused হয়ে গেছে।\n\n"
        f"🔄 আবার চালু করতে *+88{FD_MAIN_WHATSAPP}* এ WhatsApp করুন।"
        + wa_footer()
    )


def wa_subscription_cancelled(name, product):
    """Subscription cancel notification"""
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"❌ *Favourite Deals*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*,\n\n"
        f"আপনার *{product}* Subscription টি Cancel করা হয়েছে।\n\n"
        f"ভবিষ্যতে আবার সেবা নিতে চাইলে *+88{FD_MAIN_WHATSAPP}* এ যোগাযোগ করুন।"
        + wa_footer()
    )


def wa_due_reminder(name, product, count):
    """Payment due reminder"""
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Favourite Deals*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*,\n\n"
        f"আপনার *{product}* এর payment এখনো বাকি আছে।\n"
        f"🔁 Reminder #{count}\n\n"
        f"দয়া করে দ্রুত payment করুন।"
        + wa_footer()
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
            id SERIAL PRIMARY KEY,
            sub_id BIGINT UNIQUE,
            customer_name VARCHAR(200),
            customer_email VARCHAR(200),
            customer_phone VARCHAR(50),
            item_names TEXT,
            next_payment_at TIMESTAMP,
            reminder_count INTEGER DEFAULT 0,
            last_reminded_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            cleared_at TIMESTAMP
        )""")

        conn.run("""CREATE TABLE IF NOT EXISTS order_payment_due (
            id SERIAL PRIMARY KEY,
            woo_order_id VARCHAR(50) UNIQUE,
            customer_name VARCHAR(200),
            customer_phone VARCHAR(50),
            item_names TEXT,
            amount DECIMAL(10,2),
            reminder_count INTEGER DEFAULT 0,
            last_reminded_at TIMESTAMP,
            cleared_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
    finally:
        conn.close()
    logger.info("✅ Database setup complete!")

# =============================================
# DB HELPERS — SUB PAYMENT DUE
# =============================================

def upsert_sub_payment_due(sub_id, name, email, phone, items, next_dt):
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
        """, sid=sub_id, n=name, e=email, p=phone, items=items, nxt=next_dt)
    finally:
        conn.close()


def clear_sub_payment_due(sub_id):
    conn = get_db()
    try:
        conn.run(
            "UPDATE sub_payment_due SET cleared_at=NOW() WHERE sub_id=:sid",
            sid=sub_id
        )
    finally:
        conn.close()


def get_pending_sub_payment_due():
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT sub_id, customer_name, customer_email, customer_phone,
                   item_names, next_payment_at, reminder_count, last_reminded_at
            FROM sub_payment_due WHERE cleared_at IS NULL
        """)
    finally:
        conn.close()
    return [{"sub_id": r[0], "customer_name": r[1], "customer_email": r[2],
             "customer_phone": r[3], "item_names": r[4], "next_payment_at": r[5],
             "reminder_count": r[6] or 0, "last_reminded_at": r[7]} for r in rows]


def mark_sub_due_reminded(sub_id):
    conn = get_db()
    try:
        conn.run("""
            UPDATE sub_payment_due
            SET reminder_count=reminder_count+1, last_reminded_at=NOW()
            WHERE sub_id=:sid
        """, sid=sub_id)
    finally:
        conn.close()

# =============================================
# WOOCOMMERCE API
# =============================================

def wc_get(endpoint, params=None):
    try:
        return req.get(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            params=params or {},
            timeout=15
        ).json()
    except Exception as e:
        logger.error(f"WC GET [{endpoint}]: {e}")
        return None


def wc_put(endpoint, data):
    try:
        return req.put(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            json=data,
            timeout=15
        ).json()
    except Exception as e:
        logger.error(f"WC PUT [{endpoint}]: {e}")
        return None


def wc_delete(endpoint):
    try:
        return req.delete(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            params={"force": True},
            timeout=15
        ).json()
    except Exception as e:
        logger.error(f"WC DELETE [{endpoint}]: {e}")
        return None

# =============================================
# SUBSCRIPTION HELPERS
# =============================================

SUB_STATUS_EMOJI = {
    "active":    "✅",
    "on-hold":   "⏸️",
    "cancelled": "❌",
    "expired":   "⌛",
    "pending":   "🕐"
}
SUB_STATUS_LABEL = {
    "active":    "Active — চালু আছে",
    "on-hold":   "Paused — বন্ধ আছে",
    "cancelled": "Cancelled",
    "expired":   "Expired",
    "pending":   "Pending"
}


def get_subscriptions_by_email(email):
    try:
        subs = req.get(
            f"{WP_URL}/wp-json/wc/v3/subscriptions",
            auth=(WC_KEY, WC_SECRET),
            params={"search": email, "per_page": 20},
            timeout=15
        ).json()
        if isinstance(subs, list):
            return [s for s in subs
                    if s.get("billing", {}).get("email", "").lower() == email.lower()]
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
            headers={"X-FD-Secret": WP_PAYLATER_SECRET},
            json={
                "sub_id": sub_id,
                "next_payment_gmt": next_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "status": status
            },
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
    month_days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                  else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.replace(year=year, month=month, day=min(dt.day, month_days[month - 1]))

# =============================================
# CORE: NEW ORDER — SUBSCRIPTION ACTIVATE
# =============================================

async def activate_subscription_for_order(order_id, paid):
    """
    নতুন order complete করার পর subscription activate করো।
    paid=True  → payment নেওয়া হয়েছে
    paid=False → payment বাকি (due চালু হবে)
    """
    order = wc_get(f"orders/{order_id}")
    if not order:
        return False, "❌ Order পাওয়া যায়নি।"

    billing      = order.get("billing", {})
    client_name  = billing.get("first_name", "প্রিয় গ্রাহক")
    client_email = billing.get("email", "")
    client_phone = billing.get("phone", "")

    # Subscription খোঁজো
    subs = get_subscriptions_by_email(client_email) if client_email else []
    if not subs:
        # Subscription নেই — শুধু order complete করো
        wc_put(f"orders/{order_id}", {"status": "completed"})
        return True, f"✅ Order #{order_id} Complete!\n\n⚠️ কোনো Subscription পাওয়া যায়নি।"

    sub        = subs[0]
    sub_id     = sub.get("id")
    item_names = ", ".join([i.get("name", "?") for i in sub.get("line_items", [])]) or "Subscription"

    # Next payment date calculate
    base_dt = parse_wc_dt(sub.get("next_payment_date_gmt", ""))
    if base_dt < datetime.utcnow():
        base_dt = datetime.utcnow()
    next_dt   = add_one_month(base_dt)
    next_show = next_dt.strftime("%d/%m/%Y")

    # WooCommerce order → completed
    wc_put(f"orders/{order_id}", {"status": "completed"})

    # Subscription → active (bot controlled)
    wc_set_bot_controlled(sub_id, True)
    result = wp_subscription_renew(sub_id, next_dt, status="active")
    wc_set_bot_controlled(sub_id, False)

    if not result or not result.get("success"):
        return False, f"❌ Subscription activate হয়নি।\n{result.get('message','')}"

    if paid:
        # টাকা পেয়েছি — normal activation
        send_fonnte_wa(client_phone, wa_subscription_activated(client_name, item_names, next_show))
        return True, (
            f"✅ *Order #{order_id} Complete!*\n\n"
            f"📦 {item_names}\n"
            f"👤 {client_name}\n"
            f"📅 Next Renewal: {next_show}\n\n"
            f"{'✅ Client এ WhatsApp গেছে।' if client_phone else '⚠️ Phone নেই।'}"
        )
    else:
        # টাকা বাকি — due চালু করো
        send_fonnte_wa(client_phone, wa_subscription_activated_due(client_name, item_names, next_show))
        upsert_sub_payment_due(sub_id, client_name, client_email, client_phone, item_names, next_dt)

        from telegram import Bot
        kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{sub_id}")]]
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=MAIN_CHAT_ID,
            text=(
                f"💰 *Payment Due — Subscription*\n\n"
                f"#{sub_id} | Order #{order_id}\n"
                f"👤 {client_name}\n"
                f"📧 {client_email}\n"
                f"📦 {item_names}\n"
                f"📅 Next: {next_show}\n\n"
                f"টাকা পেলে press করো।"
            ),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return True, (
            f"✅ *Order #{order_id} Complete!*\n\n"
            f"📦 {item_names}\n"
            f"👤 {client_name}\n"
            f"📅 Next Renewal: {next_show}\n\n"
            f"⚠️ Payment due reminder চালু হয়েছে।\n"
            f"{'✅ Client এ WhatsApp গেছে।' if client_phone else '⚠️ Phone নেই।'}"
        )

# =============================================
# CORE: OLD SUBSCRIPTION RENEW
# =============================================

async def process_subscription_renew(sub_id, paid, custom_dt=None):
    """পুরনো subscription renew করো"""
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

    wc_set_bot_controlled(sub_id, True)
    result = wp_subscription_renew(sub_id, next_dt, status="active")
    wc_set_bot_controlled(sub_id, False)

    if not result or not result.get("success"):
        return False, f"❌ Renew হয়নি।\n{result.get('message', 'Unknown error')}"

    if result.get("next_payment_gmt"):
        try:
            next_dt   = parse_wc_dt(result["next_payment_gmt"])
            next_show = next_dt.strftime("%d/%m/%Y")
        except Exception:
            pass

    clear_sub_payment_due(sub_id)

    if paid:
        send_fonnte_wa(client_phone, wa_subscription_renewed(client_name, item_names, next_show))
        return True, (
            f"✅ *Subscription #{sub_id} Renew হয়েছে!*\n\n"
            f"📦 {item_names}\n"
            f"📅 Next Renewal: {next_show}\n\n"
            f"{'✅ Client এ WhatsApp গেছে।' if client_phone else '⚠️ Phone নেই।'}"
        )
    else:
        upsert_sub_payment_due(sub_id, client_name, client_email, client_phone, item_names, next_dt)
        send_fonnte_wa(client_phone, wa_subscription_activated_due(client_name, item_names, next_show))

        from telegram import Bot
        kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{sub_id}")]]
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=MAIN_CHAT_ID,
            text=(
                f"💰 *Payment Due — Renewal*\n\n"
                f"#{sub_id}\n"
                f"👤 {client_name}\n"
                f"📧 {client_email}\n"
                f"📦 {item_names}\n"
                f"📅 Next: {next_show}\n\n"
                f"টাকা পেলে press করো।"
            ),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return True, (
            f"✅ *Subscription #{sub_id} Active হয়েছে*\n\n"
            f"📅 Next Renewal: {next_show}\n"
            f"⚠️ Payment due reminder চালু\n\n"
            f"{'✅ Client এ WhatsApp গেছে।' if client_phone else '⚠️ Phone নেই।'}"
        )

# =============================================
# BACKGROUND: PAYMENT DUE REMINDERS
# =============================================

async def send_subscription_due_reminders():
    """প্রতি ১২ ঘন্টায় payment due reminder"""
    while True:
        await asyncio.sleep(60 * 60)  # প্রতি ঘন্টায় check
        try:
            rows = get_pending_sub_payment_due()
            now  = datetime.utcnow()
            for row in rows:
                last = row["last_reminded_at"]
                if last and (now - last) < timedelta(hours=12):
                    continue
                count = (row["reminder_count"] or 0) + 1
                send_fonnte_wa(
                    row["customer_phone"],
                    wa_due_reminder(row["customer_name"], row["item_names"], count)
                )
                mark_sub_due_reminded(row["sub_id"])

                from telegram import Bot
                kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{row['sub_id']}")]]
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=(
                        f"⏰ *Due Reminder #{count}*\n\n"
                        f"#{row['sub_id']} — {row['customer_name']}\n"
                        f"📦 {row['item_names']}"
                    ),
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Sub due reminder error: {e}")

# =============================================
# MENU
# =============================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Active Orders",       callback_data="active_orders")],
        [InlineKeyboardButton("🔍 Subscription Check", callback_data="sub_check")],
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

    # Custom renew date waiting
    if context.user_data.get("state") == "waiting_custom_renew_date":
        text = update.message.text.strip()
        try:
            custom_dt = datetime.strptime(text, "%Y-%m-%d")
        except Exception:
            await update.message.reply_text(
                "❌ Date format: `YYYY-MM-DD`\n\nExample: `2026-06-15`",
                parse_mode="Markdown"
            )
            return
        sub_id  = context.user_data.get("custom_renew_sub_id")
        paid    = context.user_data.get("custom_renew_paid", False)
        context.user_data["state"] = None
        context.user_data.pop("custom_renew_sub_id", None)
        context.user_data.pop("custom_renew_paid", None)
        ok, msg = await process_subscription_renew(sub_id, paid, custom_dt=custom_dt)
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return

    await update.message.reply_text("Menu থেকে কাজ করো bhai:", reply_markup=main_menu_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    try:
        # ── Menu ──────────────────────────────────────────────────────
        if data == "menu":
            await query.edit_message_text(
                "🛍️ *FD Assistant*\n\nকী করবো?",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )

        # ── Active Orders ─────────────────────────────────────────────
        elif data == "active_orders":
            await show_active_orders(query)

        # ── Subscription Check ────────────────────────────────────────
        elif data == "sub_check":
            await query.edit_message_text(
                "🔍 *Subscription Check*\n\nClient এর email দাও:\n`/sub email@gmail.com`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        # ── Order: Status Change ──────────────────────────────────────
        elif data.startswith("order_status_"):
            order_id = tail_int(data)
            await query.edit_message_text(
                f"✏️ *Order #{order_id}*\n\nনতুন status কী করবো?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Complete করো", callback_data=f"order_complete_{order_id}")],
                    [InlineKeyboardButton("⏳ Processing",   callback_data=f"order_set_processing_{order_id}")],
                    [InlineKeyboardButton("❌ Cancel",        callback_data=f"order_set_cancelled_{order_id}")],
                    [InlineKeyboardButton("🔙 Back",          callback_data="active_orders")]
                ]),
                parse_mode="Markdown"
            )

        elif data.startswith("order_set_processing_"):
            order_id = tail_int(data)
            result   = wc_put(f"orders/{order_id}", {"status": "processing"})
            status   = result.get("status") if result else None
            await query.edit_message_text(
                f"✅ Order #{order_id} → Processing" if status == "processing" else "❌ Update হয়নি।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        elif data.startswith("order_set_cancelled_"):
            order_id = tail_int(data)
            result   = wc_put(f"orders/{order_id}", {"status": "cancelled"})
            status   = result.get("status") if result else None
            await query.edit_message_text(
                f"✅ Order #{order_id} → Cancelled" if status == "cancelled" else "❌ Update হয়নি।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        # ── Order: Complete → টাকা পেয়েছো? ──────────────────────────
        elif data.startswith("order_complete_"):
            order_id = tail_int(data)
            await query.edit_message_text(
                f"💳 *Order #{order_id}*\n\nটাকা পেয়েছো?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ হ্যাঁ পেয়েছি",  callback_data=f"order_paid_yes_{order_id}")],
                    [InlineKeyboardButton("❌ না এখনো পাইনি", callback_data=f"order_paid_no_{order_id}")],
                    [InlineKeyboardButton("🔙 Back",            callback_data=f"order_status_{order_id}")]
                ]),
                parse_mode="Markdown"
            )

        elif data.startswith("order_paid_yes_"):
            order_id = tail_int(data)
            await query.edit_message_text(f"⏳ Order #{order_id} complete করছি...")
            ok, msg = await activate_subscription_for_order(order_id, paid=True)
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        elif data.startswith("order_paid_no_"):
            order_id = tail_int(data)
            await query.edit_message_text(f"⏳ Order #{order_id} complete করছি...")
            ok, msg = await activate_subscription_for_order(order_id, paid=False)
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        # ── Sub: Due Paid ─────────────────────────────────────────────
        elif data.startswith("sub_due_paid_"):
            sub_id = tail_int(data)
            clear_sub_payment_due(sub_id)
            await query.edit_message_text(
                f"✅ *Subscription #{sub_id}*\n\nPayment due reminder বন্ধ করা হয়েছে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

        # ── Sub: Pause ────────────────────────────────────────────────
        elif data.startswith("sub_pause_confirm_"):
            sub_id = tail_int(data)
            wc_set_bot_controlled(sub_id, True)
            result = wc_put(f"subscriptions/{sub_id}", {"status": "on-hold"})
            wc_set_bot_controlled(sub_id, False)
            if result and result.get("status") == "on-hold":
                billing      = result.get("billing", {})
                client_name  = billing.get("first_name", "প্রিয় গ্রাহক")
                client_phone = billing.get("phone", "")
                item_names   = ", ".join([i.get("name", "?") for i in result.get("line_items", [])])
                send_fonnte_wa(client_phone, wa_subscription_paused(client_name, item_names))
                await query.edit_message_text(
                    f"⏸️ *Subscription #{sub_id} Paused!*\n\n"
                    f"{'✅ Client এ WhatsApp গেছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    "❌ Pause হয়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )

        elif data.startswith("sub_pause_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"⏸️ *Subscription #{sub_id} Pause করবে?*",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏸️ হ্যাঁ Pause করো", callback_data=f"sub_pause_confirm_{sub_id}"),
                    InlineKeyboardButton("❌ না",               callback_data="menu")
                ]]),
                parse_mode="Markdown"
            )

        # ── Sub: Renew ────────────────────────────────────────────────
        elif data.startswith("sub_renew_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"🔄 *Subscription #{sub_id} Renew*\n\nটাকা পেয়েছো?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ হ্যাঁ পেয়েছি",  callback_data=f"sub_paid_yes_{sub_id}")],
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
                    [InlineKeyboardButton("📅 ১ মাস পর",    callback_data=f"sub_extend1m_{mode}_{sub_id}")],
                    [InlineKeyboardButton("✍️ Custom Date", callback_data=f"sub_extendcustom_{mode}_{sub_id}")],
                    [InlineKeyboardButton("🔙 Back",         callback_data=f"sub_renew_{sub_id}")]
                ]),
                parse_mode="Markdown"
            )

        elif data.startswith("sub_extend1m_"):
            parts   = data.split("_")
            paid    = parts[2] == "paid"
            sub_id  = int(parts[3])
            ok, msg = await process_subscription_renew(sub_id, paid)
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )

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

        # ── Sub: Cancel ───────────────────────────────────────────────
        elif data.startswith("sub_cancel_confirm_"):
            sub_id = tail_int(data)
            # আগে subscription info নাও
            sub          = wc_get(f"subscriptions/{sub_id}") or {}
            billing      = sub.get("billing", {})
            client_name  = billing.get("first_name", "প্রিয় গ্রাহক")
            client_phone = billing.get("phone", "")
            item_names   = ", ".join([i.get("name", "?") for i in sub.get("line_items", [])]) or "Subscription"

            # Cancel করো
            result = wc_put(f"subscriptions/{sub_id}", {"status": "cancelled"})
            if result and result.get("status") == "cancelled":
                send_fonnte_wa(client_phone, wa_subscription_cancelled(client_name, item_names))
                await query.edit_message_text(
                    f"❌ *Subscription #{sub_id} Cancelled!*\n\n"
                    f"{'✅ Client এ WhatsApp গেছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    "❌ Cancel হয়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )

        elif data.startswith("sub_cancel_"):
            sub_id = tail_int(data)
            await query.edit_message_text(
                f"⚠️ *Subscription #{sub_id} Cancel করবে?*\n\nClient কে notify করা হবে।",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ হ্যাঁ Cancel করো", callback_data=f"sub_cancel_confirm_{sub_id}"),
                    InlineKeyboardButton("❌ না",               callback_data="menu")
                ]]),
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
            "📋 কোনো active order নেই।",
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
        items    = ", ".join([i.get("name", "?") for i in o.get("line_items", [])])
        text    += (
            f"{status_emoji.get(status,'❓')} *#{order_id}* — {name}\n"
            f"   📦 {items}\n"
            f"   💵 ৳{o.get('total','0')} | {status}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✏️ #{order_id} — {name[:20]}", callback_data=f"order_status_{order_id}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# =============================================
# COMMANDS
# =============================================

async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔍 Format: `/sub email@gmail.com`",
            parse_mode="Markdown"
        )
        return

    email = context.args[0].lower().strip()
    await update.message.reply_text(f"🔍 `{email}` খুঁজছি...", parse_mode="Markdown")

    subs = get_subscriptions_by_email(email)
    if not subs:
        await update.message.reply_text(
            f"❌ `{email}` এ কোনো subscription নেই।",
            parse_mode="Markdown"
        )
        return

    text     = f"📋 *{email} এর Subscriptions ({len(subs)}টা):*\n\n"
    keyboard = []

    for sub in subs:
        sub_id = sub.get("id")
        status = sub.get("status", "unknown")
        text  += format_subscription_text(sub) + "\n"
        row    = []

        if status in ["active"]:
            row += [
                InlineKeyboardButton(f"⏸️ #{sub_id} Pause",  callback_data=f"sub_pause_{sub_id}"),
                InlineKeyboardButton(f"🔄 #{sub_id} Renew",  callback_data=f"sub_renew_{sub_id}"),
                InlineKeyboardButton(f"❌ #{sub_id} Cancel",  callback_data=f"sub_cancel_{sub_id}")
            ]
        elif status in ["on-hold", "pending", "expired", "cancelled"]:
            row += [
                InlineKeyboardButton(f"🔄 #{sub_id} Renew",  callback_data=f"sub_renew_{sub_id}"),
                InlineKeyboardButton(f"❌ #{sub_id} Cancel",  callback_data=f"sub_cancel_{sub_id}")
            ]

        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

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
        items_text     = ", ".join([
            f"{i['name']} x{i['quantity']}" for i in data.get("line_items", [])
        ])

        # Bot এ notification পাঠাও + Active Orders button
        msg = (
            f"🛍️ *নতুন Order!*\n\n"
            f"📋 #{order_id}\n"
            f"👤 {customer_name}\n"
            f"📧 {customer_email}\n"
            f"📦 {items_text}\n"
            f"💵 ৳{total}\n"
            f"📊 Status: {status}"
        )
        asyncio.run_coroutine_threadsafe(
            send_order_notification(msg, order_id),
            main_loop
        )
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


async def send_order_notification(message, order_id):
    from telegram import Bot
    kb = [[InlineKeyboardButton("📋 Active Orders দেখো", callback_data="active_orders")]]
    await Bot(token=BOT_TOKEN).send_message(
        chat_id=CHAT_ID,
        text=message,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"}), 200


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
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("sub",   subscription_command))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ FD Admin Bot started!")

    async with bot_app:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        asyncio.create_task(send_subscription_due_reminders())
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
