import os
import telebot
import requests
import time
import urllib.parse
import traceback
import threading
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot import types
from flask import Flask, request, jsonify
import logging
import sys
import random

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
DATABASE_URL = os.getenv("DATABASE_URL") # set this in Render environment variables

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN not found! Please set your bot token in environment variables.")
    sys.exit(1)

# Persistence flag: if DATABASE_URL provided, we'll persist data to Postgres
PERSISTENCE = bool(DATABASE_URL)

REQUIRED_CHANNELS = ["@Saidul_Official","@premium_like_bot_34","@saidul_bot_34"]
GROUP_JOIN_LINK = "https://t.me/premium_like_bot_34"
OWNER_ID = 8408849795
DEFAULT_AUTO_LIKE_CHAT_ID = -1003988111389
ALLOWED_GROUPS_ID = -1003988111389
LATE_REGIONS = ["na", "us", "sac", "th"]  # These regions execute last in auto-like

bot = telebot.TeleBot(BOT_TOKEN)
like_tracker = {}   # in-memory cache (will be loaded from DB if persistence enabled)
vip_users = {}
auto_like_jobs = {}
negative_likes_cache = {}  # entity_id (str) -> amount

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
    # jobs table — target-based system
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        user_id BIGINT,
        region TEXT,
        uid TEXT,
        chat_id BIGINT,
        next_run TIMESTAMP,
        created_at TIMESTAMP,
        target_likes INT DEFAULT 0,
        delivered_likes INT DEFAULT 0
    )
    """)

    # vip_users table — target-based system
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vip_users (
        user_id BIGINT PRIMARY KEY,
        limit_per_day INT,
        target_likes INT DEFAULT 0,
        delivered_likes INT DEFAULT 0
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

    # negative likes table — tracked per entity (user_ or uid_)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS negative_likes (
        entity_id TEXT PRIMARY KEY,
        amount INT DEFAULT 0
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
                'chat_id': row['chat_id'],
                'next_run': row['next_run'],
                'created_at': row['created_at'],
                'target_likes': row.get('target_likes', 0) or 0,
                'delivered_likes': row.get('delivered_likes', 0) or 0
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
                'target_likes': row.get('target_likes', 0) or 0,
                'delivered_likes': row.get('delivered_likes', 0) or 0
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


def load_negative_likes_from_db():
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM negative_likes")
        rows = cur.fetchall()
        for row in rows:
            negative_likes_cache[row['entity_id']] = row['amount']
        cur.close()
        conn.close()
        logger.info(f"Loaded {len(negative_likes_cache)} negative likes entries from DB")
    except Exception as e:
        logger.error(f"Failed to load negative likes from DB: {e}")


# === SAVE helpers ===

def save_job_to_db(job_id, job_data):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO jobs (job_id, user_id, region, uid, chat_id, next_run, created_at, target_likes, delivered_likes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (job_id) DO UPDATE SET
                next_run = EXCLUDED.next_run,
                chat_id = EXCLUDED.chat_id,
                target_likes = EXCLUDED.target_likes,
                delivered_likes = EXCLUDED.delivered_likes
        """, (
            job_id,
            job_data['user_id'],
            job_data['region'],
            job_data['uid'],
            job_data['chat_id'],
            job_data['next_run'],
            job_data['created_at'],
            job_data.get('target_likes', 0),
            job_data.get('delivered_likes', 0)
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


def save_vip_to_db(user_id, limit, target_likes, delivered_likes=0):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vip_users (user_id, limit_per_day, target_likes, delivered_likes)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET
                limit_per_day=EXCLUDED.limit_per_day,
                target_likes=EXCLUDED.target_likes,
                delivered_likes=EXCLUDED.delivered_likes
        """, (user_id, limit, target_likes, delivered_likes))
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


def save_negative_like_to_db(entity_id, amount):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO negative_likes (entity_id, amount)
            VALUES (%s, %s)
            ON CONFLICT (entity_id) DO UPDATE SET amount = EXCLUDED.amount
        """, (entity_id, amount))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save negative like to DB: {e}")


def delete_negative_like_from_db(entity_id):
    if not PERSISTENCE:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM negative_likes WHERE entity_id=%s", (entity_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to delete negative like from DB: {e}")


# === UTILS ===

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
    url = f"https://like-premium-two.vercel.app/like?uid={uid}&server_name={region}"
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
    """Get VIP daily limit. Returns 0 if not VIP or target completed."""
    vip = vip_users.get(user_id)
    if not vip:
        return 0
    # Check if target is completed (target_likes > 0 means limited)
    target = vip.get('target_likes', 0)
    delivered = vip.get('delivered_likes', 0)
    if target > 0 and delivered >= target:
        return 0  # Target reached, no more likes
    return vip['limit']


def make_progress_bar(delivered, target, bar_length=10):
    """Create a visual progress bar."""
    if target <= 0:
        return "♾️ Unlimited"
    percentage = min((delivered / target) * 100, 100)
    filled = int(bar_length * (percentage / 100))
    empty = bar_length - filled
    bar = "█" * filled + "░" * empty
    return f"{bar} {percentage:.1f}%"


def estimate_days(remaining_likes, avg_per_day=220):
    """Estimate remaining days based on avg likes per day (210-220 range, avg 220)."""
    if remaining_likes <= 0:
        return 0
    return max(1, round(remaining_likes / avg_per_day))


def escape_md(text):
    """Escape MarkdownV1 special characters to prevent parse errors."""
    for ch in ['_', '*', '`', '[']:
        text = str(text).replace(ch, f'\\{ch}')
    return text


def get_negative_likes(entity_id):
    """Get negative likes for an entity (user_ or uid_)."""
    return negative_likes_cache.get(str(entity_id), 0)


def apply_negative_likes(entity_id, target_likes):
    """Apply negative likes deduction for an entity. Returns effective target and negative consumed."""
    entity_id = str(entity_id)
    neg = get_negative_likes(entity_id)
    if neg <= 0 or target_likes <= 0:
        return target_likes, 0
    # Deduct negative from target
    effective_target = max(0, target_likes - neg)
    consumed = target_likes - effective_target
    # Clear negative likes
    negative_likes_cache.pop(entity_id, None)
    delete_negative_like_from_db(entity_id)
    return effective_target, consumed


# === Threads: reset_limits and auto_like_scheduler ===

def reset_limits():
    while True:
        try:
            # Calculate time until next 22:00 UTC
            now_utc = datetime.utcnow()
            next_reset = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
            if next_reset <= now_utc:
                next_reset += timedelta(days=1)
            sleep_seconds = (next_reset - now_utc).total_seconds()

            time.sleep(sleep_seconds)

            if PERSISTENCE:
                # Reset all usage at 22:00 UTC
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
                logger.info("✅ Daily limits reset at 22:00 UTC in DB and memory.")
            else:
                like_tracker.clear()
                logger.info("✅ Daily limits reset at 22:00 UTC (in-memory).")
        except Exception as e:
            logger.error(f"Error in reset_limits thread: {e}")


# OWNER LINK
owner_link = "[𝐒𝐚𝐢𝐝𝐮𝐥 _ 𝐎𝐟𝐟𝐢𝐜𝐢𝐚𝐥](https://t.me/saidulbhai34)"

