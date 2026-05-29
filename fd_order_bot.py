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
    STATE_CLIENT_NAME,
    STATE_CLIENT_PHONE,
    STATE_CLIENT_EMAIL,
    STATE_DISCOUNT,
    STATE_CUSTOM_PRICE,
    STATE_CONFIRM,
) = range(8)


# ── WooCommerce API ───────────────────────────────────────────────────

def wc_get(endpoint, params=None):
    try:
        return req.get(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            params=params or {},
            timeout=15
        ).json()
    except Exception as e:
        logger.error(f"WC GET {endpoint}: {e}")
        return None


def wc_post(endpoint, data):
    try:
        return req.post(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            json=data,
            timeout=15
        ).json()
    except Exception as e:
        logger.error(f"WC POST {endpoint}: {e}")
        return None


def wc_delete(endpoint):
    try:
        return req.delete(
            f"{WP_URL}/wp-json/wc/v3/{endpoint}",
            auth=(WC_KEY, WC_SECRET),
            params={"force": True},
            timeout=15
        ).json()
    except Exception as e:
        logger.error(f"WC DELETE {endpoint}: {e}")
        return None


# ── Live Product Fetch ────────────────────────────────────────────────

def fetch_live_products():
    try:
        products = wc_get("products", {
            "status": "publish",
            "stock_status": "instock",
            "per_page": 50,
            "orderby": "menu_order",
            "order": "asc"
        })
        if not isinstance(products, list):
            return []

        result = []
        for p in products:
            if p.get("catalog_visibility") == "hidden":
                continue
            name  = p.get("name", "")
            pid   = p.get("id")
            ptype = p.get("type", "simple")
            price = p.get("price", "0")
            result.append({
                "pid":   pid,
                "name":  name,
                "type":  ptype,
                "price": price,
            })
        return result
    except Exception as e:
        logger.error(f"fetch_live_products: {e}")
        return []


def fetch_variations(pid):
    try:
        resp = wc_get(f"products/{pid}/variations", {"per_page": 20})
        if isinstance(resp, list):
            return [
                {
                    "id":    v["id"],
                    "name":  " | ".join([a["option"] for a in v.get("attributes", [])]) or f"Plan {v['id']}",
                    "price": v.get("price", "0")
                }
                for v in resp
            ]
        return []
    except Exception as e:
        logger.error(f"fetch_variations({pid}): {e}")
        return []


# ── Dynamic Coupon ────────────────────────────────────────────────────

def create_discount_coupon(original_price, custom_price):
    try:
        discount = float(original_price) - float(custom_price)
        if discount <= 0:
            return None

        import random, string
        code = "FD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

        coupon = wc_post("coupons", {
            "code":               code,
            "discount_type":      "fixed_cart",
            "amount":             str(round(discount, 2)),
            "usage_limit":        1,
            "individual_use":     True,
            "free_shipping":      False,
        })

        if coupon and coupon.get("id"):
            logger.info(f"Coupon created: {code} (discount: {discount})")
            return {"id": coupon["id"], "code": code, "discount": discount}
        return None
    except Exception as e:
        logger.error(f"create_discount_coupon: {e}")
        return None


def delete_coupon(coupon_id):
    try:
        wc_delete(f"coupons/{coupon_id}")
        logger.info(f"Coupon {coupon_id} deleted")
    except Exception as e:
        logger.error(f"delete_coupon: {e}")


# ── PHP Bypass ────────────────────────────────────────────────────────

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


# ── Playwright ────────────────────────────────────────────────────────

