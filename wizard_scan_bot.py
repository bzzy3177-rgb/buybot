"""
Wizard Scan Buy Bot — minimal build
Features:
  1. CA / chart-link paste -> live token snapshot (DexScreener + best-effort
     security data). Works for everyone in DM, and in any group/channel
     where the bot currently has admin rights.
  2. When someone makes the bot admin in a group or channel, the owner gets
     a DM: who did it + which group/channel.
  3. /ownerhelp — owner-only panel with two buttons:
       - Broadcast: DM all (or selected) users who ever started the bot
       - Admin Groups: list every group/channel the bot is admin in right now
"""

import os
import re
import json
import time
import html
import logging
import asyncio
import functools
from pathlib import Path

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ChatMemberHandler,
    ApplicationHandlerStop,
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs (incl. bot token) at INFO

# ─── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", "6018602211") or 0)
OWNER_ID_2 = int(os.environ.get("OWNER_ID_2", "0") or 0)
OWNER_IDS = [oid for oid in [OWNER_ID, OWNER_ID_2] if oid]

BOT_USERNAME = "@WizardScanBuyBot"

# ─── Storage (plain JSON files — survives Railway redeploys if you attach a volume) ──
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE        = DATA_DIR / "users.json"
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


def save_users(d: dict):
    save_json(USERS_FILE, d)


def add_user(uid: int, username: str = None, name: str = None):
    d = load_users()
    key = str(uid)
    entry = d.get(key, {})
    entry["id"] = uid
    entry["username"] = username or entry.get("username")
    entry["name"] = name or entry.get("name")
    entry["last_seen"] = time.time()
    d[key] = entry
    save_users(d)


def load_admin_groups() -> dict:
    return load_json(ADMIN_GROUPS_FILE, {})


def save_admin_groups(d: dict):
    save_json(ADMIN_GROUPS_FILE, d)


# ─── Owner-only decorator ────────────────────────────────────────────────────
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
            logger.warning(f"notify_owners: failed to DM owner {oid}: {e}")


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


# ─── CA / chart-link token snapshot ──────────────────────────────────────────
CA_SOL_RE = re.compile(r'(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])')
CA_EVM_RE = re.compile(r'(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])')
CHART_LINK_RE = re.compile(
    r'https?://(?:www\.)?(?:'
    r'dexscreener\.com/[a-zA-Z\-]+/(?P<ds_addr>[A-Za-z0-9]+)'
    r'|dextools\.io/app/[a-zA-Z\-]+/pair-explorer/(?P<dt_addr>[A-Za-z0-9]+)'
    r'|birdeye\.so/token/(?P<be_addr>[A-Za-z0-9]+)'
    r'|geckoterminal\.com/[a-zA-Z\-]+/pools/(?P<gt_addr>[A-Za-z0-9]+)'
    r'|pump\.fun/(?:coin/)?(?P<pf_addr>[A-Za-z0-9]+)'
    r')', re.I)

SUPPORTED_CHAINS = {
    "solana": "SOL", "ethereum": "ETH", "bsc": "BSC", "base": "BASE",
    "arbitrum": "ARB", "polygon": "POLY", "avalanche": "AVAX", "ton": "TON",
}
_GOPLUS_CHAIN_IDS = {"ETH": "1", "BSC": "56", "BASE": "8453"}


def extract_ca_from_text(text: str):
    """Pull a bare contract address out of `text` — from a known chart link
    or a raw Solana/EVM address. Returns None if nothing usable is found."""
    if not text:
        return None
    m = CHART_LINK_RE.search(text)
    if m:
        for key in ("ds_addr", "dt_addr", "be_addr", "gt_addr", "pf_addr"):
            addr = m.group(key)
            if addr:
                return addr
    m = CA_EVM_RE.search(text)
    if m:
        return m.group(0)
    m = CA_SOL_RE.search(text)
    if m:
        return m.group(0)
    return None


