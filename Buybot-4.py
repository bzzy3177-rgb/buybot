"""
Wizard Scan Buy Bot - minimal build (multi-chain, English)

Features:
  1. Paste a contract address (CA) or a chart link -> live token snapshot.
     Works on every chain DexScreener supports (Solana, Ethereum, BSC, Base,
     Arbitrum, Polygon, Avalanche, Optimism, TON, Sui, Tron, ...).
     Works in DM for everyone, and in any group where the bot is admin.
  2. When someone makes the bot admin in a group or channel, the owner gets
     a DM with who did it and where.
  3. /ownerhelp - owner-only panel with two buttons:
       - Broadcast: DM all (or selected) users who ever started the bot
       - Admin Groups: every group/channel the bot is admin in right now
"""

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
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
)
from telegram.error import RetryAfter, BadRequest
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


# --- CA / chart-link detection ---------------------------------------------------
CA_SUI_RE = re.compile(r'(?<![A-Za-z0-9])0x[a-fA-F0-9]{64}::\w+::\w+')  # Sui/Aptos coin type (suffix required, so plain tx hashes never match)
CA_EVM_RE = re.compile(r'(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])')
CA_TON_RE = re.compile(r'(?<![A-Za-z0-9_\-])(?:EQ|UQ)[A-Za-z0-9_\-]{46}(?![A-Za-z0-9_\-])')
CA_TRON_RE = re.compile(r'(?<![A-Za-z0-9])T[1-9A-HJ-NP-Za-km-z]{33}(?![A-Za-z0-9])')
CA_SOL_RE = re.compile(r'(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])')

CHART_LINK_RE = re.compile(
    r'https?://(?:www\.)?(?:'
    r'dexscreener\.com/(?P<ds_chain>[a-zA-Z0-9\-]+)/(?P<ds_addr>[A-Za-z0-9]+)'
    r'|dextools\.io/app/[a-zA-Z\-]+/(?P<dt_chain>[a-zA-Z0-9\-]+)/pair-explorer/(?P<dt_addr>[A-Za-z0-9]+)'
    r'|birdeye\.so/(?:solana/)?token/(?P<be_addr>[A-Za-z0-9]+)'
    r'|geckoterminal\.com/(?P<gt_chain>[a-zA-Z0-9_\-]+)/pools/(?P<gt_addr>[A-Za-z0-9]+)'
    r'|pump\.fun/(?:coin/)?(?P<pf_addr>[A-Za-z0-9]+)'
    r'|gmgn\.ai/(?P<gm_chain>[a-zA-Z]+)/token/(?:\w+?_)?(?P<gm_addr>[A-Za-z0-9]{32,})'
    r'|photon-sol\.tinyastro\.io/[a-z]+/lp/(?P<ph_addr>[A-Za-z0-9]+)'
    r')', re.I)

# Different sites name chains differently - normalise to DexScreener's chainId
_CHAIN_ALIASES = {
    "eth": "ethereum", "ether": "ethereum", "bnb": "bsc", "polygon_pos": "polygon",
    "matic": "polygon", "avax": "avalanche", "sol": "solana", "arb": "arbitrum",
    "op": "optimism", "trx": "tron",
}
_LINK_DEFAULT_CHAIN = {"pf": "solana", "ph": "solana"}


def extract_ca_from_text(text: str):
    """Return (address, chain_hint) or None. chain_hint is a DexScreener chainId
    when the link tells us the chain, otherwise None."""
    if not text:
        return None
    m = CHART_LINK_RE.search(text)
    if m:
        gd = m.groupdict()
        for prefix in ("ds", "dt", "be", "gt", "pf", "gm", "ph"):
            addr = gd.get(f"{prefix}_addr")
            if addr:
                chain = (gd.get(f"{prefix}_chain") or _LINK_DEFAULT_CHAIN.get(prefix) or "").lower()
                chain = _CHAIN_ALIASES.get(chain, chain) or None
                return addr, chain
    for rx in (CA_SUI_RE, CA_EVM_RE, CA_TON_RE, CA_TRON_RE, CA_SOL_RE):
        m = rx.search(text)
        if m:
            return m.group(0), None
    return None


# --- Formatting helpers ------------------------------------------------------------
_CHAIN_LABELS = {
    "solana": "SOL", "ethereum": "ETH", "bsc": "BSC", "base": "BASE",
    "arbitrum": "ARB", "polygon": "POLY", "avalanche": "AVAX", "optimism": "OP",
    "ton": "TON", "sui": "SUI", "tron": "TRX", "aptos": "APT", "robinhood": "RH",
}


def chain_label(chain_id: str) -> str:
    return _CHAIN_LABELS.get(chain_id, (chain_id or "?").upper())


def fmt_num(value) -> str:
    """404K, 57.7K, 4.30K, 1.23M ... (3 significant digits)."""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= div:
            v = value / div
            return f"{v:.0f}{suffix}" if v >= 100 else (f"{v:.1f}{suffix}" if v >= 10 else f"{v:.2f}{suffix}")
    return f"{value:.0f}"


def fmt_usd(value) -> str:
    return f"${fmt_num(value)}" if value else "N/A"


