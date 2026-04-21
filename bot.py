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
CHAT_ID            = os.environ.get("CHAT_ID")          # admin Telegram ID
MAIN_CHAT_ID       = os.environ.get("CHAT_ID")          # same — used inside functions
DATABASE_URL       = os.environ.get("DATABASE_URL")
WC_KEY             = os.environ.get("WC_KEY")
WC_SECRET          = os.environ.get("WC_SECRET")
OPENAI_KEY         = os.environ.get("OPENAI_API_KEY")
RESELLER_BOT_TOKEN = os.environ.get("RESELLER_BOT_TOKEN")
BKASH_NUMBER       = os.environ.get("BKASH_NUMBER", "01997806925")
NAGAD_NUMBER       = os.environ.get("NAGAD_NUMBER", "01997806925")

app        = Flask(__name__)
main_loop  = None
user_conversations = {}

WAITING_CODE = 1

PRODUCTS = {
    "chatgpt": {"name": "ChatGPT Plus (1 Month)",    "price": 199},
    "gemini":  {"name": "Gemini Advanced (1 Month)", "price": 850},
}

# =================== DATABASE ===================

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
        conn.run("""CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY, woo_order_id VARCHAR(50),
            customer_name VARCHAR(200), customer_email VARCHAR(200),
            total DECIMAL(10,2), status VARCHAR(50), items TEXT,
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS income (
            id SERIAL PRIMARY KEY, amount DECIMAL(10,2), note TEXT,
            type VARCHAR(20) DEFAULT 'manual', created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS resellers (
            id SERIAL PRIMARY KEY, name VARCHAR(200), phone VARCHAR(50),
            reseller_code VARCHAR(20), telegram_chat_id VARCHAR(50),
            created_at TIMESTAMP DEFAULT NOW())""")

        conn.run("""CREATE TABLE IF NOT EXISTS reseller_orders (
            id SERIAL PRIMARY KEY, reseller_id INTEGER REFERENCES resellers(id),
            product TEXT, quantity INTEGER, price DECIMAL(10,2),
            created_at TIMESTAMP DEFAULT NOW())""")

        # ── reseller_bot_orders: payment_method column added ──
        conn.run("""CREATE TABLE IF NOT EXISTS reseller_bot_orders (
            id SERIAL PRIMARY KEY,
            reseller_id       INTEGER REFERENCES resellers(id),
            reseller_code     VARCHAR(20),
            product           VARCHAR(100),
            customer_email    VARCHAR(200),
            transaction_id    VARCHAR(100),
            payment_method    VARCHAR(20) DEFAULT 'bkash',
            amount            DECIMAL(10,2),
            status            VARCHAR(20) DEFAULT 'pending',
            reject_reason     TEXT,
            due_reminder_count INTEGER DEFAULT 0,
            created_at        TIMESTAMP DEFAULT NOW())""")

        # add payment_method if old DB doesn't have it
        try:
            conn.run("ALTER TABLE reseller_bot_orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR(20) DEFAULT 'bkash'")
        except Exception:
            pass

        conn.run("""CREATE TABLE IF NOT EXISTS bot_memory (
            id SERIAL PRIMARY KEY, key VARCHAR(200) UNIQUE,
            value TEXT, created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW())""")
    finally:
        conn.close()
    logger.info("Database setup complete!")

# =================== MEMORY ===================

