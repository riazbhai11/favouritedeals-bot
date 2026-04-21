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
    conn.run("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            woo_order_id VARCHAR(50),
            customer_name VARCHAR(200),
            customer_email VARCHAR(200),
            total DECIMAL(10,2),
            status VARCHAR(50),
            items TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.run("""
        CREATE TABLE IF NOT EXISTS income (
            id SERIAL PRIMARY KEY,
            amount DECIMAL(10,2),
            note TEXT,
            type VARCHAR(20) DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.run("""
        CREATE TABLE IF NOT EXISTS resellers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200),
            phone VARCHAR(50),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.run("""
        CREATE TABLE IF NOT EXISTS reseller_orders (
            id SERIAL PRIMARY KEY,
            reseller_id INTEGER REFERENCES resellers(id),
            product TEXT,
            quantity INTEGER,
            price DECIMAL(10,2),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.close()
    logger.info("Database setup complete!")

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 আজকের অর্ডার", callback_data="today_orders"),
         InlineKeyboardButton("💰 আজকের ইনকাম", callback_data="today_income")],
        [InlineKeyboardButton("📅 ৭ দিনের অর্ডার", callback_data="week_orders"),
         InlineKeyboardButton("📊 মাসের রিপোর্ট", callback_data="month_report")],
        [InlineKeyboardButton("👥 রিসেলার", callback_data="resellers"),
         InlineKeyboardButton("➕ ম্যানুয়াল ইনকাম", callback_data="manual_income")],
        [InlineKeyboardButton("🔍 কাস্টমার খুঁজুন", callback_data="search_customer")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛍️ *Favourite Deals Bot*\n\nস্বাগতম! নিচের মেনু থেকে যা দরকার select করো:\n\nঅথবা সরাসরি প্রশ্ন করো! 🤖",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    conn = get_db()
    today_orders = conn.run("SELECT COUNT(*) FROM orders WHERE created_at >= NOW() - INTERVAL '1 day'")
    today_income = conn.run("SELECT COALESCE(SUM(amount), 0) FROM income WHERE created_at >= NOW() - INTERVAL '1 day'")
    month_orders = conn.run("SELECT COUNT(*) FROM orders WHERE created_at >= NOW() - INTERVAL '30 days'")
    month_income = conn.run("SELECT COALESCE(SUM(amount), 0) FROM income WHERE created_at >= NOW() - INTERVAL '30 days'")
    conn.close()

    system_context = f"""তুমি Favourite Deals এর AI business assistant।
আজকের অর্ডার: {today_orders[0][0]}টি
আজকের ইনকাম: ৳{today_income[0][0]}
এই মাসের অর্ডার: {month_orders[0][0]}টি
এই মাসের ইনকাম: ৳{month_income[0][0]}

ব্যবহারকারীর প্রশ্নের উত্তর বাংলায় দাও। সংক্ষিপ্ত ও সহায়ক হও।
মেনু দেখতে /start লেখো বলো।"""

    try:
        response = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "system", "content": system_context},
                    {"role": "user", "content": text}
                ],
                "max_tokens": 500
            },
            timeout=10
        )
        resp_json = response.json()
        if 'choices' in resp_json:
            reply = resp_json['choices'][0]['message']['content']
        elif 'error' in resp_json:
            logger.error(f"OpenAI error: {resp_json['error']}")
            reply = "দুঃখিত, AI সাড়া দিচ্ছে না।"
        else:
            logger.error(f"OpenAI unknown: {resp_json}")
            reply = "দুঃখিত, AI সাড়া দিচ্ছে না।"
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "দুঃখিত, এখন উত্তর দিতে পারছি না। মেনুর জন্য /start দাও।"

    await update.message.reply_text(reply)

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
    elif data == "manual_income":
        await query.edit_message_text("💰 ম্যানুয়াল ইনকাম যোগ করতে লেখো:\n\n`/income 500 বিকাশে পেমেন্ট`", parse_mode="Markdown")
    elif data == "search_customer":
        await query.edit_message_text("🔍 কাস্টমারের email দিয়ে লেখো:\n\n`/customer example@email.com`", parse_mode="Markdown")
    elif data == "menu":
        await query.edit_message_text(
            "🛍️ *Favourite Deals Bot*\n\nমেনু থেকে যা দরকার select করো:",
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
        await update_order_status(query, order_id, new_status)

async def show_orders(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn = get_db()
    rows = conn.run("SELECT id, woo_order_id, customer_name, total, status FROM orders WHERE created_at >= :since ORDER BY created_at DESC", since=since)
    conn.close()

    if not rows:
        label = "আজকে" if days == 1 else f"শেষ {days} দিনে"
        await query.edit_message_text(
            f"📦 {label} কোনো অর্ডার নেই।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]])
        )
        return

    text = f"📦 *শেষ {days} দিনের অর্ডার ({len(rows)}টি):*\n\n"
    keyboard = []
    for o in rows[:10]:
        text += f"🔸 #{o[1]} — {o[2]}\n"
        text += f"   💵 ৳{o[3]} | {o[4]}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o[1]} status বদলাও", callback_data=f"status_{o[0]}")])
    keyboard.append([InlineKeyboardButton("🔙 মেনু", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_income(query, days=1):
    since = datetime.now() - timedelta(days=days)
    conn = get_db()
    rows = conn.run("SELECT SUM(amount), COUNT(*) FROM income WHERE created_at >= :since", since=since)
    conn.close()
    total = rows[0][0] or 0
    count = rows[0][1] or 0
    label = "আজকের" if days == 1 else f"শেষ {days} দিনের"
    await query.edit_message_text(
        f"💰 *{label} ইনকাম*\n\nমোট: ৳{total}\nএন্ট্রি: {count}টি",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]),
        parse_mode="Markdown"
    )

async def show_month_report(query):
    since = datetime.now() - timedelta(days=30)
    conn = get_db()
    order_rows = conn.run("SELECT COUNT(*), SUM(total) FROM orders WHERE created_at >= :since", since=since)
    income_rows = conn.run("SELECT SUM(amount) FROM income WHERE created_at >= :since", since=since)
    conn.close()
    text = "📊 *মাসের রিপোর্ট (শেষ ৩০ দিন)*\n\n"
    text += f"📦 মোট অর্ডার: {order_rows[0][0] or 0}টি\n"
    text += f"💵 WooCommerce রেভেনিউ: ৳{order_rows[0][1] or 0}\n"
    text += f"💰 মোট ম্যানুয়াল ইনকাম: ৳{income_rows[0][0] or 0}\n"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]),
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
            "👥 কোনো রিসেলার নেই।\n\nযোগ করতে: `/addreseller নাম ফোন`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]),
            parse_mode="Markdown"
        )
        return
    text = "👥 *এই মাসের রিসেলার রিপোর্ট:*\n\n"
    for r in rows:
        text += f"🔸 {r[0]} ({r[1]})\n"
        text += f"   অর্ডার: {r[2]}টি | মোট: ৳{r[3]}\n\n"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]),
        parse_mode="Markdown"
    )

async def show_status_options(query, order_id):
    keyboard = [
        [InlineKeyboardButton("⏳ Processing", callback_data=f"setstatus_{order_id}_processing")],
        [InlineKeyboardButton("✅ Completed", callback_data=f"setstatus_{order_id}_completed")],
        [InlineKeyboardButton("💳 Payment Pending", callback_data=f"setstatus_{order_id}_pending")],
        [InlineKeyboardButton("❌ Cancelled", callback_data=f"setstatus_{order_id}_cancelled")],
        [InlineKeyboardButton("🔙 পিছনে", callback_data="today_orders")]
    ]
    await query.edit_message_text(
        f"✏️ Order #{order_id} এর নতুন status বেছে নাও:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def update_order_status(query, order_id, new_status):
    conn = get_db()
    rows = conn.run("SELECT woo_order_id FROM orders WHERE id = :id", id=int(order_id))
    conn.run("UPDATE orders SET status = :status WHERE id = :id", status=new_status, id=int(order_id))
    conn.close()

    if rows:
        woo_order_id = rows[0][0]
        wc_url = f"https://favouritedeals.online/wp-json/wc/v3/orders/{woo_order_id}"
        try:
            req.put(wc_url, json={"status": new_status}, auth=(WC_KEY, WC_SECRET), timeout=10)
        except Exception as e:
            logger.error(f"WC update error: {e}")

    await query.edit_message_text(
        f"✅ Order #{order_id} এর status *{new_status}* করা হয়েছে!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]),
        parse_mode="Markdown"
    )

async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("ফরম্যাট: /income [টাকা] [নোট]\nউদাহরণ: /income 500 বিকাশে পেমেন্ট")
        return
    try:
        amount = float(context.args[0])
        note = " ".join(context.args[1:]) if len(context.args) > 1 else "ম্যানুয়াল এন্ট্রি"
        conn = get_db()
        conn.run("INSERT INTO income (amount, note, type) VALUES (:amount, :note, 'manual')", amount=amount, note=note)
        conn.close()
        await update.message.reply_text(f"✅ ইনকাম যোগ হয়েছে!\n💰 ৳{amount}\n📝 {note}")
    except:
        await update.message.reply_text("❌ ভুল ফরম্যাট! উদাহরণ: /income 500 বিকাশে পেমেন্ট")

async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("ফরম্যাট: /customer [email]\nউদাহরণ: /customer example@gmail.com")
        return
    email = context.args[0].lower()
    conn = get_db()
    rows = conn.run("SELECT woo_order_id, customer_name, total, status, created_at FROM orders WHERE LOWER(customer_email) = :email ORDER BY created_at DESC", email=email)
    conn.close()
    if not rows:
        await update.message.reply_text(f"❌ {email} এই email এ কোনো অর্ডার পাওয়া যায়নি।")
        return
    total_spent = sum(float(o[2]) for o in rows)
    text = f"👤 *Customer: {rows[0][1]}*\n📧 {email}\n\n"
    for o in rows:
        status_emoji = "✅" if o[3] == "completed" else "⏳" if o[3] == "processing" else "❌"
        text += f"{status_emoji} Order #{o[0]} — {o[4].strftime('%d %b %Y')}\n"
        text += f"   ৳{o[2]} | {o[3]}\n\n"
    text += f"💰 *মোট খরচ: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def addreseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("ফরম্যাট: /addreseller [নাম] [ফোন]\nউদাহরণ: /addreseller রাহুল 01712345678")
        return
    name = context.args[0]
    phone = context.args[1]
    conn = get_db()
    conn.run("INSERT INTO resellers (name, phone) VALUES (:name, :phone)", name=name, phone=phone)
    conn.close()
    await update.message.reply_text(f"✅ রিসেলার যোগ হয়েছে!\n👤 {name}\n📞 {phone}")

async def resellersale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text("ফরম্যাট: /rsale [ফোন] [পণ্য] [পরিমাণ] [দাম]\nউদাহরণ: /rsale 01712345678 শার্ট 3 450")
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
            await update.message.reply_text(f"❌ {phone} এই নম্বরে কোনো রিসেলার নেই।")
            return
        conn.run("INSERT INTO reseller_orders (reseller_id, product, quantity, price) VALUES (:rid, :product, :quantity, :price)",
                 rid=reseller[0][0], product=product, quantity=quantity, price=price)
        conn.close()
        total = quantity * price
        await update.message.reply_text(f"✅ রিসেলার সেল রেকর্ড হয়েছে!\n👤 {reseller[0][1]}\n📦 {product} x{quantity}\n💰 ৳{total}")
    except:
        await update.message.reply_text("❌ ভুল ফরম্যাট! উদাহরণ: /rsale 01712345678 শার্ট 3 450")

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
            f"🛍️ *নতুন অর্ডার!*\n\n"
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