def auto_like_scheduler():
    """Background thread to handle auto like jobs. Target-based system."""

    while True:

        try:

            current_time = datetime.utcnow()

            jobs_to_execute = []

            for job_id, job_data in list(auto_like_jobs.items()):

                # TARGET CHECK
                target = job_data.get('target_likes', 0)

                delivered = job_data.get('delivered_likes', 0)

                region = job_data['region'].upper()

                # REGION FLAGS
                region_flags = {
                    "BD": "🇧🇩",
                    "SG": "🇸🇬",
                    "IND": "🇮🇳",
                    "BR": "🇧🇷",
                    "US": "🇺🇸",
                    "PK": "🇵🇰",
                    "ID": "🇮🇩",
                    "TH": "🇹🇭",
                    "VN": "🇻🇳",
                    "RU": "🇷🇺",
                    "ME": "🇲🇪"
                }

                flag = region_flags.get(region, "🌍")

                user_id = job_data['user_id']

                # USER INFO
                try:

                    user_info = bot.get_chat(user_id)

                    first_name = (
                        user_info.first_name
                        if user_info.first_name
                        else f"User {user_id}"
                    )

                except:

                    first_name = f"User {user_id}"

                user_mention = (
                    f"[{first_name}]"
                    f"(tg://user?id={user_id})"
                )

                # TARGET COMPLETE
                if target > 0 and delivered >= target:

                    overflow = delivered - target

                    entity_id = f"uid_{job_data['uid']}"

                    if overflow > 0:

                        existing_neg = get_negative_likes(entity_id)

                        new_neg = existing_neg + overflow

                        negative_likes_cache[entity_id] = new_neg

                        save_negative_like_to_db(
                            entity_id,
                            new_neg
                        )

                    delete_job_from_db(job_id)

                    auto_like_jobs.pop(job_id, None)

                    try:

                        bot.send_message(
                            job_data['chat_id'],
                            f"✅ *Auto Like Target Completed!*\n\n"
                            f"👤 *User:* {user_mention}\n"
                            f"🆔 UID: `{job_data['uid']}`\n"
                            f"{flag} *Region:* `{region}`\n"
                            f"🎯 Target: `{target}` likes\n"
                            f"📦 Delivered: `{delivered}` likes\n"
                            f"{'⚠️ Overflow: `' + str(overflow) + '` likes saved as negative' if overflow > 0 else ''}\n\n"
                            f"👑 *Owner* : {owner_link}",
                            parse_mode='Markdown',
                            disable_web_page_preview=True
                        )

                    except Exception as e:

                        logger.error(
                            f"Failed to send target complete msg: {e}"
                        )

                    continue

                # CHECK NEXT RUN
                if current_time >= job_data['next_run']:

                    jobs_to_execute.append(
                        (job_id, job_data)
                    )

            # SORT REGIONS
            jobs_to_execute.sort(
                key=lambda x: 1
                if x[1]['region'].lower() in LATE_REGIONS
                else 0
            )

            # EXECUTE JOBS
            for job_id, job_data in jobs_to_execute:

                try:

                    user_id = job_data['user_id']

                    region = job_data['region'].upper()

                    uid = job_data['uid']

                    chat_id = job_data['chat_id']

                    target = job_data.get(
                        'target_likes',
                        0
                    )

                    delivered = job_data.get(
                        'delivered_likes',
                        0
                    )

                    # REGION FLAGS
                    region_flags = {
                        "BD": "🇧🇩",
                        "SG": "🇸🇬",
                        "IND": "🇮🇳",
                        "BR": "🇧🇷",
                        "US": "🇺🇸",
                        "PK": "🇵🇰",
                        "ID": "🇮🇩",
                        "TH": "🇹🇭",
                        "VN": "🇻🇳",
                        "RU": "🇷🇺",
                        "ME": "🇲🇪"
                    }

                    flag = region_flags.get(
                        region,
                        "🌍"
                    )

                    # USER INFO
                    try:

                        user_info = bot.get_chat(user_id)

                        first_name = (
                            user_info.first_name
                            if user_info.first_name
                            else f"User {user_id}"
                        )

                    except:

                        first_name = f"User {user_id}"

                    user_mention = (
                        f"[{first_name}]"
                        f"(tg://user?id={user_id})"
                    )

                    # API CALL
                    response = call_api(
                        region,
                        uid
                    )

                    # API ERROR
                    if 'error' in response:

                        bot.send_message(
                            chat_id,
                            f"⚠️ *Auto Like Failed*\n\n"
                            f"👤 *User:* {user_mention}\n"
                            f"🆔 UID: `{uid}`\n"
                            f"{flag} *Region:* `{region}`\n"
                            f"🚫 Error: `{response['error']}`\n\n"
                            f"👑 *Owner* : {owner_link}",
                            parse_mode='Markdown',
                            disable_web_page_preview=True
                        )

                        next_run = current_time.replace(
                            hour=22,
                            minute=0,
                            second=0,
                            microsecond=0
                        )

                        if next_run <= current_time:

                            next_run += timedelta(days=1)

                        auto_like_jobs[job_id][
                            'next_run'
                        ] = next_run

                        save_job_to_db(
                            job_id,
                            auto_like_jobs[job_id]
                        )

                        continue

                    # INVALID RESPONSE
                    if (
                        not isinstance(response, dict)
                        or response.get('status') != 1
                    ):

                        bot.send_message(
                            chat_id,
                            f"❌ *Auto Like Failed*\n\n"
                            f"👤 *User:* {user_mention}\n"
                            f"🆔 UID: `{uid}`\n"
                            f"{flag} *Region:* `{region}`\n"
                            f"⚠️ Maximum likes reached for today.\n\n"
                            f"👑 *Owner* : {owner_link}",
                            parse_mode='Markdown',
                            disable_web_page_preview=True
                        )

                        next_run = current_time.replace(
                            hour=22,
                            minute=0,
                            second=0,
                            microsecond=0
                        )

                        if next_run <= current_time:

                            next_run += timedelta(days=1)

                        auto_like_jobs[job_id][
                            'next_run'
                        ] = next_run

                        save_job_to_db(
                            job_id,
                            auto_like_jobs[job_id]
                        )

                        continue

                    # PLAYER DATA
                    player_name = response.get(
                        'PlayerNickname',
                        'N/A'
                    )

                    likes_given_str = str(
                        response.get(
                            'LikesGivenByAPI',
                            '0'
                        )
                    )

                    total_likes = str(
                        response.get(
                            'LikesafterCommand',
                            'N/A'
                        )
                    )

                    utc_time = datetime.utcnow()

                    # UPDATE DELIVERED
                    try:

                        likes_given_int = int(
                            likes_given_str
                        )

                    except ValueError:

                        likes_given_int = 0

                    new_delivered = (
                        delivered + likes_given_int
                    )

                    auto_like_jobs[job_id][
                        'delivered_likes'
                    ] = new_delivered

                    # PROGRESS
                    remaining = (
                        max(0, target - new_delivered)
                        if target > 0
                        else 0
                    )

                    progress_bar = make_progress_bar(
                        new_delivered,
                        target
                    )

                    est_days = (
                        estimate_days(remaining)
                        if target > 0
                        else 0
                    )

                    # TARGET INFO
                    if target > 0:

                        target_info = (
                            f"🎯 *Target:* `{target}` likes\n"
                            f"📦 *Delivered:* `{new_delivered}` likes\n"
                            f"📊 *Progress:* {progress_bar}\n"
                            f"⏳ *Remaining:* `{remaining}` likes\n"
                            f"📅 *Est. Days Left:* "
                            f"`~{est_days}` days "
                            f"_(~210-220/day)_"
                        )

                    else:

                        target_info = (
                            f"🎯 *Target:* `♾️ Unlimited`\n"
                            f"📦 *Total Delivered:* "
                            f"`{new_delivered}` likes"
                        )

                    # FINAL AUTO MESSAGE
                    auto_msg = (
                        f"🤖 *Auto Like Executed Successfully*\n\n"
                        f"👤 *User:* {user_mention}\n"
                        f"👤 *Name:* `{player_name}`\n"
                        f"🆔 *UID:* `{uid}`\n"
                        f"{flag} *Region:* `{region}`\n"
                        f"📈 *Likes Added:* "
                        f"`{likes_given_str}`\n"
                        f"🗿 *Total Likes Now:* "
                        f"`{total_likes}`\n"
                        f"⏱ *Executed At:* "
                        f"`{utc_time.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
                        f"🔁 *Next Auto Like:* `22:00 UTC`\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{target_info}\n\n"
                        f"👑 *Owner* : {owner_link}"
                    )

                    bot.send_message(
                        chat_id,
                        auto_msg,
                        parse_mode='Markdown',
                        disable_web_page_preview=True
                    )

                    # NEXT RUN
                    next_run = current_time.replace(
                        hour=22,
                        minute=0,
                        second=0,
                        microsecond=0
                    )

                    if next_run <= current_time:

                        next_run += timedelta(days=1)

                    auto_like_jobs[job_id][
                        'next_run'
                    ] = next_run

                    save_job_to_db(
                        job_id,
                        auto_like_jobs[job_id]
                    )

                    # TARGET REACHED
                    if (
                        target > 0
                        and new_delivered >= target
                    ):

                        overflow = (
                            new_delivered - target
                        )

                        if overflow > 0:

                            entity_id = f"uid_{uid}"

                            existing_neg = (
                                get_negative_likes(
                                    entity_id
                                )
                            )

                            new_neg = (
                                existing_neg + overflow
                            )

                            negative_likes_cache[
                                entity_id
                            ] = new_neg

                            save_negative_like_to_db(
                                entity_id,
                                new_neg
                            )

                        delete_job_from_db(job_id)

                        auto_like_jobs.pop(
                            job_id,
                            None
                        )

                        try:

                            bot.send_message(
                                chat_id,
                                f"🏁 *Auto Like Target Reached!*\n\n"
                                f"👤 *User:* {user_mention}\n"
                                f"🆔 UID: `{uid}`\n"
                                f"{flag} *Region:* `{region}`\n"
                                f"🎯 Target: `{target}` likes\n"
                                f"📦 Final Delivered: "
                                f"`{new_delivered}` likes\n"
                                f"{'⚠️ Negative Likes: `-' + str(overflow) + '` saved for next subscription' if overflow > 0 else '✅ Exactly on target!'}\n\n"
                                f"👑 *Owner* : {owner_link}",
                                parse_mode='Markdown',
                                disable_web_page_preview=True
                            )

                        except Exception as e:

                            logger.error(
                                f"Failed to send target reached msg: {e}"
                            )

                except Exception as e:

                    logger.error(
                        f"Auto like execution error "
                        f"for job {job_id}: {e}"
                    )

        except Exception as e:

            logger.error(
                f"Auto like scheduler error: {e}"
            )

        time.sleep(30)

