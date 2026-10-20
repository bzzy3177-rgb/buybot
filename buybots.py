"""buybots.py - live buy-bot: alert formatting, settings, setup wizard, /buybot,
info pages and the pool-trades watcher."""

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
    COINGECKO_PRO_API_KEY,
    DATA_DIR,
    DEV_LINK,
    EMOJI_IDS,
    OWNER_IDS,
    add_user,
    bot_is_admin_here,
    em,
    load_json,
    logger,
    save_json,
)
from ca_scan import (
    _GT_NETWORKS,
    _dex_batch_mcaps,
    _f,
    _gt_get,
    _http,
    _to_ts,
    chain_label,
    extract_ca_from_text,
    fetch_dex_data,
    fetch_gt_fallback,
    maestro_url,
)


# ===== BUYBOT START =====
# Live buy alerts for a token in a group. Install: add the bot as admin, then /buybot <CA> in the group.
# Buys come from GeckoTerminal's pool trades endpoint (free, no key needed).
BUYBOT_FILE = DATA_DIR / "buybot.json"
# GeckoTerminal calls per minute the buy watcher may use (free plan ~30/min total, shared with lookups).
BB_POLL_PER_MIN = int(os.environ.get("BUYBOT_POLL_PER_MIN", "300" if COINGECKO_PRO_API_KEY else "12") or 12)
BB_CALLS_LINK = "https://t.me/WIZARD_SCAN_BOT"  # "Calls" button target (hardcoded on purpose)
BB_CYCLE_PAUSE = 8
BB_MIN_CHOICES = [0, 10, 50, 100]
BB_TOP_COUNT = 8      # 🔮 in the top row
BB_COIN_COUNT = 33    # 🪙 in the coin row
ANON_ADMIN_BOT_ID = 1087968824  # Telegram's "GroupAnonymousBot"

_BB_CHAIN_TAG = {"bsc": "BNB"}
_BB_NATIVE = {"bsc": "WBNB", "ethereum": "WETH", "base": "WETH", "arbitrum": "WETH", "optimism": "WETH",
              "polygon": "WPOL", "avalanche": "WAVAX", "solana": "SOL", "ton": "TON", "tron": "TRX", "sui": "SUI"}
# chain -> (buyer/address link, transaction link)
_BB_EXPLORERS = {
    "bsc": ("https://bscscan.com/address/{}", "https://bscscan.com/tx/{}"),
    "ethereum": ("https://etherscan.io/address/{}", "https://etherscan.io/tx/{}"),
    "base": ("https://basescan.org/address/{}", "https://basescan.org/tx/{}"),
    "arbitrum": ("https://arbiscan.io/address/{}", "https://arbiscan.io/tx/{}"),
    "polygon": ("https://polygonscan.com/address/{}", "https://polygonscan.com/tx/{}"),
    "optimism": ("https://optimistic.etherscan.io/address/{}", "https://optimistic.etherscan.io/tx/{}"),
    "avalanche": ("https://snowtrace.io/address/{}", "https://snowtrace.io/tx/{}"),
    "linea": ("https://lineascan.build/address/{}", "https://lineascan.build/tx/{}"),
    "solana": ("https://solscan.io/account/{}", "https://solscan.io/tx/{}"),
    "ton": ("https://tonviewer.com/{}", "https://tonviewer.com/transaction/{}"),
    "tron": ("https://tronscan.org/#/address/{}", "https://tronscan.org/#/transaction/{}"),
    "sui": ("https://suivision.xyz/account/{}", "https://suivision.xyz/txblock/{}"),
}


# --- formatting ---------------------------------------------------------------------
def _bb_short_usd(v) -> str:
    if not v:
        return "N/A"
    if v >= 1e9:
        s, suf = v / 1e9, "B"
    elif v >= 1e6:
        s, suf = v / 1e6, "M"
    elif v >= 1e3:
        s, suf = v / 1e3, "K"
    else:
        return f"${v:,.0f}"
    num = f"{s:.2f}".rstrip("0").rstrip(".")
    return f"${num}{suf}"


def _bb_amt(v) -> str:
    s = f"{(v or 0):.5f}".rstrip("0").rstrip(".")
    return s or "0"


def _bb_tokens(v) -> str:
    v = v or 0
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.6f}".rstrip("0").rstrip(".") or "0"


def bb_banner(premium: bool = True) -> str:
    """The 8 banner emojis, glued together with no space."""
    return "".join(em(f"bb_top{i}", "🔮", premium) for i in range(1, 9))


def bbx(text: str, premium: bool = True) -> str:
    """Replace the @@BANNER@@ placeholder with the banner emojis."""
    return text.replace("@@BANNER@@", bb_banner(premium))


_PREMIUM_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')


def _strip_premium(text: str) -> str:
    """Premium tg-emoji tags -> their plain fallback char (when custom emojis aren't allowed)."""
    return _PREMIUM_RE.sub(r"\1", text)



async def _bb_text(send, text: str, **kw):
    """send = msg.reply_text / status.edit_text. Premium banner first, plain emojis if Telegram refuses."""
    for premium in (True, False):
        try:
            t = bbx(text, premium)
            if not premium:
                t = _strip_premium(t)
            return await send(t, **kw)
        except BadRequest as ex:
            logger.warning(f"buybot text send failed (premium={premium}): {ex}")
            if not premium:
                raise

# Buy tiers for media: normal (<$1,000), big ($1,000-$4,999), whale ($5,000+)
BB_BIG_MIN = 1000
BB_WHALE_MIN = 5000