def fmt_age(ts_ms) -> str:
    if not ts_ms:
        return "N/A"
    try:
        secs = max(0, time.time() - (float(ts_ms) / 1000.0))
    except Exception:
        return "N/A"
    d = int(secs // 86400)
    h = int((secs % 86400) // 3600)
    m = int((secs % 3600) // 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m}m" if h else f"{m}m"


def fmt_pct(value) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


# --- Data sources --------------------------------------------------------------------
_http = requests.Session()


def _get_json(url, params=None, timeout=10):
    try:
        r = _http.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"GET failed ({url}): {e}")
        return None


def _f(value):
    try:
        return float(value) if value not in (None, "") else None
    except Exception:
        return None


def fetch_dex_data(ca: str, chain_hint: str = None):
    """Live token data from DexScreener, on ANY chain it supports (sync)."""
    pairs = []
    data = _get_json(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
    if data:
        pairs = data.get("pairs") or []

    # Chart links usually carry a PAIR address, not a token address
    if not pairs and chain_hint:
        data = _get_json(f"https://api.dexscreener.com/latest/dex/pairs/{chain_hint}/{ca}")
        if data:
            pairs = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])

    if not pairs:
        data = _get_json("https://api.dexscreener.com/latest/dex/search", params={"q": ca})
        wanted = ca.lower()
        pairs = [
            p for p in ((data or {}).get("pairs") or [])
            if wanted in (
                ((p.get("baseToken") or {}).get("address") or "").lower(),
                ((p.get("quoteToken") or {}).get("address") or "").lower(),
                (p.get("pairAddress") or "").lower(),
            )
        ]
    if not pairs:
        return None

    wanted = ca.lower()
    by_pair = [p for p in pairs if (p.get("pairAddress") or "").lower() == wanted]
    if by_pair:
        pairs = by_pair
    else:
        as_base = [p for p in pairs if ((p.get("baseToken") or {}).get("address") or "").lower() == wanted]
        if as_base:
            pairs = as_base
    if chain_hint:
        on_chain = [p for p in pairs if (p.get("chainId") or "").lower() == chain_hint]
        if on_chain:
            pairs = on_chain
    pairs = sorted(pairs, key=lambda p: (_f((p.get("liquidity") or {}).get("usd")) or 0), reverse=True)
    best = pairs[0]

    base = best.get("baseToken") or {}
    price = _f(best.get("priceUsd")) or 0
    mc = _f(best.get("marketCap")) or _f(best.get("fdv")) or 0
    if price <= 0 and mc <= 0 and not base.get("symbol"):
        return None

    info = best.get("info") or {}
    links = {}
    for s in info.get("socials") or []:
        url = s.get("url") or ""
        kind = (s.get("type") or "").lower()
        if kind == "telegram" or "t.me/" in url:
            links.setdefault("TG", url)
        elif kind == "twitter" or "x.com/" in url or "twitter.com/" in url:
            links.setdefault("X", url)
    for w in info.get("websites") or []:
        if w.get("url"):
            links.setdefault("WEB", w["url"])
            break

    h24 = (best.get("txns") or {}).get("h24") or {}
    pc = best.get("priceChange") or {}
    chain_id = (best.get("chainId") or "").lower()
    return {
        "chain_id": chain_id,
        "chain": chain_label(chain_id),
        "name": base.get("name") or "",
        "symbol": base.get("symbol") or "",
        "token_addr": base.get("address") or ca,
        "pair_addr": best.get("pairAddress") or "",
        "chart_url": best.get("url") or f"https://dexscreener.com/{chain_id}/{best.get('pairAddress') or ca}",
        "price": price,
        "mcap": mc,
        "dex": best.get("dexId") or "",
        "listed_at": best.get("pairCreatedAt"),
        "volume_h24": _f((best.get("volume") or {}).get("h24")) or 0,
        "liquidity_usd": _f((best.get("liquidity") or {}).get("usd")) or 0,
        "txns_buys": int(h24.get("buys") or 0),
        "txns_sells": int(h24.get("sells") or 0),
        "chg_m5": _f(pc.get("m5")),
        "chg_h1": _f(pc.get("h1")),
        "links": links,
        "logo": info.get("imageUrl") or "",
    }


_GT_FALLBACK_CHAINS = ["ethereum", "bsc", "base", "arbitrum", "polygon", "avalanche", "optimism"]


def fetch_gt_fallback(ca: str, chain_hint: str = None):
    """GeckoTerminal fallback for when DexScreener hasn't indexed the pair yet (common for
    brand-new pairs, or when DexScreener's free API is rate-limiting us). Checked per network
    directly by token address, so it doesn't depend on DexScreener knowing about the token."""
    for chain_id in ([chain_hint] if chain_hint else _GT_FALLBACK_CHAINS):
        net = _GT_NETWORKS.get(chain_id, chain_id)
        d = _gt_get(f"/networks/{net}/tokens/{ca}/pools", params={"page": 1})
        pools = (d or {}).get("data") or []
        if not pools:
            continue
        def liq(p):
            return _f((p.get("attributes") or {}).get("reserve_in_usd")) or 0
        best = max(pools, key=liq)
        a = best.get("attributes") or {}
        price = _f(a.get("base_token_price_usd")) or 0
        mc = _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")) or 0
        if price <= 0 and mc <= 0:
            continue
        name_pair = a.get("name") or ""
        sym = name_pair.split("/")[0].strip() if "/" in name_pair else name_pair
        tx = (a.get("transactions") or {}).get("h24") or {}
        vol = a.get("volume_usd")
        vol_h24 = _f(vol.get("h24")) if isinstance(vol, dict) else _f(vol)
        pc = a.get("price_change_percentage") or {}
        pool_addr = a.get("address") or (best.get("id") or "").split("_")[-1]
        dex_id = ((best.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id", "")
        return {
            "chain_id": chain_id, "chain": chain_label(chain_id),
            "name": sym, "symbol": sym, "token_addr": ca, "pair_addr": pool_addr,
            "chart_url": f"https://www.geckoterminal.com/{net}/pools/{pool_addr}",
            "price": price, "mcap": mc, "dex": dex_id,
            "listed_at": _iso_to_ms(a.get("pool_created_at")) if a.get("pool_created_at") else None,
            "volume_h24": vol_h24 or 0, "liquidity_usd": _f(a.get("reserve_in_usd")) or 0,
            "txns_buys": int(tx.get("buys") or 0), "txns_sells": int(tx.get("sells") or 0),
            "chg_m5": _f(pc.get("m5")), "chg_h1": _f(pc.get("h1")), "links": {},
        }
    return None


_DEAD_ADDRS = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
_BURN_TAGS = ("burn", "dead", "incinerator")
_POOL_TAGS = ("pool", "amm", "raydium", "pump", "meteora", "orca", "liquidity", "vault", "burn", "incinerator", "dead")
_RAYDIUM_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
_NO_AUTH = (None, "", "11111111111111111111111111111111")


def _to_ts(v):
    """Unix seconds from an epoch number/string or an ISO-style date string."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return x / 1000 if x > 1e11 else x
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _summarize_lp(holders):
    """GoPlus lp_holders -> % of LP burned, % locked, and when the lock ends."""
    burned = locked = 0.0
    unlock = None
    for h in holders or []:
        pct = (_f(h.get("percent")) or 0) * 100
        addr = str(h.get("address") or h.get("token_account") or "")
        tag = str(h.get("tag") or "").lower()
        if addr.lower() in _DEAD_ADDRS or addr.startswith("1nc1nerator") or any(b in tag for b in _BURN_TAGS):
            burned += pct
        elif str(h.get("is_locked")) == "1":
            locked += pct
            for ld in h.get("locked_detail") or []:
                ts = _to_ts(ld.get("end_time"))
                if ts and (unlock is None or ts > unlock):
                    unlock = ts
    return {"lp_burned": round(burned, 1), "lp_locked": round(locked, 1), "lp_unlock": unlock}


def _solana_rpc_mint(addr: str):
    """Read the token's mint account straight from a Solana RPC node:
    mint/freeze authority and transfer fee (tax). Never raises."""
    try:
        r = requests.post(SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [addr, {"encoding": "jsonParsed"}]}, timeout=8)
        if r.status_code != 200:
            return {}
        val = ((r.json() or {}).get("result") or {}).get("value") or {}
        data = val.get("data") or {}
        parsed = data.get("parsed") or {}
        if parsed.get("type") != "mint":
            return {}
        info = parsed.get("info") or {}
        revoked = info.get("mintAuthority") is None
        out = {"renounced": revoked, "mint_revoked": revoked,
               "freeze_disabled": info.get("freezeAuthority") is None}
        program = data.get("program") or ""
        fee_bps = None
        for ext in info.get("extensions") or []:
            if ext.get("extension") == "transferFeeConfig":
                fee = (ext.get("state") or {}).get("newerTransferFee") or {}
                fee_bps = _f(fee.get("transferFeeBasisPoints"))
        if fee_bps is not None:
            out["buy_tax"] = out["sell_tax"] = round(fee_bps / 100, 2)
        elif program in ("spl-token", "spl-token-2022"):
            out["buy_tax"] = out["sell_tax"] = 0  # no transfer-fee extension = no token tax
        return out
    except Exception:
        return {}


def _rugcheck_full(addr: str):
    """RugCheck: holders, top-holder concentration, dev holding, LP lock, rating."""
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report", timeout=7)
        if r.status_code != 200:
            return {}
        d = r.json() or {}
        out = {}
        if isinstance(d.get("totalHolders"), int):
            out["holders"] = d["totalHolders"]
        if "mintAuthority" in d:
            rev = d.get("mintAuthority") in _NO_AUTH
            out["renounced"] = out["mint_revoked"] = rev
        if "freezeAuthority" in d:
            out["freeze_disabled"] = d.get("freezeAuthority") in _NO_AUTH

        # Tax: Token-2022 transfer fee, or 0 for a classic SPL token
        tfee = d.get("transferFee")
        if isinstance(tfee, dict) and _f(tfee.get("pct")) is not None:
            out["buy_tax"] = out["sell_tax"] = round(_f(tfee.get("pct")), 2)
        elif d.get("tokenProgram") == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
            out["buy_tax"] = out["sell_tax"] = 0

        markets = d.get("markets") or []
        pools = {_RAYDIUM_AUTHORITY}
        for m in markets:
            for k in ("pubkey", "liquidityA", "liquidityB"):
                if m.get(k):
                    pools.add(m[k])
        everyone, top = [], []
        for h in d.get("topHolders") or []:
            p = _f(h.get("pct"))
            if p is None:
                continue
            everyone.append((h.get("owner") or h.get("address"), p))
            if h.get("address") in pools or h.get("owner") in pools:
                continue
            top.append(p)
        if top:
            out["t5"] = round(sum(top[:5]), 2)
            out["t10"] = round(sum(top[:10]), 2)
        creator = d.get("creator")
        if creator and everyone:
            hit = next((p for o, p in everyone if o == creator), None)
            if hit is not None:
                out["dev"] = round(hit, 2)
            else:
                out["dev_text"] = f"<{min(p for _, p in everyone):.1f}%"

        best, best_liq = None, -1
        for m in markets:
            lp = m.get("lp") or {}
            pct = _f(lp.get("lpLockedPct"))
            if pct is None:
                continue
            liq = (_f(lp.get("quoteUSD")) or 0) + (_f(lp.get("baseUSD")) or 0)
            if liq > best_liq:
                best, best_liq = pct, liq
        if best is not None:
            out["rc_lp_locked"] = best

        if d.get("risks") is not None:
            levels = {str(x.get("level", "")).lower() for x in d.get("risks") or []}
            if d.get("rugged") is True or "danger" in levels or "error" in levels:
                rating = "Danger 🔴"
            elif "warn" in levels:
                rating = "Warning 🟡"
            else:
                rating = "Good 🟢"
            out["audit"] = f"RugCheck · {rating}"
        return out
    except Exception:
        return {}


def _goplus_solana(addr: str):
    """GoPlus Solana: mint/freeze flags, transfer fee, honeypot-style flags, LP lock."""
    try:
        r = requests.get("https://api.gopluslabs.io/api/v1/solana/token_security",
                         params={"contract_addresses": addr}, timeout=7)
        if r.status_code != 200:
            return {}
        res = (r.json() or {}).get("result") or {}
        d = res.get(addr) or (next(iter(res.values()), {}) if res else {})
        if not d:
            return {}

        def status(key):
            v = d.get(key)
            return str(v.get("status")) if isinstance(v, dict) and v.get("status") is not None else None

        out = {}
        ms, fs = status("mintable"), status("freezable")
        if ms is not None:
            out["mint_revoked"] = out["renounced"] = (ms != "1")
        if fs is not None:
            out["freeze_disabled"] = (fs != "1")
        out["sol_bad_flags"] = (
            str(d.get("non_transferable")) == "1" or bool(d.get("transfer_hook"))
            or status("balance_mutable_authority") == "1" or status("closable") == "1")
        if "transfer_fee" in d:
            tf = d.get("transfer_fee")
            bps = _f(((tf or {}).get("current_fee_rate") or {}).get("fee_rate")) if isinstance(tf, dict) else None
            out["buy_tax"] = out["sell_tax"] = round((bps or 0) / 100, 2)

        top = []
        for h in d.get("holders") or []:
            tag = str(h.get("tag") or "").lower()
            if any(k in tag for k in _POOL_TAGS):
                continue
            p = _f(h.get("percent"))
            if p is not None:
                top.append(p * 100)
        if top:
            out["t5"] = round(sum(top[:5]), 2)
            out["t10"] = round(sum(top[:10]), 2)

        dexes = [x for x in (d.get("dex") or []) if isinstance(x, dict) and x.get("lp_holders")]
        if dexes:
            best = max(dexes, key=lambda x: _f(x.get("tvl")) or 0)
            out.update(_summarize_lp(best.get("lp_holders")))
        return out
    except Exception:
        return {}


def fetch_solana_security(addr: str):
    """Solana security bundle. The three sources run in parallel; where they
    overlap the on-chain RPC read wins, then RugCheck, then GoPlus."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_gp = ex.submit(_goplus_solana, addr)
        f_rc = ex.submit(_rugcheck_full, addr)
        f_rpc = ex.submit(_solana_rpc_mint, addr)
        gp, rc, rpc = f_gp.result(), f_rc.result(), f_rpc.result()
    out = {}
    for src in (gp, rc, rpc):
        out.update({k: v for k, v in src.items() if v is not None})

    # Solana has no classic honeypot; the closest equivalent is the freeze switch
    # (plus transfer hooks / balance-editing authorities on Token-2022).
    if out.get("freeze_disabled") is not None:
        out["honeypot"] = "pass" if (out["freeze_disabled"] and not gp.get("sol_bad_flags")) else "risk"
    # LP: prefer GoPlus's breakdown, fall back to RugCheck's locked %
    if out.get("lp_burned") is None and out.get("rc_lp_locked") is not None:
        out["lp_burned"], out["lp_locked"], out["lp_unlock"] = 0, out["rc_lp_locked"], None
    return out


# DexScreener chainId -> GeckoTerminal network id
_GT_NETWORKS = {
    "solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base",
    "arbitrum": "arbitrum", "polygon": "polygon_pos", "avalanche": "avax",
    "optimism": "optimism", "ton": "ton", "sui": "sui-network", "tron": "tron",
    "linea": "linea", "fantom": "ftm", "cronos": "cro", "zksync": "zksync",
    "scroll": "scroll", "blast": "blast", "mantle": "mantle", "pulsechain": "pulsechain",
    "sonic": "sonic", "aptos": "aptos", "hyperevm": "hyperevm", "berachain": "berachain",
    "abstract": "abstract", "unichain": "unichain", "ronin": "ronin",
}
_gt_calls: list = []  # timestamps, to stay under GeckoTerminal's free rate limit


def _gt_get(path: str, params=None):
    limit = 400 if COINGECKO_PRO_API_KEY else 25  # free plan: ~30 calls/min
    now = time.time()
    _gt_calls[:] = [t for t in _gt_calls if now - t < 60]
    if len(_gt_calls) >= limit:
        return None
    _gt_calls.append(now)
    if COINGECKO_PRO_API_KEY:
        url = f"https://pro-api.coingecko.com/api/v3/onchain{path}"
        headers = {"accept": "application/json", "x-cg-pro-api-key": COINGECKO_PRO_API_KEY}
    else:
        url = f"https://api.geckoterminal.com/api/v2{path}"
        headers = {"accept": "application/json;version=20230302"}
    try:
        r = _http.get(url, params=params, headers=headers, timeout=8)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug(f"GT get failed ({path}): {e}")
        return None


def fetch_gt_extras(chain_id: str, token: str, pair: str):
    """GeckoTerminal fills the gaps DexScreener leaves: holders, unique traders,
    all-time-high price, pair age, short-term price change, socials. Never raises."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    net = _GT_NETWORKS.get(chain_id, chain_id)

    def job_pool():
        d = _gt_get(f"/networks/{net}/pools/{pair}")
        a = ((d or {}).get("data") or {}).get("attributes") or {}
        r = {}
        tx = (a.get("transactions") or {}).get("h24") or {}
        b, s_ = tx.get("buyers"), tx.get("sellers")
        if isinstance(b, int) and isinstance(s_, int):
            r["traders"] = b + s_
        pc = a.get("price_change_percentage") or {}
        r["chg_m5"], r["chg_h1"] = _f(pc.get("m5")), _f(pc.get("h1"))
        r["listed_iso"] = a.get("pool_created_at")
        r["gt_mcap"] = _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd"))
        r["gt_price"] = _f(a.get("base_token_price_usd"))
        return r

    def job_ohlcv():
        o = _gt_get(f"/networks/{net}/pools/{pair}/ohlcv/day",
                    params={"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"})
        candles = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        highs = [_f(c[2]) for c in candles if isinstance(c, list) and len(c) >= 3 and _f(c[2])]
        return {"ath_price": max(highs)} if highs else {}

    def job_trades():
        """Most recent BUY on the pool (newest trade first)."""
        d = _gt_get(f"/networks/{net}/pools/{pair}/trades")
        for tr in (d or {}).get("data") or []:
            a = tr.get("attributes") or {}
            if str(a.get("kind")).lower() == "buy":
                return {"last_buy_usd": _f(a.get("volume_in_usd")),
                        "last_buy_ts": _to_ts(a.get("block_timestamp"))}
        return {}

    def job_info():
        info = _gt_get(f"/networks/{net}/tokens/{token}/info")
        ia = ((info or {}).get("data") or {}).get("attributes") or {}
        r = {}
        h = (ia.get("holders") or {}).get("count")
        if h not in (None, ""):
            try:
                r["holders"] = int(h)
            except Exception:
                pass
        r["websites"] = ia.get("websites") or []
        r["twitter"] = ia.get("twitter_handle") or ""
        r["telegram"] = ia.get("telegram_handle") or ""
        return r

    # All GeckoTerminal calls run at the same time (was one after another) = much faster
    jobs = [job_info] + ([job_pool, job_ohlcv] if pair else [])
    if pair and chain_id != "robinhood":
        jobs.append(job_trades)
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fut in [ex.submit(j) for j in jobs]:
            try:
                out.update(fut.result() or {})
            except Exception as e:
                logger.debug(f"GT extras job failed: {e}")
    return out


def fetch_blockscout_holders(token: str, chain_id: str):
    """Fast path for Robinhood chain: only the holder count (1 request). Never raises."""
    base = _BLOCKSCOUT_APIS.get(chain_id)
    if not base:
        return {}
    try:
        info = requests.get(f"{base}/api/v2/tokens/{token}", timeout=5).json() or {}
        hc = info.get("holders_count")
        return {"holders": int(hc)} if str(hc or "").isdigit() else {}
    except Exception:
        return {}


_HONEYPOT_CHAINS = {"ethereum": 1, "bsc": 56, "base": 8453}

# Chain-id -> numeric EVM chain id, used to build Mobula's "evm:<id>" blockchain param
_EVM_NUMERIC_CHAIN = {
    "ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161, "polygon": 137,
    "avalanche": 43114, "optimism": 10, "linea": 59144, "fantom": 250, "cronos": 25,
    "zksync": 324, "scroll": 534352, "blast": 81457, "mantle": 5000, "pulsechain": 369,
    "sonic": 146, "robinhood": 4663,
}


def fetch_mobula_snipers(token: str, chain_id: str):
    """Snipers % and Bundled % of supply, via Mobula's free-tier token/details endpoint
    (needs a free MOBULA_API_KEY from admin.mobula.io). Never raises."""
    if not MOBULA_API_KEY:
        return {}
    bc = "solana" if chain_id == "solana" else (
        f"evm:{_EVM_NUMERIC_CHAIN[chain_id]}" if chain_id in _EVM_NUMERIC_CHAIN else None)
    if not bc:
        return {}
    try:
        r = requests.get(
            "https://api.mobula.io/api/2/token/details",
            params={"blockchain": bc, "address": token},
            headers={"Authorization": MOBULA_API_KEY}, timeout=8)
        if r.status_code != 200:
            return {}
        d = (r.json() or {}).get("data") or {}
        out = {}
        sp, bp = _f(d.get("snipersHoldingsPercentage")), _f(d.get("bundlersHoldingsPercentage"))
        if sp is not None:
            out["snipers"] = f"{sp:.1f}%"
        if bp is not None:
            out["bundled"] = f"{bp:.1f}%"
        return out
    except Exception as e:
        logger.debug(f"Mobula snipers/bundled fetch failed ({chain_id}): {e}")
        return {}


# Chains GoPlus/honeypot.is don't cover yet, but that run a Blockscout explorer with an
# open API - used as a fallback for holders / dev% / T5 / T10 (owner/mint/freeze/tax/LP
# still need a security scanner that Blockscout itself doesn't provide).
_BLOCKSCOUT_APIS = {"robinhood": "https://robinhoodchain.blockscout.com"}


def fetch_blockscout_security(token: str, chain_id: str):
    """Fallback for chains not in GoPlus: holders, dev%, T5/T10 straight from a
    Blockscout explorer's public API. Never raises."""
    base = _BLOCKSCOUT_APIS.get(chain_id)
    if not base:
        return {}
    out = {}
    try:
        info = requests.get(f"{base}/api/v2/tokens/{token}", timeout=8).json() or {}
        decimals = int(info.get("decimals") or 18)
        total_supply = int(info.get("total_supply") or 0)
        hc = info.get("holders_count")
        if str(hc or "").isdigit():
            out["holders"] = int(hc)
        if total_supply > 0:
            hd = requests.get(f"{base}/api/v2/tokens/{token}/holders",
                              params={"items_count": 10}, timeout=8).json() or {}
            shares = []
            for it in hd.get("items") or []:
                addr = ((it.get("address") or {}).get("hash") or "").lower()
                val = int(it.get("value") or 0)
                if addr in _DEAD_ADDRS or not addr:
                    continue
                shares.append((addr, val / total_supply * 100))
            if shares:
                out["t5"] = round(sum(p for _, p in shares[:5]), 2)
                out["t10"] = round(sum(p for _, p in shares[:10]), 2)
            try:
                c = requests.get(f"{base}/api/v2/addresses/{token}", timeout=8).json() or {}
                creator = (c.get("creator_address_hash") or "").lower()
                if creator:
                    dev_pct = next((p for a, p in shares if a == creator), 0.0)
                    out["dev"] = round(dev_pct, 2)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Blockscout security fetch failed ({chain_id}): {e}")
        return {}
    return out


def fetch_honeypot_is(addr: str, chain_id: str):
    """EVM fallback for buy/sell tax + honeypot check (honeypot.is simulates a real buy/sell)."""
    cid = _HONEYPOT_CHAINS.get(chain_id)
    if not cid:
        return {}
    try:
        r = requests.get("https://api.honeypot.is/v2/IsHoneypot",
                         params={"address": addr, "chainID": cid}, timeout=8)
        if r.status_code != 200:
            return {}
        d = r.json() or {}
        sim = d.get("simulationResult") or {}
        out = {}
        b, s_ = _f(sim.get("buyTax")), _f(sim.get("sellTax"))
        if b is not None:
            out["buy_tax"] = round(b, 1)
        if s_ is not None:
            out["sell_tax"] = round(s_, 1)
        hp = (d.get("honeypotResult") or {}).get("isHoneypot")
        if hp is not None:
            out["honeypot"] = "fail" if hp else "pass"
        return out
    except Exception:
        return {}


# DexScreener chainId -> GoPlus chain id
_GOPLUS_CHAINS = {
    "ethereum": "1", "bsc": "56", "base": "8453", "arbitrum": "42161",
    "polygon": "137", "avalanche": "43114", "optimism": "10", "linea": "59144",
    "fantom": "250", "cronos": "25", "zksync": "324", "scroll": "534352",
    "blast": "81457", "mantle": "5000", "pulsechain": "369", "sonic": "146",
    "tron": "tron",
}


def fetch_goplus_security(addr: str, chain_id: str):
    """EVM/Tron: tax, owner, honeypot, mint, holders, dev %, LP lock via GoPlus. Never raises."""
    gp = _GOPLUS_CHAINS.get(chain_id)
    if not gp:
        return {}
    try:
        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{gp}",
            params={"contract_addresses": addr}, timeout=7)
        if r.status_code != 200:
            return {}
        res = (r.json() or {}).get("result") or {}
        d = res.get(addr.lower()) or res.get(addr) or {}
        if not d:
            return {}

        def flag(k):
            return str(d.get(k)) == "1"

        out = {}
        for key in ("buy_tax", "sell_tax"):
            v = _f(d.get(key))
            if v is not None:
                out[key] = round(v * 100, 1)
        if "owner_address" in d:
            out["renounced"] = (d.get("owner_address") or "").lower() in _DEAD_ADDRS | {""}
        if str(d.get("holder_count") or "").isdigit():
            out["holders"] = int(d["holder_count"])
        if "is_mintable" in d:
            out["mint_revoked"] = not flag("is_mintable")
        if "transfer_pausable" in d or "is_blacklisted" in d:
            out["freeze_disabled"] = not (flag("transfer_pausable") or flag("is_blacklisted"))
        if "is_honeypot" in d:
            out["honeypot"] = "fail" if (flag("is_honeypot") or flag("cannot_sell_all")) else "pass"

        risk_keys = ("hidden_owner", "can_take_back_ownership", "owner_change_balance",
                     "selfdestruct", "transfer_pausable", "is_blacklisted", "is_mintable")
        if any(k in d for k in risk_keys + ("is_honeypot",)):
            if flag("is_honeypot") or flag("cannot_sell_all"):
                rating = "Danger 🔴"
            elif any(flag(k) for k in risk_keys) or str(d.get("is_open_source")) == "0":
                rating = "Warning 🟡"
            else:
                rating = "Good 🟢"
            out["audit"] = f"GoPlus · {rating}"

        top = []
        for h in d.get("holders") or []:
            a = str(h.get("address") or "").lower()
            if str(h.get("is_contract")) == "1" or a in _DEAD_ADDRS:
                continue
            p = _f(h.get("percent"))
            if p is not None:
                top.append(p * 100)
        if top:
            out["t5"] = round(sum(top[:5]), 2)
            out["t10"] = round(sum(top[:10]), 2)
        cp = _f(d.get("creator_percent"))
        if cp is not None:
            out["dev"] = round(cp * 100, 2)
        if d.get("lp_holders"):
            out.update(_summarize_lp(d["lp_holders"]))
        return out
    except Exception:
        return {}


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
}


def em(key: str, fallback: str, premium: bool = True) -> str:
    eid = EMOJI_IDS.get(key)
    if premium and eid:
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


def fmt_lp(t: dict) -> str:
    burned, locked, unlock = t.get("lp_burned"), t.get("lp_locked"), t.get("lp_unlock")
    if burned is None and locked is None:
        return "N/A"
    burned, locked = burned or 0, locked or 0
    if burned >= 90:
        return "Burned 🔥"
    total = burned + locked
    if total < 5:
        return "Not locked ⚠️"
    text = f"{total:.0f}% " + ("burned 🔥" if burned > locked else "locked 🔒")
    if unlock:
        secs = unlock - time.time()
        if secs <= 0:
            text += " · unlocked ⚠️"
        else:
            months = secs / (30.44 * 86400)
            if months >= 1:
                n = round(months)
                text += f" · {n} month{'s' if n != 1 else ''}"
            else:
                text += f" · {max(1, round(secs / 86400))} days"
    return text


def fmt_share(t: dict, key: str) -> str:
    v = t.get(key)
    return "N/A" if v is None else (f"{v:.1f}%" if v else "0%")


_DEX_NAMES = {
    "pancakeswap": "PancakeSwap", "uniswap": "Uniswap", "raydium": "Raydium",
    "pumpswap": "PumpSwap", "pumpfun": "Pump.fun", "meteora": "Meteora", "orca": "Orca",
    "aerodrome": "Aerodrome", "sushiswap": "SushiSwap", "quickswap": "QuickSwap",
    "traderjoe": "Trader Joe", "camelot": "Camelot", "fourmeme": "Four.meme",
    "moonshot": "Moonshot", "bonk": "Bonk", "dexscreener": "DexScreener",
}


def pretty_dex(dex: str) -> str:
    if not dex:
        return "N/A"
    return _DEX_NAMES.get(dex.lower(), dex.replace("-", " ").replace("_", " ").title())


_UNI_CHAINS = {"ethereum": "ethereum", "base": "base", "arbitrum": "arbitrum", "polygon": "polygon",
               "optimism": "optimism", "avalanche": "avalanche", "bsc": "bnb"}
_CAKE_CHAINS = {"bsc": "bsc", "ethereum": "eth", "base": "base", "arbitrum": "arb"}


def dex_url(t: dict):
    """Link to the trading platform named in the header (not the chart). None = show plain text."""
    dex = (t.get("dex") or "").lower().replace("-", "").replace("_", "")
    ca, chain = t.get("token_addr") or "", t.get("chain_id") or ""
    if not dex or not ca:
        return None
    if dex in ("pumpfun", "pumpswap"):
        return f"https://pump.fun/coin/{ca}"
    if dex.startswith("pancakeswap") and chain in _CAKE_CHAINS:
        return f"https://pancakeswap.finance/swap?chain={_CAKE_CHAINS[chain]}&outputCurrency={ca}"
    if dex.startswith("uniswap") and chain in _UNI_CHAINS:
        return f"https://app.uniswap.org/explore/tokens/{_UNI_CHAINS[chain]}/{ca}"
    if dex.startswith("raydium"):
        return f"https://raydium.io/swap/?inputMint=sol&outputMint={ca}"
    if dex.startswith("orca"):
        return f"https://www.orca.so/?tokenIn=So11111111111111111111111111111111111111112&tokenOut={ca}"
    if dex.startswith("meteora"):
        return "https://app.meteora.ag"
    if dex.startswith("aerodrome"):
        return f"https://aerodrome.finance/swap?to={ca}"
    if dex.startswith("sushiswap"):
        return "https://www.sushi.com/swap"
    if dex.startswith("quickswap"):
        return "https://quickswap.exchange"
    if dex.startswith("traderjoe"):
        return "https://traderjoexyz.com"
    if dex.startswith("camelot"):
        return "https://app.camelot.exchange"
    if dex == "fourmeme":
        return f"https://four.meme/token/{ca}"
    if dex == "moonshot":
        return "https://moonshot.com"
    return None


def format_token_snapshot(t: dict, premium: bool = True) -> str:
    r1, r2, r3, r4, r5 = (em(k, "🔹", premium) for k in ("r1", "r2", "r3", "r4", "r5"))
    LH = em("lp_hold", "🔹", premium)
    A = em("arrow", "➡️", premium)
    BUY, SELL = em("buy", "🟢", premium), em("sell", "🔴", premium)
    YES, NO = em("yes", "✅", premium), em("no", "❌", premium)

    chart = html.escape(t.get("chart_url") or "https://dexscreener.com", quote=True)
    name = html.escape(t.get("name") or "Unknown Token")
    sym = html.escape((t.get("symbol") or "").upper())
    head = " ".join(x for x in ((f"${sym}" if sym else ""), name, f"({html.escape(t.get('chain') or '?')})") if x)

    price = t.get("price") or 0
    price_s = f"${price:.10f}".rstrip("0").rstrip(".") if price else "N/A"
    dex_name = html.escape(pretty_dex(t.get("dex")))
    _durl = dex_url(t)
    # Link goes to the platform named here (Pump.fun, PancakeSwap...), not to the chart
    dex_s = f'<a href="{html.escape(_durl, quote=True)}">{dex_name}</a>' if _durl else dex_name

    holders = t.get("holders")
    holders_s = f"{fmt_num(holders)} (Holders)" if isinstance(holders, int) else "N/A"
    if t.get("buy_tax") is None and t.get("sell_tax") is None:
        tax_s = "N/A"
    else:
        tax_s = f"{t.get('buy_tax') or 0}% {BUY} &amp; {t.get('sell_tax') or 0}% {SELL}"
    renounced = t.get("renounced")
    owner_s = (f"Renounced {YES}" if renounced is True
               else "Not renounced ⚠️" if renounced is False else "N/A")
    hp = {"pass": f"Pass {YES}", "fail": f"Fail {NO}", "risk": "Risk ⚠️"}.get(t.get("honeypot"), "N/A")
    mint = t.get("mint_revoked")
    mint_s = "N/A" if mint is None else (f"Revoked {YES}" if mint else "Active ⚠️")
    frz = t.get("freeze_disabled")
    frz_s = "N/A" if frz is None else (f"Disabled {YES}" if frz else "Enabled ⚠️")
    dev_s = html.escape(t.get("dev_text") or fmt_share(t, "dev"))  # dev_text can be "<0.5%" - must be escaped
    peak = t.get("peak")

    lb_usd, lb_ts = t.get("last_buy_usd"), t.get("last_buy_ts")
    if lb_usd:
        last_buy_s = f"{fmt_usd(lb_usd)} bought"
        if lb_ts:
            last_buy_s += f" ({fmt_age(lb_ts * 1000)} ago)"
    else:
        last_buy_s = "N/A"

    links = t.get("links") or {}
    parts = [f'<a href="{html.escape(links[k], quote=True)}">{k}</a>'
             for k in ("TG", "WEB", "X")
             if links.get(k, "").startswith(("http://", "https://"))]
    parts.append(f'<a href="{chart}">DEX</a>')
    social_s = " | ".join(parts)

    # Robinhood chain: short template (no security/holder-analysis blocks)
    if t.get("chain_id") == "robinhood":
        return (
            f'{em("title", "🔮", premium)} <b><a href="{chart}">{head}</a></b>\n'
            f'       <b>{dex_s} — {fmt_age(t.get("listed_at"))}</b>\n\n'
            f'{em("snapshot", "🐳", premium)} <b>TOKEN SNAPSHOT</b>\n\n'
            f'{r1} MCAP   {A} {fmt_usd(t.get("mcap"))} (Current MC)\n'
            f'{r2} PEAK    {A} {fmt_usd(peak) if peak else "N/A"} (ATH)\n'
            f'{r2} Price     {A} {price_s}\n'
            f'{r3} Owner   {A} {owner_s}\n\n'
            f'{em("market", "⚡️", premium)} <b>MARKET STATS</b>\n\n'
            f'{r1} Txns (24h)  {A} {fmt_num(t.get("txns_buys", 0))}{BUY} |  {fmt_num(t.get("txns_sells", 0))}{SELL}\n'
            f'{LH} Volume       {A} {fmt_usd(t.get("volume_h24"))} (24h)\n'
            f'{r3} Liquidity      {A} {fmt_usd(t.get("liquidity_usd"))}\n'
            f'{LH} Traders       {A} {holders_s}\n\n'
            f'{em("social", "🛡️", premium)} <b>SOCIAL MEDIA:</b>\n\n'
            f'{social_s}\n\n'
            f'<blockquote><code>{html.escape(t.get("token_addr") or "")}</code></blockquote>'
        )

    return (
        f'{em("title", "🔮", premium)} <b><a href="{chart}">{head}</a></b>\n'
        f'       <b>{dex_s} — {fmt_age(t.get("listed_at"))}</b>\n\n'
        f'{em("snapshot", "🐳", premium)} <b>TOKEN SNAPSHOT</b>\n\n'
        f'{r1} MCAP   {A} {fmt_usd(t.get("mcap"))} (Current MC)\n'
        f'{r2} PEAK    {A} {fmt_usd(peak) if peak else "N/A"} (ATH)\n'
        f'{r2} TAX      {A} {tax_s}\n'
        f'{r2} Price     {A} {price_s}\n'
        f'{r3} Owner   {A} {owner_s}\n\n'
        f'{em("security", "👤", premium)} <b>Security &amp; Activity</b>\n\n'
        f'{LH} LP Locked:  {fmt_lp(t)}\n'
        f'{r5} AUDIT:        {html.escape(t.get("audit") or "N/A")}\n'
        f'{r5} Honeypot:   {hp}\n'
        f'{r5} MINT:         {mint_s}\n'
        f'{r4} Freeze:        {frz_s}\n\n'
        f'{em("market", "⚡️", premium)} <b>MARKET STATS</b>\n\n'
        f'{r1} Txns (24h)  {A} {fmt_num(t.get("txns_buys", 0))}{BUY} |  {fmt_num(t.get("txns_sells", 0))}{SELL}\n'
        f'{r2} Volume       {A} {fmt_usd(t.get("volume_h24"))} (24h)\n'
        f'{r3} Liquidity      {A} {fmt_usd(t.get("liquidity_usd"))}\n\n'
        f'{em("holders", "🏆", premium)} <b>HOLDER ANALYSIS</b>\n\n'
        f'{LH} Traders   {A} {holders_s}\n'
        f'{r5} Last Buy {A} {last_buy_s}\n'
        f'{r5} DEV        {A} {dev_s}\n'
        f'{r5} T05        {A} {fmt_share(t, "t5")}\n'
        f'{r4} T10        {A} {fmt_share(t, "t10")}\n\n'
        f'{em("social", "🛡️", premium)} <b>SOCIAL MEDIA:</b>\n\n'
        f'{social_s}\n\n'
        f'<blockquote><code>{html.escape(t.get("token_addr") or "")}</code></blockquote>'
    )


def _iso_to_ms(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


HOLDERS_FILE = DATA_DIR / "holders.json"


def new_holders_24h(t: dict):
    """New holders in 24h. Token younger than 24h -> every holder is new. Otherwise the
    bot compares with the holder count it saw on earlier scans of the same token."""
    h = t.get("holders")
    if not isinstance(h, int):
        return None
    now = time.time()
    la = t.get("listed_at")
    try:
        if la and now - float(la) / 1000.0 < 86400:
            return f"+{h:,}"
    except Exception:
        pass
    key = f"{t.get('chain_id')}:{(t.get('token_addr') or '').lower()}"
    data = load_json(HOLDERS_FILE, {})
    hist = [x for x in data.get(key, []) if now - x[0] <= 90000]
    res = None
    old = [x for x in hist if now - x[0] >= 3600]
    if old:
        base = min(old, key=lambda x: x[0])
        span = int((now - base[0]) // 3600)
        res = f"{h - base[1]:+,}" + ("" if span >= 23 else f" ({span}h)")
    if not hist or now - hist[-1][0] >= 600:
        hist.append([now, h])
    data[key] = hist
    if len(data) > 3000:
        for k in sorted(data, key=lambda k: data[k][-1][0] if data[k] else 0)[:500]:
            data.pop(k, None)
    save_json(HOLDERS_FILE, data)
    return res


_snap_cache: dict = {}
SNAP_TTL = 45  # seconds - repeated pastes of the same token reuse the result


async def build_token_data(addr: str, chain_hint: str = None):
    key = (addr.lower(), chain_hint)
    hit = _snap_cache.get(key)
    if hit and time.time() - hit[0] < SNAP_TTL:
        return hit[1]

    t = await asyncio.to_thread(fetch_dex_data, addr, chain_hint)
    if not t:
        t = await asyncio.to_thread(fetch_gt_fallback, addr, chain_hint)
    if not t:
        return None
    token = t["token_addr"]
    if t["chain_id"] == "solana":
        sec_job = asyncio.to_thread(fetch_solana_security, token)
    elif t["chain_id"] in _BLOCKSCOUT_APIS:  # Robinhood chain: short template, only holders needed
        sec_job = asyncio.to_thread(fetch_blockscout_holders, token, t["chain_id"])
    else:
        sec_job = asyncio.to_thread(fetch_goplus_security, token, t["chain_id"])
    gt_job = asyncio.to_thread(fetch_gt_extras, t["chain_id"], token, t.get("pair_addr"))
    sec, gt = await asyncio.gather(sec_job, gt_job, return_exceptions=True)
    sec = sec if isinstance(sec, dict) else {}
    gt = gt if isinstance(gt, dict) else {}

    if (t["chain_id"] in _HONEYPOT_CHAINS and (sec.get("buy_tax") is None or sec.get("sell_tax") is None)):
        hp_is = await asyncio.to_thread(fetch_honeypot_is, token, t["chain_id"])
        for k, v in hp_is.items():
            if sec.get(k) is None:
                sec[k] = v
    t.update(sec)
    t["last_buy_usd"] = gt.get("last_buy_usd")
    t["last_buy_ts"] = gt.get("last_buy_ts")
    # Fill anything still missing from GeckoTerminal
    if t.get("holders") is None and gt.get("holders") is not None:
        t["holders"] = gt["holders"]
    t["traders"] = gt.get("traders")
    for k in ("chg_m5", "chg_h1"):
        if t.get(k) is None:
            t[k] = gt.get(k)
    if not t.get("listed_at") and gt.get("listed_iso"):
        t["listed_at"] = _iso_to_ms(gt["listed_iso"])
    if not t.get("mcap") and gt.get("gt_mcap"):
        t["mcap"] = gt["gt_mcap"]
    if not t.get("price") and gt.get("gt_price"):
        t["price"] = gt["gt_price"]

    links = t.setdefault("links", {})
    if "WEB" not in links:
        for w in gt.get("websites") or []:
            if str(w).startswith("http"):
                links["WEB"] = w
                break
    if "X" not in links and gt.get("twitter"):
        links["X"] = f"https://x.com/{gt['twitter'].lstrip('@')}"
    if "TG" not in links and gt.get("telegram"):
        links["TG"] = f"https://t.me/{gt['telegram'].lstrip('@')}"

    # PEAK = all-time-high market cap = ATH price x current supply.
    # No candle history yet (brand-new pair, or GeckoTerminal hasn't indexed it) -> ATH
    # can't be less than the current MC, so show the current MC instead of N/A.
    if gt.get("ath_price") and t.get("price") and t.get("mcap"):
        t["peak"] = max(t["mcap"], gt["ath_price"] * (t["mcap"] / t["price"]))
    elif t.get("mcap"):
        t["peak"] = t["mcap"]

    if len(_snap_cache) > 500:
        _snap_cache.clear()
    _snap_cache[key] = (time.time(), t)
    return t


_refresh_map: dict = {}    # short id -> (address, chain) for very long addresses
_refresh_last: dict = {}   # (chat_id, message_id) -> last refresh time
REFRESH_COOLDOWN = 8       # seconds


def maestro_url(t: dict) -> str:
    """Trade button: Maestro opens on THIS token, with your referral attached."""
    ca = t.get("token_addr") or ""
    if MAESTRO_TOKEN_LINK and ca and "::" not in ca and len(ca) <= 64:
        return MAESTRO_TOKEN_LINK.replace("{ca}", ca)
    return MAESTRO_LINK


def refresh_callback_data(t: dict) -> str:
    addr = t.get("token_addr") or ""
    data = f"rf:{t.get('chain_id') or ''}:{addr}"
    if len(data.encode()) > 64:
        key = hashlib.sha1(addr.encode()).hexdigest()[:16]
        _refresh_map[key] = (addr, t.get("chain_id"))
        data = f"rf:#{key}"
    return data


def audit_url(t: dict) -> str:
    """Serialized Audit link for THIS token (chain + address filled in)."""
    ca = t.get("token_addr") or ""
    chain = _AUDIT_CHAINS.get(t.get("chain_id") or "")
    if not chain or not ca:
        return "https://www.serializedaudit.io"
    return AUDIT_LINK_TEMPLATE.replace("{chain}", chain).replace("{ca}", quote(ca, safe=""))


def token_buttons(t: dict) -> InlineKeyboardMarkup:
    """Row 1: Refresh | Trade | Dev. Row 2: full-width audit button for this token."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Refresh", callback_data=refresh_callback_data(t)),
            InlineKeyboardButton("Trade", url=maestro_url(t)),
            InlineKeyboardButton("Dev", url=DEV_LINK),
        ],
        [InlineKeyboardButton(AUDIT_BUTTON_TEXT, url=audit_url(t))],
    ])


async def handle_ca_lookup(msg, addr: str, chain_hint: str = None, quiet_if_missing: bool = False):
    """quiet_if_missing=True (used in groups): say nothing if the token isn't found."""
    status = None
    if not quiet_if_missing:
        status = await msg.reply_text("🔮 Scanning token…")
    try:
        t = await build_token_data(addr, chain_hint)
    except Exception as e:
        logger.warning(f"CA lookup failed for {addr[:12]}...: {e}")
        t = None
    if status:
        try:
            await status.delete()
        except Exception:
            pass
    if not t:
        if not quiet_if_missing:
            await msg.reply_text("⚠️ Couldn't find data for this token. Check the address and try again.")
        return
    markup = token_buttons(t)
    if msg.from_user:  # remember this "call" so /pnl can show the X later
        asyncio.create_task(asyncio.to_thread(record_call, msg.chat_id, msg.from_user, t))
    try:
        await msg.reply_text(format_token_snapshot(t, premium=True), parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=markup)
    except Exception as e:
        logger.warning(f"premium-emoji send failed ({type(e).__name__}: {e}); retrying with plain emojis")
        try:
            await msg.reply_text(format_token_snapshot(t, premium=False), parse_mode="HTML",
                                 disable_web_page_preview=True, reply_markup=markup)
        except Exception as e2:
            logger.warning(f"snapshot send failed for {addr[:12]}...: {e2}")
            if not quiet_if_missing:
                await msg.reply_text("⚠️ Couldn't display this token right now. Try again in a moment.")


async def cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔄 Refresh: re-scan the token and update the message in place."""
    q = update.callback_query
    body = (q.data or "")[3:]
    if body.startswith("#"):
        addr, hint = _refresh_map.get(body[1:], (None, None))
    else:
        hint, _, addr = body.partition(":")
    if not addr or not q.message:
        await q.answer("This scan has expired - paste the address again.", show_alert=True)
        return

    key = (q.message.chat_id, q.message.message_id)
    now = time.time()
    if now - _refresh_last.get(key, 0) < REFRESH_COOLDOWN:
        await q.answer("Just updated - try again in a few seconds.")
        return
    if len(_refresh_last) > 2000:
        _refresh_last.clear()
    _refresh_last[key] = now
    await q.answer("Refreshing…")

    for k in [k for k in _snap_cache if k[0] == addr.lower()]:
        _snap_cache.pop(k, None)
    try:
        t = await build_token_data(addr, hint or None)
    except Exception as e:
        logger.warning(f"refresh failed for {addr[:12]}...: {e}")
        t = None
    if not t:
        return
    markup = token_buttons(t)
    for premium in (True, False):
        try:
            await q.edit_message_text(format_token_snapshot(t, premium=premium), parse_mode="HTML",
                                      disable_web_page_preview=True, reply_markup=markup)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return  # nothing changed since the last scan
            logger.warning(f"refresh edit failed (premium={premium}): {e}")
        except Exception as e:
            logger.warning(f"refresh edit failed (premium={premium}): {e}")


# --- Public commands -------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Buy Bot for Token", callback_data="st:buy")],
        [InlineKeyboardButton("PnL Cards", callback_data="st:pnl")],
    ])
    for premium in (True, False):
        text = (
            f'{em("w_welcome", "🔮", premium)} <b><u>Welcome to Wizard Scan Buy Bot!</u></b>\n\n'
            "Add the bot as an admin in your lounge or any group. Whenever a token CA is dropped, "
            "the bot will instantly show you all the token details.\n\n"
            f'{em("w_more", "⚡️", premium)}<b>+ the bot comes with two more powerful features:</b>\n\n'
            f'{em("w_feat", "🔹", premium)} Buy Bot for Token\n'
            f'{em("w_feat", "🔹", premium)} PnL Cards\n\n'
            f'{em("snapshot", "🐳", premium)} Tap the buttons below to explore all features\n'
            "      and see what the bot can do."
        )
        try:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            logger.warning(f"/start send failed (premium={premium}): {e}")


_START_INFO = {
    "st:buy": "🔮 Buy Bot for Token: drop any token CA or chart link and get instant live details "
              "(price, MCAP, liquidity, volume, security) in your lounge or DM.",
    "st:pnl": "📸 PnL Cards: paste a token CA, then send /pnl later - the bot makes a card showing "
              "how many X the token did since you called it.",
}


async def cb_start_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(_START_INFO.get(q.data, ""), show_alert=True)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    found = extract_ca_from_text(" ".join(context.args or []))
    if not found:
        await update.message.reply_text(
            "Usage: <code>/scan &lt;contract address or chart link&gt;</code>", parse_mode="HTML")
        return
    await handle_ca_lookup(update.message, found[0], found[1])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "📋 <b>Commands</b>\n\n"
        "/start - about this bot\n"
        "/help - this list\n"
        "/scan &lt;address or link&gt; - scan a token\n"
        "/pnl - PnL card for the last token you pasted\n\n"
        "🔮 <b>Token lookup:</b> paste any contract address or chart link, "
        "here in DM or in a group where I'm an admin.",
        parse_mode="HTML")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM: owner broadcast wizard first, then CA/chart-link lookup for everyone."""
    msg = update.message
    u = update.effective_user
    if not msg or not u:
        return
    add_user(u.id, u.username, u.first_name)

    if u.id in OWNER_IDS and u.id in broadcast_state:
        if await handle_broadcast_flow(update, context):
            return

    if not msg.text:
        return
    found = extract_ca_from_text(msg.text)
    if found:
        await handle_ca_lookup(msg, found[0], found[1])


# --- Channel-join gate (DM): users must join @WizardScan before using the bot ---------------------
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@WizardScan")
REQUIRED_CHANNEL_LINK = os.environ.get("REQUIRED_CHANNEL_LINK", "https://t.me/WizardScan")
_member_cache: dict = {}   # user_id -> time of last positive check (10 min)
_pending_gate: dict = {}   # user_id -> the message they sent before being blocked


async def is_channel_member(bot, uid: int, fresh: bool = False) -> bool:
    """True if the user is in the required channel. The bot must be admin there.
    If Telegram can't be asked (e.g. bot removed from channel) it lets users through."""
    if not fresh:
        hit = _member_cache.get(uid)
        if hit and time.time() - hit < 600:
            return True
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        ok = m.status in ("member", "administrator", "creator") or (
            m.status == "restricted" and bool(getattr(m, "is_member", False)))
    except Exception as e:
        logger.warning(f"channel membership check failed for {uid}: {e}")
        return True
    if ok:
        _member_cache[uid] = time.time()
    else:
        _member_cache.pop(uid, None)
    return ok


async def join_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every DM handler: commands and CA/chart-link pastes from users who
    have not joined the channel get the 'join first' message instead."""
    msg, u = update.effective_message, update.effective_user
    if not msg or not u or u.id in OWNER_IDS:
        return
    text = (msg.text or "").strip()
    if not text or not (text.startswith("/") or extract_ca_from_text(text)):
        return
    if await is_channel_member(context.bot, u.id):
        return
    add_user(u.id, u.username, u.first_name)
    _pending_gate[u.id] = msg
    if len(_pending_gate) > 5000:
        _pending_gate.clear()
        _pending_gate[u.id] = msg
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Wizard Scan Channel", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Done", callback_data="jn:done")],
    ])
    await msg.reply_text(
        "🔒 <b>Join our channel first!</b>\n\n"
        "Please join the Wizard Scan channel before using the bot's commands.\n"
        "After joining, tap <b>Done</b> and I'll run your command.",
        parse_mode="HTML", reply_markup=kb)
    raise ApplicationHandlerStop


async def _replay_message(orig, context):
    """Run the command/CA the user sent before they were blocked."""
    fake = Update(update_id=0, message=orig)
    text = (orig.text or "").strip()
    if text.startswith("/"):
        parts = text.split()
        context.args = parts[1:]
        fn = {"start": cmd_start, "help": cmd_help, "scan": cmd_scan, "pnl": cmd_pnl}.get(
            parts[0][1:].split("@")[0].lower())
        if fn:
            await fn(fake, context)
    else:
        await handle_private_message(fake, context)


async def cb_join_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await is_channel_member(context.bot, uid, fresh=True):
        await q.answer("❌ You haven't joined the channel yet. Join first, then tap Done.", show_alert=True)
        return
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    orig = _pending_gate.pop(uid, None)
    if not orig:
        await context.bot.send_message(uid, "✅ Thanks for joining! Please send your command again.")
        return
    try:
        await _replay_message(orig, context)
    except Exception as e:
        logger.warning(f"replay after join failed for {uid}: {e}")


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


async def block_non_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot does nothing in groups, EXCEPT: a CA/chart-link paste in a group
    where it has admin rights gets a snapshot reply."""
    if _is_private(update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    text = ((getattr(msg, "text", None) or getattr(msg, "caption", None) or "") if msg else "")
    if msg and chat and chat.type in ("group", "supergroup") and text.startswith("/pnl"):
        parts = text.split()
        if parts[0].split("@")[0].lower() == "/pnl" and await bot_is_admin_here(context, chat):
            context.args = parts[1:]
            try:
                await cmd_pnl(update, context)
            except Exception as e:
                logger.warning(f"group /pnl failed in {chat.id}: {e}")
    if msg and chat and chat.type in ("group", "supergroup") and text and not text.startswith("/"):
        found = extract_ca_from_text(text)
        if found and await bot_is_admin_here(context, chat):
            try:
                await handle_ca_lookup(msg, found[0], found[1], quiet_if_missing=True)
            except Exception as e:
                logger.warning(f"group CA lookup failed in {chat.id}: {e}")
    raise ApplicationHandlerStop


# --- Admin status tracking + owner notification ---------------------------------------------
async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when the bot's own status changes in any group/supergroup/channel."""
    try:
        cm = update.my_chat_member
        if not cm:
            return
        chat = cm.chat
        if chat.type not in ("group", "supergroup", "channel"):
            return
        old, new, actor = cm.old_chat_member, cm.new_chat_member, cm.from_user

        was_admin = bool(old and old.status == "administrator")
        is_admin = new.status == "administrator"
        groups = load_admin_groups()
        key = str(chat.id)
        title = chat.title or chat.username or str(chat.id)

        if is_admin and not was_admin:
            groups[key] = {"title": title, "username": chat.username or "", "type": chat.type}
            save_admin_groups(groups)
            who = "Unknown"
            if actor:
                who = f"@{actor.username}" if actor.username else actor.full_name
                who += f" (ID <code>{actor.id}</code>)"
            else:
                who = html.escape(who)
            kind = "Channel" if chat.type == "channel" else "Group"
            where = f"@{html.escape(chat.username)}" if chat.username else f"ID <code>{chat.id}</code>"
            await notify_owners(
                context.bot,
                f"👑 <b>Bot was made admin!</b>\n\n"
                f"👤 By: {who}\n"
                f"📍 {kind}: <b>{html.escape(title)}</b> ({where})")
        elif was_admin and not is_admin:
            if groups.pop(key, None) is not None:
                save_admin_groups(groups)
    except Exception as e:
        logger.warning(f"on_chat_member_update: {e}")


# --- /ownerhelp panel --------------------------------------------------------------------------
OH_HOME_TEXT = (
    "🔮 <b>OWNER CONTROL PANEL</b>\n\n"
    "📢 <b>Broadcast</b> - DM all users (or a selection) who started the bot\n"
    "📋 <b>Admin Groups</b> - groups/channels where the bot is currently admin"
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
            "📋 <b>Admin Groups</b>\n\nThe bot is not an admin anywhere right now.",
            parse_mode="HTML")
        return
    lines = []
    for cid, info in groups.items():
        label = f"@{info['username']}" if info.get("username") else (info.get("title") or "Unknown")
        kind = "📢 Channel" if info.get("type") == "channel" else "👥 Group"
        lines.append(f"{kind} - <b>{html.escape(label)}</b> (<code>{cid}</code>)")
    await reply_target.reply_text(
        f"📋 <b>Bot is admin in {len(groups)} place(s):</b>\n\n" + "\n".join(lines),
        parse_mode="HTML")


@owner_only
async def cmd_admin_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_admin_groups_list(update.message)


# --- Broadcast wizard (in-memory state) ---------------------------------------------------------
# broadcast_state[owner_id] = {"stage": "pick", "all_users": {...}} or {"stage": "message", "targets": [...]}
broadcast_state: dict = {}


async def _broadcast_panel(reply_target, owner_id: int):
    users = load_users()
    if not users:
        await reply_target.reply_text("No users yet - nobody has started the bot.")
        return
    broadcast_state[owner_id] = {"stage": "pick", "all_users": users}

    names = [f"@{v['username']}" for v in users.values() if v.get("username")]
    no_username = len(users) - len(names)
    note = (f"\n⚠️ {no_username} user(s) have no username (they still receive it if you send 'all')."
            if no_username else "")

    # Pages of max 100 names, also capped by characters to stay under Telegram's 4096 limit
    chunks, cur, cur_len = [], [], 0
    for n in names:
        if cur and (len(cur) >= 100 or cur_len + len(n) + 1 > 3500):
            chunks.append(cur); cur = []; cur_len = 0
        cur.append(n); cur_len += len(n) + 1
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = [[]]

    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        page = f" (Page {i}/{total})" if total > 1 else ""
        body = f"<pre>{html.escape(chr(10).join(chunk))}</pre>" if chunk else "<i>(no usernames)</i>"
        footer = ""
        if i == total:
            footer = (
                f"{note}\n\n"
                f"Reply with usernames, comma-separated:\n"
                f"<code>@user1, @user2</code>\n\n"
                f"Or send <code>all</code> to message everyone.\n"
                f"/cancel to stop.")
        await reply_target.reply_text(
            f"📢 <b>Broadcast - {len(users)} users{page}</b>\n\n{body}{footer}",
            parse_mode="HTML")
        if i < total:
            await asyncio.sleep(0.3)


@owner_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _broadcast_panel(update.message, update.effective_user.id)


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if broadcast_state.pop(update.effective_user.id, None):
        await update.message.reply_text("✅ Broadcast cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def _copy_with_retry(bot, target_id: int, from_chat: int, message_id: int):
    try:
        await bot.copy_message(chat_id=target_id, from_chat_id=from_chat, message_id=message_id)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        await bot.copy_message(chat_id=target_id, from_chat_id=from_chat, message_id=message_id)


async def handle_broadcast_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if this owner message was consumed by the broadcast wizard."""
    uid = update.effective_user.id
    st = broadcast_state.get(uid)
    if not st:
        return False
    msg = update.message

    if st["stage"] == "pick":
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("Send usernames (comma-separated) or <code>all</code>.", parse_mode="HTML")
            return True
        all_users = st["all_users"]
        if text.lower() == "all":
            targets = [int(k) for k in all_users]
        else:
            wanted = {x.strip().lstrip("@").lower() for x in text.split(",") if x.strip()}
            targets = [int(k) for k, v in all_users.items()
                       if (v.get("username") or "").lower() in wanted]
            if not targets:
                await msg.reply_text("⚠️ No matching usernames. Try again, or /cancel.")
                return True
        broadcast_state[uid] = {"stage": "message", "targets": targets}
        await msg.reply_text(
            f"✅ <b>{len(targets)} recipient(s) selected.</b>\n\n"
            f"Now send the message (text, photo, video, etc.) and it will be delivered to all of them.\n"
            f"/cancel to stop.",
            parse_mode="HTML")
        return True

    if st["stage"] == "message":
        targets = st["targets"]
        broadcast_state.pop(uid, None)
        status = await msg.reply_text(f"⏳ Sending to {len(targets)} user(s)…")
        ok = fail = 0
        for i, target_id in enumerate(targets, start=1):
            try:
                await _copy_with_retry(context.bot, target_id, msg.chat_id, msg.message_id)
                ok += 1
            except Exception as e:
                fail += 1
                logger.debug(f"broadcast to {target_id} failed: {e}")
            if i % 25 == 0:
                await asyncio.sleep(1)
        await status.edit_text(f"✅ <b>Broadcast complete.</b>\nSent: {ok} | Failed: {fail}", parse_mode="HTML")
        return True

    return False


async def cb_ownerhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if uid not in OWNER_IDS:
        await query.answer("Owner only.", show_alert=True)
        return
    await query.answer()
    if query.data == "oh:broadcast":
        await _broadcast_panel(query.message, uid)
    elif query.data == "oh:admingroups":
        await _send_admin_groups_list(query.message)


# --- Main ----------------------------------------------------------------------------------------
# ===== PNL-CARD START =====
# /pnl: after someone pastes a CA, /pnl makes a card image showing the X since their call.
CALLS_FILE = DATA_DIR / "calls.json"
FONT_PATH = DATA_DIR / "fonts" / "Poppins-Bold.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf"
CARD_W, CARD_H = 1280, 720

# (bg top, bg bottom, accent) - a random one is picked for every card
_GAIN_THEMES = [
    ((10, 60, 30), (2, 20, 10), (34, 255, 85)),      # green
    ((45, 18, 90), (10, 4, 28), (190, 110, 255)),    # purple
    ((78, 52, 0), (22, 14, 0), (255, 200, 61)),      # gold
    ((0, 55, 80), (2, 12, 28), (60, 220, 255)),      # cyan
    ((85, 15, 60), (24, 4, 20), (255, 90, 190)),     # pink
    ((15, 35, 95), (4, 8, 30), (90, 150, 255)),      # blue
    ((85, 40, 5), (26, 10, 2), (255, 140, 40)),      # orange
    ((20, 70, 70), (4, 22, 24), (80, 255, 200)),     # teal
]
_LOSS_THEME = ((85, 12, 12), (24, 3, 3), (255, 77, 77))


def ensure_font():
    """Download the card font once (kept in DATA_DIR). Falls back to system fonts if it fails."""
    try:
        if FONT_PATH.exists():
            return
        FONT_PATH.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(FONT_URL, timeout=20)
        if r.status_code == 200 and len(r.content) > 10000:
            FONT_PATH.write_bytes(r.content)
    except Exception as e:
        logger.warning(f"card font download failed: {e}")


_font_cache: dict = {}


def _font(size: int):
    from PIL import ImageFont
    if size in _font_cache:
        return _font_cache[size]
    f = None
    for p in (str(FONT_PATH), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        try:
            f = ImageFont.truetype(p, size)
            break
        except Exception:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size)
        except TypeError:
            f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def fmt_dur(secs: float) -> str:
    secs = max(0, int(secs))
    d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
    if d:
        return f"{d}d, {h}h"
    return f"{h}h, {m}m" if h else f"{m}m"


def fmt_mult(m: float) -> str:
    return f"{m:.0f}X" if m >= 10 else f"{m:.1f}X"


def record_call(chat_id: int, user, t: dict):
    """Remember the market cap at the moment this user first pasted this token in this chat."""
    try:
        if not user or not t.get("token_addr"):
            return
        d = load_json(CALLS_FILE, {})
        key = f"{chat_id}:{user.id}"
        e = d.get(key) or {"calls": {}, "last": None}
        tok = f"{t.get('chain_id')}:{t['token_addr'].lower()}"
        if tok not in e["calls"]:
            e["calls"][tok] = {
                "addr": t["token_addr"], "chain_id": t.get("chain_id"), "chain": t.get("chain"),
                "symbol": t.get("symbol"), "mcap": t.get("mcap"), "price": t.get("price"),
                "ts": time.time(), "logo": t.get("logo"),
            }
            if len(e["calls"]) > 30:
                oldest = min(e["calls"], key=lambda k: e["calls"][k]["ts"])
                e["calls"].pop(oldest, None)
        e["last"] = tok
        e["name"] = user.username or user.first_name or "anon"
        e["username"] = user.username or ""
        e["fname"] = user.first_name or ""
        d[key] = e
        if len(d) > 20000:
            for k in sorted(d, key=lambda k: max((c["ts"] for c in d[k]["calls"].values()), default=0))[:2000]:
                d.pop(k, None)
        save_json(CALLS_FILE, d)
    except Exception as ex:
        logger.debug(f"record_call failed: {ex}")


def make_pnl_card(symbol: str, chain: str, mult: float, called_mc, age_secs: float,
                  caller: str, logo_url: str = None):
    """Draws the PnL card with Pillow and returns a BytesIO PNG."""
    import io
    import random
    from PIL import Image, ImageDraw, ImageFilter

    W, H = CARD_W, CARD_H
    top, bot, acc = _LOSS_THEME if mult < 1 else random.choice(_GAIN_THEMES)
    rnd = random.Random()

    # background gradient
    img = Image.new("RGB", (W, H))
    dr = ImageDraw.Draw(img)
    for y in range(H):
        k = y / (H - 1)
        dr.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
    img = img.convert("RGBA")

    # soft glow blobs + sparkles
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for _ in range(4):
        cx, cy, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(120, 260)
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=acc + (38,))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    img = Image.alpha_composite(img, glow)
    sp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sp)
    for _ in range(70):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.choice((1, 1, 2, 3))
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rnd.randint(40, 160)))
    img = Image.alpha_composite(img, sp)

    # crystal orb with a wizard star (left side)
    ox, oy, orad = 300, 430, 190
    og = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(og).ellipse((ox - orad - 30, oy - orad - 30, ox + orad + 30, oy + orad + 30), fill=acc + (90,))
    og = og.filter(ImageFilter.GaussianBlur(45))
    img = Image.alpha_composite(img, og)
    ol = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ol)
    od.ellipse((ox - orad, oy - orad, ox + orad, oy + orad), fill=(top[0] // 2, top[1] // 2, top[2] // 2, 235),
               outline=acc + (255,), width=7)
    od.ellipse((ox - orad + 40, oy - orad + 28, ox - 20, oy - 60), fill=(255, 255, 255, 55))
    # 4-point sparkles inside the orb
    for sx, sy, sr in ((ox + 20, oy - 20, 70), (ox - 80, oy + 70, 34), (ox + 90, oy + 85, 26)):
        od.polygon([(sx, sy - sr), (sx + sr * 0.22, sy - sr * 0.22), (sx + sr, sy), (sx + sr * 0.22, sy + sr * 0.22),
                    (sx, sy + sr), (sx - sr * 0.22, sy + sr * 0.22), (sx - sr, sy), (sx - sr * 0.22, sy - sr * 0.22)],
                   fill=acc + (255,))
    # orb stand
    od.rounded_rectangle((ox - 120, oy + orad - 10, ox + 120, oy + orad + 38), radius=18,
                         fill=(20, 14, 30, 255), outline=acc + (255,), width=4)
    img = Image.alpha_composite(img, ol)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle((46, 46, W - 46, H - 46), radius=30, outline=acc + (255,), width=3)

    # token logo (top centre)
    lr, lcx, lcy = 56, W // 2, 46
    logo = None
    if logo_url:
        try:
            import io as _io
            r = requests.get(logo_url, timeout=4)
            if r.status_code == 200:
                logo = Image.open(_io.BytesIO(r.content)).convert("RGBA").resize((lr * 2 - 8, lr * 2 - 8))
        except Exception:
            logo = None
    d.ellipse((lcx - lr, lcy - lr, lcx + lr, lcy + lr), fill=(10, 10, 14, 255), outline=acc + (255,), width=4)
    if logo:
        mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, logo.size[0] - 1, logo.size[1] - 1), fill=255)
        img.paste(logo, (lcx - logo.size[0] // 2, lcy - logo.size[1] // 2), mask)
    else:
        d.text((lcx, lcy), (symbol or "?")[:1].upper(), font=_font(54), fill=(255, 255, 255, 255), anchor="mm")

    RX = W - 90
    # chain pill
    cw = d.textlength(chain, font=_font(26)) + 36
    d.rounded_rectangle((RX - cw, 96, RX, 140), radius=22, outline=acc + (255,), width=2)
    d.text((RX - cw / 2, 118), chain, font=_font(26), fill=acc + (255,), anchor="mm")

    # symbol (auto-shrinks for long names)
    sym = (symbol or "TOKEN").upper()
    size = 120
    while size > 50 and d.textlength(sym, font=_font(size)) > 640:
        size -= 6
    d.text((RX, 150), sym, font=_font(size), fill=(255, 255, 255, 255), anchor="ra")

    # big multiplier with glow
    mt = fmt_mult(mult)
    mf = _font(240)
    mglow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(mglow).text((RX, 232), mt, font=mf, fill=acc + (255,), anchor="ra")
    img = Image.alpha_composite(img, mglow.filter(ImageFilter.GaussianBlur(22)))
    img = Image.alpha_composite(img, mglow.filter(ImageFilter.GaussianBlur(8)))
    d = ImageDraw.Draw(img)
    light = tuple(min(255, c + 90) for c in acc)
    d.text((RX, 232), mt, font=mf, fill=light + (255,), anchor="ra")

    # "Called at 530K | 1h, 58m"
    pieces = [("Called at ", (205, 205, 215)), (fmt_num(called_mc) if called_mc else "N/A", (255, 200, 61)),
              ("  |  ", (140, 140, 150)), (fmt_dur(age_secs), (205, 205, 215))]
    f2 = _font(38)
    x = RX - sum(d.textlength(p, font=f2) for p, _ in pieces)
    for txt, col in pieces:
        d.text((x, 508), txt, font=f2, fill=col + (255,), anchor="la")
        x += d.textlength(txt, font=f2)

    # caller + footer
    d.text((RX, 560), (caller or "ANON").upper()[:22], font=_font(52), fill=(255, 255, 255, 255), anchor="ra")
    d.text((RX, 624), "@WizardScan  ·  Wizard Scan Buy Bot", font=_font(24), fill=acc + (255,), anchor="ra")

    out = io.BytesIO()
    out.name = "pnl.png"
    img.convert("RGB").save(out, "PNG", optimize=False)
    out.seek(0)
    return out


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u = update.effective_message, update.effective_user
    if not msg or not u:
        return
    d = load_json(CALLS_FILE, {})
    e = d.get(f"{msg.chat_id}:{u.id}")
    arg = extract_ca_from_text(" ".join(context.args or []))
    call = None
    if e:
        if arg:
            call = next((c for c in e["calls"].values() if c["addr"].lower() == arg[0].lower()), None)
        else:
            call = e["calls"].get(e.get("last"))
    if not call:
        await msg.reply_text(
            "📸 <b>No call found.</b>\n\nPaste a token contract address here first, "
            "then use /pnl later to get your PnL card.", parse_mode="HTML")
        return
    try:
        await context.bot.send_chat_action(msg.chat_id, "upload_photo")
    except Exception:
        pass
    try:
        t = await build_token_data(call["addr"], call.get("chain_id"))
    except Exception as ex:
        logger.warning(f"pnl data failed: {ex}")
        t = None
    if not t:
        await msg.reply_text("⚠️ Couldn't get live data for this token right now. Try again in a moment.")
        return
    mc0, now_mc = call.get("mcap"), t.get("mcap")
    if mc0 and now_mc:
        mult = now_mc / mc0
    elif call.get("price") and t.get("price"):
        mult = t["price"] / call["price"]
    else:
        await msg.reply_text("⚠️ Not enough price data to calculate this PnL yet.")
        return
    name = u.username or u.first_name or "anon"
    try:
        png = await asyncio.to_thread(
            make_pnl_card, call.get("symbol") or t.get("symbol") or "TOKEN",
            call.get("chain") or t.get("chain") or "?", mult, mc0,
            time.time() - call["ts"], name, call.get("logo") or t.get("logo"))
    except Exception as ex:
        logger.warning(f"pnl card failed: {type(ex).__name__}: {ex}")
        await msg.reply_text("⚠️ Couldn't generate the card right now.")
        return
    sym = html.escape((call.get("symbol") or t.get("symbol") or "").upper())
    trend = "📈" if mult >= 1 else "📉"
    await msg.reply_photo(
        photo=png, parse_mode="HTML",
        caption=f"{trend} <b>${sym}</b> is <b>{fmt_mult(mult)}</b> since your call")
# ===== PNL-CARD END =====


# ===== ALERTS START =====
# Multiplier alerts: when a token that someone called in a group reaches 2X, 3X, 4X ... the bot
# posts an alert in that group. Only the FIRST caller of a token in a group gets the credit.
ALERTS_FILE = DATA_DIR / "alerts.json"
ALERT_INTERVAL = 45                      # seconds between checks
ALERT_MAX_AGE = 7 * 86400                # stop watching a call after 7 days
MIN_ALERT_LIQ = float(os.environ.get("MIN_ALERT_LIQ", "1000"))  # ignore fake pumps on dust liquidity
MAX_WATCHED = 400                        # tokens checked per cycle
_LADDER = [2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 300, 500, 1000]


def ladder_level(mult: float):
    """Highest milestone (2X, 3X, ...) that `mult` has reached, or None."""
    hit = [x for x in _LADDER if mult >= x]
    return hit[-1] if hit else None


def _dex_batch_mcaps(tokens):
    """tokens = [(chain_id, address)] -> {(chain_id, address_lower): (mcap, liquidity)} using
    DexScreener's multi-token endpoint (30 addresses per request, run in parallel)."""
    from concurrent.futures import ThreadPoolExecutor
    addrs = sorted({a for _, a in tokens})
    chunks = [addrs[i:i + 30] for i in range(0, len(addrs), 30)]

    def one(ch):
        d = _get_json("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(ch), timeout=10)
        return (d or {}).get("pairs") or []

    best = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for pairs in ex.map(one, chunks):
            for p in pairs:
                base = ((p.get("baseToken") or {}).get("address") or "").lower()
                chain = (p.get("chainId") or "").lower()
                liq = _f((p.get("liquidity") or {}).get("usd")) or 0
                mc = _f(p.get("marketCap")) or _f(p.get("fdv"))
                if not base or not mc:
                    continue
                k = (chain, base)
                if k not in best or liq > best[k][1]:
                    best[k] = (mc, liq)
    return best


def _collect_first_calls():
    """For every (group, token) find who called it FIRST and how many people called it."""
    d = load_json(CALLS_FILE, {})
    cutoff = time.time() - ALERT_MAX_AGE
    first, count = {}, {}
    for key, e in d.items():
        chat_s, _, uid_s = key.partition(":")
        try:
            chat_id, uid = int(chat_s), int(uid_s)
        except ValueError:
            continue
        if chat_id >= 0:  # DMs have no group to alert
            continue
        for tok, c in (e.get("calls") or {}).items():
            if c.get("ts", 0) < cutoff or not c.get("mcap"):
                continue
            k = (chat_id, tok)
            count[k] = count.get(k, 0) + 1
            if k not in first or c["ts"] < first[k]["ts"]:
                first[k] = dict(c, uid=uid, chat_id=chat_id, tok=tok,
                                uname=e.get("username") or "", fname=e.get("fname") or e.get("name") or "anon")
    return first, count


def _caller_html(c: dict) -> str:
    if c.get("uname"):
        return f'<a href="https://t.me/{html.escape(c["uname"], quote=True)}">@{html.escape(c["uname"])}</a>'
    return f'<a href="tg://user?id={c["uid"]}">{html.escape(c.get("fname") or "anon")}</a>'


def alert_text(c: dict, t: dict, mult: float, level: int, peak: float, callers: int) -> str:
    sym = html.escape((c.get("symbol") or t.get("symbol") or "TOKEN").upper())
    icon = "👑" if level >= 50 else "💎" if level >= 10 else "🔥" if level >= 5 else "🚀"
    lines = [
        f"{icon} <b>${sym}</b> just hit <b>{level}X</b> since the call!",
        "",
        f"🔮 Called at <b>{fmt_usd(c.get('mcap'))}</b> → now <b>{fmt_usd(t.get('mcap'))}</b> (<b>{fmt_mult(mult)}</b>)",
        f"👤 First caller: {_caller_html(c)}",
        f"⏱ {fmt_dur(time.time() - c['ts'])} after the call",
    ]
    if peak and peak > mult * 1.02:
        lines.append(f"📈 Peak so far: <b>{fmt_mult(peak)}</b>")
    lines.append(f"💧 Liq {fmt_usd(t.get('liquidity_usd'))}  ·  📊 Vol 24h {fmt_usd(t.get('volume_h24'))}")
    if callers > 1:
        lines.append(f"👥 {callers} people called this token here")
    lines += ["", f'<blockquote><code>{html.escape(t.get("token_addr") or c["addr"])}</code></blockquote>']
    return "\n".join(lines)


def alert_buttons(t: dict) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("Trade", url=maestro_url(t))]
    if t.get("chart_url"):
        row.append(InlineKeyboardButton("Chart", url=t["chart_url"]))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton(AUDIT_BUTTON_TEXT, url=audit_url(t))]])


async def _check_multipliers(bot):
    first, count = await asyncio.to_thread(_collect_first_calls)
    if not first:
        return
    toks = {}
    for c in first.values():
        toks[(c.get("chain_id") or "", c["addr"].lower())] = (c.get("chain_id") or "", c["addr"])
        if len(toks) >= MAX_WATCHED:
            break
    prices = await asyncio.to_thread(_dex_batch_mcaps, list(toks.values()))
    state = load_json(ALERTS_FILE, {})
    changed = False
    for (chat_id, tok), c in first.items():
        got = prices.get(((c.get("chain_id") or "").lower(), c["addr"].lower()))
        if not got:
            continue
        mc_now, liq = got
        if liq < MIN_ALERT_LIQ:
            continue
        mult = mc_now / c["mcap"]
        sk = f"{chat_id}|{tok}"
        st = state.get(sk) or {"level": 0, "peak": 0}
        if mult > st.get("peak", 0):
            st["peak"] = round(mult, 3)
            state[sk] = st
            changed = True
        level = ladder_level(mult)
        if not level or level <= st.get("level", 0):
            continue
        st["level"] = level
        state[sk] = st
        changed = True
        try:
            t = await build_token_data(c["addr"], c.get("chain_id") or None) or {}
            if not t.get("mcap"):
                t["mcap"] = mc_now
            t.setdefault("token_addr", c["addr"])
            t.setdefault("chain_id", c.get("chain_id"))
            await bot.send_message(
                chat_id, alert_text(c, t, mult, level, st["peak"], count.get((chat_id, tok), 1)),
                parse_mode="HTML", disable_web_page_preview=True, reply_markup=alert_buttons(t))
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logger.warning(f"alert send failed in {chat_id}: {e}")
        await asyncio.sleep(0.6)
    if changed:
        if len(state) > 20000:
            for k in list(state)[:2000]:
                state.pop(k, None)
        save_json(ALERTS_FILE, state)


async def multiplier_watcher(app):
    await asyncio.sleep(20)
    while True:
        try:
            await _check_multipliers(app.bot)
        except Exception as e:
            logger.warning(f"multiplier watcher error: {type(e).__name__}: {e}")
        await asyncio.sleep(ALERT_INTERVAL)
# ===== ALERTS END =====


PUBLIC_COMMANDS = [
    BotCommand("start", "About this bot"),
    BotCommand("help", "List of commands"),
    BotCommand("scan", "Scan a token by address or link"),
    BotCommand("pnl", "PnL card for the last token you pasted"),
]
OWNER_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("ownerhelp", "Owner control panel"),
    BotCommand("broadcast", "Message your users"),
    BotCommand("admingroups", "Groups/channels where the bot is admin"),
    BotCommand("cancel", "Cancel the current action"),
]


async def validate_emoji_ids(bot):
    """Ask Telegram which custom emoji IDs really exist; a single bad ID makes the whole
    premium message fail, so bad ones are switched to plain emoji (and logged)."""
    ids = [v for v in EMOJI_IDS.values() if v]
    try:
        stickers = await bot.get_custom_emoji_stickers(ids)
        good = {st.custom_emoji_id for st in stickers}
    except Exception as e:
        logger.warning(f"emoji ID check failed: {e}")
        return
    if not good:
        return
    for k, v in list(EMOJI_IDS.items()):
        if v and v not in good:
            logger.warning(f"Custom emoji '{k}' ({v}) is invalid/unavailable - using plain emoji for it")
            EMOJI_IDS[k] = ""


async def post_init(application):
    """Registers the command menu that appears when someone types '/'."""
    await validate_emoji_ids(application.bot)
    await asyncio.to_thread(ensure_font)
    application.bot_data["watcher"] = asyncio.create_task(multiplier_watcher(application))
    try:
        await application.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning(f"set_my_commands (public) failed: {e}")
    for oid in OWNER_IDS:
        try:
            await application.bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=oid))
        except Exception as e:
            logger.warning(f"set_my_commands (owner {oid}) failed - owner must /start the bot once: {e}")


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Add it in Railway -> service -> Variables.")
        raise SystemExit(1)
    if not OWNER_IDS:
        logger.warning("OWNER_ID is not set - owner commands are disabled.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    private = filters.ChatType.PRIVATE

    # Public commands
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=private))
    app.add_handler(CommandHandler("pnl", cmd_pnl, filters=private))

    # Owner commands
    app.add_handler(CommandHandler("ownerhelp", cmd_ownerhelp, filters=private))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=private))
    app.add_handler(CommandHandler("admingroups", cmd_admin_groups, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CallbackQueryHandler(cb_ownerhelp, pattern=r"^oh:"))
    app.add_handler(CallbackQueryHandler(cb_refresh, pattern=r"^rf:"))
    app.add_handler(CallbackQueryHandler(cb_start_info, pattern=r"^st:"))

    # Group gate - runs before everything else
    app.add_handler(MessageHandler(filters.ALL, block_non_private), group=-1)

    # Channel-join gate for DMs - runs before everything else
    app.add_handler(MessageHandler(private, join_gate), group=-2)
    app.add_handler(CallbackQueryHandler(cb_join_done, pattern=r"^jn:"))

    # DM messages: CA lookup + owner broadcast wizard (any content type)
    app.add_handler(MessageHandler(private & ~filters.COMMAND, handle_private_message))

    # Bot's own admin-status changes (group / supergroup / channel)
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info(f"Wizard Scan Buy Bot starting - owner(s): {OWNER_IDS}")
    backoff = 5
    while True:
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)
            break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            logger.error(f"Polling crashed: {type(e).__name__}: {e} - restarting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