# Initialize DB and load persisted data (if enabled)
try:
    init_db()
    load_jobs_from_db()

    # Update all existing jobs to run at 22:00 UTC if they haven't been updated yet
    if auto_like_jobs:
        now_utc = datetime.utcnow()
        update_count = 0
        for job_id, job_data in auto_like_jobs.items():
            target_time = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
            if target_time <= now_utc:
                target_time += timedelta(days=1)

            if job_data['next_run'] != target_time:
                job_data['next_run'] = target_time
                save_job_to_db(job_id, job_data)
                update_count += 1
        if update_count > 0:
            logger.info(f"Updated {update_count} existing jobs to 22:00 UTC")

    load_vip_from_db()
    load_usage_from_db()
    load_negative_likes_from_db()
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


# === TELEGRAM COMMANDS ===

@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id

    if not is_user_in_channel(user_id):
        markup = InlineKeyboardMarkup()

        for channel in REQUIRED_CHANNELS:
            markup.add(
                InlineKeyboardButton(
                    f"🔗 Join {channel}",
                    url=f"https://t.me/{channel.strip('@')}"
                )
            )

        bot.reply_to(
            message,
            "📢 Channel Membership Required\nTo use this bot, you must join all our channels first",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    if user_id not in like_tracker:
        like_tracker[user_id] = {
            "used": 0,
            "last_used": datetime.utcnow() - timedelta(days=1)
        }

        save_usage_to_db(user_id, like_tracker[user_id])

    bot.reply_to(
        message,
        "✅ You're verified! Use /like to send likes.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['like'])
def handle_like(message):

    user_id = message.from_user.id
    chat_id = message.chat.id
    args = message.text.split()

    # ONLY GROUP ALLOWED
    if message.chat.type == "private" and message.from_user.id != OWNER_ID:

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                "🔗 Join Official Group",
                url=GROUP_JOIN_LINK
            )
        )

        bot.reply_to(
            message,
            "❌ Sorry! command is not allowed here.\n\nJoin our official group:",
            reply_markup=markup
        )
        return

    # CHANNEL CHECK
    if not is_user_in_channel(user_id):

        markup = InlineKeyboardMarkup()

        for channel in REQUIRED_CHANNELS:

            markup.add(
                InlineKeyboardButton(
                    f"🔗 Join {channel}",
                    url=f"https://t.me/{channel.strip('@')}"
                )
            )

        bot.reply_to(
            message,
            "❌ You must join all our channels to use this command.",
            reply_markup=markup,
            parse_mode="Markdown"
        )

        return

    # COMMAND FORMAT CHECK
    if len(args) != 3:

        bot.reply_to(
            message,
            "❌ Format: `/like server_name uid`",
            parse_mode="Markdown"
        )

        return

    region, uid = args[1], args[2]

    # INPUT VALIDATION
    if not region.isalpha() or not uid.isdigit():

        bot.reply_to(
            message,
            "⚠️ Invalid input. Use: `/like server_name uid`",
            parse_mode="Markdown"
        )

        return

    # START THREAD
    threading.Thread(
        target=process_like,
        args=(message, region, uid)
    ).start()


def process_like(message, region, uid):

    user_id = message.from_user.id
    chat_id = message.chat.id

    now_utc = datetime.utcnow()

    usage = like_tracker.get(
        user_id,
        {
            "used": 0,
            "last_used": now_utc - timedelta(days=1)
        }
    )

    # DAILY RESET
    last_used_date = usage["last_used"].date()
    current_date = now_utc.date()

    if current_date > last_used_date:
        usage["used"] = 0

    max_limit = get_user_limit(user_id)

    # LIMIT CHECK
    if usage["used"] >= max_limit:

        next_reset = (
            now_utc + timedelta(days=1)
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        remaining_time = next_reset - now_utc

        hours, remainder = divmod(
            remaining_time.total_seconds(),
            3600
        )

        minutes = divmod(remainder, 60)[0]

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                "💎 Buy More VIP",
                url="https://t.me/saidulbhai34"
            )
        )

        bot.reply_to(
            message,
            f"⚠️ You have exceeded your daily request limit!\n"
            f"Or You Do Not Have permission To Send Like Request\n\n"
            f"⏳ Next request available at: 22:00 UTC\n"
            f"(in {int(hours)}h {int(minutes)}m)\n\n"
            f"🔐 Buy More VIP or wait for the cooldown.",
            reply_markup=markup
        )

        return

    # PROCESS MESSAGE
    processing_msg = bot.reply_to(
        message,
        "⏳ Please wait... Sending likes..."
    )

    # LIVE PROGRESS BAR
    progress_stages = [
        "[▓░░░░░░░░░] 10%",
        "[▓▓░░░░░░░░] 25%",
        "[▓▓▓░░░░░░░] 50%",
        "[▓▓▓▓░░░░░░] 75%",
        "[▓▓▓▓▓▓▓▓▓▓] 100% ✅"
    ]

    for stage in progress_stages:

        time.sleep(0.5)

        try:

            bot.edit_message_text(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id,
                text=(
                    "⏳ Processing your like request...\n"
                    f"{stage}"
                )
            )

        except Exception:
            pass

    # API CALL
    response = call_api(region, uid)

    # API ERROR
    if "error" in response:

        try:

            bot.edit_message_text(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id,
                text=f"⚠️ API Error: {response['error']}"
            )

        except:

            bot.reply_to(
                message,
                f"⚠️ API Error: {response['error']}"
            )

        return

    # INVALID RESPONSE
    if not isinstance(response, dict) or response.get("status") != 1:

        try:

            bot.edit_message_text(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id,
                text=(
                    "❌ UID has already received its max amount of likes.\n"
                    "Limit reached for today, try another UID or after 24 hrs."
                )
            )

        except:

            bot.reply_to(
                message,
                "⚠️ Invalid UID or unable to fetch data."
            )

        return

    try:

        # PLAYER DATA
        player_uid = str(
            response.get("UID", uid)
        ).strip()

        player_name = response.get(
            "PlayerNickname",
            "N/A"
        )

        # REGION UPPER
        region = region.upper()

        # REGION FLAGS
        region_flags = {
            "BD": "🇧🇩",
            "SG": "🇸🇬",
            "IND": "🇮🇳",
            "BR": "🇧🇷",
            "US": "🇺🇸",
            "PK": "🇵🇰",
            "ID": "🇮🇩",
            "TH": "🇹🇭",
            "VN": "🇻🇳",
            "RU": "🇷🇺",
            "ME": "🇲🇪"
        }

        flag = region_flags.get(region, "🌍")

        likes_before = str(
            response.get(
                "LikesbeforeCommand",
                "N/A"
            )
        )

        likes_after = str(
            response.get(
                "LikesafterCommand",
                "N/A"
            )
        )

        likes_given = str(
            response.get(
                "LikesGivenByAPI",
                "N/A"
            )
        )

        # VIP UPDATE
        vip_data = vip_users.get(user_id)

        if vip_data:

            try:
                lg = int(likes_given)

            except ValueError:
                lg = 0

            vip_data['delivered_likes'] = (
                vip_data.get('delivered_likes', 0) + lg
            )

            save_vip_to_db(
                user_id,
                vip_data['limit'],
                vip_data.get('target_likes', 0),
                vip_data['delivered_likes']
            )

            target = vip_data.get('target_likes', 0)

            delivered = vip_data['delivered_likes']

            remaining_likes = (
                max(0, target - delivered)
                if target > 0 else 0
            )

            progress_bar = make_progress_bar(
                delivered,
                target
            )

            est_days = (
                estimate_days(remaining_likes)
                if target > 0 else 0
            )

            if target > 0:

                vip_info = (
                    f"📊 *Progress:* {progress_bar}\n"
                    f"⏳ *Remaining:* `{remaining_likes}` likes\n"
                    f"📅 *Est. Days:* `~{est_days}` days"
                )

            else:

                vip_info = (
                    f"📊 *Target:* `♾️ Unlimited`\n"
                    f"📈 *Delivered:* `{delivered}`"
                )

            # VIP TARGET COMPLETE
            if target > 0 and delivered >= target:

                overflow = delivered - target

                if overflow > 0:

                    entity_id = f"user_{user_id}"

                    existing_neg = get_negative_likes(entity_id)

                    new_neg = existing_neg + overflow

                    negative_likes_cache[entity_id] = new_neg

                    save_negative_like_to_db(
                        entity_id,
                        new_neg
                    )

                vip_users.pop(user_id, None)

                delete_vip_from_db(user_id)

        else:

            vip_info = "📊 *VIP:* `No Active Plan`"

        total_like = likes_after

        # UPDATE USAGE
        usage["used"] += 1
        usage["last_used"] = now_utc

        like_tracker[user_id] = usage

        save_usage_to_db(user_id, usage)

        # USER MENTION
        user_mention = (
            f"[{message.from_user.first_name}]"
            f"(tg://user?id={message.from_user.id})"
        )

        owner_mention = (
            f"[𝐒𝐚𝐢𝐝𝐮𝐥 _ 𝐎𝐟𝐟𝐢𝐜𝐢𝐚𝐥]"
            f"(tg://user?id={OWNER_ID})"
        )

        # FINAL RESPONSE
        response_text = (
            f"✅ *Like Request Successfully*\n\n"
            f"👤 *Name:* {user_mention}\n"
            f"👤 *Player:* `{player_name}`\n"
            f"🆔 *UID:* `{player_uid}`\n"
            f"{flag} *Region:* `{region}`\n"
            f"🤡 *Likes Before:* `{likes_before}`\n"
            f"📈 *Likes Added:* `{likes_given}`\n"
            f"🗿 *Total Likes Now:* `{total_like}`\n"
            f"🔐 *Remaining Requests:* "
            f"`{max_limit - usage['used']}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{vip_info}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 *Note:* Thanks For Purchase Premium Likes 220 like Subscription\n"
            f"👑 *Owner:* {owner_mention}"
        )

        # SHARE TEXT
        share_text = (
            f"✅ I just got {likes_given} likes for my "
            f"Free Fire account!\n\n"
            f"👤 Name: {player_name}\n"
            f"🆔 UID: {player_uid}\n"
            f"🗿 Total Likes: {total_like}\n\n"
            f"Try it yourself: @premium_like_bot_34"
        )

        share_url = (
            f"https://t.me/share/url?"
            f"url={requests.utils.quote(share_text)}"
        )

        # DELETE PROCESS MESSAGE
        try:
            bot.delete_message(
                chat_id=processing_msg.chat.id,
                message_id=processing_msg.message_id
            )
        except:
            pass

        # UPDATED BUTTON LAYOUT
        markup = InlineKeyboardMarkup()

        markup.row(
            InlineKeyboardButton(
                "💎 𝐁𝐔𝐘 𝐕𝐈𝐏",
                url="https://t.me/saidulbhai34"
            ),

            InlineKeyboardButton(
                "📤 𝐒𝐇𝐀𝐑𝐄",
                url=share_url
            ),

            InlineKeyboardButton(
                "📢 𝐑𝐄𝐏𝐎𝐑𝐓",
                url=GROUP_JOIN_LINK
            )
        )

        markup.row(
            InlineKeyboardButton(
                "📱 𝐖𝐇𝐀𝐓𝐒𝐀𝐏𝐏",
                url="https://wa.me/8801879854758"
            ),

            InlineKeyboardButton(
                "🌐 𝐅𝐀𝐂𝐄𝐁𝐎𝐎𝐊",
                url="https://facebook.com/Saidulmiah124"
            )
        )

        # SEND VIDEO RESULT
        try:

            with open("like_video.mp4", "rb") as video:

                bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    caption=response_text,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )

        except Exception as e:

            logger.error(f"Video Send Error: {e}")

            # FALLBACK MESSAGE
            bot.send_message(
                chat_id=chat_id,
                text=response_text,
                reply_markup=markup,
                parse_mode="Markdown"
            )

    except Exception as e:

        logger.error(f"Error in process_like: {e}")

        bot.reply_to(
            message,
            "⚠️ Something went wrong.\n"
            "Likes Send, I can't decode your info."
        )


