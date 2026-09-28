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
from datetime import datetime
from pathlib import Path

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
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
MAESTRO_LINK = os.environ.get("MAESTRO_LINK", "https://t.me/maestro?start=r-wizard_scan")  # Trade button

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
    "ton": "TON", "sui": "SUI", "tron": "TRX", "aptos": "APT",
}


def chain_label(chain_id: str) -> str:
    return _CHAIN_LABELS.get(chain_id, (chain_id or "?").upper())


def fmt_num(value) -> str:
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value/1_000:.2f}K"
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
    }


def _solana_rpc_mint(addr: str):
    """Read the token's mint account straight from a Solana RPC node:
    mint authority (renounced?) and transfer fee (tax). Never raises."""
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
        out = {"renounced": info.get("mintAuthority") is None}
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


def _rugcheck(addr: str):
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


def fetch_solana_security(addr: str):
    """Solana: holders (RugCheck), renounce + tax (RPC, falling back to RugCheck)."""
    out = _rugcheck(addr)
    out.update(_solana_rpc_mint(addr))
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
    out = {}
    try:
        net = _GT_NETWORKS.get(chain_id, chain_id)
        if pair:
            d = _gt_get(f"/networks/{net}/pools/{pair}")
            a = ((d or {}).get("data") or {}).get("attributes") or {}
            tx = (a.get("transactions") or {}).get("h24") or {}
            b, s_ = tx.get("buyers"), tx.get("sellers")
            if isinstance(b, int) and isinstance(s_, int):
                out["traders"] = b + s_
            pc = a.get("price_change_percentage") or {}
            out["chg_m5"], out["chg_h1"] = _f(pc.get("m5")), _f(pc.get("h1"))
            out["listed_iso"] = a.get("pool_created_at")
            out["gt_mcap"] = _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd"))
            out["gt_price"] = _f(a.get("base_token_price_usd"))

            o = _gt_get(f"/networks/{net}/pools/{pair}/ohlcv/day",
                        params={"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"})
            candles = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            highs = [_f(c[2]) for c in candles if isinstance(c, list) and len(c) >= 3 and _f(c[2])]
            if highs:
                out["ath_price"] = max(highs)

        info = _gt_get(f"/networks/{net}/tokens/{token}/info")
        ia = ((info or {}).get("data") or {}).get("attributes") or {}
        h = (ia.get("holders") or {}).get("count")
        if h not in (None, ""):
            try:
                out["holders"] = int(h)
            except Exception:
                pass
        out["websites"] = ia.get("websites") or []
        out["twitter"] = ia.get("twitter_handle") or ""
        out["telegram"] = ia.get("telegram_handle") or ""
    except Exception as e:
        logger.debug(f"GT extras failed: {e}")
    return out


# DexScreener chainId -> GoPlus chain id
_GOPLUS_CHAINS = {
    "ethereum": "1", "bsc": "56", "base": "8453", "arbitrum": "42161",
    "polygon": "137", "avalanche": "43114", "optimism": "10", "linea": "59144",
    "fantom": "250", "cronos": "25", "zksync": "324", "scroll": "534352",
    "blast": "81457", "mantle": "5000", "pulsechain": "369", "sonic": "146",
    "tron": "tron",
}


def fetch_goplus_security(addr: str, chain_id: str):
    """Best-effort buy/sell tax, renounce and holders via GoPlus. Never raises."""
    gp = _GOPLUS_CHAINS.get(chain_id)
    if not gp:
        return {}
    try:
        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{gp}",
            params={"contract_addresses": addr}, timeout=6)
        if r.status_code != 200:
            return {}
        res = (r.json() or {}).get("result") or {}
        d = res.get(addr.lower()) or res.get(addr) or {}
        if not d:
            return {}
        out = {}
        for key in ("buy_tax", "sell_tax"):
            v = _f(d.get(key))
            if v is not None:
                out[key] = round(v * 100, 1)
        if "owner_address" in d:
            owner = (d.get("owner_address") or "").lower()
            out["renounced"] = owner in (
                "", "0x0000000000000000000000000000000000000000",
                "0x000000000000000000000000000000000000dead")
        if str(d.get("holder_count") or "").isdigit():
            out["holders"] = int(d["holder_count"])
        return out
    except Exception:
        return {}


