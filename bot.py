import os
import logging
import pg8000.native
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import threading
import asyncio
import nest_asyncio
import requests as req
from urllib.parse import urlparse
import json

nest_asyncio.apply()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ENV
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WC_KEY = os.environ.get("WC_KEY")
WC_SECRET = os.environ.get("WC_SECRET")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
RESELLER_BOT_TOKEN = os.environ.get("RESELLER_BOT_TOKEN")

app = Flask(__name__)
main_loop = None
user_conversations = {}

# Reseller conversation states
WAITING_CODE = 1
WAITING_EMAIL = 2
WAITING_TRANSACTION = 3

PRODUCTS = {
    "chatgpt": {"name": "ChatGPT Plus (1 Month)", "price": 1200},
    "gemini": {"name": "Gemini Advanced (1 Month)", "price": 1000},
}

# =================== DATABASE ===================

def get_db():
    url = urlparse(DATABASE_URL)
    import ssl
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    conn = pg8000.native.Connection(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path[1:],
        user=url.username,
        password=url.password,
        ssl_context=ssl_context
    )
    return conn

def setup_db():
    conn = get_db()
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
    conn.run("""CREATE TABLE IF NOT EXISTS reseller_bot_orders (
        id SERIAL PRIMARY KEY, reseller_id INTEGER REFERENCES resellers(id),
        reseller_code VARCHAR(20), product VARCHAR(100),
        customer_email VARCHAR(200), transaction_id VARCHAR(100),
        amount DECIMAL(10,2), status VARCHAR(20) DEFAULT 'pending',
        reject_reason TEXT, created_at TIMESTAMP DEFAULT NOW())""")
    conn.close()
    logger.info("Database setup complete!")

# =================== DB HELPERS ===================

