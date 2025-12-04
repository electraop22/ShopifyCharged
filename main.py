import os
import asyncio
import time
import logging
from datetime import datetime, timedelta
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
import uuid
from collections import defaultdict
from gate import sh, ShResult
from gate2 import sh14  # Import the new gate function
from gate3 import live_check  # Import the new live check function

# MongoDB Setup
MONGO_URI = "mongodb+srv://ElectraOp:BGMI272@cluster0.1jmwb.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "fn_bot"
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
keys_col = db["keys"]
users_col = db["users"]
logs_col = db["logs"]
broadcast_col = db["broadcast"]
user_limits_col = db["user_limits"]
proxies_col = db["proxiess"]
admins_col = db["admins"]  # New collection for admins

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Bot Owner ID
OWNER_ID = 7593550190

# Initialize collections if not exists
existing_collections = db.list_collection_names()
collections = [keys_col, users_col, logs_col, broadcast_col, user_limits_col, proxies_col, admins_col]
for col in collections:
    if col.name not in existing_collections:
        db.create_collection(col.name)

# Proxy Manager
class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.current_index = 0
        self.bad_proxies = set()
        self.semaphore = asyncio.Semaphore(100)  # Increased concurrency limit
        self.last_batch_time = 0
        self.load_proxies()

    def load_proxies(self):
        # Load proxies from database
        db_proxies = []
        for p in proxies_col.find({}, {'proxy': 1}):
            if 'proxy' in p:
                db_proxies.append(p['proxy'])
        
        # Load proxies from proxies.txt file
        file_proxies = []
        try:
            if os.path.exists("proxies.txt"):
                with open("proxies.txt", "r") as f:
                    file_proxies = [line.strip() for line in f if line.strip()]
        except Exception as e:
            logger.error(f"Error loading proxies from file: {str(e)}")
        
        # Combine and deduplicate proxies
        all_proxies = list(set(db_proxies + file_proxies))
        
        # Update database with new proxies
        for proxy in file_proxies:
            if not proxies_col.find_one({"proxy": proxy}):
                proxies_col.insert_one({"proxy": proxy, "source": "file", "added_at": datetime.utcnow()})
        
        self.proxies = all_proxies
        logger.info(f"Loaded {len(self.proxies)} proxies (DB: {len(db_proxies)}, File: {len(file_proxies)})")

    async def rotate_proxy(self):
        if not self.proxies:
            return None
            
        async with self.semaphore:
            # Check if we need to wait before next batch
            current_time = time.time()
            if current_time - self.last_batch_time < 70 and len(self.proxies) - len(self.bad_proxies) < 3:
                wait_time = 70 - (current_time - self.last_batch_time)
                logger.info(f"Waiting {wait_time:.1f}s before next proxy batch")
                await asyncio.sleep(wait_time)
                self.last_batch_time = time.time()
            elif current_time - self.last_batch_time >= 70:
                self.last_batch_time = time.time()
            
            # Find next good proxy
            start_index = self.current_index
            while True:
                proxy = self.proxies[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.proxies)
                
                if proxy not in self.bad_proxies:
                    return proxy
                    
                if self.current_index == start_index:
                    logger.warning("All proxies marked as bad, resetting bad list")
                    self.bad_proxies = set()
                    return self.proxies[self.current_index]

    def mark_bad(self, proxy):
        if proxy and proxy not in self.bad_proxies:
            self.bad_proxies.add(proxy)
            logger.warning(f"Marked proxy as bad: {proxy}")

# Global proxy manager
proxy_manager = ProxyManager()

def log_event(level: str, message: str, user_id: int = None):
    entry = {
        "timestamp": datetime.utcnow(),
        "level": level,
        "message": message,
        "user_id": user_id
    }
    logs_col.insert_one(entry)
    if level == "ERROR":
        logger.error(f"User {user_id}: {message}" if user_id else message)
    else:
        logger.info(f"User {user_id}: {message}" if user_id else message)

def is_admin(user_id: int) -> bool:
    """Check if user is admin or owner"""
    if user_id == OWNER_ID:
        return True
    admin = admins_col.find_one({"user_id": user_id})
    return admin is not None

def generate_key(duration_days: int) -> str:
    key_id = str(uuid.uuid4()).split('-')[0].upper()
    key = f"FN-SHOPIFY-{key_id}"
    expiry = datetime.utcnow() + timedelta(days=duration_days)
    
    keys_col.insert_one({
        "key": key,
        "duration_days": duration_days,
        "created_at": datetime.utcnow(),
        "expires_at": expiry,
        "used": False
    })
    
    return key

def redeem_key(key: str, user_id: int) -> bool:
    key_data = keys_col.find_one({"key": key})
    if not key_data or key_data["used"]:
        return False
    
    expiry = datetime.utcnow() + timedelta(days=key_data["duration_days"])
    users_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "expires_at": expiry,
            "key_used": key,
            "access_granted": True
        }},
        upsert=True
    )
    
    keys_col.update_one(
        {"key": key},
        {"$set": {"used": True, "used_by": user_id, "used_at": datetime.utcnow()}}
    )
    
    return True

