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

(
    STATE_PRODUCT,
    STATE_VARIATION,
    STATE_DISCOUNT,
    STATE_CUSTOM_PRICE,
    STATE_CLIENT_NAME,
    STATE_CLIENT_PHONE,
    STATE_CLIENT_EMAIL,
    STATE_CONFIRM,
) = range(8)


def wc_get(endpoint, params=None):
    try:
        return req.get(f"{WP_URL}/wp-json/wc/v3/{endpoint}", auth=(WC_KEY, WC_SECRET), params=params or {}, timeout=15).json()
    except Exception as e:
        logger.error(f"WC GET {endpoint}: {e}")
        return None


def wc_post(endpoint, data):
    try:
        return req.post(f"{WP_URL}/wp-json/wc/v3/{endpoint}", auth=(WC_KEY, WC_SECRET), json=data, timeout=15).json()
    except Exception as e:
        logger.error(f"WC POST {endpoint}: {e}")
        return None


def wc_delete(endpoint):
    try:
        return req.delete(f"{WP_URL}/wp-json/wc/v3/{endpoint}", auth=(WC_KEY, WC_SECRET), params={"force": True}, timeout=15).json()
    except Exception as e:
        logger.error(f"WC DELETE {endpoint}: {e}")
        return None


def fetch_live_products():
    try:
        products = wc_get("products", {"status": "publish", "stock_status": "instock", "per_page": 50, "orderby": "menu_order", "order": "asc"})
        if not isinstance(products, list):
            return []
        result = []
        for p in products:
            if p.get("catalog_visibility") == "hidden":
                continue
            result.append({"pid": p.get("id"), "name": p.get("name", ""), "type": p.get("type", "simple"), "price": p.get("price", "0")})
        return result
    except Exception as e:
        logger.error(f"fetch_live_products: {e}")
        return []


def fetch_variations(pid):
    try:
        resp = wc_get(f"products/{pid}/variations", {"per_page": 20})
        if isinstance(resp, list):
            return [{"id": v["id"], "name": " | ".join([a["option"] for a in v.get("attributes", [])]) or f"Plan {v['id']}", "price": v.get("price", "0")} for v in resp]
        return []
    except Exception as e:
        logger.error(f"fetch_variations({pid}): {e}")
        return []


def create_discount_coupon(original_price, custom_price):
    try:
        discount = float(original_price) - float(custom_price)
        if discount <= 0:
            return None
        import random, string
        code = "FD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        coupon = wc_post("coupons", {"code": code, "discount_type": "fixed_cart", "amount": str(round(discount, 2)), "usage_limit": 1, "individual_use": True, "free_shipping": False})
        if coupon and coupon.get("id"):
            return {"id": coupon["id"], "code": code, "discount": discount}
        return None
    except Exception as e:
        logger.error(f"create_discount_coupon: {e}")
        return None


def delete_coupon(coupon_id):
    try:
        wc_delete(f"coupons/{coupon_id}")
    except Exception as e:
        logger.error(f"delete_coupon: {e}")