def db_get_recent_orders(limit=10, status=None):
    conn = get_db()
    if status:
        rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, items, created_at FROM orders WHERE status = :s ORDER BY created_at DESC LIMIT :l", s=status, l=limit)
    else:
        rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, items, created_at FROM orders ORDER BY created_at DESC LIMIT :l", l=limit)
    conn.close()
    return [{"id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3], "total": str(r[4]), "status": r[5], "items": r[6], "created_at": str(r[7])} for r in rows]

def db_get_last_order():
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, items, created_at FROM orders ORDER BY created_at DESC LIMIT 1")
    conn.close()
    if rows:
        r = rows[0]
        return {"id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3], "total": str(r[4]), "status": r[5], "items": r[6], "created_at": str(r[7])}
    return None

def db_update_order_status(order_id, new_status, use_woo_id=False):
    conn = get_db()
    if use_woo_id:
        rows = conn.run("SELECT id, woo_order_id FROM orders WHERE woo_order_id = :oid", oid=str(order_id))
    else:
        rows = conn.run("SELECT id, woo_order_id FROM orders WHERE id = :id", id=int(order_id))
    if not rows:
        conn.close()
        return False, "Order paoa jaini"
    db_id, woo_id = rows[0][0], rows[0][1]
    conn.run("UPDATE orders SET status = :s WHERE id = :id", s=new_status, id=db_id)
    conn.close()
    try:
        req.put(f"https://favouritedeals.online/wp-json/wc/v3/orders/{woo_id}", json={"status": new_status}, auth=(WC_KEY, WC_SECRET), timeout=10)
    except Exception as e:
        logger.error(f"WC update error: {e}")
    return True, woo_id

def db_get_income_summary(days=1):
    conn = get_db()
    since = datetime.now() - timedelta(days=days)
    rows = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at >= :s", s=since)
    conn.close()
    return {"total": str(rows[0][0] or 0), "count": rows[0][1] or 0}

def db_get_orders_summary(days=1):
    conn = get_db()
    since = datetime.now() - timedelta(days=days)
    rows = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at >= :s", s=since)
    conn.close()
    return {"count": rows[0][0] or 0, "total": str(rows[0][1] or 0)}

def db_search_orders_by_name(name):
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, created_at FROM orders WHERE LOWER(customer_name) LIKE :n ORDER BY created_at DESC LIMIT 5", n=f"%{name.lower()}%")
    conn.close()
    return [{"id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3], "total": str(r[4]), "status": r[5], "created_at": str(r[6])} for r in rows]

def db_add_income(amount, note):
    conn = get_db()
    conn.run("INSERT INTO income (amount, note, type) VALUES (:a, :n, 'manual')", a=float(amount), n=note)
    conn.close()
    return True

def db_get_reseller_summary(reseller_name=None):
    conn = get_db()
    if reseller_name:
        rows = conn.run("""SELECT r.name, r.phone, r.reseller_code, COUNT(ro.id), COALESCE(SUM(ro.price * ro.quantity), 0)
            FROM resellers r LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id AND ro.created_at >= date_trunc('month', NOW())
            WHERE LOWER(r.name) LIKE :n OR LOWER(r.reseller_code) LIKE :n GROUP BY r.id, r.name, r.phone, r.reseller_code""", n=f"%{reseller_name.lower()}%")
    else:
        rows = conn.run("""SELECT r.name, r.phone, r.reseller_code, COUNT(ro.id), COALESCE(SUM(ro.price * ro.quantity), 0)
            FROM resellers r LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id AND ro.created_at >= date_trunc('month', NOW())
            GROUP BY r.id, r.name, r.phone, r.reseller_code""")
    conn.close()
    return [{"name": r[0], "phone": r[1], "code": r[2], "orders": r[3], "total": str(r[4])} for r in rows]

def get_reseller_bot_order(order_id):
    conn = get_db()
    rows = conn.run("SELECT id, reseller_code, product, customer_email, amount, status FROM reseller_bot_orders WHERE id = :id", id=order_id)
    conn.close()
    if rows:
        return {"id": rows[0][0], "reseller_code": rows[0][1], "product": rows[0][2], "customer_email": rows[0][3], "amount": str(rows[0][4]), "status": rows[0][5]}
    return None

def update_reseller_bot_order(order_id, status, reject_reason=None):
    conn = get_db()
    if reject_reason:
        conn.run("UPDATE reseller_bot_orders SET status = :s, reject_reason = :r WHERE id = :id", s=status, r=reject_reason, id=order_id)
    else:
        conn.run("UPDATE reseller_bot_orders SET status = :s WHERE id = :id", s=status, id=order_id)
    conn.close()

def get_reseller_by_chat_id(chat_id):
    conn = get_db()
    rows = conn.run("SELECT id, name, phone, reseller_code FROM resellers WHERE telegram_chat_id = :c", c=str(chat_id))
    conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2], "code": rows[0][3]}
    return None

def get_reseller_by_code(code):
    conn = get_db()
    rows = conn.run("SELECT id, name, phone FROM resellers WHERE reseller_code = :c", c=code.upper())
    conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2]}
    return None

async def notify_reseller(reseller_code, message):
    try:
        conn = get_db()
        rows = conn.run("SELECT telegram_chat_id FROM resellers WHERE reseller_code = :c", c=reseller_code)
        conn.close()
        if rows and rows[0][0]:
            from telegram import Bot
            reseller_bot = Bot(token=RESELLER_BOT_TOKEN)
            await reseller_bot.send_message(chat_id=rows[0][0], text=message)
    except Exception as e:
        logger.error(f"Reseller notify error: {e}")

# =================== AI ===================

AI_FUNCTIONS = [
    {"name": "get_recent_orders", "description": "Recent orders dekhao", "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}, "status": {"type": "string"}}}},
    {"name": "get_last_order", "description": "Sorboshesh order dekhao", "parameters": {"type": "object", "properties": {}}},
    {"name": "update_order_status", "description": "Order er status change koro", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "new_status": {"type": "string"}, "use_woo_id": {"type": "boolean"}}, "required": ["order_id", "new_status"]}},
    {"name": "get_income_summary", "description": "Income summary dekhao", "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "get_orders_summary", "description": "Orders count dekhao", "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "search_orders_by_name", "description": "Customer naam diye order khojo", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "add_income", "description": "Manual income jog koro", "parameters": {"type": "object", "properties": {"amount": {"type": "number"}, "note": {"type": "string"}}, "required": ["amount", "note"]}},
    {"name": "get_reseller_summary", "description": "Reseller er summary dekhao. Code ba naam diye filter kora jabe.", "parameters": {"type": "object", "properties": {"reseller_name": {"type": "string"}}}},
]

