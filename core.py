"""core.py - shared config, storage helpers, owner helpers, premium emoji IDs.
Imported by every other module. Has no dependency on them."""

import os
import re
import json
import time
import html
import logging
import asyncio
import functools
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChat,
)
from telegram.error import RetryAfter, BadRequest, Forbidden
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ChatMemberHandler,
    ApplicationHandlerStop,
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs (incl. bot token) at INFO

# --- Config ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "6018602211") or 0)
OWNER_ID_2 = int(os.environ.get("OWNER_ID_2", "0") or 0)
OWNER_IDS = [oid for oid in [OWNER_ID, OWNER_ID_2] if oid]

# Optional - the bot works without these, they just make it faster / more reliable
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
COINGECKO_PRO_API_KEY = os.environ.get("COINGECKO_PRO_API_KEY", "")  # only if you have a paid CoinGecko plan
MOBULA_API_KEY = os.environ.get("MOBULA_API_KEY", "")  # free key from admin.mobula.io - powers Snipers/Bundled %
# Premium emoji shown before the token name in buy-bot alerts ("" = plain emoji). Change without touching code.
BB_TITLE_EMOJI_ID = os.environ.get("BB_TITLE_EMOJI_ID", "5920537435996958710")
MAESTRO_LINK = os.environ.get("MAESTRO_LINK", "https://t.me/maestro?start=r-wizard_scan")  # your Maestro referral link
# Trade button: Maestro opens on the scanned token WITH your referral. {ca} = token address.
# Hardcoded on purpose (an old Railway variable was making the bot fall back to the plain link).
MAESTRO_TOKEN_LINK = "https://t.me/maestro?start={ca}_r-wizard_scan"
DEV_LINK = os.environ.get("DEV_LINK", "https://t.me/Wizard_Scan")  # Dev button
# Serialized Audit button. {chain} = Serialized chain symbol, {ca} = token address.
# NOTE: serializedaudit.io doesn't document a per-token page URL - test this pattern in a browser,
# and if the site uses another one, just change this variable on Railway (no code change needed).
AUDIT_LINK_TEMPLATE = os.environ.get(
    "AUDIT_LINK_TEMPLATE", "https://www.serializedaudit.io/?chain={chain}&address={ca}")
AUDIT_BUTTON_TEXT = os.environ.get("AUDIT_BUTTON_TEXT", "🛡️ SAFE OR RUG? · Instant AI Audit ⚡")
# DexScreener chainId -> Serialized Audit chain symbol
_AUDIT_CHAINS = {
    "robinhood": "ROBINHOOD", "bsc": "BSC", "base": "BASE", "ethereum": "ETH", "avalanche": "AVAX",
    "arbitrum": "ARB", "polygon": "POLYGON", "optimism": "OP", "linea": "LINEA", "mantle": "MANTLE",
    "sonic": "SONIC", "blast": "BLAST", "scroll": "SCROLL", "zksync": "ZKSYNC", "abstract": "ABSTRACT",
    "unichain": "UNICHAIN", "monad": "MONAD", "hyperevm": "HYPE", "ink": "INK", "story": "STORY",
    "solana": "SOLANA",
}

# --- Storage (JSON files; attach a Railway volume to keep them across deploys) -
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
ADMIN_GROUPS_FILE = DATA_DIR / "admin_groups.json"


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text() or "null") or default
    except Exception as e:
        logger.warning(f"load_json({path}) failed: {e}")
    return default


def save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"save_json({path}) failed: {e}")


def load_users() -> dict:
    return load_json(USERS_FILE, {})


def add_user(uid: int, username: str = None, name: str = None):
    d = load_users()
    key = str(uid)
    entry = d.get(key, {})
    entry["id"] = uid
    entry["username"] = username or entry.get("username")
    entry["name"] = name or entry.get("name")
    entry["last_seen"] = time.time()
    d[key] = entry
    save_json(USERS_FILE, d)


def load_admin_groups() -> dict:
    return load_json(ADMIN_GROUPS_FILE, {})


def save_admin_groups(d: dict):
    save_json(ADMIN_GROUPS_FILE, d)