def _coin_row_html(cfg: dict, premium: bool, budget: int = None) -> str:
    """Admin-set emoji unit (1-3 emojis) repeated across the whole row - classic buy-bot style.
    1 emoji -> x33, 2 -> x17, 3 -> x11. Skip/not set -> the default coin row.
    `budget` = max characters (used to shrink the row so a media caption fits)."""
    unit = cfg.get("coin_row_unit") or ""
    if not unit:
        return em("bb_coin", "🪙", premium) * BB_COIN_COUNT
    n = max(1, int(cfg.get("coin_row_n") or 1))
    repeats = -(-BB_COIN_COUNT // n)                 # ceil(33 / n)
    if budget is not None:
        repeats = max(1, min(repeats, budget // max(1, len(unit))))
    row = unit * repeats
    return row if premium else _strip_premium(row)


def _bb_pick_media(cfg: dict, usd: float) -> dict:
    """Media for this buy size: whale tier first, then big, then normal (with fallbacks)."""
    if usd >= BB_WHALE_MIN and cfg.get("media_whale"):
        return cfg["media_whale"]
    if usd >= BB_BIG_MIN and cfg.get("media_big"):
        return cfg["media_big"]
    return cfg.get("media_normal") or cfg.get("media_big") or cfg.get("media_whale") or {}


def bb_message(cfg: dict, buy: dict, mcap, premium: bool = True, coin_budget: int = None) -> str:
    def e(key, fb):
        return em(key, fb, premium)

    chain = cfg.get("chain_id") or ""
    name = html.escape((cfg.get("name") or cfg.get("symbol") or "TOKEN").upper())
    tag = html.escape(_BB_CHAIN_TAG.get(chain, chain_label(chain)))
    sym = html.escape(cfg.get("symbol") or "TOKEN")
    quote = html.escape(cfg.get("quote") or _BB_NATIVE.get(chain, "?"))
    ex = _BB_EXPLORERS.get(chain)
    txn = "Txn"
    if ex and buy.get("tx"):
        txn = f'<a href="{html.escape(ex[1].format(buy["tx"]), quote=True)}">Txn</a>'

    # title: premium emoji + NAME (CHAIN) in bold; clickable when a project link was set
    title_inner = f'{e("bb_name", "🔮")} <b>{name} ({tag})</b>'
    link = cfg.get("title_link") or ""
    if link.startswith(("http://", "https://")):
        title_line = f'<a href="{html.escape(link, quote=True)}">{title_inner}</a>'
    else:
        title_line = title_inner

    coin_row = _coin_row_html(cfg, premium, coin_budget)

    return "\n".join([
        bb_banner(premium),
        "",
        title_line,
        "",
        coin_row,
        "",
        f'{e("bb_mc", "🔮")}| <b>Market Cap:</b> {_bb_short_usd(mcap)}',
        "",
        f'{e("bb_pay", "🔮")}| <b>{quote}:</b> {_bb_amt(buy.get("spent"))} (${(buy.get("usd") or 0):,.2f})',
        f'{e("bb_buyer", "🔮")}| <b>Buyer</b> | {txn}',
        f'{e("bb_got", "🔮")}| <b>Got:</b> {_bb_tokens(buy.get("got"))} {sym}',
    ])


def _bb_btn(label: str, key: str, url: str, premium: bool) -> InlineKeyboardButton:
    """Button with a premium emoji icon (plain 🔮 in the text if premium is off / ID missing)."""
    eid = EMOJI_IDS.get(key)
    if premium and eid:
        return InlineKeyboardButton(label, url=url, api_kwargs={"icon_custom_emoji_id": eid})
    return InlineKeyboardButton(f"🔮 {label}", url=url)


def bb_buttons(cfg: dict, premium: bool = True) -> InlineKeyboardMarkup:
    row = []
    if cfg.get("chart_url"):
        row.append(_bb_btn("Chart", "bb_btn_chart", cfg["chart_url"], premium))
    row.append(_bb_btn("Trade", "bb_btn_trade", maestro_url({"token_addr": cfg.get("addr")}), premium))
    row.append(_bb_btn("Calls", "bb_btn_calls", BB_CALLS_LINK, premium))
    return InlineKeyboardMarkup([row])


async def _bb_send_media(bot, media: dict, **kw):
    t, fid = media.get("type"), media.get("file_id")
    if t == "animation":
        await bot.send_animation(animation=fid, **kw)
    elif t == "video":
        await bot.send_video(video=fid, **kw)
    else:
        await bot.send_photo(photo=fid, **kw)


async def _bb_send(bot, chat_id, cfg: dict, buy: dict, mcap) -> bool:
    """Premium emojis first; plain only if Telegram refuses. The alert text and the media always go
    out TOGETHER - if the caption would exceed Telegram's limit, the coin row shrinks so it fits
    (that is what keeps the locked title emoji premium even with a GIF attached)."""
    usd = buy.get("usd") or 0
    media = _bb_pick_media(cfg, usd)
    for premium in (True, False):
        kb = bb_buttons(cfg, premium)
        text = bb_message(cfg, buy, mcap, premium)
        if media.get("file_id") and len(text) > 1000:
            row = _coin_row_html(cfg, premium)
            budget = max(50, 1000 - (len(text) - len(row)))
            text = bb_message(cfg, buy, mcap, premium, coin_budget=budget)
        for _ in range(2):
            try:
                if media.get("file_id"):
                    await _bb_send_media(bot, media, chat_id=chat_id, caption=text,
                                         parse_mode="HTML", reply_markup=kb)
                else:
                    await bot.send_message(chat_id, text, parse_mode="HTML",
                                           disable_web_page_preview=True, reply_markup=kb)
                return True
            except RetryAfter as ex:
                await asyncio.sleep(ex.retry_after + 1)
            except Forbidden:
                raise
            except Exception as ex:
                logger.warning(f"buybot send failed in {chat_id} (premium={premium}): {ex}")
                break
    return False


# --- settings text / keyboards ---------------------------------------------------------------
def _bb_media_label(media) -> str:
    if not isinstance(media, dict):
        return "not set"
    return {"animation": "🎬 GIF", "video": "🎥 Video", "photo": "🖼 Photo"}.get(media.get("type"), "not set")


def _bb_settings_text(cfg: dict, header: str = "@@BANNER@@\n\n🔮 <b>Buy Bot</b>") -> str:
    if cfg.get("setup_pending"):
        state = "⚙️ Setup pending - finish in private chat"
    else:
        state = "⏸ Paused" if cfg.get("paused") else "🟢 Active"
    mn = cfg.get("min_usd") or 0
    link = cfg.get("title_link") or ""
    link_s = (f'<a href="{html.escape(link, quote=True)}">{html.escape(link, quote=True)}</a>'
              if link.startswith(("http://", "https://")) else "not set")
    coin_row = cfg.get("coin_row_unit") or ""
    coin_s = coin_row if coin_row else f"default ({BB_COIN_COUNT} coins)"
    return (
        f"{header} · {state}\n\n"
        f"🪙 Token: <b>{html.escape(cfg.get('name') or cfg.get('symbol') or '?')}</b> "
        f"(${html.escape(cfg.get('symbol') or '?')}) · {html.escape(chain_label(cfg.get('chain_id') or ''))}\n"
        f"💵 Min buy: <b>{'any amount' if not mn else '$' + format(mn, 'g')}</b>\n"
        f"🔗 Title link: {link_s}\n"
        f"🎨 Coin row: {coin_s}\n"
        f"🖼 Media — Normal: {_bb_media_label(cfg.get('media_normal'))}\n"
        f"💰 Media — Big ($1k+): {_bb_media_label(cfg.get('media_big'))}\n"
        f"🐳 Media — Whale ($5k+): {_bb_media_label(cfg.get('media_whale'))}\n\n"
        f"<code>{html.escape(cfg.get('addr') or '')}</code>\n\n"
        "Every new buy is posted here automatically. Use the buttons below to adjust.\n\nℹ️ Link / emojis / media are set in your private chat with the bot (tap the buttons)."
    )


def _bb_settings_kb(cfg: dict) -> InlineKeyboardMarkup:
    cur = cfg.get("min_usd") or 0
    mins = [InlineKeyboardButton(("✅ " if cur == n else "") + ("Any" if n == 0 else f"${n}"),
                                 callback_data=f"bb:min:{n}") for n in BB_MIN_CHOICES]
    paused = cfg.get("paused")
    return InlineKeyboardMarkup([
        mins,
        [InlineKeyboardButton("▶️ Resume" if paused else "⏸ Pause", callback_data="bb:resume" if paused else "bb:pause"),
         InlineKeyboardButton("🧪 Preview", callback_data="bb:preview")],
        [InlineKeyboardButton("🔗 Title Link", callback_data="bb:tlink"),
         InlineKeyboardButton("🎨 Emojis", callback_data="bb:emoj"),
         InlineKeyboardButton("🖼 Media", callback_data="bb:media")],
        [InlineKeyboardButton("🗑 Remove", callback_data="bb:rm")],
    ])


_BB_SAMPLE_BUY = {"spent": 0.05084, "usd": 99.49, "got": 421.16}


async def _bb_send_preview(bot, chat_id, cfg: dict):
    await _bb_send(bot, chat_id, cfg, _BB_SAMPLE_BUY, cfg.get("mcap"))


# --- who may manage it ---------------------------------------------------------------------------
async def _is_group_admin(context, chat, user, msg=None) -> bool:
    if user and user.id in OWNER_IDS:
        return True
    if msg is not None and getattr(msg, "sender_chat", None) and msg.sender_chat.id == chat.id:
        return True  # anonymous admin
    if not user or user.id == ANON_ADMIN_BOT_ID:
        return False
    try:
        m = await context.bot.get_chat_member(chat.id, user.id)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


# --- setup wizard: runs in the ADMIN'S private chat --------------------------------------------
# Group install -> the bot DMs the admin -> 5 steps with Back/Skip buttons -> summary -> Done goes live.
_bb_setup: dict = {}   # user_id -> {"stage", "t", "prompt", "chat_id" (target group)}
SETUP_TIMEOUT = 900

_BB_SETUP_STAGES = ["ca", "link", "emoji", "media_normal", "media_big", "media_whale", "ca_scan"]

# Custom (premium) number emojis 1-9. _num(n) gives the premium tag; the plain keycap is the fallback.
NUM_EMOJI = {
    1: "5807615306901759629", 2: "5807631344309642763", 3: "5807396667296588812",
    4: "5807712914328526729", 5: "5807614774325813553", 6: "5807561465191737490",
    7: "5807686972726058383", 8: "5807454391657045517", 9: "5807632997872049875",
}


def _num(n: int) -> str:
    return f'<tg-emoji emoji-id="{NUM_EMOJI[n]}">{n}\ufe0f\u20e3</tg-emoji>'


_BB_SETUP_PROMPTS = {
    "ca": (f"{_num(1)} <b>Step 1/6 - Your token</b>\n\n"
           "Send the contract address (or a chart link) of the token you want the buy bot for.\n\n"
           "ℹ️ This replaces the current buy bot of your group."),
    "link": (f"{_num(2)} <b>Step 2/6 - Project link</b>\n\n"
             "Send any link - Telegram group, website, X. The token name on every alert "
             "will open this link when tapped."),
    "emoji": (f"{_num(3)} <b>Step 3/6 - Coin-row emojis</b>\n\n"
              "Send <b>1-3 emojis</b> (normal or premium). If you send just 1, it will be repeated "
              "across the whole row - exactly like the classic buy bots.\n\n"
              "Skip = the bot uses its own default row."),
    "media_normal": (f"{_num(4)} <b>Step 4/6 - Alert media</b>\n\n"
                     "You can set <b>3 different medias</b> for your alerts:\n"
                     f"{_num(1)} Normal buys - every buy under $1,000\n"
                     f"{_num(2)} Big buys - $1,000 to $4,999\n"
                     f"{_num(3)} Whale buys - $5,000 and above\n\n"
                     "One media for all three is fine too. Send a GIF, photo or video for "
                     "<b>NORMAL BUYS</b> now."),
    "media_big": (f"{_num(5)} <b>Step 5/6 - Media for BIG BUYS ($1,000 - $4,999)</b>\n\n"
                  "Send a GIF, photo or video - or tap Same as Normal to reuse the previous media."),
    "media_whale": (f"{_num(6)} <b>Step 6/6 - Media for WHALE BUYS ($5,000+)</b>\n\n"
                    "Send a GIF, photo or video - or tap Same as Normal to reuse the previous media."),
    "ca_scan": ("🔍 <b>One last question - token scans</b>\n\n"
                "When someone pastes a token CA in your group, should I also post the full "
                "token details there?"),
}


def _bb_setup_kb(stage: str) -> InlineKeyboardMarkup:
    cancel = [InlineKeyboardButton("❌ Cancel", callback_data="bbw:cancel")]
    if stage == "ca":
        return InlineKeyboardMarkup([cancel])
    if stage == "link":
        return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip", callback_data="bbw:skip")], cancel])
    if stage == "ca_scan":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data="bbw:yes"),
            InlineKeyboardButton("❌ No", callback_data="bbw:no")],
            [InlineKeyboardButton("⬅️ Back", callback_data="bbw:back")], cancel])
    if stage in ("media_big", "media_whale"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="bbw:back"),
             InlineKeyboardButton("⏭ Skip", callback_data="bbw:skip")],
            [InlineKeyboardButton("🔁 Same as Normal", callback_data="bbw:copy")], cancel])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="bbw:back"),
        InlineKeyboardButton("⏭ Skip", callback_data="bbw:skip")], cancel])


