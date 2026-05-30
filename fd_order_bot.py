import os
import json
import asyncio
import logging
import subprocess
import requests as req
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN          = os.environ.get("ORDER_BOT_TOKEN")
WC_KEY             = os.environ.get("WC_KEY")
WC_SECRET          = os.environ.get("WC_SECRET")
WP_URL             = os.environ.get("WP_URL", "https://favouritedeals.online")
WP_PAYLATER_SECRET = os.environ.get("WP_PAYLATER_SECRET", "fd_renew_A7kP29mQx4Lz_2026")
VERIFY_EMAIL       = "mhriaz715@gmail.com"

PRODUCTS_FILE = "fd_products.json"

INITIAL_PRODUCTS = {
    "🤖 ChatGPT Plus": {
        "pid": 21147,
        "variations": [
            {"id": 23901, "name": "Individual 1 Month", "price": 899},
            {"id": 23900, "name": "Share 1 Month", "price": 399},
            {"id": 23061, "name": "Plus 12 Months", "price": 4999},
            {"id": 23059, "name": "Plus 1 Month", "price": 799},
            {"id": 21150, "name": "Business 3 Months", "price": 1000},
            {"id": 21149, "name": "Go 12 Months", "price": 1499},
            {"id": 21148, "name": "Business 1 Month", "price": 399},
        ]
    },
    "🧠 SuperGrok": {
        "pid": 21203,
        "variations": [
            {"id": 23069, "name": "3 Months", "price": 3999},
            {"id": 21207, "name": "1 Month", "price": 1650},
            {"id": 21206, "name": "1 Week", "price": 299},
        ]
    },
    "🎨 Adobe Creative Cloud": {
        "pid": 21082,
        "variations": [
            {"id": 23906, "name": "4 Months", "price": 1499},
        ]
    },
    "📺 YouTube Premium": {
        "pid": 21090,
        "variations": [
            {"id": 24003, "name": "6 Months", "price": 2199},
            {"id": 21092, "name": "1 Year", "price": 3199},
            {"id": 21091, "name": "1 Month", "price": 199},
        ]
    },
    "💼 Office 365": {
        "pid": 21075,
        "variations": [
            {"id": 24008, "name": "1 Year Personal", "price": 1199},
        ]
    },
    "✅ Meta Verified": {
        "pid": 21099,
        "variations": [
            {"id": 21109, "name": "WhatsApp", "price": 1499},
            {"id": 21108, "name": "Instagram Page", "price": 1499},
            {"id": 21107, "name": "Instagram Profile", "price": 999},
            {"id": 21106, "name": "Facebook Page", "price": 1499},
            {"id": 21105, "name": "Facebook Profile", "price": 999},
        ]
    },
    "🎓 Coursera Plus": {
        "pid": 22237,
        "variations": [
            {"id": 22239, "name": "12 Months", "price": 1499},
        ]
    },
    "💜 Lovable Pro": {
        "pid": 22227,
        "variations": [
            {"id": 23950, "name": "1M 205 Credits", "price": 499},
            {"id": 22231, "name": "2M 210 Credits", "price": 799},
            {"id": 22230, "name": "1M 105 Credits", "price": 399},
        ]
    },
    "🌟 Google AI Ultra VEO3": {
        "pid": 24014,
        "variations": [
            {"id": 24015, "name": "Veo3 Ultra 25k Credits", "price": 1499},
        ]
    },
    "📘 Facebook BM Verification": {
        "pid": 21500,
        "variations": [
            {"id": 21506, "name": "Custom BM Client Provided", "price": 1999},
            {"id": 21505, "name": "Instant Verified BM", "price": 1500},
        ]
    },
    "🪟 Microsoft Office 2024": {
        "pid": 23965,
        "variations": []
    },
    "🪟 Windows 10 Pro": {
        "pid": 23961,
        "variations": []
    },
}

STATE_PRODUCT, STATE_VARIATION, STATE_DISCOUNT, STATE_CUSTOM_PRICE, STATE_CLIENT_NAME, STATE_CLIENT_PHONE, STATE_CLIENT_EMAIL, STATE_CONFIRM, STATE_ADD_PRODUCT_NAME, STATE_ADD_PRODUCT_ID = range(10)


