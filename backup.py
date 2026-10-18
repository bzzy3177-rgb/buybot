"""backup.py - owner-only backup / restore.

  * Auto backup: every BACKUP_INTERVAL_HOURS (default 3) the bot DMs every owner one JSON file
    with ALL its data (users, admin groups, buy bot settings, holders cache, calls, alerts).
  * /backupnow  - instant backup, sent to you right away.
  * /restorenow - restore from a backup file. Reply to the backup file with /restorenow, or send
    /restorenow and then upload the file. A confirm button is shown before anything is overwritten.

The backup is a normal Telegram message in your DM, so it survives a Railway reset / redeploy.
"""

import io
import os
import json
import gzip
import time
import asyncio
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import RetryAfter

from core import (
    DATA_DIR,
    OWNER_IDS,
    USERS_FILE,
    ADMIN_GROUPS_FILE,
    load_json,
    save_json,
    logger,
    owner_only,
)
from ca_scan import HOLDERS_FILE, CALLS_FILE, ALERTS_FILE
from buybots import BUYBOT_FILE

BACKUP_INTERVAL = float(os.environ.get("BACKUP_INTERVAL_HOURS", "3") or 3) * 3600
GZIP_ABOVE = 15 * 1024 * 1024      # Telegram lets bots download max 20 MB, so compress big backups
PENDING_TTL = 600                  # seconds /restorenow waits for the file / the confirm tap
META_FILE = DATA_DIR / "backup_meta.json"

# backup section name -> data file. Add a new data file here and it is backed up + restored too.
BACKUP_FILES = {
    "users": USERS_FILE,
    "admin_groups": ADMIN_GROUPS_FILE,
    "buybot": BUYBOT_FILE,
    "holders": HOLDERS_FILE,
    "calls": CALLS_FILE,
    "alerts": ALERTS_FILE,
}
_LABELS = {
    "users": "users", "admin_groups": "admin groups", "buybot": "buy bots",
    "holders": "holder cache", "calls": "calls", "alerts": "alerts",
}

_restore_pending: dict = {}   # owner id -> time /restorenow asked for the file
_restore_staged: dict = {}    # owner id -> {"snap": dict, "ts": float}


# --- build / serialize ----------------------------------------------------------------------
def _read_file(path):
    """Parsed JSON of a data file, or None if it is missing / unreadable."""
    try:
        if path.exists():
            txt = path.read_text(encoding="utf-8")
            return json.loads(txt) if txt.strip() else {}
    except Exception as e:
        logger.warning(f"backup: could not read {path.name}: {e}")
    return None


def build_snapshot() -> dict:
    snap = {"_timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "_bot": "wizard-scan-buybot", "_version": 1}
    for name, path in BACKUP_FILES.items():
        data = _read_file(path)
        if data is not None:
            snap[name] = data
    return snap


def _is_empty(snap: dict) -> bool:
    return not any(v for k, v in snap.items() if not k.startswith("_"))


def _summary(snap: dict) -> str:
    parts = []
    for name in BACKUP_FILES:
        v = snap.get(name)
        if isinstance(v, (dict, list)):
            parts.append(f"{len(v)} {_LABELS[name]}")
    return " · ".join(parts) or "empty"


def _make_backup():
    """-> (snapshot, file bytes, file name). Blocking - run it in a thread."""
    snap = build_snapshot()
    raw = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    if len(raw) > GZIP_ABOVE:
        return snap, gzip.compress(raw), f"buybot_backup_{stamp}UTC.json.gz"
    return snap, raw, f"buybot_backup_{stamp}UTC.json"


async def _send_doc(bot, chat_id: int, data: bytes, name: str, caption: str):
    for attempt in (1, 2):
        try:
            await bot.send_document(chat_id, document=io.BytesIO(data), filename=name, caption=caption)
            return True
        except RetryAfter as e:
            if attempt == 2:
                return False
            await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
        except Exception as e:
            logger.warning(f"backup: send to {chat_id} failed: {type(e).__name__}: {e}")
            return False
    return False


async def send_backup(bot, reason: str, targets=None, snap_data=None) -> int:
    """Build a backup and DM it. Returns how many owners received it."""
    snap, data, name = snap_data or await asyncio.to_thread(_make_backup)
    caption = f"💾 {reason} backup · {snap['_timestamp'][:16].replace('T', ' ')} UTC\n{_summary(snap)}"
    sent = 0
    for oid in (targets or OWNER_IDS):
        if await _send_doc(bot, oid, data, name, caption):
            sent += 1
    return sent


# --- commands ----------------------------------------------------------------------------------
@owner_only
async def cmd_backupnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    wait = await msg.reply_text("⏳ Making backup...")
    snap_data = await asyncio.to_thread(_make_backup)
    ok = await send_backup(context.bot, "Manual", targets=[update.effective_user.id], snap_data=snap_data)
    try:
        await wait.delete()
    except Exception:
        pass
    if not ok:
        await msg.reply_text("⚠️ Backup was made but Telegram would not accept the file. Try again in a minute.")
    elif _is_empty(snap_data[0]):
        await msg.reply_text("⚠️ The bot has no data right now, so that backup file is empty. "
                             "Do not overwrite your old backup with it.")


def _cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="rs:cancel")]])


@owner_only
async def cmd_restorenow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    replied = msg.reply_to_message
    if replied and replied.document:
        await _stage_from_document(msg, uid, context, replied.document)
        return
    _restore_pending[uid] = time.time()
    await msg.reply_text(
        "📂 Send me the backup file now (.json or .json.gz) - or reply to the backup file with /restorenow.\n"
        "Nothing is overwritten until you confirm. Waiting 10 minutes.",
        reply_markup=_cancel_kb())