def execute_function(name, args):
    try:
        if name == "get_recent_orders":
            return db_get_recent_orders(args.get("limit", 5), args.get("status"))
        elif name == "get_last_order":
            return db_get_last_order()
        elif name == "update_order_status":
            success, result = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=True)
            if not success:
                success, result = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=False)
            return {"success": success, "result": result}
        elif name == "get_income_summary":
            return db_get_income_summary(args.get("days", 1))
        elif name == "get_orders_summary":
            return db_get_orders_summary(args.get("days", 1))
        elif name == "search_orders_by_name":
            return db_search_orders_by_name(args["name"])
        elif name == "add_income":
            return {"success": db_add_income(args["amount"], args["note"])}
        elif name == "get_reseller_summary":
            return db_get_reseller_summary(args.get("reseller_name"))
    except Exception as e:
        return {"error": str(e)}

SYSTEM_PROMPT = """Tumi Favourite Deals er personal business assistant. Tomar naam "FD Assistant".
Tumi Banglish e kotha bolbe. Jemon: "Bhai, last order ta #23410 er. Status ekhon processing."
Chhoto sentence, casual tone. "yes/haan/ok" mane age jar kaj confirm koro.
Database functions use kore real data dekhabe. Reseller code diye specific reseller info dite parbe."""