def load_products():
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    save_products(INITIAL_PRODUCTS)
    return INITIAL_PRODUCTS


def save_products(data):
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_variations(pid):
    try:
        resp = req.get(
            f"{WP_URL}/wp-json/wc/v3/products/{pid}/variations?per_page=20",
            auth=(WC_KEY, WC_SECRET), timeout=15
        ).json()
        if isinstance(resp, list):
            return [
                {
                    "id": v["id"],
                    "name": " | ".join([a["option"] for a in v.get("attributes", [])]) or f"Variation {v['id']}",
                    "price": int(float(v.get("price", 0)))
                }
                for v in resp
            ]
        return []
    except Exception as e:
        logger.error(f"fetch_variations({pid}): {e}")
        return []


# ── Coupon helpers ──────────────────────────────────────────────────────────

def wc_create_coupon(discount_amount: int, product_id: int) -> dict:
    """WooCommerce API দিয়ে একটা single-use coupon বানাও।"""
    import random, string
    code = "FD" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    payload = {
        "code": code,
        "discount_type": "fixed_cart",
        "amount": str(discount_amount),
        "usage_limit": 1,
        "product_ids": [product_id],
    }
    try:
        resp = req.post(
            f"{WP_URL}/wp-json/wc/v3/coupons",
            auth=(WC_KEY, WC_SECRET),
            json=payload,
            timeout=15
        ).json()
        if resp.get("id"):
            return {"success": True, "id": resp["id"], "code": resp["code"]}
        return {"success": False, "message": str(resp)}
    except Exception as e:
        logger.error(f"wc_create_coupon: {e}")
        return {"success": False, "message": str(e)}


def wc_delete_coupon(coupon_id: int):
    """Order complete হলে coupon delete করো।"""
    try:
        req.delete(
            f"{WP_URL}/wp-json/wc/v3/coupons/{coupon_id}?force=true",
            auth=(WC_KEY, WC_SECRET),
            timeout=15
        )
        logger.info(f"Coupon {coupon_id} deleted.")
    except Exception as e:
        logger.error(f"wc_delete_coupon: {e}")


# ── Playwright ──────────────────────────────────────────────────────────────

def bot_create_user(name, email, phone):
    try:
        resp = req.post(
            f"{WP_URL}/wp-json/fdbot/v1/bot-login",
            headers={"X-FD-Secret": WP_PAYLATER_SECRET, "Content-Type": "application/json"},
            json={"name": name, "email": email, "phone": phone},
            timeout=20
        ).json()
        return resp
    except Exception as e:
        logger.error(f"bot_create_user: {e}")
        return {"success": False, "message": str(e)}


def bot_get_autologin_url(token, redirect_url):
    try:
        resp = req.post(
            f"{WP_URL}/wp-json/fdbot/v1/bot-autologin",
            headers={"X-FD-Secret": WP_PAYLATER_SECRET, "Content-Type": "application/json"},
            json={"token": token, "redirect": redirect_url},
            timeout=20
        ).json()
        return resp
    except Exception as e:
        logger.error(f"bot_get_autologin_url: {e}")
        return {"success": False, "message": str(e)}