def has_valid_access(user_id: int) -> bool:
    user = users_col.find_one({"user_id": user_id})
    if not user:
        return False
    
    if "expires_at" in user and user["expires_at"] > datetime.utcnow():
        return True
    return False

def can_user_check_more(user_id: int, card_count: int) -> bool:
    user_limit = user_limits_col.find_one({"user_id": user_id})
    current_time = datetime.utcnow()
    
    if not user_limit:
        user_limits_col.insert_one({
            "user_id": user_id,
            "last_check_time": current_time,
            "cards_checked": card_count
        })
        return True
    
    # Reset if more than 5 minutes have passed
    if (current_time - user_limit["last_check_time"]) > timedelta(minutes=5):
        user_limits_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "last_check_time": current_time,
                "cards_checked": card_count
            }}
        )
        return True
    
    # Check if user has checked less than 1500 cards
    if user_limit["cards_checked"] + card_count <= 1500:
        user_limits_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "last_check_time": current_time,
            },
             "$inc": {"cards_checked": card_count}}
        )
        return True
    
    return False

def get_user_wait_time(user_id: int) -> int:
    user_limit = user_limits_col.find_one({"user_id": user_id})
    if not user_limit:
        return 0
    
    current_time = datetime.utcnow()
    time_passed = (current_time - user_limit["last_check_time"]).total_seconds()
    time_left = max(300 - time_passed, 0)  # 5 minutes in seconds
    
    return int(time_left)

# Telegram Bot Configuration
TOKEN = "8181079198:AAFIE0MVuCPWaC0w1HbBsHlCLJKKGpbDneM"

# Per-user cooldown tracking
user_cooldowns = {}
user_locks = defaultdict(asyncio.Lock)

