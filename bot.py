import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

app = Flask(__name__)

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def setup_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS income (
            id SERIAL PRIMARY KEY,
            amount DECIMAL(10,2),
            note TEXT,
            type VARCHAR(20) DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS resellers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200),
            phone VARCHAR(50),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reseller_orders (
            id SERIAL PRIMARY KEY,
            reseller_id INTEGER REFERENCES resellers(id),
            product TEXT,
            quantity INTEGER,
            price DECIMAL(10,2),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# =================== TELEGRAM BOT ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 আজকের অর্ডার", callback_data="today_orders"),
         InlineKeyboardButton("💰 আজকের ইনকাম", callback_data="today_income")],
        [InlineKeyboardButton("📅 ৭ দিনের অর্ডার", callback_data="week_orders"),
         InlineKeyboardButton("📊 মাসের রিপোর্ট", callback_data="month_report")],
        [InlineKeyboardButton("👥 রিসেলার", callback_data="resellers"),
         InlineKeyboardButton("➕ ম্যানুয়াল ইনকাম", callback_data="manual_income")],
        [InlineKeyboardButton("🔍 কাস্টমার খুঁজুন", callback_data="search_customer")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🛍️ *Favourite Deals Bot*\n\nস্বাগতম! নিচের মেনু থেকে যা দরকার select করো:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

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
        await query.edit_message_text("💰 ম্যানুয়াল ইনকাম যোগ করতে লেখো:\n\n`/income 500 বিকাশে পেমেন্ট`\n\nফরম্যাট: /income [টাকা] [নোট]", parse_mode="Markdown")
    elif data == "search_customer":
        await query.edit_message_text("🔍 কাস্টমারের email দিয়ে লেখো:\n\n`/customer example@email.com`", parse_mode="Markdown")
    elif data == "menu":
        keyboard = [
            [InlineKeyboardButton("📦 আজকের অর্ডার", callback_data="today_orders"),
             InlineKeyboardButton("💰 আজকের ইনকাম", callback_data="today_income")],
            [InlineKeyboardButton("📅 ৭ দিনের অর্ডার", callback_data="week_orders"),
             InlineKeyboardButton("📊 মাসের রিপোর্ট", callback_data="month_report")],
            [InlineKeyboardButton("👥 রিসেলার", callback_data="resellers"),
             InlineKeyboardButton("➕ ম্যানুয়াল ইনকাম", callback_data="manual_income")],
            [InlineKeyboardButton("🔍 কাস্টমার খুঁজুন", callback_data="search_customer")]
        ]
        await query.edit_message_text(
            "🛍️ *Favourite Deals Bot*\n\nমেনু থেকে যা দরকার select করো:",
            reply_markup=InlineKeyboardMarkup(keyboard),
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
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    since = datetime.now() - timedelta(days=days)
    cur.execute("SELECT * FROM orders WHERE created_at >= %s ORDER BY created_at DESC", (since,))
    orders = cur.fetchall()
    cur.close()
    conn.close()

    if not orders:
        label = "আজকে" if days == 1 else f"শেষ {days} দিনে"
        keyboard = [[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]
        await query.edit_message_text(f"📦 {label} কোনো অর্ডার নেই।", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text = f"📦 *শেষ {days} দিনের অর্ডার ({len(orders)}টি):*\n\n"
    keyboard = []
    for o in orders[:10]:
        text += f"🔸 #{o['woo_order_id']} — {o['customer_name']}\n"
        text += f"   💵 ৳{o['total']} | {o['status']}\n\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{o['woo_order_id']} status বদলাও", callback_data=f"status_{o['id']}")])

    keyboard.append([InlineKeyboardButton("🔙 মেনু", callback_data="menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_income(query, days=1):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    since = datetime.now() - timedelta(days=days)
    cur.execute("SELECT SUM(amount) as total, COUNT(*) as count FROM income WHERE created_at >= %s", (since,))
    result = cur.fetchone()
    cur.close()
    conn.close()

    total = result['total'] or 0
    count = result['count'] or 0
    label = "আজকের" if days == 1 else f"শেষ {days} দিনের"
    keyboard = [[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]
    await query.edit_message_text(
        f"💰 *{label} ইনকাম*\n\nমোট: ৳{total}\nএন্ট্রি: {count}টি",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_month_report(query):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    since = datetime.now() - timedelta(days=30)
    cur.execute("SELECT COUNT(*) as orders, SUM(total) as revenue FROM orders WHERE created_at >= %s", (since,))
    order_data = cur.fetchone()
    cur.execute("SELECT SUM(amount) as income FROM income WHERE created_at >= %s", (since,))
    income_data = cur.fetchone()
    cur.close()
    conn.close()

    text = f"📊 *মাসের রিপোর্ট (শেষ ৩০ দিন)*\n\n"
    text += f"📦 মোট অর্ডার: {order_data['orders'] or 0}টি\n"
    text += f"💵 WooCommerce রেভেনিউ: ৳{order_data['revenue'] or 0}\n"
    text += f"💰 মোট ম্যানুয়াল ইনকাম: ৳{income_data['income'] or 0}\n"
    keyboard = [[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_resellers(query):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT r.id, r.name, r.phone, COUNT(ro.id) as total_orders, COALESCE(SUM(ro.price * ro.quantity), 0) as total_amount
        FROM resellers r
        LEFT JOIN reseller_orders ro ON r.id = ro.reseller_id
        AND ro.created_at >= date_trunc('month', NOW())
        GROUP BY r.id, r.name, r.phone
    """)
    resellers = cur.fetchall()
    cur.close()
    conn.close()

    if not resellers:
        keyboard = [[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]
        await query.edit_message_text(
            "👥 কোনো রিসেলার নেই।\n\nযোগ করতে লেখো:\n`/addreseller নাম ফোন`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    text = "👥 *এই মাসের রিসেলার রিপোর্ট:*\n\n"
    for r in resellers:
        text += f"🔸 {r['name']} ({r['phone']})\n"
        text += f"   অর্ডার: {r['total_orders']}টি | মোট: ৳{r['total_amount']}\n\n"
    keyboard = [[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

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
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = %s WHERE id = %s", (new_status, order_id))
    conn.commit()
    cur.close()
    conn.close()
    keyboard = [[InlineKeyboardButton("🔙 মেনু", callback_data="menu")]]
    await query.edit_message_text(
        f"✅ Order #{order_id} এর status *{new_status}* করা হয়েছে!",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
        cur = conn.cursor()
        cur.execute("INSERT INTO income (amount, note, type) VALUES (%s, %s, 'manual')", (amount, note))
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text(f"✅ ইনকাম যোগ হয়েছে!\n💰 ৳{amount}\n📝 {note}")
    except Exception as e:
        await update.message.reply_text("❌ ভুল ফরম্যাট! উদাহরণ: /income 500 বিকাশে পেমেন্ট")

async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("ফরম্যাট: /customer [email]\nউদাহরণ: /customer example@gmail.com")
        return
    email = context.args[0]
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM orders WHERE customer_email ILIKE %s ORDER BY created_at DESC", (email,))
    orders = cur.fetchall()
    cur.close()
    conn.close()

    if not orders:
        await update.message.reply_text(f"❌ {email} এই email এ কোনো অর্ডার পাওয়া যায়নি।")
        return

    total_spent = sum(float(o['total']) for o in orders)
    text = f"👤 *Customer: {orders[0]['customer_name']}*\n"
    text += f"📧 {email}\n\n"
    for o in orders:
        status_emoji = "✅" if o['status'] == "completed" else "⏳" if o['status'] == "processing" else "❌"
        text += f"{status_emoji} Order #{o['woo_order_id']} — {o['created_at'].strftime('%d %b %Y')}\n"
        text += f"   ৳{o['total']} | {o['status']}\n\n"
    text += f"💰 *মোট খরচ: ৳{total_spent:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def addreseller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("ফরম্যাট: /addreseller [নাম] [ফোন]\nউদাহরণ: /addreseller রাহুল 01712345678")
        return
    name = context.args[0]
    phone = context.args[1]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO resellers (name, phone) VALUES (%s, %s)", (name, phone))
    conn.commit()
    cur.close()
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
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM resellers WHERE phone = %s", (phone,))
        reseller = cur.fetchone()
        if not reseller:
            await update.message.reply_text(f"❌ {phone} এই নম্বরে কোনো রিসেলার নেই।")
            return
        cur.execute("INSERT INTO reseller_orders (reseller_id, product, quantity, price) VALUES (%s, %s, %s, %s)",
                    (reseller['id'], product, quantity, price))
        conn.commit()
        cur.close()
        conn.close()
        total = quantity * price
        await update.message.reply_text(
            f"✅ রিসেলার সেল রেকর্ড হয়েছে!\n👤 {reseller['name']}\n📦 {product} x{quantity}\n💰 ৳{total}"
        )
    except Exception as e:
        await update.message.reply_text("❌ ভুল ফরম্যাট! উদাহরণ: /rsale 01712345678 শার্ট 3 450")

# =================== FLASK WEBHOOK ===================

telegram_app = None

@app.route('/webhook/woocommerce', methods=['POST'])
def woocommerce_webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 400

        order_id = str(data.get('id', 'N/A'))
        customer = data.get('billing', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Unknown"
        customer_email = customer.get('email', '')
        total = float(data.get('total', 0))
        status = data.get('status', 'pending')

        line_items = data.get('line_items', [])
        items_text = ", ".join([f"{item['name']} x{item['quantity']}" for item in line_items])

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (woo_order_id, customer_name, customer_email, total, status, items) VALUES (%s, %s, %s, %s, %s, %s)",
            (order_id, customer_name, customer_email, total, status, items_text)
        )
        cur.execute("INSERT INTO income (amount, note, type) VALUES (%s, %s, 'auto')", (total, f"WooCommerce Order #{order_id}"))
        conn.commit()
        cur.close()
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

        import asyncio
        asyncio.run(send_telegram_message(message))

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
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

def main():
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

    logger.info("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    main()
