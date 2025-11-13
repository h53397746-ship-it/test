
}

def safe_load_store(path=DATA_STORE_PATH):
    with _data_lock:
        if not os.path.exists(path):
            safe_write_store(DEFAULT_STORE, path)
            return json.loads(json.dumps(DEFAULT_STORE))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k in DEFAULT_STORE:
                    if k not in data:
                        data[k] =].get("username") != username:
                users[uid]["username"] = username; changed = True
            if first_name and users[uid].get("first_name") != first_name:
                users[uid]["first_name"] = first_name; changed = True
            if changed:
                users[uid]["last_updated"] = now_iso()
                persist_store()

def get_user_credits(user_id: int):
    if is_owner(user_id):
        return "Unlimited"
    uid = str(int(user_id))
    with _data_lock:
        u = _store.get("users", {}).get(uid)
        return u.get("credits", 0) if u else 0

def add_credits(user_id: int, credits: int):
    uid = str(int(user_id))
    with _data_lock:
        users = _store.setdefault("users", {})
        if uid not in users:
            users[uid] = {"user_id": int(user_id), "credits": max(0, credits), "created_at": now_iso(), "last_updated": now_iso()}
        else:
            users[uid]["credits"] = users[uid].get("credits", 0) + credits
            users[uid]["last_updated"] = now_iso()
        persist_store()

def deduct_credits(user_id: int, credits: int=1) -> bool:
    if is_owner(user_id):
        return True
    uid = str(int(user_id))
    with _data_lock:
        users = _store.setdefault("users", {})
        u = users.get(uid)
        if not u:
            return False
        if u.get("credits", 0) < credits:
            return False
        u["credits"] = u.get("credits", 0) - credits
        u["last_updated"] = now_iso()
        persist_store()
        return True

def log_search(user_id: int, username: Optional[str], mobile: str, results_count: int, credits_deducted: int):
    entry = {
        "user_id": int(user_id),
        "username": username,
        "mobile": clean_mobile_number(mobile),
        "timestamp": now_iso(),
        "results_count": int(results_count),
        "credits_deducted": int(credits_deducted)
    }
    with _data_lock:
        _store.setdefault("search_logs", []).append(entry)
        if len(_store["search_logs"]) > 5000:
            _store["search_logs"] = _store["search_logs"][-5000:]
        persist_store()

def save_search_history(user_id: int, mobile: str, data: List[Dict[str, Any]]):
    uid = str(int(user_id))
    m = clean_mobile_number(mobile)
    with _data_lock:
        hist = _store.setdefault("search_history", {}).setdefault(uid, {})
        hist[m] = {"data": data, "timestamp": now_iso()}
        persist_store()

def get_search_history(user_id: int, mobile: str):
    uid = str(int(user_id))
    m = clean_mobile_number(mobile)
    with _data_lock:
        return _store.get("search_history", {}).get(uid, {}).get(m)

def get_user_search_history_list(user_id: int, limit: int=20):
    uid = str(int(user_id))
    with _data_lock:
        d = _store.get("search_history", {}).get(uid, {})
        items = sorted(d.items(), key=lambda kv: kv[1].get("timestamp", ""), reverse=True)[:limit]
        return [{"mobile": k, "timestamp": v.get("timestamp"), "data": v.get("data")} for k, v in items]

# redeem codes
def generate_redeem_codes(credits: int, count: int, generated_by: int) -> List[str]:
    codes = []
    with _data_lock:
        rc = _store.setdefault("redeem_codes", {})
        for _ in range(count):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            while code in rc:
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            rc[code] = {
                "code": code,
                "credits": int(credits),
                "is_used": False,
                "used_by": None,
                "used_at": None,
                "generated_by": int(generated_by),
                "generated_at": now_iso()
            }
            codes.append(code)
        persist_store()
    return codes

def redeem_code(user_id: int, code: str):
    code_up = code.strip().upper()
    with _data_lock:
        rc = _store.setdefault("redeem_codes", {})
        doc = rc.get(code_up)
        if not doc:
            return False, "Invalid or already used redemption code"
        if doc["is_used"]:
            return False, "Code already used"
        doc["is_used"] = True
        doc["used_by"] = int(user_id)
        doc["used_at"] = now_iso()
        add_credits(user_id, int(doc["credits"]))
        persist_store()
        return True, int(doc["credits"])

# blacklist
def add_to_blacklist(mobile: str, added_by: int, reason: Optional[str]=None):
    m = clean_mobile_number(mobile)
    with _data_lock:
        bl = _store.setdefault("blacklist", {})
        if m in bl and bl[m].get("is_active", False):
            return False, "Already blacklisted"
        bl[m] = {
            "mobile": m,
            "is_active": True,
            "added_by": int(added_by),
            "reason": reason or "No reason provided",
            "added_at": now_iso(),
            "removed_at": None
        }
        persist_store()
        return True, "Blacklisted"

