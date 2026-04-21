import os
import logging
import pg8000.native
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from datetime import datetime
import asyncio
import nest_asyncio
from urllib.parse import urlparse
from telegram import Bot

nest_asyncio.apply()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RESELLER_BOT_TOKEN = os.environ.get("RESELLER_BOT_TOKEN")
MAIN_BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAIN_CHAT_ID = os.environ.get("CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Conversation states
WAITING_PRODUCT, WAITING_EMAIL, WAITING_TRANSACTION = range(3)

# Product prices
PRODUCTS = {
    "chatgpt": {"name": "ChatGPT Plus (1 Month)", "price": 1200},
    "gemini": {"name": "Gemini Advanced (1 Month)", "price": 1000},
}

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

def get_reseller_by_code(code):
    conn = get_db()
    rows = conn.run("SELECT id, name, phone FROM resellers WHERE reseller_code = :code", code=code.upper())
    conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2]}
    return None

def get_reseller_by_chat_id(chat_id):
    conn = get_db()
    rows = conn.run("SELECT id, name, phone, reseller_code FROM resellers WHERE telegram_chat_id = :chat_id", chat_id=str(chat_id))
    conn.close()
    if rows:
        return {"id": rows[0][0], "name": rows[0][1], "phone": rows[0][2], "code": rows[0][3]}
    return None

def save_reseller_order(reseller_id, reseller_code, product, customer_email, transaction_id, amount):
    conn = get_db()
    rows = conn.run(
        "INSERT INTO reseller_bot_orders (reseller_id, reseller_code, product, customer_email, transaction_id, amount, status) VALUES (:rid, :code, :product, :email, :txn, :amount, 'pending') RETURNING id",
        rid=reseller_id, code=reseller_code, product=product, email=customer_email, txn=transaction_id, amount=amount
    )
    conn.close()
    return rows[0][0] if rows else None

def update_order_status(order_id, status, reject_reason=None):
    conn = get_db()
    if reject_reason:
        conn.run("UPDATE reseller_bot_orders SET status = :status, reject_reason = :reason WHERE id = :id",
                 status=status, reason=reject_reason, id=order_id)
    else:
        conn.run("UPDATE reseller_bot_orders SET status = :status WHERE id = :id", status=status, id=order_id)
    conn.close()

def get_order_by_id(order_id):
    conn = get_db()
    rows = conn.run(
        "SELECT id, reseller_id, reseller_code, product, customer_email, transaction_id, amount, status FROM reseller_bot_orders WHERE id = :id",
        id=order_id
    )
    conn.close()
    if rows:
        r = rows[0]
        return {"id": r[0], "reseller_id": r[1], "reseller_code": r[2], "product": r[3], "customer_email": r[4], "transaction_id": r[5], "amount": str(r[6]), "status": r[7]}
    return None

def get_reseller_chat_id_by_code(code):
    conn = get_db()
    rows = conn.run("SELECT telegram_chat_id FROM resellers WHERE reseller_code = :code", code=code.upper())
    conn.close()
    if rows and rows[0][0]:
        return rows[0][0]
    return None

# =================== TELEGRAM HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    # Check if already registered
    reseller = get_reseller_by_chat_id(chat_id)
    if reseller:
        keyboard = [
            [InlineKeyboardButton("🛒 Notun Order", callback_data="new_order")],
            [InlineKeyboardButton("📋 Amar Orders", callback_data="my_orders")]
        ]
        await update.message.reply_text(
            f"Welcome back {reseller['name']} bhai! 👋\n\nTomar code: `{reseller['code']}`\n\nKi korte chao?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🛍️ *Favourite Deals Reseller Bot*\n\nAssalamualaikum! Ami Favourite Deals er reseller bot.\n\nTomar unique reseller code dao (e.g. RES001):",
        parse_mode="Markdown"
    )
    return WAITING_PRODUCT

