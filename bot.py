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

        # Main table for reseller bot orders
        # Status flow: pending → account_delivered → payment_due → completed / rejected
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

        # safe migrations for existing DB
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
        req.put(f"https://favouritedeals.online/wp-json/wc/v3/orders/{woo_id}",
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
            WHERE rbo.created_at >= :s
            ORDER BY rbo.created_at DESC
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
        woo = conn.run(
            "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        res = conn.run(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM reseller_bot_orders "
            "WHERE created_at>=:s AND status != 'rejected'", s=since)
        res_detail = conn.run("""
            SELECT rbo.reseller_code, r.name, rbo.product, rbo.amount, rbo.status, rbo.created_at
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id=rbo.reseller_id
            WHERE rbo.created_at>=:s
            ORDER BY rbo.created_at DESC
        """, s=since)
    finally:
        conn.close()

    woo_count = woo[0][0] or 0
    woo_total = float(woo[0][1] or 0)
    res_count = res[0][0] or 0
    res_total = float(res[0][1] or 0)
    detail_list = [
        {"reseller_code": r[0], "reseller_name": r[1], "product": r[2],
         "amount": f"৳{r[3]}", "status": r[4], "time": str(r[5])}
        for r in res_detail
    ]
    return {
        "woocommerce_orders":   woo_count,
        "woocommerce_revenue":  f"৳{woo_total:.0f}",
        "reseller_bot_orders":  res_count,
        "reseller_bot_revenue": f"৳{res_total:.0f}",
        "total_orders":         woo_count + res_count,
        "total_revenue":        f"৳{woo_total + res_total:.0f}",
        "reseller_order_details": detail_list
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
        return {
            "id": rows[0][0], "reseller_code": rows[0][1], "product": rows[0][2],
            "customer_email": rows[0][3], "amount": str(rows[0][4]),
            "status": rows[0][5], "transaction_id": rows[0][6],
            "payment_method": rows[0][7], "reminder_count": rows[0][8]
        }
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
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2], "code": rows[0][3]}
    return None

def get_reseller_by_code(code):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,name,phone FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)", c=code)
    finally:
        conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2]}
    return None