# /start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("Gates", callback_data='gates')],
        [InlineKeyboardButton("Help", callback_data='help')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_message = (
        f"🔥 𝐖𝐞𝐥𝐜𝐨𝐦𝐞 | 𝐅𝐍 𝐂𝐇𝐄𝐂𝐊𝐄𝐑 𝐁𝐎𝐓! \n\n"
        f"[<a href='https://t.me/FN_SH_BOT'>⌬</a>] 𝐅𝐍 𝐂𝐇𝐄𝐂𝐊𝐄𝐑 𝐕1⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 𝐖𝐞𝐥𝐜𝐨𝐦𝐞 𝐁𝐚𝐜𝐤 , <a href='tg://user?id={user.id}'>{user.first_name}</a>\n\n"
        "[⌬] 𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐔𝐒 : 𝐎𝐍 ✅\n\n"
        "[⌬] 𝐓𝐎 𝐔𝐒𝐄 𝐓𝐇𝐄 𝐁𝐎𝐓 𝐒𝐄𝐋𝐄𝐂𝐓 𝐁𝐔𝐓𝐓𝐎𝐍 𝐁𝐄𝐋𝐎𝐖\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    
    # Check if the command is from a group
    if update.message.chat.type in ['group', 'supergroup']:
        await update.message.reply_text(
            "Please use me in private messages for better experience!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Start Private Chat", url=f"https://t.me/FN_SH_BOT?start=start")]])
        )
        return
    
    await update.message.reply_text(
        welcome_message,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    log_event("INFO", "User started bot", update.effective_user.id)

# Add admin command (owner only)
async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        log_event("WARNING", "Unauthorized addadmin attempt", user_id)
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    
    try:
        target_user_id = int(context.args[0])
        
        # Check if user is already admin
        existing_admin = admins_col.find_one({"user_id": target_user_id})
        if existing_admin:
            await update.message.reply_text(f"❌ User {target_user_id} is already an admin.")
            return
        
        # Check if user exists in users collection
        user_exists = users_col.find_one({"user_id": target_user_id})
        
        # Add to admins collection
        admins_col.insert_one({
            "user_id": target_user_id,
            "added_by": user_id,
            "added_at": datetime.utcnow(),
            "is_active": True
        })
        
        if user_exists:
            await update.message.reply_text(
                f"✅ User {target_user_id} has been promoted to admin!\n"
                f"🔑 They now have access to admin commands."
            )
        else:
            await update.message.reply_text(
                f"✅ User {target_user_id} has been added as admin!\n"
                f"🔑 They now have access to admin commands.\n"
                f"⚠️ Note: User not found in users database."
            )
        
        log_event("INFO", f"Admin added: {target_user_id}", user_id)
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a numeric user ID.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        log_event("ERROR", f"Error adding admin: {str(e)}", user_id)

# Remove admin command (owner only)
async def rmadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        log_event("WARNING", "Unauthorized rmadmin attempt", user_id)
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /rmadmin <user_id>")
        return
    
    try:
        target_user_id = int(context.args[0])
        
        # Check if user is admin
        existing_admin = admins_col.find_one({"user_id": target_user_id})
        if not existing_admin:
            await update.message.reply_text(f"❌ User {target_user_id} is not an admin.")
            return
        
        # Remove from admins collection
        result = admins_col.delete_one({"user_id": target_user_id})
        
        if result.deleted_count > 0:
            await update.message.reply_text(
                f"✅ User {target_user_id} has been removed from admin role!\n"
                f"🔒 They no longer have access to admin commands."
            )
            log_event("INFO", f"Admin removed: {target_user_id}", user_id)
        else:
            await update.message.reply_text(f"❌ Failed to remove admin.")
            
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a numeric user ID.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        log_event("ERROR", f"Error removing admin: {str(e)}", user_id)

# List admins command (owner and admins)
async def listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    admins = list(admins_col.find({}))
    
    if not admins:
        await update.message.reply_text("📋 No admins found (only owner has admin rights).")
        return
    
    admin_list = "📋 <b>Admin List:</b>\n\n"
    admin_list += f"👑 <b>Owner:</b> {OWNER_ID}\n"
    admin_list += "━━━━━━━━━━━━━━━━━━━━\n"
    
    for i, admin in enumerate(admins, 1):
        admin_list += (
            f"<b>{i}. Admin ID:</b> {admin['user_id']}\n"
            f"   <b>Added by:</b> {admin.get('added_by', 'Unknown')}\n"
            f"   <b>Added at:</b> {admin.get('added_at', datetime.utcnow()).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"   <b>Status:</b> {'✅ Active' if admin.get('is_active', True) else '❌ Inactive'}\n\n"
        )
    
    admin_list += f"<b>Total Admins:</b> {len(admins)}"
    
    await update.message.reply_text(admin_list, parse_mode='HTML')
    log_event("INFO", "Admin list viewed", user_id)

# Key generation command (owner and admins)
async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        log_event("WARNING", "Unauthorized genkey attempt", user_id)
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /genkey <days>")
        return
    
    try:
        duration = int(context.args[0])
        if duration <= 0:
            await update.message.reply_text("❌ Duration must be a positive integer")
            return
            
        key = generate_key(duration)
        await update.message.reply_text(
            f"🔑 Key generated successfully!\n"
            f"Key: <code>{key}</code>\n"
            f"Duration: {duration} days\n\n"
            "User can redeem with /redeem command",
            parse_mode='HTML'
        )
        log_event("INFO", f"Key generated: {key} for {duration} days", user_id)
    except ValueError:
        await update.message.reply_text("❌ Invalid duration. Please provide a number.")
        log_event("ERROR", "Invalid duration for genkey", user_id)

# Key redemption command
async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /redeem <key>")
        return
    
    key = context.args[0].strip()
    if redeem_key(key, user_id):
        user = users_col.find_one({"user_id": user_id})
        expiry = user["expires_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
        await update.message.reply_text(
            f"🎉 Key redeemed successfully!\n"
            f"🔑 Key: <code>{key}</code>\n"
            f"⏳ Expires: {expiry}\n\n"
            "You now have access to premium features!",
            parse_mode='HTML'
        )
        log_event("INFO", f"Key redeemed: {key}", user_id)
    else:
        await update.message.reply_text(
            "❌ Invalid or already used key\n"
            "Contact @FNxELECTRA for a valid key"
        )
        log_event("WARNING", f"Failed redemption attempt: {key}", user_id)

# Delete key command (owner and admins)
async def delkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        log_event("WARNING", "Unauthorized delkey attempt", user_id)
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /delkey <user_id>")
        return
    
    try:
        target_user_id = int(context.args[0])
        result = users_col.delete_one({"user_id": target_user_id})
        
        if result.deleted_count > 0:
            await update.message.reply_text(f"✅ Subscription for user {target_user_id} has been deleted")
            log_event("INFO", f"Subscription deleted for user: {target_user_id}", user_id)
        else:
            await update.message.reply_text(f"❌ No active subscription found for user {target_user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a numeric user ID.")

# Proxy management commands (owner and admins)
async def add_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /addproxy <proxy>\nFormat: ip:port or user:pass:ip:port")
        return
    
    proxy = context.args[0].strip()
    proxies_col.insert_one({"proxy": proxy, "added_at": datetime.utcnow(), "source": "manual"})
    proxy_manager.load_proxies()
    
    await update.message.reply_text(f"✅ Proxy added successfully!\n<code>{proxy}</code>", parse_mode='HTML')
    log_event("INFO", f"Proxy added: {proxy}", user_id)

async def list_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    proxies = [p for p in proxies_col.find({})]
    if not proxies:
        await update.message.reply_text("No proxies in database")
        return
    
    proxy_list = "\n".join([f"{i+1}. {p['proxy']} (Source: {p.get('source', 'unknown')}" for i, p in enumerate(proxies)])
    await update.message.reply_text(f"<b>Proxy List:</b>\n<code>{proxy_list}</code>", parse_mode='HTML')

async def del_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /delproxy <proxy_index>")
        return
    
    try:
        index = int(context.args[0]) - 1
        proxies = [p for p in proxies_col.find({})]
        if index < 0 or index >= len(proxies):
            await update.message.reply_text("❌ Invalid proxy index")
            return
            
        proxy = proxies[index]['proxy']
        proxies_col.delete_one({"proxy": proxy})
        proxy_manager.load_proxies()
        
        await update.message.reply_text(f"✅ Proxy deleted successfully!\n<code>{proxy}</code>", parse_mode='HTML')
        log_event("INFO", f"Proxy deleted: {proxy}", user_id)
    except ValueError:
        await update.message.reply_text("❌ Invalid index. Please provide a number.")

# Reload proxies command (owner and admins)
async def reload_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    proxy_manager.load_proxies()
    await update.message.reply_text(f"✅ Proxies reloaded! Total: {len(proxy_manager.proxies)}")
    log_event("INFO", "Proxies reloaded", user_id)

# Broadcast command (owner and admins)
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        log_event("WARNING", "Unauthorized broadcast attempt", user_id)
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = ' '.join(context.args)
    users = users_col.distinct("user_id")
    success = 0
    failed = 0
    
    broadcast_col.insert_one({
        "message": message,
        "sent_by": user_id,
        "sent_at": datetime.utcnow(),
        "total_users": len(users)
    })
    
    status_msg = await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")
    
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            success += 1
        except Exception as e:
            log_event("ERROR", f"Broadcast failed for {user_id}: {str(e)}", None)
            failed += 1
        await asyncio.sleep(0.1)  # Rate limiting
    
    await status_msg.edit_text(
        f"📢 Broadcast completed!\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}"
    )
    log_event("INFO", f"Broadcast sent: {success} success, {failed} failed", user_id)

# Check user access middleware
async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
    user_id = update.effective_user.id
    
    # Extract command if it's a text message starting with '/'
    command = None
    if update.message and update.message.text and update.message.text.startswith('/'):
        parts = update.message.text.split()
        if parts:
            cmd = parts[0][1:]  # remove the leading '/'
            # Remove bot username if present
            if '@' in cmd:
                cmd = cmd.split('@')[0]
            command = cmd

    # Allow access to these commands without a key
    if command in ["start", "help", "redeem", "genkey"]:
        return await handler(update, context)

    # Allow owner and admins full access to all commands
    if user_id == OWNER_ID or is_admin(user_id):
        log_event("INFO", f"Admin/Owner access for {command}", user_id)
        return await handler(update, context)

    # Check if user has valid access
    if has_valid_access(user_id):
        log_event("INFO", f"Access granted for {command}", user_id)
        return await handler(update, context)

    # Access denied
    user = users_col.find_one({"user_id": user_id})
    if user and "expires_at" in user:
        expiry = user["expires_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
        message = (
            "⛔ Subscription Expired!\n"
            f"Your access expired on: {expiry}\n\n"
            "🔑 Renew your subscription with /redeem <key>\n"
            "Contact @FNxELECTRA for premium keys"
        )
    else:
        message = (
            "⛔ Premium Access Required!\n\n"
            "🔑 Purchase a key to unlock premium features\n"
            "Use /redeem <key> after purchase\n"
            "Contact @FNxELECTRA for premium keys"
        )
    
    await update.message.reply_text(message)
    log_event("WARNING", f"Access denied for {command}", user_id)

# /sh command handler for single card check (gate1)
async def single_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    
    # Check cooldown
    if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < 10:
        wait_time = 10 - (current_time - user_cooldowns[user_id])
        await update.message.reply_text(f"⏳ Please wait {wait_time:.1f} seconds before checking another card.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a card in the format: /sh 4242424242424242|01|29|308")
        return
    
    card = " ".join(context.args)
    checking_msg = await update.message.reply_text("🔍 Checking Your Card. Please Wait....")
    
    # Use a new task to free up the main thread
    asyncio.create_task(process_single_check(update, context, card, user_id))
    
    # Update cooldown
    user_cooldowns[user_id] = time.time()

async def process_single_check(update, context, card, user_id):
    # Acquire lock for this user only
    lock = user_locks[user_id]
    async with lock:
        result = await sh(card, proxy_manager)
    
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id + 1)
    except:
        pass

    if "Invalid" in result.message or "failed" in result.message or "error" in result.message.lower():
        await update.message.reply_text(result.message)
        return

    # Use structured data from ShResult
    status = result.status
    response = result.response
    card_info = result.card_info
    issuer = result.issuer
    country_display = f"{result.country}{result.flag} - {result.currency}"
    
    checked_by = f"<a href='tg://user?id={update.effective_user.id}'>{update.effective_user.first_name}</a>"
    time_taken = f"{result.elapsed_time:.2f}s"
    proxy_status = result.proxy_status

    if status == "Charged":
        response_text = (
            f"𝐂𝐇𝐀𝐑𝐆𝐄𝐃 1$🔥🔥\n\n"
            f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
            f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» Charged 🔥\n"
            f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 1$\n"
            f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response}\n\n"
            f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
            f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
            f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
            f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
            f"[⌬]𝗣𝗿𝗼𝘅𝘆 -» {proxy_status}\n"
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
    elif status == "Approved":
        response_text = (
            f"𝐀𝐏𝐏𝐑𝐎𝐕𝐄𝐃 ✅\n\n"
            f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
            f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» Approved ✅\n"
            f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 1$\n"
            f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response}\n\n"
            f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
            f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
            f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
            f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
            f"[⌬]𝗣𝗿𝗼𝘅𝘆 -» {proxy_status}\n"
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
    else:
        response_text = (
            f"𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝 ❌\n\n"
            f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
            f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘁𝘀 -» Declined ❌\n"
            f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 1$\n"
            f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response}\n\n"
            f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
            f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
            f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
            f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
            f"[⌬]𝗣𝗿𝗼𝘅𝘆 -» {proxy_status}\n"
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
    
    await update.message.reply_text(response_text, parse_mode='HTML')

# /sm command handler for single card check (gate2)
async def single_check_gate2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    
    # Check cooldown
    if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < 10:
        wait_time = 10 - (current_time - user_cooldowns[user_id])
        await update.message.reply_text(f"⏳ Please wait {wait_time:.1f} seconds before checking another card.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a card in the format: /sm 4242424242424242|01|29|308")
        return
    
    card = " ".join(context.args)
    checking_msg = await update.message.reply_text("🔍 Checking Your Card. Please Wait....")
    
    # Use a new task to free up the main thread
    asyncio.create_task(process_single_check_gate2(update, context, card, user_id))
    
    # Update cooldown
    user_cooldowns[user_id] = time.time()

async def process_single_check_gate2(update, context, card, user_id):
    # Acquire lock for this user only
    lock = user_locks[user_id]
    async with lock:
        result = await sh14(card, proxy_manager)  # Use the new gate function
    
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id + 1)
    except:
        pass

    if "Invalid" in result.message or "failed" in result.message or "error" in result.message.lower():
        await update.message.reply_text(result.message)
        return

    # Use structured data from ShResult
    status = result.status
    response = result.response
    card_info = result.card_info
    issuer = result.issuer
    country_display = f"{result.country}{result.flag} - {result.currency}"
    
    checked_by = f"<a href='tg://user?id={update.effective_user.id}'>{update.effective_user.first_name}</a>"
    time_taken = f"{result.elapsed_time:.2f}s"
    proxy_status = result.proxy_status

    if status == "Charged":
        response_text = (
            f"𝐂𝐇𝐀𝐑𝐆𝐄𝐃 14$🔥🔥\n\n"
            f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
            f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» Charged 🔥\n"
            f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 14$\n"
            f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response}\n\n"
            f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
            f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
            f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
            f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
            f"[⌬]𝗣𝗿𝗼𝘅𝘆 -» {proxy_status}\n"
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
    elif status == "Approved":
        response_text = (
            f"𝐀𝐏𝐏𝐑𝐎𝐕𝐄𝐃 ✅\n\n"
            f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
            f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» Approved ✅\n"
            f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 14$\n"
            f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response}\n\n"
            f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
            f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
            f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
            f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
            f"[⌬]𝗣𝗿𝗼𝘅𝘆 -» {proxy_status}\n"
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
    else:
        response_text = (
            f"𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝 ❌\n\n"
            f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
            f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» Declined ❌\n"
            f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 14$\n"
            f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response}\n\n"
            f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
            f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
            f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
            f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
            f"[⌬]𝗣𝗿𝗼𝘅𝘆 -» {proxy_status}\n"
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
    
    await update.message.reply_text(response_text, parse_mode='HTML')

# /lv command handler for Live Check (gate3)
async def single_check_gate3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    
    # Check cooldown
    if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < 10:
        wait_time = 10 - (current_time - user_cooldowns[user_id])
        await update.message.reply_text(f"⏳ Please wait {wait_time:.1f} seconds before checking another card.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a card in the format: /lv 4242424242424242|01|29|308")
        return
    
    card = " ".join(context.args)
    checking_msg = await update.message.reply_text("🔍 Checking Your Card. Please Wait....")
    
    # Use a new task to free up the main thread
    asyncio.create_task(process_single_check_gate3(update, context, card, user_id))
    
    # Update cooldown
    user_cooldowns[user_id] = time.time()

async def process_single_check_gate3(update, context, card, user_id):
    # Acquire lock for this user only
    lock = user_locks[user_id]
    async with lock:
        result = await live_check(card)  # Removed proxy_manager
    
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id + 1)
    except:
        pass

    if result.status == "Error":
        await update.message.reply_text(f"❌ Error: {result.message}")
        return

    # Create dynamic header based on status
    if result.status == "Live ✅":
        header = "𝐋𝐢𝐯𝐞 ✅"
    elif result.status == "Dead ❌":
        header = "𝐃𝐞𝐚𝐝 ❌"
    else:
        header = "𝐄𝐫𝐫𝐨𝐫 ⚠️"

    # Format country display with flag emoji
    country_display = f"{result.country} {result.flag} - {result.currency}" if result.country else "Unknown"
    
    # Format card info
    card_info = result.card_info or "Unknown"
    
    # Format issuer
    issuer = result.issuer or "Unknown"
    
    checked_by = f"<a href='tg://user?id={update.effective_user.id}'>{update.effective_user.first_name}</a>"
    time_taken = f"{result.elapsed_time:.2f}s"
    
    # Updated response with dynamic header
    response_text = (
        f"{header}\n\n"
        f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
        f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» {result.status}\n"
        f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Live Check\n"
        f"[ϟ]𝗥𝗲𝗮𝘀𝗼𝗻 -» {result.message}\n\n"
        f"[ϟ]𝗜𝗻𝗳𝗼 -» {card_info}\n"
        f"[ϟ]𝗜𝘀𝘀𝘂𝗲𝗿 -» {issuer} 🏛\n"
        f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n\n"
        f"[⌬]𝗧𝗶𝗺𝗲 -» {time_taken}\n"
        f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
        f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
    )
    
    await update.message.reply_text(response_text, parse_mode='HTML')

