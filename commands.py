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
    rows = []
    if context.bot.username:
        rows.append([InlineKeyboardButton(
            "➕ Add Me to Your Group",
            url=f"https://t.me/{context.bot.username}?startgroup=buybot&admin=delete_messages")])
    rows.append([InlineKeyboardButton("📘 How to Install", callback_data="st:buy")])
    kb = InlineKeyboardMarkup(rows)
    for premium in (True, False):
        text = (
            f'{em("w_welcome", "🔮", premium)} <b>Welcome to Wizard Scan Buy Bot</b>\n\n'
            "The fastest buy bot for your token - a live alert on every buy with market cap, buyer, "
            "transaction link and Chart / Trade / Calls buttons.\n\n"
            f'{em("w_feat", "🔹", premium)} <b>Chains:</b> Solana · BNB · Ethereum · Base · Arbitrum · Polygon &amp; more\n\n'
            f'{em("w_more", "⚡️", premium)} <b>Get started in 30 seconds:</b>\n'
            f'{em("w_feat", "1️⃣", premium)} Add me to your group as <b>admin</b>\n'
            f'{em("w_feat", "2️⃣", premium)} Send <code>/buybot &lt;token CA&gt;</code> in the group\n'
            f'{em("w_feat", "3️⃣", premium)} Set your link, emojis and media - done!'
        )
        try:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb,
                                            disable_web_page_preview=True)
            return
        except Exception as e:
            logger.warning(f"/start send failed (premium={premium}): {e}")


_START_INFO = {
    "st:buy": "🔮 Buy Bot for Token: add the bot to your group, send /buybot <CA> and every buy of "
              "your token gets a live alert with amount, market cap and Chart/Trade/Calls buttons.",
}


async def cb_start_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(_START_INFO.get(q.data, ""), show_alert=True)


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



PUBLIC_COMMANDS = [
    BotCommand("start", "About this bot"),
    BotCommand("buybot", "Add a live buy bot to your group"),
]
OWNER_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("ownerhelp", "Owner control panel"),
    BotCommand("broadcast", "Message your users"),
    BotCommand("admingroups", "Groups/channels where the bot is admin"),
    BotCommand("cancel", "Cancel the current action"),
]

