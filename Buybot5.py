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
MAESTRO_LINK = os.environ.get("MAESTRO_LINK", "https://t.me/maestro?start=r-wizard_scan")  # your Maestro referral link
# Trade button: Maestro opens on the scanned token WITH your referral. {ca} = token address.
# Hardcoded on purpose (an old Railway variable was making the bot fall back to the plain link).
MAESTRO_TOKEN_LINK = "https://t.me/maestro?start={ca}_r-wizard_scan"
DEV_LINK = os.environ.get("DEV_LINK", "https://t.me/Wizard_Scan")  # Dev button

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
    }


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


_HONEYPOT_CHAINS = {"ethereum": 1, "bsc": 56, "base": 8453}


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


def format_token_snapshot(t: dict, premium: bool = True) -> str:
    r1, r2, r3, r4, r5 = (em(k, "🔹", premium) for k in ("r1", "r2", "r3", "r4", "r5"))
    A = em("arrow", "➤", premium)
    BUY, SELL = em("buy", "🟢", premium), em("sell", "🔴", premium)
    YES, NO = em("yes", "✅", premium), em("no", "❌", premium)

    chart = html.escape(t.get("chart_url") or "https://dexscreener.com", quote=True)
    name = html.escape(t.get("name") or "Unknown Token")
    sym = html.escape((t.get("symbol") or "").upper())
    head = " ".join(x for x in ((f"${sym}" if sym else ""), name, f"({html.escape(t.get('chain') or '?')})") if x)

    price = t.get("price") or 0
    price_s = f"${price:.10f}".rstrip("0").rstrip(".") if price else "N/A"
    dex_s = html.escape(pretty_dex(t.get("dex")))

    holders = t.get("holders")
    holders_s = f"{fmt_num(holders)} (Holders)" if isinstance(holders, int) else "N/A"
    if t.get("buy_tax") is None and t.get("sell_tax") is None:
        tax_s = "N/A"
    else:
        tax_s = f"{t.get('buy_tax') or 0}% buy &amp; {t.get('sell_tax') or 0}% sell"
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

    links = t.get("links") or {}
    parts = [f'<a href="{html.escape(links[k], quote=True)}">{k}</a>'
             for k in ("TG", "WEB", "X")
             if links.get(k, "").startswith(("http://", "https://"))]
    parts.append(f'<a href="{chart}">DEX</a>')
    social_s = " | ".join(parts)

    return (
        f'{em("title", "🔮", premium)} <b><a href="{chart}">{head}</a></b>\n'
        f'      <b><a href="{chart}">{dex_s}</a>   •  {fmt_age(t.get("listed_at"))}</b>\n\n'
        f'{em("snapshot", "🐳", premium)} <b>TOKEN SNAPSHOT</b>\n\n'
        f'{r1} MCAP   {A} {fmt_usd(t.get("mcap"))} (Current MC)\n'
        f'{r2} PEAK    {A} {fmt_usd(peak) if peak else "N/A"} (ATH)\n'
        f'{r2} TAX      {A} {tax_s}\n'
        f'{r2} Price     {A} {price_s}\n'
        f'{r3} Owner   {A} {owner_s}\n\n'
        f'{em("security", "👤", premium)} <b>Security &amp; Activity</b>\n\n'
        f'{r4} LP Locked:  {fmt_lp(t)}\n'
        f'{r5} Snipers:       {html.escape(str(t.get("snipers") or "N/A"))}\n'
        f'{r5} Bundled:      {html.escape(str(t.get("bundled") or "N/A"))}\n'
        f'{r5} AUDIT:        {html.escape(t.get("audit") or "N/A")}\n'
        f'{r5} Honeypot:   {hp}\n'
        f'{r5} MINT:         {mint_s}\n'
        f'{r4} Freeze:        {frz_s}\n\n'
        f'{em("market", "⚡️", premium)} <b>MARKET STATS</b>\n\n'
        f'{r1} Txns (24h)  {A} {fmt_num(t.get("txns_buys", 0))}{BUY} |  {fmt_num(t.get("txns_sells", 0))}{SELL}\n'
        f'{r2} Volume       {A} {fmt_usd(t.get("volume_h24"))} (24h)\n'
        f'{r3} Liquidity      {A} {fmt_usd(t.get("liquidity_usd"))}\n\n'
        f'{em("holders", "🏆", premium)} <b>HOLDER ANALYSIS</b>\n\n'
        f'{r4} Traders   {A} {holders_s}\n'
        f'{r5} New 24h {A} {html.escape(str(t.get("new24") or "N/A"))}\n'
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

    if t["chain_id"] != "solana" and (sec.get("buy_tax") is None or sec.get("sell_tax") is None):
        hp_is = await asyncio.to_thread(fetch_honeypot_is, token, t["chain_id"])
        for k, v in hp_is.items():
            if sec.get(k) is None:
                sec[k] = v
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

    try:
        t["new24"] = new_holders_24h(t)
    except Exception as e:
        logger.debug(f"new24 failed: {e}")
        t["new24"] = None

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


def token_buttons(t: dict) -> InlineKeyboardMarkup:
    """Refresh | Trade | Dev - all on ONE row."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data=refresh_callback_data(t)),
        InlineKeyboardButton("Trade", url=maestro_url(t)),
        InlineKeyboardButton("Dev", url=DEV_LINK),
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
            logger.info(f"refresh edit failed (premium={premium}): {e}")
        except Exception as e:
            logger.info(f"refresh edit failed (premium={premium}): {e}")


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
        "/scan &lt;address or link&gt; - scan a token\n\n"
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
PUBLIC_COMMANDS = [
    BotCommand("start", "About this bot"),
    BotCommand("help", "List of commands"),
    BotCommand("scan", "Scan a token by address or link"),
]
OWNER_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("ownerhelp", "Owner control panel"),
    BotCommand("broadcast", "Message your users"),
    BotCommand("admingroups", "Groups/channels where the bot is admin"),
    BotCommand("cancel", "Cancel the current action"),
]


async def post_init(application):
    """Registers the command menu that appears when someone types '/'."""
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

    # Owner commands
    app.add_handler(CommandHandler("ownerhelp", cmd_ownerhelp, filters=private))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=private))
    app.add_handler(CommandHandler("admingroups", cmd_admin_groups, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CallbackQueryHandler(cb_ownerhelp, pattern=r"^oh:"))
    app.add_handler(CallbackQueryHandler(cb_refresh, pattern=r"^rf:"))

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