async def process_ai_message(messages_history):
    if not OPENAI_KEY:
        return None
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages_history
    try:
        response = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-3.5-turbo", "messages": messages, "functions": AI_FUNCTIONS, "function_call": "auto", "max_tokens": 1000},
            timeout=15
        )
        resp = response.json()
        if 'error' in resp:
            logger.error(f"OpenAI error: {resp['error']}")
            return None
        msg = resp['choices'][0]['message']
        if msg.get('function_call'):
            func_name = msg['function_call']['name']
            func_args = json.loads(msg['function_call']['arguments'])
            func_result = execute_function(func_name, func_args)
            messages.append(msg)
            messages.append({"role": "function", "name": func_name, "content": json.dumps(func_result, ensure_ascii=False)})
            response2 = req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-3.5-turbo", "messages": messages, "max_tokens": 500},
                timeout=15
            )
            resp2 = response2.json()
            if 'error' in resp2:
                return None
            return resp2['choices'][0]['message']['content']
        else:
            return msg.get('content')
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# =================== MAIN BOT ===================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Aajker Order", callback_data="today_orders"),
         InlineKeyboardButton("💰 Aajker Income", callback_data="today_income")],
        [InlineKeyboardButton("📅 7 Diner Order", callback_data="week_orders"),
         InlineKeyboardButton("📊 Maser Report", callback_data="month_report")],
        [InlineKeyboardButton("👥 Reseller", callback_data="resellers"),
         InlineKeyboardButton("➕ Manual Income", callback_data="manual_income")],
        [InlineKeyboardButton("🔍 Customer Khojo", callback_data="search_customer"),
         InlineKeyboardButton("⏳ Pending Orders", callback_data="pending_orders")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_conversations[chat_id] = []
    await update.message.reply_text(
        "🛍️ *Favourite Deals Assistant*\n\nAssalamualaikum bhai! Ami tomar business assistant. Menu theke kaj koro othoba seedha bolo ki dorkar! 🤖",
        reply_markup=main_menu_keyboard(), parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")

    if context.user_data.get('state') == 'waiting_reject_reason':
        order_id = context.user_data.get('rejecting_order_id')
        order = get_reseller_bot_order(order_id)
        if order:
            update_reseller_bot_order(order_id, "rejected", text)
            await notify_reseller(order['reseller_code'],
                f"❌ Tomar order #{order_id} reject hoye geche bhai.\nKaron: {text}\n\nSomossa hole admin er sathe jogajog koro.")
            await update.message.reply_text(f"❌ Order #{order_id} rejected! Reseller ke notify kora hoyeche.", reply_markup=main_menu_keyboard())
        context.user_data['state'] = None
        return

    if chat_id not in user_conversations:
        user_conversations[chat_id] = []
    user_conversations[chat_id].append({"role": "user", "content": text})
    if len(user_conversations[chat_id]) > 10:
        user_conversations[chat_id] = user_conversations[chat_id][-10:]

    ai_reply = await process_ai_message(user_conversations[chat_id])
    if ai_reply:
        user_conversations[chat_id].append({"role": "assistant", "content": ai_reply})
        await update.message.reply_text(ai_reply, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
    else:
        await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("rapprove_"):
        order_id = int(data.split("_")[1])
        order = get_reseller_bot_order(order_id)
        if order:
            update_reseller_bot_order(order_id, "approved")
            await notify_reseller(order['reseller_code'],
                f"✅ Tomar order #{order_id} approve hoye geche bhai!\n📦 {order['product']}\n📧 {order['customer_email']}\n\n24 ghontar moddhe deliver kora hobe!")
            await query.edit_message_text(f"✅ Order #{order_id} approved! Reseller ke notify kora hoyeche.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))
        return

    elif data.startswith("rreject_"):
        order_id = int(data.split("_")[1])
        context.user_data['rejecting_order_id'] = order_id
        context.user_data['state'] = 'waiting_reject_reason'
        await query.edit_message_text(f"❌ Order #{order_id} reject er karon likho:")
        return

    if data == "today_orders":
        await show_orders(query, days=1)
    elif data == "week_orders":
        await show_orders(query, days=7)
    elif data == "today_income":
        await show_income(query, days=1)
    elif data == "month_report":
        await show_month_report(query)
    elif data == "resellers":
        await show_resellers(query)
    elif data == "pending_orders":
        await show_orders_by_status(query, "pending")
    elif data == "manual_income":
        await query.edit_message_text("💰 `/income 500 bkash e paisi`", parse_mode="Markdown")
    elif data == "search_customer":
        await query.edit_message_text("🔍 `/customer example@email.com`", parse_mode="Markdown")
    elif data == "menu":
        await query.edit_message_text("🛍️ *FD Assistant*\n\nMenu theke kaj koro:",
            reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif data.startswith("status_"):
        await show_status_options(query, data.split("_")[1])
    elif data.startswith("setstatus_"):
        parts = data.split("_")
        await update_order_status_btn(query, parts[1], parts[2])

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, total, status FROM orders WHERE created_at >= :s ORDER BY created_at DESC", s=since)
    conn.close()
    if not rows:
        await query.edit_message_text(f"📦 Kono order nei bhai.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    text = f"📦 *Last {days} diner order ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status change", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_orders_by_status(query, status):
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, total, status FROM orders WHERE status = :s ORDER BY created_at DESC LIMIT 10", s=status)
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
    conn = get_db()
    rows = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at >= :s", s=since)
    conn.close()
    total = rows[0][0] or 0
    count = rows[0][1] or 0
    label = "Aajker" if days == 1 else f"Last {days} diner"
    await query.edit_message_text(f"💰 *{label} Income*\n\nMot: ৳{total}\nEntry: {count}ta",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]), parse_mode="Markdown")

async def show_month_report(query):
    since = datetime.now() - timedelta(days=30)
    conn = get_db()
    o = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at >= :s", s=since)
    i = conn.run("SELECT SUM(amount) FROM income WHERE created_at >= :s", s=since)
    conn.close()
    text = f"📊 *Last 30 diner Report*\n\n📦 Total Order: {o[0][0] or 0}ta\n💵 Revenue: ৳{o[0][1] or 0}\n💰 Manual Income: ৳{i[0][0] or 0}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]), parse_mode="Markdown")

async def show_resellers(query):
    conn = get_db()
    rows = conn.run("""SELECT r.name, r.phone, r.reseller_code, COUNT(ro.id), COALESCE(SUM(ro.price * ro.quantity), 0)
        FROM resellers r LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id AND ro.created_at >= date_trunc('month', NOW())
        GROUP BY r.id, r.name, r.phone, r.reseller_code""")
    conn.close()
    if not rows:
        await query.edit_message_text("👥 Kono reseller nei.\n\nAdd: `/addreseller naam phone CODE`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]), parse_mode="Markdown")
        return
    text = "👥 *Ei Maser Reseller Report:*\n\n"
    for r in rows:
        text += f"🔸 {r[0]} ({r[1]}) — `{r[2] or 'N/A'}`\n   Order: {r[3]}ta | ৳{r[4]}\n\n"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]), parse_mode="Markdown")

async def show_status_options(query, order_id):
    keyboard = [
        [InlineKeyboardButton("⏳ Processing", callback_data=f"setstatus_{order_id}_processing")],
        [InlineKeyboardButton("✅ Completed", callback_data=f"setstatus_{order_id}_completed")],
        [InlineKeyboardButton("💳 Payment Pending", callback_data=f"setstatus_{order_id}_pending")],
        [InlineKeyboardButton("❌ Cancelled", callback_data=f"setstatus_{order_id}_cancelled")],
        [InlineKeyboardButton("🔙 Back", callback_data="today_orders")]
    ]
    await query.edit_message_text(f"✏️ Order #{order_id} er notun status:", reply_markup=InlineKeyboardMarkup(keyboard))

async def update_order_status_btn(query, order_id, new_status):
    success, result = db_update_order_status(order_id, new_status)
    if success:
        await query.edit_message_text(f"✅ Order #{result} — *{new_status}*!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]), parse_mode="Markdown")
    else:
        await query.edit_message_text(f"❌ {result}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /income [taka] [note]")
        return
    try:
        amount = float(context.args[0])
        note = " ".join(context.args[1:]) if len(context.args) > 1 else "Manual entry"
        db_add_income(amount, note)
        await update.message.reply_text(f"✅ ৳{amount} income add hoye geche!\n📝 {note}")
    except:
        await update.message.reply_text("❌ Vul format!")

async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /customer [email]")
        return
    email = context.args[0].lower()
    conn = get_db()
    rows = conn.run("SELECT woo_order_id, customer_name, total, status, created_at FROM orders WHERE LOWER(customer_email) = :e ORDER BY created_at DESC", e=email)
    conn.close()
    if not rows:
        await update.message.reply_text(f"❌ {email} email e kono order nei.")
        return
    total_spent = sum(float(o[2]) for o in rows)
    text = f"👤 *{rows[0][1]}*\n📧 {email}\n\n"
    for o in rows:
        emoji = "✅" if o[3] == "completed" else "⏳" if o[3] == "processing" else "❌"
        text += f"{emoji} #{o[0]} — {o[4].strftime('%d %b %Y')} | ৳{o[2]} | {o[3]}\n"
    text += f"\n💰 *Total: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def addreseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Format: /addreseller [naam] [phone] [CODE]\nEx: /addreseller Rahul 01712345678 RES001")
        return
    name, phone, code = context.args[0], context.args[1], context.args[2].upper()
    conn = get_db()
    conn.run("INSERT INTO resellers (name, phone, reseller_code) VALUES (:n, :p, :c)", n=name, p=phone, c=code)
    conn.close()
    await update.message.reply_text(f"✅ Reseller added!\n👤 {name} | 📞 {phone} | 🔑 {code}")

async def resellersale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text("Format: /rsale [phone] [product] [qty] [price]")
        return
    try:
        phone, product, quantity, price = context.args[0], context.args[1], int(context.args[2]), float(context.args[3])
        conn = get_db()
        reseller = conn.run("SELECT id, name FROM resellers WHERE phone = :p", p=phone)
        if not reseller:
            conn.close()
            await update.message.reply_text(f"❌ {phone} number e reseller nei.")
            return
        conn.run("INSERT INTO reseller_orders (reseller_id, product, quantity, price) VALUES (:r, :p, :q, :pr)", r=reseller[0][0], p=product, q=quantity, pr=price)
        conn.close()
        await update.message.reply_text(f"✅ {reseller[0][1]} — {product} x{quantity} = ৳{quantity*price}")
    except:
        await update.message.reply_text("❌ Vul format!")

# =================== RESELLER BOT ===================

reseller_user_data = {}

def reseller_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Notun Order", callback_data="res_new_order")],
        [InlineKeyboardButton("📋 Amar Orders", callback_data="res_my_orders")]
    ])