def remove_from_blacklist(mobile: str):
    m = clean_mobile_number(mobile)
    with _data_lock:
        bl = _store.setdefault("blacklist", {})
        if m not in bl or not bl[m].get("is_active", False):
            return False, "Not found in blacklist"
        bl[m]["is_active"] = False
        bl[m]["removed_at"] = now_iso()
        persist_store()
        return True, "Removed from blacklist"

def is_blacklisted(mobile: str) -> bool:
    m = clean_mobile_number(mobile)
    with _data_lock:
        bl = _store.get("blacklist", {})
        return bool(bl.get(m, {}).get("is_active", False))

# -------------------------
# UI: refined keyboards and layout
# -------------------------
def main_menu_keyboard(is_owner_user=False):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🔍 Search", callback_data="search_mobile"),
           InlineKeyboardButton("📋 History", callback_data="my_history"))
    kb.add(InlineKeyboardButton("💳 Credits", callback_data="my_credits"),
           InlineKeyboardButton("💰 Buy", callback_data="buy_credits"))
    kb.add(InlineKeyboardButton("🆘 Support", url=SUPPORT_BOT),
           InlineKeyboardButton("ℹ️ Help", callback_data="help"))
    if is_owner_user:
        kb.add(InlineKeyboardButton("📊 Admin", callback_data="admin_stats"),
               InlineKeyboardButton("🎫 Redeem Stats", callback_data="redeem_stats"))
    return kb

def compact_back_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
    return kb

# -------------------------
# Auto-delete helper
# -------------------------
def schedule_delete(chat_id: int, message_id: int, delay_minutes: int=DATA_EXPIRY_MINUTES):
    def worker():
        try:
            bot.delete_message(chat_id, message_id)
            logger.debug("Auto-deleted %s:%s", chat_id, message_id)
        except Exception as e:
            logger.debug("Auto-delete failed: %s", e)
    t = threading.Timer(delay_minutes * 60, worker)
    t.daemon = True
    t.start()
    return t

# -------------------------
# Bot handlers
# -------------------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    credits = get_user_credits(user_id)
    header = f"<b>{html_escape(BOT_NAME)}</b>\n<i>{html_escape(COMPANY_LINE)}</i>\n\n"
    body = (f"{header}"
            f"Welcome, <b>{html_escape(message.from_user.first_name or message.from_user.username or str(user_id))}</b> 👋\n"
            f"<b>Credits:</b> {html_escape(str(credits))}\n\n"
            f"{html_escape(PRIVACY_NOTE)}")
    kb = main_menu_keyboard(is_owner(user_id))
    bot.send_message(user_id, body, parse_mode='HTML', reply_markup=kb)

@bot.message_handler(commands=['help'])
def cmd_help(message):
    help_text = (
        "<b>How to use</b>\n\n"
        "• Send a 10-digit mobile number (e.g. <code>9876543210</code>) to perform a lookup.\n"
        "• Use the buttons to view history, check credits, or purchase more.\n"
        "• Admins can manage credits and generate redeem codes.\n\n"
        f"<b>Support:</b> {html_escape(SUPPORT_BOT)}"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='HTML', reply_markup=compact_back_keyboard())

# Admin: add credits
@bot.message_handler(regexp=r'^/add\s+\d+\s+\d+$')
def cmd_add(msg):
    if not is_owner(msg.from_user.id):
        bot.reply_to(msg, "❌ You are not authorized to use this command.")
        return
    parts = msg.text.split()
    target = int(parts[1]); credits = int(parts[2])
    add_credits(target, credits)
    bot.reply_to(msg, f"✅ Added <b>{credits}</b> credits to <code>{target}</code>. New balance: <b>{get_user_credits(target)}</b>", parse_mode='HTML')

# Admin: remove credits
@bot.message_handler(regexp=r'^/remove\s+\d+\s+\d+$')
def cmd_remove(msg):
    if not is_owner(msg.from_user.id):
        bot.reply_to(msg, "❌ You are not authorized to use this command.")
        return
    parts = msg.text.split()
    target = int(parts[1]); credits = int(parts[2])
    if is_owner(target):
        bot.reply_to(msg, "❌ Cannot remove credits from owner.")
        return
    add_credits(target, -credits)
    bot.reply_to(msg, f"✅ Removed <b>{credits}</b> credits from <code>{target}</code>. New balance: <b>{get_user_credits(target)}</b>", parse_mode='HTML')

