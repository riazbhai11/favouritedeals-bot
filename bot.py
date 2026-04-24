import os
import logging
import pg8000.native
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                           MessageHandler, filters, ContextTypes, ConversationHandler)
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

# =================== CONFIG ===================

BOT_TOKEN          = os.environ.get("BOT_TOKEN")
CHAT_ID            = os.environ.get("CHAT_ID")
MAIN_CHAT_ID       = os.environ.get("CHAT_ID")
DATABASE_URL       = os.environ.get("DATABASE_URL")
WC_KEY             = os.environ.get("WC_KEY")
WC_SECRET          = os.environ.get("WC_SECRET")
OPENAI_KEY         = os.environ.get("OPENAI_API_KEY")
RESELLER_BOT_TOKEN = os.environ.get("RESELLER_BOT_TOKEN")
BKASH_NUMBER       = os.environ.get("BKASH_NUMBER", "01997806925")
NAGAD_NUMBER       = os.environ.get("NAGAD_NUMBER", "01997806925")
WP_URL             = os.environ.get("WP_URL", "https://favouritedeals.online")
WP_PAYLATER_SECRET = os.environ.get("WP_PAYLATER_SECRET", "")

# =================== SUBSCRIPTION PRODUCTS ===================
# WooCommerce product ID → display name
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

# Status constants
STATUS_PENDING            = "pending"
STATUS_ACCOUNT_DELIVERED  = "account_delivered"
STATUS_PAYMENT_DUE        = "payment_due"
STATUS_COMPLETED          = "completed"
STATUS_REJECTED           = "rejected"

app       = Flask(__name__)
main_loop = None
user_conversations = {}

WAITING_CODE = 1

PRODUCTS = {
    "chatgpt": {"name": "ChatGPT Plus Business (1 Month)", "price": 199},
    "gemini":  {"name": "Gemini Advanced (1 Month)",       "price": 850},
}

# =================== DATABASE ===================

def get_db():
    url = urlparse(DATABASE_URL)
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return pg8000.native.Connection(
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:], user=url.username,
        password=url.password, ssl_context=ctx
    )

def setup_db():
    conn = get_db()
    try:
        conn.run("""CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            woo_order_id   VARCHAR(50),
            customer_name  VARCHAR(200),
            customer_email VARCHAR(200),
            total          DECIMAL(10,2),
            status         VARCHAR(50),
            items          TEXT,
            created_at     TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS income (
            id         SERIAL PRIMARY KEY,
            amount     DECIMAL(10,2),
            note       TEXT,
            type       VARCHAR(20) DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS resellers (
            id               SERIAL PRIMARY KEY,
            name             VARCHAR(200),
            phone            VARCHAR(50),
            reseller_code    VARCHAR(20),
            telegram_chat_id VARCHAR(50),
            created_at       TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS reseller_orders (
            id          SERIAL PRIMARY KEY,
            reseller_id INTEGER REFERENCES resellers(id),
            product     TEXT,
            quantity    INTEGER,
            price       DECIMAL(10,2),
            created_at  TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS reseller_bot_orders (
            id                 SERIAL PRIMARY KEY,
            reseller_id        INTEGER REFERENCES resellers(id),
            reseller_code      VARCHAR(20),
            product            VARCHAR(100),
            customer_email     VARCHAR(200),
            transaction_id     VARCHAR(100),
            payment_method     VARCHAR(20)  DEFAULT 'bkash',
            amount             DECIMAL(10,2),
            status             VARCHAR(30)  DEFAULT 'pending',
            reject_reason      TEXT,
            payment_reminder_count INTEGER DEFAULT 0,
            account_delivered_at   TIMESTAMP,
            payment_due_at         TIMESTAMP,
            completed_at           TIMESTAMP,
            created_at         TIMESTAMP DEFAULT NOW())""")

        for col, definition in [
            ("payment_method",          "VARCHAR(20) DEFAULT 'bkash'"),
            ("payment_reminder_count",  "INTEGER DEFAULT 0"),
            ("account_delivered_at",    "TIMESTAMP"),
            ("payment_due_at",          "TIMESTAMP"),
            ("completed_at",            "TIMESTAMP"),
        ]:
            try:
                conn.run(f"ALTER TABLE reseller_bot_orders ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                pass

        conn.run("""CREATE TABLE IF NOT EXISTS bot_memory (
            id         SERIAL PRIMARY KEY,
            key        VARCHAR(200) UNIQUE,
            value      TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW())""")

    finally:
        conn.close()
    logger.info("✅ Database setup complete!")

# =================== MEMORY ===================

def memory_save(key, value):
    conn = get_db()
    try:
        conn.run(
            "INSERT INTO bot_memory (key, value, updated_at) VALUES (:k,:v,NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()",
            k=key, v=value)
    finally:
        conn.close()

def memory_get_all():
    conn = get_db()
    try:
        rows = conn.run("SELECT key, value, updated_at FROM bot_memory ORDER BY updated_at DESC")
    finally:
        conn.close()
    return [{"key": r[0], "value": r[1], "updated_at": str(r[2])} for r in rows]

# =================== DB HELPERS ===================

def db_get_recent_orders(limit=10, status=None):
    conn = get_db()
    try:
        if status:
            rows = conn.run(
                "SELECT id,woo_order_id,customer_name,customer_email,total,status,items,created_at "
                "FROM orders WHERE status=:s ORDER BY created_at DESC LIMIT :l", s=status, l=limit)
        else:
            rows = conn.run(
                "SELECT id,woo_order_id,customer_name,customer_email,total,status,items,created_at "
                "FROM orders ORDER BY created_at DESC LIMIT :l", l=limit)
    finally:
        conn.close()
    return [{"id":r[0],"woo_order_id":r[1],"customer_name":r[2],"customer_email":r[3],
             "total":str(r[4]),"status":r[5],"items":r[6],"created_at":str(r[7])} for r in rows]

def db_get_last_order():
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,customer_email,total,status,items,created_at "
            "FROM orders ORDER BY created_at DESC LIMIT 1")
    finally:
        conn.close()
    if rows:
        r = rows[0]
        return {"id":r[0],"woo_order_id":r[1],"customer_name":r[2],"customer_email":r[3],
                "total":str(r[4]),"status":r[5],"items":r[6],"created_at":str(r[7])}
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
        req.put(f"{WP_URL}/wp-json/wc/v3/orders/{woo_id}",
                json={"status": new_status}, auth=(WC_KEY, WC_SECRET), timeout=10)
    except Exception as e:
        logger.error(f"WC update error: {e}")
    return True, woo_id

def db_get_income_summary(days=1):
    conn = get_db()
    try:
        since = datetime.now() - timedelta(days=days)
        rows  = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    return {"total": str(rows[0][0] or 0), "count": rows[0][1] or 0}

def db_get_orders_summary(days=1):
    conn = get_db()
    try:
        since = datetime.now() - timedelta(days=days)
        rows  = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at>=:s", s=since)
    finally:
        conn.close()
    return {"count": rows[0][0] or 0, "total": str(rows[0][1] or 0)}

def db_search_orders_by_name(name):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,customer_email,total,status,created_at "
            "FROM orders WHERE LOWER(customer_name) LIKE :n ORDER BY created_at DESC LIMIT 5",
            n=f"%{name.lower()}%")
    finally:
        conn.close()
    return [{"id":r[0],"woo_order_id":r[1],"customer_name":r[2],"customer_email":r[3],
             "total":str(r[4]),"status":r[5],"created_at":str(r[6])} for r in rows]

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
                COUNT(DISTINCT ro.id)  AS manual_orders,
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
                n=f"%{reseller_name}%")
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
    return [{"reseller_code":r[0],"name":r[1] or "Unknown","due_orders":r[2] or 0,
             "due_amount":f"৳{float(r[3] or 0):.0f}"} for r in rows]

def db_get_today_reseller_bot_orders():
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows  = conn.run("""
            SELECT rbo.id, r.name, rbo.reseller_code, rbo.product,
                   rbo.customer_email, rbo.amount, rbo.status,
                   rbo.transaction_id, rbo.payment_method, rbo.created_at
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id=rbo.reseller_id
            WHERE rbo.created_at >= :s ORDER BY rbo.created_at DESC
        """, s=since)
    finally:
        conn.close()
    return [{"id":r[0],"reseller_name":r[1],"reseller_code":r[2],"product":r[3],
             "email":r[4],"amount":f"৳{r[5]}","status":r[6],
             "txn":r[7],"payment_method":r[8],"created_at":str(r[9])} for r in rows]

def db_get_combined_today_summary():
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        woo = conn.run("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        res = conn.run(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM reseller_bot_orders "
            "WHERE created_at>=:s AND status != 'rejected'", s=since)
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
    detail_list = [{"reseller_code":r[0],"reseller_name":r[1],"product":r[2],
                    "amount":f"৳{r[3]}","status":r[4],"time":str(r[5])} for r in res_detail]
    return {
        "woocommerce_orders":woo_count,"woocommerce_revenue":f"৳{woo_total:.0f}",
        "reseller_bot_orders":res_count,"reseller_bot_revenue":f"৳{res_total:.0f}",
        "total_orders":woo_count+res_count,"total_revenue":f"৳{woo_total+res_total:.0f}",
        "reseller_order_details":detail_list
    }

def get_reseller_bot_order(order_id):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,reseller_code,product,customer_email,amount,status,"
            "transaction_id,payment_method,payment_reminder_count "
            "FROM reseller_bot_orders WHERE id=:id", id=order_id)
    finally:
        conn.close()
    if rows:
        return {"id":rows[0][0],"reseller_code":rows[0][1],"product":rows[0][2],
                "customer_email":rows[0][3],"amount":str(rows[0][4]),"status":rows[0][5],
                "transaction_id":rows[0][6],"payment_method":rows[0][7],"reminder_count":rows[0][8]}
    return None

def get_reseller_by_chat_id(chat_id):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,name,phone,reseller_code FROM resellers WHERE telegram_chat_id=:c",
            c=str(chat_id))
    finally:
        conn.close()
    if rows:
        return {"id":rows[0][0],"name":rows[0][1],"phone":rows[0][2],"code":rows[0][3]}
    return None

def get_reseller_by_code(code):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,name,phone FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)", c=code)
    finally:
        conn.close()
    if rows:
        return {"id":rows[0][0],"name":rows[0][1],"phone":rows[0][2]}
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
    return [{"id":r[0],"reseller_code":r[1],"product":r[2],"customer_email":r[3],
             "amount":str(r[4]),"reminder_count":r[5],"due_at":str(r[6])} for r in rows]

# =================== WOOCOMMERCE API ===================