def memory_save(key, value):
    conn = get_db()
    try:
        conn.run(
            "INSERT INTO bot_memory (key, value, updated_at) VALUES (:k, :v, NOW()) "
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
        r=rows[0]
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
    """reseller_orders (manual) + reseller_bot_orders (bot) দুটোই একসাথে"""
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
                ON r.id = ro.reseller_id
                AND ro.created_at >= date_trunc('month', NOW())
            LEFT JOIN reseller_bot_orders rbo
                ON r.id = rbo.reseller_id
                AND rbo.created_at >= date_trunc('month', NOW())
                AND rbo.status != 'rejected'
        """
        if reseller_name:
            rows = conn.run(q + " WHERE UPPER(r.name) LIKE UPPER(:n) OR UPPER(r.reseller_code) LIKE UPPER(:n)"
                               " GROUP BY r.id,r.name,r.phone,r.reseller_code", n=f"%{reseller_name}%")
        else:
            rows = conn.run(q + " GROUP BY r.id,r.name,r.phone,r.reseller_code")
    finally:
        conn.close()
    result = []
    for r in rows:
        total_orders = (r[3] or 0) + (r[5] or 0)
        total_amount = float(r[4] or 0) + float(r[6] or 0)
        result.append({"name":r[0],"phone":r[1],"code":r[2],
                        "orders":total_orders,"total":str(total_amount),
                        "manual_orders":r[3] or 0,"bot_orders":r[5] or 0})
    return result

def db_get_today_reseller_bot_orders():
    """আজকের reseller bot orders — AI + admin panel উভয়ের জন্য"""
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = conn.run("""
            SELECT rbo.id, r.name, rbo.reseller_code, rbo.product,
                   rbo.customer_email, rbo.amount, rbo.status,
                   rbo.transaction_id, rbo.payment_method, rbo.created_at
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id = rbo.reseller_id
            WHERE rbo.created_at >= :s
            ORDER BY rbo.created_at DESC
        """, s=since)
    finally:
        conn.close()
    return [{"id":r[0],"reseller_name":r[1],"reseller_code":r[2],"product":r[3],
             "email":r[4],"amount":str(r[5]),"status":r[6],"txn":r[7],
             "payment_method":r[8],"created_at":str(r[9])} for r in rows]

def db_get_combined_today_summary():
    """
    আজকের সব orders: WooCommerce + reseller bot একসাথে।
    AI এর জন্য — যখন জিজ্ঞেস করে 'আজকে কতটা order'
    """
    conn = get_db()
    try:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        woo = conn.run("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at>=:s", s=since)
        res = conn.run("""SELECT COUNT(*), COALESCE(SUM(amount),0)
                          FROM reseller_bot_orders
                          WHERE created_at>=:s AND status != 'rejected'""", s=since)
        res_detail = conn.run("""
            SELECT rbo.reseller_code, r.name, rbo.product, rbo.amount, rbo.status, rbo.created_at
            FROM reseller_bot_orders rbo
            LEFT JOIN resellers r ON r.id=rbo.reseller_id
            WHERE rbo.created_at>=:s
            ORDER BY rbo.created_at DESC
        """, s=since)
    finally:
        conn.close()

    woo_count  = woo[0][0] or 0
    woo_total  = float(woo[0][1] or 0)
    res_count  = res[0][0] or 0
    res_total  = float(res[0][1] or 0)

    detail_list = []
    for r in res_detail:
        detail_list.append({
            "reseller_code": r[0], "reseller_name": r[1],
            "product": r[2], "amount": str(r[3]),
            "status": r[4], "time": str(r[5])
        })

    return {
        "woocommerce_orders":  woo_count,
        "woocommerce_revenue": woo_total,
        "reseller_bot_orders": res_count,
        "reseller_bot_revenue": res_total,
        "total_orders":  woo_count + res_count,
        "total_revenue": woo_total + res_total,
        "reseller_order_details": detail_list
    }

def get_reseller_bot_order(order_id):
    conn = get_db()
    try:
        rows = conn.run(
            "SELECT id,reseller_code,product,customer_email,amount,status,transaction_id,payment_method "
            "FROM reseller_bot_orders WHERE id=:id", id=order_id)
    finally:
        conn.close()
    if rows:
        return {"id":rows[0][0],"reseller_code":rows[0][1],"product":rows[0][2],
                "customer_email":rows[0][3],"amount":str(rows[0][4]),
                "status":rows[0][5],"transaction_id":rows[0][6],"payment_method":rows[0][7]}
    return None

def update_reseller_bot_order(order_id, status, reject_reason=None):
    conn = get_db()
    try:
        if reject_reason:
            conn.run("UPDATE reseller_bot_orders SET status=:s, reject_reason=:r WHERE id=:id",
                     s=status, r=reject_reason, id=order_id)
        else:
            conn.run("UPDATE reseller_bot_orders SET status=:s WHERE id=:id", s=status, id=order_id)
    finally:
        conn.close()

def get_reseller_by_chat_id(chat_id):
    conn = get_db()
    try:
        rows = conn.run("SELECT id,name,phone,reseller_code FROM resellers WHERE telegram_chat_id=:c",
                        c=str(chat_id))
    finally:
        conn.close()
    if rows:
        return {"id":rows[0][0],"name":rows[0][1],"phone":rows[0][2],"code":rows[0][3]}
    return None

def get_reseller_by_code(code):
    conn = get_db()
    try:
        rows = conn.run("SELECT id,name,phone FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)", c=code)
    finally:
        conn.close()
    if rows:
        return {"id":rows[0][0],"name":rows[0][1],"phone":rows[0][2]}
    return None

def get_pending_due_orders():
    conn = get_db()
    try:
        rows = conn.run("""
            SELECT id, reseller_code, product, customer_email, amount, due_reminder_count, created_at
            FROM reseller_bot_orders
            WHERE transaction_id='PORE_DIBO' AND status='approved'
        """)
    finally:
        conn.close()
    return [{"id":r[0],"reseller_code":r[1],"product":r[2],"customer_email":r[3],
             "amount":str(r[4]),"reminder_count":r[5],"created_at":str(r[6])} for r in rows]

# =================== NOTIFICATION HELPERS ===================

async def notify_reseller(reseller_code, message, parse_mode="Markdown"):
    """Reseller bot দিয়ে reseller কে message পাঠায়"""
    try:
        conn = get_db()
        try:
            rows = conn.run("SELECT telegram_chat_id FROM resellers WHERE UPPER(reseller_code)=UPPER(:c)",
                            c=reseller_code)
        finally:
            conn.close()
        if rows and rows[0][0]:
            from telegram import Bot
            bot = Bot(token=RESELLER_BOT_TOKEN)
            await bot.send_message(chat_id=rows[0][0], text=message, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Reseller notify error [{reseller_code}]: {e}")

async def send_main_notification(order_id, reseller, chat_data, txn_id, payment_method="bkash"):
    """
    Reseller bot এ order হলে → main bot এ admin কে notification পাঠায়।
    Approve / Reject / Check Payment button সহ।
    """
    try:
        from telegram import Bot
        main_bot = Bot(token=BOT_TOKEN)

        is_due = (txn_id in ("PORE_DIBO", "PORE_DIBO (Due)"))
        due_line = "⚠️ *DUE ORDER — Payment বাকি আছে!*\n\n" if is_due else ""
        pay_line = f"💳 TxnID: `{txn_id}`\n" if not is_due else "💳 Payment: *এখনো হয়নি* (পরে দেবে)\n"
        method_line = f"📲 Method: {payment_method.upper()}\n" if not is_due else ""

        msg = (
            f"🔔 *নতুন Reseller Order!*\n\n{due_line}"
            f"👤 {reseller['name']}  (`{reseller['code']}`)\n"
            f"📦 {chat_data['product_name']}\n"
            f"📧 {chat_data.get('customer_email', 'N/A')}\n"
            f"{pay_line}"
            f"{method_line}"
            f"💵 ৳{chat_data['amount']}\n"
            f"🆔 Order #{order_id}"
        )

        keyboard = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"rapprove_{order_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"rreject_{order_id}")
        ]]
        await main_bot.send_message(
            chat_id=MAIN_CHAT_ID, text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        logger.info(f"Main bot notification sent — order #{order_id}")
    except Exception as e:
        logger.error(f"Main bot notify error for order #{order_id}: {e}")

async def send_payment_check_notification(order_id, reseller_code, txn_id, payment_method):
    """
    Reseller payment করলে → admin কে চেক করতে বলে (main bot এ)
    """
    try:
        from telegram import Bot
        main_bot = Bot(token=BOT_TOKEN)
        method_emoji = "📱" if payment_method == "nagad" else "💳"
        msg = (
            f"💰 *Payment Verification দরকার!*\n\n"
            f"Reseller: `{reseller_code}`\n"
            f"Order: #{order_id}\n"
            f"{method_emoji} {payment_method.upper()} TxnID: `{txn_id}`\n\n"
            f"TxnID verify করে নিচের button এ click করো 👇"
        )
        keyboard = [[
            InlineKeyboardButton("✅ টাকা পেয়েছি — Complete", callback_data=f"rcomplete_{order_id}"),
            InlineKeyboardButton("❌ TxnID ভুল",               callback_data=f"rwrong_txn_{order_id}")
        ]]
        await main_bot.send_message(
            chat_id=MAIN_CHAT_ID, text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Payment check notify error: {e}")

# =================== DUE REMINDER BACKGROUND TASK ===================

async def send_due_reminders():
    while True:
        await asyncio.sleep(3 * 60 * 60)   # 3 ঘন্টা পর পর
        try:
            due_orders = get_pending_due_orders()
            for order in due_orders:
                conn = get_db()
                try:
                    conn.run("UPDATE reseller_bot_orders SET due_reminder_count=due_reminder_count+1 WHERE id=:id",
                             id=order['id'])
                finally:
                    conn.close()

                reseller_msg = (
                    f"⏰ *Payment Reminder #{order['reminder_count']+1}*\n\n"
                    f"Bhai, tomar order #{order['id']} approved hoye geche,\n"
                    f"kintu payment ekhono baaki!\n\n"
                    f"📦 {order['product']}\n"
                    f"📧 {order['customer_email']}\n"
                    f"💵 Amount: ৳{order['amount']}\n\n"
                    f"Payment korte 'Amar Orders' → 'Payment Korbo' button press koro!"
                )
                await notify_reseller(order['reseller_code'], reseller_msg)

                from telegram import Bot
                admin_bot = Bot(token=BOT_TOKEN)
                await admin_bot.send_message(
                    chat_id=CHAT_ID,
                    text=(f"⚠️ *Due Reminder #{order['reminder_count']+1} sent!*\n"
                          f"Reseller: `{order['reseller_code']}`\n"
                          f"Order #{order['id']} — ৳{order['amount']}"),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Due reminder error: {e}")

# =================== AI (GPT-4o) ===================

AI_FUNCTIONS = [
    {
        "name": "get_recent_orders",
        "description": "Recent WooCommerce website orders dekhao",
        "parameters": {"type":"object","properties":{
            "limit": {"type":"integer"},
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
        "description": "WooCommerce order status change koro",
        "parameters": {"type":"object","properties":{
            "order_id": {"type":"string"},
            "new_status": {"type":"string"},
            "use_woo_id": {"type":"boolean"}
        },"required":["order_id","new_status"]}
    },
    {
        "name": "get_income_summary",
        "description": "Income summary dao (manual + auto)",
        "parameters": {"type":"object","properties":{"days":{"type":"integer"}}}
    },
    {
        "name": "get_combined_today_summary",
        "description": (
            "Aajker TOTAL orders dekhao — website (WooCommerce) + reseller bot duitoi milie. "
            "'Aaj koyta order ashche', 'aaj er summary', 'aaj ki ki hoyeche' "
            "— ei dharoner jigges hole OBOSSOI ei function call koro. "
            "Reseller er details o thakbe."
        ),
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "get_today_reseller_bot_orders",
        "description": (
            "Shudhu reseller bot er aajker orders dekhao. "
            "'Kon reseller aaj order korche', 'reseller theke aaj ki ashche' "
            "— ei khhetre use koro."
        ),
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "get_reseller_summary",
        "description": (
            "Specific reseller er summary — manual + bot orders milie. "
            "Code (jemon RS001) ba naam diye filter korte paro."
        ),
        "parameters": {"type":"object","properties":{
            "reseller_name": {
                "type":"string",
                "description":"Reseller code ba naam (partial match hobe, case-insensitive)"
            }
        }}
    },
    {
        "name": "get_all_reseller_summary",
        "description": (
            "Sob reseller er summary ekসাথে — 'reseller theke koyta order ashche', "
            "'sob reseller er hishab dao' — ei khhetre use koro."
        ),
        "parameters": {"type":"object","properties":{}}
    },
    {
        "name": "search_orders_by_name",
        "description": "Customer naam diye WooCommerce order khojo",
        "parameters": {"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}
    },
    {
        "name": "add_income",
        "description": "Manual income add koro",
        "parameters": {"type":"object","properties":{
            "amount":{"type":"number"},
            "note":{"type":"string"}
        },"required":["amount","note"]}
    },
    {
        "name": "save_memory",
        "description": "Important info, reminder, note save koro",
        "parameters": {"type":"object","properties":{
            "key":{"type":"string"},
            "value":{"type":"string"}
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
            return db_get_recent_orders(args.get("limit",5), args.get("status"))
        elif name == "get_last_order":
            return db_get_last_order()
        elif name == "update_order_status":
            ok, r = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=True)
            if not ok:
                ok, r = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=False)
            return {"success": ok, "result": r}
        elif name == "get_income_summary":
            return db_get_income_summary(args.get("days",1))
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
"Haan, income add korlam."

Khaas rules:
- "aaj koyta order", "aaj er summary" jigges korle: get_combined_today_summary call koro.
- "reseller theke koyta/kon order" jigges korle: get_today_reseller_bot_orders call koro.
- "sob reseller" jigges korle: get_all_reseller_summary call koro.
- Specific reseller (jemon "RS001 er order"): get_reseller_summary call koro name/code diye.
- Kono kaaj korle confirm kore dao.
- Important info dile save_memory te rakho.
- Spelling thik rakho — bangla shobdo English harf e lekho, kono mix koro na.
{mem_text}"""

async def process_ai_message(messages_history):
    if not OPENAI_KEY:
        return None
    messages = [{"role":"system","content":build_system_prompt()}] + messages_history
    try:
        resp = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
            json={"model":"gpt-4o","messages":messages,
                  "functions":AI_FUNCTIONS,"function_call":"auto","max_tokens":1000},
            timeout=20
        ).json()

        if "error" in resp:
            logger.error(f"OpenAI error: {resp['error']}")
            return None

        msg = resp["choices"][0]["message"]
        if msg.get("function_call"):
            fn   = msg["function_call"]["name"]
            args = json.loads(msg["function_call"]["arguments"])
            result = execute_function(fn, args)
            messages.append(msg)
            messages.append({"role":"function","name":fn,"content":json.dumps(result, ensure_ascii=False)})
            resp2 = req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
                json={"model":"gpt-4o","messages":messages,"max_tokens":600},
                timeout=20
            ).json()
            if "error" in resp2:
                return None
            return resp2["choices"][0]["message"]["content"]
        return msg.get("content")
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# =================== MAIN BOT — MENUS ===================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 আজকের WooCommerce Order", callback_data="today_orders"),
         InlineKeyboardButton("💰 আজকের Income",           callback_data="today_income")],
        [InlineKeyboardButton("📅 ৭ দিনের Order",         callback_data="week_orders"),
         InlineKeyboardButton("📊 মাসের Report",           callback_data="month_report")],
        [InlineKeyboardButton("👥 Reseller",               callback_data="resellers"),
         InlineKeyboardButton("➕ Manual Income",           callback_data="manual_income")],
        [InlineKeyboardButton("🔍 Customer খোঁজো",        callback_data="search_customer"),
         InlineKeyboardButton("⏳ Pending Orders",          callback_data="pending_orders")],
        [InlineKeyboardButton("🛍️ Reseller Bot Orders আজ", callback_data="reseller_bot_orders_today")],
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

    # /txn_ command handling
    if text.startswith("/txn_"):
        parts = text.split("_")
        if len(parts) >= 3:
            try:
                order_id = int(parts[1])
                txn_id   = "_".join(parts[2:])
                conn = get_db()
                try:
                    conn.run("UPDATE reseller_bot_orders SET transaction_id=:t WHERE id=:id",
                             t=txn_id, id=order_id)
                finally:
                    conn.close()
                await update.message.reply_text(
                    f"✅ Order #{order_id} er transaction ID update hoye geche: `{txn_id}`",
                    parse_mode="Markdown"
                )
            except Exception:
                await update.message.reply_text("❌ Format thik na. Example: /txn_1_TXN123456")
        return

    # reject reason waiting state
    if context.user_data.get("state") == "waiting_reject_reason":
        order_id = context.user_data.get("rejecting_order_id")
        order    = get_reseller_bot_order(order_id)
        if order:
            update_reseller_bot_order(order_id, "rejected", text)
            await notify_reseller(
                order["reseller_code"],
                f"❌ Tomar order #{order_id} reject hoye geche bhai.\nKaron: {text}\n\nAdmin er sathe jogajog koro."
            )
            await update.message.reply_text(
                f"❌ Order #{order_id} rejected! Reseller ke notify kora hoye geche.",
                reply_markup=main_menu_keyboard()
            )
        context.user_data["state"] = None
        return

    # AI message
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
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Approve reseller order ──
    if data.startswith("rapprove_"):
        order_id = int(data.split("_")[1])
        order    = get_reseller_bot_order(order_id)
        if order:
            update_reseller_bot_order(order_id, "approved")
            is_due = (order["transaction_id"] in ("PORE_DIBO", None, ""))
            if not is_due:
                # payment already done → income add + complete
                conn = get_db()
                try:
                    conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'reseller')",
                             a=float(order["amount"]),
                             n=f"Reseller #{order_id} — {order['product']} ({order['reseller_code']})")
                finally:
                    conn.close()
                await notify_reseller(
                    order["reseller_code"],
                    f"✅ *Order #{order_id} Approved!*\n\n"
                    f"📦 {order['product']}\n"
                    f"📧 {order['customer_email']}\n"
                    f"💵 ৳{order['amount']}\n\n"
                    f"24 ghontar moddhe account deliver hobe. Dhonnobad! 🙏"
                )
                await query.edit_message_text(
                    f"✅ Order #{order_id} approved!\n"
                    f"💰 ৳{order['amount']} income e add hoye geche.\n"
                    f"Reseller ke notify kora hoye geche.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
            else:
                # due order → notify reseller to pay
                await notify_reseller(
                    order["reseller_code"],
                    f"✅ *Order #{order_id} Approved!*\n\n"
                    f"📦 {order['product']}\n"
                    f"📧 {order['customer_email']}\n"
                    f"💵 ৳{order['amount']}\n\n"
                    f"⚠️ *Ekhon payment kore dao bhai!*\n"
                    f"'Amar Orders' → 'Payment Korbo' button press koro!"
                )
                await query.edit_message_text(
                    f"✅ Order #{order_id} approved (due)!\n"
                    f"Reseller ke payment korte bola hoye geche. 3 ghonta por por reminder jabe.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                    parse_mode="Markdown"
                )
        return

    # ── Reject reseller order ──
    elif data.startswith("rreject_"):
        order_id = int(data.split("_")[1])
        context.user_data["rejecting_order_id"] = order_id
        context.user_data["state"]               = "waiting_reject_reason"
        await query.edit_message_text(f"❌ Order #{order_id} reject er karon likho:")
        return

    # ── Complete after payment verified ──
    elif data.startswith("rcomplete_"):
        order_id = int(data.split("_")[1])
        order    = get_reseller_bot_order(order_id)
        if order:
            update_reseller_bot_order(order_id, "completed")
            conn = get_db()
            try:
                conn.run("INSERT INTO income (amount,note,type) VALUES (:a,:n,'reseller')",
                         a=float(order["amount"]),
                         n=f"Reseller #{order_id} payment — {order['product']} ({order['reseller_code']})")
            finally:
                conn.close()
            await notify_reseller(
                order["reseller_code"],
                f"🎉 *Order #{order_id} Complete!*\n\n"
                f"Payment received! Tomar order completed.\n\n"
                f"📦 {order['product']}\n"
                f"📧 {order['customer_email']}\n"
                f"💵 ৳{order['amount']}\n\n"
                f"Account 24 ghontar moddhe deliver hobe. Dhonnobad! 🙏"
            )
            await query.edit_message_text(
                f"✅ Order #{order_id} completed!\n"
                f"💰 ৳{order['amount']} income e add hoye geche.\n"
                f"Reseller ke complete notify kora hoye geche.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        return

    # ── Wrong TxnID ──
    elif data.startswith("rwrong_txn_"):
        order_id = int(data.split("_")[2])
        order    = get_reseller_bot_order(order_id)
        if order:
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET transaction_id='PORE_DIBO' WHERE id=:id", id=order_id)
            finally:
                conn.close()
            await notify_reseller(
                order["reseller_code"],
                f"❌ *Order #{order_id} — Transaction ID ভুল!*\n\n"
                f"Admin verify korte pare ni. Sothik TxnID patha bhai.\n"
                f"'Amar Orders' → 'Payment Korbo' button press koro!"
            )
            await query.edit_message_text(
                f"❌ Order #{order_id} er TxnID ভুল বলা হয়েছে। Reseller কে জানানো হয়েছে।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
                parse_mode="Markdown"
            )
        return

    # ── Menu items ──
    if   data == "today_orders":              await show_orders(query, days=1)
    elif data == "week_orders":               await show_orders(query, days=7)
    elif data == "today_income":              await show_income(query, days=1)
    elif data == "month_report":              await show_month_report(query)
    elif data == "resellers":                 await show_resellers(query)
    elif data == "pending_orders":            await show_orders_by_status(query, "pending")
    elif data == "reseller_bot_orders_today": await show_reseller_bot_orders_today(query)
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

# ── Display helpers ──

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn  = get_db()
    try:
        rows = conn.run(
            "SELECT id,woo_order_id,customer_name,total,status FROM orders "
            "WHERE created_at>=:s ORDER BY created_at DESC", s=since)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text("📦 Kono order nei bhai.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
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
            "SELECT id,woo_order_id,customer_name,total,status FROM orders "
            "WHERE status=:s ORDER BY created_at DESC LIMIT 10", s=status)
    finally:
        conn.close()
    if not rows:
        await query.edit_message_text(f"📦 {status} status e kono order nei.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
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
        parse_mode="Markdown"
    )

async def show_month_report(query):
    since = datetime.now() - timedelta(days=30)
    conn  = get_db()
    try:
        o  = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at>=:s", s=since)
        i  = conn.run("SELECT SUM(amount) FROM income WHERE created_at>=:s", s=since)
        rb = conn.run("SELECT COUNT(*), SUM(amount) FROM reseller_bot_orders WHERE created_at>=:s AND status NOT IN ('rejected','pending')", s=since)
    finally:
        conn.close()
    text = (
        f"📊 *Last 30 diner Report*\n\n"
        f"🌐 WooCommerce Orders: {o[0][0] or 0}ta | ৳{o[0][1] or 0}\n"
        f"🛍️ Reseller Bot Orders: {rb[0][0] or 0}ta | ৳{rb[0][1] or 0}\n\n"
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
                COUNT(DISTINCT ro.id), COALESCE(SUM(ro.price*ro.quantity),0),
                COUNT(DISTINCT rbo.id), COALESCE(SUM(rbo.amount),0)
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
            parse_mode="Markdown")
        return
    text = "👥 *Ei Maser Reseller Report:*\n\n"
    for r in rows:
        t_orders = (r[3] or 0) + (r[5] or 0)
        t_amount = float(r[4] or 0) + float(r[6] or 0)
        text += (f"🔸 *{r[0]}* ({r[1]}) — `{r[2] or 'N/A'}`\n"
                 f"   Manual: {r[3]}ta | Bot: {r[5]}ta | Mot: {t_orders}ta\n"
                 f"   💵 ৳{t_amount:.0f}\n\n")
    await query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown")

async def show_reseller_bot_orders_today(query):
    orders = db_get_today_reseller_bot_orders()
    if not orders:
        await query.edit_message_text("🛍️ Aaj ekhono kono reseller bot order nei.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    text = f"🛍️ *Aajker Reseller Bot Orders ({len(orders)}ta):*\n\n"
    keyboard = []
    for o in orders:
        emoji  = "✅" if o["status"]=="approved" else ("🎉" if o["status"]=="completed" else ("❌" if o["status"]=="rejected" else "⏳"))
        due_t  = " ⚠️DUE" if o["txn"]=="PORE_DIBO" else ""
        text  += (f"{emoji} #{o['id']} — *{o['reseller_name']}* (`{o['reseller_code']}`){due_t}\n"
                  f"   📦 {o['product']} | 💵 ৳{o['amount']} | {o['status']}\n"
                  f"   📧 {o['email']}\n\n")
        if o["status"] == "pending":
            keyboard.append([
                InlineKeyboardButton(f"✅ Approve #{o['id']}", callback_data=f"rapprove_{o['id']}"),
                InlineKeyboardButton(f"❌ Reject #{o['id']}",  callback_data=f"rreject_{o['id']}")
            ])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_status_options(query, order_id):
    keyboard = [
        [InlineKeyboardButton("⏳ Processing",       callback_data=f"setstatus_{order_id}_processing")],
        [InlineKeyboardButton("✅ Completed",         callback_data=f"setstatus_{order_id}_completed")],
        [InlineKeyboardButton("💳 Payment Pending",  callback_data=f"setstatus_{order_id}_pending")],
        [InlineKeyboardButton("❌ Cancelled",         callback_data=f"setstatus_{order_id}_cancelled")],
        [InlineKeyboardButton("🔙 Back",              callback_data="today_orders")]
    ]
    await query.edit_message_text(f"✏️ Order #{order_id} er notun status:",
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
            "SELECT woo_order_id,customer_name,total,status,created_at FROM orders "
            "WHERE LOWER(customer_email)=:e ORDER BY created_at DESC", e=email)
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text(f"❌ {email} email e kono order nei."); return
    total_spent = sum(float(o[2]) for o in rows)
    text = f"👤 *{rows[0][1]}*\n📧 {email}\n\n"
    for o in rows:
        emoji = "✅" if o[3]=="completed" else "⏳" if o[3]=="processing" else "❌"
        text += f"{emoji} #{o[0]} — {o[4].strftime('%d %b %Y')} | ৳{o[2]} | {o[3]}\n"
    text += f"\n💰 *Total: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

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
                await update.message.reply_text(f"❌ {phone} number e reseller nei."); return
            conn.run("INSERT INTO reseller_orders (reseller_id,product,quantity,price) VALUES (:r,:p,:q,:pr)",
                     r=r[0][0], p=product, q=qty, pr=price)
        finally:
            conn.close()
        await update.message.reply_text(f"✅ {r[0][1]} — {product} x{qty} = ৳{qty*price}")
    except:
        await update.message.reply_text("❌ Vul format!")

# =================== RESELLER BOT ===================

reseller_user_data: dict = {}

def reseller_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 নতুন Order দিব",    callback_data="res_new_order")],
        [InlineKeyboardButton("📋 আমার Orders",        callback_data="res_my_orders")],
        [InlineKeyboardButton("ℹ️ Price List",         callback_data="res_price_list")],
    ])

async def reseller_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.message.chat_id
    reseller = get_reseller_by_chat_id(chat_id)
    if reseller:
        conn = get_db()
        try:
            due = conn.run(
                "SELECT COUNT(*) FROM reseller_bot_orders "
                "WHERE reseller_code=:c AND transaction_id='PORE_DIBO' AND status='approved'",
                c=reseller["code"])
        finally:
            conn.close()
        due_count = due[0][0] if due else 0
        due_txt   = f"\n\n⚠️ *{due_count}ta due payment* baaki ache!" if due_count > 0 else ""
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
        f"✅ *Register Successful!*\n\nWelcome *{reseller['name']}* bhai! 🎉\nCode: `{code}`\n\nEkhon order dite paro!",
        reply_markup=reseller_main_menu(), parse_mode="Markdown")
    return ConversationHandler.END