def _bb_unit_count(unit: str) -> int:
    plain = _PREMIUM_RE.sub("", unit)
    return len(_PREMIUM_RE.findall(unit)) + sum(1 for ch in plain if _is_emoji_char(ch))


def _bb_load_cfgs() -> dict:
    """Buy-bot configs, with migration of pre-tier keys (media -> media_normal, coin_row -> unit)."""
    cfgs = load_json(BUYBOT_FILE, {})
    for c in cfgs.values():
        if not isinstance(c, dict):
            continue
        if c.get("media") and not c.get("media_normal"):
            c["media_normal"] = c["media"]
        if c.get("coin_row") and not c.get("coin_row_unit"):
            c["coin_row_unit"] = c.pop("coin_row")
            c["coin_row_n"] = max(1, _bb_unit_count(c["coin_row_unit"]))
    return cfgs


async def _bb_setup_render(bot, uid: int, stage: str, gid: int, old_prompt_id=None):
    """Ask the given step in the admin's DM: delete the previous prompt, send the new one."""
    if old_prompt_id:
        try:
            await bot.delete_message(uid, old_prompt_id)
        except Exception:
            pass
    m = None
    for premium in (True, False):
        text = _BB_SETUP_PROMPTS[stage] if premium else _strip_premium(_BB_SETUP_PROMPTS[stage])
        try:
            m = await bot.send_message(uid, text, parse_mode="HTML",
                                       reply_markup=_bb_setup_kb(stage), disable_web_page_preview=True)
            break
        except BadRequest as e:
            logger.warning(f"setup prompt '{stage}' failed (premium={premium}): {e}")
            if not premium:
                raise
    _bb_setup[uid] = {"stage": stage, "t": time.time(), "prompt": m.message_id, "chat_id": gid}