def get_payment_due_orders():
    """payment_due status — যারা account পেয়েছে কিন্তু টাকা দেয়নি"""
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT id, reseller_code, product, customer_email, amount,
                   payment_reminder_count, payment_due_at
            FROM reseller_bot_orders
            WHERE status = 'payment_due'
        """)
    finally:
        conn.close()
    return [{"id":r[0],"reseller_code":r[1],"product":r[2],"customer_email":r[3],
             "amount":str(r[4]),"reminder_count":r[5],"due_at":str(r[6])} for r in rows]

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
            bot = Bot(token=RESELLER_BOT_TOKEN)
            await bot.send_message(chat_id=rows[0][0], text=message, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Reseller notify error [{reseller_code}]: {e}")

async def send_new_order_notification(order_id, reseller, product_name, customer_email, amount, txn_id=None, payment_method=None):
    """
    Reseller bot এ order → main bot এ admin কে notification।
    Admin দেখবে: Approve (account দেব) / Reject।
    """
    try:
        from telegram import Bot
        txn_line = ""
        if txn_id and txn_id != "LATER":
            method_label = "Nagad" if payment_method == "nagad" else "Bkash"
            txn_line = f"💳 {method_label} TxnID: `{txn_id}`\n"
        msg = (
            f"🔔 *নতুন Reseller Order!*\n\n"
            f"👤 {reseller['name']}  (`{reseller['code']}`)\n"
            f"📦 {product_name}\n"
            f"📧 Customer Email: `{customer_email}`\n"
            f"💵 Amount: ৳{amount}\n"
            f"{txn_line}"
            f"🆔 Order #{order_id}\n\n"
            f"Stock আছে + টাকা পেলে Approve করো।"
        )
        keyboard = [[
            InlineKeyboardButton("✅ Approve করব",  callback_data=f"rapprove_{order_id}"),
            InlineKeyboardButton("❌ Reject করব",   callback_data=f"rreject_{order_id}")
        ]]
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=MAIN_CHAT_ID, text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        logger.info(f"✅ New order notification sent — #{order_id}")
    except Exception as e:
        logger.error(f"New order notify error #{order_id}: {e}")

async def send_account_delivered_to_reseller(order_id, reseller_code, product, customer_email, amount):
    """Admin account দিল → reseller কে জানাও"""
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
            keyboard = [[
                InlineKeyboardButton("✅ Client পেয়েছে!", callback_data=f"res_client_got_{order_id}")
            ]]
            await Bot(token=RESELLER_BOT_TOKEN).send_message(
                chat_id=rows[0][0],
                text=(
                    f"🎉 *Account Delivered!*\n\n"
                    f"Order #{order_id} — *{product}*\n"
                    f"📧 Customer Email: `{customer_email}`\n\n"
                    f"✅ Invitation পাঠানো হয়েছে!\n"
                    f"Client কে জিজ্ঞেস করো account পেয়েছে কিনা।\n\n"
                    f"Client confirm করলে নিচের button press করো 👇"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Account delivered notify error [{reseller_code}]: {e}")

async def send_payment_check_to_admin(order_id, reseller_code, txn_id, payment_method, amount):
    """Reseller payment করল → admin কে verify করতে বলো"""
    try:
        from telegram import Bot
        method_label = "Nagad" if payment_method == "nagad" else "Bkash"
        msg = (
            f"💰 *Payment Verification দরকার!*\n\n"
            f"Reseller: `{reseller_code}`\n"
            f"Order: #{order_id}\n"
            f"📲 {method_label} TxnID: `{txn_id}`\n"
            f"💵 Amount: ৳{amount}\n\n"
            f"TxnID check করে নিচের button press করো 👇"
        )
        keyboard = [[
            InlineKeyboardButton("✅ টাকা পেয়েছি — Complete", callback_data=f"rcomplete_{order_id}"),
            InlineKeyboardButton("❌ TxnID ভুল",               callback_data=f"rwrong_txn_{order_id}")
        ]]
        await Bot(token=BOT_TOKEN).send_message(
            chat_id=MAIN_CHAT_ID, text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Payment check notify error: {e}")

# =================== BACKGROUND: PAYMENT REMINDER ===================

async def send_payment_reminders():
    """প্রতি ৩ ঘন্টায় payment_due orders এর reseller + admin কে remind করে"""
    while True:
        await asyncio.sleep(3 * 60 * 60)
        try:
            due_orders = get_payment_due_orders()
            for order in due_orders:
                conn = get_db()
                try:
                    conn.run(
                        "UPDATE reseller_bot_orders SET payment_reminder_count=payment_reminder_count+1 "
                        "WHERE id=:id", id=order["id"])
                finally:
                    conn.close()

                count = order["reminder_count"] + 1

                # Reseller কে remind — with payment button
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
                        remind_keyboard = [[
                            InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order['id']}")
                        ]]
                        await TBot(token=RESELLER_BOT_TOKEN).send_message(
                            chat_id=rr[0][0],
                            text=(
                                f"⏰ *Payment Reminder #{count}*\n\n"
                                f"Bhai, tomar order #{order['id']} er account already deliver hoye geche!\n"
                                f"Kintu payment ekhono baaki ache.\n\n"
                                f"📦 {order['product']}\n"
                                f"💵 Amount: ৳{order['amount']}\n\n"
                                f"Joto taratari payment korbe toto valo 👇"
                            ),
                            reply_markup=InlineKeyboardMarkup(remind_keyboard),
                            parse_mode="Markdown"
                        )
                except Exception as e:
                    logger.error(f"Reseller reminder notify error: {e}")

                # Admin কে remind + manual message পাঠানোর button
                try:
                    from telegram import Bot
                    msg = (
                        f"⚠️ *Auto Reminder #{count} sent!*\n\n"
                        f"Reseller: `{order['reseller_code']}`\n"
                        f"Order #{order['id']} — {order['product']}\n"
                        f"💵 ৳{order['amount']} — Payment বাকি\n\n"
                        f"চাইলে manually message পাঠাও 👇"
                    )
                    keyboard = [[
                        InlineKeyboardButton(
                            "📩 Manual Message পাঠাও",
                            callback_data=f"rsend_reminder_{order['id']}")
                    ]]
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=CHAT_ID, text=msg,
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Admin reminder notify error: {e}")

        except Exception as e:
            logger.error(f"Payment reminder loop error: {e}")

# =================== AI (GPT-4o) ===================

AI_FUNCTIONS = [
    {
        "name": "get_recent_orders",
        "description": "Recent WooCommerce website orders dekhao",
        "parameters": {"type":"object","properties":{
            "limit":  {"type":"integer"},
            "status": {"type":"string"}
        }}
    },
    {
        "name": "get_last_order",
        "description": "WooCommerce er sorboshesh ekta order",
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "update_order_status",
        "description": "WooCommerce order er status change koro",
        "parameters": {"type":"object","properties":{
            "order_id":   {"type":"string"},
            "new_status": {"type":"string"},
            "use_woo_id": {"type":"boolean"}
        },"required":["order_id","new_status"]}
    },
    {
        "name": "get_income_summary",
        "description": "Income summary — days diye filter korte paro",
        "parameters": {"type":"object","properties":{
            "days": {"type":"integer"}
        }}
    },
    {
        "name": "get_combined_today_summary",
        "description": (
            "Aajker MOTA HISHAB — website (WooCommerce) + reseller bot duitoi milie. "
            "'Aaj koyta order ashche', 'aaj er total', 'aaj ki ki hoyeche' — "
            "ei dharoner jigges hole OBOSSOI ei function call koro."
        ),
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "get_today_reseller_bot_orders",
        "description": (
            "Shudhu reseller bot er aajker orders. "
            "'Kon reseller aaj order korche', 'reseller theke aaj ki ashche' — ei khhetre use koro."
        ),
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "get_reseller_summary",
        "description": (
            "Specific reseller er summary — manual + bot orders milie. "
            "Code (RS001) ba naam diye filter koro."
        ),
        "parameters": {"type":"object","properties":{
            "reseller_name": {
                "type":"string",
                "description": "Reseller code ba naam (partial match, case-insensitive)"
            }
        }}
    },
    {
        "name": "get_all_reseller_summary",
        "description": (
            "Sob reseller er summary — 'reseller theke koyta order ashche', "
            "'sob reseller er hishab dao' — ei khhetre use koro."
        ),
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "search_orders_by_name",
        "description": "Customer naam diye WooCommerce order khojo",
        "parameters": {"type":"object","properties":{
            "name": {"type":"string"}
        },"required":["name"]}
    },
    {
        "name": "add_income",
        "description": "Manual income add koro",
        "parameters": {"type":"object","properties":{
            "amount": {"type":"number"},
            "note":   {"type":"string"}
        },"required":["amount","note"]}
    },
    {
        "name": "save_memory",
        "description": "Important info, reminder, note save koro",
        "parameters": {"type":"object","properties":{
            "key":   {"type":"string"},
            "value": {"type":"string"}
        },"required":["key","value"]}
    },
    {
        "name": "get_all_memories",
        "description": "Sob saved notes/reminders/info dekhao",
        "parameters": {"type":"object","properties":{}}
    },
]

def execute_function(name, args):
    try:
        if name == "get_recent_orders":
            return db_get_recent_orders(args.get("limit", 5), args.get("status"))
        elif name == "get_last_order":
            return db_get_last_order()
        elif name == "update_order_status":
            ok, r = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=True)
            if not ok:
                ok, r = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=False)
            return {"success": ok, "result": r}
        elif name == "get_income_summary":
            return db_get_income_summary(args.get("days", 1))
        elif name == "get_combined_today_summary":
            return db_get_combined_today_summary()
        elif name == "get_today_reseller_bot_orders":
            return db_get_today_reseller_bot_orders()
        elif name == "get_reseller_summary":
            return db_get_reseller_summary(args.get("reseller_name"))
        elif name == "get_all_reseller_summary":
            return db_get_reseller_summary()
        elif name == "search_orders_by_name":
            return db_search_orders_by_name(args["name"])
        elif name == "add_income":
            return {"success": db_add_income(args["amount"], args["note"])}
        elif name == "save_memory":
            memory_save(args["key"], args["value"])
            return {"success": True, "saved": args["key"]}
        elif name == "get_all_memories":
            return memory_get_all()
    except Exception as e:
        return {"error": str(e)}

def build_system_prompt():
    memories = memory_get_all()
    mem_text = ""
    if memories:
        mem_text = "\n\nTomar saved notes/reminders:\n"
        for m in memories[:10]:
            mem_text += f"- {m['key']}: {m['value']}\n"

    return f"""Tumi Favourite Deals er personal business assistant. Tomar naam "FD Assistant".