@bot.message_handler(commands=["vip", "rmvip", "remain", "autolike", "rmautolike", "autolikelist", "runalljobs", "add", "change"])
def owner_commands(message):
    # OWNER_ID চেক করা
    if message.from_user.id != OWNER_ID:
        return

    args = message.text.split()
    cmd = args[0].lower()

    # ===================== PROGRESS BAR =====================
    def animate_progress(chat_id, text="⏳ AUTO LIKE SET PROGRESS", duration=3):
        steps = ["░░░░░░░░░░ 0%", "██░░░░░░░░ 20%", "████░░░░░░ 40%", "██████░░░░ 60%", "████████░░ 80%", "██████████ 100%"]
        try:
            progress_message = bot.send_message(chat_id, f"{text}\nProgress: {steps[0]}")
            for step in steps[1:]:
                time.sleep(duration / (len(steps) - 1))
                try: bot.edit_message_text(f"{text}\nProgress: {step}", chat_id, progress_message.message_id)
                except: pass
            time.sleep(0.5)
            bot.delete_message(chat_id, progress_message.message_id)
        except Exception as e: print(f"Progress Error: {e}")

    # /autolike কমান্ডের জন্য
    if cmd == "/autolike" and (len(args) >= 4 and len(args) <= 5):
        try:
            animate_progress(message.chat.id)
            
            user_id = int(args[1])
            region = args[2]
            uid = args[3]

            try:
                user_info = bot.get_chat(user_id)
                user_mention = f"[{user_info.first_name}](tg://user?id={user_id})"
            except:
                user_mention = f"[User](tg://user?id={user_id})"
            
            try:
                owner_info = bot.get_chat(OWNER_ID)
                owner_link = f"[{owner_info.first_name}](tg://user?id={OWNER_ID})"
            except:
                owner_link = f"[Owner](tg://user?id={OWNER_ID})"

            # --- আপডেট করা ফ্ল্যাগ লিস্ট ---
            region_flags = {
                "BD": "🇧🇩", "IND": "🇮🇳", "PK": "🇵🇰", "SG": "🇸🇬", 
                "MY": "🇲🇾", "BR": "🇧🇷", "RU": "🇷🇺", "ID": "🇮🇩", 
                "ME": "🇸🇦", "NA": "🇺🇸", "EU": "🇪🇺", "TH": "🇹🇭", 
                "VN": "🇻🇳", "TW": "🇹🇼", "ASIA": "🌏"
            }
            reg_code = region.upper()
            flag = region_flags.get(reg_code, "🌍")

            chat_id = DEFAULT_AUTO_LIKE_CHAT_ID
            target_likes = int(args[4]) if len(args) == 5 else 0

            neg_deducted = 0
            if target_likes > 0:
                entity_id = f"uid_{uid}"
                target_likes, neg_deducted = apply_negative_likes(entity_id, target_likes)

            now_utc = datetime.utcnow()
            next_run = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
            if next_run <= now_utc:
                next_run += timedelta(days=1)

            job_id = f"{user_id}_{uid}_{int(time.time())}"
            job_data = {
                "user_id": user_id, "region": reg_code, "uid": uid,
                "chat_id": chat_id, "next_run": next_run,
                "created_at": datetime.utcnow(), "target_likes": target_likes,
                "delivered_likes": 0
            }

            auto_like_jobs[job_id] = job_data
            save_job_to_db(job_id, job_data)

            # --- টেক্সট গুলো নরমাল করা হয়েছে ---
            target_info = f"🎯 Target Likes: `{target_likes}`\n📅 Est. Delivery: `~{estimate_days(target_likes)}` days" if target_likes > 0 else "🎯 Target Likes: `Unlimited`"
            neg_info = f"\n⚠️ Negative Likes Deducted: `-{neg_deducted}`" if neg_deducted > 0 else ""

            caption_success = (
                f"⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️\n"
                f"✅ AUTO LIKE SET SUCCESSFULLY\n"
                f"⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️\n\n"
                f"👤 Name : {user_mention}\n"
                f"🆔 UID : `{uid}`\n\n"
                f"{flag} Region : `{reg_code}` \n"
                f"💬 Chat ID : `{chat_id}`\n"
                f"📅 Next Run : `{next_run.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
                f"{target_info}{neg_info}\n"
                f"🆔 Job ID : `{job_id}`\n\n"
                f"⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️\n"
                f"👑 Owner : {owner_link}"
            )

            # বাটন লেআউট ২x২ রাখা হয়েছে
            markup = InlineKeyboardMarkup()
            markup.row(
                InlineKeyboardButton("💎 BUY VIP", url="https://t.me/saidulbhai34"),
                InlineKeyboardButton("📱 WHATSAPP", url="https://wa.me/8801879854758")
            )
            markup.row(
                InlineKeyboardButton("🌐 FACEBOOK", url="https://facebook.com/Saidulmiah124"),
                InlineKeyboardButton("📢 Report Group", url=GROUP_JOIN_LINK)
            )

            try:
                with open("auto_like_video.mp4", "rb") as video:
                    bot.send_video(message.chat.id, video, caption=caption_success, parse_mode="Markdown", reply_markup=markup)
            except Exception:
                bot.reply_to(message, caption_success, parse_mode="Markdown", reply_markup=markup)

        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")


    # ================= REMOVE =================
    elif cmd == "/rmautolike" and len(args) == 2:
        job_id = args[1]
        if job_id in auto_like_jobs:
            del auto_like_jobs[job_id]
            delete_job_from_db(job_id)
            bot.reply_to(message, f"✅ Auto Like job <code>{job_id}</code> removed.", parse_mode="HTML")
        else:
            delete_job_from_db(job_id)
            bot.reply_to(message, "❌ Job ID not found.")

    # ================= LIST =================
    elif cmd == "/autolikelist":
        if not auto_like_jobs:
            bot.reply_to(message, "📝 *No active auto like jobs.*", parse_mode="Markdown")
            return

        # ড্যাশবোর্ড হেডার (একবার শুরুতে যাবে)
        bot.send_message(message.chat.id, "📋 *Active Auto Like Jobs:*", parse_mode="Markdown")

        for job_id, job_data in auto_like_jobs.items():
            # ১. ডাটা প্রিপারেশন
            u_id = job_data.get('user_id')
            try:
                u_info = bot.get_chat(u_id)
                user_mention = f"[{u_info.first_name}](tg://user?id={u_id})"
            except:
                user_mention = f"[User](tg://user?id={u_id})"

            # ফ্ল্যাগ ডিকশনারি (সব সার্ভার ফ্ল্যাগ এখানে আছে)
            region_flags = {
                "BD": "🇧🇩", "IND": "🇮🇳", "PK": "🇵🇰", "SG": "🇸🇬", 
                "MY": "🇲🇾", "BR": "🇧🇷", "RU": "🇷🇺", "ID": "🇮🇩", 
                "ME": "🇸🇦", "NA": "🇺🇸", "EU": "🇪🇺", "TH": "🇹🇭", "VN": "🇻🇳"
            }
            reg_code = job_data.get('region', 'N/A').upper()
            flag = region_flags.get(reg_code, "🌍") # লিস্টে না থাকলে পৃথিবী আইকন দেখাবে

            next_run_utc = job_data['next_run'].strftime("%Y-%m-%d %H:%M:%S")
            target = job_data.get('target_likes', 0)
            delivered = job_data.get('delivered_likes', 0)
            remaining = max(0, target - delivered) if target > 0 else 0
            
            # প্রোগ্রেস বার (আপনার মেক_প্রোগ্রেস_বার ফাংশন থাকলে সেটা কাজ করবে)
            try:
                progress = make_progress_bar(delivered, target)
            except:
                p = int((delivered / target * 100)) if target > 0 else 0
                bar_fill = int(min(p, 100) / 10)
                progress = "▮" * bar_fill + "▯" * (10 - bar_fill)

            # ২. আপনার অরিজিনাল ডিজাইন (বিন্দুমাত্র পরিবর্তনহীন)
            current_job_lines = []
            current_job_lines.append("⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")
            
            current_job_lines.append(f"👤 Name: {user_mention}")
            current_job_lines.append(f"👤 User: `{u_id}`")
            current_job_lines.append(f"🎯 UID: `{job_data.get('uid', 'N/A')}`")
            current_job_lines.append(f"{flag} Region: `{reg_code}`")
            
            if target > 0:
                current_job_lines.append(f"🎯 Target: `{target}` likes")
                current_job_lines.append(f"📦 Delivered: `{delivered}` likes")
                current_job_lines.append(f"⏳ Left: `{remaining}` likes")
                current_job_lines.append(f"📊 {progress}")
            else:
                current_job_lines.append(f"🎯 Target: `♾️ Unlimited`")
                current_job_lines.append(f"📦 Delivered: `{delivered}` likes")
            current_job_lines.append(f"📅 Next: `{next_run_utc} UTC`")
            current_job_lines.append(f"🆔 `{job_id}`")
            
            current_job_lines.append(f"👑 Owner: [𝐒𝐚𝐢𝐝𝐮𝐥 _ 𝐎𝐟𝐟𝐢𝐜𝐢𝐚𝐥](tg://user?id={OWNER_ID})")
            current_job_lines.append("⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")

            # ৩. একটা একটা করে আলাদা মেসেজ পাঠানো (লুপের ভেতরেই সেন্ড করা হচ্ছে)
            msg_text = "\n".join(current_job_lines)
            bot.send_message(message.chat.id, msg_text, parse_mode="Markdown")

    # /vip <userid> <limit> <target_like>
    elif cmd == "/vip" and len(args) == 4:
        try:
            user_id = int(args[1])
            limit = int(args[2])
            target_likes = int(args[3])

            # Apply negative likes deduction per VIP user
            neg_deducted = 0
            if target_likes > 0:
                entity_id = f"user_{user_id}"
                target_likes, neg_deducted = apply_negative_likes(entity_id, target_likes)

            vip_users[user_id] = {
                "limit": limit,
                "target_likes": target_likes,
                "delivered_likes": 0
            }
            save_vip_to_db(user_id, limit, target_likes, 0)

            if target_likes > 0:
                est_days = estimate_days(target_likes)
                target_info = f"🎯 Target: `{target_likes}` likes (~{est_days} days)"
            else:
                target_info = f"🎯 Target: `♾️ Unlimited`"

            neg_info = f"\n⚠️ Negative Likes Deducted: `-{neg_deducted}`" if neg_deducted > 0 else ""

            bot.reply_to(message,
                f"✅ *VIP Set Successfully!*\n\n"
                f"👤 User: `{user_id}`\n"
                f"📊 Daily Limit: `{limit}`/day\n"
                f"{target_info}{neg_info}",
                parse_mode="Markdown")
        except:
            bot.reply_to(message, "⚠️ Invalid format. Use: `/vip userid limit target_like`", parse_mode="Markdown")

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
        all_user_ids = set(like_tracker.keys()) | set(vip_users.keys())
        
        if not all_user_ids:
            bot.reply_to(message, "❌ *No data available for today.*", parse_mode="Markdown")
        else:
            # হেডার - স্টাইলিশ বড় লেখা
            lines = ["📊 𝐔𝐒𝐄𝐑 𝐔𝐒𝐀𝐆𝐄 𝐃𝐀𝐒𝐇𝐁𝐎𝐀𝐑𝐃", "⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️"]
            
            for uid in all_user_ids:
                usage = like_tracker.get(uid, {"used": 0})
                used = usage.get("used", 0)
                limit = get_user_limit(uid)
                remaining = max(0, limit - used)
                
                # প্রগ্রেস বার (৮টি ব্লক)
                filled = int((used / limit) * 8) if limit > 0 else 0
                bar = "█" * filled + "▒" * (8 - filled)
                
                # লিমিট চেক
                status_icon = "🚫" if remaining == 0 else "✅"
                
                try:
                    chat_member = bot.get_chat_member(message.chat.id, uid)
                    # পুরো নাম পাওয়ার চেষ্টা
                    fname = chat_member.user.first_name
                    lname = chat_member.user.last_name or ""
                    full_name = f"{fname} {lname}".strip()
                    
                    # নাম বড় হলে ছোট করা (২০ ক্যারেক্টার লিমিট)
                    display_name = (full_name[:20] + '..') if len(full_name) > 20 else full_name
                    user_display = f"[{display_name}](tg://user?id={uid})"
                except:
                    user_display = f"`{uid}`"
                
                # আউটপুট লাইন - স্টাইলিশ বোল্ড ফন্ট সহ
                lines.append(f"{status_icon} *𝐔𝐒𝐄𝐑:* {user_display}")
                lines.append(f"📊 *𝐔𝐒𝐀𝐆𝐄:* `{bar}` `{used}/{limit}`")
                lines.append(f"⏳ *𝐑𝐄𝐌𝐀𝐈𝐍𝐈𝐍𝐆:* `{remaining}` {'*(Limit Reached!)*' if remaining == 0 else ''}")
                lines.append("⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")
            
            # ফুটার
            lines.append("✨ *𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐔𝐒:* `𝐀𝐂𝐓𝐈𝐕𝐄`")
            
            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


    elif cmd == "/remain":
        all_user_ids = set(like_tracker.keys()) | set(vip_users.keys())

        if not all_user_ids:
            bot.reply_to(message, "❌ *No data available for today.*", parse_mode='Markdown')
        else:
            # হেডার - স্টাইলিশ বড় লেখা
            lines = [
                "📊 **USER USAGE DASHBOARD**",
                "⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️"
            ]

            for uid in all_user_ids:
                usage = like_tracker.get(uid, {'used': 0})
                used = usage.get('used', 0)
                limit = get_user_limit(uid)
                remaining = max(0, limit - used)

                # প্রোগ্রেস বার (৮টি ব্লক)
                filled = int((used / limit) * 8) if limit > 0 else 0
                bar = '🟩' * filled + '⬜' * (8 - filled)

                # লিমিট চেক
                status_icon = "🚫" if remaining == 0 else "✅"

                try:
                    chat_member = bot.get_chat_member(message.chat.id, uid)
                    # পুরো নাম পাওয়ার চেষ্টা
                    fname = chat_member.user.first_name
                    lname = chat_member.user.last_name or ""
                    full_name = f"{fname} {lname}".strip()

                    # নাম বড় হলে ছোট করা (২০ ক্যারেক্টার লিমিট)
                    display_name = (full_name[:20] + '..') if len(full_name) > 20 else full_name
                    user_display = f"[{display_name}](tg://user?id={uid})"
                except:
                    user_display = f"`{uid}`"

                # আউটপুট লাইন - স্টাইলিশ বোল্ড ফন্ট সহ
                limit_reached_text = " *(Limit Reached!)*" if remaining == 0 else ""
                
                lines.append(f"{status_icon} *USER:* {user_display}")
                lines.append(f"📊 *USAGE:* `{bar}` ({used}/{limit})")
                lines.append(f"⏳ *REMAINING:* `{remaining}`{limit_reached_text}")
                lines.append("⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")

            # ফুটার
            lines.append("✨ *BOT STATUS:* `ACTIVE`")
            bot.reply_to(message, "\n".join(lines), parse_mode='Markdown')

    elif cmd == "/runalljobs":  # ইনডেন্টেশন একদম সোজা করে দেওয়া হয়েছে
        if not auto_like_jobs:
            bot.reply_to(message, "📝 No active auto like jobs to run.")
            return

        bot.reply_to(message, f"🚀 Manually triggering all `{len(auto_like_jobs)}` jobs now... (next_run schedule unchanged)")

        def run_all_now():
            # --- Owner Info ---
            owner_id = 8408849795   
            owner_name = "𝐒𝐚𝐢𝐝𝐮𝐥 _ 𝐎𝐟𝐟𝐢𝐜𝐢𝐚𝐥" 
            owner_link = f"[{owner_name}](tg://user?id={owner_id})"
            # ------------------

            jobs = sorted(auto_like_jobs.items(), key=lambda x: 1 if x[1]['region'].lower() in LATE_REGIONS else 0)
            success, failed = 0, 0
            
            for job_id, job_data in list(jobs):
                try:
                    user_id = job_data['user_id']
                    region = job_data['region'].upper()
                    uid = job_data['uid']
                    chat_id = job_data['chat_id']
                    target = job_data.get('target_likes', 0)
                    delivered = job_data.get('delivered_likes', 0)

                    # --- REGION FLAGS ---
                    region_flags = {
                        "BD": "🇧🇩", "SG": "🇸🇬", "IND": "🇮🇳", "BR": "🇧🇷", 
                        "US": "🇺🇸", "PK": "🇵🇰", "ID": "🇮🇩", "TH": "🇹🇭", 
                        "VN": "🇻🇳", "RU": "🇷🇺", "ME": "🇲🇪"
                    }
                    flag = region_flags.get(region, "🌍")

                    response = call_api(region, uid)

                    try:
                        user_info = bot.get_chat(user_id)
                        first_name = user_info.first_name if user_info.first_name else f"User {user_id}"
                    except:
                        first_name = f"User {user_id}"
                    user_mention = f"[{first_name}](tg://user?id={user_id})"

                    if 'error' in response:
                        bot.send_message(
                            chat_id, 
                            f"⚠️ *[Manual] Auto Like Failed*\n\n"
                            f"👤 *User:* {user_mention}\n"
                            f"🆔 *UID:* `{uid}`\n"
                            f"{flag} *Region:* `{region}`\n"
                            f"🚫 *Error:* `{response['error']}`\n\n"
                            f"👑 *Owner* : {owner_link}", 
                            parse_mode='Markdown'
                        )
                        failed += 1
                        continue

                    if not isinstance(response, dict) or response.get('status') != 1:
                        bot.send_message(
                            chat_id, 
                            f"❌ *[Manual] Auto Like Failed*\n\n"
                            f"👤 *User:* {user_mention}\n"
                            f"🆔 *UID:* `{uid}`\n"
                            f"{flag} *Region:* `{region}`\n"
                            f"⚠️ Maximum likes reached for today.\n\n"
                            f"👑 *Owner* : {owner_link}", 
                            parse_mode='Markdown'
                        )
                        failed += 1
                        continue

                    player_name = response.get('PlayerNickname', 'N/A')
                    likes_given_str = str(response.get('LikesGivenByAPI', '0'))
                    total_likes = str(response.get('LikesafterCommand', 'N/A'))
                    utc_time = datetime.utcnow()

                    try:
                        likes_given_int = int(likes_given_str)
                    except ValueError:
                        likes_given_int = 0

                    new_delivered = delivered + likes_given_int
                    auto_like_jobs[job_id]['delivered_likes'] = new_delivered

                    remaining = max(0, target - new_delivered) if target > 0 else 0
                    progress_bar = make_progress_bar(new_delivered, target)
                    est_days = estimate_days(remaining) if target > 0 else 0

                    if target > 0:
                        target_info = (
                            f"🎯 *Target:* `{target}` likes\n"
                            f"📦 *Delivered:* `{new_delivered}` likes\n"
                            f"📊 *Progress:* {progress_bar}\n"
                            f"⏳ *Remaining:* `{remaining}` likes\n"
                            f"📅 *Est. Days Left:* `~{est_days}` days"
                        )
                    else:
                        target_info = (
                            f"🎯 *Target:* `♾️ Unlimited`\n"
                            f"📦 *Total Delivered:* `{new_delivered}` likes"
                        )

                    next_run_str = job_data['next_run'].strftime('%Y-%m-%d %H:%M:%S')
                    
                    auto_msg = (
                        f"🤖 *[Manual] Auto Like Executed*\n\n"
                        f"👤 *User:* {user_mention}\n"
                        f"👤 *Name:* `{player_name}`\n"
                        f"🆔 *UID:* `{uid}`\n"
                        f"{flag} *Region:* `{region}`\n"
                        f"📈 *Likes Added:* `{likes_given_str}`\n"
                        f"🗿 *Total Likes Now:* `{total_likes}`\n"
                        f"⏱ *Executed At:* `{utc_time.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
                        f"🔁 *Next Auto Like:* `{next_run_str} UTC`\n"
                        f"⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️\n"
                        f"{target_info}\n\n"
                        f"👑 *Owner* : {owner_link}"
                    )
                    bot.send_message(chat_id, auto_msg, parse_mode='Markdown')

                    save_job_to_db(job_id, auto_like_jobs[job_id])

                    if target > 0 and new_delivered >= target:
                        delete_job_from_db(job_id)
                        auto_like_jobs.pop(job_id, None)
                        bot.send_message(
                            chat_id,
                            f"🏁 *Target Reached!*\n\n"
                            f"👤 *User:* {user_mention}\n"
                            f"🆔 *UID:* `{uid}`\n"
                            f"{flag} *Region:* `{region}`\n"
                            f"🎯 *Target:* `{target}` likes\n"
                            f"📦 *Delivered:* `{new_delivered}/{target}` likes\n\n"
                            f"👑 *Owner* : {owner_link}",
                            parse_mode='Markdown'
                        )

                    success += 1

                except Exception as e:
                    logger.error(f"Manual run error for job {job_id}: {e}")
                    failed += 1

            bot.send_message(
                message.chat.id, 
                f"✅ *Manual Run Complete!*\n\n"
                f"✔️ *Success:* `{success}`\n"
                f"❌ *Failed:* `{failed}`\n\n"
                f"👑 *Owner* : {owner_link}", 
                parse_mode='Markdown'
            )

        threading.Thread(target=run_all_now, daemon=True).start()



    # /add <uid> <likes> — Add likes to target
    elif cmd == "/add" and len(args) == 3:
        try:
            target_uid = args[1]
            extra_likes = int(args[2])

            # OWNER DETAILS
            owner_id = 8408849795
            owner_name = "𝐒𝐚𝐢𝐝𝐮𝐥 _ 𝐎𝐟𝐟𝐢𝐜𝐢𝐚𝐥"
            owner_link = f"[{owner_name}](tg://user?id={owner_id})"

            if extra_likes <= 0:
                bot.reply_to(
                    message,
                    "⚠️ *Likes must be a positive number.*",
                    parse_mode="Markdown"
                )
                return

            matched_jobs = [
                (jid, jdata)
                for jid, jdata in auto_like_jobs.items()
                if jdata['uid'] == target_uid
            ]

            if not matched_jobs:
                bot.reply_to(
                    message,
                    f"❌ *No active auto-like job found for UID* `{target_uid}`.",
                    parse_mode="Markdown"
                )
                return

            updated_responses = []

            for job_id, job_data in matched_jobs:
                current_target = job_data.get('target_likes', 0)
                region = job_data.get('region', 'N/A').upper()
                user_id = job_data.get('user_id')

                # REGION FLAGS
                region_flags = {
                    "BD": "🇧🇩", "SG": "🇸🇬", "IND": "🇮🇳", "BR": "🇧🇷",
                    "US": "🇺🇸", "PK": "🇵🇰", "ID": "🇮🇩", "TH": "🇹🇭",
                    "VN": "🇻🇳", "RU": "🇷🇺", "ME": "🇲🇪"
                }
                flag = region_flags.get(region, "🌍")

                # USER INFO
                try:
                    user_info = bot.get_chat(user_id)
                    first_name = user_info.first_name if user_info.first_name else f"User {user_id}"
                except:
                    first_name = f"User {user_id}"

                user_mention = f"[{first_name}](tg://user?id={user_id})"

                # UNLIMITED CASE
                if current_target == 0:
                    updated_responses.append(
                        f"👤 *User:* {user_mention}\n"
                        f"🆔 *Job ID:* `{job_id}`\n"
                        f"{flag} *Region:* `{region}`\n"
                        f"📊 *Status:* `♾️ Unlimited (unchanged)`"
                    )
                    continue

                # NEW TARGET
                new_target = current_target + extra_likes
                auto_like_jobs[job_id]['target_likes'] = new_target

                save_job_to_db(job_id, auto_like_jobs[job_id])

                delivered = job_data.get('delivered_likes', 0)
                remaining = max(0, new_target - delivered)
                est = estimate_days(remaining)

                # FORMATTED BLOCK
                job_block = (
                    f"👤 *User:* {user_mention}\n"
                    f"🆔 *Job ID:* `{job_id}`\n"
                    f"{flag} *Region:* `{region}`\n"
                    f"🎯 *New Target:* `{new_target}` likes\n"
                    f"📦 *Delivered:* `{delivered}` likes\n"
                    f"⏳ *Remaining:* `{remaining}` likes\n"
                    f"📅 *Estimated:* `~{est} days`"
                )
                updated_responses.append(job_block)

            # FINAL MESSAGE FORMATION
            final_lines = [
                "✅ *Likes Added Successfully*\n",
                f"🆔 *UID:* `{target_uid}`",
                f"📈 *Added:* `+{extra_likes}` likes",
                "⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️"
            ]

            for resp in updated_responses:
                final_lines.append(resp)
                final_lines.append("⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")

            final_lines.append(f"\n👑 *Owner* : {owner_link}")
            full_message = "\n".join(final_lines)

            bot.reply_to(message, full_message, parse_mode="Markdown")

        except ValueError:
            bot.reply_to(
                message,
                "⚠️ *Invalid format.*\nUse: `/add <uid> <likes>`",
                parse_mode="Markdown"
            )
        except Exception as e:
            bot.reply_to(
                message,
                f"❌ *Error:* `{e}`",
                parse_mode="Markdown"
            )

    # /change <old_uid> <new_uid> — Change UID in existing autolike jobs
    elif cmd == "/change" and len(args) == 3:
        try:
            old_uid = args[1]
            new_uid = args[2]

            if not old_uid.isdigit() or not new_uid.isdigit():
                bot.reply_to(message, "⚠️ Both UIDs must be numeric.", parse_mode="Markdown")
                return

            matched_jobs = [(jid, jdata) for jid, jdata in auto_like_jobs.items() if jdata['uid'] == old_uid]

            if not matched_jobs:
                bot.reply_to(message, f"❌ No active auto-like job found for UID `{old_uid}`.", parse_mode="Markdown")
                return

            changed = []
            for job_id, job_data in matched_jobs:
                auto_like_jobs[job_id]['uid'] = new_uid
                # Need to re-save to DB (delete old and insert with same job_id but new uid)
                save_job_to_db(job_id, auto_like_jobs[job_id])
                changed.append(job_id)

            lines = [f"✅ *UID Changed Successfully!*\n"]
            lines.append(f"🔄 `{old_uid}` → `{new_uid}`\n")
            for job_id in changed:
                lines.append(f"🆔 Job: `{job_id}`")
            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")

        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")