# /msh command handler for multi-card check (gate1)
async def multi_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    
    # Check cooldown
    if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < 10:
        wait_time = 10 - (current_time - user_cooldowns[user_id])
        await update.message.reply_text(f"⏳ Please wait {wait_time:.1f} seconds before checking more cards.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide cards in the format:\n/msh 4242424242424242|02|30|885\n4242424242424242|09|27|807\n... (up to 5 cards)")
        return
    
    # Parse cards
    text = update.message.text
    lines = text.split('\n')
    
    # Skip the first line (command) and take up to 5 cards
    cards = []
    for line in lines[1:]:
        if line.strip() and len(cards) < 5:
            cards.append(line.strip())
    
    if not cards:
        await update.message.reply_text("❌ No valid cards provided")
        return
    
    # Update cooldown
    user_cooldowns[user_id] = time.time()
    
    # Start processing in background
    asyncio.create_task(process_multi_check(update, context, cards, user_id))

async def process_multi_check(update, context, cards, user_id):
    # Acquire lock for this user only
    lock = user_locks[user_id]
    async with lock:
        # Create status message
        status_msg = await update.message.reply_text("🔍 Starting multi-card check...")
        
        results = []
        for i, card in enumerate(cards):
            # Update status
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"🔍 Checking card {i+1}/{len(cards)}..."
            )
            
            # Check card
            result = await sh(card, proxy_manager)
            results.append((card, result))
            
            # Wait 10 seconds between cards
            if i < len(cards) - 1:
                await asyncio.sleep(10)
        
        # Format results
        result_text = ""
        for card, res in results:
            # Determine status display
            if res.status == "Charged":
                status = "Charged 🔥"
            elif res.status == "Approved":
                status = "Approved ✅"
            elif res.status == "Declined":
                status = "Declined ❌"
            else:
                status = "Error ❓"
                
            # Use response if available, otherwise use first line of message
            response_str = res.response if res.response else res.message.split('\n')[0]
            
            result_text += (
                f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
                f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» {status}\n"
                f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 1$\n"
                f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
        
        # Add footer
        checked_by = f"<a href='tg://user?id={user_id}'>{update.effective_user.first_name}</a>"
        result_text += (
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
        
        # Send results
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id
        )
        await update.message.reply_text(result_text, parse_mode='HTML')

# /msm command handler for multi-card check (gate2)
async def multi_check_gate2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    
    # Check cooldown
    if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < 10:
        wait_time = 10 - (current_time - user_cooldowns[user_id])
        await update.message.reply_text(f"⏳ Please wait {wait_time:.1f} seconds before checking more cards.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide cards in the format:\n/msm 4242424242424242|02|30|885\n4242424242424242|09|27|807\n... (up to 5 cards)")
        return
    
    # Parse cards
    text = update.message.text
    lines = text.split('\n')
    
    # Skip the first line (command) and take up to 5 cards
    cards = []
    for line in lines[1:]:
        if line.strip() and len(cards) < 5:
            cards.append(line.strip())
    
    if not cards:
        await update.message.reply_text("❌ No valid cards provided")
        return
    
    # Update cooldown
    user_cooldowns[user_id] = time.time()
    
    # Start processing in background
    asyncio.create_task(process_multi_check_gate2(update, context, cards, user_id))

async def process_multi_check_gate2(update, context, cards, user_id):
    # Acquire lock for this user only
    lock = user_locks[user_id]
    async with lock:
        # Create status message
        status_msg = await update.message.reply_text("🔍 Starting multi-card check...")
        
        results = []
        for i, card in enumerate(cards):
            # Update status
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"🔍 Checking card {i+1}/{len(cards)}..."
            )
            
            # Check card
            result = await sh14(card, proxy_manager)  # Use the new gate function
            results.append((card, result))
            
            # Wait 10 seconds between cards
            if i < len(cards) - 1:
                await asyncio.sleep(10)
        
        # Format results
        result_text = ""
        for card, res in results:
            # Determine status display
            if res.status == "Charged":
                status = "Charged 🔥"
            elif res.status == "Approved":
                status = "Approved ✅"
            elif res.status == "Declined":
                status = "Declined ❌"
            else:
                status = "Error ❓"
                
            # Use response if available, otherwise use first line of message
            response_str = res.response if res.response else res.message.split('\n')[0]
            
            result_text += (
                f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
                f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» {status}\n"
                f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Shopify 14$\n"
                f"[ϟ]𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 -» {response_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
        
        # Add footer
        checked_by = f"<a href='tg://user?id={user_id}'>{update.effective_user.first_name}</a>"
        result_text += (
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
        
        # Send results
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id
        )
        await update.message.reply_text(result_text, parse_mode='HTML')

# /mlv command handler for Live Check multi-card (gate3)
async def multi_check_gate3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    
    # Check cooldown
    if user_id in user_cooldowns and current_time - user_cooldowns[user_id] < 10:
        wait_time = 10 - (current_time - user_cooldowns[user_id])
        await update.message.reply_text(f"⏳ Please wait {wait_time:.1f} seconds before checking more cards.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide cards in the format:\n/mlv 4242424242424242|02|30|885\n4242424242424242|09|27|807\n... (up to 10 cards)")
        return
    
    # Parse cards
    text = update.message.text
    lines = text.split('\n')
    
    # Skip the first line (command) and take up to 10 cards
    cards = []
    for line in lines[1:]:
        if line.strip() and len(cards) < 10:
            cards.append(line.strip())
    
    if not cards:
        await update.message.reply_text("❌ No valid cards provided")
        return
    
    # Update cooldown
    user_cooldowns[user_id] = time.time()
    
    # Start processing in background
    asyncio.create_task(process_multi_check_gate3(update, context, cards, user_id))

async def process_multi_check_gate3(update, context, cards, user_id):
    # Acquire lock for this user only
    lock = user_locks[user_id]
    async with lock:
        # Create status message
        status_msg = await update.message.reply_text("🔍 Starting multi-card check...")
        
        results = []
        for i, card in enumerate(cards):
            # Update status
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"🔍 Checking card {i+1}/{len(cards)}..."
            )
            
            # Check card
            result = await live_check(card)  # Removed proxy_manager
            results.append((card, result))
            
            # Wait 3 seconds between cards
            if i < len(cards) - 1:
                await asyncio.sleep(3)
        
        # Format results
        result_text = ""
        for card, res in results:
            # Determine status display
            if res.status == "Live ✅":
                status = "Live ✅"
            elif res.status == "Dead ❌":
                status = "Dead ❌"
            else:
                status = "Unknown"
            
            # Format country display with flag emoji
            country_display = f"{res.country} {res.flag} - {res.currency}" if res.country else "Unknown"
            
            result_text += (
                f"[ϟ]𝗖𝗮𝗿𝗱 -» <code>{card}</code>\n"
                f"[ϟ]𝗦𝘁𝗮𝘁𝘂𝘀 -» {status}\n"
                f"[ϟ]𝗚𝗮𝘁𝗲𝘄𝗮𝘆 -» Live Check\n"
                f"[ϟ]𝗥𝗲𝗮𝘀𝗼𝗻 -» {res.message}\n"
                f"[ϟ]𝗜𝗻𝗳𝗼 -» {res.card_info}\n"
                f"[ϟ]𝗖𝗼𝘂𝗻𝘁𝗿𝘆 -» {country_display}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
        
        # Add footer
        checked_by = f"<a href='tg://user?id={user_id}'>{update.effective_user.first_name}</a>"
        result_text += (
            f"[⌬]𝗖𝗵𝐞𝐜𝐤𝐞𝐝 𝐁𝐲 -» {checked_by}\n"
            f"[み]𝗕𝗼𝘁 -» <a href='https://t.me/FN_SH_BOT'>𝙁𝙉 𝙎𝙃𝙊𝙋𝙄𝙁𝙔</a>"
        )
        
        # Send results
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id
        )
        await update.message.reply_text(result_text, parse_mode='HTML')