def bot_create_user(name, email, phone):
    try:
        return req.post(f"{WP_URL}/wp-json/fdbot/v1/bot-login", headers={"X-FD-Secret": WP_PAYLATER_SECRET, "Content-Type": "application/json"}, json={"name": name, "email": email, "phone": phone}, timeout=20).json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def bot_get_autologin_url(token, redirect_url):
    try:
        return req.post(f"{WP_URL}/wp-json/fdbot/v1/bot-autologin", headers={"X-FD-Secret": WP_PAYLATER_SECRET, "Content-Type": "application/json"}, json={"token": token, "redirect": redirect_url}, timeout=20).json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def run_playwright_order(autologin_url, variation_id, client_name, client_phone, client_email, coupon_code=None):
    wp_url = WP_URL
    verify = VERIFY_EMAIL
    cart_url = f"{wp_url}/?add-to-cart={variation_id}&quantity=1"
    checkout_url = f"{wp_url}/checkout/"

    coupon_part = ""
    if coupon_code:
        coupon_part = f"""
        try:
            ci = page.locator('#coupon_code')
            if await ci.count() > 0:
                await ci.fill('{coupon_code}')
                ab = page.locator("button[name='apply_coupon']")
                if await ab.count() > 0:
                    await ab.first.click()
                    await asyncio.sleep(2)
                    print('Coupon applied!')
        except Exception as ce:
            print('Coupon error:', ce)
"""

    script = (
        "import asyncio\n"
        "from playwright.async_api import async_playwright\n"
        "async def do_order():\n"
        "    async with async_playwright() as p:\n"
        "        browser = await p.chromium.launch(headless=True)\n"
        "        ctx = await browser.new_context()\n"
        "        page = await ctx.new_page()\n"
        f"        print('Step 1: Login...')\n"
        f"        await page.goto('{autologin_url}', timeout=60000, wait_until='networkidle')\n"
        "        await asyncio.sleep(2)\n"
        "        print('After login:', page.url)\n"
        f"        print('Step 2: Cart...')\n"
        f"        await page.goto('{cart_url}', timeout=60000, wait_until='networkidle')\n"
        "        await asyncio.sleep(2)\n"
        "        print('After cart:', page.url)\n"
        f"        print('Step 3: Checkout...')\n"
        f"        await page.goto('{checkout_url}', timeout=60000, wait_until='networkidle')\n"
        "        await asyncio.sleep(3)\n"
        + (coupon_part if coupon_code else "")
        + f"        print('Step 4: Billing...')\n"
        f"        for sel, val in [('#billing_first_name', '{client_name}'), ('#billing_phone', '{client_phone}'), ('#billing_email', '{client_email}')]:\n"
        "            try:\n"
        "                el = page.locator(sel)\n"
        "                if await el.count() > 0:\n"
        "                    await el.fill(val)\n"
        "            except: pass\n"
        "        await asyncio.sleep(1)\n"
        f"        print('Step 5: Verify...')\n"
        "        try:\n"
        "            v = page.locator('#manual_verify_email')\n"
        "            if await v.count() > 0:\n"
        f"                await v.fill('{verify}')\n"
        "                btn = page.locator(\"button:has-text('VERIFY ACCESS')\")\n"
        "                if await btn.count() > 0:\n"
        "                    await btn.first.click()\n"
        "                    await asyncio.sleep(3)\n"
        "                    print('Verified!')\n"
        "        except Exception as ve:\n"
        "            print('Verify error:', ve)\n"
        "        await asyncio.sleep(1)\n"
        "        print('Step 6: Terms...')\n"
        "        try:\n"
        "            t = page.locator('#terms')\n"
        "            if await t.count() > 0:\n"
        "                await t.check()\n"
        "                print('Terms checked!')\n"
        "        except: pass\n"
        "        await asyncio.sleep(1)\n"
        "        print('Step 7: Place order...')\n"
        "        try:\n"
        "            await page.click('#place_order', timeout=10000)\n"
        "            await asyncio.sleep(5)\n"
        "            try:\n"
        "                await page.wait_for_url('**/order-received/**', timeout=20000)\n"
        "            except: pass\n"
        "        except Exception as oe:\n"
        "            print('Place order error:', oe)\n"
        "        url = page.url\n"
        "        print('Final URL:', url)\n"
        "        if 'order-received' in url:\n"
        "            print('SUCCESS!')\n"
        "        else:\n"
        "            print('DONE:', url)\n"
        "        await browser.close()\n"
        "asyncio.run(do_order())\n"
    )

    try:
        result = subprocess.run(["python3", "-c", script], capture_output=True, text=True, timeout=180)
        output = result.stdout + result.stderr
        logger.info(f"Playwright: {output}")
        success = "SUCCESS!" in output or "order-received" in output
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def product_keyboard(products):
    keyboard = []
    row = []
    for i, p in enumerate(products):
        name = p["name"][:25] + "..." if len(p["name"]) > 25 else p["name"]
        row.append(InlineKeyboardButton(name, callback_data=f"prod_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def variation_keyboard(variations):
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{v['name']} — ৳{v['price']}", callback_data=f"var_{i}")] for i, v in enumerate(variations)] + [[InlineKeyboardButton("🔙 Back", callback_data="back_product")]])


def order_summary(context):
    var = context.user_data["selected_variation"]
    product = context.user_data["selected_product"]
    name = context.user_data.get("client_name", "")
    phone = context.user_data.get("client_phone", "")
    email = context.user_data.get("client_email", "")
    final_price = context.user_data.get("final_price", var["price"])
    return (f"📋 *Order Summary:*\n\n📦 {product['name'][:40]}\n🎯 Plan: {var['name']}\n💵 Price: ৳{final_price}\n\n👤 {name}\n📱 {phone}\n📧 {email}\n\nOrder করবো?")


def confirm_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ হ্যাঁ Order করো", callback_data="confirm_yes"), InlineKeyboardButton("❌ Cancel", callback_data="confirm_no")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛍️ *FD Order Bot*\n\nAssalamualaikum bhai! 👋", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 নতুন Order", callback_data="new_order")]]))


async def new_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Products fetch করছি...")
    products = fetch_live_products()
    if not products:
        await query.edit_message_text("❌ Products fetch হয়নি।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 আবার চেষ্টা", callback_data="new_order")]]))
        return ConversationHandler.END
    context.user_data["products"] = products
    await query.edit_message_text(f"📦 *Product বেছে নাও:* ({len(products)}টা)", parse_mode="Markdown", reply_markup=product_keyboard(products))
    return STATE_PRODUCT