# /mystats command — with all server flags added
@bot.message_handler(commands=['mystats'])
def mystats_command(message):
    user_id = message.from_user.id
    
    # --- Owner Info ---
    owner_link = "[𝐒𝐚𝐢𝐝𝐮𝐥 _ 𝐎𝐟𝐟𝐢𝐜𝐢𝐚𝐥](tg://user?id=8408849795)"
    # ------------------

    # Comprehensive Server Flags Mapping (Original file change na kore add kora hoyeche)
    flags = {
        "BD": "🇧🇩", "BANGLADESH": "🇧🇩",
        "IND": "🇮🇳", "INDIA": "🇮🇳",
        "PK": "🇵🇰", "PAKISTAN": "🇵🇰",
        "NP": "🇳🇵", "NEPAL": "🇳🇵",
        "SG": "🇸🇬", "SINGAPORE": "🇸🇬",
        "BR": "🇧🇷", "BRAZIL": "🇧🇷",
        "RU": "🇷🇺", "RUSSIA": "🇷🇺",
        "ID": "🇮🇩", "INDONESIA": "🇮🇩",
        "TW": "🇹🇼", "TAIWAN": "🇹🇼",
        "TH": "🇹🇭", "THAILAND": "🇹🇭",
        "VN": "🇻🇳", "VIETNAM": "🇻🇳",
        "MY": "🇲🇾", "MALAYSIA": "🇲🇾",
        "ME": "🇦🇪", "MIDDLE EAST": "🇦🇪",
        "EU": "🇪🇺", "EUROPE": "🇪🇺",
        "NA": "🇺🇸", "NORTH AMERICA": "🇺🇸",
        "US": "🇺🇸", "USA": "🇺🇸",
        "SA": "🇦🇷", "SOUTH AMERICA": "🇦🇷",
        "LATAM": "🇲🇽", "MEXICO": "🇲🇽",
        "MENA": "🇪🇬", "AFRICA": "🇿🇦"
    }

    if not is_user_in_channel(user_id):
        markup = InlineKeyboardMarkup()
        for channel in REQUIRED_CHANNELS:
            markup.add(InlineKeyboardButton(f"🔗 Join {channel}", url=f"https://t.me/{channel.strip('@')}") )
        
        # Owner added here
        bot.reply_to(message, 
            "❌ You must join all our channels to use this command.\n\n"
            f"👑 **Owner** : {owner_link}", 
            reply_markup=markup, parse_mode="Markdown")
        return

    is_vip = user_id in vip_users
    user_jobs = [(jid, jdata) for jid, jdata in auto_like_jobs.items() if jdata['user_id'] == user_id]
    is_autolike = len(user_jobs) > 0

    user_mention = f"[{message.from_user.first_name}](tg://user?id={user_id})"
    
    sub_greetings = [
        "Hope you are having a fantastic day! 🌟",
        "It's great to see you again! 🎉",
        "Ready to boost your profile today? 🚀",
        "Keep grinding and staying awesome! 💪",
        "Your premium journey is going strong! 💎",
        "Welcome back to your dashboard! 👑",
        "Looking sharp today! Let's get some likes! 🔥",
        "Another day, another milestone! 🏆",
        "You're a VIP inside and out! 💫",
        "Hope you're enjoying your premium benefits! 🎁",
        "Time to level up your fame! 📈",
        "Glad to have you with us! 🤝"
    ]
    
    non_sub_greetings = [
        "Welcome to the bot! 👋",
        "Hope you are having a wonderful day! 🌟",
        "Looking to boost your profile? You're in the right place! 🚀",
        "Feel free to explore our premium plans! 💎",
        "Want to become famous? Let's get started! 🔥",
        "Grab a premium subscription and watch the magic! ✨",
        "Are you ready for your journey to the top? 🔝",
        "Join the premium club today! 👑",
        "Don't miss out on unlimited likes! ⚡",
        "Upgrade today to unlock all features! 🔓",
        "It's the perfect day to go Premium! 🎟️"
    ]

    if not is_vip and not is_autolike:
        # Not a subscriber
        random_greeting = random.choice(non_sub_greetings)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💎 Buy VIP / Auto Like", url="https://t.me/saidulbhai34"))
        markup.add(InlineKeyboardButton("📢 Join Report Group", url=GROUP_JOIN_LINK))
        
        # Owner added here
        bot.reply_to(message,
            f"👋 Hello {user_mention}, {random_greeting}\n\n"
            "📊 *Your Stats*\n\n"
            "❌ You don't have any active subscription.\n\n"
            "💡 *Get VIP or Auto Like to receive daily likes automatically!*\n"
            "📞 Contact owner to purchase.\n\n"
            f"👑 **Owner** : {owner_link}",
            parse_mode="Markdown", reply_markup=markup)
        return

    random_greeting = random.choice(sub_greetings)
    lines = [f"👋 Hello {user_mention}, {random_greeting}\n"]
    lines.append("📊 *Your Premium Stats*\n")

    # VIP section
    if is_vip:
        vip = vip_users[user_id]
        target = vip.get('target_likes', 0)
        delivered = vip.get('delivered_likes', 0)
        daily_limit = vip.get('limit', 0)
        remaining = max(0, target - delivered) if target > 0 else 0
        progress_bar = make_progress_bar(delivered, target)
        est_days = estimate_days(remaining) if target > 0 else 0

        # Daily usage
        usage = like_tracker.get(user_id, {"used": 0})
        used_today = usage.get("used", 0)

        lines.append("👑 *VIP Subscription*")
        lines.append(f"⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")
        lines.append(f"📊 *Daily Limit:* `{daily_limit}`/day")
        lines.append(f"📈 *Used Today:* `{used_today}`/{daily_limit}")

        if target > 0:
            lines.append(f"🎯 *Target Likes:* `{target}`")
            lines.append(f"📦 *Delivered:* `{delivered}`")
            lines.append(f"⏳ *Remaining:* `{remaining}`")
            lines.append(f"📊 *Progress:* {progress_bar}")
            lines.append(f"📅 *Est. Completion:* `~{est_days}` days _(~210-220/day)_")
        else:
            lines.append(f"🎯 *Target:* `♾️ Unlimited`")
            lines.append(f"📦 *Total Delivered:* `{delivered}`")
        lines.append("")

    # Auto-like section
    if is_autolike:
        lines.append("🤖 *Auto Like Subscriptions*")
        lines.append(f"⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")

        for job_id, job_data in user_jobs:
            target = job_data.get('target_likes', 0)
            delivered = job_data.get('delivered_likes', 0)
            remaining = max(0, target - delivered) if target > 0 else 0
            progress_bar = make_progress_bar(delivered, target)
            est_days = estimate_days(remaining) if target > 0 else 0
            
            # --- Auto Flag Logic ---
            reg_raw = str(job_data.get('region', 'Global')).upper()
            flag_icon = flags.get(reg_raw, "🌍")

            lines.append(f"\n🆔 *UID:* `{job_data['uid']}`")
            lines.append(f"{flag_icon} *Region:* `{reg_raw}`")

            if target > 0:
                lines.append(f"🎯 *Target:* `{target}` likes")
                lines.append(f"📦 *Delivered:* `{delivered}` likes")
                lines.append(f"⏳ *Remaining:* `{remaining}` likes")
                lines.append(f"📊 *Progress:* {progress_bar}")
                lines.append(f"📅 *Est. Completion:* `~{est_days}` days _(~210-220/day)_")
            else:
                lines.append(f"🎯 *Target:* `♾️ Unlimited`")
                lines.append(f"📦 *Total Delivered:* `{delivered}` likes")

            lines.append(f"🔁 *Next Run:* `{job_data['next_run'].strftime('%Y-%m-%d %H:%M:%S')} UTC`")
            lines.append("⚔️▬▬▬▬▬▬▬ஜ۩۞۩ஜ▬▬▬▬▬▬▬⚔️")

    # Negative likes info
    vip_entity = f"user_{user_id}"
    vip_neg = get_negative_likes(vip_entity)
    if vip_neg > 0:
        lines.append(f"\n⚠️ *VIP Negative Likes:* `-{vip_neg}` _(deducted from next VIP plan)_")

    user_uids = set()
    for jid, jdata in auto_like_jobs.items():
        if jdata['user_id'] == user_id:
            user_uids.add(jdata['uid'])
    
    for uid in user_uids:
        uid_entity = f"uid_{uid}"
        neg_amount = get_negative_likes(uid_entity)
        if neg_amount > 0:
            lines.append(f"\n⚠️ *Auto Like Negative (UID {uid}):* `-{neg_amount}` _(deducted from next plan)_")

    # Final Owner added for Premium users
    lines.append(f"\n👑 **Owner** : {owner_link}")

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💎 Upgrade / Renew", url="https://t.me/saidulbhai34"))
    markup.add(InlineKeyboardButton("📢 Report Group", url=GROUP_JOIN_LINK))

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)