Tumi shuddho Banglish e kotha bolbe — Bangla mane kintu English harf.
Chhoto chhoto sentence. Casual, friendly. Jemon:
"Bhai, aaj 3ta order ashche — 2ta website theke, 1ta reseller er."

Sob takar hishab e OBOSSOI ৳ (BDT) sign use korbe. Jemon: ৳199, ৳850.

Business flow:
1. Reseller order dey (product + customer email)
2. Tumi (admin) approve/reject koro
3. Approve korle invitation pathao → reseller er client accept kore
4. Client confirm korle reseller "Client peyeche" button press kore
5. Tokhn payment_due shuru hoy — reseller taka dey
6. Tumi verify kore complete koro

Khaas rules:
- "aaj koyta order", "aaj er summary" → get_combined_today_summary
- "reseller theke aaj ki" → get_today_reseller_bot_orders
- "sob reseller er hishab" → get_all_reseller_summary
- Specific reseller → get_reseller_summary (code/naam diye)
- Sob takar response e ৳ sign use korbe
- Spelling thik rakho — bangla shobdo English harf e lekho
{mem_text}"""

async def process_ai_message(messages_history):
    if not OPENAI_KEY:
        return None
    messages = [{"role": "system", "content": build_system_prompt()}] + messages_history
    try:
        resp = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": messages,
                  "functions": AI_FUNCTIONS, "function_call": "auto", "max_tokens": 1000},
            timeout=20
        ).json()

        if "error" in resp:
            logger.error(f"OpenAI error: {resp['error']}")
            return None

        msg = resp["choices"][0]["message"]
        if msg.get("function_call"):
            fn     = msg["function_call"]["name"]
            args   = json.loads(msg["function_call"]["arguments"])
            result = execute_function(fn, args)
            messages.append(msg)
            messages.append({"role": "function", "name": fn,
                              "content": json.dumps(result, ensure_ascii=False)})
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
    ])

# =================== MAIN BOT — HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_conversations[chat_id] = []
    await update.message.reply_text(
        "🛍️ *Favourite Deals Assistant*\n\nAssalamualaikum bhai! "
        "Ami tomar business assistant. Menu theke kaj koro othoba seedha bolo! 🤖",
        reply_markup=main_menu_keyboard(), parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")

    # reject reason waiting
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
            await notify_reseller(
                order["reseller_code"],
                f"❌ *Order #{order_id} Reject হয়েছে*\n\n"
                f"📦 {order['product']}\n"
                f"কারণ: {text}\n\n"
                f"Admin এর সাথে যোগাযোগ করো।"
            )
            await update.message.reply_text(
                f"❌ Order #{order_id} reject করা হয়েছে। Reseller কে কারণ জানানো হয়েছে।",
                reply_markup=main_menu_keyboard()
            )
        context.user_data["state"] = None
        return

    # manual reminder message waiting
    if context.user_data.get("state") == "waiting_manual_reminder":
        order_id = context.user_data.get("reminder_order_id")
        order    = get_reseller_bot_order(order_id)
        if order:
            await notify_reseller(
                order["reseller_code"],
                f"📩 *Admin Message — Order #{order_id}*\n\n{text}"
            )
            await update.message.reply_text(
                f"✅ Message পাঠানো হয়েছে reseller কে।",
                reply_markup=main_menu_keyboard()
            )
        context.user_data["state"] = None
        return

    # AI
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

    # ── ① Approve: stock আছে, account দেব ──
    if data.startswith("rapprove_"):
        order_id = int(data.split("_")[1])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run(
                    "UPDATE reseller_bot_orders SET status='account_delivered', "
                    "account_delivered_at=NOW() WHERE id=:id", id=order_id)
            finally:
                conn.close()
            # Reseller কে জানাও + "Client পেয়েছে" button দাও
            await send_account_delivered_to_reseller(
                order_id, order["reseller_code"],
                order["product"], order["customer_email"], order["amount"]
            )
            await query.edit_message_text(
                f"✅ *Order #{order_id} Approved!*\n\n"
                f"📧 {order['customer_email']} এ invitation পাঠাও।\n"
                f"Reseller কে জানানো হয়েছে — client confirm করলে payment শুরু হবে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        return

    # ── Reject ──
    elif data.startswith("rreject_"):
        order_id = int(data.split("_")[1])
        context.user_data["rejecting_order_id"] = order_id
        context.user_data["state"]               = "waiting_reject_reason"
        await query.edit_message_text(
            f"❌ Order #{order_id} reject এর কারণ লেখো\n(reseller কে পাঠানো হবে):"
        )
        return

    # ── ⑥ Complete: টাকা পেয়েছি ──
    elif data.startswith("rcomplete_"):
        order_id = int(data.split("_")[1])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run(
                    "UPDATE reseller_bot_orders SET status='completed', completed_at=NOW() WHERE id=:id",
                    id=order_id)
                conn.run(
                    "INSERT INTO income (amount,note,type) VALUES (:a,:n,'reseller')",
                    a=float(order["amount"]),
                    n=f"Reseller #{order_id} — {order['product']} ({order['reseller_code']})")
            finally:
                conn.close()
            await notify_reseller(
                order["reseller_code"],
                f"🎉 *Order #{order_id} সম্পন্ন!*\n\n"
                f"টাকা পেয়েছি। Order complete!\n\n"
                f"📦 {order['product']}\n"
                f"💵 ৳{order['amount']}\n\n"
                f"ধন্যবাদ! পরের order এর জন্য অপেক্ষায় আছি। 🙏"
            )
            await query.edit_message_text(
                f"✅ *Order #{order_id} Complete!*\n"
                f"💵 ৳{order['amount']} income এ যোগ হয়েছে।\n"
                f"Reseller কে জানানো হয়েছে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        return

    # ── TxnID ভুল ──
    elif data.startswith("rwrong_txn_"):
        order_id = int(data.split("_")[2])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run(
                    "UPDATE reseller_bot_orders SET transaction_id=NULL WHERE id=:id",
                    id=order_id)
            finally:
                conn.close()
            await notify_reseller(
                order["reseller_code"],
                f"❌ *Order #{order_id} — TxnID ভুল!*\n\n"
                f"Admin verify করতে পারেনি। সঠিক TxnID দাও।\n"
                f"'Amar Orders' → '💳 Payment Korbo' button press koro!"
            )
            await query.edit_message_text(
                f"❌ Order #{order_id} এর TxnID ভুল বলা হয়েছে। Reseller কে জানানো হয়েছে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        return

    # ── Manual reminder message ──
    elif data.startswith("rsend_reminder_"):
        order_id = int(data.split("_")[2])
        context.user_data["reminder_order_id"] = order_id
        context.user_data["state"]              = "waiting_manual_reminder"
        order = get_reseller_bot_order(order_id)
        await query.edit_message_text(
            f"📩 Order #{order_id} ({order['reseller_code']}) কে কী message পাঠাবে?\n\n"
            f"Type করো (reseller bot এ সরাসরি যাবে):"
        )
        return

    # ── Menu navigation ──
    if   data == "today_orders":               await show_orders(query, days=1)
    elif data == "week_orders":                await show_orders(query, days=7)
    elif data == "today_income":               await show_income(query, days=1)
    elif data == "month_report":               await show_month_report(query)
    elif data == "resellers":                  await show_resellers(query)
    elif data == "pending_orders":             await show_orders_by_status(query, "pending")
    elif data == "reseller_bot_orders_today":  await show_reseller_bot_orders_today(query)
    elif data == "manual_income":
        await query.edit_message_text("💰 Format: `/income 500 bkash e paisi`", parse_mode="Markdown")
    elif data == "search_customer":
        await query.edit_message_text("🔍 Format: `/customer example@email.com`", parse_mode="Markdown")
    elif data == "menu":
        await query.edit_message_text(
            "🛍️ *FD Assistant*\n\nMenu theke kaj koro ba seedha bolo:",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif data.startswith("status_"):
        await show_status_options(query, data.split("_")[1])
    elif data.startswith("setstatus_"):
        parts = data.split("_")
        await update_order_status_btn(query, parts[1], parts[2])
    elif data.startswith("confirm_remove_reseller_"):
        # confirm_remove_reseller_{reseller_id}_{code}
        parts       = data.split("_", 4)   # ['confirm', 'remove', 'reseller', id, code]
        reseller_id = int(parts[3])
        code        = parts[4]
        conn = get_db()
        try:
            rows = conn.run("SELECT name, phone FROM resellers WHERE id=:id", id=reseller_id)
            if not rows:
                await query.edit_message_text("❌ Reseller পাওয়া যায়নি।",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])); return
            name, phone = rows[0][0], rows[0][1]
            conn.run("DELETE FROM resellers WHERE id=:id", id=reseller_id)
        finally:
            conn.close()
        await query.edit_message_text(
            f"🗑️ *Reseller বাদ দেওয়া হয়েছে!*\n\n"
            f"👤 {name} | 📞 {phone} | 🔑 `{code}`\n\n"
            f"_(এই reseller আর bot এ login করতে পারবে না।)_",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
            parse_mode="Markdown")

# ── Display helpers ──

STATUS_EMOJI = {
    STATUS_PENDING:           "🕐",
    STATUS_ACCOUNT_DELIVERED: "📦",
    STATUS_PAYMENT_DUE:       "💰",
    STATUS_COMPLETED:         "✅",
    STATUS_REJECTED:          "❌",
}
STATUS_LABEL = {
    STATUS_PENDING:           "Pending",
    STATUS_ACCOUNT_DELIVERED: "Account Delivered",
    STATUS_PAYMENT_DUE:       "Payment Due",
    STATUS_COMPLETED:         "Completed",
    STATUS_REJECTED:          "Rejected",
}

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn  = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,total,status "
            "FROM orders WHERE created_at>=:s ORDER BY created_at DESC", s=since)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text("📦 Kono order nei bhai.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])); return
    text = f"📦 *Last {days} diner WooCommerce orders ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status change", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_orders_by_status(query, status):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,total,status "
            "FROM orders WHERE status=:s ORDER BY created_at DESC LIMIT 10", s=status)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text(f"📦 {status} status e kono order nei.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])); return
    text = f"📦 *{status} orders ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status change", callback_data=f"status_{o[0]}")])
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
        rb = conn.run(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM reseller_bot_orders "
            "WHERE created_at>=:s AND status='completed'", s=since)
    finally:
        conn.close()
    text = (
        f"📊 *Last 30 দিনের Report*\n\n"
        f"🌐 WooCommerce Orders: {o[0][0] or 0}টা | ৳{o[0][1] or 0}\n"
        f"🛍️ Reseller Bot Orders: {rb[0][0] or 0}টা | ৳{rb[0][1] or 0}\n\n"
        f"💰 Total Income: ৳{i[0][0] or 0}"
    )
    await query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown")

async def show_resellers(query):
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT r.name, r.phone, r.reseller_code,
                COUNT(DISTINCT ro.id),   COALESCE(SUM(ro.price*ro.quantity),0),
                COUNT(DISTINCT rbo.id),  COALESCE(SUM(rbo.amount),0)
            FROM resellers r
            LEFT JOIN reseller_orders ro ON r.id=ro.reseller_id
                AND ro.created_at>=date_trunc('month',NOW())
            LEFT JOIN reseller_bot_orders rbo ON r.id=rbo.reseller_id
                AND rbo.created_at>=date_trunc('month',NOW())
                AND rbo.status!='rejected'
            GROUP BY r.id,r.name,r.phone,r.reseller_code
        """)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text("👥 Kono reseller nei.\n\nAdd: `/addreseller naam phone CODE`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
            parse_mode="Markdown"); return
    text = "👥 *এই মাসের Reseller Report:*\n\n"
    for r in rows:
        t_orders = (r[3] or 0) + (r[5] or 0)
        t_amount = float(r[4] or 0) + float(r[6] or 0)
        text += (f"🔸 *{r[0]}* ({r[1]}) — `{r[2] or 'N/A'}`\n"
                 f"   Manual: {r[3]}টা | Bot: {r[5]}টা | মোট: {t_orders}টা\n"
                 f"   💵 ৳{t_amount:.0f}\n\n")
    await query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown")