async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ Cancel করা হয়েছে।")
        return ConversationHandler.END
    idx = int(query.data.split("_")[1])
    products = context.user_data.get("products", [])
    if idx >= len(products):
        await query.edit_message_text("❌ Product পাওয়া যায়নি।")
        return ConversationHandler.END
    product = products[idx]
    context.user_data["selected_product"] = product
    if product["type"] in ["variable", "variable-subscription"]:
        await query.edit_message_text(f"⏳ Plans fetch করছি...")
        variations = fetch_variations(product["pid"])
        if not variations:
            await query.edit_message_text("❌ Plans পাওয়া যায়নি।")
            return ConversationHandler.END
        context.user_data["variations"] = variations
        await query.edit_message_text(f"📦 *{product['name'][:40]}*\n\nPlan বেছে নাও:", parse_mode="Markdown", reply_markup=variation_keyboard(variations))
        return STATE_VARIATION
    else:
        context.user_data["selected_variation"] = {"id": product["pid"], "name": "Standard", "price": product["price"]}
        await query.edit_message_text(f"📦 *{product['name'][:40]}*\n💵 ৳{product['price']}\n\nDiscount দেবে?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ হ্যাঁ", callback_data="discount_yes"), InlineKeyboardButton("❌ না", callback_data="discount_no")]]))
        return STATE_DISCOUNT


async def variation_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_product":
        products = context.user_data.get("products", [])
        await query.edit_message_text("📦 *Product বেছে নাও:*", parse_mode="Markdown", reply_markup=product_keyboard(products))
        return STATE_PRODUCT
    idx = int(query.data.split("_")[1])
    variations = context.user_data.get("variations", [])
    if idx >= len(variations):
        await query.edit_message_text("❌ Plan পাওয়া যায়নি।")
        return ConversationHandler.END
    selected = variations[idx]
    context.user_data["selected_variation"] = selected
    product = context.user_data["selected_product"]
    await query.edit_message_text(f"✅ *{product['name'][:40]}*\n📋 Plan: {selected['name']}\n💵 ৳{selected['price']}\n\nDiscount দেবে?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ হ্যাঁ", callback_data="discount_yes"), InlineKeyboardButton("❌ না", callback_data="discount_no")]]))
    return STATE_DISCOUNT


async def discount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    var = context.user_data["selected_variation"]
    if query.data == "discount_no":
        context.user_data["final_price"] = var["price"]
        await query.edit_message_text("👤 Client এর *নাম* দাও:", parse_mode="Markdown")
        return STATE_CLIENT_NAME
    await query.edit_message_text(f"💵 Original: *৳{var['price']}*\n\nনতুন price লেখো:\nযেমন: `300`", parse_mode="Markdown")
    return STATE_CUSTOM_PRICE


async def get_custom_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        custom_price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ শুধু সংখ্যা দাও। যেমন: 300")
        return STATE_CUSTOM_PRICE
    var = context.user_data["selected_variation"]
    original_price = float(var["price"])
    if custom_price >= original_price:
        await update.message.reply_text(f"❌ Original price (৳{int(original_price)}) এর কম দাও।")
        return STATE_CUSTOM_PRICE
    if custom_price <= 0:
        await update.message.reply_text("❌ 0 এর বেশি দাও।")
        return STATE_CUSTOM_PRICE
    context.user_data["final_price"] = custom_price
    context.user_data["discount_amount"] = original_price - custom_price
    await update.message.reply_text(f"✅ Custom: *৳{int(custom_price)}* (Discount: ৳{int(original_price - custom_price)})\n\n👤 Client এর *নাম* দাও:", parse_mode="Markdown")
    return STATE_CLIENT_NAME


async def get_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ সঠিক নাম দাও।")
        return STATE_CLIENT_NAME
    context.user_data["client_name"] = name
    await update.message.reply_text(f"✅ নাম: *{name}*\n\n📱 WhatsApp নম্বর দাও:", parse_mode="Markdown")
    return STATE_CLIENT_PHONE


async def get_client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    phone = update.message.text.strip()
    if len(re.sub(r"[^0-9]", "", phone)) < 10:
        await update.message.reply_text("❌ সঠিক নম্বর দাও।")
        return STATE_CLIENT_PHONE
    context.user_data["client_phone"] = phone
    await update.message.reply_text(f"✅ Phone: *{phone}*\n\n📧 Email দাও:", parse_mode="Markdown")
    return STATE_CLIENT_EMAIL