async def reseller_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    reseller = get_reseller_by_chat_id(chat_id)
    if reseller:
        await update.message.reply_text(
            f"Welcome back *{reseller['name']}* bhai! 👋\nTomar code: `{reseller['code']}`",
            reply_markup=reseller_main_menu(), parse_mode="Markdown"
        )
        return ConversationHandler.END
    await update.message.reply_text("🛍️ *Favourite Deals Reseller Bot*\n\nTomar unique reseller code dao:", parse_mode="Markdown")
    return WAITING_CODE

async def reseller_handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    chat_id = update.message.chat_id
    reseller = get_reseller_by_code(code)
    if not reseller:
        await update.message.reply_text("❌ Ei code valid na bhai. Sothik code dao:")
        return WAITING_CODE
    conn = get_db()
    conn.run("UPDATE resellers SET telegram_chat_id = :c WHERE reseller_code = :code", c=str(chat_id), code=code)
    conn.close()
    await update.message.reply_text(f"✅ Welcome *{reseller['name']}* bhai!\nCode: `{code}`",
        reply_markup=reseller_main_menu(), parse_mode="Markdown")
    return ConversationHandler.END

async def reseller_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "res_new_order":
        keyboard = [
            [InlineKeyboardButton("🤖 ChatGPT Plus — ৳1200", callback_data="res_order_chatgpt")],
            [InlineKeyboardButton("💎 Gemini Advanced — ৳1000", callback_data="res_order_gemini")],
        ]
        await query.edit_message_text("🛒 Kon product order korte chao?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("res_order_"):
        product_key = data.replace("res_order_", "")
        product = PRODUCTS.get(product_key)
        if product:
            reseller_user_data[chat_id] = {"product": product_key, "product_name": product['name'], "amount": product['price'], "state": "waiting_email"}
            await query.edit_message_text(
                f"📦 *{product['name']}*\n💵 ৳{product['price']}\n\nJar jonno kincho tar *email* dao:", parse_mode="Markdown")

    elif data == "res_my_orders":
        reseller = get_reseller_by_chat_id(chat_id)
        if not reseller:
            await query.edit_message_text("❌ Register koro aage. /start dao.")
            return
        conn = get_db()
        rows = conn.run("SELECT id, product, customer_email, amount, status FROM reseller_bot_orders WHERE reseller_code = :c ORDER BY created_at DESC LIMIT 10", c=reseller['code'])
        conn.close()
        if not rows:
            await query.edit_message_text("📋 Ekhono kono order nei.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]]))
            return
        text = "📋 *Tomar Orders:*\n\n"
        for r in rows:
            emoji = "✅" if r[4] == "approved" else "❌" if r[4] == "rejected" else "⏳"
            text += f"{emoji} #{r[0]} — {r[1]}\n   📧 {r[2]} | ৳{r[3]} | {r[4]}\n\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="res_back")]]), parse_mode="Markdown")

    elif data == "res_back":
        reseller = get_reseller_by_chat_id(chat_id)
        name = reseller['name'] if reseller else "Bhai"
        await query.edit_message_text(f"Ki korte chao {name}?", reply_markup=reseller_main_menu())

async def reseller_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.message.chat_id
    user_state = reseller_user_data.get(chat_id, {})
    state = user_state.get("state")
    reseller = get_reseller_by_chat_id(chat_id)

    if not reseller:
        await update.message.reply_text("Aage /start diye register koro bhai!")
        return

    if state == "waiting_email":
        if '@' not in text or '.' not in text:
            await update.message.reply_text("❌ Valid email dao (e.g. example@gmail.com):")
            return
        reseller_user_data[chat_id]["customer_email"] = text
        reseller_user_data[chat_id]["state"] = "waiting_transaction"
        await update.message.reply_text(
            f"📧 Email: `{text}`\n\nEkhon *Bkash Transaction ID* dao:\n৳{user_state['amount']} send korar por ID pathao:", parse_mode="Markdown")

    elif state == "waiting_transaction":
        reseller_user_data[chat_id]["state"] = None
        product_name = user_state.get("product_name")
        customer_email = user_state.get("customer_email")
        amount = user_state.get("amount")

        conn = get_db()
        rows = conn.run(
            "INSERT INTO reseller_bot_orders (reseller_id, reseller_code, product, customer_email, transaction_id, amount, status) VALUES (:rid, :code, :p, :e, :t, :a, 'pending') RETURNING id",
            rid=reseller['id'], code=reseller['code'], p=product_name, e=customer_email, t=text, a=amount
        )
        conn.close()
        order_id = rows[0][0] if rows else None

        if order_id:
            try:
                from telegram import Bot
                main_bot = Bot(token=BOT_TOKEN)
                msg = (f"🔔 *Notun Reseller Order!*\n\n👤 {reseller['name']} ({reseller['code']})\n"
                       f"📦 {product_name}\n📧 {customer_email}\n💳 TxnID: {text}\n💵 ৳{amount}\n🆔 Order #{order_id}")
                keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"rapprove_{order_id}"),
                             InlineKeyboardButton("❌ Reject", callback_data=f"rreject_{order_id}")]]
                await main_bot.send_message(chat_id=MAIN_CHAT_ID, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Main bot notify error: {e}")

            await update.message.reply_text(
                f"✅ *Order Submit Hoye Geche!*\n\n📋 #{order_id}\n📦 {product_name}\n📧 {customer_email}\n💳 {text}\n💵 ৳{amount}\n\n⏳ Admin confirm korle notify pabe!",
                reply_markup=reseller_main_menu(), parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Problem hoye geche. Abar try koro.")
        reseller_user_data.pop(chat_id, None)
    else:
        await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=reseller_main_menu())

# =================== FLASK WEBHOOK ===================

@app.route('/webhook/woocommerce', methods=['POST'])
def woocommerce_webhook():
    try:
        raw_data = request.data
        if not raw_data:
            return jsonify({"status": "ok"}), 200
        try:
            data = json.loads(raw_data)
        except:
            return jsonify({"status": "ok"}), 200
        if not data:
            return jsonify({"status": "ok"}), 200
        order_id = str(data.get('id', 'N/A'))
        customer = data.get('billing', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Unknown"
        customer_email = customer.get('email', '')
        total = float(data.get('total', 0))
        status = data.get('status', 'pending')
        items_text = ", ".join([f"{i['name']} x{i['quantity']}" for i in data.get('line_items', [])])
        conn = get_db()
        conn.run("INSERT INTO orders (woo_order_id, customer_name, customer_email, total, status, items) VALUES (:o, :n, :e, :t, :s, :i)",
                 o=order_id, n=customer_name, e=customer_email, t=total, s=status, i=items_text)
        conn.run("INSERT INTO income (amount, note, type) VALUES (:a, :n, 'auto')", a=total, n=f"WooCommerce Order #{order_id}")
        conn.close()
        msg = f"🛍️ *Notun Order!*\n\n📋 #{order_id}\n👤 {customer_name}\n📧 {customer_email}\n📦 {items_text}\n💵 ৳{total}\n📊 {status}"
        asyncio.run_coroutine_threadsafe(send_telegram_message(msg), main_loop)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "running"}), 200