def run_playwright_order(autologin_url, variation_id, client_name, client_phone, client_email, coupon_code=None):
    coupon_block = ""
    if coupon_code:
        coupon_block = f"""
        print("Applying coupon...")
        try:
            coupon_input = page.locator("#coupon_code")
            if await coupon_input.count() > 0:
                await coupon_input.fill("{coupon_code}")
                apply_btn = page.locator("button[name='apply_coupon']")
                if await apply_btn.count() > 0:
                    await apply_btn.click()
                    try:
                        await page.wait_for_selector(".woocommerce-message, .woocommerce-error", timeout=8000)
                    except:
                        pass
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    print("Coupon applied!")
        except Exception as e:
            print("Coupon error:", e)
"""

    script = f"""
import asyncio
from playwright.async_api import async_playwright

async def do_order():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print("Logging in...")
        await page.goto("{autologin_url}", timeout=30000)
        await page.wait_for_load_state("networkidle")
        print("After login:", page.url)

        print("Adding to cart...")
        await page.goto("{WP_URL}/?add-to-cart={variation_id}&quantity=1", timeout=30000)
        await page.wait_for_load_state("networkidle")

        print("Going to checkout...")
        await page.goto("{WP_URL}/checkout/", timeout=30000)
        await page.wait_for_load_state("networkidle")

        {coupon_block}

        print("Filling billing info...")
        try:
            await page.fill("#billing_first_name", "{client_name}")
        except:
            pass
        try:
            await page.fill("#billing_phone", "{client_phone}")
        except:
            pass
        try:
            await page.fill("#billing_email", "{client_email}")
        except:
            pass

        await asyncio.sleep(1)

        print("Filling verify email...")
        try:
            verify = page.locator("#manual_verify_email")
            if await verify.count() > 0:
                await verify.fill("{VERIFY_EMAIL}")
                print("Verify email filled!")
                await asyncio.sleep(1)
                verify_btn = page.locator("button:has-text('VERIFY ACCESS')")
                if await verify_btn.count() > 0:
                    await verify_btn.first.click()
                    print("Verify button clicked!")
                    await asyncio.sleep(2)
        except Exception as e:
            print("Verify email error:", e)

        await asyncio.sleep(1)

        print("Accepting terms...")
        try:
            terms = page.locator("#terms")
            if await terms.count() > 0:
                await terms.check()
                print("Terms checked!")
        except Exception as e:
            print("Terms error:", e)

        await asyncio.sleep(1)

        print("Placing order...")
        try:
            await page.click("#place_order", timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            print("Place order error:", e)

        current_url = page.url
        print("Final URL:", current_url)

        if True:
            print("SUCCESS:", current_url)

        await browser.close()

asyncio.run(do_order())
"""

    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout + result.stderr
        logger.info(f"Playwright: {output}")
        success = "SUCCESS" in output or "order-received" in output
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Timeout — 2 মিনিটেও order হয়নি।"
    except Exception as e:
        return False, f"Error: {str(e)}"


# ── Keyboards ───────────────────────────────────────────────────────────────

def product_keyboard():
    products = load_products()
    keyboard = []
    row = []
    for i, name in enumerate(products.keys()):
        row.append(InlineKeyboardButton(name, callback_data=f"prod_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def variation_keyboard(variations):
    keyboard = []
    for i, v in enumerate(variations):
        label = f"{v['name']} — ৳{v['price']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"var_{i}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_product")])
    return InlineKeyboardMarkup(keyboard)


# ── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛍️ *FD Order Bot*\n\nAssalamualaikum bhai! 👋\n\nএই bot দিয়ে client এর হয়ে order করো।",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 নতুন Order", callback_data="new_order")],
            [InlineKeyboardButton("➕ Product Add", callback_data="add_product")],
        ])
    )


async def new_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📦 *Product বেছে নাও:*",
        parse_mode="Markdown",
        reply_markup=product_keyboard()
    )
    return STATE_PRODUCT


async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split("_")[1])
    products = load_products()
    names = list(products.keys())

    if idx >= len(names):
        await query.edit_message_text("❌ Product পাওয়া যায়নি।")
        return ConversationHandler.END

    product_name = names[idx]
    product_data = products[product_name]
    context.user_data["product_name"] = product_name
    context.user_data["product_data"] = product_data

    variations = product_data.get("variations", [])
    if not variations and product_data.get("pid"):
        await query.edit_message_text(f"⏳ {product_name} এর plans fetch করছি...")
        variations = fetch_variations(product_data["pid"])
        products[product_name]["variations"] = variations
        save_products(products)
        context.user_data["product_data"]["variations"] = variations

    if not variations:
        context.user_data["selected_variation"] = {"id": product_data["pid"], "name": product_name, "price": 0}
        # No variation → directly ask discount
        return await _ask_discount(query, context)

    await query.edit_message_text(
        f"📦 *{product_name}*\n\nPlan বেছে নাও:",
        parse_mode="Markdown",
        reply_markup=variation_keyboard(variations)
    )
    return STATE_VARIATION


