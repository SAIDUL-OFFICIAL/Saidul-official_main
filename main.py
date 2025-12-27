import os
import telebot
import requests
import time
import threading
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, jsonify
import logging
import sys

# --- Added for PostgreSQL persistence ---
import psycopg2
from psycopg2.extras import RealDictCursor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # set this in Render environment variables

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN not found! Please set your bot token in environment variables.")
    sys.exit(1)

# Persistence flag: if DATABASE_URL provided, we'll persist data to Postgres
PERSISTENCE = bool(DATABASE_URL)

REQUIRED_CHANNELS = ["@saidul_34",]
GROUP_JOIN_LINK = "https://t.me/saidul_miah"
OWNER_ID = 8408849795

bot = telebot.TeleBot(BOT_TOKEN)
like_tracker = {}   # in-memory cache (will be loaded from DB if persistence enabled)
vip_users = {}
auto_like_jobs = {}

# Flask app for webhook
app = Flask(__name__)

# === DATABASE HELPERS ===

def get_db():
    """Create a new DB connection. Caller must close it."""
    if not PERSISTENCE:
        raise RuntimeError("Database not configured")
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        raise


def init_db():
    """Initialize DB tables if persistence enabled."""
    if not PERSISTENCE:
        logger.info("Persistence disabled: DATABASE_URL not set. Running in-memory only.")
        return

    conn = get_db()
    cur = conn.cursor()
    # jobs table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        user_id BIGINT,
        region TEXT,
        uid TEXT,
        interval_hours INT,
        chat_id BIGINT,
        next_run TIMESTAMP,
        created_at TIMESTAMP
    )
    """)

    # vip_users table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vip_users (
        user_id BIGINT PRIMARY KEY,
        limit_per_day INT,
        expires TIMESTAMP
    )
    """)

    # usage tracker
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_tracker (
        user_id BIGINT PRIMARY KEY,
        used INT,
        last_used TIMESTAMP
    )
    """)

    conn.commit()
    cur.close()
    conn.close()
    logger.info("✅ Database initialized (if enabled).")


# === LOAD FROM DB ON STARTUP ===

def load_jobs_from_db():
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs")
        rows = cur.fetchall()
        for row in rows:
            job_id = row['job_id']
            auto_like_jobs[job_id] = {
                'user_id': row['user_id'],
                'region': row['region'],
                'uid': row['uid'],
                'interval_hours': row['interval_hours'],
                'chat_id': row['chat_id'],
                'next_run': row['next_run'],
                'created_at': row['created_at']
            }
        cur.close()
        conn.close()
        logger.info(f"Loaded {len(auto_like_jobs)} jobs from DB")
    except Exception as e:
        logger.error(f"Failed to load jobs from DB: {e}")


def load_vip_from_db():
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM vip_users")
        rows = cur.fetchall()
        for row in rows:
            uid = row['user_id']
            vip_users[uid] = {
                'limit': row['limit_per_day'],
                'expires': row['expires']
            }
        cur.close()
        conn.close()
        logger.info(f"Loaded {len(vip_users)} VIP users from DB")
    except Exception as e:
        logger.error(f"Failed to load VIP users from DB: {e}")


def load_usage_from_db():
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM usage_tracker")
        rows = cur.fetchall()
        for row in rows:
            uid = row['user_id']
            like_tracker[uid] = {
                'used': row['used'],
                'last_used': row['last_used']
            }
        cur.close()
        conn.close()
        logger.info(f"Loaded {len(like_tracker)} usage rows from DB")
    except Exception as e:
        logger.error(f"Failed to load usage from DB: {e}")


# === SAVE helpers ===

def save_job_to_db(job_id, job_data):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO jobs (job_id, user_id, region, uid, interval_hours, chat_id, next_run, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (job_id) DO UPDATE SET
                next_run = EXCLUDED.next_run,
                interval_hours = EXCLUDED.interval_hours,
                chat_id = EXCLUDED.chat_id
        """, (
            job_id,
            job_data['user_id'],
            job_data['region'],
            job_data['uid'],
            job_data['interval_hours'],
            job_data['chat_id'],
            job_data['next_run'],
            job_data['created_at']
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save job to DB: {e}")


def delete_job_from_db(job_id):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM jobs WHERE job_id=%s", (job_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to delete job from DB: {e}")


