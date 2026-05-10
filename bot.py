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
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY")
WP_URL       = os.environ.get("WP_URL", "https://favouritedeals.online")
WP_PAYLATER_SECRET = os.environ.get("WP_PAYLATER_SECRET", "")

FONNTE_TOKEN     = "8oSaMqEDoyw8Bk94Ctbv"
FD_MAIN_WHATSAPP = "01781678471"
FD_WEBSITE       = "favouritedeals.online"

SUBSCRIPTION_PRODUCTS = {
    23269: "Claude Pro",
    21203: "Grok Premium",
    21147: "ChatGPT Plus",
    21099: "Meta AI Pro",
    21090: "YouTube Premium",
    21069: "CapCut Pro",
    21051: "Gemini Advanced",
    21032: "Canva Pro (Edu)",
}

app       = Flask(__name__)
main_loop = None
user_conversations = {}

# =============================================
# FONNTE WHATSAPP
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
        host=url.hostname,
        port=url.port or 5432,
        database=url.path[1:],
        user=url.username,
        password=url.password,
        ssl_context=ctx
    )


def setup_db():
    conn = get_db()
    try:
        conn.run("""CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            woo_order_id VARCHAR(50),
            customer_name VARCHAR(200),
            customer_email VARCHAR(200),
            total DECIMAL(10,2),
            status VARCHAR(50),
            items TEXT,
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS income (
            id SERIAL PRIMARY KEY,
            amount DECIMAL(10,2),
            note TEXT,
            type VARCHAR(20) DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS bot_memory (
            id SERIAL PRIMARY KEY,
            key VARCHAR(200) UNIQUE,
            value TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW())""")

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
            cleared_at TIMESTAMP)""")

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
            created_at TIMESTAMP DEFAULT NOW())""")

    finally:
        conn.close()
    logger.info("✅ Database setup complete!")

# =============================================
# MEMORY
# =============================================

def memory_save(key, value):
    conn = get_db()
    try:
        conn.run(
            "INSERT INTO bot_memory (key, value, updated_at) VALUES (:k,:v,NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()",
            k=key, v=value
        )
    finally:
        conn.close()


def memory_get_all():
    conn = get_db()
    try:
        rows = conn.run("SELECT key, value, updated_at FROM bot_memory ORDER BY updated_at DESC")
    finally:
        conn.close()
    return [{"key": r[0], "value": r[1], "updated_at": str(r[2])} for r in rows]

# =============================================
# DB HELPERS
# =============================================

def db_get_recent_orders(limit=10, status=None):
    conn = get_db()
    try:
        if status:
            rows = conn.run(
                "SELECT id,woo_order_id,customer_name,customer_email,total,status,items,created_at "
                "FROM orders WHERE status=:s ORDER BY created_at DESC LIMIT :l",
                s=status, l=limit
            )
        else:
            rows = conn.run(
                "SELECT id,woo_order_id,customer_name,customer_email,total,status,items,created_at "
                "FROM orders ORDER BY created_at DESC LIMIT :l",
                l=limit
            )
    finally:
        conn.close()
    return [{
        "id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3],
        "total": str(r[4]), "status": r[5], "items": r[6], "created_at": str(r[7])
    } for r in rows]


def db_get_last_order():
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,customer_email,total,status,items,created_at "
            "FROM orders ORDER BY created_at DESC LIMIT 1"
        )
    finally:
        conn.close()
    if rows:
        r = rows[0]
        return {
            "id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3],
            "total": str(r[4]), "status": r[5], "items": r[6], "created_at": str(r[7])
        }
    return None


def db_update_order_status(order_id, new_status, use_woo_id=False):
    conn = get_db()
    try:
        if use_woo_id:
            rows = conn.run("SELECT id,woo_order_id FROM orders WHERE woo_order_id=:oid", oid=str(order_id))
        else:
            rows = conn.run("SELECT id,woo_order_id FROM orders WHERE id=:id", id=int(order_id))
        if not rows:
            return False, "Order paoa jaini"
        db_id, woo_id = rows[0][0], rows[0][1]
        conn.run("UPDATE orders SET status=:s WHERE id=:id", s=new_status, id=db_id)
    finally:
        conn.close()
    try:
        req.put(
            f"{WP_URL}/wp-json/wc/v3/orders/{woo_id}",
            json={"status": new_status},
            auth=(WC_KEY, WC_SECRET),
            timeout=10
        )
    except Exception as e:
        logger.error(f"WC update error: {e}")
    return True, woo_id


def db_get_income_summary(days=1):
    conn = get_db()
    try:
        since = datetime.now() - timedelta(days=days)
        rows = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    return {"total": str(rows[0][0] or 0), "count": rows[0][1] or 0}


def db_get_orders_summary(days=1):
    conn = get_db()
    try:
        since = datetime.now() - timedelta(days=days)
        rows = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    return {"count": rows[0][0] or 0, "total": str(rows[0][1] or 0)}


def db_search_orders_by_name(name):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,customer_email,total,status,created_at "
            "FROM orders WHERE LOWER(customer_name) LIKE :n ORDER BY created_at DESC LIMIT 5",
            n=f"%{name.lower()}%"
        )
    finally:
        conn.close()
    return [{
        "id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3],
        "total": str(r[4]), "status": r[5], "created_at": str(r[6])
    } for r in rows]


def db_add_income(amount, note):
    conn = get_db()
    try:
        conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'manual')", a=float(amount), n=note)
    finally:
        conn.close()
    return True


