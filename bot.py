"""bot.py - entry point. Wires handlers from commands / ca_scan / buybots and starts polling.
Start command: python bot.py"""

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
    BOT_TOKEN,
    EMOJI_IDS,
    OWNER_IDS,
    logger,
)
from commands import (
    OWNER_COMMANDS,
    PUBLIC_COMMANDS,
    block_non_private,
    cb_join_done,
    cb_ownerhelp,
    cb_start_info,
    cmd_admin_groups,
    cmd_broadcast,
    cmd_cancel,
    cmd_ownerhelp,
    cmd_start,
    handle_private_message,
    join_gate,
    on_chat_member_update,
)
from buybots import (
    buybot_watcher,
    cb_bb_wizard,
    cb_buybot,
    cmd_buybot,
)
from ca_scan import (
    cb_refresh,
    multiplier_watcher,
)


async def validate_emoji_ids(bot):
    """Blank the custom emoji IDs Telegram really can't see - one bad ID rejects the WHOLE
    message, which would turn every alert plain. That is why the title emoji must NOT be kept
    when it is unusable: check the deploy log for 'bb_name' and set BB_TITLE_EMOJI_ID to a
    valid ID if you want a premium title emoji."""
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
    application.bot_data["watcher"] = asyncio.create_task(multiplier_watcher(application))
    application.bot_data["bb_watcher"] = asyncio.create_task(buybot_watcher(application))
    menu_jobs = [
        (PUBLIC_COMMANDS, None),    # default scope - overrides any BotFather command list on every boot
        (PUBLIC_COMMANDS, BotCommandScopeAllPrivateChats()),
        ([BotCommand("buybot", "Set up and manage the buy bot")], BotCommandScopeAllGroupChats()),
    ]
    for cmds, scope in menu_jobs:
        try:
            if scope is None:
                await application.bot.set_my_commands(cmds)
            else:
                await application.bot.set_my_commands(cmds, scope=scope)
        except Exception as e:
            logger.warning(f"set_my_commands failed (scope={scope}): {e}")
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
    app.add_handler(CommandHandler("buybot", cmd_buybot, filters=private))
    app.add_handler(CallbackQueryHandler(cb_buybot, pattern=r"^bb:"))
    app.add_handler(CallbackQueryHandler(cb_bb_wizard, pattern=r"^bbw:"))

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