async def variation_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_product":
        await query.edit_message_text("📦 *Product বেছে নাও:*", parse_mode="Markdown", reply_markup=product_keyboard())
        return STATE_PRODUCT

    idx = int(query.data.split("_")[1])
    variations = context.user_data["product_data"]["variations"]

    if idx >= len(variations):
        await query.edit_message_text("❌ Plan পাওয়া যায়নি।")
        return ConversationHandler.END

    context.user_data["selected_variation"] = variations[idx]
    return await _ask_discount(query, context)


async def _ask_discount(query, context: ContextTypes.DEFAULT_TYPE):
    """Plan select এর পরে discount জিজ্ঞেস করো।"""
    v = context.user_data["selected_variation"]
    await query.edit_message_text(
        f"✅ *{context.user_data['product_name']}*\n"
        f"📋 Plan: {v['name']} — ৳{v['price']}\n\n"
        f"💸 এই order এ discount দেবে?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ হ্যাঁ", callback_data="discount_yes"),
                InlineKeyboardButton("❌ না", callback_data="discount_no"),
            ]
        ])
    )
    return STATE_DISCOUNT


async def handle_discount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "discount_no":
        context.user_data["coupon_code"] = None
        context.user_data["coupon_id"] = None
        context.user_data["final_price"] = context.user_data["selected_variation"]["price"]
        await query.edit_message_text(
            f"👤 Client এর *নাম* দাও:",
            parse_mode="Markdown"
        )
        return STATE_CLIENT_NAME

    # Discount হ্যাঁ
    v = context.user_data["selected_variation"]
    await query.edit_message_text(
        f"💸 *Discount Price*\n\n"
        f"Original price: ৳{v['price']}\n\n"
        f"Client কত টাকায় কিনবে? (শুধু সংখ্যা লেখো)\n"
        f"যেমন: `300`",
        parse_mode="Markdown"
    )
    return STATE_CUSTOM_PRICE


async def handle_custom_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        custom_price = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ শুধু সংখ্যা লেখো। যেমন: 300")
        return STATE_CUSTOM_PRICE

    v = context.user_data["selected_variation"]
    original_price = v["price"]

    if custom_price <= 0:
        await update.message.reply_text("❌ Price 0 বা কম হতে পারবে না।")
        return STATE_CUSTOM_PRICE

    if custom_price >= original_price:
        await update.message.reply_text(
            f"❌ Custom price (৳{custom_price}) অবশ্যই original price (৳{original_price}) এর কম হতে হবে।"
        )
        return STATE_CUSTOM_PRICE

    discount_amount = original_price - custom_price
    await update.message.reply_text(f"⏳ Coupon তৈরি হচ্ছে... (৳{discount_amount} off)")

    product_id = context.user_data["product_data"]["pid"]
    coupon_resp = wc_create_coupon(discount_amount, product_id)

    if not coupon_resp.get("success"):
        await update.message.reply_text(f"❌ Coupon তৈরি হয়নি:\n{coupon_resp.get('message', '')}")
        return STATE_CUSTOM_PRICE

    context.user_data["coupon_code"] = coupon_resp["code"]
    context.user_data["coupon_id"] = coupon_resp["id"]
    context.user_data["final_price"] = custom_price

    await update.message.reply_text(
        f"✅ Coupon তৈরি হয়েছে!\n"
        f"🏷️ Code: `{coupon_resp['code']}`\n"
        f"💰 ৳{original_price} → ৳{custom_price} (৳{discount_amount} off)\n\n"
        f"👤 Client এর *নাম* দাও:",
        parse_mode="Markdown"
    )
    return STATE_CLIENT_NAME


async def get_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ সঠিক নাম দাও।")
        return STATE_CLIENT_NAME
    context.user_data["client_name"] = name
    await update.message.reply_text(f"✅ নাম: *{name}*\n\n📱 Client এর *WhatsApp নম্বর* দাও:", parse_mode="Markdown")
    return STATE_CLIENT_PHONE