# Admin: generate redeem codes
@bot.message_handler(regexp=r'^/redemption\s+\d+\s+\d+$')
def cmd_redemption(msg):
    if not is_owner(msg.from_user.id):
        bot.reply_to(msg, "❌ You are not authorized.")
        return
    parts = msg.text.split()
    credits = int(parts[1]); count = int(parts[2])
    if count > 500:
        bot.reply_to(msg, "❌ Maximum 500 codes at once.")
        return
    codes = generate_redeem_codes(credits, count, msg.from_user.id)
    codes_text = "\n".join(codes)
    if len(codes_text) > 3000:
        fname = f"redeem_{int(time.time())}.txt"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(codes_text)
        with open(fname, "rb") as f:
            bot.send_document(msg.chat.id, f, caption=f"Generated {count} codes ({credits} credits each)")
        os.remove(fname)
    else:
        bot.reply_to(msg, f"✅ Generated <b>{count}</b> codes (each <b>{credits}</b> credits):\n<pre>{html_escape(codes_text)}</pre>", parse_mode='HTML')

# Redeem
@bot.message_handler(regexp=r'^/redeem\s+[A-Za-z0-9]{8}$')
def cmd_redeem(msg):
    code = msg.text.split()[1].upper()
    ok, result = redeem_code(msg.from_user.id, code)
    if ok:
        bot.reply_to(msg, f"🎉 Redeemed <b>{code}</b> — +{result} credits.\nNew balance: <b>{get_user_credits(msg.from_user.id)}</b>", parse_mode='HTML')
    else:
        bot.reply_to(msg, f"❌ {html_escape(result)}", parse_mode='HTML')

# Blacklist add (admin)
@bot.message_handler(regexp=r'^/blacklist\s+.+')
def cmd_blacklist(msg):
    if not is_owner(msg.from_user.id):
        bot.reply_to(msg, "❌ You are not authorized.")
        return
    parts = msg.text.split(maxsplit=2)
    mobile = parts[1]
    reason = parts[2] if len(parts) > 2 else "No reason provided"
    ok, message = add_to_blacklist(mobile, msg.from_user.id, reason)
    if ok:
        bot.reply_to(msg, f"✅ Blacklisted <code>+91{clean_mobile_number(mobile)}</code>", parse_mode='HTML')
    else:
        bot.reply_to(msg, f"❌ {html_escape(message)}", parse_mode='HTML')

# Unblacklist (admin)
@bot.message_handler(regexp=r'^/unblacklist\s+.+')
def cmd_unblacklist(msg):
    if not is_owner(msg.from_user.id):
        bot.reply_to(msg, "❌ You are not authorized.")
        return
    mobile = msg.text.split(maxsplit=1)[1]
    ok, message = remove_from_blacklist(mobile)
    if ok:
        bot.reply_to(msg, f"✅ Removed <code>{clean_mobile_number(mobile)}</code> from blacklist", parse_mode='HTML')
    else:
        bot.reply_to(msg, f"❌ {html_escape(message)}", parse_mode='HTML')

# History
@bot.message_handler(commands=['history'])
def cmd_history(msg):
    hist = get_user_search_history_list(msg.from_user.id, limit=50)
    if not hist:
        bot.reply_to(msg, "📋 <b>Your search history is empty.</b>", parse_mode='HTML')
        return
    text = "<b>Your Search History</b>\n\n"
    for i, entry in enumerate(hist[:20], 1):
        ts = entry.get("timestamp", "")
        text += f"{i}. <code>{html_escape(entry.get('mobile'))}</code> — {html_escape(ts)}\n"
    bot.reply_to(msg, text, parse_mode='HTML')