async def get_client_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    email = update.message.text.strip().lower()
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        await update.message.reply_text("❌ সঠিক email দাও।")
        return STATE_CLIENT_EMAIL
    context.user_data["client_email"] = email
    await update.message.reply_text(order_summary(context), parse_mode="Markdown", reply_markup=confirm_keyboard())
    return STATE_CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_no":
        await query.edit_message_text("❌ Order cancel করা হয়েছে।")
        return ConversationHandler.END

    name = context.user_data["client_name"]
    phone = context.user_data["client_phone"]
    email = context.user_data["client_email"]
    var = context.user_data["selected_variation"]
    product = context.user_data["selected_product"]
    final_price = context.user_data.get("final_price", var["price"])
    has_discount = float(final_price) < float(var["price"])

    await query.edit_message_text("⏳ User create করছি...")
    user_resp = bot_create_user(name, email, phone)
    if not user_resp.get("success"):
        await query.edit_message_text(f"❌ User create হয়নি: {user_resp.get('message','')}")
        return ConversationHandler.END

    token = user_resp.get("token")
    coupon_info = None
    coupon_code = None

    if has_discount:
        await query.edit_message_text("🎟️ Coupon বানাচ্ছি...")
        coupon_info = create_discount_coupon(var["price"], final_price)
        if coupon_info:
            coupon_code = coupon_info["code"]

    autologin = bot_get_autologin_url(token, f"{WP_URL}/?add-to-cart={var['id']}&quantity=1")
    if not autologin.get("success"):
        if coupon_info:
            delete_coupon(coupon_info["id"])
        await query.edit_message_text("❌ Auto-login URL পাওয়া যায়নি.")
        return ConversationHandler.END

    await query.edit_message_text("🤖 *Browser চালু হচ্ছে...*\n\n⏳ 2-3 মিনিট অপেক্ষা করো।", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    success, output = await loop.run_in_executor(None, run_playwright_order, autologin["autologin_url"], var["id"], name, phone, email, coupon_code)

    if coupon_info:
        delete_coupon(coupon_info["id"])

    if success:
        price_text = f"৳{int(float(final_price))}" + (f" (Discount: ৳{int(float(var['price']) - float(final_price))})" if has_discount else "")
        client_msg = (
            f"আসসালামুয়ালাইকুম! 👋\n\n"
            f"আপনার order সফলভাবে তৈরি হয়েছে! ✅\n\n"
            f"নিচের তথ্য দিয়ে login করুন:\n\n"
            f"🌐 https://favouritedeals.online/\n"
            f"📧 Email: {email}\n"
            f"📱 WhatsApp: {phone}\n\n"
            f"👉 Login → My Account → My Subscriptions\n\n"
            f"⚠️ WhatsApp এ OTP আসবে। OTP দিয়ে verify করুন।\n\n"
            f"ধন্যবাদ! 🎉"
        )
        await query.edit_message_text(
            f"🎉 *Order সফল!*\n\n📦 {product['name'][:40]}\n🎯 {var['name']}\n💵 {price_text}\n👤 {name} | {phone}\n📧 {email}\n\n⚡ Main bot থেকে activate করো।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 আরেকটা Order", callback_data="new_order")]])
        )
        await context.bot.send_message(chat_id=query.message.chat_id, text=client_msg)
    else:
        await query.edit_message_text(
            f"❌ *Order হয়নি।*\n\n`{output[:300]}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 আবার চেষ্টা", callback_data="new_order")]])
        )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Cancel করা হয়েছে।")
    else:
        await update.message.reply_text("❌ Cancel করা হয়েছে।")
    return ConversationHandler.END


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(new_order_start, pattern="^new_order$")],
        states={
            STATE_PRODUCT:      [CallbackQueryHandler(product_selected, pattern="^prod_"), CallbackQueryHandler(cancel, pattern="^cancel$")],
            STATE_VARIATION:    [CallbackQueryHandler(variation_selected, pattern="^(var_|back_product)"), CallbackQueryHandler(cancel, pattern="^cancel$")],
            STATE_DISCOUNT:     [CallbackQueryHandler(discount_choice, pattern="^discount_")],
            STATE_CUSTOM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_price)],
            STATE_CLIENT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_name)],
            STATE_CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_phone)],
            STATE_CLIENT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_email)],
            STATE_CONFIRM:      [CallbackQueryHandler(confirm_order, pattern="^confirm_")],
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
