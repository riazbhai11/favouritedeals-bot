import os
import logging
import pg8000.native
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
MAIN_CHAT_ID = os.environ.get("CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WC_KEY = os.environ.get("WC_KEY")
WC_SECRET = os.environ.get("WC_SECRET")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
RESELLER_BOT_TOKEN = os.environ.get("RESELLER_BOT_TOKEN")
BKASH_NUMBER = os.environ.get("BKASH_NUMBER", "01997806925")
NAGAD_NUMBER = os.environ.get("NAGAD_NUMBER", "01997806925")
WP_URL = os.environ.get("WP_URL", "https://favouritedeals.online")
WP_PAYLATER_SECRET = os.environ.get("WP_PAYLATER_SECRET", "")

FONNTE_TOKEN = "8oSaMqEDoyw8Bk94Ctbv"
FD_MAIN_WHATSAPP = "01781678471"
FD_WEBSITE = "favouritedeals.online"

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

STATUS_PENDING = "pending"
STATUS_ACCOUNT_DELIVERED = "account_delivered"
STATUS_PAYMENT_DUE = "payment_due"
STATUS_COMPLETED = "completed"
STATUS_REJECTED = "rejected"

app = Flask(__name__)
main_loop = None
user_conversations = {}
WAITING_CODE = 1

PRODUCTS = {
    "chatgpt": {"name": "ChatGPT Plus Business (1 Month)", "price": 199},
    "gemini": {"name": "Gemini Advanced (1 Month)", "price": 850},
}


def send_fonnte_wa(phone, message):
    digits = re.sub(r"[^0-9]", "", phone)
    if digits.startswith("880"):
        digits = "0" + digits[3:]
    if digits.startswith("88"):
        digits = "0" + digits[2:]
    if not digits.startswith("0"):
        digits = "0" + digits
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

        conn.run("""CREATE TABLE IF NOT EXISTS resellers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200),
            phone VARCHAR(50),
            reseller_code VARCHAR(20),
            telegram_chat_id VARCHAR(50),
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS reseller_orders (
            id SERIAL PRIMARY KEY,
            reseller_id INTEGER REFERENCES resellers(id),
            product TEXT,
            quantity INTEGER,
            price DECIMAL(10,2),
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS reseller_bot_orders (
            id SERIAL PRIMARY KEY,
            reseller_id INTEGER REFERENCES resellers(id),
            reseller_code VARCHAR(20),
            product VARCHAR(100),
            customer_email VARCHAR(200),
            transaction_id VARCHAR(100),
            payment_method VARCHAR(20) DEFAULT 'bkash',
            amount DECIMAL(10,2),
            status VARCHAR(30) DEFAULT 'pending',
            reject_reason TEXT,
            payment_reminder_count INTEGER DEFAULT 0,
            account_delivered_at TIMESTAMP,
            payment_due_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW())""")

        for col, definition in [
            ("payment_method", "VARCHAR(20) DEFAULT 'bkash'"),
            ("payment_reminder_count", "INTEGER DEFAULT 0"),
            ("account_delivered_at", "TIMESTAMP"),
            ("payment_due_at", "TIMESTAMP"),
            ("completed_at", "TIMESTAMP"),
        ]:
            try:
                conn.run(f"ALTER TABLE reseller_bot_orders ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass

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

        for col, definition in [
            ("customer_name", "VARCHAR(200)"),
            ("customer_email", "VARCHAR(200)"),
            ("customer_phone", "VARCHAR(50)"),
            ("item_names", "TEXT"),
            ("next_payment_at", "TIMESTAMP"),
            ("reminder_count", "INTEGER DEFAULT 0"),
            ("last_reminded_at", "TIMESTAMP"),
            ("created_at", "TIMESTAMP DEFAULT NOW()"),
            ("cleared_at", "TIMESTAMP"),
        ]:
            try:
                conn.run(f"ALTER TABLE sub_payment_due ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass

        try:
            conn.run("CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_payment_due_sub_id ON sub_payment_due (sub_id)")
        except Exception:
            pass

        conn.run("""CREATE TABLE IF NOT EXISTS bot_memory (
            id SERIAL PRIMARY KEY,
            key VARCHAR(200) UNIQUE,
            value TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW())""")
    finally:
        conn.close()

    logger.info("✅ Database setup complete!")


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


def db_get_reseller_summary(reseller_name=None):
    conn = get_db()
    try:
        q = """
            SELECT r.name, r.phone, r.reseller_code,
                   COUNT(DISTINCT ro.id) AS manual_orders,
                   COALESCE(SUM(ro.price * ro.quantity), 0) AS manual_total,
                   COUNT(DISTINCT rbo.id) AS bot_orders,
                   COALESCE(SUM(rbo.amount), 0) AS bot_total
            FROM resellers r
            LEFT JOIN reseller_orders ro
                ON r.id=ro.reseller_id
                AND ro.created_at >= date_trunc('month', NOW())
            LEFT JOIN reseller_bot_orders rbo
                ON r.id=rbo.reseller_id
                AND rbo.created_at >= date_trunc('month', NOW())
                AND rbo.status != 'rejected'
        """
        if reseller_name:
            rows = conn.run(
                q + " WHERE UPPER(r.name) LIKE UPPER(:n) OR UPPER(r.reseller_code) LIKE UPPER(:n)"
                    " GROUP BY r.id,r.name,r.phone,r.reseller_code",
                n=f"%{reseller_name}%"
            )
        else:
            rows = conn.run(q + " GROUP BY r.id,r.name,r.phone,r.reseller_code")
    finally:
        conn.close()

    result = []
    for r in rows:
        total_orders = (r[3] or 0) + (r[5] or 0)
        total_amount = float(r[4] or 0) + float(r[6] or 0)
        result.append({
            "name": r[0], "phone": r[1], "code": r[2],
            "orders": total_orders, "total": f"৳{total_amount:.0f}",
            "manual_orders": r[3] or 0, "bot_orders": r[5] or 0
        })
    return result


def db_get_payment_due_summary():
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT rbo.reseller_code, r.name, COUNT(rbo.id), SUM(rbo.amount)
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id = rbo.reseller_id
            WHERE rbo.status = 'payment_due'
            GROUP BY rbo.reseller_code, r.name
            ORDER BY SUM(rbo.amount) DESC
        """)
    finally:
        conn.close()
    return [{
        "reseller_code": r[0], "name": r[1] or "Unknown",
        "due_orders": r[2] or 0, "due_amount": f"৳{float(r[3] or 0):.0f}"
    } for r in rows]


def db_get_today_reseller_bot_orders():
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = conn.run("""
            SELECT rbo.id, r.name, rbo.reseller_code, rbo.product,
                   rbo.customer_email, rbo.amount, rbo.status,
                   rbo.transaction_id, rbo.payment_method, rbo.created_at
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id=rbo.reseller_id
            WHERE rbo.created_at >= :s ORDER BY rbo.created_at DESC
        """, s=since)
    finally:
        conn.close()
    return [{
        "id": r[0], "reseller_name": r[1], "reseller_code": r[2], "product": r[3],
        "email": r[4], "amount": f"৳{r[5]}", "status": r[6],
        "txn": r[7], "payment_method": r[8], "created_at": str(r[9])
    } for r in rows]


def db_get_combined_today_summary():
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        woo = conn.run("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        res = conn.run(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM reseller_bot_orders "
            "WHERE created_at>=:s AND status != 'rejected'",
            s=since
        )
        res_detail = conn.run("""
            SELECT rbo.reseller_code, r.name, rbo.product, rbo.amount, rbo.status, rbo.created_at
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id=rbo.reseller_id
            WHERE rbo.created_at>=:s ORDER BY rbo.created_at DESC
        """, s=since)
    finally:
        conn.close()

    woo_count = woo[0][0] or 0
    woo_total = float(woo[0][1] or 0)
    res_count = res[0][0] or 0
    res_total = float(res[0][1] or 0)
    detail_list = [{
        "reseller_code": r[0], "reseller_name": r[1], "product": r[2],
        "amount": f"৳{r[3]}", "status": r[4], "time": str(r[5])
    } for r in res_detail]

    return {
        "woocommerce_orders": woo_count,
        "woocommerce_revenue": f"৳{woo_total:.0f}",
        "reseller_bot_orders": res_count,
        "reseller_bot_revenue": f"৳{res_total:.0f}",
        "total_orders": woo_count + res_count,
        "total_revenue": f"৳{woo_total + res_total:.0f}",
        "reseller_order_details": detail_list
    }


def get_reseller_bot_order(order_id):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,reseller_code,product,customer_email,amount,status,"
            "transaction_id,payment_method,payment_reminder_count "
            "FROM reseller_bot_orders WHERE id=:id",
            id=order_id
        )
    finally:
        conn.close()
    if rows:
        return {
            "id": rows[0][0], "reseller_code": rows[0][1], "product": rows[0][2],
            "customer_email": rows[0][3], "amount": str(rows[0][4]), "status": rows[0][5],
            "transaction_id": rows[0][6], "payment_method": rows[0][7], "reminder_count": rows[0][8]
        }
    return None


def get_reseller_by_chat_id(chat_id):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,name,phone,reseller_code FROM resellers WHERE telegram_chat_id=:c",
            c=str(chat_id)
        )
    finally:
        conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2], "code": rows[0][3]}
    return None


def get_reseller_by_code(code):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,name,phone FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
            c=code
        )
    finally:
        conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2]}
    return None


def get_payment_due_orders():
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT id, reseller_code, product, customer_email, amount,
                   payment_reminder_count, payment_due_at
            FROM reseller_bot_orders WHERE status = 'payment_due'
        """)
    finally:
        conn.close()
    return [{
        "id": r[0], "reseller_code": r[1], "product": r[2], "customer_email": r[3],
        "amount": str(r[4]), "reminder_count": r[5], "due_at": str(r[6])
    } for r in rows]


def upsert_sub_payment_due(sub_id, customer_name, customer_email, customer_phone, item_names, next_payment_at):
    conn = get_db()
    try:
        conn.run("""
            INSERT INTO sub_payment_due
            (sub_id, customer_name, customer_email, customer_phone, item_names, next_payment_at,
             reminder_count, last_reminded_at, cleared_at)
            VALUES (:sid, :n, :e, :p, :items, :nxt, 0, NOW(), NULL)
            ON CONFLICT (sub_id) DO UPDATE SET
                customer_name=:n,
                customer_email=:e,
                customer_phone=:p,
                item_names=:items,
                next_payment_at=:nxt,
                reminder_count=0,
                last_reminded_at=NOW(),
                cleared_at=NULL
        """, sid=sub_id, n=customer_name, e=customer_email, p=customer_phone, items=item_names, nxt=next_payment_at)
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
            FROM sub_payment_due
            WHERE cleared_at IS NULL
        """)
    finally:
        conn.close()
    return [{
        "sub_id": r[0],
        "customer_name": r[1],
        "customer_email": r[2],
        "customer_phone": r[3],
        "item_names": r[4],
        "next_payment_at": r[5],
        "reminder_count": r[6] or 0,
        "last_reminded_at": r[7]
    } for r in rows]


def mark_sub_due_reminded(sub_id):
    conn = get_db()
    try:
        conn.run("""
            UPDATE sub_payment_due
            SET reminder_count = reminder_count + 1,
                last_reminded_at = NOW()
            WHERE sub_id = :sid
        """, sid=sub_id)
    finally:
        conn.close()


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


def fetch_subscription_products():
    products = []
    for product_id, display_name in SUBSCRIPTION_PRODUCTS.items():
        try:
            p = wc_get(f"products/{product_id}")
            if p and "id" in p:
                products.append({
                    "id": p["id"],
                    "name": display_name,
                    "price": p.get("price", "0"),
                    "variation_ids": p.get("variations", []),
                    "slug": p.get("slug", "")
                })
            else:
                products.append({
                    "id": product_id,
                    "name": display_name,
                    "price": "?",
                    "variation_ids": [],
                    "slug": ""
                })
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
                desc = re.sub(r"<[^>]+>", "", v.get("description", "")).strip()
                desc = desc[:100] if len(desc) > 100 else desc
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
        "email": email,
        "username": username,
        "password": "Temp@" + str(abs(hash(email)))[-6:],
        "first_name": first_name,
        "last_name": last_name,
        "billing": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "country": "BD"
        }
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
        "customer_id": customer_id,
        "status": "pending",
        "billing_period": "month",
        "billing_interval": 1,
        "payment_method": "bacs",
        "payment_method_title": "Manual Payment (bKash/Nagad)",
        "billing": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "country": "BD"
        },
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
        return create_order_fallback(
            email, phone, first_name, last_name,
            product_id, variation_id, variation_attributes, customer_id, coupon
        )
    return subscription, None


def create_order_fallback(email, phone, first_name, last_name, product_id, variation_id, variation_attributes, customer_id, coupon=None):
    line_item = {"product_id": product_id, "quantity": 1}
    if variation_id:
        line_item["variation_id"] = variation_id
        if variation_attributes:
            line_item["meta_data"] = [{"key": f"attribute_{k}", "value": v} for k, v in variation_attributes.items()]

    order_body = {
        "customer_id": customer_id,
        "payment_method": "bacs",
        "payment_method_title": "Manual Payment (bKash/Nagad)",
        "set_paid": False,
        "billing": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "country": "BD"
        },
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
    year = dt.year
    month = dt.month + 1
    if month > 12:
        month = 1
        year += 1
    month_days = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31
    ]
    day = min(dt.day, month_days[month - 1])
    return dt.replace(year=year, month=month, day=day)


def wc_set_bot_controlled(sub_id, enabled=True):
    sub = wc_get(f"subscriptions/{sub_id}")
    if not sub:
        return None

    meta_payload = []
    found = False

    for meta in sub.get("meta_data", []):
        if meta.get("key") == "_fd_bot_controlled":
            item = {
                "key": "_fd_bot_controlled",
                "value": "yes" if enabled else "no"
            }
            if meta.get("id"):
                item["id"] = meta["id"]
            meta_payload.append(item)
            found = True
            break

    if not found:
        meta_payload.append({
            "key": "_fd_bot_controlled",
            "value": "yes" if enabled else "no"
        })

    return wc_put(f"subscriptions/{sub_id}", {"meta_data": meta_payload})


def wc_update_subscription_next_payment(sub_id, next_dt):
    return wc_put(f"subscriptions/{sub_id}", {
        "next_payment_date_gmt": next_dt.strftime("%Y-%m-%d %H:%M:%S")
    })


SUB_STATUS_EMOJI = {
    "active": "✅",
    "on-hold": "⏸️",
    "cancelled": "❌",
    "expired": "⌛",
    "pending": "🕐"
}

SUB_STATUS_LABEL = {
    "active": "Active — চালু আছে",
    "on-hold": "Paused — বন্ধ আছে",
    "cancelled": "Cancelled",
    "expired": "Expired",
    "pending": "Pending"
}


def format_subscription_text(sub):
    sub_id = sub.get("id", "?")
    status = sub.get("status", "unknown")
    emoji = SUB_STATUS_EMOJI.get(status, "❓")
    label = SUB_STATUS_LABEL.get(status, status)
    total = sub.get("total", "0")
    next_date = sub.get("next_payment_date_gmt", "")
    items = sub.get("line_items", [])
    item_names = ", ".join([i.get("name", "?") for i in items])

    text = f"{emoji} *Subscription #{sub_id}*\n"
    text += f"   📦 {item_names}\n"
    text += f"   💵 ৳{total}\n"
    text += f"   Status: {label}\n"
    if next_date:
        text += f"   📅 Next Payment: {next_date[:10]}\n"
    return text


async def notify_reseller(reseller_code, message, parse_mode="Markdown"):
    try:
        conn = get_db()
        try:
            rows = conn.run(
                "SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                c=reseller_code
            )
        finally:
            conn.close()

        if rows and rows[0][0]:
            from telegram import Bot
            await Bot(token=RESELLER_BOT_TOKEN).send_message(
                chat_id=rows[0][0],
                text=message,
                parse_mode=parse_mode
            )
    except Exception as e:
        logger.error(f"Reseller notify error [{reseller_code}]: {e}")


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

    billing = sub.get("billing", {})
    client_name = billing.get("first_name", "প্রিয় গ্রাহক")
    client_email = billing.get("email", "")
    client_phone = billing.get("phone", "")
    items = sub.get("line_items", [])
    item_names = ", ".join([i.get("name", "?") for i in items]) or "Subscription"

    base_dt = parse_wc_dt(sub.get("next_payment_date_gmt", ""))
    if base_dt < datetime.utcnow():
        base_dt = datetime.utcnow()

    next_dt = custom_dt if custom_dt else add_one_month(base_dt)
    next_show = next_dt.strftime("%d/%m/%Y")

    result = None
    try:
        wc_set_bot_controlled(sub_id, True)
        wc_update_subscription_next_payment(sub_id, next_dt)
        result = wc_put(f"subscriptions/{sub_id}", {"status": "active"})
    finally:
        wc_set_bot_controlled(sub_id, False)

    if not result or result.get("status") != "active":
        return False, "❌ Renew/Active হয়নি। WooCommerce dashboard এ check করো।"

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
            f"👤 {client_name}\n"
            f"📧 {client_email}\n"
            f"📦 {item_names}\n"
            f"📅 Next Renewal: {next_show}\n\n"
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


async def send_payment_reminders():
    while True:
        await asyncio.sleep(3 * 60 * 60)
        try:
            due_orders = get_payment_due_orders()
            for order in due_orders:
                conn = get_db()
                try:
                    conn.run(
                        "UPDATE reseller_bot_orders SET payment_reminder_count=payment_reminder_count+1 WHERE id=:id",
                        id=order["id"]
                    )
                finally:
                    conn.close()

                count = order["reminder_count"] + 1

                try:
                    conn3 = get_db()
                    try:
                        rr = conn3.run(
                            "SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                            c=order["reseller_code"]
                        )
                    finally:
                        conn3.close()

                    if rr and rr[0][0]:
                        from telegram import Bot as TBot
                        remind_keyboard = [[InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order['id']}")]]
                        await TBot(token=RESELLER_BOT_TOKEN).send_message(
                            chat_id=rr[0][0],
                            text=(
                                f"⏰ *Payment Reminder #{count}*\n\n"
                                f"Order #{order['id']} er account deliver hoye geche!\n"
                                f"Payment ekhono baaki.\n\n"
                                f"📦 {order['product']}\n"
                                f"💵 ৳{order['amount']}\n\n"
                                f"Payment korbo 👇"
                            ),
                            reply_markup=InlineKeyboardMarkup(remind_keyboard),
                            parse_mode="Markdown"
                        )
                except Exception as e:
                    logger.error(f"Reseller reminder error: {e}")
        except Exception as e:
            logger.error(f"Payment reminder loop error: {e}")


async def send_subscription_due_reminders():
    while True:
        await asyncio.sleep(60 * 60)
        try:
            rows = get_pending_sub_payment_due()
            now = datetime.utcnow()

            for row in rows:
                last = row["last_reminded_at"]
                if last and (now - last) < timedelta(hours=12):
                    continue

                next_show = row["next_payment_at"].strftime("%d/%m/%Y") if row["next_payment_at"] else "N/A"
                next_count = (row["reminder_count"] or 0) + 1

                send_subscription_due_wa(
                    row["customer_phone"],
                    row["customer_name"],
                    row["item_names"],
                    next_show,
                    count=next_count
                )
                mark_sub_due_reminded(row["sub_id"])

                from telegram import Bot
                kb = [[InlineKeyboardButton("✅ টাকা পেয়েছি", callback_data=f"sub_due_paid_{row['sub_id']}")]]
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=(
                        f"⏰ *Subscription Due Reminder #{next_count}*\n\n"
                        f"Subscription #{row['sub_id']}\n"
                        f"👤 {row['customer_name']}\n"
                        f"📦 {row['item_names']}"
                    ),
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Subscription due reminder loop error: {e}")


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 আজকের WooCommerce Order", callback_data="today_orders"),
         InlineKeyboardButton("💰 আজকের Income", callback_data="today_income")],
        [InlineKeyboardButton("📅 ৭ দিনের Order", callback_data="week_orders"),
         InlineKeyboardButton("📊 মাসের Report", callback_data="month_report")],
        [InlineKeyboardButton("👥 Reseller", callback_data="resellers"),
         InlineKeyboardButton("➕ Manual Income", callback_data="manual_income")],
        [InlineKeyboardButton("🔍 Customer খোঁজো", callback_data="search_customer"),
         InlineKeyboardButton("⏳ Pending Orders", callback_data="pending_orders")],
        [InlineKeyboardButton("🛍️ Reseller Bot Orders আজ", callback_data="reseller_bot_orders_today")],
        [InlineKeyboardButton("💸 Due বাকি (Reseller)", callback_data="due_baki")],
        [InlineKeyboardButton("📋 Subscription Check", callback_data="sub_check"),
         InlineKeyboardButton("➕ নতুন Subscription", callback_data="sub_new")],
    ])


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
    text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    if context.user_data.get("state") == "waiting_custom_renew_date":
        try:
            custom_dt = datetime.strptime(text, "%Y-%m-%d")
        except Exception:
            await update.message.reply_text("❌ Date format হবে `YYYY-MM-DD`", parse_mode="Markdown")
            return

        sub_id = context.user_data.get("custom_renew_sub_id")
        paid = context.user_data.get("custom_renew_paid", False)
        ok, msg = await process_subscription_renew_action(sub_id, paid, custom_dt=custom_dt)

        context.user_data["state"] = None
        context.user_data.pop("custom_renew_sub_id", None)
        context.user_data.pop("custom_renew_paid", None)

        await update.message.reply_text(msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return

    await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())


async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📋 Format: `/sub email@gmail.com`", parse_mode="Markdown")
        return

    email = context.args[0].lower().strip()
    await update.message.reply_text(f"🔍 `{email}` এর subscriptions খুঁজছি...", parse_mode="Markdown")
    subs = get_subscriptions_by_email(email)

    if not subs:
        await update.message.reply_text(
            f"❌ `{email}` এ কোনো subscription নেই।",
            parse_mode="Markdown"
        )
        return

    text = f"📋 *{email} এর Subscriptions ({len(subs)}টা):*\n\n"
    keyboard = []
    for sub in subs:
        sub_id = sub.get("id")
        status = sub.get("status", "unknown")
        text += format_subscription_text(sub) + "\n"

        row = []
        if status == "on-hold":
            row.append(InlineKeyboardButton(f"▶️ #{sub_id} Resume", callback_data=f"sub_resume_{sub_id}"))
            row.append(InlineKeyboardButton(f"❌ #{sub_id} Cancel", callback_data=f"sub_cancel_{sub_id}"))
        elif status in ["active", "pending"]:
            row.append(InlineKeyboardButton(f"⏸️ #{sub_id} Pause", callback_data=f"sub_pause_{sub_id}"))
            row.append(InlineKeyboardButton(f"🔄 #{sub_id} Renew", callback_data=f"sub_renew_{sub_id}"))
        elif status in ["cancelled", "expired"]:
            row.append(InlineKeyboardButton(f"▶️ #{sub_id} Reactivate", callback_data=f"sub_resume_{sub_id}"))

        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data == "sub_check":
            await query.edit_message_text(
                "📋 *Subscription Check*\n\nClient এর email দাও:\n`/sub email@gmail.com`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

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
                next_date = result.get("next_payment_date_gmt", "")
                next_show = next_date[:10] if next_date else "N/A"
                items = result.get("line_items", [])
                item_names = ", ".join([i.get("name", "?") for i in items])
                client_email = result.get("billing", {}).get("email", "")
                client_phone = result.get("billing", {}).get("phone", "")
                client_name = result.get("billing", {}).get("first_name", "প্রিয় গ্রাহক")

                if client_phone:
                    msg = (
                        f"━━━━━━━━━━━━━━━━━━\n✅ *Favourite Deals*\n━━━━━━━━━━━━━━━━━━\n\n"
                        f"হ্যালো *{client_name}*! 🎉\n\n"
                        f"আপনার subscription সফলভাবে *চালু* হয়েছে!\n\n"
                        f"📦 Service: *{item_names}*\n"
                        f"📅 পরবর্তী Renewal: *{next_show}*\n\n"
                        f"🙏 আমাদের বেছে নেওয়ার জন্য ধন্যবাদ!"
                        + wa_footer()
                    )
                    send_fonnte_wa(client_phone, msg)

                await query.edit_message_text(
                    f"✅ *Subscription #{sub_id} Resume হয়েছে!*\n\n"
                    f"📦 {item_names}\n📧 {client_email}\n📅 পরবর্তী Renewal: {next_show}\n\n"
                    f"{'✅ WhatsApp notification পাঠানো হয়েছে।' if client_phone else '⚠️ Phone নেই।'}",
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
                f"▶️ *Subscription #{sub_id} Resume*\n\nClient এর কাছ থেকে payment নিয়েছো?\n\nConfirm হলে Resume করো 👇",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_pause_confirm_"):
            sub_id = tail_int(data)
            result = wc_put(f"subscriptions/{sub_id}", {"status": "on-hold"})
            if result and result.get("status") == "on-hold":
                await query.edit_message_text(
                    f"⏸️ *Subscription #{sub_id} Paused!*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    "❌ Pause হয়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
            return

        elif data.startswith("sub_pause_"):
            sub_id = tail_int(data)
            keyboard = [[
                InlineKeyboardButton("⏸️ হ্যাঁ Pause করো", callback_data=f"sub_pause_confirm_{sub_id}"),
                InlineKeyboardButton("❌ না", callback_data="menu")
            ]]
            await query.edit_message_text(
                f"⏸️ *Subscription #{sub_id} Pause করবে?*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_renew_"):
            sub_id = tail_int(data)
            keyboard = [
                [InlineKeyboardButton("✅ হ্যাঁ পেয়েছি", callback_data=f"sub_paid_yes_{sub_id}")],
                [InlineKeyboardButton("❌ না পাইনি", callback_data=f"sub_paid_no_{sub_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu")]
            ]
            await query.edit_message_text(
                f"🔄 *Subscription #{sub_id} Renew*\n\nটাকা পেয়েছো?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_paid_yes_") or data.startswith("sub_paid_no_"):
            sub_id = tail_int(data)
            paid = data.startswith("sub_paid_yes_")
            mode = "paid" if paid else "due"

            keyboard = [
                [InlineKeyboardButton("📅 1 Month after", callback_data=f"sub_extend1m_{mode}_{sub_id}")],
                [InlineKeyboardButton("✍️ Custom Date", callback_data=f"sub_extendcustom_{mode}_{sub_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"sub_renew_{sub_id}")]
            ]
            await query.edit_message_text(
                f"📆 *Subscription #{sub_id}*\n\nNext date কতদিন বাড়াবে?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_extend1m_"):
            parts = data.split("_")
            paid = parts[2] == "paid"
            sub_id = int(parts[3])

            ok, msg = await process_subscription_renew_action(sub_id, paid, custom_dt=None)
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
            return

        elif data.startswith("sub_extendcustom_"):
            parts = data.split("_")
            paid = parts[2] == "paid"
            sub_id = int(parts[3])

            context.user_data["state"] = "waiting_custom_renew_date"
            context.user_data["custom_renew_sub_id"] = sub_id
            context.user_data["custom_renew_paid"] = paid

            await query.edit_message_text(
                f"📅 *Subscription #{sub_id}*\n\nCustom date দাও:\n`YYYY-MM-DD`\n\nExample: `2026-06-20`",
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
                await query.edit_message_text(
                    "❌ Cancel হয়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
            return

        elif data.startswith("sub_cancel_"):
            sub_id = tail_int(data)
            keyboard = [[
                InlineKeyboardButton("✅ হ্যাঁ Cancel করো", callback_data=f"sub_cancel_confirm_{sub_id}"),
                InlineKeyboardButton("❌ না", callback_data="menu")
            ]]
            await query.edit_message_text(
                f"⚠️ *Subscription #{sub_id} Cancel করবে?*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        elif data == "menu":
            await query.edit_message_text(
                "🛍️ *FD Assistant*\n\nMenu theke kaj koro ba seedha bolo:",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown"
            )
            return

    except Exception as e:
        logger.exception(f"button_handler error for data={data}: {e}")
        try:
            await query.edit_message_text(
                f"❌ Error হয়েছে:\n`{str(e)[:350]}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return


async def run_flask_app():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


async def main():
    global main_loop
    main_loop = asyncio.get_event_loop()
    setup_db()

    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))), daemon=True)
    flask_thread.start()

    main_app = Application.builder().token(BOT_TOKEN).build()
    main_app.add_handler(CommandHandler("start", start))
    main_app.add_handler(CommandHandler("sub", subscription_command))
    main_app.add_handler(CallbackQueryHandler(button_handler))
    main_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    reseller_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)]},
        fallbacks=[CommandHandler("start", start)]
    )

    reseller_app = Application.builder().token(RESELLER_BOT_TOKEN).build()
    reseller_app.add_handler(reseller_conv)

    logger.info("✅ Both bots started!")

    async with main_app, reseller_app:
        await main_app.initialize()
        await reseller_app.initialize()
        await main_app.start()
        await reseller_app.start()
        await main_app.updater.start_polling()
        await reseller_app.updater.start_polling()
        asyncio.create_task(send_payment_reminders())
        asyncio.create_task(send_subscription_due_reminders())
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