def fmt_mc(value):
    """e.g. 12.5K, 3.45M, 159.26K — no currency sign, no MC label."""
    if not value:
        return "N/A"
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value/1_000:.1f}K"
    return f"{value:.0f}"


def _fmt_usd(value):
    return "N/A" if not value else f"${fmt_mc(value)}"


def _fmt_age(ts_ms):
    if not ts_ms:
        return "N/A"
    try:
        secs = max(0, time.time() - (float(ts_ms) / 1000.0))
    except Exception:
        return "N/A"
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    return f"{h}h {m}m" if h else f"{m}m"


_dex_session = requests.Session()


def _dex_get(url, params=None, timeout=10):
    try:
        r = _dex_session.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            return 429
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"dex_get failed ({url}): {e}")
        return None


def fetch_dex_data(ca: str):
    """Fetch live token data from DexScreener (sync, run via asyncio.to_thread)."""
    data = _dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
    pairs = (data or {}).get("pairs") or [] if data and data != 429 else []

    if not pairs:
        sdata = _dex_get(f"https://api.dexscreener.com/latest/dex/search", params={"q": ca})
        if sdata and sdata != 429:
            pairs = [
                p for p in (sdata.get("pairs") or [])
                if ca.lower() in (
                    (p.get("baseToken", {}).get("address", "") or "").lower(),
                    (p.get("quoteToken", {}).get("address", "") or "").lower(),
                )
            ]
    if not pairs:
        return None

    sup = [p for p in pairs if p.get("chainId", "").lower() in SUPPORTED_CHAINS]
    if not sup:
        return None
    wanted = ca.lower()
    filtered = [p for p in sup if ((p.get("baseToken") or {}).get("address") or "").lower() == wanted]
    if filtered:
        sup = filtered
    sup = sorted(sup, key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0) or 0, reverse=True)
    best = sup[0]

    chain = SUPPORTED_CHAINS[best.get("chainId", "").lower()]
    price = float(best.get("priceUsd") or 0)
    mc = float(best.get("marketCap") or best.get("fdv") or 0)
    symbol = (best.get("baseToken") or {}).get("symbol", "")
    name = (best.get("baseToken") or {}).get("name", "")
    if price <= 0 and mc <= 0 and not symbol:
        return None

    socials = (best.get("info") or {}).get("socials") or []
    tg_link = ""
    for s in socials:
        if "t.me" in (s.get("url", "") or "") or "telegram" in (s.get("type", "") or "").lower():
            tg_link = s.get("url", "")
            break

    return {
        "chain": chain,
        "name": name,
        "symbol": symbol,
        "price": price,
        "mcap": mc,
        "dex": best.get("dexId", ""),
        "listed_at": best.get("pairCreatedAt"),
        "volume_h24": float((best.get("volume") or {}).get("h24") or 0),
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
        "txns_buys": int(((best.get("txns") or {}).get("h24") or {}).get("buys") or 0),
        "txns_sells": int(((best.get("txns") or {}).get("h24") or {}).get("sells") or 0),
        "price_change_h1": float((best.get("priceChange") or {}).get("h1") or 0),
        "tg_link": tg_link,
    }


def fetch_solana_security(addr: str):
    """Best-effort Solana renounce/holders via RugCheck's public API. Never raises."""
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report", timeout=6)
        if r.status_code != 200:
            return {}
        d = r.json() or {}
        out = {}
        if isinstance(d.get("totalHolders"), int):
            out["holders"] = d["totalHolders"]
        if "mintAuthority" in d:
            out["renounced"] = d.get("mintAuthority") in (None, "", "11111111111111111111111111111111")
        return out
    except Exception:
        return {}