async def _bb_setup_next(bot, uid: int, gid: int, stage: str, old_prompt_id=None, note: str = None):
    idx = _BB_SETUP_STAGES.index(stage)
    if idx + 1 < len(_BB_SETUP_STAGES):
        await _bb_setup_render(bot, uid, _BB_SETUP_STAGES[idx + 1], gid, old_prompt_id)
        return
    await _bb_setup_finish(bot, uid, gid, old_prompt_id, note)


def _bb_summary_text(cfg: dict) -> str:
    link = cfg.get("title_link") or ""
    link_s = (f'<a href="{html.escape(link, quote=True)}">{html.escape(link, quote=True)}</a>'
              if link.startswith(("http://", "https://")) else "not set")
    coin = cfg.get("coin_row_unit") or f"default ({BB_COIN_COUNT} coins)"
    return (
        "📋 <b>Setup summary</b>\n\n"
        f"🪙 Token: <b>{html.escape(cfg.get('name') or cfg.get('symbol') or '?')}</b> "
        f"(${html.escape(cfg.get('symbol') or '?')}) · {html.escape(chain_label(cfg.get('chain_id') or ''))}\n"
        f"🔗 Title link: {link_s}\n"
        f"🎨 Coin row: {coin}\n"
        f"🖼 Media — Normal: {_bb_media_label(cfg.get('media_normal'))} · "
        f"Big: {_bb_media_label(cfg.get('media_big'))} · Whale: {_bb_media_label(cfg.get('media_whale'))}\n"
        f"🔍 CA scans in group: {'✅ Yes' if cfg.get('scan_ca') else '❌ No'}\n\n"
        "Check everything - send a preview if you like - then tap <b>Done</b> and the buy bot goes live."
    )


async def _bb_setup_finish(bot, uid: int, gid: int, old_prompt_id=None, note: str = None):
    if old_prompt_id:
        try:
            await bot.delete_message(uid, old_prompt_id)
        except Exception:
            pass
    cfg = _bb_load_cfgs().get(str(gid)) or {}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Preview", callback_data="bbw:preview")],
        [InlineKeyboardButton("✅ Done — Start Buy Bot", callback_data="bbw:done")],
        [InlineKeyboardButton("❌ Cancel", callback_data="bbw:cancel")]])
    send = functools.partial(bot.send_message, uid)
    await _bb_text(send, _bb_summary_text(cfg), parse_mode="HTML", reply_markup=kb,
                   disable_web_page_preview=True)
    if note:
        await bot.send_message(uid, f"ℹ️ {html.escape(note)}", parse_mode="HTML")
    _bb_setup[uid] = {"stage": "summary", "t": time.time(), "prompt": None, "chat_id": gid}