def db_get_today_summary():
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        woo   = conn.run("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        inc   = conn.run("SELECT COALESCE(SUM(amount),0) FROM income WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    return {
        "orders": woo[0][0] or 0,
        "revenue": f"৳{float(woo[0][1] or 0):.0f}",
        "income":  f"৳{float(inc[0][0] or 0):.0f}",
    }

# =============================================
# SUBSCRIPTION PAYMENT DUE
# =============================================

def upsert_sub_payment_due(sub_id, customer_name, customer_email, customer_phone, item_names, next_payment_at):
    conn = get_db()
    try:
        conn.run("""
            INSERT INTO sub_payment_due
            (sub_id, customer_name, customer_email, customer_phone, item_names, next_payment_at,
             reminder_count, last_reminded_at, cleared_at)
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
        rows = conn.run("""
            SELECT sub_id, customer_name, customer_email, customer_phone,
                   item_names, next_payment_at, reminder_count, last_reminded_at
            FROM sub_payment_due WHERE cleared_at IS NULL
        """)
    finally:
        conn.close()
    return [{
        "sub_id": r[0], "customer_name": r[1], "customer_email": r[2],
        "customer_phone": r[3], "item_names": r[4], "next_payment_at": r[5],
        "reminder_count": r[6] or 0, "last_reminded_at": r[7]
    } for r in rows]


def mark_sub_due_reminded(sub_id):
    conn = get_db()
    try:
        conn.run("""
            UPDATE sub_payment_due
            SET reminder_count = reminder_count + 1, last_reminded_at = NOW()
            WHERE sub_id = :sid
        """, sid=sub_id)
    finally:
        conn.close()

# =============================================
# WOOCOMMERCE API
# =============================================

def wc_get(endpoint, params=None):
    try:
        resp = req.get(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            params=params or {},
            timeout=15
        )
        return resp.json()
    except Exception as e:
        logger.error(f"WC GET error [{endpoint}]: {e}")
        return None


def wc_post_req(endpoint, data):
    try:
        resp = req.post(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            json=data,
            timeout=15
        )
        return resp.json()
    except Exception as e:
        logger.error(f"WC POST error [{endpoint}]: {e}")
        return None


def wc_put(endpoint, data):
    try:
        resp = req.put(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            json=data,
            timeout=15
        )
        return resp.json()
    except Exception as e:
        logger.error(f"WC PUT error [{endpoint}]: {e}")
        return None

# =============================================
# SUBSCRIPTION HELPERS
# =============================================

SUBSCRIPTION_PRODUCTS_LIST = {
    23269: "Claude Pro",
    21203: "Grok Premium",
    21147: "ChatGPT Plus",
    21099: "Meta AI Pro",
    21090: "YouTube Premium",
    21069: "CapCut Pro",
    21051: "Gemini Advanced",
    21032: "Canva Pro (Edu)",
}

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


def fetch_subscription_products():
    products = []
    for product_id, display_name in SUBSCRIPTION_PRODUCTS_LIST.items():
        try:
            p = wc_get(f"products/{product_id}")
            if p and "id" in p:
                products.append({
                    "id": p["id"], "name": display_name,
                    "price": p.get("price", "0"),
                    "variation_ids": p.get("variations", []),
                    "slug": p.get("slug", "")
                })
            else:
                products.append({"id": product_id, "name": display_name, "price": "?", "variation_ids": [], "slug": ""})
        except Exception as e:
            logger.error(f"Product fetch error [{product_id}]: {e}")
    return products


def fetch_product_variations(product_id):
    try:
        result = wc_get(f"products/{product_id}/variations", {"per_page": 20})
        if not result or isinstance(result, dict):
            return []
        variations = []
        for v in result:
            if v.get("status") == "publish" and v.get("stock_status") == "instock":
                attr_name = ""
                if v.get("attributes"):
                    attr_name = v["attributes"][0].get("option", "")
                desc = re.sub(r"<[^>]+>", "", v.get("description", "")).strip()[:100]
                variations.append({
                    "id": v["id"],
                    "name": attr_name or v.get("name", f"Plan #{v['id']}"),
                    "price": v.get("price", "0"),
                    "description": desc,
                    "attribute_slug": v["attributes"][0].get("slug", "") if v.get("attributes") else "",
                    "attribute_option": attr_name
                })
        return variations
    except Exception as e:
        logger.error(f"Variation fetch error for product {product_id}: {e}")
        return []


def get_or_create_customer(email, phone, first_name="Customer", last_name="Customer"):
    existing = wc_get("customers", {"email": email})
    if existing and isinstance(existing, list) and len(existing) > 0:
        return existing[0]["id"], None
    username = (first_name + str(abs(hash(email)))[-4:]).lower().replace(" ", "")
    customer = wc_post_req("customers", {
        "email": email, "username": username,
        "password": "Temp@" + str(abs(hash(email)))[-6:],
        "first_name": first_name, "last_name": last_name,
        "billing": {"first_name": first_name, "last_name": last_name, "email": email, "phone": phone, "country": "BD"}
    })
    if customer and "id" in customer:
        return customer["id"], None
    err = customer.get("message", "Customer create hoyni") if customer else "Customer create hoyni"
    return None, err


def create_subscription_directly(email, phone, first_name, last_name, product_id, variation_id, variation_attributes, coupon=None):
    customer_id, err = get_or_create_customer(email, phone, first_name, last_name)
    if err:
        return None, err

    line_item = {"product_id": product_id, "quantity": 1}
    if variation_id:
        line_item["variation_id"] = variation_id
        if variation_attributes:
            line_item["meta_data"] = [{"key": f"attribute_{k}", "value": v} for k, v in variation_attributes.items()]

    sub_body = {
        "customer_id": customer_id, "status": "pending",
        "billing_period": "month", "billing_interval": 1,
        "payment_method": "bacs",
        "payment_method_title": "Manual Payment (bKash/Nagad)",
        "billing": {"first_name": first_name, "last_name": last_name, "email": email, "phone": phone, "country": "BD"},
        "line_items": [line_item],
        "meta_data": [
            {"key": "_client_phone", "value": phone},
            {"key": "_client_name", "value": f"{first_name} {last_name}"},
            {"key": "_bot_order", "value": "yes"}
        ]
    }
    if coupon:
        sub_body["coupon_lines"] = [{"code": coupon}]

    subscription = wc_post_req("subscriptions", sub_body)
    if not subscription or "id" not in subscription:
        return _create_order_fallback(email, phone, first_name, last_name, product_id, variation_id, variation_attributes, customer_id, coupon)
    return subscription, None


def _create_order_fallback(email, phone, first_name, last_name, product_id, variation_id, variation_attributes, customer_id, coupon=None):
    line_item = {"product_id": product_id, "quantity": 1}
    if variation_id:
        line_item["variation_id"] = variation_id
        if variation_attributes:
            line_item["meta_data"] = [{"key": f"attribute_{k}", "value": v} for k, v in variation_attributes.items()]

    order_body = {
        "customer_id": customer_id, "payment_method": "bacs",
        "payment_method_title": "Manual Payment (bKash/Nagad)",
        "set_paid": False,
        "billing": {"first_name": first_name, "last_name": last_name, "email": email, "phone": phone, "country": "BD"},
        "line_items": [line_item],
        "meta_data": [
            {"key": "_client_phone", "value": phone},
            {"key": "_client_name", "value": f"{first_name} {last_name}"},
            {"key": "_bot_order", "value": "yes"},
            {"key": "_is_fallback_order", "value": "yes"}
        ]
    }
    if coupon:
        order_body["coupon_lines"] = [{"code": coupon}]

    order = wc_post_req("orders", order_body)
    if not order or "id" not in order:
        err = order.get("message", "Order create hoyni") if order else "Order/Subscription create hoyni"
        return None, err
    return order, None


def get_subscriptions_by_email(email):
    try:
        resp = req.get(
            f"{WP_URL}/wp-json/wc/v3/subscriptions",
            auth=(WC_KEY, WC_SECRET),
            params={"search": email, "per_page": 20},
            timeout=15
        )
        subs = resp.json()
        if isinstance(subs, list):
            return [s for s in subs if s.get("billing", {}).get("email", "").lower() == email.lower()]
        return []
    except Exception as e:
        logger.error(f"Subscription fetch error: {e}")
        return []


def generate_payment_link(order_id, order_key=None, email=None):
    if email:
        try:
            resp = req.post(
                f"{WP_URL}/wp-json/fdbot/v1/autologin-link",
                headers={"X-FD-Secret": WP_PAYLATER_SECRET or "changeme123"},
                json={"order_id": order_id, "email": email},
                timeout=10
            )
            data = resp.json()
            if data.get("success") and data.get("autologin_url"):
                return data["autologin_url"]
        except Exception as e:
            logger.error(f"Autologin link error: {e}")

    if order_key:
        return f"{WP_URL}/checkout/order-pay/{order_id}/?pay_for_order=true&key={order_key}"

    order = wc_get(f"orders/{order_id}")
    if order and order.get("order_key"):
        key = order["order_key"]
        return f"{WP_URL}/checkout/order-pay/{order_id}/?pay_for_order=true&key={key}"

    return f"{WP_URL}/my-account/"


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
        resp = req.post(
            f"{WP_URL}/wp-json/fdbot/v1/subscription-renew",
            headers={"X-FD-Secret": WP_PAYLATER_SECRET or "changeme123"},
            json={"sub_id": sub_id, "next_payment_gmt": next_dt.strftime("%Y-%m-%d %H:%M:%S"), "status": status},
            timeout=20
        )
        return resp.json()
    except Exception as e:
        logger.error(f"wp_subscription_renew error: {e}")
        return {"success": False, "message": str(e)}


def format_subscription_text(sub):
    sub_id    = sub.get("id", "?")
    status    = sub.get("status", "unknown")
    emoji     = SUB_STATUS_EMOJI.get(status, "❓")
    label     = SUB_STATUS_LABEL.get(status, status)
    total     = sub.get("total", "0")
    next_date = sub.get("next_payment_date_gmt", "")
    items     = sub.get("line_items", [])
    item_names = ", ".join([i.get("name", "?") for i in items])

    text  = f"{emoji} *Subscription #{sub_id}*\n"
    text += f"   📦 {item_names}\n"
    text += f"   💵 ৳{total}\n"
    text += f"   Status: {label}\n"
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
        month = 1
        year += 1
    month_days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                  31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(dt.day, month_days[month - 1])
    return dt.replace(year=year, month=month, day=day)

# =============================================
# SUBSCRIPTION RENEW ACTION
# =============================================

def send_subscription_due_wa(phone, name, item_names, next_show, count=None):
    if not phone:
        return
    count_text = f"\n🔁 Reminder #{count}" if count else ""
    msg = (
        f"━━━━━━━━━━━━━━━━━━\n💳 *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*,\n\n"
        f"আপনার subscription renew করা হয়েছে, কিন্তু payment এখনো বাকি আছে।{count_text}\n\n"
        f"📦 Service: *{item_names}*\n"
        f"📅 Next Renewal: *{next_show}*\n\n"
        f"দয়া করে payment complete করুন।"
        + wa_footer()
    )
    send_fonnte_wa(phone, msg)


def send_subscription_renewed_wa(phone, name, item_names, next_show):
    if not phone:
        return
    msg = (
        f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"হ্যালো *{name}*! 🎉\n\n"
        f"আপনার subscription সফলভাবে *renewed* হয়েছে!\n\n"
        f"📦 Service: *{item_names}*\n"
        f"📅 পরবর্তী Renewal: *{next_show}*\n\n"
        f"🙏 ধন্যবাদ!"
        + wa_footer()
    )
    send_fonnte_wa(phone, msg)


async def process_subscription_renew_action(sub_id, paid, custom_dt=None):
    sub = wc_get(f"subscriptions/{sub_id}")
    if not sub or "id" not in sub:
        return False, "❌ Subscription পাওয়া যায়নি।"

    billing      = sub.get("billing", {})
    client_name  = billing.get("first_name", "প্রিয় গ্রাহক")
    client_email = billing.get("email", "")
    client_phone = billing.get("phone", "")
    items        = sub.get("line_items", [])
    item_names   = ", ".join([i.get("name", "?") for i in items]) or "Subscription"

    base_dt = parse_wc_dt(sub.get("next_payment_date_gmt", ""))
    if base_dt < datetime.utcnow():
        base_dt = datetime.utcnow()

    next_dt   = custom_dt if custom_dt else add_one_month(base_dt)
    next_show = next_dt.strftime("%d/%m/%Y")

    result = wp_subscription_renew(sub_id, next_dt, status="active")
    if not result or not result.get("success"):
        return False, f"❌ Renew/Active হয়নি। {result.get('message', 'Unknown error')}"

    saved_status   = result.get("status")
    saved_next_gmt = result.get("next_payment_gmt") or ""

    if saved_status != "active":
        return False, "❌ Subscription active হয়নি।"

    if saved_next_gmt:
        try:
            next_dt   = parse_wc_dt(saved_next_gmt)
            next_show = next_dt.strftime("%d/%m/%Y")
        except Exception:
            pass

    clear_sub_payment_due(sub_id)

    if paid:
        send_subscription_renewed_wa(client_phone, client_name, item_names, next_show)
        return True, (
            f"✅ *Subscription #{sub_id} Renew হয়েছে!*\n\n"
            f"📦 {item_names}\n"
            f"📅 Next Renewal: {next_show}\n\n"
            f"{'✅ WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}"
        )

    upsert_sub_payment_due(sub_id, client_name, client_email, client_phone, item_names, next_dt)
    send_subscription_due_wa(client_phone, client_name, item_names, next_show)

    from telegram import Bot
    kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{sub_id}")]]
    await Bot(token=BOT_TOKEN).send_message(
        chat_id=MAIN_CHAT_ID,
        text=(
            f"💰 *Subscription Due তৈরি হয়েছে*\n\n"
            f"Subscription #{sub_id}\n"
            f"👤 {client_name}\n📧 {client_email}\n"
            f"📦 {item_names}\n📅 Next Renewal: {next_show}\n\n"
            f"Payment পেলে নিচের button press করো।"
        ),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

    return True, (
        f"✅ *Subscription #{sub_id} Active হয়েছে*\n\n"
        f"📅 Next Renewal: {next_show}\n"
        f"💳 Payment due reminder চালু হয়েছে\n\n"
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
                next_show   = row["next_payment_at"].strftime("%d/%m/%Y") if row["next_payment_at"] else "N/A"
                next_count  = (row["reminder_count"] or 0) + 1
                send_subscription_due_wa(
                    row["customer_phone"], row["customer_name"],
                    row["item_names"], next_show, count=next_count
                )
                mark_sub_due_reminded(row["sub_id"])

                from telegram import Bot
                kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{row['sub_id']}")]]
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=(
                        f"⏰ *Subscription Due Reminder #{next_count}*\n\n"
                        f"Subscription #{row['sub_id']}\n"
                        f"👤 {row['customer_name']}\n📦 {row['item_names']}"
                    ),
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Subscription due reminder loop error: {e}")


async def send_order_payment_due_reminders():
    while True:
        await asyncio.sleep(60 * 60)
        try:
            conn = get_db()
            try:
                rows = conn.run("""
                    SELECT woo_order_id, customer_name, customer_phone, item_names,
                           amount, reminder_count, last_reminded_at
                    FROM order_payment_due WHERE cleared_at IS NULL
                """)
            finally:
                conn.close()

            now = datetime.utcnow()
            for row in rows:
                last = row[6]
                if last and (now - last) < timedelta(hours=12):
                    continue

                phone      = row[2]
                name       = row[1]
                item_names = row[3]
                amount     = str(row[4])
                count      = (row[5] or 0) + 1

                wa_msg = (
                    f"━━━━━━━━━━━━━━━━━━\n💳 *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                    f"হ্যালো *{name}*,\n\nআপনার order এর payment এখনো বাকি আছে।\n🔁 Reminder #{count}\n\n"
                    f"📦 Product: *{item_names}*\n💵 Amount: *৳{amount}*\n\n"
                    f"দয়া করে payment complete করুন।" + wa_footer()
                )
                send_fonnte_wa(phone, wa_msg)

                from telegram import Bot
                kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"order_due_paid_{row[0]}")]]
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=(
                        f"⏰ *Order Payment Reminder #{count}*\n\n"
                        f"Order #{row[0]}\n👤 {name}\n📦 {item_names}\n💵 ৳{amount} বাকি"
                    ),
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )

                conn2 = get_db()
                try:
                    conn2.run("""
                        UPDATE order_payment_due
                        SET reminder_count = reminder_count + 1, last_reminded_at = NOW()
                        WHERE woo_order_id = :oid
                    """, oid=row[0])
                finally:
                    conn2.close()

        except Exception as e:
            logger.error(f"Order payment due reminder loop error: {e}")

# =============================================
# AI ASSISTANT
# =============================================

AI_FUNCTIONS = [
    {"name": "get_recent_orders",     "description": "Recent WooCommerce orders",           "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}, "status": {"type": "string"}}}},
    {"name": "get_last_order",        "description": "Sorboshesh order",                    "parameters": {"type": "object", "properties": {}}},
    {"name": "update_order_status",   "description": "Order status change",                 "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "new_status": {"type": "string"}, "use_woo_id": {"type": "boolean"}}, "required": ["order_id", "new_status"]}},
    {"name": "get_income_summary",    "description": "Income summary",                      "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "get_today_summary",     "description": "Aajker orders + income summary",      "parameters": {"type": "object", "properties": {}}},
    {"name": "search_orders_by_name", "description": "Customer naam diye order khojo",      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "add_income",            "description": "Manual income add",                   "parameters": {"type": "object", "properties": {"amount": {"type": "number"}, "note": {"type": "string"}}, "required": ["amount", "note"]}},
    {"name": "save_memory",           "description": "Important info save koro",            "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"name": "get_all_memories",      "description": "Sob saved notes",                     "parameters": {"type": "object", "properties": {}}},
]


def execute_function(name, args):
    try:
        if name == "get_recent_orders":     return db_get_recent_orders(args.get("limit", 5), args.get("status"))
        elif name == "get_last_order":      return db_get_last_order()
        elif name == "update_order_status":
            ok, r = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=True)
            if not ok:
                ok, r = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=False)
            return {"success": ok, "result": r}
        elif name == "get_income_summary":  return db_get_income_summary(args.get("days", 1))
        elif name == "get_today_summary":   return db_get_today_summary()
        elif name == "search_orders_by_name": return db_search_orders_by_name(args["name"])
        elif name == "add_income":          return {"success": db_add_income(args["amount"], args["note"])}
        elif name == "save_memory":
            memory_save(args["key"], args["value"])
            return {"success": True, "saved": args["key"]}
        elif name == "get_all_memories":    return memory_get_all()
    except Exception as e:
        return {"error": str(e)}


def build_system_prompt():
    memories = memory_get_all()
    mem_text = ""
    if memories:
        mem_text = "\n\nTomar saved notes:\n"
        for m in memories[:10]:
            mem_text += f"- {m['key']}: {m['value']}\n"
    return f"""Tumi Favourite Deals er personal business assistant. Naam "FD Assistant".
Banglish e kotha bolbe. Chhoto sentence. Casual, friendly.
Sob takar hishab e ৳ sign use korbe.
{mem_text}"""


async def process_ai_message(messages_history):
    if not OPENAI_KEY:
        return None
    messages = [{"role": "system", "content": build_system_prompt()}] + messages_history
    try:
        resp = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": messages, "functions": AI_FUNCTIONS,
                  "function_call": "auto", "max_tokens": 1000},
            timeout=20
        ).json()
        if "error" in resp:
            return None
        msg = resp["choices"][0]["message"]
        if msg.get("function_call"):
            fn     = msg["function_call"]["name"]
            args   = json.loads(msg["function_call"]["arguments"])
            result = execute_function(fn, args)
            messages.append(msg)
            messages.append({"role": "function", "name": fn, "content": json.dumps(result, ensure_ascii=False)})
            resp2 = req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o", "messages": messages, "max_tokens": 600},
                timeout=20
            ).json()
            if "error" in resp2:
                return None
            return resp2["choices"][0]["message"]["content"]
        return msg.get("content")
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# =============================================
# MAIN MENU
# =============================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 আজকের Orders",       callback_data="today_orders"),
         InlineKeyboardButton("💰 আজকের Income",       callback_data="today_income")],
        [InlineKeyboardButton("📅 ৭ দিনের Orders",     callback_data="week_orders"),
         InlineKeyboardButton("📊 মাসের Report",        callback_data="month_report")],
        [InlineKeyboardButton("➕ Manual Income",        callback_data="manual_income"),
         InlineKeyboardButton("🔍 Customer খোঁজো",     callback_data="search_customer")],
        [InlineKeyboardButton("📋 Active Orders",        callback_data="active_orders")],
        [InlineKeyboardButton("📋 Subscription Check",   callback_data="sub_check"),
         InlineKeyboardButton("➕ নতুন Subscription",   callback_data="sub_new")],
    ])

# =============================================
# BOT HANDLERS
# =============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_conversations[chat_id] = []
    await update.message.reply_text(
        "🛍️ *Favourite Deals Assistant*\n\nAssalamualaikum bhai! "
        "Ami tomar business assistant. Menu theke kaj koro othoba seedha bolo! 🤖",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")

    # State: reject reason
    if context.user_data.get("state") == "waiting_custom_renew_date":
        try:
            custom_dt = datetime.strptime(text, "%Y-%m-%d")
        except Exception:
            await update.message.reply_text("❌ Date format হবে `YYYY-MM-DD`", parse_mode="Markdown")
            return

        sub_id = context.user_data.get("custom_renew_sub_id")
        paid   = context.user_data.get("custom_renew_paid", False)
        ok, msg = await process_subscription_renew_action(sub_id, paid, custom_dt=custom_dt)

        context.user_data["state"] = None
        context.user_data.pop("custom_renew_sub_id", None)
        context.user_data.pop("custom_renew_paid", None)
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return

    # AI fallback
    if chat_id not in user_conversations:
        user_conversations[chat_id] = []

    user_conversations[chat_id].append({"role": "user", "content": text})
    if len(user_conversations[chat_id]) > 15:
        user_conversations[chat_id] = user_conversations[chat_id][-15:]

    ai_reply = await process_ai_message(user_conversations[chat_id])
    if ai_reply:
        user_conversations[chat_id].append({"role": "assistant", "content": ai_reply})
        await update.message.reply_text(
            ai_reply,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    try:
        # ── Subscription check / new ──────────────────────────────────
        if data == "sub_check":
            await query.edit_message_text(
                "📋 *Subscription Check*\n\nClient এর email দাও:\n`/sub email@gmail.com`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

        elif data == "sub_new":
            await query.edit_message_text(
                "➕ *নতুন Subscription Create*\n\nFormat:\n`/newsub email \"Name\" phone`\n\n"
                "Example:\n`/newsub john@gmail.com \"John Doe\" 01712345678`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

        # ── New subscription: product select ──────────────────────────
        elif data.startswith("newsub_prod_"):
            product_id = int(data.split("_")[2])
            await query.edit_message_text(f"⏳ Product #{product_id} এর plans fetch করছি...")
            variations = fetch_product_variations(product_id)
            if not variations:
                await query.edit_message_text(
                    "❌ Plans পাওয়া যায়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
                return
            context.user_data["newsub_product_id"] = product_id
            keyboard = [[InlineKeyboardButton(f"{v['name']} — ৳{v['price']}", callback_data=f"newsub_var_{product_id}_{v['id']}")] for v in variations]
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="newsub_back")])
            await query.edit_message_text("📦 *Plan select করো:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        elif data == "newsub_back":
            await query.edit_message_text("⏳ Products fetch করছি...")
            products = fetch_subscription_products()
            if not products:
                await query.edit_message_text("❌ Products পাওয়া যায়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
                return
            keyboard = [[InlineKeyboardButton(f"📦 {p['name']}", callback_data=f"newsub_prod_{p['id']}")] for p in products]
            keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
            await query.edit_message_text("🛍️ *কোন product?*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        elif data.startswith("newsub_var_"):
            parts      = data.split("_")
            product_id = int(parts[2])
            var_id     = int(parts[3])
            email      = context.user_data.get("newsub_email")
            phone      = context.user_data.get("newsub_phone")
            first_name = context.user_data.get("newsub_first_name", "Customer")
            last_name  = context.user_data.get("newsub_last_name", "Customer")
            full_name  = context.user_data.get("newsub_full_name", "Customer")
            coupon     = context.user_data.get("newsub_coupon")

            if not email:
                await query.edit_message_text("❌ Session শেষ। আবার `/newsub` দাও।", parse_mode="Markdown")
                return

            await query.edit_message_text(f"⏳ `{email}` এর জন্য subscription create করছি...")
            variations   = fetch_product_variations(product_id)
            selected_var = next((v for v in variations if v["id"] == var_id), None)
            if not selected_var:
                await query.edit_message_text("❌ Variation পাওয়া যায়নি.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
                return

            variation_attributes = {}
            if selected_var.get("attribute_slug") and selected_var.get("attribute_option"):
                variation_attributes[selected_var["attribute_slug"]] = selected_var["attribute_option"]

            order, error = create_subscription_directly(email, phone, first_name, last_name, product_id, var_id, variation_attributes, coupon)

            for k in ["newsub_email", "newsub_phone", "newsub_first_name", "newsub_last_name", "newsub_full_name", "newsub_coupon", "newsub_product_id"]:
                context.user_data.pop(k, None)

            if error:
                await query.edit_message_text(f"❌ Error: {error}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]), parse_mode="Markdown")
                return

            order_id  = order["id"]
            order_key = order.get("order_key", "")
            is_sub    = "/subscriptions/" in str(order.get("_links", {}).get("self", [{}])[0].get("href", ""))

            if is_sub:
                try:
                    sub_orders = req.get(f"{WP_URL}/wp-json/wc/v3/subscriptions/{order_id}/orders", auth=(WC_KEY, WC_SECRET), timeout=15).json()
                    if isinstance(sub_orders, list) and sub_orders:
                        init_order = sub_orders[0]
                        pay_link   = generate_payment_link(init_order["id"], init_order.get("order_key", ""), email=email)
                    else:
                        pay_link = f"{WP_URL}/my-account/view-subscription/{order_id}/"
                except Exception as e:
                    logger.error(f"Sub initial order error: {e}")
                    pay_link = f"{WP_URL}/my-account/view-subscription/{order_id}/"
            else:
                pay_link = generate_payment_link(order_id, order_key, email=email)

            type_label  = "Subscription" if is_sub else "Order"
            coupon_text = f"\n🎟️ Coupon: `{coupon}`" if coupon else ""
            keyboard    = [
                [InlineKeyboardButton(f"✅ #{order_id} Activate করো", callback_data=f"sub_activate_{order_id}_{'sub' if is_sub else 'order'}")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
            ]
            await query.edit_message_text(
                f"✅ *{type_label} #{order_id} Create হয়েছে!*\n\n"
                f"👤 Name: `{full_name}`\n📧 Email: `{email}`\n📱 Phone: `{phone}`\n"
                f"📦 Plan: {selected_var['name']}\n💵 Amount: ৳{selected_var['price']}{coupon_text}\n\n"
                f"👇 Payment link:\n`{pay_link}`\n\n_Client pay করার পর Activate করো 👇_",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        # ── Subscription: pause ───────────────────────────────────────
        elif data.startswith("sub_pause_confirm_"):
            sub_id = tail_int(data)
            result = wc_put(f"subscriptions/{sub_id}", {"status": "on-hold"})
            if result and result.get("status") == "on-hold":
                items        = result.get("line_items", [])
                plist        = ", ".join([i.get("name", "?") for i in items])
                client_phone = result.get("billing", {}).get("phone", "")
                client_name  = result.get("billing", {}).get("first_name", "প্রিয় গ্রাহক")
                if client_phone:
                    send_fonnte_wa(client_phone,
                        f"━━━━━━━━━━━━━━━━━━\n⏸️ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                        f"হ্যালো *{client_name}*,\n\nআপনার subscription *pause* হয়ে গেছে।\n\n"
                        f"📦 Service: *{plist}*\n📅 তারিখ: *{datetime.now().strftime('%d/%m/%Y')}*\n\n"
                        f"🔄 Renew করলেই service আবার চালু হবে!" + wa_footer()
                    )
                await query.edit_message_text(
                    f"⏸️ *Subscription #{sub_id} Paused!*\n\n"
                    f"{'✅ WhatsApp notification পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}\n\n"
                    f"Resume করতে: `/sub email`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Pause হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        elif data.startswith("sub_pause_"):
            sub_id = tail_int(data)
            keyboard = [[
                InlineKeyboardButton("⏸️ হ্যাঁ Pause করো", callback_data=f"sub_pause_confirm_{sub_id}"),
                InlineKeyboardButton("❌ না", callback_data="menu")
            ]]
            await query.edit_message_text(
                f"⏸️ *Subscription #{sub_id} Pause করবে?*",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
            return

        # ── Subscription: resume ──────────────────────────────────────
        elif data.startswith("sub_resume_confirm_"):
            sub_id = tail_int(data)
            await query.edit_message_text(f"⏳ #{sub_id} resume করছি...")
            result = None
            try:
                wc_set_bot_controlled(sub_id, True)
                result = wc_put(f"subscriptions/{sub_id}", {"status": "active"})
            finally:
                wc_set_bot_controlled(sub_id, False)

            if result and result.get("status") == "active":
                next_date    = result.get("next_payment_date_gmt", "")
                next_show    = next_date[:10] if next_date else "N/A"
                items        = result.get("line_items", [])
                item_names   = ", ".join([i.get("name", "?") for i in items])
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
                    f"📦 {item_names}\n📧 {client_email}\n📅 পরবর্তী Renewal: {next_show}\n\n"
                    f"{'✅ WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    f"❌ Resume হয়নি।\n\nWooCommerce → Subscriptions → #{sub_id} → Status: Active",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
            return

        elif data.startswith("sub_resume_"):
            sub_id = tail_int(data)
            keyboard = [[
                InlineKeyboardButton("✅ টাকা পেয়েছি — Resume করো", callback_data=f"sub_resume_confirm_{sub_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="menu")
            ]]
            await query.edit_message_text(
                f"▶️ *Subscription #{sub_id} Resume*\n\nClient payment দিয়েছে? Confirm করো 👇",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
            return

        # ── Subscription: renew ───────────────────────────────────────
        elif data.startswith("sub_renew_"):
            sub_id   = tail_int(data)
            keyboard = [
                [InlineKeyboardButton("✅ হ্যাঁ পেয়েছি", callback_data=f"sub_paid_yes_{sub_id}")],
                [InlineKeyboardButton("❌ না পাইনি",       callback_data=f"sub_paid_no_{sub_id}")],
                [InlineKeyboardButton("🔙 Back",            callback_data="menu")]
            ]
            await query.edit_message_text(
                f"🔄 *Subscription #{sub_id} Renew*\n\nটাকা পেয়েছো?",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_paid_yes_") or data.startswith("sub_paid_no_"):
            sub_id = tail_int(data)
            paid   = data.startswith("sub_paid_yes_")
            mode   = "paid" if paid else "due"
            keyboard = [
                [InlineKeyboardButton("📅 1 Month after",  callback_data=f"sub_extend1m_{mode}_{sub_id}")],
                [InlineKeyboardButton("✍️ Custom Date",    callback_data=f"sub_extendcustom_{mode}_{sub_id}")],
                [InlineKeyboardButton("🔙 Back",            callback_data=f"sub_renew_{sub_id}")]
            ]
            await query.edit_message_text(
                f"📆 *Subscription #{sub_id}*\n\nNext date কতদিন বাড়াবে?",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_extend1m_"):
            parts  = data.split("_")
            paid   = parts[2] == "paid"
            sub_id = int(parts[3])
            ok, msg = await process_subscription_renew_action(sub_id, paid)
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]), parse_mode="Markdown")
            return

        elif data.startswith("sub_extendcustom_"):
            parts  = data.split("_")
            paid   = parts[2] == "paid"
            sub_id = int(parts[3])
            context.user_data["state"]           = "waiting_custom_renew_date"
            context.user_data["custom_renew_sub_id"] = sub_id
            context.user_data["custom_renew_paid"]   = paid
            await query.edit_message_text(
                f"📅 *Subscription #{sub_id}*\n\nCustom date দাও:\n`YYYY-MM-DD`\n\nExample: `2026-06-15`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_due_paid_"):
            sub_id = tail_int(data)
            clear_sub_payment_due(sub_id)
            await query.edit_message_text(
                f"✅ *Subscription #{sub_id}*\n\nPayment due reminder বন্ধ করা হয়েছে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

        # ── Subscription: activate (WC direct) ───────────────────────
        elif data.startswith("sub_activate_wc_"):
            sub_id = tail_int(data)
            await query.edit_message_text(f"⏳ Subscription #{sub_id} activate করছি...")
            result = wc_put(f"subscriptions/{sub_id}", {"status": "active"})
            if result and result.get("status") == "active":
                next_date    = result.get("next_payment_date_gmt", "")[:10] or "N/A"
                items        = result.get("line_items", [])
                item_names   = ", ".join([i.get("name", "?") for i in items])
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
                    f"{'✅ Client কে WhatsApp পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Activate হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        elif data.startswith("sub_activate_") and not data.startswith("sub_activate_wc_"):
            parts  = data.split("_")
            item_id = int(parts[2])
            is_sub  = len(parts) > 3 and parts[3] == "sub"
            await query.edit_message_text(f"⏳ #{item_id} activate করছি...")
            result = wc_put(f"subscriptions/{item_id}", {"status": "active"}) if is_sub \
                     else wc_put(f"orders/{item_id}", {"status": "completed"})
            if result and result.get("status") in ["active", "completed"]:
                await query.edit_message_text(
                    f"✅ *#{item_id} Activated!* 🎉",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Activate হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        # ── Subscription: cancel ──────────────────────────────────────
        elif data.startswith("sub_cancel_confirm_"):
            sub_id = tail_int(data)
            result = wc_put(f"subscriptions/{sub_id}", {"status": "cancelled"})
            if result and result.get("status") == "cancelled":
                await query.edit_message_text(f"✅ *Subscription #{sub_id} Cancel হয়েছে!*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]), parse_mode="Markdown")
            else:
                await query.edit_message_text("❌ Cancel হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        elif data.startswith("sub_cancel_"):
            sub_id   = tail_int(data)
            keyboard = [[
                InlineKeyboardButton("✅ হ্যাঁ Cancel করো", callback_data=f"sub_cancel_confirm_{sub_id}"),
                InlineKeyboardButton("❌ না, Back",           callback_data="menu")
            ]]
            await query.edit_message_text(
                f"⚠️ *Subscription #{sub_id} Cancel করবে?*\n\nClient এর access বন্ধ হবে!",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
            )
            return

        # ── WooCommerce order status ──────────────────────────────────
        elif data.startswith("wc_order_status_"):
            order_id = int(data.split("_")[3])
            keyboard = [
                [InlineKeyboardButton("⏳ Processing",       callback_data=f"wc_setstatus_{order_id}_processing")],
                [InlineKeyboardButton("✅ Completed",         callback_data=f"wc_setstatus_{order_id}_completed")],
                [InlineKeyboardButton("💳 Payment Pending",  callback_data=f"wc_setstatus_{order_id}_pending")],
                [InlineKeyboardButton("❌ Cancelled",         callback_data=f"wc_setstatus_{order_id}_cancelled")],
                [InlineKeyboardButton("🔙 Back",              callback_data="active_orders")]
            ]
            await query.edit_message_text(f"✏️ Order #{order_id} এর নতুন status:", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        elif data.startswith("wc_paid_yes_"):
            order_id = tail_int(data)
            result   = wc_put(f"orders/{order_id}", {"status": "completed"})
            if result and result.get("status") == "completed":
                order   = wc_get(f"orders/{order_id}") or {}
                billing = order.get("billing", {})
                email   = billing.get("email", "")
                name    = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or "প্রিয় গ্রাহক"
                phone   = billing.get("phone", "")
                subs    = get_subscriptions_by_email(email) if email else []
                if subs:
                    sub        = subs[0]
                    sub_id     = sub.get("id")
                    items      = sub.get("line_items", [])
                    item_names = ", ".join([i.get("name", "?") for i in items]) or "Subscription"
                    sub_billing = sub.get("billing", {})
                    s_name  = f"{sub_billing.get('first_name','')} {sub_billing.get('last_name','')}".strip() or name
                    s_phone = sub_billing.get("phone", "") or phone
                    next_dt = add_one_month(parse_wc_dt(sub.get("next_payment_date_gmt", "")))
                    wp_subscription_renew(sub_id, next_dt, status="active")
                    next_show = next_dt.strftime("%d/%m/%Y")
                    if s_phone:
                        send_fonnte_wa(s_phone,
                            f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                            f"হ্যালো *{s_name}*! 🎉\n\nআপনার subscription সফলভাবে *চালু* হয়েছে!\n\n"
                            f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_show}*\n\n🙏 ধন্যবাদ!" + wa_footer()
                        )
                    await query.edit_message_text(f"✅ Order #{order_id} complete! Subscription active। WA পাঠানো হয়েছে।")
                else:
                    await query.edit_message_text(f"✅ Order #{order_id} complete!")
            else:
                await query.edit_message_text("❌ Update হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

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
                    sub         = subs[0]
                    sub_id      = sub.get("id")
                    items       = sub.get("line_items", [])
                    item_names  = ", ".join([i.get("name", "?") for i in items]) or "Subscription"
                    sub_billing = sub.get("billing", {})
                    s_name  = f"{sub_billing.get('first_name','')} {sub_billing.get('last_name','')}".strip() or name
                    s_phone = sub_billing.get("phone", "") or phone
                    next_dt = add_one_month(parse_wc_dt(sub.get("next_payment_date_gmt", "")))
                    wp_subscription_renew(sub_id, next_dt, status="active")
                    next_show = next_dt.strftime("%d/%m/%Y")
                    upsert_sub_payment_due(sub_id, s_name, email, s_phone, item_names, next_dt)
                    if s_phone:
                        send_fonnte_wa(s_phone,
                            f"━━━━━━━━━━━━━━━━━━\n⚠️ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                            f"হ্যালো *{s_name}*!\n\nআপনার subscription *চালু* হয়েছে!\n\n"
                            f"📦 Service: *{item_names}*\n📅 পরবর্তী Renewal: *{next_show}*\n\n"
                            f"💳 Payment এখনো বাকি আছে।" + wa_footer()
                        )
                    kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{sub_id}")]]
                    await query.edit_message_text(
                        f"💰 Subscription #{sub_id} active কিন্তু payment বাকি।\n"
                        f"👤 {s_name}\n📦 {item_names}\n📅 Next: {next_show}",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                else:
                    await query.edit_message_text("✅ Order complete! (Subscription পাওয়া যায়নি)")
            else:
                await query.edit_message_text("❌ Update হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        elif data.startswith("wc_setstatus_"):
            parts     = data.split("_")
            order_id  = int(parts[2])
            new_status = parts[3]

            if new_status == "completed":
                keyboard = [
                    [InlineKeyboardButton("✅ টাকা পেয়েছি",  callback_data=f"wc_paid_yes_{order_id}")],
                    [InlineKeyboardButton("❌ এখনো পাইনি", callback_data=f"wc_paid_no_{order_id}")]
                ]
                await query.edit_message_text(f"💳 Order #{order_id} — টাকা পেয়েছো?", reply_markup=InlineKeyboardMarkup(keyboard))
                return

            result = wc_put(f"orders/{order_id}", {"status": new_status})
            if result and result.get("status") == new_status:
                # Pending হলে client কে WA পাঠাও + order_payment_due এ রাখো
                if new_status == "pending":
                    order = wc_get(f"orders/{order_id}")
                    if order:
                        billing    = order.get("billing", {})
                        phone      = billing.get("phone", "")
                        name       = billing.get("first_name", "প্রিয় গ্রাহক")
                        items      = order.get("line_items", [])
                        item_names = ", ".join([i.get("name", "?") for i in items])
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
                                text=f"💰 *Order Payment Due*\n\nOrder #{order_id}\n👤 {name}\n📦 {item_names}\n💵 ৳{total}",
                                reply_markup=InlineKeyboardMarkup(kb),
                                parse_mode="Markdown"
                            )
                await query.edit_message_text(
                    f"✅ *Order #{order_id}* → *{new_status}*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Update হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        elif data.startswith("order_due_paid_"):
            order_id = data.split("_")[3]
            conn = get_db()
            try:
                conn.run("UPDATE order_payment_due SET cleared_at=NOW() WHERE woo_order_id=:oid", oid=str(order_id))
            finally:
                conn.close()
            await query.edit_message_text(
                f"✅ *Order #{order_id}* — Payment due বন্ধ।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

        # ── Menu navigation ───────────────────────────────────────────
        if data == "today_orders":       await show_orders(query, days=1)
        elif data == "week_orders":      await show_orders(query, days=7)
        elif data == "today_income":     await show_income(query, days=1)
        elif data == "month_report":     await show_month_report(query)
        elif data == "active_orders":    await show_active_orders(query)
        elif data == "manual_income":
            await query.edit_message_text("💰 Format: `/income 500 bkash e paisi`", parse_mode="Markdown")
        elif data == "search_customer":
            await query.edit_message_text("🔍 Format: `/customer example@email.com`", parse_mode="Markdown")
        elif data == "menu":
            await query.edit_message_text(
                "🛍️ *FD Assistant*\n\nMenu theke kaj koro ba seedha bolo:",
                reply_markup=main_menu_keyboard(), parse_mode="Markdown"
            )
        elif data.startswith("status_"):
            await show_status_options(query, data.split("_")[1])
        elif data.startswith("setstatus_"):
            parts = data.split("_")
            await update_order_status_btn(query, parts[1], parts[2])

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
# SHOW HELPERS
# =============================================

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn  = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,total,status FROM orders WHERE created_at>=:s ORDER BY created_at DESC",
            s=since
        )
    finally:
        conn.close()

    if not rows:
        await query.edit_message_text("📦 Kono order nei.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return

    text     = f"📦 *Last {days} diner orders ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_active_orders(query):
    all_orders = []
    for status in ["pending", "processing", "on-hold"]:
        orders = wc_get("orders", {"status": status, "per_page": 20})
        if orders and isinstance(orders, list):
            all_orders.extend(orders)
    all_orders.sort(key=lambda x: x.get("date_created", ""), reverse=True)

    if not all_orders:
        await query.edit_message_text("📋 কোনো active order নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
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


async def show_income(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn  = get_db()
    try:
        rows = conn.run("SELECT SUM(amount),COUNT(*) FROM income WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    label = "Aajker" if days == 1 else f"Last {days} diner"
    await query.edit_message_text(
        f"💰 *{label} Income*\n\nMot: ৳{rows[0][0] or 0}\nEntry: {rows[0][1] or 0}ta",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown"
    )


async def show_month_report(query):
    since = datetime.now() - timedelta(days=30)
    conn  = get_db()
    try:
        o = conn.run("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        i = conn.run("SELECT COALESCE(SUM(amount),0) FROM income WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    await query.edit_message_text(
        f"📊 *Last 30 দিনের Report*\n\n"
        f"🌐 WooCommerce: {o[0][0] or 0}টা | ৳{o[0][1] or 0}\n\n"
        f"💰 Total Income: ৳{i[0][0] or 0}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown"
    )


async def show_status_options(query, order_id):
    keyboard = [
        [InlineKeyboardButton("⏳ Processing",      callback_data=f"setstatus_{order_id}_processing")],
        [InlineKeyboardButton("✅ Completed",        callback_data=f"setstatus_{order_id}_completed")],
        [InlineKeyboardButton("💳 Payment Pending", callback_data=f"setstatus_{order_id}_pending")],
        [InlineKeyboardButton("❌ Cancelled",        callback_data=f"setstatus_{order_id}_cancelled")],
        [InlineKeyboardButton("🔙 Back",             callback_data="today_orders")]
    ]
    await query.edit_message_text(f"✏️ Order #{order_id} এর নতুন status:", reply_markup=InlineKeyboardMarkup(keyboard))


async def update_order_status_btn(query, order_id, new_status):
    ok, result = db_update_order_status(order_id, new_status)
    if ok:
        await query.edit_message_text(
            f"✅ Order #{result} — *{new_status}*!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(f"❌ {result}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

# =============================================
# COMMANDS
# =============================================

async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /income [taka] [note]")
        return
    try:
        amount = float(context.args[0])
        note   = " ".join(context.args[1:]) if len(context.args) > 1 else "Manual entry"
        db_add_income(amount, note)
        await update.message.reply_text(f"✅ ৳{amount} income add!\n📝 {note}")
    except Exception:
        await update.message.reply_text("❌ Vul format!")


async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /customer [email]")
        return
    email = context.args[0].lower()
    conn  = get_db()
    try:
        rows = conn.run(
            "SELECT woo_order_id,customer_name,total,status,created_at FROM orders "
            "WHERE LOWER(customer_email)=:e ORDER BY created_at DESC",
            e=email
        )
    finally:
        conn.close()

    wc_orders = wc_get("orders", {"search": email, "per_page": 20})
    wc_list   = []
    if wc_orders and isinstance(wc_orders, list):
        wc_list = [o for o in wc_orders if o.get("billing", {}).get("email", "").lower() == email.lower()]

    if not rows and not wc_list:
        await update.message.reply_text(f"❌ {email} এ কোনো order নেই।")
        return

    db_ids       = set()
    text         = f"👤 *{rows[0][1] if rows else email}*\n📧 {email}\n\n"
    total_spent  = 0

    if rows:
        text += "📦 *DB Orders:*\n"
        for o in rows:
            emoji = "✅" if o[3] == "completed" else "⏳" if o[3] == "processing" else "❌"
            text += f"{emoji} #{o[0]} — {o[4].strftime('%d %b %Y')} | ৳{o[2]} | {o[3]}\n"
            total_spent += float(o[2])
            db_ids.add(str(o[0]))

    extra = [o for o in wc_list if str(o["id"]) not in db_ids]
    if extra:
        text += "\n🌐 *WooCommerce Orders:*\n"
        for o in extra:
            status = o.get("status", "?")
            emoji  = "✅" if status == "completed" else "⏳" if status == "processing" else "❌"
            text  += f"{emoji} #{o['id']} — ৳{o['total']} | {status}\n"
            total_spent += float(o.get("total", 0))

    text += f"\n💰 *মোট: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")


async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📋 Format: `/sub email@gmail.com`", parse_mode="Markdown")
        return

    email = context.args[0].lower().strip()
    await update.message.reply_text(f"🔍 `{email}` এর subscriptions খুঁজছি...", parse_mode="Markdown")
    subs  = get_subscriptions_by_email(email)

    if not subs:
        await update.message.reply_text(
            f"❌ `{email}` এ কোনো subscription নেই।\n\nনতুন create করতে:\n`/newsub {email} \"Name\" phone`",
            parse_mode="Markdown"
        )
        return

    text     = f"📋 *{email} এর Subscriptions ({len(subs)}টা):*\n\n"
    keyboard = []
    for sub in subs:
        sub_id = sub.get("id")
        status = sub.get("status", "unknown")
        text  += format_subscription_text(sub) + "\n"

        row = []
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


async def new_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.split(None, 1)
    if len(raw) < 2:
        await update.message.reply_text(
            '📋 Format:\n`/newsub email "Full Name" phone`\n\nExample:\n`/newsub john@gmail.com "John Doe" 01712345678`',
            parse_mode="Markdown"
        )
        return

    args_str = raw[1].strip()
    parts    = args_str.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text('❌ Format ঠিক নেই!', parse_mode="Markdown")
        return

    email = parts[0].lower().strip()
    rest  = parts[1].strip()

    if rest.startswith('"'):
        end_quote = rest.find('"', 1)
        if end_quote == -1:
            await update.message.reply_text('❌ Name এর শেষে `"` দাও!', parse_mode="Markdown")
            return
        full_name  = rest[1:end_quote].strip()
        after_name = rest[end_quote + 1:].strip().split()
    else:
        after_name_parts = rest.split()
        full_name  = after_name_parts[0] if after_name_parts else ""
        after_name = after_name_parts[1:] if len(after_name_parts) > 1 else []

    if not after_name:
        await update.message.reply_text('❌ Phone number দাও!', parse_mode="Markdown")
        return

    phone  = after_name[0].strip()
    coupon = after_name[1].strip().upper() if len(after_name) > 1 else None

    if "@" not in email:
        await update.message.reply_text("❌ Valid email দাও!")
        return
    if len(phone) < 10:
        await update.message.reply_text("❌ Valid phone number দাও!")
        return

    name_parts = full_name.split(None, 1)
    first_name = name_parts[0] if name_parts else email.split("@")[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else "Customer"

    context.user_data.update({
        "newsub_email": email, "newsub_phone": phone,
        "newsub_first_name": first_name, "newsub_last_name": last_name,
        "newsub_full_name": full_name, "newsub_coupon": coupon
    })

    coupon_text = f"\n🎟️ Coupon: `{coupon}`" if coupon else ""
    await update.message.reply_text(
        f"✅ Client details নেওয়া হয়েছে!\n\n👤 Name: `{full_name}`\n📧 Email: `{email}`\n📱 Phone: `{phone}`{coupon_text}\n\n⏳ Products fetch করছি...",
        parse_mode="Markdown"
    )

    products = fetch_subscription_products()
    if not products:
        await update.message.reply_text("❌ কোনো subscription product পাওয়া যায়নি।")
        return

    keyboard = [[InlineKeyboardButton(f"📦 {p['name']}", callback_data=f"newsub_prod_{p['id']}")] for p in products]
    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    await update.message.reply_text(
        "🛍️ *কোন product এর subscription create করবে?*",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


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

    sub = context.args[0].lower()
    context.args = context.args[1:]

    if sub == "add":
        if not context.args:
            await update.message.reply_text("Format: `/paylater add email`", parse_mode="Markdown")
            return
        email  = context.args[0].lower().strip()
        result = _wp_paylater_api("POST", "add", email)
        await update.message.reply_text(
            f"✅ Pay Later চালু!\n📧 `{email}`" if result.get("success") else f"❌ Error: {result.get('message', 'Unknown')}",
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

        order_id      = str(data.get("id", "N/A"))
        customer      = data.get("billing", {})
        customer_name = f"{customer.get('first_name','')} {customer.get('last_name','')}".strip() or "Unknown"
        customer_email = customer.get("email", "")
        total         = float(data.get("total", 0))
        status        = data.get("status", "pending")
        items_text    = ", ".join([f"{i['name']} x{i['quantity']}" for i in data.get("line_items", [])])

        conn = get_db()
        try:
            conn.run(
                "INSERT INTO orders (woo_order_id,customer_name,customer_email,total,status,items) "
                "VALUES (:o,:n,:e,:t,:s,:i)",
                o=order_id, n=customer_name, e=customer_email, t=total, s=status, i=items_text
            )
            conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'auto')",
                     a=total, n=f"WooCommerce Order #{order_id}")
        finally:
            conn.close()

        msg = (
            f"🛍️ *নতুন WooCommerce Order!*\n\n"
            f"📋 #{order_id}\n👤 {customer_name}\n📧 {customer_email}\n"
            f"📦 {items_text}\n💵 ৳{total}\n📊 {status}"
        )
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
    bot_app.add_handler(CommandHandler("start",      start))
    bot_app.add_handler(CommandHandler("income",     income_command))
    bot_app.add_handler(CommandHandler("customer",   customer_command))
    bot_app.add_handler(CommandHandler("sub",        subscription_command))
    bot_app.add_handler(CommandHandler("newsub",     new_subscription_command))
    bot_app.add_handler(CommandHandler("paylater",   paylater_command))
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