def fetch_evm_security(addr: str, chain_label: str):
    """Best-effort EVM buy/sell tax + renounce via GoPlus Security API. Never raises."""
    gp_chain = _GOPLUS_CHAIN_IDS.get(chain_label)
    if not gp_chain:
        return {}
    try:
        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{gp_chain}",
            params={"contract_addresses": addr}, timeout=6)
        if r.status_code != 200:
            return {}
        d = ((r.json() or {}).get("result") or {}).get(addr.lower()) or {}
        if not d:
            return {}
        out = {}
        if d.get("buy_tax") not in (None, ""):
            out["buy_tax"] = round(float(d["buy_tax"]) * 100, 1)
        if d.get("sell_tax") not in (None, ""):
            out["sell_tax"] = round(float(d["sell_tax"]) * 100, 1)
        owner_addr = (d.get("owner_address") or "").lower()
        if "owner_address" in d:
            out["renounced"] = owner_addr in (
                "", "0x0000000000000000000000000000000000000000",
                "0x000000000000000000000000000000000000dead")
        if str(d.get("holder_count") or "").isdigit():
            out["holders"] = int(d["holder_count"])
        return out
    except Exception:
        return {}


def format_token_snapshot(dex: dict, addr: str) -> str:
    name = dex.get("name") or "Unknown Token"
    symbol = (dex.get("symbol") or "").upper()
    title = f"{name} ({symbol})" if symbol else name

    holders = dex.get("holders")
    holders_s = f"{holders:,} (Traders)" if isinstance(holders, int) else "N/A"

    mcap_s = fmt_mc(dex.get("mcap"))

    buy_tax, sell_tax = dex.get("buy_tax"), dex.get("sell_tax")
    tax_s = f"{buy_tax if buy_tax is not None else 0}% buy / {sell_tax if sell_tax is not None else 0}% sell"

    price = dex.get("price") or 0
    price_s = f"${price:.10f}".rstrip("0").rstrip(".") if price else "N/A"

    dex_name = (dex.get("dex") or "N/A").upper()
    age_s = _fmt_age(dex.get("listed_at"))
    vol_s = (_fmt_usd(dex.get("volume_h24")) + " (24h)") if dex.get("volume_h24") else "N/A"
    liq_s = _fmt_usd(dex.get("liquidity_usd"))

    txns_b = dex.get("txns_buys") or 0
    txns_s = f"🟢 {txns_b:,} / 🔴{dex.get('txns_sells') or 0:,} (24h)"

    h1 = dex.get("price_change_h1")
    h1_s = f"{h1:+.2f}%" if h1 is not None else "N/A"

    renounced = dex.get("renounced")
    owner_s = "Renounced ✅" if renounced is True else ("Not renounced ⚠️" if renounced is False else "N/A")

    tg = dex.get("tg_link") or ""
    social_line = "TG ══ WEB ══ X" if tg else "N/A"

    return (
        f"🔮{title}\n\n"
        f"🔎TOKEN SNAPSHOT\n"
        f"├  Holders → {holders_s}\n"
        f"├  MCAP   → {mcap_s}\n"
        f"├  PEAK    → N/A\n"
        f"├  TAX      → {tax_s}\n"
        f"└  PRICE   → {price_s}\n\n"
        f"⚡️MARKET STATS\n"
        f"├  DEX     → {dex_name}\n"
        f"├  AGE     → {age_s}\n"
        f"├  VOL     → {vol_s}\n"
        f"└  LIQ      → {liq_s}\n\n"
        f"👤Security & Activity \n"
        f"├  TRADERS → N/A (24h)\n"
        f"├  TXNS       → {txns_s}\n"
        f"├  5M/1H     → N/A / {h1_s}\n"
        f"└  OWNER   → {owner_s}\n\n"
        f"🛡️ Social Media:\n"
        f"├ {social_line} ┤\n\n"
        f"<code>{html.escape(addr)}</code>"
    )


async def build_token_snapshot_text(addr: str):
    dex = await asyncio.to_thread(fetch_dex_data, addr)
    if not dex:
        return None
    try:
        chain = dex.get("chain")
        if chain == "SOL":
            sec = await asyncio.to_thread(fetch_solana_security, addr)
        elif chain in _GOPLUS_CHAIN_IDS:
            sec = await asyncio.to_thread(fetch_evm_security, addr, chain)
        else:
            sec = {}
        dex.update(sec)
    except Exception as e:
        logger.debug(f"security fetch failed for {addr[:12]}…: {e}")
    return format_token_snapshot(dex, addr)