def _is_emoji_char(ch: str) -> bool:
    o = ord(ch)
    return (0x1F000 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or (0x2190 <= o <= 0x21FF) \
        or o in (0x2B50, 0x2B55, 0x3030, 0xFE0F, 0x200D)


def _extract_emojis(msg) -> list:
    """Message emojis as HTML fragments: premium custom emojis -> <tg-emoji ...>, normal -> the char."""
    text = msg.text or msg.caption or ""
    entities = msg.entities or msg.caption_entities or []
    out, pos = [], 0
    for ent in entities:
        if ent.type == MessageEntity.CUSTOM_EMOJI and getattr(ent, "custom_emoji_id", None):
            if ent.offset > pos:
                out.extend(ch for ch in text[pos:ent.offset] if _is_emoji_char(ch))
            fallback = text[ent.offset:ent.offset + ent.length] or "⭐️"
            out.append(f'<tg-emoji emoji-id="{ent.custom_emoji_id}">{html.escape(fallback)}</tg-emoji>')
            pos = ent.offset + ent.length
    out.extend(ch for ch in text[pos:] if _is_emoji_char(ch))
    return out


async def handle_bb_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """One wizard step answered with a private message. True = consumed."""
    msg = update.effective_message
    user = update.effective_user
    st = _bb_setup.get(user.id)
    if not st:
        return False
    if time.time() - st.get("t", 0) > SETUP_TIMEOUT:
        _bb_setup.pop(user.id, None)
        return False
    gid = st.get("chat_id")
    cid = str(gid)
    text = ((msg.text or "").strip() or (msg.caption or "").strip())
    if text.startswith("/"):            # any command cancels the wizard
        _bb_setup.pop(user.id, None)
        return False

    stage = st["stage"]
    if stage == "ca":
        found = extract_ca_from_text(text)
        if not found:
            await msg.reply_text("⚠️ That doesn't look like a contract address or chart link - try again.")
            return True
        status_msg = await context.bot.send_message(user.id, "🔎 Looking up your token…")
        try:
            t, err = await asyncio.to_thread(_bb_lookup, found[0], found[1])
        except Exception as e:
            logger.warning(f"buybot lookup failed: {e}")
            t, err = None, "not_found"
        try:
            await status_msg.delete()
        except Exception:
            pass
        if not t:
            text_err = ("⚠️ I couldn't find a trading pool for this token. Check the address and try again."
                        if err == "not_found" else "⚠️ This chain isn't supported by the buy bot yet.")
            await msg.reply_text(text_err)
            return True
        cfgs = _bb_load_cfgs()
        cfg = {
            "addr": t["token_addr"], "chain_id": t["chain_id"], "pair": t["pair_addr"],
            "symbol": t["symbol"], "name": t["name"] or t["symbol"], "quote": t.get("quote"),
            "chart_url": t.get("chart_url"), "mcap": t.get("mcap"),
            "min_usd": 0, "paused": True, "setup_pending": True, "since": time.time(), "seen": [],
            "by": user.id, "title": st.get("gtitle") or "",
        }
        cfgs[cid] = cfg
        save_json(BUYBOT_FILE, cfgs)
        try:
            await msg.delete()
        except Exception:
            pass
        await context.bot.send_message(
            user.id,
            f'✅ <b>{html.escape(cfg["name"] or cfg["symbol"])} '
            f'({html.escape(chain_label(cfg["chain_id"]))})</b> found!',
            parse_mode="HTML")
        await _bb_setup_render(context.bot, user.id, "link", gid, st.get("prompt"))
        return True

    cfgs = _bb_load_cfgs()
    cfg = cfgs.get(cid)
    if not cfg:
        _bb_setup.pop(user.id, None)
        return False

    note = None
    if stage == "link":
        if re.match(r"^https?://\S+$", text):
            cfg["title_link"] = text
        else:
            await msg.reply_text("⚠️ Send a valid link starting with http:// or https://.")
            return True
    elif stage == "emoji":
        all_emoji = _extract_emojis(msg)
        emojis = all_emoji[:3]
        if not emojis:
            await msg.reply_text("⚠️ Send 1-3 emojis (normal or premium).")
            return True
        if len(all_emoji) > 3:
            note = "Maximum is 3 emojis - saved the first 3."
        cfg["coin_row_unit"] = "".join(emojis)
        cfg["coin_row_n"] = len(emojis)
    elif stage in ("media_normal", "media_big", "media_whale"):
        media = None
        if msg.animation:
            media = {"type": "animation", "file_id": msg.animation.file_id}
        elif msg.video:
            media = {"type": "video", "file_id": msg.video.file_id}
        elif msg.photo:
            media = {"type": "photo", "file_id": msg.photo[-1].file_id}
        if not media:
            await msg.reply_text("⚠️ Send a GIF, photo or video.")
            return True
        cfg[stage] = media
    elif stage == "ca_scan":
        low = text.lower()
        if low in ("yes", "y"):
            cfg["scan_ca"] = True
        elif low in ("no", "n"):
            cfg["scan_ca"] = False
        else:
            await msg.reply_text("⚠️ Tap Yes or No.")
            return True
    else:
        _bb_setup.pop(user.id, None)
        return False

    cfgs[cid] = cfg
    save_json(BUYBOT_FILE, cfgs)
    try:
        await msg.delete()          # the admin's own answer disappears too - clean setup chat
    except Exception:
        pass
    await _bb_setup_next(context.bot, user.id, gid, stage, st.get("prompt"), note=note)
    return True


async def cb_bb_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Back / Skip / Same-as-Normal / Yes / No / Preview / Done buttons of the setup wizard."""
    q = update.callback_query
    user = q.from_user
    st = _bb_setup.get(user.id)
    if not st or time.time() - st.get("t", 0) > SETUP_TIMEOUT:
        _bb_setup.pop(user.id, None)
        await q.answer("This setup has expired - open it again from your group.", show_alert=True)
        return
    gid = st.get("chat_id")
    cid = str(gid)
    bot = context.bot
    act = (q.data or "")[4:]
    stage = st["stage"]

    if act == "cancel":
        _bb_setup.pop(user.id, None)
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass
        await bot.send_message(user.id, "❌ Setup cancelled. Send /buybot in your group anytime to start again.")
        return

    cfgs = _bb_load_cfgs()
    cfg = cfgs.get(cid)
    if not cfg:
        _bb_setup.pop(user.id, None)
        await q.answer()
        return

    if act == "back":
        if stage == "ca":
            await q.answer("This is the first step.")
            return
        if stage == "summary":
            await q.answer()
            await _bb_setup_render(bot, user.id, "ca_scan", gid, q.message.message_id)
            return
        prev = _BB_SETUP_STAGES[_BB_SETUP_STAGES.index(stage) - 1]
        await q.answer()
        await _bb_setup_render(bot, user.id, prev, gid, q.message.message_id)
        return

    if act == "skip":
        if stage == "link":
            cfg.pop("title_link", None)
        elif stage == "emoji":
            cfg.pop("coin_row_unit", None)
            cfg.pop("coin_row_n", None)
        elif stage in ("media_normal", "media_big", "media_whale"):
            cfg.pop(stage, None)
        elif stage == "ca_scan":
            cfg["scan_ca"] = False
        else:
            await q.answer()
            return
        cfgs[cid] = cfg
        save_json(BUYBOT_FILE, cfgs)
        await q.answer()
        await _bb_setup_next(bot, user.id, gid, stage, q.message.message_id)
        return

    if act == "copy":
        if stage not in ("media_big", "media_whale"):
            await q.answer()
            return
        if not cfg.get("media_normal"):
            await q.answer("Set the Normal-buy media first.", show_alert=True)
            return
        cfg[stage] = cfg["media_normal"]
        cfgs[cid] = cfg
        save_json(BUYBOT_FILE, cfgs)
        await q.answer("Same media applied ✅")
        await _bb_setup_next(bot, user.id, gid, stage, q.message.message_id)
        return

    if act in ("yes", "no"):
        if stage != "ca_scan":
            await q.answer()
            return
        cfg["scan_ca"] = (act == "yes")
        cfgs[cid] = cfg
        save_json(BUYBOT_FILE, cfgs)
        await q.answer()
        await _bb_setup_next(bot, user.id, gid, stage, q.message.message_id)
        return

    if act == "preview":
        await q.answer("Preview sent to your group ✅")
        try:
            await _bb_send_preview(bot, gid, cfg)
        except Exception as e:
            logger.warning(f"wizard preview failed: {e}")
        return

    if act == "done":
        try:
            m = await bot.get_chat_member(gid, user.id)
            if m.status not in ("creator", "administrator"):
                raise ValueError("not admin")
        except Exception:
            await q.answer("You are not an admin in that group anymore.", show_alert=True)
            return
        cfg["paused"] = False
        cfg.pop("setup_pending", None)
        cfgs[cid] = cfg
        save_json(BUYBOT_FILE, cfgs)
        _bb_setup.pop(user.id, None)
        try:
            await q.message.delete()
        except Exception:
            pass
        await bot.send_message(user.id, "🚀 <b>Your buy bot is LIVE!</b> Every new buy in your "
                               "group will now get an alert.", parse_mode="HTML")
        try:
            await bot.send_message(gid, "🚀 <b>Buy bot is now LIVE</b> for "
                                   f"<b>{html.escape(cfg.get('name') or cfg.get('symbol') or 'your token')}</b>!",
                                   parse_mode="HTML")
        except Exception as e:
            logger.debug(f"live announcement failed: {e}")
        return

    await q.answer()


# --- install --------------------------------------------------------------------------------------
def _bb_quote_symbol(chain_id: str, pair: str):
    net = _GT_NETWORKS.get(chain_id, chain_id)
    d = _gt_get(f"/networks/{net}/pools/{pair}")
    name = (((d or {}).get("data") or {}).get("attributes") or {}).get("name") or ""
    if "/" in name:
        parts = name.split("/", 1)[1].strip().split()
        if parts:
            return parts[0]
    return None


def _bb_lookup(addr: str, hint):
    t = fetch_dex_data(addr, hint) or fetch_gt_fallback(addr, hint)
    if not t or not t.get("pair_addr"):
        return None, "not_found"
    if t["chain_id"] not in _GT_NETWORKS or t["chain_id"] == "robinhood":
        return None, "chain"
    t["quote"] = _bb_quote_symbol(t["chain_id"], t["pair_addr"])
    return t, None



_BB_DM_SETUP = (
    "🔮 <b>Set up your buy bot</b>\n\n"
    "The whole setup runs privately with the bot - your group stays clean.\n\n"
    'Tap the button below " Set Up in Private Chat " and enter your token details one by one.'
)
BB_GROUP_MEDIA_KEY = "buybot_group"   # /ownerhelp -> Set Media key for this group message


async def _bb_reply_with_media(msg, text: str, kb, media):
    """Reply in the group with the owner-set photo/video/GIF (caption = text), or plain text."""
    if media:
        for premium in (True, False):
            t = bbx(text, premium)
            if not premium:
                t = _strip_premium(t)
            try:
                send = getattr(msg, f"reply_{media['type']}")
                visible = len(html.unescape(re.sub(r"<[^>]+>", "", t)))
                if visible <= 1024:
                    await send(media["file_id"], caption=t, parse_mode="HTML", reply_markup=kb)
                    return
                break   # caption too long: use the text-only reply below
            except Exception as e:
                logger.warning(f"group setup media failed (premium={premium}): {e}")
    await _bb_text(msg.reply_text, text, parse_mode="HTML", reply_markup=kb,
                   disable_web_page_preview=True)


async def cmd_buybot_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, u = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat:
        return
    if not await bot_is_admin_here(context, chat):
        await msg.reply_text("⚠️ Please make me an <b>admin</b> of this group first, then send /buybot again.",
                             parse_mode="HTML")
        return
    if not await _is_group_admin(context, chat, u, msg):
        await msg.reply_text("⛔ Only group admins can manage the buy bot.")
        return
    args = context.args or []
    sub = args[0].lower() if args else ""
    cfgs = _bb_load_cfgs()
    cid = str(chat.id)
    cfg = cfgs.get(cid)

    if sub in ("off", "remove", "stop", "delete"):
        if cfgs.pop(cid, None) is not None:
            save_json(BUYBOT_FILE, cfgs)
            await msg.reply_text("🗑 Buy bot removed from this group.")
        else:
            await msg.reply_text("The buy bot isn't installed here.")
        return

    if sub in ("status", "pause", "resume", "min", "test", "preview", "") and cfg:
        if sub == "pause":
            cfg["paused"] = True
        elif sub == "resume":
            cfg["paused"] = False
        elif sub == "min":
            try:
                cfg["min_usd"] = max(0.0, float(args[1].lstrip("$")))
            except Exception:
                await msg.reply_text("Usage: <code>/buybot min 25</code>", parse_mode="HTML")
                return
        elif sub in ("test", "preview"):
            await _bb_send_preview(context.bot, chat.id, cfg)
            return
        cfgs[cid] = cfg
        save_json(BUYBOT_FILE, cfgs)
        await _bb_text(msg.reply_text, _bb_settings_text(cfg), parse_mode="HTML",
                       reply_markup=_bb_settings_kb(cfg), disable_web_page_preview=True)
        return

    # Everything else (including a pasted CA): the FULL setup runs privately with the bot.
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🤖 Set Up in Private Chat",
        url=f"https://t.me/{context.bot.username}?start=bbnew_{chat.id}")]])
    await _bb_reply_with_media(msg, _BB_DM_SETUP, kb, _bb_page_media(BB_GROUP_MEDIA_KEY))


# --- DM: info pages with buttons ------------------------------------------------------------------------
_BB_PAGES = {
    "p_home": (
        '<tg-emoji emoji-id="6044119257308995249">🔮</tg-emoji> <b><u>How to Install BuyBot</u></b>\n\n'
        "The bot supports all chains and allows you to set up BuyBots for up to 5 tokens in one "
        "group at the same time! Follow the steps below to install your BuyBots.\n\n"
        "1) Add the bot to your group and make it an admin.\n"
        "2) Give it permission to post messages.\n"
        "3) Send /buybot in the group.\n"
        "4) The bot will DM you and ask for your token details.\n\n"
        '<tg-emoji emoji-id="5845744321453631630">🎥</tg-emoji> '
        "Follow the step-by-step video tutorials to set up your BuyBot easily."),
    "p_set": (
        "⚙️ <b>Settings (in your group)</b>\n\n"
        "After installing, the bot posts a settings card with buttons:\n\n"
        "💵 <b>Any / $10 / $50 / $100</b> - only alert buys above this size\n"
        "⏸ <b>Pause</b> / ▶️ <b>Resume</b> - stop or restart alerts\n"
        "🧪 <b>Preview</b> - send a sample alert\n"
        "🗑 <b>Remove</b> - uninstall the buy bot\n\n"
        "Open it again any time with <code>/buybot status</code>."),
    "p_cmds": (
        "💬 <b>Buy bot commands (in your group)</b>\n\n"
        "<code>/buybot</code> - install (setup continues in DM)\n"
        "<code>/buybot status</code> - settings card\n"
        "<code>/buybot min 25</code> - minimum buy in USD\n"
        "<code>/buybot pause</code> / <code>/buybot resume</code>\n"
        "<code>/buybot test</code> - preview alert\n"
        "<code>/buybot off</code> - remove\n\n"
        "👇 Tap a button to copy the command, then paste it in your group."),
}

BB_BTN_ICON_ID = "6044166381690166762"      # icon on every button except Support
BB_SUPPORT_ICON_ID = "6044119257308995249"  # icon on the Support button
BB_PAGE_MEDIA_KEY = "buybot_cmd"            # /ownerhelp -> Set Media key for the /buybot page


def _bb_btn_kw(icons: bool, icon_id: str = BB_BTN_ICON_ID) -> dict:
    return {"api_kwargs": {"icon_custom_emoji_id": icon_id}} if icons else {}


def _bb_home_kb(username: str, home: bool, icons: bool = True) -> InlineKeyboardMarkup:
    if not home:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="bb:p_home")]])
    kw = _bb_btn_kw(icons)
    pre = (lambda e: "") if icons else (lambda e: e + " ")
    rows = []
    if username:
        rows.append([InlineKeyboardButton(
            f"{pre('➕')}Add Bot to Your Group",
            url=f"https://t.me/{username}?startgroup=buybot&admin=post_messages+delete_messages", **kw)])
    rows += [
        [InlineKeyboardButton(f"{pre('⚙️')}Settings", callback_data="bb:p_set", **kw),
         InlineKeyboardButton(f"{pre('💬')}Commands", callback_data="bb:p_cmds", **kw)],
        [InlineKeyboardButton(f"{pre('🆘')}Support", url=DEV_LINK,
                              **_bb_btn_kw(icons, BB_SUPPORT_ICON_ID))],
    ]
    return InlineKeyboardMarkup(rows)


_BB_CMD_BUTTONS = ["/buybot status", "/buybot min 25", "/buybot pause", "/buybot resume",
                   "/buybot test", "/buybot off"]


def _bb_cmds_kb(icons: bool = True) -> InlineKeyboardMarkup:
    """One copy-to-clipboard button per group command (tap -> copied -> paste in the group)."""
    kw = _bb_btn_kw(icons)
    pre = "" if icons else "💬 "
    btns = [InlineKeyboardButton(f"{pre}{c}", **{"api_kwargs": {**kw.get("api_kwargs", {}),
                                                              "copy_text": {"text": c}}})
            for c in _BB_CMD_BUTTONS]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="bb:p_home")])
    return InlineKeyboardMarkup(rows)


def _bb_page_kb(username: str, act: str, icons: bool = True) -> InlineKeyboardMarkup:
    if act == "p_home":
        return _bb_home_kb(username, True, icons)
    if act == "p_cmds":
        return _bb_cmds_kb(icons)
    return _bb_home_kb(username, False)


def _bb_page_media(key: str = BB_PAGE_MEDIA_KEY):
    """Owner-set photo/video/GIF for a page (set via /ownerhelp -> Set Media)."""
    try:
        from commands import load_media
        return load_media().get(key)
    except Exception:
        return None


async def _bb_send_page(bot, chat_id, text: str, username: str, act: str, media=None):
    """Send a page as a NEW message: premium emojis + button icons first, plain fallback after."""
    for premium in (True, False):
        t = bbx(text, premium)
        if not premium:
            t = _strip_premium(t)
        kb = _bb_page_kb(username, act, premium)
        try:
            if media:
                send = getattr(bot, f"send_{media['type']}")
                visible = len(html.unescape(re.sub(r"<[^>]+>", "", t)))
                if visible <= 1024:
                    await send(chat_id, media["file_id"], caption=t, parse_mode="HTML", reply_markup=kb)
                    return
                await send(chat_id, media["file_id"])
            await bot.send_message(chat_id, t, parse_mode="HTML", reply_markup=kb,
                                   disable_web_page_preview=True)
            return
        except BadRequest as e:
            logger.warning(f"buybot page send failed (act={act}, premium={premium}): {e}")
            if not premium:
                raise


async def cmd_buybot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/buybot in DM = install guide + buttons. The real install happens inside a group."""
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    text = _BB_PAGES["p_home"]
    if context.args:
        text = ("ℹ️ Run <code>/buybot</code> <b>inside your group</b> (the bot must be admin there).\n\n"
                + text)
    await _bb_send_page(context.bot, update.effective_chat.id, text, context.bot.username,
                        "p_home", _bb_page_media())


async def _bb_edit(q, text: str, kb, kb_plain=None):
    for premium in (True, False):
        try:
            t = bbx(text, premium)
            if not premium:
                t = _strip_premium(t)
            await q.edit_message_text(t, parse_mode="HTML",
                                      reply_markup=kb if (premium or kb_plain is None) else kb_plain,
                                      disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            logger.warning(f"buybot edit failed (premium={premium}): {e}")


async def cb_buybot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    act = (q.data or "")[3:]
    chat = q.message.chat if q.message else None
    if not chat:
        await q.answer()
        return

    # DM info pages
    if act in _BB_PAGES:
        await q.answer()
        uname = context.bot.username
        media = _bb_page_media() if act == "p_home" else None
        if media or q.message.text is None:
            # a media message can't be edited into a text page (or vice versa): replace it
            try:
                await q.message.delete()
            except Exception:
                pass
            await _bb_send_page(context.bot, chat.id, _BB_PAGES[act], uname, act, media)
        else:
            await _bb_edit(q, _BB_PAGES[act], _bb_page_kb(uname, act, True), _bb_page_kb(uname, act, False))
        return

    # Group settings card
    if chat.type == "private":
        await q.answer()
        return
    if not await _is_group_admin(context, chat, q.from_user):
        await q.answer("Only group admins can use these buttons.", show_alert=True)
        return
    cfgs = _bb_load_cfgs()
    cid = str(chat.id)
    cfg = cfgs.get(cid)
    if not cfg:
        await q.answer("The buy bot isn't installed here. Send /buybot <CA>.", show_alert=True)
        return

    if act in ("tlink", "emoj", "media"):
        stage = {"tlink": "link", "emoj": "emoji", "media": "media_normal"}[act]
        try:
            await _bb_setup_render(context.bot, q.from_user.id, stage, chat.id)
        except Forbidden:
            await q.answer("Please press Start on my profile first, then use this button again.",
                           show_alert=True)
            return
        await q.answer("⚙️ Sent to your private chat ✅")
        return

    if act == "preview":
        await q.answer("Preview sent ✅")
        await _bb_send_preview(context.bot, chat.id, cfg)
        return
    if act == "rm":
        await q.answer()
        await _bb_edit(q, "🗑 <b>Remove the buy bot from this group?</b>\nBuy alerts will stop.",
                       InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, remove", callback_data="bb:rmy"),
                                              InlineKeyboardButton("⬅️ Cancel", callback_data="bb:set")]]))
        return
    if act == "rmy":
        cfgs.pop(cid, None)
        save_json(BUYBOT_FILE, cfgs)
        await q.answer("Removed")
        await _bb_edit(q, "🗑 Buy bot removed. Send /buybot &lt;CA&gt; to set it up again.", None)
        return

    note = ""
    if act.startswith("min:"):
        try:
            cfg["min_usd"] = float(act[4:])
            note = "Minimum buy updated"
        except Exception:
            pass
    elif act == "pause":
        cfg["paused"] = True
        note = "Alerts paused"
    elif act == "resume":
        cfg["paused"] = False
        note = "Alerts resumed"
    cfgs[cid] = cfg
    save_json(BUYBOT_FILE, cfgs)
    await q.answer(note)
    await _bb_edit(q, _bb_settings_text(cfg), _bb_settings_kb(cfg))


