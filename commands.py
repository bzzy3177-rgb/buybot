"""commands.py - /start, DM message router, channel-join gate, group gate, admin-status
tracking, /ownerhelp, /broadcast, /admingroups, /cancel and the command menus."""

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
    OWNER_IDS,
    _is_private,
    add_user,
    bot_is_admin_here,
    em,
    load_admin_groups,
    load_users,
    logger,
    notify_owners,
    owner_only,
    save_admin_groups,
)
from buybots import (
    _bb_load_cfgs,
    _bb_setup,
    _bb_setup_render,
    cmd_buybot_group,
    handle_bb_setup,
)
from ca_scan import (
    extract_ca_from_text,
    handle_ca_lookup,
)


# --- Public commands -------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    add_user(u.id, u.username, u.first_name)
    args = context.args or []
    if args and args[0].startswith("bbnew_"):
        try:
            gid = int(args[0][6:])
        except ValueError:
            gid = 0
        is_admin = False
        if gid:
            try:
                m = await context.bot.get_chat_member(gid, u.id)
                is_admin = m.status in ("creator", "administrator")
            except Exception:
                is_admin = False
        if not gid or not is_admin:
            await update.message.reply_text("⚠️ You must be an admin in that group to set up the buy bot.")
            return
        if len(_bb_setup) > 500:
            _bb_setup.clear()
        await _bb_setup_render(context.bot, u.id, "ca", gid)
        return
    if args and args[0].startswith("bbsetup_"):
        try:
            gid = int(args[0][8:])
        except ValueError:
            gid = 0
        cfg = _bb_load_cfgs().get(str(gid)) if gid else None
        is_admin = False
        if cfg:
            try:
                m = await context.bot.get_chat_member(gid, u.id)
                is_admin = m.status in ("creator", "administrator")
            except Exception:
                is_admin = False
        if not cfg or not is_admin:
            await update.message.reply_text("⚠️ No buy bot found there, or you are not an admin in that group.")
            return
        if len(_bb_setup) > 500:
            _bb_setup.clear()
        await _bb_setup_render(context.bot, u.id, "link", gid)
        return
    for premium, icons in ((True, True), (True, False), (False, False)):
        try:
            await _reply_page(update.message, "start", _start_text(premium), _start_kb(icons))
            return
        except Exception as e:
            logger.warning(f"/start send failed (premium={premium}, icons={icons}): {e}")


# --- /start texts + buttons (custom emoji IDs) -----------------------------------------------
BTN_ICON_ID = "6044166381690166762"   # custom emoji shown on both buttons