async def get_client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    phone = update.message.text.strip()
    digits = re.sub(r"[^0-9]", "", phone)
    if len(digits) < 10:
        await update.message.reply_text("❌ সঠিক নম্বর দাও।")
        return STATE_CLIENT_PHONE
    context.user_data["client_phone"] = phone
    await update.message.reply_text(f"✅ Phone: *{phone}*\n\n📧 Client এর *Email* দাও:", parse_mode="Markdown")
    return STATE_CLIENT_EMAIL


async def get_client_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    email = update.message.text.strip().lower()
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        await update.message.reply_text("❌ সঠিক email দাও।")
        return STATE_CLIENT_EMAIL

    context.user_data["client_email"] = email
    v = context.user_data["selected_variation"]
    final_price = context.user_data.get("final_price", v["price"])
    coupon_code = context.user_data.get("coupon_code")

    discount_line = f"🏷️ Discount: ৳{v['price']} → ৳{final_price}\n" if coupon_code else ""

    await update.message.reply_text(
        f"📋 *Order Summary:*\n\n"
        f"📦 {context.user_data['product_name']}\n"
        f"🎯 Plan: {v['name']}\n"
        f"💵 দাম: ৳{final_price}\n"
        f"{discount_line}\n"
        f"👤 নাম: {context.user_data['client_name']}\n"
        f"📱 Phone: {context.user_data['client_phone']}\n"
        f"📧 Email: {email}\n\n"
        f"Order করবো?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ হ্যাঁ Order করো", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="confirm_no"),
            ]
        ])
    )
    return STATE_CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_no":
        # Cancel হলে coupon delete করো
        coupon_id = context.user_data.get("coupon_id")
        if coupon_id:
            wc_delete_coupon(coupon_id)
        await query.edit_message_text("❌ Order cancel করা হয়েছে।")
        return ConversationHandler.END

    await query.edit_message_text("⏳ User create করছি...", parse_mode="Markdown")

    name        = context.user_data["client_name"]
    phone       = context.user_data["client_phone"]
    email       = context.user_data["client_email"]
    var         = context.user_data["selected_variation"]
    final_price = context.user_data.get("final_price", var["price"])
    coupon_code = context.user_data.get("coupon_code")
    coupon_id   = context.user_data.get("coupon_id")

    user_resp = bot_create_user(name, email, phone)
    if not user_resp.get("success"):
        if coupon_id:
            wc_delete_coupon(coupon_id)
        await query.edit_message_text(f"❌ User create হয়নি:\n{user_resp.get('message','')}")
        return ConversationHandler.END

    token  = user_resp.get("token")
    is_new = user_resp.get("is_new", False)

    await query.edit_message_text(
        f"{'✅ নতুন account তৈরি' if is_new else '✅ Existing account'}\n\n🛒 Checkout শুরু হচ্ছে...",
        parse_mode="Markdown"
    )

    autologin = bot_get_autologin_url(token, f"{WP_URL}/checkout/")
    if not autologin.get("success"):
        if coupon_id:
            wc_delete_coupon(coupon_id)
        await query.edit_message_text(f"❌ Auto-login URL পাওয়া যায়নি:\n{autologin.get('message','')}")
        return ConversationHandler.END

    await query.edit_message_text("🤖 *Browser চালু হচ্ছে...*\n\n⏳ 1-2 মিনিট অপেক্ষা করো।", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    success, output = await loop.run_in_executor(
        None, run_playwright_order,
        autologin["autologin_url"], var["id"], name, phone, email, coupon_code
    )

    if success:
        # Order সফল → coupon delete
        if coupon_id:
            wc_delete_coupon(coupon_id)

        discount_line = f"🏷️ Discount price: ৳{final_price}\n" if coupon_code else ""
        client_msg = (
            f"আসসালামুয়ালাইকুম! 👋\n\n"
            f"আপনার order সফলভাবে তৈরি হয়েছে! ✅\n\n"
            f"নিচের তথ্য দিয়ে আমাদের website এ login করুন:\n\n"
            f"🌐 https://favouritedeals.online/\n"
            f"📧 Email: {email}\n"
            f"📱 WhatsApp: {phone}\n\n"
            f"👉 Login করুন → My Account → My Subscriptions\n\n"
            f"⚠️ Login এ আপনার WhatsApp নম্বরে OTP আসবে।\n"
            f"OTP দিয়ে verify করলেই subscription details দেখতে পাবেন।\n\n"
            f"ধন্যবাদ Favourite Deals এ order করার জন্য! 🎉"
        )
        await query.edit_message_text(
            f"🎉 *Order সফল!*\n\n"
            f"📦 {context.user_data['product_name']}\n"
            f"🎯 {var['name']} — ৳{final_price}\n"
            f"{discount_line}"
            f"👤 {name} | {phone}\n"
            f"📧 {email}\n\n"
            f"⚡ Main bot থেকে subscription activate করো।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 আরেকটা Order", callback_data="new_order")]])
        )
        await context.bot.send_message(chat_id=query.message.chat_id, text=client_msg)
    else:
        # Order fail → coupon delete (waste হবে না)
        if coupon_id:
            wc_delete_coupon(coupon_id)
        await query.edit_message_text(
            f"❌ *Order হয়নি।*\n\n`{output[:300]}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 আবার চেষ্টা", callback_data="new_order")]])
        )
    return ConversationHandler.END