# Callback queries (UI actions)
@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    uid = call.from_user.id
    data = call.data
    try:
        if data == "main_menu":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"🏠 <b>Main Menu</b>", parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(uid)))
            return
        if data == "search_mobile":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text="🔍 Send a 10-digit mobile number (e.g. <code>9876543210</code>).", parse_mode='HTML', reply_markup=compact_back_keyboard())
            return
        if data == "my_history":
            hist = get_user_search_history_list(uid, limit=50)
            if not hist:
                bot.answer_callback_query(call.id, "No history")
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                      text="📋 <b>Your search history is empty.</b>", parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(uid)))
                return
            text = "<b>Your Search History</b>\n\n"
            for i, entry in enumerate(hist[:20], 1):
                ts = entry.get("timestamp", "")
                text += f"{i}. <code>{html_escape(entry.get('mobile'))}</code> — {html_escape(ts)}\n"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=text, parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(uid)))
            return
        if data == "my_credits":
            credits = get_user_credits(uid)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"💳 <b>Credits:</b> {html_escape(str(credits))}", parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(uid)))
            return
        if data == "buy_credits":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=format_pricing_plans_html(PRICING_PLANS), parse_mode='HTML', reply_markup=compact_back_keyboard())
            return
        if data == "help":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text="ℹ️ Use /help for instructions.", parse_mode='HTML', reply_markup=compact_back_keyboard())
            return
        if data == "admin_stats" and is_owner(uid):
            with _data_lock:
                users_count = len(_store.get("users", {}))
                searches = len(_store.get("search_logs", []))
            stats_msg = f"📊 <b>Admin Stats</b>\n\n• Users: <b>{users_count}</b>\n• Searches logged: <b>{searches}</b>"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=stats_msg, parse_mode='HTML', reply_markup=main_menu_keyboard(True))
            return
        if data == "redeem_stats" and is_owner(uid):
            with _data_lock:
                total = len(_store.get("redeem_codes", {}))
                used = sum(1 for v in _store.get("redeem_codes", {}).values() if v.get("is_used"))
                unused = total - used
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"🎫 <b>Redeem Codes</b>\n\n• Total: <b>{total}</b>\n• Used: <b>{used}</b>\n• Unused: <b>{unused}</b>", parse_mode='HTML', reply_markup=main_menu_keyboard(True))
            return
    except Exception as e:
        logger.exception("Callback error: %s", e)

# Text handler: handle 10-digit numbers
@bot.message_handler(func=lambda m: True, content_types=['text'])
def text_handler(message):
    text = (message.text or "").strip()
    user_id = message.from_user.id
    # ignore slash commands here (they're handled above)
    if text.startswith("/"):
        return
    valid, cleaned = validate_mobile_number(text)
    if not valid:
        # quietly ignore other messages (or optionally reply)
        return
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    # blacklist check
    if not is_owner(user_id) and is_blacklisted(cleaned):
        bot.send_message(user_id, "🚫 This number is unavailable for lookup.", parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(user_id)))
        return
    # check local history
    hist = get_search_history(user_id, cleaned)
    if hist:
        formatted = format_user_data_html(hist.get("data"))
        sent = bot.send_message(user_id, f"📁 <b>From your history</b>\n\n{formatted}\n\n⏰ This message will be deleted in {DATA_EXPIRY_MINUTES} minutes.", parse_mode='HTML')
        schedule_delete(sent.chat.id, sent.message_id, delay_minutes=DATA_EXPIRY_MINUTES)
        return
    # credits check
    credits = get_user_credits(user_id)
    if not is_owner(user_id) and (not isinstance(credits, int) or credits < 1):
        bot.send_message(user_id, f"⚠️ <b>Insufficient credits</b>\nBalance: {html_escape(str(credits))}\nContact {html_escape(SUPPORT_BOT)} to purchase credits.", parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(user_id)))
        return
    searching_msg = bot.send_message(user_id, f"⏳ Searching <code>{html_escape(cleaned)}</code> — please wait...", parse_mode='HTML')
    results = pg.search_mobile(cleaned) if pg and POSTGRES_URI else []
    if results:
        deducted = 0
        if not is_owner(user_id):
            ok = deduct_credits(user_id, 1)
            if not ok:
                bot.edit_message_text(chat_id=searching_msg.chat.id, message_id=searching_msg.message_id, text="❌ Could not deduct credits.", parse_mode='HTML')
                return
            deducted = 1
        save_search_history(user_id, cleaned, results)
        if not is_owner(user_id):
            log_search(user_id, message.from_user.username or "unknown", cleaned, len(results), deducted)
        formatted = format_user_data_html(results)
        bot.edit_message_text(chat_id=searching_msg.chat.id, message_id=searching_msg.message_id,
                              text=f"✅ <b>Search Successful</b>\n\n{formatted}\n\n💳 Remaining Credits: <b>{html_escape(str(get_user_credits(user_id)))}</b>\n\n⏰ This message will be deleted in {DATA_EXPIRY_MINUTES} minutes.", parse_mode='HTML')
        schedule_delete(searching_msg.chat.id, searching_msg.message_id, delay_minutes=DATA_EXPIRY_MINUTES)
    else:
        bot.edit_message_text(chat_id=searching_msg.chat.id, message_id=searching_msg.message_id,
                              text=f"🔎 <b>No data found</b>\n\n<code>{html_escape(cleaned)}</code>\nCredits not deducted.", parse_mode='HTML', reply_markup=main_menu_keyboard(is_owner(user_id)))

# -------------------------
# Starter
# -------------------------
def main():
    logger.info("Starting refined bot...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception as e:
        logger.exception("Bot stopped unexpectedly: %s", e)

if __name__ == "__main__":
    main()

