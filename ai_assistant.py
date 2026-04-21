import json
import openai
import pg8000
import os
import requests
from datetime import datetime

client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ─── Database helper ───────────────────────────────────────
def get_db():
    import urllib.parse
    url = urllib.parse.urlparse(os.environ["DATABASE_URL"])
    return pg8000.connect(
        host=url.hostname, port=url.port or 5432,
        database=url.path[1:], user=url.username,
        password=url.password, ssl_context=True
    )

# ─── Function definitions (OpenAI কে জানাবে কী কী করতে পারে) ───
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_orders",
            "description": "Orders খোঁজে বের করে। status, customer name, বা সংখ্যা দিয়ে filter করা যায়।",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "pending, processing, completed, cancelled, on-hold"},
                    "limit": {"type": "integer", "description": "কয়টা order দেখাবে, default 5"},
                    "customer_name": {"type": "string", "description": "Customer এর নাম"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_order_status",
            "description": "Order এর payment status বা order status পরিবর্তন করে।",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "description": "Order ID"},
                    "status": {"type": "string", "description": "নতুন status: pending, processing, completed, cancelled, on-hold"},
                    "find_last": {"type": "boolean", "description": "True হলে সবচেয়ে শেষ order খুঁজে update করবে"}
                },
                "required": ["status"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_products",
            "description": "Products খোঁজে বের করে। নাম বা stock status দিয়ে filter করা যায়।",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Product এর নাম বা অংশ"},
                    "low_stock": {"type": "boolean", "description": "True হলে কম stock এর products দেখাবে"},
                    "limit": {"type": "integer", "description": "কয়টা দেখাবে, default 5"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_summary",
            "description": "আজকের বা এই সপ্তাহের sales summary দেখায়।",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {"type": "string", "description": "today, week, month"}
                }
            }
        }
    }
]

# ─── Function implementations ──────────────────────────────
def get_orders(status=None, limit=5, customer_name=None):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT id, customer_name, total, status, created_at FROM orders WHERE 1=1"
    params = []
    if status:
        query += " AND status = %s"
        params.append(status)
    if customer_name:
        query += " AND customer_name ILIKE %s"
        params.append(f"%{customer_name}%")
    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return "কোনো order পাওয়া যায়নি।"
    result = []
    for r in rows:
        result.append(f"Order #{r[0]} | {r[1]} | ৳{r[2]} | {r[3]} | {r[4].strftime('%d %b %Y')}")
    return "\n".join(result)

def update_order_status(status, order_id=None, find_last=False):
    conn = get_db()
    cur = conn.cursor()
    if find_last or not order_id:
        cur.execute("SELECT id FROM orders ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.close()
            return "কোনো order পাওয়া যায়নি।"
        order_id = row[0]
    cur.execute("UPDATE orders SET status = %s WHERE id = %s RETURNING id, customer_name", (status, order_id))
    updated = cur.fetchone()
    conn.commit()
    conn.close()
    if updated:
        return f"✅ Order #{updated[0]} ({updated[1]}) এর status '{status}' করা হয়েছে।"
    return f"Order #{order_id} পাওয়া যায়নি।"

def get_products(name=None, low_stock=False, limit=5):
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT id, name, price, stock_quantity FROM products WHERE 1=1"
    params = []
    if name:
        query += " AND name ILIKE %s"
        params.append(f"%{name}%")
    if low_stock:
        query += " AND stock_quantity < 10"
    query += " ORDER BY stock_quantity ASC LIMIT %s"
    params.append(limit)
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return "কোনো product পাওয়া যায়নি।"
    result = []
    for r in rows:
        result.append(f"#{r[0]} {r[1]} | ৳{r[2]} | Stock: {r[3]}")
    return "\n".join(result)

def get_summary(period="today"):
    conn = get_db()
    cur = conn.cursor()
    if period == "today":
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE DATE(created_at) = CURRENT_DATE")
    elif period == "week":
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at >= NOW() - INTERVAL '7 days'")
    else:
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE created_at >= NOW() - INTERVAL '30 days'")
    row = cur.fetchone()
    conn.close()
    label = {"today": "আজকের", "week": "এই সপ্তাহের", "month": "এই মাসের"}.get(period, "")
    return f"📊 {label} summary:\nTotal orders: {row[0]}\nTotal sales: ৳{row[1]:,.2f}"

# ─── Function dispatcher ───────────────────────────────────
FUNCTION_MAP = {
    "get_orders": get_orders,
    "update_order_status": update_order_status,
    "get_products": get_products,
    "get_summary": get_summary
}

def run_function(name, args):
    func = FUNCTION_MAP.get(name)
    if func:
        return func(**args)
    return "Unknown function"

# ─── Main AI handler ───────────────────────────────────────
def handle_ai_message(user_text: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "তুমি Favourite Deals এর intelligent bot assistant। "
                "বাংলায় কথা বলো। User এর কথা বুঝে সঠিক function call করো। "
                "যদি কেউ বলে 'শেষ order' তাহলে find_last=True দিয়ে কাজ করো।"
            )
        },
        {"role": "user", "content": user_text}
    ]
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto"
    )
    
    msg = response.choices[0].message
    
    # Function call আছে কিনা দেখো
    if msg.tool_calls:
        tool_call = msg.tool_calls[0]
        func_name = tool_call.function.name
        func_args = json.loads(tool_call.function.arguments)
        
        # Function execute করো
        result = run_function(func_name, func_args)
        
        # Result OpenAI কে দিয়ে final response নাও
        messages.append(msg)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result
        })
        
        final = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        return final.choices[0].message.content
    
    return msg.content