async def handle_ca_lookup(msg, addr: str):
    status = await msg.reply_text("🔮 Scanning token…")
    try:
        snap = await build_token_snapshot_text(addr)
    except Exception as e:
        logger.warning(f"handle_ca_lookup failed for {addr[:12]}…: {e}")
        snap = None
    if snap:
        try:
            await status.delete()
        except Exception:
            pass
        await msg.reply_text(snap, parse_mode="HTML", disable_web_page_preview=True)
    else:
        try:
            await status.edit_text("⚠️ Is token ka data abhi nahi mila. Thodi der baad dobara try karo.")
        except Exception:
            pass


# ─── Public commands ──────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "🔮 <b>Wizard Scan Buy Bot</b>\n\n"
        "Kisi bhi token ka contract address (CA) ya chart link (Dexscreener / "
        "Dextools / Birdeye / GeckoTerminal / pump.fun) yahan paste karo — "
        "main uski live details bhej dunga.\n\n"
        "📋 Commands: /help",
        parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "📋 <b>Commands</b>\n\n"
        "/start — bot ke baare mein\n"
        "/help — yeh list\n\n"
        "🔮 <b>Token lookup:</b> koi bhi CA ya chart link paste karo, DM mein "
        "ya kisi group mein jahan mujhe admin banaya gaya ho.",
        parse_mode="HTML")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM-only: owner broadcast flow first, then CA/chart-link lookup for everyone."""
    msg = update.message
    u = update.effective_user
    if not msg or not u or not msg.text:
        return
    add_user(u.id, u.username, u.first_name)

    # Owner broadcast wizard takes priority over CA lookup in DM
    if u.id in OWNER_IDS and u.id in broadcast_state:
        handled = await handle_broadcast_flow_text(update, context)
        if handled:
            return

    if msg.text.startswith("/"):
        return  # unknown command — ignore

    addr = extract_ca_from_text(msg.text)
    if addr:
        await handle_ca_lookup(msg, addr)


# ─── Group gate: block everything except admin-group CA lookup ──────────────
async def block_non_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot does nothing in groups, EXCEPT: a CA/chart-link paste in a
    group where it currently has admin rights gets a snapshot reply."""
    if _is_private(update):
        return
    if getattr(update, "my_chat_member", None):
        return
    msg = update.effective_message
    chat = update.effective_chat
    text = (getattr(msg, "text", "") or "") if msg else ""
    if msg and chat and chat.type in ("group", "supergroup") and text and not text.startswith("/"):
        admin_groups = load_admin_groups()
        if str(chat.id) in admin_groups:
            addr = extract_ca_from_text(text)
            if addr:
                try:
                    await handle_ca_lookup(msg, addr)
                except Exception as e:
                    logger.warning(f"group CA lookup failed in {chat.id}: {e}")
    raise ApplicationHandlerStop


