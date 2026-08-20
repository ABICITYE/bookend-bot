import asyncio
import os
import re
import sqlite3

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

DB = "bookend.db"
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
POLL_SECONDS = 60
DUST = 0.001

KNOWN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}


# ---------- storage ----------

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id    INTEGER PRIMARY KEY,
                wallet         TEXT,
                currency       TEXT,
                last_signature TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                signature    TEXT,
                telegram_id  INTEGER,
                direction    TEXT,
                amount       REAL,
                token        TEXT,
                counterparty TEXT,
                timestamp    INTEGER,
                category     TEXT,
                PRIMARY KEY (signature, telegram_id, token, counterparty)
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "last_signature" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_signature TEXT")


def get_user(telegram_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def all_watched():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE wallet IS NOT NULL"
        ).fetchall()


def save_wallet(telegram_id, wallet):
    with db() as conn:
        conn.execute(
            """INSERT INTO users (telegram_id, wallet) VALUES (?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   wallet = excluded.wallet, last_signature = NULL""",
            (telegram_id, wallet),
        )


def save_currency(telegram_id, currency):
    with db() as conn:
        conn.execute(
            "UPDATE users SET currency = ? WHERE telegram_id = ?",
            (currency, telegram_id),
        )


def save_cursor(telegram_id, signature):
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_signature = ? WHERE telegram_id = ?",
            (signature, telegram_id),
        )


def save_transfer(t):
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO transactions
               (signature, telegram_id, direction, amount, token,
                counterparty, timestamp, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
            (t["signature"], t["telegram_id"], t["direction"], t["amount"],
             t["token"], t["counterparty"], t["timestamp"]),
        )


def short(addr):
    return f"{addr[:4]}...{addr[-4:]}" if len(addr) > 10 else addr


# ---------- Telegram handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user and user["wallet"]:
        await update.message.reply_text(
            f"Already watching {short(user['wallet'])}.\n"
            "Send a different address to replace it."
        )
        return
    await update.message.reply_text(
        "Paste a Solana wallet address you get paid on.\n"
        "Read-only — I never ask for private keys."
    )


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user or not user["wallet"]:
        await update.message.reply_text("No wallet yet. Send me an address.")
        return
    await update.message.reply_text(
        f"Wallet: {user['wallet']}\nCurrency: {user['currency'] or 'not set'}"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not BASE58.match(text):
        await update.message.reply_text(
            "That doesn't look like a Solana address. Paste the full address."
        )
        return

    save_wallet(update.effective_user.id, text)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("NGN", callback_data="cur:NGN"),
        InlineKeyboardButton("USD", callback_data="cur:USD"),
    ]])
    await update.message.reply_text(
        f"Watching {short(text)}.\n\nWhat currency do you think in?",
        reply_markup=keyboard,
    )


async def on_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    currency = query.data.split(":", 1)[1]
    save_currency(query.from_user.id, currency)
    await query.edit_message_text(
        f"Set to {currency}. You're done — I'll message you when money moves."
    )


# ---------- RPC ----------

async def rpc(client, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = await client.post(os.environ["RPC_URL"], json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


async def get_new_signatures(client, wallet, until):
    params = [wallet, {"limit": 25}]
    if until:
        params[1]["until"] = until
    result = await rpc(client, "getSignaturesForAddress", params) or []
    return [r["signature"] for r in result if not r.get("err")]


async def get_transaction(client, signature):
    return await rpc(client, "getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
    ])


# ---------- parsing ----------

def account_list(tx):
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in tx["transaction"]["message"].get("accountKeys", [])]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])
    return keys


def native_changes(tx, keys):
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    fee = meta.get("fee", 0)
    changes = {}
    for i, key in enumerate(keys):
        if i >= len(pre) or i >= len(post):
            break
        delta = post[i] - pre[i]
        if i == 0:
            delta += fee          # fee payer: don't count the fee as a transfer
        if delta:
            changes[key] = changes.get(key, 0) + delta / 1_000_000_000
    return changes