def _ce(emoji_id: str, fallback: str, premium: bool) -> str:
    """Custom (premium) emoji tag, or a normal emoji fallback."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>' if premium else fallback


# --- Owner-set media for public pages (photo / video / GIF) -----------------------------------
MEDIA_FILE = Path(os.environ.get("DATA_DIR", ".")) / "page_media.json"
MEDIA_LABELS = {
    "start": "/start",
    "buybot": "BuyBot button page",
    "features": "Features button page",
}
media_state: dict = {}   # owner_id -> media key they are about to upload for


def load_media() -> dict:
    try:
        return json.loads(MEDIA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_media(data: dict) -> None:
    tmp = MEDIA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(MEDIA_FILE)


async def _reply_page(msg, key: str, text: str, reply_markup=None):
    """Reply with the owner-set media for `key` (caption = text), or plain text if none."""
    media = load_media().get(key)
    if media:
        try:
            send = getattr(msg, f"reply_{media['type']}")
            visible = len(html.unescape(re.sub(r"<[^>]+>", "", text)))
            if visible <= 1024:
                await send(media["file_id"], caption=text, parse_mode="HTML",
                           reply_markup=reply_markup)
            else:   # caption too long: media first, text after
                await send(media["file_id"])
                await msg.reply_text(text, parse_mode="HTML", reply_markup=reply_markup,
                                     disable_web_page_preview=True)
            return
        except Exception as e:
            logger.warning(f"media send failed for '{key}': {e}")
    await msg.reply_text(text, parse_mode="HTML", reply_markup=reply_markup,
                         disable_web_page_preview=True)


def _start_text(p: bool) -> str:
    return (
        f'{_ce("6044119257308995249", "🔮", p)} <b><u>Welcome to Wizard Scan Bot!</u></b>\n\n'
        "Add the bot as an admin to your lounge or any group. Whenever a token CA is shared, "
        "the bot will instantly provide token details and X Alerts.\n\n"
        f'{_ce("6044166381690166762", "⚡️", p)} <b>2 More Powerful Features:</b>\n\n'
        f'{_ce("5904530561036197958", "🔹", p)} <b>BuyBots: Get instant token buy alerts.</b>\n'
        f'{_ce("5904530561036197958", "🔹", p)} <b>PnL Cards: Track your PnL.</b>\n\n'
        f'{_ce("5846193940105012285", "👇", p)} <b>Tap the buttons below to explore all the features:</b>'
    )


def _buybot_text(p: bool) -> str:
    return (
        f'{_ce("5807491057792853272", "🤖", p)} <b><u>Wizard Scan BuyBot Feature!</u></b>\n\n'
        "Developers can now install the BuyBot in their group or lounge to get instant "
        "notifications whenever someone buys a token. If your token maintains good volume, "
        "it can trend on Wizard Scan, bringing you solid trading volume.\n\n"
        f'{_ce("6044166381690166762", "⚡️", p)} <b>How to Install BuyBot</b>\n\n'
        "<b>1)</b> Add Wizard Scan Bot to your group.\n"
        "<b>2)</b> Make the bot an admin.\n"
        "<b>3)</b> Allow permission to post messages.\n"
        '<b>4)</b> Send "/buybot" in the group.\n'
        "<b>5)</b> Open the setup page.\n"
        "<b>6)</b> Follow the video tutorial.\n\n"
        "<b>When the bot asks for details in your DM, it will also show video tutorials "
        "to help you set up your BuyBot easily.</b>"
    )


def _features_text(p: bool) -> str:
    return (
        f'{_ce("6044119257308995249", "🔮", p)} <b><u>Wizard Scan — Powerful Features!</u></b>\n\n'
        "Add the bot as an admin to your lounge or private group. Paste a CA, and if the token "
        "reaches 2x or higher, the bot will alert the group with the sender’s username.\n\n"
        f'{_ce("5920335246116527693", "📈", p)} <b>Multi-X Tracking</b>\n\n'
        "Get alerts at 2x, 5x, 10x, 20x, 30x, 40x, 50x, 100x, 150x, and all the way up to 800x!\n\n"
        f'{_ce("5922570858198474764", "📢", p)} <b>Channel Exposure</b>\n\n'
        "Every 10x milestone is also shared on @WizardScan, featuring the user’s name, group name, "
        "public group link (if available), and total X.\n\n"
        f'{_ce("5920335246116527693", "📊", p)} <b>PnL Checker</b>\n\n'
        'Use "/pnl" to check how many X your call has reached.'
    )


def _start_kb(icons: bool) -> InlineKeyboardMarkup:
    kw = {"api_kwargs": {"icon_custom_emoji_id": BTN_ICON_ID}} if icons else {}
    prefix = "" if icons else "⚡️ "
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{prefix}BuyBot", callback_data="st:buy", **kw)],
        [InlineKeyboardButton(f"{prefix}Features", callback_data="st:feat", **kw)],
    ])


_START_PAGES = {"st:buy": _buybot_text, "st:feat": _features_text}
_START_KEYS = {"st:buy": "buybot", "st:feat": "features"}


async def cb_start_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    build = _START_PAGES.get(q.data)
    if not build:
        return
    for premium in (True, False):
        try:
            await _reply_page(q.message, _START_KEYS[q.data], build(premium))
            return
        except Exception as e:
            logger.warning(f"start info send failed ({q.data}, premium={premium}): {e}")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM: owner broadcast wizard first, then CA/chart-link lookup for everyone."""
    msg = update.message
    u = update.effective_user
    if not msg or not u:
        return
    add_user(u.id, u.username, u.first_name)

    if u.id in OWNER_IDS and u.id in media_state:
        if await handle_media_flow(update, context):
            return

    if u.id in OWNER_IDS and u.id in broadcast_state:
        if await handle_broadcast_flow(update, context):
            return

    if u.id in _bb_setup:
        try:
            if await handle_bb_setup(update, context):
                return
        except Exception as e:
            logger.warning(f"buybot setup answer failed for {u.id}: {e}")
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
        fn = {"start": cmd_start}.get(parts[0][1:].split("@")[0].lower())
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