# --- Owner helpers -------------------------------------------------------------
def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in OWNER_IDS:
            if update.message:
                await update.message.reply_text("⛔ Owner only.")
            return
        return await func(update, context)
    return wrapper


async def notify_owners(bot, text: str):
    for oid in OWNER_IDS:
        try:
            await bot.send_message(oid, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"notify_owners: could not DM owner {oid}: {e}")


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")



# Premium (custom) emoji IDs. Change an ID here to change that emoji; leave an
# ID empty ("") to use the normal emoji. If custom emojis are not allowed for the
# bot, Telegram just shows the normal emoji, and if a send ever fails we retry
# with plain emojis.
EMOJI_IDS = {
    "title":    "5807491057792853272",   # before the token name
    "snapshot": "5846193940105012285",   # TOKEN SNAPSHOT
    "security": "5846117180449497406",   # Security & Activity
    "market":   "6044166381690166762",   # MARKET STATS
    "holders":  "6044189703362584375",   # HOLDER ANALYSIS
    "social":   "5848118295907017044",   # SOCIAL MEDIA
    "arrow":    "5807952487604297661",   # arrow between label and value
    "buy":      "5895655127083130396",
    "sell":     "5895652464203407018",
    "yes":      "5895537926015557276",   # check mark
    "no":       "5897632955227972480",   # cross
    # row-leading emojis (used exactly as in the template)
    "r1":       "5895406615980418428",
    "r2":       "5895698643691774841",
    "r3":       "5895305301996871557",
    "r4":       "5895213750473990172",
    "r5":       "5895392498422915334",
    "lp_hold":  "5897894424247016568",   # LP Locked + Traders rows only
    # /start welcome message
    "w_welcome": "6044119257308995249",
    "w_more":    "5904530561036197958",
    "w_feat":    "5904447835671108691",
    # /buybot alert template - paste the premium emoji IDs here ("" = normal emoji)
    # banner: 8 different emojis, glued together with NO space (also used at the start of /buybot)
    "bb_top1":   "5920228301430858034",
    "bb_top2":   "5920021619014639909",
    "bb_top3":   "5920514062784930174",
    "bb_top4":   "5920490049622777493",
    "bb_top5":   "5920338282658407322",
    "bb_top6":   "5920317263088459932",
    "bb_top7":   "5920478938542382936",
    "bb_top8":   "5920542916375227763",
    "bb_name":   BB_TITLE_EMOJI_ID,   # premium emoji before the token name (alert title)
    "bb_coin":   "",   # coin row 🪙 (x33)
    "bb_mc":     "5920376769860346219",   # Market Cap
    "bb_pay":    "5920245842077293869",   # WBNB / paid row
    "bb_buyer":  "5920082221003186548",   # Buyer | Txn
    "bb_got":    "5920324255295217761",   # Got
    # button icons
    "bb_btn_chart": "5920194551577845694",
    "bb_btn_trade": "5920427562143589201",
    "bb_btn_calls": "5920146301915243170",
}


def em(key: str, fallback: str, premium: bool = True) -> str:
    eid = EMOJI_IDS.get(key)
    if premium and eid:
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback



# --- Group gate: block everything except the admin-group CA lookup ---------------------------
_admin_check_cache: dict = {}  # chat_id -> (timestamp, is_admin)
ADMIN_RECHECK_SECS = 120


async def bot_is_admin_here(context, chat) -> bool:
    """True if the bot is admin in `chat`. Checks the saved list first, then asks
    Telegram directly - so it still works after a redeploy wipes the saved list."""
    groups = load_admin_groups()
    if str(chat.id) in groups:
        return True
    hit = _admin_check_cache.get(chat.id)
    if hit and time.time() - hit[0] < ADMIN_RECHECK_SECS:
        return hit[1]
    is_admin = False
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        is_admin = member.status == "administrator"
        if is_admin:
            groups[str(chat.id)] = {"title": chat.title or str(chat.id),
                                    "username": chat.username or "", "type": chat.type}
            save_admin_groups(groups)
    except Exception as e:
        logger.debug(f"admin check failed in {chat.id}: {e}")
    _admin_check_cache[chat.id] = (time.time(), is_admin)
    return is_admin