async def handle_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    chat_id = update.message.chat_id

    reseller = get_reseller_by_code(code)
    if not reseller:
        await update.message.reply_text("❌ Ei code ta valid na bhai. Sothik code dao:")
        return WAITING_PRODUCT

    # Save chat_id to reseller
    conn = get_db()
    conn.run("UPDATE resellers SET telegram_chat_id = :chat_id WHERE reseller_code = :code",
             chat_id=str(chat_id), code=code)
    conn.close()

    keyboard = [
        [InlineKeyboardButton("🛒 Notun Order", callback_data="new_order")],
        [InlineKeyboardButton("📋 Amar Orders", callback_data="my_orders")]
    ]
    await update.message.reply_text(
        f"✅ Welcome {reseller['name']} bhai!\n\nTomar code: `{code}`\n\nEkhon ki korte chao?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "new_order":
        keyboard = [
            [InlineKeyboardButton("🤖 ChatGPT Plus - ৳1200", callback_data="order_chatgpt")],
            [InlineKeyboardButton("💎 Gemini Advanced - ৳1000", callback_data="order_gemini")],
        ]
        await query.edit_message_text(
            "🛒 *Notun Order*\n\nKon product order korte chao?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data.startswith("order_"):
        product_key = data.replace("order_", "")
        product = PRODUCTS.get(product_key)
        if product:
            context.user_data['product'] = product_key
            context.user_data['product_name'] = product['name']
            context.user_data['amount'] = product['price']
            await query.edit_message_text(
                f"📦 Product: *{product['name']}*\n💵 Price: ৳{product['price']}\n\nJar jonno kincho tar *email address* dao:",
                parse_mode="Markdown"
            )
            context.user_data['state'] = 'waiting_email'

    elif data == "my_orders":
        reseller = get_reseller_by_chat_id(chat_id)
        if not reseller:
            await query.edit_message_text("❌ Tumi registered na bhai.")
            return

        conn = get_db()
        rows = conn.run(
            "SELECT id, product, customer_email, amount, status, created_at FROM reseller_bot_orders WHERE reseller_code = :code ORDER BY created_at DESC LIMIT 10",
            code=reseller['code']
        )
        conn.close()

        if not rows:
            await query.edit_message_text(
                "📋 Ekhono kono order nei bhai.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_home")]])
            )
            return

        text = "📋 *Tomar Orders:*\n\n"
        for r in rows:
            status_emoji = "✅" if r[4] == "approved" else "❌" if r[4] == "rejected" else "⏳"
            text += f"{status_emoji} #{r[0]} — {r[1]}\n"
            text += f"   📧 {r[2]} | ৳{r[3]} | {r[4]}\n\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_home")]]),
            parse_mode="Markdown"
        )

    elif data == "back_home":
        reseller = get_reseller_by_chat_id(chat_id)
        keyboard = [
            [InlineKeyboardButton("🛒 Notun Order", callback_data="new_order")],
            [InlineKeyboardButton("📋 Amar Orders", callback_data="my_orders")]
        ]
        await query.edit_message_text(
            f"Ki korte chao {reseller['name']} bhai?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.message.chat_id
    state = context.user_data.get('state')

    reseller = get_reseller_by_chat_id(chat_id)
    if not reseller:
        await update.message.reply_text("Aage /start diye register koro bhai!")
        return

    if state == 'waiting_email':
        if '@' not in text or '.' not in text:
            await update.message.reply_text("❌ Valid email dao bhai (e.g. example@gmail.com):")
            return
        context.user_data['customer_email'] = text
        context.user_data['state'] = 'waiting_transaction'

        product_name = context.user_data.get('product_name')
        amount = context.user_data.get('amount')

        await update.message.reply_text(
            f"📧 Email: `{text}`\n\n"
            f"Ekhon *Bkash Transaction ID* dao:\n"
            f"(৳{amount} Bkash koro: 01XXXXXXXXX)\n\n"
            f"Transaction complete hole ID ta pathao:",
            parse_mode="Markdown"
        )

    elif state == 'waiting_transaction':
        context.user_data['transaction_id'] = text
        context.user_data['state'] = None

        product_key = context.user_data.get('product')
        product_name = context.user_data.get('product_name')
        customer_email = context.user_data.get('customer_email')
        amount = context.user_data.get('amount')

        # Save order
        order_id = save_reseller_order(
            reseller['id'], reseller['code'],
            product_name, customer_email, text, amount
        )

        if order_id:
            # Notify main bot
            await send_main_bot_notification(order_id, reseller, product_name, customer_email, text, amount)

            await update.message.reply_text(
                f"✅ *Order Submit Hoye Geche!*\n\n"
                f"📋 Order ID: #{order_id}\n"
                f"📦 Product: {product_name}\n"
                f"📧 Email: {customer_email}\n"
                f"💳 Transaction ID: {text}\n"
                f"💵 Amount: ৳{amount}\n\n"
                f"⏳ Admin confirm korle tumi notification pabe!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="back_home")]]),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Order submit e problem hoye geche bhai. Abar try koro.")

async def send_main_bot_notification(order_id, reseller, product_name, customer_email, transaction_id, amount):
    try:
        bot = Bot(token=MAIN_BOT_TOKEN)
        message = (
            f"🔔 *Notun Reseller Order!*\n\n"
            f"👤 Reseller: {reseller['name']} ({reseller['code']})\n"
            f"📦 Product: {product_name}\n"
            f"📧 Customer Email: {customer_email}\n"
            f"💳 Bkash TxnID: {transaction_id}\n"
            f"💵 Amount: ৳{amount}\n"
            f"🆔 Order ID: #{order_id}"
        )
        keyboard = [
            [InlineKeyboardButton("✅ Approve", callback_data=f"rapprove_{order_id}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"rreject_{order_id}")]
        ]
        await bot.send_message(
            chat_id=MAIN_CHAT_ID,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Main bot notification error: {e}")

async def main():
    application = Application.builder().token(RESELLER_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code_input)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Reseller bot started!")
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