async def block_non_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot does nothing in groups, EXCEPT: a CA/chart-link paste in a group
    where it has admin rights gets a snapshot reply."""
    if _is_private(update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    text = ((getattr(msg, "text", None) or getattr(msg, "caption", None) or "") if msg else "")

    if msg and chat and chat.type in ("group", "supergroup") and text.startswith("/buybot"):
        parts = text.split()
        cmd, _, target = parts[0].partition("@")
        if cmd.lower() == "/buybot" and (not target or target.lower() == (context.bot.username or "").lower()):
            context.args = parts[1:]
            try:
                await cmd_buybot_group(update, context)
            except Exception as e:
                logger.warning(f"group /buybot failed in {chat.id}: {e}")
    if msg and chat and chat.type in ("group", "supergroup") and text and not text.startswith("/"):
        found = extract_ca_from_text(text)
        if found and await bot_is_admin_here(context, chat):
            bb_cfg = _bb_load_cfgs().get(str(chat.id))
            if bb_cfg is not None and not bb_cfg.get("scan_ca"):
                raise ApplicationHandlerStop   # buybot group without scan permission
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
    "📋 <b>Admin Groups</b> - groups/channels where the bot is currently admin\n"
    "🖼 <b>Set Media</b> - attach a photo/video/GIF to the public commands"
)


def _oh_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Broadcast", callback_data="oh:broadcast"),
        InlineKeyboardButton("📋 Admin Groups", callback_data="oh:admingroups"),
    ], [
        InlineKeyboardButton("🖼 Set Media", callback_data="oh:media"),
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
    uid = update.effective_user.id
    was_bc = broadcast_state.pop(uid, None)
    was_media = media_state.pop(uid, None)
    if was_bc or was_media:
        await update.message.reply_text("✅ Cancelled.")
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


def _media_menu_text() -> str:
    return ("🖼 <b>SET MEDIA</b>\n\n"
            "Pick a public command/page, then send a photo, video or GIF. "
            "It will be shown together with that reply.\n"
            "✅ = media set")


def _media_menu_kb() -> InlineKeyboardMarkup:
    media = load_media()
    rows = [[InlineKeyboardButton(f"{'✅' if k in media else '▫️'} {label}",
                                  callback_data=f"oh:m:{k}")]
            for k, label in MEDIA_LABELS.items()]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="oh:home")])
    return InlineKeyboardMarkup(rows)


async def _edit_panel(message, text: str, kb):
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"owner panel edit failed: {e}")


async def handle_media_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Owner is in 'set media' mode: save the photo/video/GIF they send."""
    uid = update.effective_user.id
    key = media_state.get(uid)
    if not key:
        return False
    msg = update.message
    if msg.animation:
        mtype, fid = "animation", msg.animation.file_id
    elif msg.video:
        mtype, fid = "video", msg.video.file_id
    elif msg.photo:
        mtype, fid = "photo", msg.photo[-1].file_id
    else:
        await msg.reply_text("⚠️ Send a photo, video or GIF. /cancel to stop.")
        return True
    media = load_media()
    media[key] = {"type": mtype, "file_id": fid}
    save_media(media)
    media_state.pop(uid, None)
    await msg.reply_text(
        f"✅ Media saved for <b>{html.escape(MEDIA_LABELS[key])}</b>. Test it from your own chat.",
        parse_mode="HTML", reply_markup=_media_menu_kb())
    return True


async def cb_ownerhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if uid not in OWNER_IDS:
        await query.answer("Owner only.", show_alert=True)
        return
    await query.answer()
    data = query.data
    if data == "oh:broadcast":
        await _broadcast_panel(query.message, uid)
    elif data == "oh:admingroups":
        await _send_admin_groups_list(query.message)
    elif data == "oh:home":
        media_state.pop(uid, None)
        await _edit_panel(query.message, OH_HOME_TEXT, _oh_kb())
    elif data == "oh:media":
        media_state.pop(uid, None)
        await _edit_panel(query.message, _media_menu_text(), _media_menu_kb())
    elif data.startswith("oh:m:"):
        key = data[5:]
        if key not in MEDIA_LABELS:
            return
        media_state[uid] = key
        has = key in load_media()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Remove media", callback_data=f"oh:mdel:{key}")] if has else [],
            [InlineKeyboardButton("⬅️ Back", callback_data="oh:media")],
        ])
        kb = InlineKeyboardMarkup([r for r in kb.inline_keyboard if r])
        await _edit_panel(
            query.message,
            f"🖼 <b>{html.escape(MEDIA_LABELS[key])}</b>\n\n"
            f"Status: {'✅ media set' if has else '▫️ no media'}\n\n"
            "Now send the photo, video or GIF.\n/cancel to stop.", kb)
    elif data.startswith("oh:mdel:"):
        key = data[8:]
        media = load_media()
        if media.pop(key, None) is not None:
            save_media(media)
        media_state.pop(uid, None)
        await _edit_panel(query.message, _media_menu_text(), _media_menu_kb())


PUBLIC_COMMANDS = [
    BotCommand("start", "About this bot"),
    BotCommand("buybot", "Add a live buy bot to your group"),
]
# Owner commands (/ownerhelp, /broadcast, /admingroups, /cancel, /backupnow, /restorenow) are
# intentionally NOT listed in the "/" menu - they still work when the owner types them manually.
OWNER_COMMANDS = list(PUBLIC_COMMANDS)