async def handle_restore_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner uploads the backup file after /restorenow."""
    msg, u = update.message, update.effective_user
    if not msg or not msg.document or not u or u.id not in OWNER_IDS:
        return
    ts = _restore_pending.get(u.id)
    if not ts or time.time() - ts > PENDING_TTL:
        _restore_pending.pop(u.id, None)
        return
    _restore_pending.pop(u.id, None)
    await _stage_from_document(msg, u.id, context, msg.document)


def _parse_backup(data: bytes):
    """-> (snapshot, None) or (None, error text)."""
    try:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        snap = json.loads(data.decode("utf-8"))
    except Exception:
        return None, "That file is not a valid backup (could not read it as JSON)."
    if not isinstance(snap, dict):
        return None, "That file is not a backup from this bot."
    found = [k for k in BACKUP_FILES if k in snap]
    if not found:
        return None, "That file has none of this bot's data sections - wrong backup file?"
    for k in found:
        if not isinstance(snap[k], dict):
            return None, f"Section '{k}' in the file is damaged - not restoring."
    return snap, None


async def _stage_from_document(msg, uid: int, context, doc):
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("⚠️ That file is over 20 MB - Telegram does not let bots download it.")
        return
    try:
        f = await context.bot.get_file(doc.file_id)
        data = bytes(await f.download_as_bytearray())
    except Exception as e:
        logger.warning(f"restore: download failed: {type(e).__name__}: {e}")
        await msg.reply_text("⚠️ Could not download that file from Telegram. Send it again.")
        return
    snap, err = await asyncio.to_thread(_parse_backup, data)
    if err:
        await msg.reply_text("⚠️ " + err)
        return
    _restore_staged[uid] = {"snap": snap, "ts": time.time()}
    sections = [k for k in BACKUP_FILES if k in snap]
    lines = [f"• {_LABELS[k]}: {len(snap[k])}" for k in sections]
    when = str(snap.get("_timestamp", "unknown"))[:16].replace("T", " ")
    await msg.reply_text(
        f"📦 Backup from {when} UTC\n" + "\n".join(lines) +
        "\n\n⚠️ Restoring REPLACES the bot's current data for these sections. Continue?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Restore now", callback_data="rs:go"),
            InlineKeyboardButton("❌ Cancel", callback_data="rs:cancel"),
        ]]))


def _atomic_write(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _apply_snapshot(snap: dict) -> list:
    done = []
    for name, path in BACKUP_FILES.items():
        if name in snap:
            _atomic_write(path, snap[name])
            done.append(f"{_LABELS[name]} ({len(snap[name])})")
    return done


async def cb_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid not in OWNER_IDS:
        await q.answer("Owner only.", show_alert=True)
        return
    if q.data == "rs:cancel":
        _restore_pending.pop(uid, None)
        _restore_staged.pop(uid, None)
        await q.answer("Cancelled")
        try:
            await q.edit_message_text("❌ Restore cancelled. Nothing was changed.")
        except Exception:
            pass
        return
    if q.data != "rs:go":
        await q.answer()
        return
    staged = _restore_staged.pop(uid, None)
    if not staged or time.time() - staged["ts"] > PENDING_TTL:
        await q.answer("Expired - run /restorenow again.", show_alert=True)
        return
    await q.answer("Restoring...")
    try:
        await q.edit_message_text("⏳ Restoring...")
    except Exception:
        pass
    # Safety net: DM the CURRENT data first (skipped when there is nothing to lose).
    current = await asyncio.to_thread(_make_backup)
    if not _is_empty(current[0]):
        if not await send_backup(context.bot, "Pre-restore", targets=[uid], snap_data=current):
            await q.message.reply_text("⚠️ Could not send the safety copy of your current data, so I did not "
                                       "restore anything. Try /restorenow again.")
            return
    try:
        done = await asyncio.to_thread(_apply_snapshot, staged["snap"])
    except Exception as e:
        logger.error(f"restore failed: {type(e).__name__}: {e}")
        await q.message.reply_text(f"⚠️ Restore failed: {type(e).__name__}: {e}")
        return
    logger.info(f"restore done by {uid}: {done}")
    await q.message.reply_text("✅ Restored: " + ", ".join(done))


# --- auto backup ----------------------------------------------------------------------------------
async def backup_watcher(app):
    """Every BACKUP_INTERVAL: DM all owners the backup. The 'last sent' time is kept on disk, so a
    redeploy does not restart the 3h clock; an empty bot (fresh reset) never sends a blank backup."""
    await asyncio.sleep(60)
    while True:
        try:
            last = float(load_json(META_FILE, {}).get("last_auto_ts", 0) or 0)
            wait = BACKUP_INTERVAL - (time.time() - last)
            if wait > 0:
                await asyncio.sleep(min(wait, 600))
                continue
            snap_data = await asyncio.to_thread(_make_backup)
            if _is_empty(snap_data[0]):
                logger.warning("auto-backup skipped: the bot has no data yet")
                await asyncio.sleep(600)
                continue
            if await send_backup(app.bot, "Auto", snap_data=snap_data):
                save_json(META_FILE, {"last_auto_ts": time.time()})
                logger.info(f"auto-backup sent ({_summary(snap_data[0])})")
            else:
                await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"backup watcher error: {type(e).__name__}: {e}")
            await asyncio.sleep(300)