@bot.message_handler(commands=['help'])
def help_command(message):
    user_id = message.from_user.id
    
    # ওনারের রিয়েল নাম বের করা (সবাই দেখতে পাবে)
    try:
        owner_chat = bot.get_chat(OWNER_ID)
        real_name = owner_chat.first_name
    except:
        real_name = "Owner"

    markup = InlineKeyboardMarkup()
    
    # ১. প্রথমে একটি বাটন
    markup.add(InlineKeyboardButton("🆘 /help", callback_data="show_help"))
    
    # ২. বাটনগুলো 2x2 আকারে
    btns = [
        InlineKeyboardButton("🧑‍💻 /like", callback_data="show_like"),
        InlineKeyboardButton("🔰 /start", callback_data="show_start"),
        InlineKeyboardButton("📊 /mystats", callback_data="show_mystats"),
        InlineKeyboardButton("🆘 /help", callback_data="show_help")
    ]
    
    for i in range(0, len(btns), 2):
        markup.row(*btns[i:i+2])
    
    # ৩. ওনারের কমান্ড বাটন (শুধুমাত্র ওনারের জন্য)
    if user_id == OWNER_ID:
        owner_btns = [
            InlineKeyboardButton("💎 /vip", callback_data="show_vip"),
            InlineKeyboardButton("❌ /rmvip", callback_data="show_rmvip"),
            InlineKeyboardButton("📈 /remain", callback_data="show_remain"),
            InlineKeyboardButton("🤖 /autolike", callback_data="show_autolike"),
            InlineKeyboardButton("🗑️ /rmautolike", callback_data="show_rmautolike"),
            InlineKeyboardButton("📋 /autolikelist", callback_data="show_autolikelist"),
            InlineKeyboardButton("🚀 /runalljobs", callback_data="show_runalljobs"),
            InlineKeyboardButton("➕ /add", callback_data="show_add"),
            InlineKeyboardButton("🔄 /change", callback_data="show_change")
        ]
        for i in range(0, len(owner_btns), 2):
            markup.row(*owner_btns[i:i+2])
    
    # ৪. ওনার নেম ও সাপোর্ট বাটন (সবাই দেখতে পাবে)
    markup.add(InlineKeyboardButton(f"👑 Owner ➜ {real_name}", url=f"tg://user?id={OWNER_ID}"))
    markup.add(InlineKeyboardButton("📞 Support: @saidulbhai34", url="https://t.me/saidulbhai34"))
    
    help_text = """
╭━━━━━━━━━━━━━━━━━━╮
   🌐 𝐇𝐄𝐋𝐏 𝐌𝐄𝐍𝐔
╰━━━━━━━━━━━━━━━━━━╯

✨ নিচের অপশনগুলো থেকে নির্বাচন করুন
👇 Click on the buttons below:
    """
    
    bot.send_message(message.chat.id, help_text, reply_markup=markup, parse_mode="Markdown")