def wc_get(endpoint, params=None):
    try:
        resp = req.get(f"{WP_URL}/wp-json/wc/v3/{endpoint}",
                       auth=(WC_KEY, WC_SECRET), params=params or {}, timeout=15)
        return resp.json()
    except Exception as e:
        logger.error(f"WC GET error [{endpoint}]: {e}")
        return None

def wc_post_req(endpoint, data):
    try:
        resp = req.post(f"{WP_URL}/wp-json/wc/v3/{endpoint}",
                        auth=(WC_KEY, WC_SECRET), json=data, timeout=15)
        return resp.json()
    except Exception as e:
        logger.error(f"WC POST error [{endpoint}]: {e}")
        return None

def wc_put(endpoint, data):
    try:
        resp = req.put(f"{WP_URL}/wp-json/wc/v3/{endpoint}",
                       auth=(WC_KEY, WC_SECRET), json=data, timeout=15)
        return resp.json()
    except Exception as e:
        logger.error(f"WC PUT error [{endpoint}]: {e}")
        return None

# =================== DYNAMIC PRODUCT FETCHING ===================

def fetch_subscription_products():
    """
    Hardcoded product IDs থেকে WooCommerce এ real-time price fetch করো।
    """
    products = []
    for product_id, display_name in SUBSCRIPTION_PRODUCTS.items():
        try:
            p = wc_get(f"products/{product_id}")
            if p and "id" in p:
                price_range = ""
                if p.get("price"):
                    price_range = f" (৳{p['price']}+)"
                products.append({
                    "id": p["id"],
                    "name": display_name,
                    "price": p.get("price", "0"),
                    "price_range": price_range,
                    "variation_ids": p.get("variations", []),
                    "slug": p.get("slug", "")
                })
            else:
                # API fail হলেও list এ রাখো
                products.append({
                    "id": product_id,
                    "name": display_name,
                    "price": "?",
                    "price_range": "",
                    "variation_ids": [],
                    "slug": ""
                })
        except Exception as e:
            logger.error(f"Product fetch error [{product_id}]: {e}")
    return products

def fetch_product_variations(product_id):
    """একটা product এর সব variations fetch করো"""
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
                # Clean up HTML from description
                desc = re.sub(r'<[^>]+>', '', v.get("description", "")).strip()
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
    """Customer নাও অথবা বানাও"""
    # Already exist করে কিনা check করো
    existing = wc_get("customers", {"email": email})
    if existing and isinstance(existing, list) and len(existing) > 0:
        logger.info(f"Existing customer: {existing[0]['id']} for {email}")
        return existing[0]["id"], None

    # নতুন customer create করো
    username = (first_name + str(abs(hash(email)))[-4:]).lower().replace(" ", "")
    customer = wc_post_req("customers", {
        "email":      email,
        "username":   username,
        "password":   "Temp@" + str(abs(hash(email)))[-6:],  # temp password
        "first_name": first_name,
        "last_name":  last_name,
        "billing": {
            "first_name": first_name,
            "last_name":  last_name,
            "email":      email,
            "phone":      phone,
            "country":    "BD"
        }
    })
    if customer and "id" in customer:
        logger.info(f"New customer created: {customer['id']} for {email}")
        return customer["id"], None

    err = customer.get("message", "Customer create হয়নি") if customer else "Customer create হয়নি"
    return None, err


def create_subscription_directly(email, phone, first_name, last_name, product_id, variation_id, variation_attributes, coupon=None):
    """
    সরাসরি WooCommerce Subscription create করো।
    Payment হওয়ার পর manually activate করতে হবে।
    """
    customer_id, err = get_or_create_customer(email, phone, first_name, last_name)
    if err:
        return None, err

    # Line item
    line_item = {"product_id": product_id, "quantity": 1}
    if variation_id:
        line_item["variation_id"] = variation_id
        if variation_attributes:
            line_item["meta_data"] = [
                {"key": f"attribute_{k}", "value": v}
                for k, v in variation_attributes.items()
            ]

    # Subscription body
    sub_body = {
        "customer_id":          customer_id,
        "status":               "pending",
        "billing_period":       "month",
        "billing_interval":     1,
        "payment_method":       "bacs",
        "payment_method_title": "Manual Payment (bKash/Nagad)",
        "billing": {
            "first_name": first_name,
            "last_name":  last_name,
            "email":      email,
            "phone":      phone,
            "country":    "BD"
        },
        "line_items": [line_item],
        "meta_data": [
            {"key": "_client_phone",    "value": phone},
            {"key": "_client_name",     "value": f"{first_name} {last_name}"},
            {"key": "_bot_order",       "value": "yes"}
        ]
    }

    # Coupon থাকলে যোগ করো
    if coupon:
        sub_body["coupon_lines"] = [{"code": coupon}]

    subscription = wc_post_req("subscriptions", sub_body)

    if not subscription or "id" not in subscription:
        # Fallback — normal order
        logger.warning("Subscription API failed, trying order API...")
        return create_order_fallback(
            email, phone, first_name, last_name,
            product_id, variation_id, variation_attributes,
            customer_id, coupon)

    return subscription, None


def create_order_fallback(email, phone, first_name, last_name, product_id,
                          variation_id, variation_attributes, customer_id, coupon=None):
    """Subscription API কাজ না করলে normal order create করো"""
    line_item = {"product_id": product_id, "quantity": 1}
    if variation_id:
        line_item["variation_id"] = variation_id
        if variation_attributes:
            line_item["meta_data"] = [
                {"key": f"attribute_{k}", "value": v}
                for k, v in variation_attributes.items()
            ]

    order_body = {
        "customer_id":          customer_id,
        "payment_method":       "bacs",
        "payment_method_title": "Manual Payment (bKash/Nagad)",
        "set_paid":             False,
        "billing": {
            "first_name": first_name,
            "last_name":  last_name,
            "email":      email,
            "phone":      phone,
            "country":    "BD"
        },
        "line_items": [line_item],
        "meta_data": [
            {"key": "_client_phone",      "value": phone},
            {"key": "_client_name",       "value": f"{first_name} {last_name}"},
            {"key": "_bot_order",         "value": "yes"},
            {"key": "_is_fallback_order", "value": "yes"}
        ]
    }

    if coupon:
        order_body["coupon_lines"] = [{"code": coupon}]

    order = wc_post_req("orders", order_body)

    if not order or "id" not in order:
        err = order.get("message", "Order create হয়নি") if order else "Order/Subscription create হয়নি"
        return None, err

    return order, None

def get_subscriptions_by_email(email):
    """Email দিয়ে সব WooCommerce subscriptions খোঁজো"""
    try:
        resp = req.get(
            f"{WP_URL}/wp-json/wc/v3/subscriptions",
            auth=(WC_KEY, WC_SECRET),
            params={"search": email, "per_page": 20},
            timeout=15
        )
        subs = resp.json()
        if isinstance(subs, list):
            return [s for s in subs
                    if s.get("billing", {}).get("email", "").lower() == email.lower()]
        return []
    except Exception as e:
        logger.error(f"Subscription fetch error: {e}")
        return []

def create_renewal_order(sub_id):
    try:
        resp = req.post(
            f"{WP_URL}/wp-json/wc/v3/subscriptions/{sub_id}/orders",
            auth=(WC_KEY, WC_SECRET), json={}, timeout=15)
        return resp.json()
    except Exception as e:
        logger.error(f"Renewal order error: {e}")
        return None

def generate_payment_link(order_id, order_key=None, email=None):
    """
    Auto-login payment link generate করো।
    Client login ছাড়াই pay করতে পারবে।
    """
    # WordPress autologin endpoint call করো
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

    # Fallback — normal payment link
    if order_key:
        return f"{WP_URL}/checkout/order-pay/{order_id}/?pay_for_order=true&key={order_key}"
    order = wc_get(f"orders/{order_id}")
    if order and order.get("order_key"):
        key = order["order_key"]
        return f"{WP_URL}/checkout/order-pay/{order_id}/?pay_for_order=true&key={key}"
    return f"{WP_URL}/my-account/"

SUB_STATUS_EMOJI = {"active":"✅","on-hold":"⏸️","cancelled":"❌","expired":"⌛","pending":"🕐"}
SUB_STATUS_LABEL = {"active":"Active","on-hold":"On Hold","cancelled":"Cancelled","expired":"Expired","pending":"Pending"}

def format_subscription_text(sub):
    sub_id     = sub.get("id", "?")
    status     = sub.get("status", "unknown")
    emoji      = SUB_STATUS_EMOJI.get(status, "❓")
    label      = SUB_STATUS_LABEL.get(status, status)
    total      = sub.get("total", "0")
    next_date  = sub.get("next_payment_date_gmt", "")
    items      = sub.get("line_items", [])
    item_names = ", ".join([i.get("name", "?") for i in items])
    text  = f"{emoji} *Subscription #{sub_id}*\n"
    text += f"   📦 {item_names}\n"
    text += f"   💵 ৳{total}\n"
    text += f"   Status: {label}\n"
    if next_date:
        text += f"   🔄 Next Payment: {next_date[:10]}\n"
    return text

# =================== NOTIFICATION HELPERS ===================