async def show_reseller_bot_orders_today(query):
    orders = db_get_today_reseller_bot_orders()
    if not orders:
        await query.edit_message_text("🛍️ আজ এখনো কোনো reseller bot order নেই।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])); return
    text = f"🛍️ *আজকের Reseller Bot Orders ({len(orders)}টা):*\n\n"
    keyboard = []
    for o in orders:
        emoji = STATUS_EMOJI.get(o["status"], "❓")
        label = STATUS_LABEL.get(o["status"], o["status"])
        text += (f"{emoji} #{o['id']} — *{o['reseller_name']}* (`{o['reseller_code']}`)\n"
                 f"   📦 {o['product']} | {o['amount']} | {label}\n"
                 f"   📧 {o['email']}\n\n")
        if o["status"] == STATUS_PENDING:
            keyboard.append([
                InlineKeyboardButton(f"✅ Approve #{o['id']}", callback_data=f"rapprove_{o['id']}"),
                InlineKeyboardButton(f"❌ Reject #{o['id']}",  callback_data=f"rreject_{o['id']}")
            ])
        elif o["status"] == STATUS_PAYMENT_DUE:
            keyboard.append([
                InlineKeyboardButton(f"📩 Remind #{o['id']}", callback_data=f"rsend_reminder_{o['id']}")
            ])
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
        await update.message.reply_text(f"✅ ৳{amount} income add hoye geche!\n📝 {note}")
    except:
        await update.message.reply_text("❌ Vul format!")

async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /customer [email]"); return
    email = context.args[0].lower()
    conn  = get_db()
    try:
        rows = conn.run(
            "SELECT woo_order_id,customer_name,total,status,created_at "
            "FROM orders WHERE LOWER(customer_email)=:e ORDER BY created_at DESC", e=email)
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text(f"❌ {email} এ কোনো order নেই।"); return
    total_spent = sum(float(o[2]) for o in rows)
    text = f"👤 *{rows[0][1]}*\n📧 {email}\n\n"
    for o in rows:
        emoji = "✅" if o[3] == "completed" else "⏳" if o[3] == "processing" else "❌"
        text += f"{emoji} #{o[0]} — {o[4].strftime('%d %b %Y')} | ৳{o[2]} | {o[3]}\n"
    text += f"\n💰 *মোট: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def addreseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Format: /addreseller [naam] [phone] [CODE]"); return
    name, phone, code = context.args[0], context.args[1], context.args[2].upper()
    conn = get_db()
    try:
        conn.run("INSERT INTO resellers (name,phone,reseller_code) VALUES (:n,:p,:c)",
                 n=name, p=phone, c=code)
    finally:
        conn.close()
    await update.message.reply_text(f"✅ Reseller added!\n👤 {name} | 📞 {phone} | 🔑 {code}")