# --- the watcher: polls pool trades and posts new buys -------------------------------------------------------
def _bb_fetch_trades(chain_id: str, pair: str):
    """Recent trades of a pool (newest first) or None. Own rate limit - doesn't eat the lookup budget."""
    net = _GT_NETWORKS.get(chain_id, chain_id)
    if COINGECKO_PRO_API_KEY:
        url = f"https://pro-api.coingecko.com/api/v3/onchain/networks/{net}/pools/{pair}/trades"
        headers = {"accept": "application/json", "x-cg-pro-api-key": COINGECKO_PRO_API_KEY}
    else:
        url = f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pair}/trades"
        headers = {"accept": "application/json;version=20230302"}
    try:
        r = _http.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("data") or []
    except Exception as e:
        logger.debug(f"buybot trades fetch failed ({pair}): {e}")
        return None


def _bb_parse_buys(raw) -> list:
    out = []
    for tr in raw or []:
        a = tr.get("attributes") or {}
        if str(a.get("kind")).lower() != "buy":
            continue
        ts = _to_ts(a.get("block_timestamp"))
        if not ts:
            continue
        got = _f(a.get("to_token_amount"))
        out.append({
            "ts": ts, "key": f"{a.get('tx_hash')}:{got}", "tx": a.get("tx_hash"),
            "buyer": a.get("tx_from_address"), "usd": _f(a.get("volume_in_usd")) or 0,
            "spent": _f(a.get("from_token_amount")), "got": got,
        })
    out.sort(key=lambda b: b["ts"])  # oldest first
    return out