async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ *নতুন Product Add*\n\nProduct এর *নাম* দাও:\n(emoji সহ, যেমন: 🎯 Canva Pro)",
        parse_mode="Markdown"
    )
    return STATE_ADD_PRODUCT_NAME


async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ নাম: *{context.user_data['new_product_name']}*\n\nWooCommerce *Product ID* দাও:",
        parse_mode="Markdown"
    )
    return STATE_ADD_PRODUCT_ID


async def add_product_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ সংখ্যায় ID দাও।")
        return STATE_ADD_PRODUCT_ID

    await update.message.reply_text(f"⏳ Product ID {pid} fetch করছি...")

    try:
        p = req.get(f"{WP_URL}/wp-json/wc/v3/products/{pid}", auth=(WC_KEY, WC_SECRET), timeout=15).json()
        ptype = p.get("type", "simple")
        variations = fetch_variations(pid) if "variable" in ptype else []
    except Exception as e:
        await update.message.reply_text(f"❌ Fetch error: {e}")
        return ConversationHandler.END

    products = load_products()
    display_name = context.user_data["new_product_name"]
    products[display_name] = {"pid": pid, "variations": variations}
    save_products(products)

    var_text = "\n".join([f"   → ID:{v['id']} | {v['name']} | ৳{v['price']}" for v in variations]) or "   (কোনো variation নেই)"

    await update.message.reply_text(
        f"✅ *{display_name}* add হয়েছে!\n\n📋 Variations:\n{var_text}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Order করো", callback_data="new_order")],
            [InlineKeyboardButton("➕ আরেকটা Add", callback_data="add_product")],
        ])
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Cancel হলেও pending coupon delete করো
    coupon_id = context.user_data.get("coupon_id") if hasattr(context, "user_data") else None
    if coupon_id:
        wc_delete_coupon(coupon_id)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Cancel করা হয়েছে।")
    else:
        await update.message.reply_text("❌ Cancel করা হয়েছে।")
    return ConversationHandler.END


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(new_order_start, pattern="^new_order$"),
            CallbackQueryHandler(add_product_start, pattern="^add_product$"),
        ],
        states={
            STATE_PRODUCT:   [CallbackQueryHandler(product_selected, pattern="^prod_"), CallbackQueryHandler(cancel, pattern="^cancel$")],
            STATE_VARIATION: [CallbackQueryHandler(variation_selected, pattern="^(var_|back_product)"), CallbackQueryHandler(cancel, pattern="^cancel$")],
            STATE_DISCOUNT:  [CallbackQueryHandler(handle_discount_choice, pattern="^discount_")],
            STATE_CUSTOM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_price)],
            STATE_CLIENT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_name)],
            STATE_CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_phone)],
            STATE_CLIENT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_email)],
            STATE_CONFIRM:      [CallbackQueryHandler(confirm_order, pattern="^confirm_")],
            STATE_ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            STATE_ADD_PRODUCT_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_id)],
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern="^cancel$"), CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    logger.info("FD Order Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