def save_vip_to_db(user_id, limit, expires_dt):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vip_users (user_id, limit_per_day, expires)
            VALUES (%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET limit_per_day=EXCLUDED.limit_per_day, expires=EXCLUDED.expires
        """, (user_id, limit, expires_dt))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save VIP to DB: {e}")


def delete_vip_from_db(user_id):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM vip_users WHERE user_id=%s", (user_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to delete VIP from DB: {e}")


def save_usage_to_db(user_id, usage):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usage_tracker (user_id, used, last_used)
            VALUES (%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET used=EXCLUDED.used, last_used=EXCLUDED.last_used
        """, (user_id, usage['used'], usage['last_used']))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save usage to DB: {e}")


# === UTILS (unchanged logic) ===

def is_user_in_channel(user_id):
    try:
        for channel in REQUIRED_CHANNELS:
            member = bot.get_chat_member(channel, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        return True
    except Exception as e:
        logger.error(f"Join check failed: {e}")
        return False


def call_api(region, uid):
    url = f"https://like-bd-api.vercel.app/like?uid={uid}&server_name={region}"
    try:
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            return {"⚠️Invalid": " Maximum likes reached for today. Please try again tomorrow."}
        return response.json()
    except requests.exceptions.RequestException:
        return {"error": "API Failed. Please try again later."}
    except ValueError:
        return {"error": "Invalid JSON response."}


def get_user_limit(user_id):
    now = datetime.now()
    vip = vip_users.get(user_id)
    if vip and vip['expires'] > now:
        return vip['limit']
    return 1


# === Threads: reset_limits and auto_like_scheduler (modified to persist) ===

def reset_limits():
    while True:
        try:
            # Calculate time until next 00:00 UTC
            now_utc = datetime.utcnow()
            next_reset = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (next_reset - now_utc).total_seconds()
            
            time.sleep(sleep_seconds)
            
            if PERSISTENCE:
                # Reset all usage at 00:00 UTC
                conn = get_db()
                cur = conn.cursor()
                reset_time = datetime.utcnow()
                cur.execute("UPDATE usage_tracker SET used=0, last_used=%s", (reset_time,))
                conn.commit()
                cur.close()
                conn.close()
                # update in-memory cache
                for uid in list(like_tracker.keys()):
                    like_tracker[uid] = {'used': 0, 'last_used': reset_time}
                logger.info("✅ Daily limits reset at 00:00 UTC in DB and memory.")
            else:
                like_tracker.clear()
                logger.info("✅ Daily limits reset at 00:00 UTC (in-memory).")
        except Exception as e:
            logger.error(f"Error in reset_limits thread: {e}")


def auto_like_scheduler():
    """Background thread to handle auto like jobs. Persists next_run back to DB when job executed."""
    while True:
        try:
            current_time = datetime.now()
            jobs_to_execute = []

            for job_id, job_data in list(auto_like_jobs.items()):
                # job_data['next_run'] should be datetime
                if current_time >= job_data['next_run']:
                    jobs_to_execute.append((job_id, job_data))

            for job_id, job_data in jobs_to_execute:
                try:
                    user_id = job_data['user_id']
                    region = job_data['region']
                    uid = job_data['uid']
                    chat_id = job_data['chat_id']

                    # Check usage with 00:00 UTC reset
                    current_time_utc = datetime.utcnow()
                    usage = like_tracker.get(user_id, {"used": 0, "last_used": current_time_utc - timedelta(days=1)})
                    last_used_date = usage['last_used'].date()
                    current_date = current_time_utc.date()
                    if current_date > last_used_date:
                        usage['used'] = 0

                    max_limit = get_user_limit(user_id)
                    if usage['used'] >= max_limit:
                        bot.send_message(chat_id, f"⚠️ Auto Like Failed for UID {uid}: Daily limit exceeded!")
                        # schedule next run anyway or skip? we'll schedule next run
                        auto_like_jobs[job_id]['next_run'] = current_time + timedelta(hours=job_data['interval_hours'])
                        save_job_to_db(job_id, auto_like_jobs[job_id])
                        continue

                    response = call_api(region, uid)

                    if 'error' in response:
                        bot.send_message(chat_id, f"⚠️ Auto Like Failed for UID {uid}: {response['error']}")
                        # schedule next
                        auto_like_jobs[job_id]['next_run'] = current_time + timedelta(hours=job_data['interval_hours'])
                        save_job_to_db(job_id, auto_like_jobs[job_id])
                        continue

                    if not isinstance(response, dict) or response.get('status') != 1:
                        bot.send_message(chat_id, f"❌ Auto Like Failed for UID {uid}: Maximum likes reached for today.")
                        auto_like_jobs[job_id]['next_run'] = current_time + timedelta(hours=job_data['interval_hours'])
                        save_job_to_db(job_id, auto_like_jobs[job_id])
                        continue

                    # Success - update usage
                    usage['used'] += 1
                    usage['last_used'] = current_time_utc
                    like_tracker[user_id] = usage
                    save_usage_to_db(user_id, usage)

                    player_name = response.get('PlayerNickname', 'N/A')
                    likes_given = str(response.get('LikesGivenByAPI', 'N/A'))
                    total_likes = str(response.get('LikesafterCommand', 'N/A'))
                    utc_time = datetime.utcnow()

                    auto_msg = f"""🤖 *Auto Like Executed Successfully*\n\n👤 *Name:* `{player_name}`\n🆔 *UID:* `{uid}`\n📈 *Likes Added:* `{likes_given}`\n🗿 *Total Likes Now:* `{total_likes}`\n⏱ *Executed At:* `{utc_time.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n🔁 *Next Auto Like:* `{(utc_time + timedelta(hours=job_data['interval_hours'])).strftime('%Y-%m-%d %H:%M:%S')} UTC`\n🔐 *Remaining Requests:* `{max_limit - usage['used']}`"""

                    bot.send_message(chat_id, auto_msg, parse_mode='Markdown')

                    # Schedule next run and persist
                    auto_like_jobs[job_id]['next_run'] = current_time + timedelta(hours=job_data['interval_hours'])
                    save_job_to_db(job_id, auto_like_jobs[job_id])

                except Exception as e:
                    logger.error(f"Auto like execution error for job {job_id}: {e}")

        except Exception as e:
            logger.error(f"Auto like scheduler error: {e}")

        time.sleep(60)


# Initialize DB and load persisted data (if enabled)
try:
    init_db()
    load_jobs_from_db()
    load_vip_from_db()
    load_usage_from_db()
    # Don't load allowed groups anymore
    logger.info("✅ Database initialization completed successfully")
except Exception as e:
    logger.error(f"❌ Database initialization failed: {e}")
    if PERSISTENCE:
        logger.warning("⚠️ Running without database persistence due to connection issues")

# Start background threads
threading.Thread(target=reset_limits, daemon=True).start()
threading.Thread(target=auto_like_scheduler, daemon=True).start()

# === FLASK ROUTES ===

@app.route('/')
def home():
    return jsonify({
        'status': 'Bot is running',
        'bot': 'Free Fire Likes Bot',
        'health': 'OK'
    })

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_str = request.get_data().decode('UTF-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return '', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return '', 500


# === TELEGRAM COMMANDS (original logic preserved, added DB persistence where needed) ===

@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    if not is_user_in_channel(user_id):
        markup = InlineKeyboardMarkup()
        for channel in REQUIRED_CHANNELS:
            markup.add(InlineKeyboardButton(f"🔗 Join {channel}", url=f"https://t.me/{channel.strip('@')}") )
        bot.reply_to(message, "📢 Channel Membership Required\nTo use this bot, you must join all our channels first", reply_markup=markup, parse_mode="Markdown")
        return
    if user_id not in like_tracker:
        like_tracker[user_id] = {"used": 0, "last_used": datetime.now() - timedelta(days=1)}
        # persist initial usage
        save_usage_to_db(user_id, like_tracker[user_id])
    bot.reply_to(message, "✅ You're verified! Use /like to send likes.", parse_mode="Markdown")


@bot.message_handler(commands=['like'])
def handle_like(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    args = message.text.split()

    # Only allow in groups, not in private messages (except owner)
    if message.chat.type == "private" and message.from_user.id != OWNER_ID:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔗 Join Official Group", url=GROUP_JOIN_LINK))
        markup.add(InlineKeyboardButton("➕ Add to your group", url=f"https://t.me/{bot.get_me().username}?startgroup=true"))
        bot.reply_to(message, "❌ Sorry! command is not allowed here.\n\nJoin our official group or add me to your group:", reply_markup=markup)
        return

    if not is_user_in_channel(user_id):
        markup = InlineKeyboardMarkup()
        for channel in REQUIRED_CHANNELS:
            markup.add(InlineKeyboardButton(f"🔗 Join {channel}", url=f"https://t.me/{channel.strip('@')}") )
        bot.reply_to(message, "❌ You must join all our channels to use this command.", reply_markup=markup, parse_mode="Markdown")
        return

    if len(args) != 3:
        bot.reply_to(message, "❌ Format: `/like server_name uid`", parse_mode="Markdown")
        return

    region, uid = args[1], args[2]
    if not region.isalpha() or not uid.isdigit():
        bot.reply_to(message, "⚠️ Invalid input. Use: `/like server_name uid`", parse_mode="Markdown")
        return

    threading.Thread(target=process_like, args=(message, region, uid)).start()


def process_like(message, region, uid):
    user_id = message.from_user.id
    now_utc = datetime.utcnow()
    usage = like_tracker.get(user_id, {"used": 0, "last_used": now_utc - timedelta(days=1)})

    # Check if it's a new day (00:00 UTC reset)
    last_used_date = usage["last_used"].date()
    current_date = now_utc.date()
    if current_date > last_used_date:
        usage["used"] = 0

    max_limit = get_user_limit(user_id)
    if usage["used"] >= max_limit:
        next_reset = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        remaining_time = next_reset - now_utc
        hours, remainder = divmod(remaining_time.total_seconds(), 3600)
        minutes = divmod(remainder, 60)[0]

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💎 Buy VIP", url="https://t.me/saidulbhai34"))
        
        bot.reply_to(message, f"⚠️ You have exceeded your daily request limit!\n⏳ Next request available at: 00:00 UTC (in {int(hours)}h {int(minutes)}m)\n🔐Buy VIP or wait for the cooldown.", reply_markup=markup)
        return

    processing_msg = bot.reply_to(message, "⏳ Please wait... Sending likes...")
    time.sleep(1)
    response = call_api(region, uid)

    if "error" in response:
        try:
            bot.edit_message_text(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id,
                text=f"⚠️ API Error: {response['error']}"
            )
        except:
            bot.reply_to(message, f"⚠️ API Error: {response['error']}")
        return

    if not isinstance(response, dict) or response.get("status") != 1:
        try:
            bot.edit_message_text(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id,
                text="❌ UID has already received its max amount of likes. Limit reached for today, try another UID or after 24 hrs."
            )
        except:
            bot.reply_to(message, "⚠️ Invalid UID or unable to fetch data.")
        return

    try:
        player_uid = str(response.get("UID", uid)).strip()
        player_name = response.get("PlayerNickname", "N/A")
        level = str(response.get("Level", "N/A"))
        region = str(response.get("Region", "N/A"))
        likes_before = str(response.get("LikesbeforeCommand", "N/A"))
        likes_after = str(response.get("LikesafterCommand", "N/A"))
        likes_given = str(response.get("LikesGivenByAPI", "N/A"))
        release_version = str(response.get("ReleaseVersion", "N/A"))
        utc_time = datetime.utcnow()
        vip_data = vip_users.get(user_id, {})
        if vip_data and vip_data.get("expires"):
            expire_date = vip_data["expires"].strftime("%Y-%m-%d %H:%M:%S")
        else:
            expire_date = "No VIP"
        total_like = likes_after

        usage["used"] += 1
        usage["last_used"] = now_utc
        like_tracker[user_id] = usage
        save_usage_to_db(user_id, usage)

        next_request_time_utc = utc_time + timedelta(hours=24)
        response_text = f"""✅ *Request Processed Successfully*\n\n👤 *Name:* `{player_name}`\n🆔 *UID:* `{player_uid}`\n⚜️ *Level:* `{level}`\n🌍 *Region:* `{region}`\n🤡 *Likes Before:* `{likes_before}`\n📈 *Likes Added:* `{likes_given}`\n🗿 *Total Likes Now:* `{total_like}`\n🔐 *Remaining Requests:* `{max_limit - usage['used']}`\n📅 *VIP Expiry:* `{expire_date}`\n📡 *Release Version:* `{release_version}`\n👑 *Owner:* @saidulbhai34"""

        # Create share link for the message
        share_text = f"✅ I just got {likes_given} likes for my Free Fire account!\n\n👤 Name: {player_name}\n🆔 UID: {player_uid}\n🗿 Total Likes: {total_like}\n\nTry it yourself: @saidulbhai34"
        share_url = f"https://t.me/share/url?url={requests.utils.quote(share_text)}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💎 Buy VIP Or Auto Like", url="https://t.me/saidulbhai34"))
        markup.add(InlineKeyboardButton("📤 Share to Friends", url=share_url))
        markup.add(InlineKeyboardButton("➕ Add Me to your group", url=f"https://t.me/{bot.get_me().username}?startgroup=true"))

        bot.edit_message_text(
            chat_id=processing_msg.chat.id,
            message_id=processing_msg.message_id,
            text=response_text,
            reply_markup=markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error in process_like: {e}")
        bot.reply_to(message, "⚠️ Something went wrong. Likes Send, I can't decode your info.")


@bot.message_handler(commands=["vip", "rmvip", "remain", "autolike", "rmautolike", "autolikelist"])
def owner_commands(message):
    if message.from_user.id != OWNER_ID:
        return

    args = message.text.split()
    cmd = args[0].lower()

    if cmd == "/autolike" and len(args) == 6:
        try:
            user_id = int(args[1])
            region = args[2]
            uid = args[3]
            interval_hours = int(args[4])
            chat_id = int(args[5])

            if interval_hours < 1:
                bot.reply_to(message, "⚠️ Interval must be at least 1 hour.")
                return

            job_id = f"{user_id}_{uid}_{int(time.time())}"
            job_data = {
                "user_id": user_id,
                "region": region,
                "uid": uid,
                "interval_hours": interval_hours,
                "chat_id": chat_id,
                "next_run": datetime.now() + timedelta(hours=interval_hours),
                "created_at": datetime.now()
            }

            auto_like_jobs[job_id] = job_data
            # persist
            save_job_to_db(job_id, job_data)

            next_run_utc = (datetime.utcnow() + timedelta(hours=interval_hours)).strftime("%Y-%m-%d %H:%M:%S")
            bot.reply_to(message, f"✅ Auto Like set for user `{user_id}`\n🆔 UID: `{uid}`\n🌍 Region: `{region}`\n⏰ Interval: `{interval_hours}` hours\n📅 Next Run: `{next_run_utc} UTC`\n🆔 Job ID: `{job_id}`", parse_mode="Markdown")
        except ValueError:
            bot.reply_to(message, "⚠️ Invalid format. Use: /autolike userid region uid interval_hours chat_id")
        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")

    elif cmd == "/rmautolike" and len(args) == 2:
        job_id = args[1]
        if job_id in auto_like_jobs:
            del auto_like_jobs[job_id]
            delete_job_from_db(job_id)
            bot.reply_to(message, f"✅ Auto Like job `{job_id}` removed.", parse_mode="Markdown")
        else:
            # try deleting from DB just in case
            delete_job_from_db(job_id)
            bot.reply_to(message, "❌ Job ID not found.")

    elif cmd == "/autolikelist":
        if not auto_like_jobs:
            bot.reply_to(message, "📝 No active auto like jobs.")
            return

        lines = ["📋 *Active Auto Like Jobs:*\n"]
        for job_id, job_data in auto_like_jobs.items():
            next_run_utc = job_data['next_run'].strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"🆔 `{job_id}`")
            lines.append(f"👤 User: `{job_data['user_id']}`")
            lines.append(f"🎯 UID: `{job_data['uid']}`")
            lines.append(f"🌍 Region: `{job_data['region']}`")
            lines.append(f"⏰ Interval: `{job_data['interval_hours']}h`")
            lines.append(f"📅 Next: `{next_run_utc} UTC`")
            lines.append("─────────────")

        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    elif cmd == "/vip" and len(args) == 4:
        try:
            user_id = int(args[1])
            limit = int(args[2])
            days = int(args[3])
            expires_dt = datetime.now() + timedelta(days=days)
            vip_users[user_id] = {"limit": limit, "expires": expires_dt}
            save_vip_to_db(user_id, limit, expires_dt)
            bot.reply_to(message, f"✅ VIP set for `{user_id}` with limit {limit}/day for {days} days.", parse_mode="Markdown")
        except:
            bot.reply_to(message, "⚠️ Invalid format. Use: /vip userid limit days")

    elif cmd == "/rmvip" and len(args) == 2:
        try:
            user_id = int(args[1])
            if vip_users.pop(user_id, None):
                delete_vip_from_db(user_id)
                bot.reply_to(message, f"✅ VIP removed for `{user_id}`", parse_mode="Markdown")
            else:
                # Attempt DB delete as well
                delete_vip_from_db(user_id)
                bot.reply_to(message, "❌ VIP not found.")
        except Exception as e:
            bot.reply_to(message, f"⚠️ Error: {e}")

    elif cmd == "/remain":
        lines = ["📊 *Remaining Daily Requests Per User:*"]
        all_user_ids = set(like_tracker.keys()) | set(vip_users.keys())
        if not all_user_ids:
            lines.append("❌ No users have used the bot yet today.")
        else:
            for uid in all_user_ids:
                usage = like_tracker.get(uid, {"used": 0})
                limit = get_user_limit(uid)
                used = usage.get("used", 0)
                lines.append(f"👤 `{uid}` ➜ {used}/{limit}")
        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['help'])
