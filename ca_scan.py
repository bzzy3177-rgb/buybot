"""ca_scan.py - CA / chart-link scan: address detection, data sources, token snapshot,
Refresh button, call recording and the multiplier (2X/3X...) alert watcher."""

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

from core import (
    AUDIT_BUTTON_TEXT,
    AUDIT_LINK_TEMPLATE,
    COINGECKO_PRO_API_KEY,
    DATA_DIR,
    DEV_LINK,
    MAESTRO_LINK,
    MAESTRO_TOKEN_LINK,
    MOBULA_API_KEY,
    SOLANA_RPC_URL,
    _AUDIT_CHAINS,
    em,
    load_json,
    logger,
    save_json,
)


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



# ===== PNL-CARD START =====
# /pnl command removed - only the call recording stays (multiplier alerts use it).
CALLS_FILE = DATA_DIR / "calls.json"


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