async def notify_reseller(reseller_code, message, parse_mode="Markdown"):
    try:
        conn = get_db()
        try:
            rows = conn.run(
                "SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                c=reseller_code)
        finally:
            conn.close()
        if rows and rows[0][0]:
            from telegram import Bot
            await Bot(token=RESELLER_BOT_TOKEN).send_message(
                chat_id=rows[0][0], text=message, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Reseller notify error [{reseller_code}]: {e}")

async def send_new_order_notification(order_id, reseller, product_name, customer_email, amount, txn_id=None, payment_method=None):
    try:
        from telegram import Bot
        txn_line = ""
        if txn_id and txn_id != "LATER":
            method_label = "Nagad" if payment_method == "nagad" else "Bkash"
            txn_line = f"💳 {method_label} TxnID: `{txn_id}`\n"
        msg = (f"🔔 *নতুন Reseller Order!*\n\n"
               f"👤 {reseller['name']}  (`{reseller['code']}`)\n"
               f"📦 {product_name}\n📧 Customer: `{customer_email}`\n"
               f"💵 Amount: ৳{amount}\n{txn_line}🆔 Order #{order_id}\n\n"
               f"Stock আছে + টাকা পেলে Approve করো।")
        keyboard = [[
            InlineKeyboardButton("✅ Approve করব", callback_data=f"rapprove_{order_id}"),
            InlineKeyboardButton("❌ Reject করব",  callback_data=f"rreject_{order_id}")
        ]]
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=MAIN_CHAT_ID, text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"New order notify error #{order_id}: {e}")

async def send_account_delivered_to_reseller(order_id, reseller_code, product, customer_email, amount):
    try:
        conn = get_db()
        try:
            rows = conn.run(
                "SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                c=reseller_code)
        finally:
            conn.close()
        if rows and rows[0][0]:
            from telegram import Bot
            keyboard = [[InlineKeyboardButton("✅ Client পেয়েছে!", callback_data=f"res_client_got_{order_id}")]]
            await Bot(token=RESELLER_BOT_TOKEN).send_message(
                chat_id=rows[0][0],
                text=(f"🎉 *Account Delivered!*\n\nOrder #{order_id} — *{product}*\n"
                      f"📧 Customer Email: `{customer_email}`\n\n"
                      f"✅ Invitation পাঠানো হয়েছে!\nClient confirm করলে button press করো 👇"),
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Account delivered notify error [{reseller_code}]: {e}")

async def send_payment_check_to_admin(order_id, reseller_code, txn_id, payment_method, amount):
    try:
        from telegram import Bot
        method_label = "Nagad" if payment_method == "nagad" else "Bkash"
        msg = (f"💰 *Payment Verification দরকার!*\n\nReseller: `{reseller_code}`\n"
               f"Order: #{order_id}\n📲 {method_label} TxnID: `{txn_id}`\n"
               f"💵 Amount: ৳{amount}\n\nTxnID check করে button press করো 👇")
        keyboard = [[
            InlineKeyboardButton("✅ টাকা পেয়েছি — Complete", callback_data=f"rcomplete_{order_id}"),
            InlineKeyboardButton("❌ TxnID ভুল", callback_data=f"rwrong_txn_{order_id}")
        ]]
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=MAIN_CHAT_ID, text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Payment check notify error: {e}")

# =================== BACKGROUND: PAYMENT REMINDER ===================

async def send_payment_reminders():
    while True:
        await asyncio.sleep(3 * 60 * 60)
        try:
            due_orders = get_payment_due_orders()
            for order in due_orders:
                conn = get_db()
                try:
                    conn.run("UPDATE reseller_bot_orders SET payment_reminder_count=payment_reminder_count+1 WHERE id=:id",
                             id=order["id"])
                finally:
                    conn.close()
                count = order["reminder_count"] + 1
                try:
                    conn3 = get_db()
                    try:
                        rr = conn3.run(
                            "SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                            c=order["reseller_code"])
                    finally:
                        conn3.close()
                    if rr and rr[0][0]:
                        from telegram import Bot as TBot
                        remind_keyboard = [[InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order['id']}")]]
                        await TBot(token=RESELLER_BOT_TOKEN).send_message(
                            chat_id=rr[0][0],
                            text=(f"⏰ *Payment Reminder #{count}*\n\nOrder #{order['id']} er account deliver hoye geche!\n"
                                  f"Payment ekhono baaki.\n\n📦 {order['product']}\n💵 ৳{order['amount']}\n\nPayment korbo 👇"),
                            reply_markup=InlineKeyboardMarkup(remind_keyboard), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Reseller reminder error: {e}")
                try:
                    from telegram import Bot
                    keyboard = [[InlineKeyboardButton("📩 Manual Message", callback_data=f"rsend_reminder_{order['id']}")]]
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=CHAT_ID,
                        text=(f"⚠️ *Auto Reminder #{count}*\n\nReseller: `{order['reseller_code']}`\n"
                              f"Order #{order['id']} — {order['product']}\n💵 ৳{order['amount']} বাকি"),
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Admin reminder error: {e}")
        except Exception as e:
            logger.error(f"Payment reminder loop error: {e}")

# =================== AI (GPT-4o) ===================

AI_FUNCTIONS = [
    {"name":"get_recent_orders","description":"Recent WooCommerce orders","parameters":{"type":"object","properties":{"limit":{"type":"integer"},"status":{"type":"string"}}}},
    {"name":"get_last_order","description":"WooCommerce er sorboshesh order","parameters":{"type":"object","properties":{}}},
    {"name":"update_order_status","description":"WooCommerce order status change","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"new_status":{"type":"string"},"use_woo_id":{"type":"boolean"}},"required":["order_id","new_status"]}},
    {"name":"get_income_summary","description":"Income summary","parameters":{"type":"object","properties":{"days":{"type":"integer"}}}},
    {"name":"get_combined_today_summary","description":"Aajker MOTA HISHAB — website + reseller bot","parameters":{"type":"object","properties":{}}},
    {"name":"get_today_reseller_bot_orders","description":"Shudhu reseller bot er aajker orders","parameters":{"type":"object","properties":{}}},
    {"name":"get_reseller_summary","description":"Specific reseller er summary","parameters":{"type":"object","properties":{"reseller_name":{"type":"string"}}}},
    {"name":"get_all_reseller_summary","description":"Sob reseller er summary","parameters":{"type":"object","properties":{}}},
    {"name":"get_payment_due_summary","description":"Kon reseller taka dey nai","parameters":{"type":"object","properties":{}}},
    {"name":"search_orders_by_name","description":"Customer naam diye order khojo","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}},
    {"name":"add_income","description":"Manual income add","parameters":{"type":"object","properties":{"amount":{"type":"number"},"note":{"type":"string"}},"required":["amount","note"]}},
    {"name":"save_memory","description":"Important info save","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"}},"required":["key","value"]}},
    {"name":"get_all_memories","description":"Sob saved notes","parameters":{"type":"object","properties":{}}},
]

def execute_function(name, args):
    try:
        if name == "get_recent_orders": return db_get_recent_orders(args.get("limit",5),args.get("status"))
        elif name == "get_last_order": return db_get_last_order()
        elif name == "update_order_status":
            ok,r = db_update_order_status(args["order_id"],args["new_status"],use_woo_id=True)
            if not ok: ok,r = db_update_order_status(args["order_id"],args["new_status"],use_woo_id=False)
            return {"success":ok,"result":r}
        elif name == "get_income_summary": return db_get_income_summary(args.get("days",1))
        elif name == "get_combined_today_summary": return db_get_combined_today_summary()
        elif name == "get_today_reseller_bot_orders": return db_get_today_reseller_bot_orders()
        elif name == "get_reseller_summary": return db_get_reseller_summary(args.get("reseller_name"))
        elif name == "get_all_reseller_summary": return db_get_reseller_summary()
        elif name == "get_payment_due_summary": return db_get_payment_due_summary()
        elif name == "search_orders_by_name": return db_search_orders_by_name(args["name"])
        elif name == "add_income": return {"success":db_add_income(args["amount"],args["note"])}
        elif name == "save_memory":
            memory_save(args["key"],args["value"])
            return {"success":True,"saved":args["key"]}
        elif name == "get_all_memories": return memory_get_all()
    except Exception as e:
        return {"error":str(e)}

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
    if not OPENAI_KEY: return None
    messages = [{"role":"system","content":build_system_prompt()}] + messages_history
    try:
        resp = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
            json={"model":"gpt-4o","messages":messages,"functions":AI_FUNCTIONS,"function_call":"auto","max_tokens":1000},
            timeout=20).json()
        if "error" in resp: return None
        msg = resp["choices"][0]["message"]
        if msg.get("function_call"):
            fn   = msg["function_call"]["name"]
            args = json.loads(msg["function_call"]["arguments"])
            result = execute_function(fn, args)
            messages.append(msg)
            messages.append({"role":"function","name":fn,"content":json.dumps(result,ensure_ascii=False)})
            resp2 = req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
                json={"model":"gpt-4o","messages":messages,"max_tokens":600},
                timeout=20).json()
            if "error" in resp2: return None
            return resp2["choices"][0]["message"]["content"]
        return msg.get("content")
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# =================== MAIN BOT — MENU ===================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 আজকের WooCommerce Order", callback_data="today_orders"),
         InlineKeyboardButton("💰 আজকের Income",            callback_data="today_income")],
        [InlineKeyboardButton("📅 ৭ দিনের Order",          callback_data="week_orders"),
         InlineKeyboardButton("📊 মাসের Report",            callback_data="month_report")],
        [InlineKeyboardButton("👥 Reseller",                callback_data="resellers"),
         InlineKeyboardButton("➕ Manual Income",            callback_data="manual_income")],
        [InlineKeyboardButton("🔍 Customer খোঁজো",         callback_data="search_customer"),
         InlineKeyboardButton("⏳ Pending Orders",           callback_data="pending_orders")],
        [InlineKeyboardButton("🛍️ Reseller Bot Orders আজ",  callback_data="reseller_bot_orders_today")],
        [InlineKeyboardButton("💸 Due বাকি (Reseller)",     callback_data="due_baki")],
        [InlineKeyboardButton("📋 Subscription Check",      callback_data="sub_check"),
         InlineKeyboardButton("➕ নতুন Subscription",       callback_data="sub_new")],
    ])