def token_changes(tx):
    meta = tx.get("meta") or {}
    balances = {}
    for entry in meta.get("preTokenBalances") or []:
        key = (entry.get("owner"), entry.get("mint"))
        amt = (entry.get("uiTokenAmount") or {}).get("uiAmount") or 0
        balances[key] = balances.get(key, 0) - amt
    for entry in meta.get("postTokenBalances") or []:
        key = (entry.get("owner"), entry.get("mint"))
        amt = (entry.get("uiTokenAmount") or {}).get("uiAmount") or 0
        balances[key] = balances.get(key, 0) + amt
    return {k: v for k, v in balances.items() if abs(v) > 1e-9}


def counterparty_for(changes, wallet, delta):
    """Whoever moved the opposite way by the closest amount."""
    best, best_gap = None, None
    for addr, other in changes.items():
        if addr == wallet or other == 0:
            continue
        if (other > 0) == (delta > 0):
            continue
        gap = abs(abs(other) - abs(delta))
        if best_gap is None or gap < best_gap:
            best, best_gap = addr, gap
    return best or "unknown"


def extract_transfers(tx, signature, wallet, telegram_id):
    out = []
    ts = tx.get("blockTime", 0)
    keys = account_list(tx)

    native = native_changes(tx, keys)
    delta = native.get(wallet, 0)
    if abs(delta) >= DUST:
        out.append(dict(
            signature=signature, telegram_id=telegram_id,
            direction="in" if delta > 0 else "out",
            amount=abs(delta), token="SOL",
            counterparty=counterparty_for(native, wallet, delta),
            timestamp=ts,
        ))

    tokens = token_changes(tx)
    for (owner, mint), amount in tokens.items():
        if owner != wallet:
            continue
        same_mint = {o: v for (o, m), v in tokens.items() if m == mint}
        out.append(dict(
            signature=signature, telegram_id=telegram_id,
            direction="in" if amount > 0 else "out",
            amount=abs(amount),
            token=KNOWN_MINTS.get(mint, short(mint or "token")),
            counterparty=counterparty_for(same_mint, wallet, amount),
            timestamp=ts,
        ))

    return out


def format_transfer(t):
    sign = "+" if t["direction"] == "in" else "−"
    word = "from" if t["direction"] == "in" else "to"
    amount = f"{t['amount']:,.4f}".rstrip("0").rstrip(".")
    return f"{sign}{amount} {t['token']} {word} {short(t['counterparty'])}"


# ---------- watcher ----------

async def watch_loop(app):
    async with httpx.AsyncClient() as client:
        while True:
            try:
                for user in all_watched():
                    wallet = user["wallet"]
                    cursor = user["last_signature"]
                    try:
                        sigs = await get_new_signatures(client, wallet, cursor)
                    except Exception as e:
                        print("signature fetch error:", e)
                        continue

                    if not sigs:
                        continue

                    newest = sigs[0]

                    if not cursor:
                        # First run: bookmark only, don't replay history
                        save_cursor(user["telegram_id"], newest)
                        continue

                    for sig in reversed(sigs):
                        try:
                            tx = await get_transaction(client, sig)
                        except Exception as e:
                            print("tx fetch error:", e)
                            continue
                        if not tx:
                            continue
                        for t in extract_transfers(
                            tx, sig, wallet, user["telegram_id"]
                        ):
                            save_transfer(t)
                            try:
                                await app.bot.send_message(
                                    user["telegram_id"], format_transfer(t)
                                )
                            except Exception as e:
                                print("send error:", e)

                    save_cursor(user["telegram_id"], newest)
            except Exception as e:
                print("watch loop error:", e)

            await asyncio.sleep(POLL_SECONDS)


async def post_init(app):
    app.create_task(watch_loop(app))


def main():
    init_db()
    token = os.environ["BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .get_updates_connect_timeout(60)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .get_updates_pool_timeout(60)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CallbackQueryHandler(on_currency, pattern=r"^cur:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()