# Premium (custom) emoji IDs. Change an ID here to change that emoji.
# If custom emojis are not allowed for the bot, Telegram just shows the normal
# emoji next to it, and if a send ever fails we retry with plain emojis.
EMOJI_IDS = {
    "title":    "5807491057792853272",
    "snapshot": "5846193940105012285",
    "market":   "6044166381690166762",
    "security": "5846117180449497406",
    "social":   "5848118295907017044",
}


def em(key: str, fallback: str, premium: bool = True) -> str:
    eid = EMOJI_IDS.get(key)
    if premium and eid:
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


def format_token_snapshot(t: dict, premium: bool = True) -> str:
    title = f"{t.get('name') or 'Unknown Token'}"
    if t.get("symbol"):
        title += f" ({t['symbol'].upper()})"

    holders = t.get("holders")
    holders_s = f"{holders:,} (Traders)" if isinstance(holders, int) else "N/A"

    if t.get("buy_tax") is None and t.get("sell_tax") is None:
        tax_s = "N/A"
    else:
        tax_s = f"{t.get('buy_tax') or 0}% buy / {t.get('sell_tax') or 0}% sell"

    price = t.get("price") or 0
    price_s = f"${price:.10f}".rstrip("0").rstrip(".") if price else "N/A"

    dex_s = f"{(t.get('dex') or 'N/A').upper()} · {t.get('chain')}"
    traders = t.get("traders")
    traders_s = f"{traders:,} (24h)" if isinstance(traders, int) else "N/A"
    vol_s = f"{fmt_usd(t.get('volume_h24'))} (24h)" if t.get("volume_h24") else "N/A"

    renounced = t.get("renounced")
    owner_s = ("Renounced ✅" if renounced is True
               else "Not renounced ⚠️" if renounced is False else "N/A")

    links = t.get("links") or {}
    parts = [
        f'<a href="{html.escape(links[k], quote=True)}">{k}</a>'
        for k in ("TG", "WEB", "X")
        if links.get(k, "").startswith(("http://", "https://"))
    ]
    social_s = "├ " + " ══ ".join(parts) + " ┤" if parts else "└ N/A"

    return (
        f"{em('title', '🔮', premium)}{html.escape(title)}\n\n"
        f"{em('snapshot', '🔎', premium)}TOKEN SNAPSHOT\n"
        f"├  Holders → {holders_s}\n"
        f"├  MCAP   → {fmt_usd(t.get('mcap'))}\n"
        f"├  PEAK    → {fmt_usd(t.get('peak'))}\n"
        f"├  TAX      → {tax_s}\n"
        f"└  PRICE   → {price_s}\n\n"
        f"{em('market', '⚡️', premium)}MARKET STATS\n"
        f"├  DEX     → {html.escape(dex_s)}\n"
        f"├  AGE     → {fmt_age(t.get('listed_at'))}\n"
        f"├  VOL     → {vol_s}\n"
        f"└  LIQ      → {fmt_usd(t.get('liquidity_usd'))}\n\n"
        f"{em('security', '👤', premium)}Security & Activity \n"
        f"├  TRADERS → {traders_s}\n"
        f"├  TXNS       → 🟢 {t.get('txns_buys', 0):,} / 🔴{t.get('txns_sells', 0):,} (24h)\n"
        f"├  5M/1H     → {fmt_pct(t.get('chg_m5'))} / {fmt_pct(t.get('chg_h1'))}\n"
        f"└  OWNER   → {owner_s}\n\n"
        f"{em('social', '🛡️', premium)} Social Media:\n"
        f"{social_s}\n\n"
        f"<code>{html.escape(t.get('token_addr') or '')}</code>"
    )