# /stop command handler
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Cooldowns are automatically handled by time, so just acknowledge
    await update.message.reply_text("⏱️ Cooldown tracking active. Next check available in 10 seconds after last check.")
    log_event("INFO", "Stop command acknowledged", user_id)

# Inline button handler
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'help':
        help_text = (
            "🤖 <b>FN_SH_BOT Help</b>\n\n"
            "🔑 <b>Key System</b>\n"
            "1. Use /redeem &lt;key&gt; to activate premium access\n"
            "2. Contact @FNxELECTRA for keys\n\n"
            "💳 <b>Card Checking</b>\n"
            "1. Single check: /sh 4242424242424242|01|29|308\n"
            "2. Multi check (up to 5): /msh with cards separated by new lines\n"
            "3. Format: 4242424242424242|01|29|308\n\n"
            "⚠️ <b>Cooldowns</b>\n"
            "- 10 seconds between card checks\n"
            "- Use /stop to view cooldown status\n\n"
            "⚙️ <b>Other Commands</b>\n"
            "- /start: Show main menu\n"
            "- /help: Show this help message"
        )
        await query.edit_message_text(help_text, parse_mode='HTML')
    
    elif query.data == 'gates':
        keyboard = [
            [InlineKeyboardButton("Shopify 1$", callback_data='gate1')],
            [InlineKeyboardButton("Shopify 14$", callback_data='gate2')],
            [InlineKeyboardButton("Live Check", callback_data='gate3')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = (
            "𝐆𝐚𝐭𝐞𝐰𝐚𝐲𝐬 | 𝐅𝐍 𝐂𝐇𝐄𝐂𝐊𝐄𝐑\n"
            "[⌬] Choose the type of gateway you want to explore.\n"
            "[⌬] A variety of powerful and useful tools await you!\n\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    elif query.data == 'gate1':
        keyboard = [
            [InlineKeyboardButton("Back 🔙", callback_data='gates')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = (
            "𝐒𝐡𝐨𝐩𝐢𝐟𝐲 𝐆𝐚𝐭𝐞𝐬 | 𝐅𝐍 𝐂𝐇𝐄𝐂𝐊𝐄𝐑\n\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Single Check \n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Format: /sh\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Status: 🟢\n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Mass Check Format \n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Command: /msh\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Status: 🟢\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Format [5]: /msh\n"
            "cc|mm|yy|cvv\n"
            "cc|mm|yy|cvv\n\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    elif query.data == 'gate2':
        keyboard = [
            [InlineKeyboardButton("Back 🔙", callback_data='gates')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = (
            "𝐒𝐡𝐨𝐩𝐢𝐟𝐲 𝐆𝐚𝐭𝐞𝐬 | 𝐅𝐍 𝐂𝐇𝐄𝐂𝐊𝐄𝐑\n\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Single Check \n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Format: /sm\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Status: 🟢\n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Mass Check Format \n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Command: /msm\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Status: 🟢\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Format [5]: /msm\n"
            "cc|mm|yy|cvv\n"
            "cc|mm|yy|cvv\n\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    elif query.data == 'gate3':
        keyboard = [
            [InlineKeyboardButton("Back 🔙", callback_data='gates')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = (
            "𝐋𝐢𝐯𝐞 𝐂𝐡𝐞𝐜𝐤 | 𝐅𝐍 𝐂𝐇𝐄𝐂𝐊𝐄𝐑\n\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Single Check \n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Format: /lv\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Status: 🟢\n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Mass Check Format \n"
            "- - - - - - - - - - - - - - -\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Command: /mlv\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Status: 🟢\n"
            "[<a href='https://t.me/FN_SH_BOT'>⌬</a>] Format [10]: /mlv\n"
            "cc|mm|yy|cvv\n"
            "cc|mm|yy|cvv\n\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    user_id = update.effective_user.id if update else None
    log_event("ERROR", f"Bot error: {str(error)}", user_id)
    if update:
        await update.message.reply_text("⚠️ An error occurred. Please try again later.")
    logger.error(f"Update {update} caused error {error}")

# Main function to run the bot
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers with access control
    application.add_handler(CommandHandler("start", start))
    
    # Admin management commands (owner only)
    application.add_handler(CommandHandler("addadmin", addadmin))
    application.add_handler(CommandHandler("rmadmin", rmadmin))
    application.add_handler(CommandHandler("listadmins", listadmins))
    
    # Owner/Admin commands
    application.add_handler(CommandHandler("genkey", genkey))
    application.add_handler(CommandHandler("redeem", redeem))
    application.add_handler(CommandHandler("delkey", delkey))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("addproxy", add_proxy))
    application.add_handler(CommandHandler("delproxy", del_proxy))
    application.add_handler(CommandHandler("listproxies", list_proxies))
    application.add_handler(CommandHandler("reloadproxies", reload_proxies))
    
    # Gate1 commands
    application.add_handler(CommandHandler("sh", 
        lambda update, context: check_access(update, context, single_check)
    ))
    application.add_handler(CommandHandler("msh", 
        lambda update, context: check_access(update, context, multi_check)
    ))
    
    # Gate2 commands
    application.add_handler(CommandHandler("sm", 
        lambda update, context: check_access(update, context, single_check_gate2)
    ))
    application.add_handler(CommandHandler("msm", 
        lambda update, context: check_access(update, context, multi_check_gate2)
    ))
    
    # Gate3 commands
    application.add_handler(CommandHandler("lv", 
        lambda update, context: check_access(update, context, single_check_gate3)
    ))
    application.add_handler(CommandHandler("mlv", 
        lambda update, context: check_access(update, context, multi_check_gate3)
    ))
    
    application.add_handler(CommandHandler("stop", stop))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(button, pattern='^help$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^gates$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^gate1$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^gate2$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^gate3$'))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Start the bot
    log_event("INFO", "Bot started successfully", None)
    application.run_polling()

if __name__ == '__main__':
    main()