async def _bb_cycle(bot):
    cfgs = _bb_load_cfgs()
    active = {cid: c for cid, c in cfgs.items() if not c.get("paused") and c.get("pair")}
    if not active:
        return
    pools = {}
    for cid, c in active.items():
        pools.setdefault((c["chain_id"], c["pair"]), []).append(cid)
    mcaps = await asyncio.to_thread(
        _dex_batch_mcaps, list({(c["chain_id"], c["addr"]) for c in active.values()}))
    gap = 60.0 / max(1, BB_POLL_PER_MIN)
    seen_updates, dead, mc_updates = {}, set(), {}

    for (chain, pair), chats in pools.items():
        raw = await asyncio.to_thread(_bb_fetch_trades, chain, pair)
        await asyncio.sleep(gap)
        buys = _bb_parse_buys(raw)
        if not buys:
            continue
        for cid in chats:
            c = active[cid]
            seen = list(c.get("seen") or [])
            got_mc = mcaps.get((chain.lower(), (c["addr"] or "").lower()))
            mcap = got_mc[0] if got_mc else c.get("mcap")
            if got_mc:
                mc_updates[cid] = got_mc[0]
            for b in buys:
                if b["ts"] < c.get("since", 0) or b["key"] in seen:
                    continue
                seen.append(b["key"])
                if b["usd"] < (c.get("min_usd") or 0):
                    continue
                try:
                    await _bb_send(bot, int(cid), c, b, mcap)
                except Forbidden:
                    dead.add(cid)
                    break
                except Exception as e:
                    logger.warning(f"buybot alert failed in {cid}: {e}")
                await asyncio.sleep(0.4)
            seen_updates[cid] = seen[-100:]

    # Re-read before saving so settings changed during the cycle aren't overwritten
    cur = _bb_load_cfgs()
    for cid, seen in seen_updates.items():
        if cid in cur:
            cur[cid]["seen"] = seen
    for cid, mc in mc_updates.items():
        if cid in cur:
            cur[cid]["mcap"] = mc
    for cid in dead:
        cur.pop(cid, None)
    save_json(BUYBOT_FILE, cur)


async def buybot_watcher(app):
    await asyncio.sleep(15)
    while True:
        try:
            await _bb_cycle(app.bot)
        except Exception as e:
            logger.warning(f"buybot watcher error: {type(e).__name__}: {e}")
        await asyncio.sleep(BB_CYCLE_PAUSE)
# ===== BUYBOT END =====