async def removereseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text(
            "Format: `/removereseller [CODE]`\nExample: `/removereseller RS001`",
            parse_mode="Markdown"); return
    code = context.args[0].upper()
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id, name, phone FROM resellers WHERE UPPER(reseller_code)=:c", c=code)
        if not rows:
            await update.message.reply_text(f"❌ `{code}` নামে কোনো reseller নেই।",
                parse_mode="Markdown"); return
        reseller_id   = rows[0][0]
        reseller_name = rows[0][1]
        reseller_phone= rows[0][2]
        # Pending/active orders আছে কিনা check
        active = conn.run(
            "SELECT COUNT(*) FROM reseller_bot_orders "
            "WHERE reseller_id=:rid AND status NOT IN ('completed','rejected')",
            rid=reseller_id)
        active_count = active[0][0] if active else 0
    finally:
        conn.close()

    if active_count > 0:
        # Confirm button দেখাও
        keyboard = [[
            InlineKeyboardButton(
                f"⚠️ হ্যাঁ, তারপরেও বাদ দাও",
                callback_data=f"confirm_remove_reseller_{reseller_id}_{code}"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu")
        ]]
        await update.message.reply_text(
            f"⚠️ *সতর্কতা!*\n\n"
            f"👤 {reseller_name} (`{code}`) এর *{active_count}টা active order* আছে!\n\n"
            f"বাদ দিলে সেই orders এ আর access থাকবে না।\n"
            f"তারপরেও বাদ দিতে চাও?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        # সরাসরি বাদ দাও
        keyboard = [[
            InlineKeyboardButton(f"✅ হ্যাঁ, বাদ দাও", callback_data=f"confirm_remove_reseller_{reseller_id}_{code}"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu")
        ]]
        await update.message.reply_text(
            f"🗑️ *Reseller বাদ দেবে?*\n\n"
            f"👤 {reseller_name} | 📞 {reseller_phone} | 🔑 `{code}`",
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

# =================== RESELLER BOT ===================

reseller_user_data: dict = {}

def reseller_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 নতুন Order দিব",   callback_data="res_new_order")],
        [InlineKeyboardButton("📋 আমার Orders",       callback_data="res_my_orders")],
        [InlineKeyboardButton("ℹ️ Price List",        callback_data="res_price_list")],
    ])

async def reseller_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.message.chat_id
    reseller = get_reseller_by_chat_id(chat_id)
    if reseller:
        conn = get_db()
        try:
            due = conn.run(
                "SELECT COUNT(*) FROM reseller_bot_orders "
                "WHERE reseller_code=:c AND status='payment_due'",
                c=reseller["code"])
        finally:
            conn.close()
        due_count = due[0][0] if due else 0
        due_txt   = f"\n\n⚠️ *{due_count}টা order এর payment বাকি আছে!*" if due_count > 0 else ""
        await update.message.reply_text(
            f"🛍️ Welcome back *{reseller['name']}* bhai! 👋\n"
            f"Code: `{reseller['code']}`{due_txt}\n\nKi korte chao?",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")
        return ConversationHandler.END
    await update.message.reply_text(
        "🛍️ *FD Reseller Bot*\n\nAssalamualaikum! 👋\n\n"
        "Tomar *reseller code* dao (admin er kach theke pawa):",
        parse_mode="Markdown")
    return WAITING_CODE

async def reseller_handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code     = update.message.text.strip().upper()
    chat_id  = update.message.chat_id
    reseller = get_reseller_by_code(code)
    if not reseller:
        await update.message.reply_text("❌ Ei code valid na bhai. Sothik code dao:")
        return WAITING_CODE
    conn = get_db()
    try:
        conn.run("UPDATE resellers SET telegram_chat_id=:c WHERE UPPER(reseller_code)=UPPER(:code)",
                 c=str(chat_id), code=code)
    finally:
        conn.close()
    await update.message.reply_text(
        f"✅ *Register Successful!*\n\nWelcome *{reseller['name']}* bhai! 🎉\n"
        f"Code: `{code}`\n\nEkhon order dite paro!",
        reply_markup=reseller_main_menu(), parse_mode="Markdown")
    return ConversationHandler.END

async def reseller_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data == "res_price_list":
        await query.edit_message_text(
            f"📋 *Price List:*\n\n"
            f"🤖 ChatGPT Plus Business (1 Month) — *৳{PRODUCTS['chatgpt']['price']}*\n"
            f"💎 Gemini Advanced (1 Month) — *৳{PRODUCTS['gemini']['price']}*\n\n"
            f"Order দিতে 'নতুন Order দিব' press koro!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]]),
            parse_mode="Markdown")
        return

    if data == "res_new_order":
        keyboard = [
            [InlineKeyboardButton(f"🤖 ChatGPT Plus — ৳{PRODUCTS['chatgpt']['price']}", callback_data="res_order_chatgpt")],
            [InlineKeyboardButton(f"💎 Gemini Advanced — ৳{PRODUCTS['gemini']['price']}", callback_data="res_order_gemini")],
            [InlineKeyboardButton("🔙 Back", callback_data="res_back")]
        ]
        await query.edit_message_text("🛒 *কোন product order করতে চাও?*",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("res_order_"):
        product_key = data.replace("res_order_", "")
        product     = PRODUCTS.get(product_key)
        if product:
            reseller_user_data[chat_id] = {
                "product": product_key, "product_name": product["name"],
                "amount": product["price"], "state": "waiting_email"
            }
            await query.edit_message_text(
                f"📦 *{product['name']}*\n💵 ৳{product['price']}\n\n"
                f"👇 *Customer এর Gmail/email দাও:*\n_(যার জন্য কিনছো তার email)_",
                parse_mode="Markdown")

    elif data == "res_my_orders":
        reseller = get_reseller_by_chat_id(chat_id)
        if not reseller:
            await query.edit_message_text("❌ Register koro aage. /start dao."); return
        conn = get_db()
        try:
            rows = conn.run(
                "SELECT id,product,customer_email,amount,status,transaction_id,created_at "
                "FROM reseller_bot_orders WHERE reseller_code=:c ORDER BY created_at DESC LIMIT 10",
                c=reseller["code"])
        finally:
            conn.close()
        if not rows:
            await query.edit_message_text("📋 এখনো কোনো order নেই বাই।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]])); return
        text = "📋 *তোমার Last 10 Order:*\n\n"
        keyboard = []
        for r in rows:
            emoji = STATUS_EMOJI.get(r[4], "❓")
            label = STATUS_LABEL.get(r[4], r[4])
            date_s = r[6].strftime("%d %b, %I:%M %p") if r[6] else ""
            text  += (f"{emoji} *#{r[0]}* — {r[1]}\n"
                      f"   📧 {r[2]}\n"
                      f"   💵 ৳{r[3]} | {label} | {date_s}\n\n")
            # payment_due → show payment button
            if r[4] == STATUS_PAYMENT_DUE:
                keyboard.append([
                    InlineKeyboardButton(f"💳 #{r[0]} Payment করব", callback_data=f"res_pay_order_{r[0]}")
                ])
            # account_delivered → show client confirmed button
            elif r[4] == STATUS_ACCOUNT_DELIVERED:
                keyboard.append([
                    InlineKeyboardButton(f"✅ #{r[0]} Client পেয়েছে!", callback_data=f"res_client_got_{r[0]}")
                ])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="res_back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # ── ③ Client পেয়েছে ──
    elif data.startswith("res_client_got_"):
        order_id = int(data.split("_")[3])
        order    = get_reseller_bot_order(order_id)
        if order:
            already_paid = order.get("transaction_id") and order["transaction_id"] != "LATER"

            if already_paid:
                # ─ আগেই TxnID দিয়েছে → Admin verify করবে → complete করবে
                conn = get_db()
                try:
                    conn.run(
                        "UPDATE reseller_bot_orders SET status='payment_due', payment_due_at=NOW() WHERE id=:id",
                        id=order_id)
                finally:
                    conn.close()
                # Admin কে verify করতে বলো
                try:
                    from telegram import Bot
                    txn = order["transaction_id"]
                    method_label = "Nagad" if order.get("payment_method") == "nagad" else "Bkash"
                    admin_keyboard = [[
                        InlineKeyboardButton("✅ টাকা পেয়েছি — Complete", callback_data=f"rcomplete_{order_id}"),
                        InlineKeyboardButton("❌ TxnID ভুল", callback_data=f"rwrong_txn_{order_id}")
                    ]]
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=MAIN_CHAT_ID,
                        text=(
                            f"✅ *Client Account Confirm করেছে!*\n\n"
                            f"Reseller: `{order['reseller_code']}`\n"
                            f"Order #{order_id} — {order['product']}\n"
                            f"📧 {order['customer_email']}\n"
                            f"💵 ৳{order['amount']}\n\n"
                            f"💳 {method_label} TxnID: `{txn}`\n\n"
                            f"TxnID check করে complete করো 👇"
                        ),
                        reply_markup=InlineKeyboardMarkup(admin_keyboard),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Admin txn verify notify error: {e}")

                await query.edit_message_text(
                    f"✅ *Confirmed!*\n\n"
                    f"Order #{order_id} — Client account পেয়েছে।\n\n"
                    f"তুমি আগেই payment করেছিলে। Admin TxnID verify করে complete করবে। Notify করব!",
                    reply_markup=reseller_main_menu(), parse_mode="Markdown")

            else:
                # ─ এখনো payment করেনি → payment_due শুরু
                conn = get_db()
                try:
                    conn.run(
                        "UPDATE reseller_bot_orders SET status='payment_due', payment_due_at=NOW() WHERE id=:id",
                        id=order_id)
                finally:
                    conn.close()
                # Reseller কে payment button দাও
                try:
                    conn2 = get_db()
                    try:
                        rrows = conn2.run(
                            "SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                            c=order["reseller_code"])
                    finally:
                        conn2.close()
                    if rrows and rrows[0][0]:
                        from telegram import Bot
                        pay_keyboard = [[
                            InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order_id}")
                        ]]
                        await Bot(token=RESELLER_BOT_TOKEN).send_message(
                            chat_id=rrows[0][0],
                            text=(
                                f"💰 *Payment Due শুরু হয়েছে!*\n\n"
                                f"Reseller: `{order['reseller_code']}`\n"
                                f"Order #{order_id} — {order['product']}\n"
                                f"📧 {order['customer_email']}\n"
                                f"💵 ৳{order['amount']}\n\n"
                                f"Client account confirm করেছে। এখন payment করো 👇"
                            ),
                            reply_markup=InlineKeyboardMarkup(pay_keyboard),
                            parse_mode="Markdown"
                        )
                except Exception as e:
                    logger.error(f"Reseller payment due notify error: {e}")

                # Admin কে জানাও + remind button
                try:
                    from telegram import Bot
                    admin_keyboard = [[
                        InlineKeyboardButton("📩 Remind পাঠাও", callback_data=f"rsend_reminder_{order_id}")
                    ]]
                    await Bot(token=BOT_TOKEN).send_message(
                        chat_id=MAIN_CHAT_ID,
                        text=(
                            f"💰 *Payment Due শুরু হয়েছে!*\n\n"
                            f"Reseller: `{order['reseller_code']}`\n"
                            f"Order #{order_id} — {order['product']}\n"
                            f"📧 {order['customer_email']}\n"
                            f"💵 ৳{order['amount']}\n\n"
                            f"Client account confirm করেছে। Payment এর জন্য অপেক্ষা।"
                        ),
                        reply_markup=InlineKeyboardMarkup(admin_keyboard),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Admin payment due notify error: {e}")

                # Confirmed message + payment button
                confirmed_keyboard = [[
                    InlineKeyboardButton("💳 Payment করব", callback_data=f"res_pay_order_{order_id}")
                ]]
                await query.edit_message_text(
                    f"✅ *Confirmed!*\n\n"
                    f"Order #{order_id} — Client account পেয়েছে বলে mark হয়েছে।\n\n"
                    f"এখন payment করো 👇",
                    reply_markup=InlineKeyboardMarkup(confirmed_keyboard), parse_mode="Markdown")

    # ── ⑤ Payment করব — method select ──
    elif data.startswith("res_pay_order_"):
        order_id = int(data.split("_")[3])
        order    = get_reseller_bot_order(order_id)
        if order:
            reseller_user_data[chat_id] = {
                "paying_order_id": order_id,
                "product_name":    order["product"],
                "amount":          order["amount"],
                "state":           "waiting_pay_method"
            }
            keyboard = [
                [InlineKeyboardButton("📱 Bkash",  callback_data=f"res_method_bkash_{order_id}")],
                [InlineKeyboardButton("📱 Nagad",  callback_data=f"res_method_nagad_{order_id}")],
            ]
            await query.edit_message_text(
                f"💳 *Order #{order_id} Payment*\n\n"
                f"📦 {order['product']}\n💵 ৳{order['amount']}\n\n"
                f"কোন method এ payment করবে?",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("res_method_"):
        parts    = data.split("_")   # res_method_bkash_123
        method   = parts[2]
        order_id = int(parts[3])
        number   = NAGAD_NUMBER if method == "nagad" else BKASH_NUMBER
        reseller = get_reseller_by_chat_id(chat_id)
        if chat_id in reseller_user_data:
            reseller_user_data[chat_id]["payment_method"]  = method
            reseller_user_data[chat_id]["paying_order_id"] = order_id
            reseller_user_data[chat_id]["state"]           = "waiting_due_txn"
        order = get_reseller_bot_order(order_id)
        code  = reseller["code"] if reseller else "RSCODE"
        number_label = "Send Money" if method == "nagad" else "Payment/Merchant Number"
        await query.edit_message_text(
            f"💳 *{method.upper()} Payment*\n\n"
            f"📦 {order['product']}\n"
            f"💵 Amount: ৳{order['amount']}\n\n"
            f"📱 Number: *{number}* ({number_label})\n\n"
            f"Payment এর পর *Transaction ID* দাও:\n"
            f"_(Example: `8N6A7B3X2Y`)_",
            parse_mode="Markdown")

    elif data == "res_back":
        reseller = get_reseller_by_chat_id(chat_id)
        name = reseller["name"] if reseller else "Bhai"
        await query.edit_message_text(
            f"কী করতে চাও *{name}* bhai? 👇",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")

async def reseller_paynow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Email দেওয়ার পর 'এখনই Payment করব' — method select"""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if chat_id not in reseller_user_data:
        await query.edit_message_text("❌ Session শেষ। আবার /start দাও।"); return
    amount  = reseller_user_data[chat_id].get("amount", 0)
    product = reseller_user_data[chat_id].get("product_name", "")
    reseller_user_data[chat_id]["state"] = "waiting_pay_method_new"
    keyboard = [
        [InlineKeyboardButton("📱 Bkash", callback_data="res_pay_bkash")],
        [InlineKeyboardButton("📱 Nagad", callback_data="res_pay_nagad")],
    ]
    await query.edit_message_text(
        f"💳 *Payment Method*\n\n📦 {product}\n💵 ৳{amount}\n\nকোন method এ payment করবে?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def reseller_pay_method_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    method  = "bkash" if query.data == "res_pay_bkash" else "nagad"
    number  = NAGAD_NUMBER if method == "nagad" else BKASH_NUMBER
    if chat_id not in reseller_user_data:
        await query.edit_message_text("❌ Session শেষ। আবার /start দাও।"); return
    reseller_user_data[chat_id]["payment_method"] = method
    reseller_user_data[chat_id]["state"]          = "waiting_transaction"
    amount_val = reseller_user_data.get(chat_id, {}).get("amount", "?")
    product    = reseller_user_data.get(chat_id, {}).get("product_name", "")
    reseller   = get_reseller_by_chat_id(chat_id)
    code       = reseller["code"] if reseller else "RSCODE"
    number_label = "Send Money" if method == "nagad" else "Payment/Merchant Number"
    await query.edit_message_text(
        f"💳 *{method.upper()} Payment*\n\n"
        f"📦 {product}\n💵 Amount: ৳{amount_val}\n\n"
        f"📱 Number: *{number}* ({number_label})\n\n"
        f"Payment এর পর *Transaction ID* দাও:\n"
        f"_(Example: `8N6A7B3X2Y`)_",
        parse_mode="Markdown")

async def reseller_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.message.chat_id

    user_state = reseller_user_data.get(chat_id, {})
    state      = user_state.get("state")
    reseller   = get_reseller_by_chat_id(chat_id)

    if not reseller:
        await update.message.reply_text("আগে /start দিয়ে register করো bhai!"); return

    # ── ① Customer email ──
    if state == "waiting_email":
        if "@" not in text or "." not in text:
            await update.message.reply_text("❌ Valid email দাও!\nExample: example@gmail.com"); return
        reseller_user_data[chat_id]["customer_email"] = text
        reseller_user_data[chat_id]["state"]           = "waiting_pay_choice"
        keyboard = [
            [InlineKeyboardButton("💳 এখনই Payment করব", callback_data="res_pay_now")],
            [InlineKeyboardButton("⏰ পরে Payment দিব",   callback_data="res_submit_order")]
        ]
        await update.message.reply_text(
            f"✅ Email নেওয়া হয়েছে!\n\n"
            f"📧 {text}\n📦 {user_state['product_name']}\n💵 ৳{user_state['amount']}\n\n"
            f"Payment এখন করবে নাকি পরে?\n"
            f"_(যেকোনো ক্ষেত্রে order আগে submit হবে, admin approve করবে)_",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # ── TxnID for new order (pay now) ──
    elif state == "waiting_transaction":
        if len(text) < 6:
            await update.message.reply_text("❌ TxnID minimum 6 character lagbe!"); return
        method   = user_state.get("payment_method", "bkash")
        product  = user_state.get("product_name")
        email    = user_state.get("customer_email")
        amount   = user_state.get("amount")
        if not product or not email or not amount:
            await update.message.reply_text("❌ Session data নেই। আবার /start দিয়ে নতুন order দাও।",
                reply_markup=reseller_main_menu()); return
        conn = get_db()
        try:
            rows = conn.run(
                "INSERT INTO reseller_bot_orders "
                "(reseller_id,reseller_code,product,customer_email,"
                "transaction_id,payment_method,amount,status) "
                "VALUES (:rid,:code,:p,:e,:t,:m,:a,'pending') RETURNING id",
                rid=reseller["id"], code=reseller["code"],
                p=product, e=email, t=text, m=method, a=amount)
        finally:
            conn.close()
        order_id = rows[0][0] if rows else None
        if order_id:
            reseller_user_data[chat_id]["state"] = None
            await send_new_order_notification(order_id, reseller, product, email, amount, txn_id=text, payment_method=method)
            await update.message.reply_text(
                f"✅ *Order Submit হয়েছে!*\n\n"
                f"🆔 Order #{order_id}\n📦 {product}\n📧 {email}\n"
                f"💳 TxnID: `{text}` ({method.upper()})\n💵 ৳{amount}\n\n"
                f"⏳ Admin approve করলে account পাবে। Notify করব!",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Problem hoye geche. Abar try koro.")
        reseller_user_data.pop(chat_id, None)

    # ── TxnID for due order payment ──
    elif state == "waiting_due_txn":
        if len(text) < 6:
            await update.message.reply_text("❌ TxnID minimum 6 character lagbe!"); return
        order_id = user_state.get("paying_order_id")
        method   = user_state.get("payment_method", "bkash")
        if not order_id:
            await update.message.reply_text("❌ Session data নেই। 'আমার Orders' থেকে আবার try করো।",
                reply_markup=reseller_main_menu()); return
        order = get_reseller_bot_order(order_id)
        conn  = get_db()
        try:
            conn.run(
                "UPDATE reseller_bot_orders SET transaction_id=:t, payment_method=:m WHERE id=:id",
                t=text, m=method, id=order_id)
        finally:
            conn.close()
        await send_payment_check_to_admin(order_id, reseller["code"], text, method, order["amount"])
        await update.message.reply_text(
            f"✅ *Payment Info পাঠানো হয়েছে!*\n\n"
            f"Order #{order_id}\n"
            f"💳 {method.upper()} TxnID: `{text}`\n\n"
            f"Admin verify করবে। Notify করব!",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")
        reseller_user_data.pop(chat_id, None)

    else:
        await update.message.reply_text("Menu থেকে কাজ করো বাই 👇", reply_markup=reseller_main_menu())

async def reseller_submit_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """পরে payment দেব — order submit করো"""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    chat_data = reseller_user_data.get(chat_id, {})
    reseller  = get_reseller_by_chat_id(chat_id)
    if reseller and chat_data:
        conn = get_db()
        try:
            rows = conn.run(
                "INSERT INTO reseller_bot_orders "
                "(reseller_id,reseller_code,product,customer_email,"
                "transaction_id,amount,status) "
                "VALUES (:rid,:code,:p,:e,'LATER',:a,'pending') RETURNING id",
                rid=reseller["id"], code=reseller["code"],
                p=chat_data["product_name"],
                e=chat_data.get("customer_email","N/A"),
                a=chat_data["amount"])
        finally:
            conn.close()
        order_id = rows[0][0] if rows else None
        if order_id:
            await send_new_order_notification(
                order_id, reseller,
                chat_data["product_name"],
                chat_data.get("customer_email","N/A"),
                chat_data["amount"]
            )
            await query.edit_message_text(
                f"✅ *Order #{order_id} Submit হয়েছে!*\n\n"
                f"📦 {chat_data['product_name']}\n"
                f"📧 {chat_data.get('customer_email','N/A')}\n"
                f"💵 ৳{chat_data['amount']}\n\n"
                f"⏳ Admin approve করলে account পাবে।\n"
                f"Account পাওয়ার পর client confirm করলে payment করবে।",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        reseller_user_data.pop(chat_id, None)

# =================== FLASK WEBHOOK ===================

@app.route("/webhook/woocommerce", methods=["POST"])
def woocommerce_webhook():
    try:
        raw = request.data
        if not raw:
            return jsonify({"status": "ok"}), 200
        try:
            data = json.loads(raw)
        except:
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

        conn = get_db()
        try:
            conn.run(
                "INSERT INTO orders (woo_order_id,customer_name,customer_email,total,status,items) "
                "VALUES (:o,:n,:e,:t,:s,:i)",
                o=order_id, n=customer_name, e=customer_email, t=total, s=status, i=items_text)
            conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'auto')",
                     a=total, n=f"WooCommerce Order #{order_id}")
        finally:
            conn.close()

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
    return jsonify({"status": "running", "bkash": BKASH_NUMBER}), 200

async def send_telegram_message(message):
    from telegram import Bot
    await Bot(token=BOT_TOKEN).send_message(
        chat_id=CHAT_ID, text=message, parse_mode="Markdown")

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
    main_app.add_handler(CommandHandler("start",       start))
    main_app.add_handler(CommandHandler("income",      income_command))
    main_app.add_handler(CommandHandler("customer",    customer_command))
    main_app.add_handler(CommandHandler("addreseller",    addreseller_command))
    main_app.add_handler(CommandHandler("removereseller", removereseller_command))
    main_app.add_handler(CommandHandler("rsale",          resellersale_command))
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