def run_playwright_order(autologin_url, variation_id, product_id, client_name, client_phone, client_email, coupon_code=None):
    coupon_js = ""
    if coupon_code:
        coupon_js = f"""
        print("Applying coupon...")
        try:
            coupon_input = page.locator("#coupon_code, input[name='coupon_code']")
            if await coupon_input.count() > 0:
                await coupon_input.fill("{coupon_code}")
                apply_btn = page.locator("button[name='apply_coupon'], .woocommerce-form-coupon button")
                if await apply_btn.count() > 0:
                    await apply_btn.first.click()
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
        await page.goto("{WP_URL}/?add-to-cart={product_id}&variation_id={variation_id}&quantity=1", timeout=30000)
        await page.wait_for_load_state("networkidle")
        print("After add to cart:", page.url)

        print("Going to checkout...")
        await page.goto("{WP_URL}/checkout/", timeout=30000)
        await page.wait_for_load_state("networkidle")

        {coupon_js}

        print("Filling billing info...")
        try:
            await page.fill("#billing_first_name", "{client_name}")
        except: pass
        try:
            await page.fill("#billing_phone", "{client_phone}")
        except: pass
        try:
            await page.fill("#billing_email", "{client_email}")
        except: pass

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
        if "order-received" in current_url:
            print("SUCCESS: order placed!")
        else:
            print("FAILED: still on", current_url)

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
        success = "SUCCESS: order placed!" in output
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Timeout — 2 মিনিটেও order হয়নি।"
    except Exception as e:
        return False, f"Error: {str(e)}"


# ── Keyboards ─────────────────────────────────────────────────────────

def product_keyboard(products):
    keyboard = []
    row = []
    for i, p in enumerate(products):
        name = p["name"]
        if len(name) > 25:
            name = name[:25] + "..."
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


# ── Handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛍️ *FD Order Bot*\n\nAssalamualaikum bhai! 👋\n\nএই bot দিয়ে client এর হয়ে order করো।",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 নতুন Order", callback_data="new_order")],
        ])
    )


async def new_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Products fetch করছি...", parse_mode="Markdown")

    products = fetch_live_products()
    if not products:
        await query.edit_message_text(
            "❌ Products fetch হয়নি।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 আবার চেষ্টা", callback_data="new_order")]])
        )
        return ConversationHandler.END

    context.user_data["products"] = products

    await query.edit_message_text(
        f"📦 *Product বেছে নাও:* ({len(products)}টা পাওয়া গেছে)",
        parse_mode="Markdown",
        reply_markup=product_keyboard(products)
    )
    return STATE_PRODUCT


async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancel করা হয়েছে।")
        return ConversationHandler.END

    idx      = int(query.data.split("_")[1])
    products = context.user_data.get("products", [])

    if idx >= len(products):
        await query.edit_message_text("❌ Product পাওয়া যায়নি।")
        return ConversationHandler.END

    product = products[idx]
    context.user_data["selected_product"] = product

    if product["type"] in ["variable", "variable-subscription"]:
        await query.edit_message_text(f"⏳ {product['name'][:30]} এর plans fetch করছি...")
        variations = fetch_variations(product["pid"])
        if not variations:
            await query.edit_message_text("❌ Plans পাওয়া যায়নি।")
            return ConversationHandler.END
        context.user_data["variations"] = variations
        await query.edit_message_text(
            f"📦 *{product['name'][:40]}*\n\nPlan বেছে নাও:",
            parse_mode="Markdown",
            reply_markup=variation_keyboard(variations)
        )
        return STATE_VARIATION
    else:
        context.user_data["selected_variation"] = {
            "id":    product["pid"],
            "name":  "Standard",
            "price": product["price"]
        }
        await query.edit_message_text(
            f"📦 *{product['name'][:40]}*\n💵 ৳{product['price']}\n\n👤 Client এর *নাম* দাও:",
            parse_mode="Markdown"
        )
        return STATE_CLIENT_NAME


async def variation_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_product":
        products = context.user_data.get("products", [])
        await query.edit_message_text(
            f"📦 *Product বেছে নাও:*",
            parse_mode="Markdown",
            reply_markup=product_keyboard(products)
        )
        return STATE_PRODUCT

    idx        = int(query.data.split("_")[1])
    variations = context.user_data.get("variations", [])

    if idx >= len(variations):
        await query.edit_message_text("❌ Plan পাওয়া যায়নি।")
        return ConversationHandler.END

    selected = variations[idx]
    context.user_data["selected_variation"] = selected

    product = context.user_data["selected_product"]
    await query.edit_message_text(
        f"✅ *{product['name'][:40]}*\n"
        f"📋 Plan: {selected['name']}\n"
        f"💵 Original Price: ৳{selected['price']}\n\n"
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
    await update.message.reply_text(
        f"✅ নাম: *{name}*\n\n📱 Client এর *WhatsApp নম্বর* দাও:",
        parse_mode="Markdown"
    )
    return STATE_CLIENT_PHONE


async def get_client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    phone = update.message.text.strip()
    if len(re.sub(r"[^0-9]", "", phone)) < 10:
        await update.message.reply_text("❌ সঠিক নম্বর দাও।")
        return STATE_CLIENT_PHONE
    context.user_data["client_phone"] = phone
    await update.message.reply_text(
        f"✅ Phone: *{phone}*\n\n📧 Client এর *Email* দাও:",
        parse_mode="Markdown"
    )
    return STATE_CLIENT_EMAIL


async def get_client_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    email = update.message.text.strip().lower()
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        await update.message.reply_text("❌ সঠিক email দাও।")
        return STATE_CLIENT_EMAIL

    context.user_data["client_email"] = email
    var = context.user_data["selected_variation"]

    await update.message.reply_text(
        f"💵 Original Price: *৳{var['price']}*\n\nDiscount দেবে?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ হ্যাঁ", callback_data="discount_yes"),
                InlineKeyboardButton("❌ না, Original Price", callback_data="discount_no"),
            ]
        ])
    )
    return STATE_DISCOUNT


async def discount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "discount_no":
        var = context.user_data["selected_variation"]
        context.user_data["final_price"]  = var["price"]
        context.user_data["coupon_code"]  = None
        await query.edit_message_text(
            _order_summary(context),
            parse_mode="Markdown",
            reply_markup=_confirm_keyboard()
        )
        return STATE_CONFIRM
    else:
        var = context.user_data["selected_variation"]
        await query.edit_message_text(
            f"💵 Original: *৳{var['price']}*\n\nনতুন price লেখো (শুধু সংখ্যা):\nযেমন: `300`",
            parse_mode="Markdown"
        )
        return STATE_CUSTOM_PRICE


async def get_custom_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        custom_price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ শুধু সংখ্যা দাও। যেমন: 300")
        return STATE_CUSTOM_PRICE

    var            = context.user_data["selected_variation"]
    original_price = float(var["price"])

    if custom_price >= original_price:
        await update.message.reply_text(
            f"❌ Custom price অবশ্যই original price (৳{original_price}) এর কম হতে হবে।"
        )
        return STATE_CUSTOM_PRICE

    if custom_price <= 0:
        await update.message.reply_text("❌ Price 0 এর বেশি হতে হবে।")
        return STATE_CUSTOM_PRICE

    discount = original_price - custom_price
    context.user_data["final_price"]    = custom_price
    context.user_data["discount_amount"] = discount
    context.user_data["coupon_code"]    = None

    await update.message.reply_text(
        f"✅ Custom Price: *৳{int(custom_price)}*\n"
        f"💰 Discount: *৳{int(discount)}*\n\n"
        + _order_summary(context),
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard()
    )
    return STATE_CONFIRM


def _order_summary(context):
    var          = context.user_data["selected_variation"]
    product      = context.user_data["selected_product"]
    name         = context.user_data["client_name"]
    phone        = context.user_data["client_phone"]
    email        = context.user_data["client_email"]
    final_price  = context.user_data.get("final_price", var["price"])

    return (
        f"📋 *Order Summary:*\n\n"
        f"📦 {product['name'][:40]}\n"
        f"🎯 Plan: {var['name']}\n"
        f"💵 Price: ৳{final_price}\n\n"
        f"👤 {name}\n"
        f"📱 {phone}\n"
        f"📧 {email}\n\n"
        f"Order করবো?"
    )


def _confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ হ্যাঁ Order করো", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm_no"),
        ]
    ])


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_no":
        await query.edit_message_text("❌ Order cancel করা হয়েছে।")
        return ConversationHandler.END

    name         = context.user_data["client_name"]
    phone        = context.user_data["client_phone"]
    email        = context.user_data["client_email"]
    var          = context.user_data["selected_variation"]
    product      = context.user_data["selected_product"]
    final_price  = context.user_data.get("final_price", var["price"])
    has_discount = float(final_price) < float(var["price"])

    await query.edit_message_text("⏳ User create করছি...")

    user_resp = bot_create_user(name, email, phone)
    if not user_resp.get("success"):
        await query.edit_message_text(f"❌ User create হয়নি:\n{user_resp.get('message','')}")
        return ConversationHandler.END

    token = user_resp.get("token")
    await query.edit_message_text("✅ User ready!\n\n🎟️ Coupon বানাচ্ছি..." if has_discount else "✅ User ready!\n\n🛒 Checkout শুরু হচ্ছে...")

    coupon_info = None
    coupon_code = None
    if has_discount:
        coupon_info = create_discount_coupon(var["price"], final_price)
        if coupon_info:
            coupon_code = coupon_info["code"]
            await query.edit_message_text(f"✅ Coupon ready: `{coupon_code}`\n\n🤖 Browser চালু হচ্ছে...", parse_mode="Markdown")
        else:
            await query.edit_message_text("⚠️ Coupon বানানো যায়নি, original price এ order হবে।")

    autologin = bot_get_autologin_url(token, f"{WP_URL}/checkout/")
    if not autologin.get("success"):
        if coupon_info:
            delete_coupon(coupon_info["id"])
        await query.edit_message_text(f"❌ Auto-login URL পাওয়া যায়নি.")
        return ConversationHandler.END

    await query.edit_message_text("🤖 *Browser চালু হচ্ছে...*\n\n⏳ 1-2 মিনিট অপেক্ষা করো।", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    success, output = await loop.run_in_executor(
        None, run_playwright_order,
        autologin["autologin_url"], var["id"], product["pid"], name, phone, email, coupon_code
    )

    if coupon_info:
        delete_coupon(coupon_info["id"])

    if success:
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

        price_text = f"৳{int(float(final_price))}" + (f" (Discount: ৳{int(float(var['price']) - float(final_price))})" if has_discount else "")

        await query.edit_message_text(
            f"🎉 *Order সফল!*\n\n"
            f"📦 {product['name'][:40]}\n"
            f"🎯 {var['name']}\n"
            f"💵 {price_text}\n"
            f"👤 {name} | {phone}\n"
            f"📧 {email}\n\n"
            f"⚡ Main bot থেকে subscription activate করো।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 আরেকটা Order", callback_data="new_order")]])
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=client_msg
        )
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


# ── Main ──────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(new_order_start, pattern="^new_order$"),
        ],
        states={
            STATE_PRODUCT:   [
                CallbackQueryHandler(product_selected, pattern="^prod_"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            STATE_VARIATION: [
                CallbackQueryHandler(variation_selected, pattern="^(var_|back_product)"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            STATE_CLIENT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_name)],
            STATE_CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_phone)],
            STATE_CLIENT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_email)],
            STATE_DISCOUNT:     [CallbackQueryHandler(discount_choice, pattern="^discount_")],
            STATE_CUSTOM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_price)],
            STATE_CONFIRM:      [CallbackQueryHandler(confirm_order, pattern="^confirm_")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel$"),
            CommandHandler("cancel", cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    logger.info("FD Order Bot v3 started!")
    app.run_polling()


if __name__ == "__main__":
    main()