async def send_telegram_message(message):
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

async def main():
    global main_loop
    main_loop = asyncio.get_event_loop()
    setup_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Main bot
    main_app = Application.builder().token(BOT_TOKEN).build()
    main_app.add_handler(CommandHandler("start", start))
    main_app.add_handler(CommandHandler("income", income_command))
    main_app.add_handler(CommandHandler("customer", customer_command))
    main_app.add_handler(CommandHandler("addreseller", addreseller_command))
    main_app.add_handler(CommandHandler("rsale", resellersale_command))
    main_app.add_handler(CallbackQueryHandler(button_handler))
    main_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Reseller bot
    reseller_conv = ConversationHandler(
        entry_points=[CommandHandler("start", reseller_start)],
        states={WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reseller_handle_code)]},
        fallbacks=[CommandHandler("start", reseller_start)]
    )
    reseller_app = Application.builder().token(RESELLER_BOT_TOKEN).build()
    reseller_app.add_handler(reseller_conv)
    reseller_app.add_handler(CallbackQueryHandler(reseller_button_handler))
    reseller_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reseller_handle_text))

    logger.info("Both bots started!")

    # Run both bots together
    async with main_app, reseller_app:
        await main_app.initialize()
        await reseller_app.initialize()
        await main_app.start()
        await reseller_app.start()
        await main_app.updater.start_polling()
        await reseller_app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
