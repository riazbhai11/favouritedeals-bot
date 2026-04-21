import os
import logging
import pg8000.native
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WC_KEY = os.environ.get("WC_KEY")
WC_SECRET = os.environ.get("WC_SECRET")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

app = Flask(__name__)
main_loop = None
user_conversations = {}

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
        id SERIAL PRIMARY KEY,
        woo_order_id VARCHAR(50),
        customer_name VARCHAR(200),
        customer_email VARCHAR(200),
        total DECIMAL(10,2),
        status VARCHAR(50),
        items TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.run("""CREATE TABLE IF NOT EXISTS income (
        id SERIAL PRIMARY KEY,
        amount DECIMAL(10,2),
        note TEXT,
        type VARCHAR(20) DEFAULT 'manual',
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.run("""CREATE TABLE IF NOT EXISTS resellers (
        id SERIAL PRIMARY KEY,
        name VARCHAR(200),
        phone VARCHAR(50),
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.run("""CREATE TABLE IF NOT EXISTS reseller_orders (
        id SERIAL PRIMARY KEY,
        reseller_id INTEGER REFERENCES resellers(id),
        product TEXT,
        quantity INTEGER,
        price DECIMAL(10,2),
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    conn.close()
    logger.info("Database setup complete!")

# =================== DATABASE FUNCTIONS ===================

def db_get_recent_orders(limit=10, status=None):
    conn = get_db()
    if status:
        rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, items, created_at FROM orders WHERE status = :status ORDER BY created_at DESC LIMIT :limit", status=status, limit=limit)
    else:
        rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, items, created_at FROM orders ORDER BY created_at DESC LIMIT :limit", limit=limit)
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
    db_id = rows[0][0]
    woo_id = rows[0][1]
    conn.run("UPDATE orders SET status = :status WHERE id = :id", status=new_status, id=db_id)
    conn.close()
    try:
        wc_url = f"https://favouritedeals.online/wp-json/wc/v3/orders/{woo_id}"
        req.put(wc_url, json={"status": new_status}, auth=(WC_KEY, WC_SECRET), timeout=10)
    except Exception as e:
        logger.error(f"WC update error: {e}")
    return True, woo_id

def db_get_income_summary(days=1):
    conn = get_db()
    since = datetime.now() - timedelta(days=days)
    rows = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at >= :since", since=since)
    conn.close()
    return {"total": str(rows[0][0] or 0), "count": rows[0][1] or 0}

def db_get_orders_summary(days=1):
    conn = get_db()
    since = datetime.now() - timedelta(days=days)
    rows = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at >= :since", since=since)
    conn.close()
    return {"count": rows[0][0] or 0, "total": str(rows[0][1] or 0)}

def db_search_orders_by_name(name):
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, customer_email, total, status, created_at FROM orders WHERE LOWER(customer_name) LIKE :name ORDER BY created_at DESC LIMIT 5", name=f"%{name.lower()}%")
    conn.close()
    return [{"id": r[0], "woo_order_id": r[1], "customer_name": r[2], "customer_email": r[3], "total": str(r[4]), "status": r[5], "created_at": str(r[6])} for r in rows]

def db_add_income(amount, note):
    conn = get_db()
    conn.run("INSERT INTO income (amount, note, type) VALUES (:amount, :note, 'manual')", amount=float(amount), note=note)
    conn.close()
    return True

def db_get_reseller_summary(reseller_name=None):
    conn = get_db()
    if reseller_name:
        rows = conn.run("""
            SELECT r.name, r.phone, COUNT(ro.id), COALESCE(SUM(ro.price * ro.quantity), 0)
            FROM resellers r
            LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id AND ro.created_at >= date_trunc('month', NOW())
            WHERE LOWER(r.name) LIKE :name
            GROUP BY r.id, r.name, r.phone
        """, name=f"%{reseller_name.lower()}%")
    else:
        rows = conn.run("""
            SELECT r.name, r.phone, COUNT(ro.id), COALESCE(SUM(ro.price * ro.quantity), 0)
            FROM resellers r
            LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id AND ro.created_at >= date_trunc('month', NOW())
            GROUP BY r.id, r.name, r.phone
        """)
    conn.close()
    return [{"name": r[0], "phone": r[1], "orders": r[2], "total": str(r[3])} for r in rows]

# =================== AI FUNCTIONS ===================

AI_FUNCTIONS = [
    {
        "name": "get_recent_orders",
        "description": "Recent orders dekhao. Status diye filter kora jabe.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Kototа order dekhabe, default 5"},
                "status": {"type": "string", "description": "Filter by status: processing, completed, pending, cancelled"}
            }
        }
    },
    {
        "name": "get_last_order",
        "description": "Sorboshesh order dekhao",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "update_order_status",
        "description": "Order er status change koro",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order ID"},
                "new_status": {"type": "string", "description": "New status: processing, completed, pending, cancelled"},
                "use_woo_id": {"type": "boolean", "description": "True hole WooCommerce order ID use korbe"}
            },
            "required": ["order_id", "new_status"]
        }
    },
    {
        "name": "get_income_summary",
        "description": "Income summary dekhao",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Koto diner income, default 1 (aajke)"}
            }
        }
    },
    {
        "name": "get_orders_summary",
        "description": "Orders summary/count dekhao",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Koto diner orders, default 1 (aajke)"}
            }
        }
    },
    {
        "name": "search_orders_by_name",
        "description": "Customer naam diye order khojo",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer er naam"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "add_income",
        "description": "Manual income jog koro",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Takar poriman"},
                "note": {"type": "string", "description": "Note ba karon"}
            },
            "required": ["amount", "note"]
        }
    },
    {
        "name": "get_reseller_summary",
        "description": "Reseller er summary dekhao",
        "parameters": {
            "type": "object",
            "properties": {
                "reseller_name": {"type": "string", "description": "Reseller er naam, khali rakhole sob dekhabe"}
            }
        }
    }
]

def execute_function(name, args):
    try:
        if name == "get_recent_orders":
            return db_get_recent_orders(args.get("limit", 5), args.get("status"))
        elif name == "get_last_order":
            return db_get_last_order()
        elif name == "update_order_status":
            # First try with woo_id, then with db id
            success, result = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=True)
            if not success:
                success, result = db_update_order_status(args["order_id"], args["new_status"], use_woo_id=False)
            return {"success": success, "result": result}
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

Tumi Banglish e kotha bolbe - mane Bangla sentence structure kintu English haroof e lekha. Jemon:
- "Bhai, last order ta #23410, Mehedi Hassan er. Status ekhon processing ache."
- "Haan bhai, income add hoye geche! 500 taka jog korা hoyeche."
- "Kono order paoa jaini bhai ei naam e."

Tumi chhoto chhoto sentence e bolbe, casual tone e, jeno ekjon helper/employee kotha bolche.
"yes/haan/ok/ha/koro" mane age jar kaj confirm koro.
Kono kaaj korle confirm kore janaabe.
Database functions use kore real data dekhabe."""

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

# =================== TELEGRAM BOT ===================

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
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")

    if chat_id not in user_conversations:
        user_conversations[chat_id] = []

    user_conversations[chat_id].append({"role": "user", "content": text})

    if len(user_conversations[chat_id]) > 10:
        user_conversations[chat_id] = user_conversations[chat_id][-10:]

    ai_reply = await process_ai_message(user_conversations[chat_id])

    if ai_reply:
        user_conversations[chat_id].append({"role": "assistant", "content": ai_reply})
        await update.message.reply_text(
            ai_reply,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )
    else:
        await update.message.reply_text("Menu theke kaj koro bhai:", reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

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
        await query.edit_message_text("💰 Manual income add korte likho:\n\n`/income 500 bkash e paisi`", parse_mode="Markdown")
    elif data == "search_customer":
        await query.edit_message_text("🔍 Customer er email diye likho:\n\n`/customer example@email.com`", parse_mode="Markdown")
    elif data == "menu":
        await query.edit_message_text(
            "🛍️ *FD Assistant*\n\nMenu theke kaj koro ba seedha bolo:",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
    elif data.startswith("status_"):
        order_id = data.split("_")[1]
        await show_status_options(query, order_id)
    elif data.startswith("setstatus_"):
        parts = data.split("_")
        order_id = parts[1]
        new_status = parts[2]
        await update_order_status_btn(query, order_id, new_status)

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, total, status FROM orders WHERE created_at >= :since ORDER BY created_at DESC", since=since)
    conn.close()

    if not rows:
        label = "aajke" if days == 1 else f"last {days} din e"
        await query.edit_message_text(
            f"📦 {label} kono order nei bhai.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
        )
        return

    text = f"📦 *Last {days} diner order ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n"
        text += f"   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status change", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_orders_by_status(query, status):
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, total, status FROM orders WHERE status = :status ORDER BY created_at DESC LIMIT 10", status=status)
    conn.close()

    if not rows:
        await query.edit_message_text(
            f"📦 {status} status e kono order nei bhai.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
        )
        return

    text = f"📦 *{status} orders ({len(rows)}ta):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n"
        text += f"   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status change", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_income(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn = get_db()
    rows = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at >= :since", since=since)
    conn.close()
    total = rows[0][0] or 0
    count = rows[0][1] or 0
    label = "Aajker" if days == 1 else f"Last {days} diner"
    await query.edit_message_text(
        f"💰 *{label} Income*\n\nMot: ৳{total}\nEntry: {count}ta",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown"
    )

async def show_month_report(query):
    since = datetime.now() - timedelta(days=30)
    conn = get_db()
    order_rows = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at >= :since", since=since)
    income_rows = conn.run("SELECT SUM(amount) FROM income WHERE created_at >= :since", since=since)
    conn.close()
    text = "📊 *Last 30 diner Report*\n\n"
    text += f"📦 Total Order: {order_rows[0][0] or 0}ta\n"
    text += f"💵 WooCommerce Revenue: ৳{order_rows[0][1] or 0}\n"
    text += f"💰 Manual Income: ৳{income_rows[0][0] or 0}\n"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown"
    )

async def show_resellers(query):
    conn = get_db()
    rows = conn.run("""
        SELECT r.name, r.phone, COUNT(ro.id), COALESCE(SUM(ro.price * ro.quantity), 0)
        FROM resellers r
        LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id
        AND ro.created_at >= date_trunc('month', NOW())
        GROUP BY r.id, r.name, r.phone
    """)
    conn.close()
    if not rows:
        await query.edit_message_text(
            "👥 Kono reseller nei bhai.\n\nAdd korte: `/addreseller naam phone`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
            parse_mode="Markdown"
        )
        return
    text = "👥 *Ei Maser Reseller Report:*\n\n"
    for r in rows:
        text += f"🔸 {r[0]} ({r[1]})\n"
        text += f"   Order: {r[2]}ta | Mot: ৳{r[3]}\n\n"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
        parse_mode="Markdown"
    )

async def show_status_options(query, order_id):
    keyboard = [
        [InlineKeyboardButton("⏳ Processing", callback_data=f"setstatus_{order_id}_processing")],
        [InlineKeyboardButton("✅ Completed", callback_data=f"setstatus_{order_id}_completed")],
        [InlineKeyboardButton("💳 Payment Pending", callback_data=f"setstatus_{order_id}_pending")],
        [InlineKeyboardButton("❌ Cancelled", callback_data=f"setstatus_{order_id}_cancelled")],
        [InlineKeyboardButton("🔙 Pichone", callback_data="today_orders")]
    ]
    await query.edit_message_text(
        f"✏️ Order #{order_id} er notun status choose koro:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def update_order_status_btn(query, order_id, new_status):
    success, result = db_update_order_status(order_id, new_status)
    if success:
        await query.edit_message_text(
            f"✅ Order #{result} er status *{new_status}* kora hoyeche!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]),
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(
            f"❌ {result}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
        )

async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /income [taka] [note]\nUdahoron: /income 500 bkash e paisi")
        return
    try:
        amount = float(context.args[0])
        note = " ".join(context.args[1:]) if len(context.args) > 1 else "Manual entry"
        db_add_income(amount, note)
        await update.message.reply_text(f"✅ Income add hoye geche bhai!\n💰 ৳{amount}\n📝 {note}")
    except:
        await update.message.reply_text("❌ Vul format! Udahoron: /income 500 bkash e paisi")

async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Format: /customer [email]")
        return
    email = context.args[0].lower()
    conn = get_db()
    rows = conn.run("SELECT woo_order_id, customer_name, total, status, created_at FROM orders WHERE LOWER(customer_email) = :email ORDER BY created_at DESC", email=email)
    conn.close()
    if not rows:
        await update.message.reply_text(f"❌ {email} ei email e kono order nei bhai.")
        return
    total_spent = sum(float(o[2]) for o in rows)
    text = f"👤 *Customer: {rows[0][1]}*\n📧 {email}\n\n"
    for o in rows:
        status_emoji = "✅" if o[3] == "completed" else "⏳" if o[3] == "processing" else "❌"
        text += f"{status_emoji} Order #{o[0]} — {o[4].strftime('%d %b %Y')}\n"
        text += f"   ৳{o[2]} | {o[3]}\n\n"
    text += f"💰 *Total khorch: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def addreseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Format: /addreseller [naam] [phone]")
        return
    name = context.args[0]
    phone = context.args[1]
    conn = get_db()
    conn.run("INSERT INTO resellers (name, phone) VALUES (:name, :phone)", name=name, phone=phone)
    conn.close()
    await update.message.reply_text(f"✅ Reseller add hoyeche bhai!\n👤 {name}\n📞 {phone}")

async def resellersale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text("Format: /rsale [phone] [product] [quantity] [price]")
        return
    try:
        phone = context.args[0]
        product = context.args[1]
        quantity = int(context.args[2])
        price = float(context.args[3])
        conn = get_db()
        reseller = conn.run("SELECT id, name FROM resellers WHERE phone = :phone", phone=phone)
        if not reseller:
            conn.close()
            await update.message.reply_text(f"❌ {phone} ei number e kono reseller nei bhai.")
            return
        conn.run("INSERT INTO reseller_orders (reseller_id, product, quantity, price) VALUES (:rid, :product, :quantity, :price)",
                 rid=reseller[0][0], product=product, quantity=quantity, price=price)
        conn.close()
        total = quantity * price
        await update.message.reply_text(f"✅ Reseller sale record hoyeche!\n👤 {reseller[0][1]}\n📦 {product} x{quantity}\n💰 ৳{total}")
    except:
        await update.message.reply_text("❌ Vul format!")

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
        line_items = data.get('line_items', [])
        items_text = ", ".join([f"{item['name']} x{item['quantity']}" for item in line_items])

        conn = get_db()
        conn.run(
            "INSERT INTO orders (woo_order_id, customer_name, customer_email, total, status, items) VALUES (:oid, :name, :email, :total, :status, :items)",
            oid=order_id, name=customer_name, email=customer_email, total=total, status=status, items=items_text
        )
        conn.run("INSERT INTO income (amount, note, type) VALUES (:amount, :note, 'auto')",
                 amount=total, note=f"WooCommerce Order #{order_id}")
        conn.close()

        message = (
            f"🛍️ *Notun Order Aseche!*\n\n"
            f"📋 Order #{order_id}\n"
            f"👤 {customer_name}\n"
            f"📧 {customer_email}\n"
            f"📦 {items_text}\n"
            f"💵 ৳{total}\n"
            f"📊 Status: {status}"
        )
        asyncio.run_coroutine_threadsafe(send_telegram_message(message), main_loop)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("income", income_command))
    application.add_handler(CommandHandler("customer", customer_command))
    application.add_handler(CommandHandler("addreseller", addreseller_command))
    application.add_handler(CommandHandler("rsale", resellersale_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started!")
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