# =================== MAIN BOT — HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_conversations[chat_id] = []
    await update.message.reply_text(
        "🛍️ *Favourite Deals Assistant*\n\nAssalamualaikum bhai! "
        "Ami tomar business assistant. Menu theke kaj koro othoba seedha bolo! 🤖",
        reply_markup=main_menu_keyboard(), parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")

    if context.user_data.get("state") == "waiting_reject_reason":
        order_id = context.user_data.get("rejecting_order_id")
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET status='rejected', reject_reason=:r WHERE id=:id",
                         r=text, id=order_id)
            finally:
                conn.close()
            await notify_reseller(order["reseller_code"],
                f"❌ *Order #{order_id} Reject*\n\n📦 {order['product']}\nকারণ: {text}")
            await update.message.reply_text(f"❌ Order #{order_id} reject।", reply_markup=main_menu_keyboard())
        context.user_data["state"] = None
        return

    if context.user_data.get("state") == "waiting_manual_reminder":
        order_id = context.user_data.get("reminder_order_id")
        order    = get_reseller_bot_order(order_id)
        if order:
            await notify_reseller(order["reseller_code"], f"📩 *Admin Message — Order #{order_id}*\n\n{text}")
            await update.message.reply_text("✅ Message পাঠানো হয়েছে।", reply_markup=main_menu_keyboard())
        context.user_data["state"] = None
        return

    if chat_id not in user_conversations:
        user_conversations[chat_id] = []
    user_conversations[chat_id].append({"role":"user","content":text})
    if len(user_conversations[chat_id]) > 15:
        user_conversations[chat_id] = user_conversations[chat_id][-15:]

    ai_reply = await process_ai_message(user_conversations[chat_id])
    if ai_reply:
        user_conversations[chat_id].append({"role":"assistant","content":ai_reply})
        await update.message.reply_text(
            ai_reply,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown")
    else:
        await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ══════════════════════════════════════════
    # ✅ Subscription — Check
    # ══════════════════════════════════════════

    if data == "sub_check":
        await query.edit_message_text(
            "📋 *Subscription Check*\n\nClient এর email দাও:\n`/sub email@gmail.com`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    # ✅ Subscription — নতুন Create (Dynamic)
    # ══════════════════════════════════════════

    elif data == "sub_new":
        await query.edit_message_text(
            "➕ *নতুন Subscription Create*\n\nFormat:\n`/newsub email phone password`\n\n"
            "Example:\n`/newsub john@gmail.com 01712345678 Pass@123`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    # ✅ Product select (dynamic from WooCommerce)
    # ══════════════════════════════════════════

    elif data.startswith("newsub_prod_"):
        product_id = int(data.split("_")[2])
        await query.edit_message_text(f"⏳ Product #{product_id} এর plans fetch করছি...")
        variations = fetch_product_variations(product_id)
        if not variations:
            await query.edit_message_text(
                "❌ Plans পাওয়া যায়নি। পরে try করো।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return
        context.user_data["newsub_product_id"] = product_id
        keyboard = []
        for var in variations:
            label = f"{var['name']} — ৳{var['price']}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"newsub_var_{product_id}_{var['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="newsub_back")])
        await query.edit_message_text(
            "📦 *Plan select করো:*",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    elif data == "newsub_back":
        # Products list আবার দেখাও
        await query.edit_message_text("⏳ Products fetch করছি...")
        products = fetch_subscription_products()
        if not products:
            await query.edit_message_text(
                "❌ Products পাওয়া যায়নি।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return
        keyboard = []
        for p in products:
            keyboard.append([InlineKeyboardButton(f"📦 {p['name']}", callback_data=f"newsub_prod_{p['id']}")])
        keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
        await query.edit_message_text(
            "🛍️ *কোন product এর subscription create করবে?*",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    # ✅ Variation select → Order create
    # ══════════════════════════════════════════

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
            await query.edit_message_text(
                "❌ Session শেষ। আবার `/newsub` দাও।",
                parse_mode="Markdown")
            return

        await query.edit_message_text(f"⏳ `{email}` এর জন্য subscription create করছি...")

        # Variation info নাও
        variations = fetch_product_variations(product_id)
        selected_var = next((v for v in variations if v["id"] == var_id), None)

        if not selected_var:
            await query.edit_message_text("❌ Variation পাওয়া যায়নি।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
            return

        # Attribute prepare করো
        variation_attributes = {}
        if selected_var.get("attribute_slug") and selected_var.get("attribute_option"):
            variation_attributes[selected_var["attribute_slug"]] = selected_var["attribute_option"]

        order, error = create_subscription_directly(
            email, phone, first_name, last_name,
            product_id, var_id, variation_attributes, coupon)

        # Context clear
        context.user_data.pop("newsub_email", None)
        context.user_data.pop("newsub_phone", None)
        context.user_data.pop("newsub_first_name", None)
        context.user_data.pop("newsub_last_name", None)
        context.user_data.pop("newsub_full_name", None)
        context.user_data.pop("newsub_coupon", None)
        context.user_data.pop("newsub_product_id", None)

        if error:
            await query.edit_message_text(
                f"❌ Error: {error}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
            return

        order_id  = order["id"]
        order_key = order.get("order_key", "")

        # Subscription নাকি Order check করো
        is_sub = "/subscriptions/" in str(
            order.get("_links", {}).get("self", [{}])[0].get("href", ""))

        # Payment link generate করো
        if is_sub:
            # Subscription এর initial order এর link নাও
            try:
                sub_orders_resp = req.get(
                    f"{WP_URL}/wp-json/wc/v3/subscriptions/{order_id}/orders",
                    auth=(WC_KEY, WC_SECRET), timeout=15)
                sub_orders = sub_orders_resp.json()
                if isinstance(sub_orders, list) and len(sub_orders) > 0:
                    init_order    = sub_orders[0]
                    init_order_id = init_order["id"]
                    init_order_key = init_order.get("order_key", "")
                    pay_link = generate_payment_link(init_order_id, init_order_key, email=email)
                else:
                    pay_link = f"{WP_URL}/my-account/view-subscription/{order_id}/"
            except Exception as e:
                logger.error(f"Sub initial order error: {e}")
                pay_link = f"{WP_URL}/my-account/view-subscription/{order_id}/"
        else:
            pay_link = generate_payment_link(order_id, order_key, email=email)

        type_label   = "Subscription" if is_sub else "Order"
        coupon_text  = f"\n🎟️ Coupon: `{coupon}`" if coupon else ""

        keyboard = [
            [InlineKeyboardButton(
                f"✅ #{order_id} Activate করো (Payment নেওয়ার পর)",
                callback_data=f"sub_activate_{order_id}_{'sub' if is_sub else 'order'}"
            )],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
        ]

        await query.edit_message_text(
            f"✅ *{type_label} #{order_id} Create হয়েছে!*\n\n"
            f"👤 Name: `{full_name}`\n"
            f"📧 Email: `{email}`\n"
            f"📱 Phone: `{phone}`\n"
            f"📦 Plan: {selected_var['name']}\n"
            f"💵 Amount: ৳{selected_var['price']}{coupon_text}\n\n"
            f"👇 Payment link client কে পাঠাও:\n`{pay_link}`\n\n"
            f"_Client pay করার পর নিচের button press করো_ 👇",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    # ✅ Subscription/Order Activate button
    # ══════════════════════════════════════════

    elif data.startswith("sub_activate_"):
        parts   = data.split("_")
        item_id = int(parts[2])
        is_sub  = len(parts) > 3 and parts[3] == "sub"

        await query.edit_message_text(f"⏳ #{item_id} activate করছি...")

        if is_sub:
            # Subscription directly activate করো
            result = wc_put(f"subscriptions/{item_id}", {"status": "active"})
        else:
            # Order complete করো → subscription auto create হবে
            result = wc_put(f"orders/{item_id}", {"status": "completed"})

        if result and result.get("status") in ["active", "completed"]:
            await query.edit_message_text(
                f"✅ *#{item_id} Activated!*\n\nClient এর subscription চালু হয়ে গেছে। 🎉",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"❌ Activate হয়নি।\n\nWooCommerce dashboard এ manually করো:\n"
                f"{'Subscriptions' if is_sub else 'Orders'} → #{item_id} → Status: Active/Completed",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    # ✅ Subscription Renew/Cancel buttons
    # ══════════════════════════════════════════

    elif data.startswith("sub_renew_"):
        sub_id = int(data.split("_")[2])
        keyboard = [
            [InlineKeyboardButton("🔗 Payment Link Generate", callback_data=f"sub_link_{sub_id}")],
            [InlineKeyboardButton("✅ Manually Renew করলাম",  callback_data=f"sub_manual_{sub_id}")],
            [InlineKeyboardButton("🔙 Back",                   callback_data="menu")]
        ]
        await query.edit_message_text(
            f"🔄 *Subscription #{sub_id} Renew*\n\nকীভাবে renew করবে?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    elif data.startswith("sub_link_"):
        sub_id = int(data.split("_")[2])
        await query.edit_message_text("⏳ Payment link generate করছি...")
        renewal = create_renewal_order(sub_id)
        if renewal and "id" in renewal:
            order_id  = renewal["id"]
            order_key = renewal.get("order_key", "")
            # Sub এর billing email নাও
            sub_data  = wc_get(f"subscriptions/{sub_id}")
            sub_email = sub_data.get("billing", {}).get("email") if sub_data else None
            pay_link  = generate_payment_link(order_id, order_key, email=sub_email)
            await query.edit_message_text(
                f"✅ *Payment Link Ready!*\n\nSubscription #{sub_id} | Order #{order_id}\n\n"
                f"👇 Client কে পাঠাও:\n`{pay_link}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        else:
            pay_link = f"{WP_URL}/my-account/view-subscription/{sub_id}/"
            await query.edit_message_text(
                f"⚠️ Auto link হয়নি।\n\nClient কে পাঠাও:\n`{pay_link}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Manually Renew", callback_data=f"sub_manual_{sub_id}")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
                ]), parse_mode="Markdown")
        return

    elif data.startswith("sub_manual_"):
        sub_id = int(data.split("_")[2])
        result = wc_put(f"subscriptions/{sub_id}", {"status": "active"})
        if result and result.get("status") == "active":
            await query.edit_message_text(
                f"✅ *Subscription #{sub_id} Active!*",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                "❌ Activate হয়নি। WooCommerce dashboard এ manually করো।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
        return

    elif data.startswith("sub_cancel_confirm_"):
        sub_id = int(data.split("_")[3])
        result = wc_put(f"subscriptions/{sub_id}", {"status": "cancelled"})
        if result and result.get("status") == "cancelled":
            await query.edit_message_text(
                f"✅ *Subscription #{sub_id} Cancel হয়েছে!*",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                "❌ Cancel হয়নি। WooCommerce dashboard এ manually করো.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
        return

    elif data.startswith("sub_cancel_"):
        sub_id = int(data.split("_")[2])
        keyboard = [[
            InlineKeyboardButton("✅ হ্যাঁ Cancel করো", callback_data=f"sub_cancel_confirm_{sub_id}"),
            InlineKeyboardButton("❌ না, Back",          callback_data="menu")
        ]]
        await query.edit_message_text(
            f"⚠️ *Subscription #{sub_id} Cancel করবে?*\n\nClient এর access বন্ধ হবে!",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    # Existing Reseller buttons
    # ══════════════════════════════════════════

    if data.startswith("rapprove_"):
        order_id = int(data.split("_")[1])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET status='account_delivered', account_delivered_at=NOW() WHERE id=:id",
                         id=order_id)
            finally:
                conn.close()
            await send_account_delivered_to_reseller(
                order_id, order["reseller_code"], order["product"], order["customer_email"], order["amount"])
            await query.edit_message_text(
                f"✅ *Order #{order_id} Approved!*\n\n📧 {order['customer_email']} এ invitation পাঠাও।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        return

    elif data.startswith("rreject_"):
        order_id = int(data.split("_")[1])
        context.user_data["rejecting_order_id"] = order_id
        context.user_data["state"]               = "waiting_reject_reason"
        await query.edit_message_text(f"❌ Order #{order_id} reject এর কারণ লেখো:")
        return

    elif data.startswith("rcomplete_"):
        order_id = int(data.split("_")[1])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET status='completed', completed_at=NOW() WHERE id=:id", id=order_id)
                conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'reseller')",
                         a=float(order["amount"]),
                         n=f"Reseller #{order_id} — {order['product']} ({order['reseller_code']})")
            finally:
                conn.close()
            await notify_reseller(order["reseller_code"],
                f"🎉 *Order #{order_id} সম্পন্ন!*\n\n📦 {order['product']}\n💵 ৳{order['amount']}\n\nধন্যবাদ!")
            await query.edit_message_text(
                f"✅ *Order #{order_id} Complete!*\n💵 ৳{order['amount']} income এ যোগ।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        return

    elif data.startswith("rwrong_txn_"):
        order_id = int(data.split("_")[2])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET transaction_id=NULL WHERE id=:id", id=order_id)
            finally:
                conn.close()
            await notify_reseller(order["reseller_code"],
                f"❌ *Order #{order_id} — TxnID ভুল!*\n\nSothik TxnID dao।")
            await query.edit_message_text(
                f"❌ Order #{order_id} TxnID ভুল। Reseller কে জানানো হয়েছে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown")
        return

    elif data.startswith("rsend_reminder_"):
        order_id = int(data.split("_")[2])
        context.user_data["reminder_order_id"] = order_id
        context.user_data["state"]              = "waiting_manual_reminder"
        order = get_reseller_bot_order(order_id)
        await query.edit_message_text(
            f"📩 Order #{order_id} ({order['reseller_code']}) কে কী message পাঠাবে?")
        return

    # ── Menu navigation ──
    if   data == "today_orders":              await show_orders(query, days=1)
    elif data == "week_orders":               await show_orders(query, days=7)
    elif data == "today_income":              await show_income(query, days=1)
    elif data == "month_report":              await show_month_report(query)
    elif data == "resellers":                 await show_resellers(query)
    elif data == "pending_orders":            await show_orders_by_status(query, "pending")
    elif data == "reseller_bot_orders_today": await show_reseller_bot_orders_today(query)
    elif data == "due_baki":                  await show_due_baki(query)
    elif data == "manual_income":
        await query.edit_message_text("💰 Format: `/income 500 bkash e paisi`", parse_mode="Markdown")
    elif data == "search_customer":
        await query.edit_message_text("🔍 Format: `/customer example@email.com`", parse_mode="Markdown")
    elif data == "menu":
        await query.edit_message_text(
            "🛍️ *FD Assistant*\n\nMenu theke kaj koro ba seedha bolo:",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif data.startswith("status_"):   await show_status_options(query, data.split("_")[1])
    elif data.startswith("setstatus_"):
        parts = data.split("_")
        await update_order_status_btn(query, parts[1], parts[2])
    elif data.startswith("remind_reseller_"):
        reseller_code = data.replace("remind_reseller_", "")
        conn = get_db()
        try:
            orders = conn.run(
                "SELECT id, product, amount FROM reseller_bot_orders WHERE reseller_code=:c AND status='payment_due'",
                c=reseller_code)
        finally:
            conn.close()
        if not orders:
            await query.edit_message_text(f"✅ {reseller_code} এর কোনো due নেই।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="due_baki")]])); return
        count = 0
        for o in orders:
            try:
                conn2 = get_db()
                try:
                    rr = conn2.run("SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)", c=reseller_code)
                finally:
                    conn2.close()
                if rr and rr[0][0]:
                    from telegram import Bot
                    pay_keyboard = [[InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{o[0]}")]]
                    await Bot(token=RESELLER_BOT_TOKEN).send_message(
                        chat_id=rr[0][0],
                        text=(f"⏰ *Admin Reminder!*\n\nOrder #{o[0]} — {o[1]}\n💵 ৳{o[2]} বাকি!\n\nPayment করো 👇"),
                        reply_markup=InlineKeyboardMarkup(pay_keyboard), parse_mode="Markdown")
                    count += 1
            except Exception as e:
                logger.error(f"Remind error: {e}")
        await query.edit_message_text(f"✅ *{reseller_code} কে {count}টা reminder পাঠানো হয়েছে!*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="due_baki")]]),
            parse_mode="Markdown")

    elif data.startswith("confirm_remove_reseller_"):
        parts       = data.split("_", 4)
        reseller_id = int(parts[3])
        code        = parts[4]
        conn = get_db()
        try:
            rows = conn.run("SELECT name, phone FROM resellers WHERE id=:id", id=reseller_id)
            if not rows:
                await query.edit_message_text("❌ Reseller পাওয়া যায়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])); return
            name, phone = rows[0][0], rows[0][1]
            conn.run("UPDATE reseller_bot_orders SET reseller_id=NULL WHERE reseller_id=:id", id=reseller_id)
            conn.run("UPDATE reseller_orders SET reseller_id=NULL WHERE reseller_id=:id", id=reseller_id)
            conn.run("DELETE FROM resellers WHERE id=:id", id=reseller_id)
        finally:
            conn.close()
        await query.edit_message_text(
            f"🗑️ *Reseller বাদ দেওয়া হয়েছে!*\n\n👤 {name} | 📞 {phone} | 🔑 `{code}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown")

# ── Display helpers ──

STATUS_EMOJI = {STATUS_PENDING:"🕐",STATUS_ACCOUNT_DELIVERED:"📦",STATUS_PAYMENT_DUE:"💰",STATUS_COMPLETED:"✅",STATUS_REJECTED:"❌"}
STATUS_LABEL = {STATUS_PENDING:"Pending",STATUS_ACCOUNT_DELIVERED:"Account Delivered",STATUS_PAYMENT_DUE:"Payment Due",STATUS_COMPLETED:"Completed",STATUS_REJECTED:"Rejected"}

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn  = get_db()
    try:
        rows = conn.run("SELECT id,woo_order_id,customer_name,total,status FROM orders WHERE created_at>=:s ORDER BY created_at DESC", s=since)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text("📦 Kono order nei.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])); return
    text = f"📦 *Last {days} diner orders ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_orders_by_status(query, status):
    conn = get_db()
    try:
        rows = conn.run("SELECT id,woo_order_id,customer_name,total,status FROM orders WHERE status=:s ORDER BY created_at DESC LIMIT 10", s=status)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text(f"📦 {status} status e kono order nei.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])); return
    text = f"📦 *{status} orders ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status", callback_data=f"status_{o[0]}")])
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
        parse_mode="Markdown")

async def show_month_report(query):
    since = datetime.now() - timedelta(days=30)
    conn  = get_db()
    try:
        o  = conn.run("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        i  = conn.run("SELECT COALESCE(SUM(amount),0) FROM income WHERE created_at>=:s", s=since)
        rb = conn.run("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM reseller_bot_orders WHERE created_at>=:s AND status='completed'", s=since)
    finally:
        conn.close()
    text = (f"📊 *Last 30 দিনের Report*\n\n"
            f"🌐 WooCommerce: {o[0][0] or 0}টা | ৳{o[0][1] or 0}\n"
            f"🛍️ Reseller Bot: {rb[0][0] or 0}টা | ৳{rb[0][1] or 0}\n\n"
            f"💰 Total Income: ৳{i[0][0] or 0}")
    await query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown")

async def show_resellers(query):
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT r.name, r.phone, r.reseller_code,
                COUNT(DISTINCT ro.id), COALESCE(SUM(ro.price*ro.quantity),0),
                COUNT(DISTINCT rbo.id), COALESCE(SUM(rbo.amount),0)
            FROM resellers r
            LEFT JOIN reseller_orders ro ON r.id=ro.reseller_id AND ro.created_at>=date_trunc('month',NOW())
            LEFT JOIN reseller_bot_orders rbo ON r.id=rbo.reseller_id AND rbo.created_at>=date_trunc('month',NOW()) AND rbo.status!='rejected'
            GROUP BY r.id,r.name,r.phone,r.reseller_code
        """)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text("👥 Kono reseller nei.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
            parse_mode="Markdown"); return
    text = "👥 *এই মাসের Reseller Report:*\n\n"
    for r in rows:
        t_orders = (r[3] or 0) + (r[5] or 0)
        t_amount = float(r[4] or 0) + float(r[6] or 0)
        text += f"🔸 *{r[0]}* ({r[1]}) — `{r[2] or 'N/A'}`\n   {t_orders}টা | ৳{t_amount:.0f}\n\n"
    await query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown")

async def show_reseller_bot_orders_today(query):
    orders = db_get_today_reseller_bot_orders()
    if not orders:
        await query.edit_message_text("🛍️ আজ কোনো reseller bot order নেই।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])); return
    text = f"🛍️ *আজকের Reseller Bot Orders ({len(orders)}টা):*\n\n"
    keyboard = []
    for o in orders:
        emoji = STATUS_EMOJI.get(o["status"], "❓")
        label = STATUS_LABEL.get(o["status"], o["status"])
        text += (f"{emoji} #{o['id']} — *{o['reseller_name']}* (`{o['reseller_code']}`)\n"
                 f"   📦 {o['product']} | {o['amount']} | {label}\n\n")
        if o["status"] == STATUS_PENDING:
            keyboard.append([
                InlineKeyboardButton(f"✅ Approve #{o['id']}", callback_data=f"rapprove_{o['id']}"),
                InlineKeyboardButton(f"❌ Reject #{o['id']}",  callback_data=f"rreject_{o['id']}")
            ])
        elif o["status"] == STATUS_PAYMENT_DUE:
            keyboard.append([InlineKeyboardButton(f"📩 Remind #{o['id']}", callback_data=f"rsend_reminder_{o['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_due_baki(query):
    due_list = db_get_payment_due_summary()
    if not due_list:
        await query.edit_message_text("✅ *সব clear!*\n\nকোনো payment বাকি নেই।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
            parse_mode="Markdown"); return
    total_due = sum(float(d["due_amount"].replace("৳","")) for d in due_list)
    text = "💸 *Due বাকি:*\n\n"
    keyboard = []
    for d in due_list:
        text += f"🔸 *{d['name']}* (`{d['reseller_code']}`)\n   {d['due_orders']}টা | {d['due_amount']} বাকি\n\n"
        keyboard.append([InlineKeyboardButton(f"📩 {d['name']} Remind", callback_data=f"remind_reseller_{d['reseller_code']}")])
    text += f"💰 *মোট: ৳{total_due:.0f}*"
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_status_options(query, order_id):
    keyboard = [
        [InlineKeyboardButton("⏳ Processing",      callback_data=f"setstatus_{order_id}_processing")],
        [InlineKeyboardButton("✅ Completed",        callback_data=f"setstatus_{order_id}_completed")],
        [InlineKeyboardButton("💳 Payment Pending", callback_data=f"setstatus_{order_id}_pending")],
        [InlineKeyboardButton("❌ Cancelled",        callback_data=f"setstatus_{order_id}_cancelled")],
        [InlineKeyboardButton("🔙 Back",             callback_data="today_orders")]
    ]
    await query.edit_message_text(f"✏️ Order #{order_id} এর নতুন status:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))

async def update_order_status_btn(query, order_id, new_status):
    ok, result = db_update_order_status(order_id, new_status)
    if ok:
        await query.edit_message_text(f"✅ Order #{result} — *{new_status}*!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown")
    else:
        await query.edit_message_text(f"❌ {result}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

# ── Commands ──

async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /income [taka] [note]"); return
    try:
        amount = float(context.args[0])
        note   = " ".join(context.args[1:]) if len(context.args) > 1 else "Manual entry"
        db_add_income(amount, note)
        await update.message.reply_text(f"✅ ৳{amount} income add!\n📝 {note}")
    except:
        await update.message.reply_text("❌ Vul format!")

async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /customer [email]"); return
    email = context.args[0].lower()
    conn  = get_db()
    try:
        rows = conn.run(
            "SELECT woo_order_id,customer_name,total,status,created_at FROM orders WHERE LOWER(customer_email)=:e ORDER BY created_at DESC",
            e=email)
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text(f"❌ {email} এ কোনো order নেই।"); return
    total_spent = sum(float(o[2]) for o in rows)
    text = f"👤 *{rows[0][1]}*\n📧 {email}\n\n"
    for o in rows:
        emoji = "✅" if o[3]=="completed" else "⏳" if o[3]=="processing" else "❌"
        text += f"{emoji} #{o[0]} — {o[4].strftime('%d %b %Y')} | ৳{o[2]} | {o[3]}\n"
    text += f"\n💰 *মোট: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sub email@gmail.com — subscription check"""
    if not context.args:
        await update.message.reply_text("📋 Format: `/sub email@gmail.com`", parse_mode="Markdown"); return
    email = context.args[0].lower().strip()
    await update.message.reply_text(f"🔍 `{email}` এর subscriptions খুঁজছি...", parse_mode="Markdown")
    subs = get_subscriptions_by_email(email)
    if not subs:
        await update.message.reply_text(
            f"❌ `{email}` এ কোনো subscription নেই।\n\nনতুন create করতে:\n`/newsub {email} phone password`",
            parse_mode="Markdown"); return
    text = f"📋 *{email} এর Subscriptions ({len(subs)}টা):*\n\n"
    keyboard = []
    for sub in subs:
        sub_id = sub.get("id")
        status = sub.get("status", "unknown")
        text  += format_subscription_text(sub) + "\n"
        row = []
        if status in ["active", "on-hold", "pending"]:
            row.append(InlineKeyboardButton(f"🔄 #{sub_id} Renew",  callback_data=f"sub_renew_{sub_id}"))
            row.append(InlineKeyboardButton(f"❌ #{sub_id} Cancel", callback_data=f"sub_cancel_{sub_id}"))
        elif status in ["cancelled", "expired"]:
            row.append(InlineKeyboardButton(f"🔄 #{sub_id} Reactivate", callback_data=f"sub_renew_{sub_id}"))
        if row:
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def new_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /newsub email "Full Name" phone [coupon]
    Example: /newsub john@gmail.com "John Doe" 01712345678
    Example with coupon: /newsub john@gmail.com "John Doe" 01712345678 SAVE20
    """
    # Raw text থেকে parse করো (quoted name support)
    raw = update.message.text.split(None, 1)
    if len(raw) < 2:
        await update.message.reply_text(
            '📋 Format:\n`/newsub email "Full Name" phone`\n\n'
            'Example:\n`/newsub john@gmail.com "John Doe" 01712345678`\n\n'
            'Coupon সহ:\n`/newsub john@gmail.com "John Doe" 01712345678 SAVE20`',
            parse_mode="Markdown"); return

    args_str = raw[1].strip()

    # Email নাও (প্রথম word)
    parts = args_str.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            '❌ Format ঠিক নেই!\n\n`/newsub email "Full Name" phone`',
            parse_mode="Markdown"); return

    email    = parts[0].lower().strip()
    rest     = parts[1].strip()

    # Quoted name parse করো
    if rest.startswith('"'):
        end_quote = rest.find('"', 1)
        if end_quote == -1:
            await update.message.reply_text('❌ Name এর শেষে `"` দাও!', parse_mode="Markdown"); return
        full_name  = rest[1:end_quote].strip()
        after_name = rest[end_quote+1:].strip().split()
    else:
        # Quote ছাড়া হলে প্রথম word name, বাকি phone+coupon
        after_name_parts = rest.split()
        full_name  = after_name_parts[0] if after_name_parts else ""
        after_name = after_name_parts[1:] if len(after_name_parts) > 1 else []

    if not after_name:
        await update.message.reply_text(
            '❌ Phone number দাও!\n\n`/newsub email "Full Name" phone`',
            parse_mode="Markdown"); return

    phone  = after_name[0].strip()
    coupon = after_name[1].strip().upper() if len(after_name) > 1 else None

    # Validate
    if "@" not in email or "." not in email:
        await update.message.reply_text("❌ Valid email দাও!"); return
    if len(phone) < 10:
        await update.message.reply_text("❌ Valid phone number দাও!"); return

    # Name split করো
    name_parts = full_name.split(None, 1)
    first_name = name_parts[0] if name_parts else email.split("@")[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else "Customer"

    # Context এ save করো
    context.user_data["newsub_email"]      = email
    context.user_data["newsub_phone"]      = phone
    context.user_data["newsub_first_name"] = first_name
    context.user_data["newsub_last_name"]  = last_name
    context.user_data["newsub_full_name"]  = full_name
    context.user_data["newsub_coupon"]     = coupon

    coupon_text = f"\n🎟️ Coupon: `{coupon}`" if coupon else ""

    await update.message.reply_text(
        f"✅ Client details নেওয়া হয়েছে!\n\n"
        f"👤 Name: `{full_name}`\n"
        f"📧 Email: `{email}`\n"
        f"📱 Phone: `{phone}`{coupon_text}\n\n"
        f"⏳ Products fetch করছি...",
        parse_mode="Markdown")

    # WooCommerce থেকে subscription products আনো
    products = fetch_subscription_products()

    if not products:
        await update.message.reply_text("❌ কোনো subscription product পাওয়া যায়নি।")
        return

    keyboard = []
    for p in products:
        price_label = f" — ৳{p['price']}+" if p.get("price") and p["price"] != "?" else ""
        keyboard.append([InlineKeyboardButton(f"📦 {p['name']}{price_label}", callback_data=f"newsub_prod_{p['id']}")])
    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])

    await update.message.reply_text(
        f"🛍️ *কোন product এর subscription create করবে?*",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def listresellers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    try:
        rows = conn.run("SELECT id, name, phone, reseller_code FROM resellers ORDER BY id")
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text("কোনো reseller নেই।"); return
    text = "👥 *সব Reseller:*\n\n"
    keyboard = []
    for r in rows:
        rid, name, phone, code = r[0], r[1], r[2], r[3] or "N/A"
        text += f"🔸 ID:`{rid}` — *{name}* | 📞`{phone}` | 🔑`{code}`\n"
        keyboard.append([InlineKeyboardButton(f"🗑️ {name} বাদ দাও", callback_data=f"confirm_remove_reseller_{rid}_ID{rid}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def addreseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Format: /addreseller [naam] [phone] [CODE]"); return
    name, phone, code = context.args[0], context.args[1], context.args[2].upper()
    conn = get_db()
    try:
        conn.run("INSERT INTO resellers (name,phone,reseller_code) VALUES (:n,:p,:c)", n=name, p=phone, c=code)
    finally:
        conn.close()
    await update.message.reply_text(f"✅ Reseller added!\n👤 {name} | 📞 {phone} | 🔑 {code}")

async def removereseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: `/removereseller RS001`", parse_mode="Markdown"); return
    query_val = context.args[0].upper()
    conn = get_db()
    try:
        if context.args[0][0].isdigit():
            rows = conn.run("SELECT id,name,phone,reseller_code FROM resellers WHERE phone=:p", p=context.args[0])
        else:
            rows = conn.run("SELECT id,name,phone,reseller_code FROM resellers WHERE UPPER(reseller_code)=:c", c=query_val)
        if not rows:
            await update.message.reply_text(f"❌ `{context.args[0]}` পাওয়া যায়নি।", parse_mode="Markdown"); return
        reseller_id = rows[0][0]; reseller_name = rows[0][1]; reseller_phone = rows[0][2]; reseller_code = rows[0][3] or "N/A"
        active = conn.run("SELECT COUNT(*) FROM reseller_bot_orders WHERE reseller_id=:rid AND status NOT IN ('completed','rejected')", rid=reseller_id)
        active_count = active[0][0] if active else 0
    finally:
        conn.close()
    safe_code = reseller_code.replace(".", "_")
    cb_data   = f"confirm_remove_reseller_{reseller_id}_{safe_code}"
    if active_count > 0:
        keyboard = [[InlineKeyboardButton("⚠️ তারপরেও বাদ দাও", callback_data=cb_data),InlineKeyboardButton("❌ Cancel",callback_data="menu")]]
        await update.message.reply_text(
            f"⚠️ {reseller_name} এর *{active_count}টা active order* আছে! তারপরেও বাদ দেবে?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        keyboard = [[InlineKeyboardButton("✅ বাদ দাও", callback_data=cb_data),InlineKeyboardButton("❌ Cancel",callback_data="menu")]]
        await update.message.reply_text(
            f"🗑️ *{reseller_name}* বাদ দেবে?\n📞 {reseller_phone} | 🔑 `{reseller_code}`",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def resellersale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text("Format: /rsale [phone] [product] [qty] [price]"); return
    try:
        phone, product = context.args[0], context.args[1]
        qty, price     = int(context.args[2]), float(context.args[3])
        conn = get_db()
        try:
            r = conn.run("SELECT id,name FROM resellers WHERE phone=:p", p=phone)
            if not r:
                await update.message.reply_text(f"❌ {phone} এ কোনো reseller নেই।"); return
            conn.run("INSERT INTO reseller_orders (reseller_id,product,quantity,price) VALUES (:r,:p,:q,:pr)",
                     r=r[0][0], p=product, q=qty, pr=price)
        finally:
            conn.close()
        await update.message.reply_text(f"✅ {r[0][1]} — {product} x{qty} = ৳{qty*price}")
    except:
        await update.message.reply_text("❌ ভুল format!")

def _wp_paylater_api(method, endpoint, email=None):
    url     = f"{WP_URL}/wp-json/fdbot/v1/paylater/{endpoint}"
    headers = {"X-FD-Secret": WP_PAYLATER_SECRET}
    try:
        if method == "GET":
            resp = req.get(url, headers=headers, timeout=10)
        else:
            resp = req.post(url, headers=headers, json={"email": email}, timeout=10)
        return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}

async def paylater_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text(
            "📋 *Pay Later:*\n\n`/paylater add email`\n`/paylater remove email`\n`/paylater list`",
            parse_mode="Markdown"); return
    sub = context.args[0].lower()
    context.args = context.args[1:]
    if sub == "add":
        if not context.args:
            await update.message.reply_text("Format: `/paylater add email`", parse_mode="Markdown"); return
        email  = context.args[0].lower().strip()
        result = _wp_paylater_api("POST", "add", email)
        if result.get("success"):
            await update.message.reply_text(f"✅ Pay Later চালু!\n📧 `{email}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Error: {result.get('message', 'Unknown')}")
    elif sub == "remove":
        if not context.args:
            await update.message.reply_text("Format: `/paylater remove email`", parse_mode="Markdown"); return
        email  = context.args[0].lower().strip()
        result = _wp_paylater_api("POST", "remove", email)
        if result.get("success"):
            await update.message.reply_text(f"🗑️ Pay Later বাদ!\n📧 `{email}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ `{email}` পাওয়া যায়নি।")
    elif sub == "list":
        result = _wp_paylater_api("GET", "list")
        if not result.get("success"):
            await update.message.reply_text(f"❌ Error: {result.get('message')}"); return
        emails = result.get("emails", [])
        if not emails:
            await update.message.reply_text("📋 কোনো approved email নেই।"); return
        text = f"📋 *Approved ({len(emails)}টা):*\n\n"
        for i, e in enumerate(emails, 1):
            text += f"{i}. `{e}`\n"
        await update.message.reply_text(text, parse_mode="Markdown")

# =================== RESELLER BOT ===================

reseller_user_data: dict = {}

def reseller_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 নতুন Order দিব", callback_data="res_new_order")],
        [InlineKeyboardButton("📋 আমার Orders",     callback_data="res_my_orders")],
        [InlineKeyboardButton("ℹ️ Price List",      callback_data="res_price_list")],
    ])

async def reseller_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.message.chat_id
    reseller = get_reseller_by_chat_id(chat_id)
    if reseller:
        conn = get_db()
        try:
            due = conn.run("SELECT COUNT(*) FROM reseller_bot_orders WHERE reseller_code=:c AND status='payment_due'", c=reseller["code"])
        finally:
            conn.close()
        due_count = due[0][0] if due else 0
        due_txt   = f"\n\n⚠️ *{due_count}টা payment বাকি!*" if due_count > 0 else ""
        await update.message.reply_text(
            f"🛍️ Welcome back *{reseller['name']}* bhai! 👋\nCode: `{reseller['code']}`{due_txt}\n\nKi korte chao?",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")
        return ConversationHandler.END
    await update.message.reply_text(
        "🛍️ *FD Reseller Bot*\n\nAssalamualaikum! 👋\n\nTomar *reseller code* dao:",
        parse_mode="Markdown")
    return WAITING_CODE

async def reseller_handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code     = update.message.text.strip().upper()
    chat_id  = update.message.chat_id
    reseller = get_reseller_by_code(code)
    if not reseller:
        await update.message.reply_text("❌ Ei code valid na. Sothik code dao:")
        return WAITING_CODE
    conn = get_db()
    try:
        conn.run("UPDATE resellers SET telegram_chat_id=:c WHERE UPPER(reseller_code)=UPPER(:code)", c=str(chat_id), code=code)
    finally:
        conn.close()
    await update.message.reply_text(
        f"✅ *Register Successful!*\n\nWelcome *{reseller['name']}* bhai! 🎉\nCode: `{code}`",
        reply_markup=reseller_main_menu(), parse_mode="Markdown")
    return ConversationHandler.END

async def reseller_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data == "res_price_list":
        await query.edit_message_text(
            f"📋 *Price List:*\n\n🤖 ChatGPT Plus — *৳{PRODUCTS['chatgpt']['price']}*\n"
            f"💎 Gemini Advanced — *৳{PRODUCTS['gemini']['price']}*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]]),
            parse_mode="Markdown")
        return

    if data == "res_new_order":
        keyboard = [
            [InlineKeyboardButton(f"🤖 ChatGPT Plus — ৳{PRODUCTS['chatgpt']['price']}", callback_data="res_order_chatgpt")],
            [InlineKeyboardButton(f"💎 Gemini Advanced — ৳{PRODUCTS['gemini']['price']}", callback_data="res_order_gemini")],
            [InlineKeyboardButton("🔙 Back", callback_data="res_back")]
        ]
        await query.edit_message_text("🛒 *কোন product?*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("res_order_"):
        product_key = data.replace("res_order_", "")
        product     = PRODUCTS.get(product_key)
        if product:
            reseller_user_data[chat_id] = {
                "product": product_key, "product_name": product["name"],
                "amount": product["price"], "state": "waiting_email"
            }
            await query.edit_message_text(
                f"📦 *{product['name']}*\n💵 ৳{product['price']}\n\n👇 *Customer এর email দাও:*",
                parse_mode="Markdown")

    elif data == "res_my_orders":
        reseller = get_reseller_by_chat_id(chat_id)
        if not reseller:
            await query.edit_message_text("❌ /start দাও।"); return
        conn = get_db()
        try:
            rows = conn.run(
                "SELECT id,product,customer_email,amount,status,transaction_id,created_at "
                "FROM reseller_bot_orders WHERE reseller_code=:c ORDER BY created_at DESC LIMIT 10",
                c=reseller["code"])
        finally:
            conn.close()
        if not rows:
            await query.edit_message_text("📋 কোনো order নেই।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="res_back")]])); return
        text = "📋 *Last 10 Order:*\n\n"
        keyboard = []
        for r in rows:
            emoji = STATUS_EMOJI.get(r[4], "❓")
            label = STATUS_LABEL.get(r[4], r[4])
            date_s = r[6].strftime("%d %b, %I:%M %p") if r[6] else ""
            text  += f"{emoji} *#{r[0]}* — {r[1]}\n   📧 {r[2]}\n   💵 ৳{r[3]} | {label} | {date_s}\n\n"
            if r[4] == STATUS_PAYMENT_DUE:
                keyboard.append([InlineKeyboardButton(f"💳 #{r[0]} Payment", callback_data=f"res_pay_order_{r[0]}")])
            elif r[4] == STATUS_ACCOUNT_DELIVERED:
                keyboard.append([InlineKeyboardButton(f"✅ #{r[0]} Client পেয়েছে!", callback_data=f"res_client_got_{r[0]}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="res_back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("res_client_got_"):
        order_id = int(data.split("_")[3])
        order    = get_reseller_bot_order(order_id)
        if order:
            already_paid = order.get("transaction_id") and order["transaction_id"] != "LATER"
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET status='payment_due', payment_due_at=NOW() WHERE id=:id", id=order_id)
            finally:
                conn.close()
            if already_paid:
                try:
                    from telegram import Bot
                    txn = order["transaction_id"]
                    method_label = "Nagad" if order.get("payment_method") == "nagad" else "Bkash"
                    admin_kb = [[
                        InlineKeyboardButton("✅ Complete", callback_data=f"rcomplete_{order_id}"),
                        InlineKeyboardButton("❌ TxnID ভুল", callback_data=f"rwrong_txn_{order_id}")
                    ]]
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=MAIN_CHAT_ID,
                        text=(f"✅ *Client Confirmed!*\n\nOrder #{order_id}\n{method_label} TxnID: `{txn}`\n\nVerify করো 👇"),
                        reply_markup=InlineKeyboardMarkup(admin_kb), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Admin txn verify error: {e}")
                await query.edit_message_text(
                    f"✅ Order #{order_id} confirmed! Admin verify করবে।",
                    reply_markup=reseller_main_menu(), parse_mode="Markdown")
            else:
                try:
                    conn2 = get_db()
                    try:
                        rrows = conn2.run("SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)", c=order["reseller_code"])
                    finally:
                        conn2.close()
                    if rrows and rrows[0][0]:
                        from telegram import Bot
                        pay_kb = [[InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order_id}")]]
                        await Bot(token=RESELLER_BOT_TOKEN).send_message(
                            chat_id=rrows[0][0],
                            text=(f"💰 *Payment Due!*\n\nOrder #{order_id}\n💵 ৳{order['amount']}\n\nPayment করো 👇"),
                            reply_markup=InlineKeyboardMarkup(pay_kb), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Reseller payment due error: {e}")
                try:
                    from telegram import Bot
                    admin_kb = [[InlineKeyboardButton("📩 Remind", callback_data=f"rsend_reminder_{order_id}")]]
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=MAIN_CHAT_ID,
                        text=(f"💰 *Payment Due!*\n\nReseller: `{order['reseller_code']}`\nOrder #{order_id}\n💵 ৳{order['amount']}"),
                        reply_markup=InlineKeyboardMarkup(admin_kb), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Admin payment due error: {e}")
                confirmed_kb = [[InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order_id}")]]
                await query.edit_message_text(
                    f"✅ Confirmed! Order #{order_id}.\n\nPayment করো 👇",
                    reply_markup=InlineKeyboardMarkup(confirmed_kb), parse_mode="Markdown")

    elif data.startswith("res_pay_order_"):
        order_id = int(data.split("_")[3])
        order    = get_reseller_bot_order(order_id)
        if order:
            reseller_user_data[chat_id] = {
                "paying_order_id": order_id, "product_name": order["product"],
                "amount": order["amount"], "state": "waiting_pay_method"
            }
            keyboard = [
                [InlineKeyboardButton("📱 Bkash", callback_data=f"res_method_bkash_{order_id}")],
                [InlineKeyboardButton("📱 Nagad", callback_data=f"res_method_nagad_{order_id}")],
            ]
            await query.edit_message_text(
                f"💳 Order #{order_id} — {order['product']}\n💵 ৳{order['amount']}\n\nKotha theke payment?",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("res_method_"):
        parts    = data.split("_")
        method   = parts[2]
        order_id = int(parts[3])
        number   = NAGAD_NUMBER if method == "nagad" else BKASH_NUMBER
        if chat_id in reseller_user_data:
            reseller_user_data[chat_id]["payment_method"]  = method
            reseller_user_data[chat_id]["paying_order_id"] = order_id
            reseller_user_data[chat_id]["state"]           = "waiting_due_txn"
        order = get_reseller_bot_order(order_id)
        number_label = "Send Money" if method == "nagad" else "Payment"
        await query.edit_message_text(
            f"💳 *{method.upper()}*\n\n📱 Number: *{number}* ({number_label})\n\n"
            f"Payment এর পর *Transaction ID* দাও:",
            parse_mode="Markdown")

    elif data == "res_back":
        reseller = get_reseller_by_chat_id(chat_id)
        name = reseller["name"] if reseller else "Bhai"
        await query.edit_message_text(f"কী করবে *{name}* bhai? 👇",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")

async def reseller_paynow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if chat_id not in reseller_user_data:
        await query.edit_message_text("❌ Session শেষ। /start দাও।"); return
    amount  = reseller_user_data[chat_id].get("amount", 0)
    product = reseller_user_data[chat_id].get("product_name", "")
    reseller_user_data[chat_id]["state"] = "waiting_pay_method_new"
    keyboard = [
        [InlineKeyboardButton("📱 Bkash", callback_data="res_pay_bkash")],
        [InlineKeyboardButton("📱 Nagad", callback_data="res_pay_nagad")],
    ]
    await query.edit_message_text(
        f"💳 *Payment Method*\n\n📦 {product}\n💵 ৳{amount}",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def reseller_pay_method_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    method  = "bkash" if query.data == "res_pay_bkash" else "nagad"
    number  = NAGAD_NUMBER if method == "nagad" else BKASH_NUMBER
    if chat_id not in reseller_user_data:
        await query.edit_message_text("❌ Session শেষ। /start দাও।"); return
    reseller_user_data[chat_id]["payment_method"] = method
    reseller_user_data[chat_id]["state"]          = "waiting_transaction"
    amount_val = reseller_user_data.get(chat_id, {}).get("amount", "?")
    product    = reseller_user_data.get(chat_id, {}).get("product_name", "")
    number_label = "Send Money" if method == "nagad" else "Payment"
    await query.edit_message_text(
        f"💳 *{method.upper()}*\n\n📦 {product}\n💵 ৳{amount_val}\n\n"
        f"📱 Number: *{number}* ({number_label})\n\nTransaction ID দাও:",
        parse_mode="Markdown")

async def reseller_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.message.chat_id
    user_state = reseller_user_data.get(chat_id, {})
    state      = user_state.get("state")
    reseller   = get_reseller_by_chat_id(chat_id)

    if not reseller:
        await update.message.reply_text("আগে /start দাও!"); return

    if state == "waiting_email":
        if "@" not in text or "." not in text:
            await update.message.reply_text("❌ Valid email দাও!"); return
        reseller_user_data[chat_id]["customer_email"] = text
        reseller_user_data[chat_id]["state"]           = "waiting_pay_choice"
        keyboard = [
            [InlineKeyboardButton("💳 এখনই Payment", callback_data="res_pay_now")],
            [InlineKeyboardButton("⏰ পরে Payment",   callback_data="res_submit_order")]
        ]
        await update.message.reply_text(
            f"✅ Email: `{text}`\n📦 {user_state['product_name']}\n💵 ৳{user_state['amount']}\n\nPayment এখন নাকি পরে?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif state == "waiting_transaction":
        if len(text) < 6:
            await update.message.reply_text("❌ TxnID minimum 6 character!"); return
        method  = user_state.get("payment_method", "bkash")
        product = user_state.get("product_name")
        email   = user_state.get("customer_email")
        amount  = user_state.get("amount")
        if not product or not email or not amount:
            await update.message.reply_text("❌ Session শেষ। /start দাও।", reply_markup=reseller_main_menu()); return
        conn = get_db()
        try:
            rows = conn.run(
                "INSERT INTO reseller_bot_orders (reseller_id,reseller_code,product,customer_email,transaction_id,payment_method,amount,status) "
                "VALUES (:rid,:code,:p,:e,:t,:m,:a,'pending') RETURNING id",
                rid=reseller["id"], code=reseller["code"], p=product, e=email, t=text, m=method, a=amount)
        finally:
            conn.close()
        order_id = rows[0][0] if rows else None
        if order_id:
            reseller_user_data[chat_id]["state"] = None
            await send_new_order_notification(order_id, reseller, product, email, amount, txn_id=text, payment_method=method)
            await update.message.reply_text(
                f"✅ *Order #{order_id} Submit!*\n\n📦 {product}\n📧 {email}\n💳 TxnID: `{text}`\n\n⏳ Admin approve করবে।",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Problem! আবার try করো।")
        reseller_user_data.pop(chat_id, None)

    elif state == "waiting_due_txn":
        if len(text) < 6:
            await update.message.reply_text("❌ TxnID minimum 6 character!"); return
        order_id = user_state.get("paying_order_id")
        method   = user_state.get("payment_method", "bkash")
        if not order_id:
            await update.message.reply_text("❌ Session শেষ।", reply_markup=reseller_main_menu()); return
        order = get_reseller_bot_order(order_id)
        conn  = get_db()
        try:
            conn.run("UPDATE reseller_bot_orders SET transaction_id=:t, payment_method=:m WHERE id=:id",
                     t=text, m=method, id=order_id)
        finally:
            conn.close()
        await send_payment_check_to_admin(order_id, reseller["code"], text, method, order["amount"])
        await update.message.reply_text(
            f"✅ Payment info পাঠানো হয়েছে!\nOrder #{order_id} | TxnID: `{text}`\n\nAdmin verify করবে।",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")
        reseller_user_data.pop(chat_id, None)

    else:
        await update.message.reply_text("Menu থেকে কাজ করো 👇", reply_markup=reseller_main_menu())

async def reseller_submit_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    chat_id   = query.message.chat_id
    chat_data = reseller_user_data.get(chat_id, {})
    reseller  = get_reseller_by_chat_id(chat_id)
    if reseller and chat_data:
        conn = get_db()
        try:
            rows = conn.run(
                "INSERT INTO reseller_bot_orders (reseller_id,reseller_code,product,customer_email,transaction_id,amount,status) "
                "VALUES (:rid,:code,:p,:e,'LATER',:a,'pending') RETURNING id",
                rid=reseller["id"], code=reseller["code"],
                p=chat_data["product_name"], e=chat_data.get("customer_email","N/A"), a=chat_data["amount"])
        finally:
            conn.close()
        order_id = rows[0][0] if rows else None
        if order_id:
            await send_new_order_notification(order_id, reseller, chat_data["product_name"],
                                              chat_data.get("customer_email","N/A"), chat_data["amount"])
            await query.edit_message_text(
                f"✅ *Order #{order_id} Submit!*\n\n📦 {chat_data['product_name']}\n📧 {chat_data.get('customer_email','N/A')}\n\n⏳ Admin approve করবে।",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        reseller_user_data.pop(chat_id, None)

# =================== FLASK WEBHOOK ===================

@app.route("/webhook/woocommerce", methods=["POST"])
def woocommerce_webhook():
    try:
        raw = request.data
        if not raw: return jsonify({"status":"ok"}), 200
        try: data = json.loads(raw)
        except: return jsonify({"status":"ok"}), 200
        if not data: return jsonify({"status":"ok"}), 200

        order_id       = str(data.get("id", "N/A"))
        customer       = data.get("billing", {})
        customer_name  = f"{customer.get('first_name','')} {customer.get('last_name','')}".strip() or "Unknown"
        customer_email = customer.get("email", "")
        total          = float(data.get("total", 0))
        status         = data.get("status", "pending")
        items_text     = ", ".join([f"{i['name']} x{i['quantity']}" for i in data.get("line_items", [])])

        conn = get_db()
        try:
            conn.run("INSERT INTO orders (woo_order_id,customer_name,customer_email,total,status,items) VALUES (:o,:n,:e,:t,:s,:i)",
                     o=order_id, n=customer_name, e=customer_email, t=total, s=status, i=items_text)
            conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'auto')",
                     a=total, n=f"WooCommerce Order #{order_id}")
        finally:
            conn.close()

        msg = (f"🛍️ *নতুন WooCommerce Order!*\n\n📋 #{order_id}\n👤 {customer_name}\n"
               f"📧 {customer_email}\n📦 {items_text}\n💵 ৳{total}\n📊 {status}")
        asyncio.run_coroutine_threadsafe(send_telegram_message(msg), main_loop)
        return jsonify({"status":"ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status":"error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"running","bkash":BKASH_NUMBER}), 200

async def send_telegram_message(message):
    from telegram import Bot
    await Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# =================== MAIN ===================

async def main():
    global main_loop
    main_loop = asyncio.get_event_loop()
    setup_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # ── Main bot ──
    main_app = Application.builder().token(BOT_TOKEN).build()
    main_app.add_handler(CommandHandler("start",          start))
    main_app.add_handler(CommandHandler("income",         income_command))
    main_app.add_handler(CommandHandler("customer",       customer_command))
    main_app.add_handler(CommandHandler("addreseller",    addreseller_command))
    main_app.add_handler(CommandHandler("removereseller", removereseller_command))
    main_app.add_handler(CommandHandler("listresellers",  listresellers_command))
    main_app.add_handler(CommandHandler("rsale",          resellersale_command))
    main_app.add_handler(CommandHandler("paylater",       paylater_command))
    main_app.add_handler(CommandHandler("sub",            subscription_command))
    main_app.add_handler(CommandHandler("newsub",         new_subscription_command))
    main_app.add_handler(CallbackQueryHandler(button_handler))
    main_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ── Reseller bot ──
    reseller_conv = ConversationHandler(
        entry_points=[CommandHandler("start", reseller_start)],
        states={WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reseller_handle_code)]},
        fallbacks=[CommandHandler("start", reseller_start)]
    )
    reseller_app = Application.builder().token(RESELLER_BOT_TOKEN).build()
    reseller_app.add_handler(reseller_conv)
    reseller_app.add_handler(CallbackQueryHandler(reseller_paynow_handler,      pattern="^res_pay_now$"))
    reseller_app.add_handler(CallbackQueryHandler(reseller_submit_order_handler,pattern="^res_submit_order$"))
    reseller_app.add_handler(CallbackQueryHandler(reseller_pay_method_handler,  pattern="^res_pay_bkash$"))
    reseller_app.add_handler(CallbackQueryHandler(reseller_pay_method_handler,  pattern="^res_pay_nagad$"))
    reseller_app.add_handler(CallbackQueryHandler(reseller_button_handler))
    reseller_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reseller_handle_text))

    logger.info("✅ Both bots started!")

    async with main_app, reseller_app:
        await main_app.initialize()
        await reseller_app.initialize()
        await main_app.start()
        await reseller_app.start()
        await main_app.updater.start_polling()
        await reseller_app.updater.start_polling()
        asyncio.create_task(send_payment_reminders())
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