def _iso_to_ms(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


_snap_cache: dict = {}
SNAP_TTL = 45  # seconds - repeated pastes of the same token reuse the result


async def build_token_data(addr: str, chain_hint: str = None):
    key = (addr.lower(), chain_hint)
    hit = _snap_cache.get(key)
    if hit and time.time() - hit[0] < SNAP_TTL:
        return hit[1]

    t = await asyncio.to_thread(fetch_dex_data, addr, chain_hint)
    if not t:
        return None
    token = t["token_addr"]
    if t["chain_id"] == "solana":
        sec_job = asyncio.to_thread(fetch_solana_security, token)
    else:
        sec_job = asyncio.to_thread(fetch_goplus_security, token, t["chain_id"])
    gt_job = asyncio.to_thread(fetch_gt_extras, t["chain_id"], token, t.get("pair_addr"))
    sec, gt = await asyncio.gather(sec_job, gt_job, return_exceptions=True)
    sec = sec if isinstance(sec, dict) else {}
    gt = gt if isinstance(gt, dict) else {}

    t.update(sec)
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

    # PEAK = all-time-high market cap = ATH price x current supply
    if gt.get("ath_price") and t.get("price") and t.get("mcap"):
        t["peak"] = max(t["mcap"], gt["ath_price"] * (t["mcap"] / t["price"]))

    if len(_snap_cache) > 500:
        _snap_cache.clear()
    _snap_cache[key] = (time.time(), t)
    return t


def token_buttons(t: dict) -> InlineKeyboardMarkup:
    """Chart + Trade side by side on ONE row."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Chart", url=t.get("chart_url") or "https://dexscreener.com"),
        InlineKeyboardButton("⚡ Trade", url=MAESTRO_LINK),
    ]])


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
    try:
        await msg.reply_text(format_token_snapshot(t, premium=True), parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=markup)
    except Exception as e:
        logger.info(f"premium-emoji send failed ({e}); retrying with plain emojis")
        await msg.reply_text(format_token_snapshot(t, premium=False), parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=markup)


# --- Public commands -------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "🔮 <b>Wizard Scan Buy Bot</b>\n\n"
        "Paste any token contract address or chart link "
        "(Dexscreener, Dextools, Birdeye, GeckoTerminal, pump.fun, GMGN) "
        "and I'll reply with its live details. All major chains are supported.\n\n"
        "Add me as an admin in your group and I'll do the same there.\n\n"
        "Commands: /help",
        parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    await update.message.reply_text(
        "📋 <b>Commands</b>\n\n"
        "/start - about this bot\n"
        "/help - this list\n\n"
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


# --- Group gate: block everything except the admin-group CA lookup ---------------------------
async def block_non_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot does nothing in groups, EXCEPT: a CA/chart-link paste in a group
    where it currently has admin rights gets a snapshot reply."""
    if _is_private(update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    text = (getattr(msg, "text", "") or "") if msg else ""
    if msg and chat and chat.type in ("group", "supergroup") and text and not text.startswith("/"):
        if str(chat.id) in load_admin_groups():
            found = extract_ca_from_text(text)
            if found:
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
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Add it in Railway -> service -> Variables.")
        raise SystemExit(1)
    if not OWNER_IDS:
        logger.warning("OWNER_ID is not set - owner commands are disabled.")

    app = Application.builder().token(BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE

    # Public commands
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))

    # Owner commands
    app.add_handler(CommandHandler("ownerhelp", cmd_ownerhelp, filters=private))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=private))
    app.add_handler(CommandHandler("admingroups", cmd_admin_groups, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CallbackQueryHandler(cb_ownerhelp, pattern=r"^oh:"))

    # Group gate - runs before everything else
    app.add_handler(MessageHandler(filters.ALL, block_non_private), group=-1)

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