async def reseller_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    # ── Price list ──
    if data == "res_price_list":
        text = (
            "📋 *Price List:*\n\n"
            f"🤖 ChatGPT Plus (1 Month) — *৳{PRODUCTS['chatgpt']['price']}*\n"
            f"💎 Gemini Advanced (1 Month) — *৳{PRODUCTS['gemini']['price']}*\n\n"
            "Order dite 'নতুন Order দিব' press koro!"
        )
        await query.edit_message_text(text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]]),
            parse_mode="Markdown")
        return

    # ── New order: product select ──
    if data == "res_new_order":
        keyboard = [
            [InlineKeyboardButton(f"🤖 ChatGPT Plus — ৳{PRODUCTS['chatgpt']['price']}", callback_data="res_order_chatgpt")],
            [InlineKeyboardButton(f"💎 Gemini Advanced — ৳{PRODUCTS['gemini']['price']}", callback_data="res_order_gemini")],
            [InlineKeyboardButton("🔙 Back", callback_data="res_back")]
        ]
        await query.edit_message_text("🛒 *Kon product order korte chao?*",
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
                f"👇 *Customer er Gmail/email address dao:*\n_(jar jonno kincho tar email)_",
                parse_mode="Markdown")

    # ── Pay later ──
    elif data == "res_pay_later":
        chat_data = reseller_user_data.get(chat_id, {})
        reseller  = get_reseller_by_chat_id(chat_id)
        if reseller and chat_data:
            conn = get_db()
            try:
                rows = conn.run(
                    "INSERT INTO reseller_bot_orders "
                    "(reseller_id,reseller_code,product,customer_email,transaction_id,amount,status) "
                    "VALUES (:rid,:code,:p,:e,'PORE_DIBO',:a,'pending') RETURNING id",
                    rid=reseller["id"], code=reseller["code"],
                    p=chat_data["product_name"],
                    e=chat_data.get("customer_email","N/A"),
                    a=chat_data["amount"])
            finally:
                conn.close()
            order_id = rows[0][0] if rows else None
            if order_id:
                await send_main_notification(order_id, reseller, chat_data, "PORE_DIBO (Due)")
                await query.edit_message_text(
                    f"✅ *Order #{order_id} Submit Hoye Geche!*\n\n"
                    f"📦 {chat_data['product_name']}\n"
                    f"📧 {chat_data.get('customer_email','N/A')}\n"
                    f"💵 ৳{chat_data['amount']}\n\n"
                    f"⚠️ Admin approve korle payment korte bolbe.\n"
                    f"Tumi 'Amar Orders' theke payment dite parbe.",
                    reply_markup=reseller_main_menu(), parse_mode="Markdown")
            reseller_user_data.pop(chat_id, None)

    # ── My orders ──
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
            await query.edit_message_text("📋 Ekhono kono order nei bhai.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]])); return
        text = "📋 *Tomar Last 10 Order:*\n\n"
        keyboard = []
        for r in rows:
            emoji   = "🎉" if r[4]=="completed" else ("✅" if r[4]=="approved" else ("❌" if r[4]=="rejected" else "⏳"))
            due_tag = " ⚠️*PAYMENT BAAKI*" if r[5]=="PORE_DIBO" else ""
            date_s  = r[6].strftime("%d %b, %I:%M %p") if r[6] else ""
            text   += (f"{emoji} *#{r[0]}* — {r[1]}{due_tag}\n"
                       f"   📧 {r[2]}\n"
                       f"   💵 ৳{r[3]} | {r[4]} | {date_s}\n\n")
            # show pay button only for approved+due orders
            if r[4] == "approved" and r[5] == "PORE_DIBO":
                keyboard.append([InlineKeyboardButton(
                    f"💳 #{r[0]} Payment Korbo", callback_data=f"res_pay_order_{r[0]}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="res_back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # ── Payment for specific due order ──
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
                f"Kon method e payment korbe?",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("res_method_"):
        parts    = data.split("_")    # res_method_bkash_123
        method   = parts[2]           # bkash / nagad
        order_id = int(parts[3])
        number   = NAGAD_NUMBER if method == "nagad" else BKASH_NUMBER
        method_emoji = "📱"
        reseller_user_data[chat_id]["payment_method"]   = method
        reseller_user_data[chat_id]["paying_order_id"]  = order_id
        reseller_user_data[chat_id]["state"]            = "waiting_due_txn"
        order = get_reseller_bot_order(order_id)
        await query.edit_message_text(
            f"💳 *{method.upper()} Payment*\n\n"
            f"📦 {order['product']}\n"
            f"💵 Amount: ৳{order['amount']}\n\n"
            f"{method_emoji} Number: *{number}* (Send Money)\n\n"
            f"Payment er por *Transaction ID* dao:\n"
            f"_(Format: TxnID Order# - Tomar reseller code)_\n"
            f"Example: `8N6A7B3X2Y {order_id} - {reseller_user_data[chat_id].get('code','CODE')}`",
            parse_mode="Markdown")

    # ── Back ──
    elif data == "res_back":
        reseller = get_reseller_by_chat_id(chat_id)
        name = reseller["name"] if reseller else "Bhai"
        await query.edit_message_text(f"Ki korte chao *{name}* bhai? 👇",
            reply_markup=reseller_main_menu(), parse_mode="Markdown")

async def reseller_paynow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Email enter করার পর 'Ekhoni Payment Korbo' button"""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if chat_id not in reseller_user_data:
        await query.edit_message_text("❌ Session শেষ। আবার /start দাও।"); return
    reseller_user_data[chat_id]["state"] = "waiting_pay_method_new"
    amount  = reseller_user_data[chat_id].get("amount", 0)
    product = reseller_user_data[chat_id].get("product_name", "")
    keyboard = [
        [InlineKeyboardButton("📱 Bkash", callback_data="res_pay_bkash")],
        [InlineKeyboardButton("📱 Nagad", callback_data="res_pay_nagad")],
    ]
    await query.edit_message_text(
        f"💳 *Payment Method*\n\n📦 {product}\n💵 ৳{amount}\n\nKon method e payment korbe?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def reseller_pay_method_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bkash / Nagad select করলে"""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    method  = "bkash" if query.data == "res_pay_bkash" else "nagad"
    number  = NAGAD_NUMBER if method == "nagad" else BKASH_NUMBER
    if chat_id in reseller_user_data:
        reseller_user_data[chat_id]["payment_method"] = method
        reseller_user_data[chat_id]["state"] = "waiting_transaction"
    amount  = reseller_user_data[chat_id].get("amount", 0) if chat_id in reseller_user_data else "?"
    product = reseller_user_data[chat_id].get("product_name", "") if chat_id in reseller_user_data else ""
    await query.edit_message_text(
        f"💳 *{method.upper()} Payment*\n\n"
        f"📦 {product}\n"
        f"💵 Amount: ৳{amount}\n\n"
        f"📱 Number: *{number}* (Send Money)\n\n"
        f"Payment er por *Transaction ID* dao:\n_(Example: 8N6A7B3X2Y)_",
        parse_mode="Markdown")

async def reseller_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.message.chat_id

    # /txn_ command
    if text.startswith("/txn_"):
        parts = text.split("_")
        if len(parts) >= 3:
            try:
                order_id = int(parts[1])
                txn_id   = "_".join(parts[2:])
                if len(txn_id) < 6:
                    await update.message.reply_text("❌ Transaction ID minimum 6 character lagbe!"); return
                conn = get_db()
                try:
                    conn.run("UPDATE reseller_bot_orders SET transaction_id=:t WHERE id=:id", t=txn_id, id=order_id)
                finally:
                    conn.close()
                from telegram import Bot
                await Bot(token=BOT_TOKEN).send_message(
                    chat_id=MAIN_CHAT_ID,
                    text=f"💳 *TxnID Update!*\n\nOrder #{order_id}\nTxnID: `{txn_id}`\nVerify kore approve koro.",
                    parse_mode="Markdown")
                await update.message.reply_text(
                    f"✅ TxnID submit hoye geche! Order #{order_id}\nAdmin verify korbe.",
                    reply_markup=reseller_main_menu())
            except Exception as e:
                logger.error(f"TXN update error: {e}")
                await update.message.reply_text("❌ Format thik na. Example: /txn_1_TXN123456")
        return

    user_state = reseller_user_data.get(chat_id, {})
    state      = user_state.get("state")
    reseller   = get_reseller_by_chat_id(chat_id)

    if not reseller:
        await update.message.reply_text("Aage /start diye register koro bhai!"); return

    # ── Waiting for customer email ──
    if state == "waiting_email":
        if "@" not in text or "." not in text:
            await update.message.reply_text("❌ Valid email dao!\nExample: example@gmail.com"); return
        reseller_user_data[chat_id]["customer_email"] = text
        reseller_user_data[chat_id]["state"]           = "waiting_pay_choice"
        keyboard = [
            [InlineKeyboardButton("💳 Ekhoni Payment Korbo", callback_data="res_pay_now")],
            [InlineKeyboardButton("⏰ Pore Payment Dibo",    callback_data="res_pay_later")]
        ]
        await update.message.reply_text(
            f"✅ Email newa hoye geche!\n\n"
            f"📧 {text}\n📦 {user_state['product_name']}\n💵 ৳{user_state['amount']}\n\n"
            f"Payment ki ekhon korbe?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # ── Waiting for TxnID (new order, payment now) ──
    elif state == "waiting_transaction":
        if len(text) < 6:
            await update.message.reply_text("❌ TxnID minimum 6 character lagbe!"); return
        method   = user_state.get("payment_method", "bkash")
        product  = user_state.get("product_name")
        email    = user_state.get("customer_email")
        amount   = user_state.get("amount")
        conn = get_db()
        try:
            rows = conn.run(
                "INSERT INTO reseller_bot_orders "
                "(reseller_id,reseller_code,product,customer_email,transaction_id,payment_method,amount,status) "
                "VALUES (:rid,:code,:p,:e,:t,:m,:a,'pending') RETURNING id",
                rid=reseller["id"], code=reseller["code"],
                p=product, e=email, t=text, m=method, a=amount)
        finally:
            conn.close()
        order_id = rows[0][0] if rows else None
        if order_id:
            reseller_user_data[chat_id]["state"] = None
            await send_main_notification(order_id, reseller, user_state, text, method)
            await update.message.reply_text(
                f"✅ *Order Submit Hoye Geche!*\n\n"
                f"🆔 Order #{order_id}\n📦 {product}\n📧 {email}\n"
                f"💳 TxnID: `{text}`\n📲 {method.upper()}\n💵 ৳{amount}\n\n"
                f"⏳ Admin verify korle notify pabe! Dhonnobad 🙏",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Problem hoye geche. Abar try koro.")
        reseller_user_data.pop(chat_id, None)

    # ── Waiting for TxnID for due/approved order ──
    elif state == "waiting_due_txn":
        if len(text) < 6:
            await update.message.reply_text("❌ TxnID minimum 6 character lagbe!"); return
        order_id = user_state.get("paying_order_id")
        method   = user_state.get("payment_method", "bkash")
        if order_id:
            conn = get_db()
            try:
                conn.run("UPDATE reseller_bot_orders SET transaction_id=:t, payment_method=:m WHERE id=:id",
                         t=text, m=method, id=order_id)
            finally:
                conn.close()
            await send_payment_check_notification(order_id, reseller["code"], text, method)
            await update.message.reply_text(
                f"✅ *Payment Info Pathano Hoye Geche!*\n\n"
                f"Order #{order_id}\n💳 {method.upper()} TxnID: `{text}`\n\n"
                f"Admin verify korbe shighroi. Notify korbe!",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        reseller_user_data.pop(chat_id, None)

    else:
        await update.message.reply_text("Menu theke kaj koro bhai 👇", reply_markup=reseller_main_menu())

# =================== FLASK WEBHOOK ===================

@app.route("/webhook/woocommerce", methods=["POST"])
def woocommerce_webhook():
    try:
        raw = request.data
        if not raw:
            return jsonify({"status":"ok"}), 200
        try:
            data = json.loads(raw)
        except:
            return jsonify({"status":"ok"}), 200
        if not data:
            return jsonify({"status":"ok"}), 200

        order_id       = str(data.get("id","N/A"))
        customer       = data.get("billing", {})
        customer_name  = f"{customer.get('first_name','')} {customer.get('last_name','')}".strip() or "Unknown"
        customer_email = customer.get("email","")
        total          = float(data.get("total", 0))
        status         = data.get("status","pending")
        items_text     = ", ".join([f"{i['name']} x{i['quantity']}" for i in data.get("line_items",[])])

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
    main_app.add_handler(CommandHandler("start",        start))
    main_app.add_handler(CommandHandler("income",       income_command))
    main_app.add_handler(CommandHandler("customer",     customer_command))
    main_app.add_handler(CommandHandler("addreseller",  addreseller_command))
    main_app.add_handler(CommandHandler("rsale",        resellersale_command))
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
    # ── register specific patterns first, then general ──
    reseller_app.add_handler(CallbackQueryHandler(reseller_paynow_handler,     pattern="^res_pay_now$"))
    reseller_app.add_handler(CallbackQueryHandler(reseller_pay_method_handler, pattern="^res_pay_bkash$"))
    reseller_app.add_handler(CallbackQueryHandler(reseller_pay_method_handler, pattern="^res_pay_nagad$"))
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
        asyncio.create_task(send_due_reminders())
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