# ─── Admin status tracking + owner notification ──────────────────────────────
async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires whenever the bot's own status changes in any chat (group,
    supergroup or channel). Notifies the owner when the bot is made admin,
    and keeps the admin-groups store (used by the CA-lookup feature) in sync."""
    try:
        cm = update.my_chat_member
        if not cm:
            return
        chat = cm.chat
        old = cm.old_chat_member
        new = cm.new_chat_member
        actor = cm.from_user
        actor_label = (f"@{actor.username}" if actor and actor.username
                       else (f"ID {actor.id}" if actor else "Unknown"))

        if chat.type not in ("group", "supergroup", "channel"):
            return

        was_admin = bool(old and old.status == "administrator")
        is_admin = new.status == "administrator"
        groups = load_admin_groups()
        key = str(chat.id)
        title = chat.title or chat.username or str(chat.id)

        if is_admin and not was_admin:
            groups[key] = {"title": title, "username": chat.username or "", "type": chat.type}
            save_admin_groups(groups)
            kind_label = "Channel" if chat.type == "channel" else "Group"
            where = f"@{html.escape(chat.username)}" if chat.username else f"<code>{chat.id}</code>"
            await notify_owners(
                context.bot,
                f"👑 <b>Bot ko admin bana diya gaya!</b>\n\n"
                f"👤 Kisne: {html.escape(actor_label)}\n"
                f"📍 {kind_label}: <b>{html.escape(title)}</b> ({where})")
        elif was_admin and new.status != "administrator":
            if groups.pop(key, None) is not None:
                save_admin_groups(groups)
    except Exception as e:
        logger.warning(f"on_chat_member_update: {e}")


# ─── /ownerhelp panel ─────────────────────────────────────────────────────
OH_HOME_TEXT = (
    "🔮 <b>OWNER CONTROL PANEL</b>\n\n"
    "📢 <b>Broadcast</b> — sab (ya kuch select) users ko DM bhejo\n"
    "📋 <b>Admin Groups</b> — jin groups/channels mein bot abhi admin hai"
)


def _oh_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Broadcast", callback_data="oh:broadcast"),
        InlineKeyboardButton("📋 Admin Groups", callback_data="oh:admingroups"),
    ]])


@owner_only
async def cmd_ownerhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(OH_HOME_TEXT, parse_mode="HTML", reply_markup=_oh_kb())


async def _send_admin_groups_list(reply_target):
    groups = load_admin_groups()
    if not groups:
        await reply_target.reply_text(
            "📋 <b>Admin Groups</b>\n\nAbhi bot kisi group/channel mein admin nahi hai.",
            parse_mode="HTML")
        return
    lines = []
    for cid, info in groups.items():
        label = f"@{info.get('username')}" if info.get("username") else html.escape(info.get("title") or "Unknown")
        kind = "📢 Channel" if info.get("type") == "channel" else "👥 Group"
        lines.append(f"{kind} — <b>{label}</b> (<code>{cid}</code>)")
    await reply_target.reply_text(
        f"📋 <b>Bot Admin Hai In {len(groups)} Jagah:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML")


@owner_only
async def cmd_admin_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_admin_groups_list(update.message)


# ─── Broadcast: simple in-memory wizard ──────────────────────────────────────
# broadcast_state[owner_id] = {"stage": "pick" | "message", "targets": [...]}
broadcast_state: dict = {}


async def _broadcast_panel(reply_target, owner_id: int):
    users = load_users()
    if not users:
        await reply_target.reply_text("Abhi tak koi user nahi mila (kisi ne /start nahi kiya).")
        return
    broadcast_state[owner_id] = {"stage": "pick", "all_users": users}

    with_username = [v for v in users.values() if v.get("username")]
    no_username = len(users) - len(with_username)
    lines = [f"@{v['username']}" for v in with_username]
    no_uname_note = (
        f"\n⚠️ {no_username} user(s) have no username (will still receive if 'all' is used)."
        if no_username else "")

    CHUNK_CHARS = 3500
    CHUNK_MAX = 100
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        if cur and (len(cur) >= CHUNK_MAX or cur_len + len(line) + 1 > CHUNK_CHARS):
            chunks.append(cur); cur = []; cur_len = 0
        cur.append(line); cur_len += len(line) + 1
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = [[]]

    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        users_list = "\n".join(chunk)
        part_label = f" (Page {i}/{total})" if total > 1 else ""
        header = f"📢 <b>Broadcast — {len(users)} Users{part_label}</b>\n\n"
        body = f"<pre>{users_list}</pre>" if users_list else "<i>(no usernames in this part)</i>"
        footer = ""
        if i == total:
            footer = (
                f"{no_uname_note}\n\n"
                f"Reply with usernames (comma-separated):\n"
                f"Example: <code>@user1, @user2</code>\n\n"
                f"Or send <code>all</code> to broadcast to everyone.\n\n"
                f"/cancel to stop."
            )
        await reply_target.reply_text(header + body + footer, parse_mode="HTML")
        if total > 1 and i < total:
            await asyncio.sleep(0.3)


@owner_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _broadcast_panel(update.message, update.effective_user.id)


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if broadcast_state.pop(update.effective_user.id, None):
        await update.message.reply_text("✅ Broadcast cancel ho gaya.")
    else:
        await update.message.reply_text("Koi active broadcast nahi tha.")


async def handle_broadcast_flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if this message was consumed by the broadcast wizard."""
    uid = update.effective_user.id
    st = broadcast_state.get(uid)
    if not st:
        return False
    msg = update.message
    text = (msg.text or "").strip()

    if text.lower() == "/cancel":
        broadcast_state.pop(uid, None)
        await msg.reply_text("✅ Broadcast cancel ho gaya.")
        return True

    if st["stage"] == "pick":
        all_users = st["all_users"]
        if text.lower() == "all":
            targets = [int(k) for k in all_users.keys()]
        else:
            wanted = {u.strip().lstrip("@").lower() for u in text.split(",") if u.strip()}
            if not wanted:
                await msg.reply_text("⚠️ Username(s) bhejo, ya <code>all</code>.", parse_mode="HTML")
                return True
            targets = [
                int(k) for k, v in all_users.items()
                if (v.get("username") or "").lower() in wanted
            ]
            if not targets:
                await msg.reply_text("⚠️ Koi matching username nahi mila. Dobara try karo, ya /cancel.")
                return True
        broadcast_state[uid] = {"stage": "message", "targets": targets}
        await msg.reply_text(
            f"✅ <b>{len(targets)} target(s) selected.</b>\n\n"
            f"Ab jo bhi message bhejoge, wahi sab ko DM ho jayega.\n/cancel se rok sakte ho.",
            parse_mode="HTML")
        return True

    if st["stage"] == "message":
        targets = st["targets"]
        broadcast_state.pop(uid, None)
        status = await msg.reply_text(f"⏳ Sending to {len(targets)} user(s)…")
        ok = fail = 0
        for i, target_id in enumerate(targets):
            try:
                await context.bot.copy_message(
                    chat_id=target_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
                ok += 1
            except Exception as e:
                fail += 1
                logger.debug(f"broadcast to {target_id} failed: {e}")
            if (i + 1) % 25 == 0:
                await asyncio.sleep(1)
        await status.edit_text(f"✅ <b>Broadcast done.</b>\nSent: {ok} | Failed: {fail}", parse_mode="HTML")
        return True

    return False


async def cb_ownerhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if uid not in OWNER_IDS:
        await query.answer("Owner only.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    if data == "oh:broadcast":
        await _broadcast_panel(query.message, uid)
    elif data == "oh:admingroups":
        await _send_admin_groups_list(query.message)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set! Railway → Variables mein BOT_TOKEN add karo.")
        raise SystemExit(1)
    if not OWNER_IDS:
        logger.warning("⚠️ OWNER_ID not set — owner commands disabled.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Public commands (private chat only)
    app.add_handler(CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", cmd_help, filters=filters.ChatType.PRIVATE))

    # Owner commands (private chat only)
    app.add_handler(CommandHandler("ownerhelp", cmd_ownerhelp, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("admingroups", cmd_admin_groups, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=filters.ChatType.PRIVATE))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(cb_ownerhelp, pattern=r"^oh:"))

    # Group kill-switch (with the one CA-lookup exception) — must run before
    # anything else, so it's registered in an earlier handler group. Commands
    # above are private-only, so nothing group-side needs to be excluded here.
    app.add_handler(MessageHandler(filters.ALL, block_non_private), group=-1)

    # DM text (CA lookup + broadcast wizard)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message))

    # Bot's own admin-status changes anywhere (group/supergroup/channel)
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info(f"✅ Wizard Scan Buy Bot starting — Owner(s): {OWNER_IDS}")
    backoff = 5
    while True:
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)
            break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            logger.error(f"Polling crashed: {type(e).__name__}: {e} — restarting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