def help_command(message):
    user_id = message.from_user.id

    # For owner, show owner commands directly
    if user_id == OWNER_ID:
        help_text = (
            "📖 *Bot Commands:*\n\n"
            "🧑‍💻 `/like <region> <uid>` - Send likes to Free Fire UID\n"
            "🔰 `/start` - Start or verify\n"
            "🆘 `/help` - Show this help menu\n\n"
            "👑 *Owner Commands:*\n"
            "📊 `/vip <userid> <limit> <days>` - Add VIP user\n"
            "❌ `/rmvip <userid>` - Remove VIP user\n"
            "📈 `/remain` - Show all users' usage & stats\n"
            "🤖 `/autolike <userid> <region> <uid> <interval_hours> <chat_id>` - Set auto like\n"
            "🗑️ `/rmautolike <job_id>` - Remove auto like job\n"
            "📋 `/autolikelist` - Show all active auto like jobs\n\n"
            "📞 *Support:* @saidulbhai34"
        )
        bot.reply_to(message, help_text, parse_mode="Markdown")
        return

    # For regular users, check channel membership first
    if not is_user_in_channel(user_id):
        markup = InlineKeyboardMarkup()
        for channel in REQUIRED_CHANNELS:
            markup.add(InlineKeyboardButton(f"🔗 Join {channel}", url=f"https://t.me/{channel.strip('@')}") )
        bot.reply_to(message, "❌ You must join all our channels to use this command.", reply_markup=markup, parse_mode="Markdown")
        return

    # Show regular user help
    help_text = (
        "📖 *Bot Commands:*\n\n"
        "🧑‍💻 `/like <region> <uid>` - Send likes to Free Fire UID\n"
        "🔰 `/start` - Start or verify\n"
        "🆘 `/help` - Show this help menu\n\n"
        "📞 *Support:* @saidulbhai34\n"
        "🔗 Join our channels for updates!"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")


@bot.message_handler(func=lambda message: True, content_types=['text'])
def reply_all(message):
    if message.text.startswith('/'):
        # Handle unknown commands - only reply if it's actually an unknown command
        known_commands = ['/start', '/like', '/help', '/vip', '/rmvip', '/remain', '/autolike', '/rmautolike', '/autolikelist']
        command = message.text.split()[0].lower()
        return


# === WEBHOOK SETUP ===
def set_webhook():
    """Set webhook for production deployment"""
    webhook_url = os.getenv('WEBHOOK_URL')
    if not webhook_url:
        logger.warning("WEBHOOK_URL not set, falling back to polling mode")
        return False

    try:
        if not webhook_url.endswith('/webhook'):
            webhook_url += '/webhook'

        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook set: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"❌ Webhook setup failed: {e}")
        return False


# === MAIN ===
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))

    # For production/render deployment, use webhook mode
    webhook_url = os.getenv('WEBHOOK_URL')

    if webhook_url:
        # Production mode with webhook
        logger.info("🚀 Starting in webhook mode")
        is_webhook_mode = set_webhook()
        if not is_webhook_mode:
            logger.error("❌ Webhook setup failed, exiting")
            sys.exit(1)
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        # Development mode with polling
        logger.info("🏠 Starting in polling mode")
        # Start Flask server in a background thread for health checks
        flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False), daemon=True)
        flask_thread.start()

        # Add retry logic for polling
        max_retries = 5
        retry_count = 0

        while retry_count < max_retries:
            try:
                logger.info(f"Starting bot polling (attempt {retry_count + 1}/{max_retries})")
                bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"Polling failed (attempt {retry_count}): {e}")
                if retry_count < max_retries:
                    logger.info("Retrying in 10 seconds...")
                    time.sleep(10)
                else:
                    logger.error("Max retries reached. Exiting.")
                    sys.exit(1)