# বাটনে ক্লিক করলে ফরম্যাট দেখানোর হ্যান্ডলার
@bot.callback_query_handler(func=lambda call: call.data.startswith("show_"))
def callback_handler(call):
    cmd = call.data.split("_")[1]
    formats = {
        "like": "/like <region> <uid>",
        "start": "/start",
        "mystats": "/mystats",
        "help": "/help",
        "vip": "/vip <userid> <limit> <target_like>",
        "rmvip": "/rmvip <userid>",
        "remain": "Check remaining status",
        "autolike": "/autolike <userid> <region> <uid> [target_like]",
        "rmautolike": "/rmautolike <job_id>",
        "autolikelist": "View auto-like list",
        "runalljobs": "Run all jobs",
        "add": "/add <uid> <likes>",
        "change": "/change <old_uid> <new_uid>"
    }
    
    if cmd in formats:
        msg = f"💠 *Command Details*\n\n✅ *Action:* `/{cmd}`\n💡 *Format:* `{formats[cmd]}`"
    else:
        msg = "⚠️ *কমান্ডটি খুঁজে পাওয়া যায়নি।*"
    bot.answer_callback_query(call.id, text=f"Details for {cmd}")
    bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(func=lambda message: True, content_types=['text'])
def reply_all(message):
    if message.text.startswith('/'):
        # Handle unknown commands - only reply if it's actually an unknown command
        known_commands = ['/start', '/like', '/help', '/vip', '/rmvip', '/remain', '/autolike', '/rmautolike', '/autolikelist', '/add', '/mystats', '/change']
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
