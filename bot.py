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

CATEGORIES = {
    "cli": "Client payment",
    "ref": "Refund",
    "con": "Contractor",
    "exp": "Expense",
    "own": "My own wallet",
    "ign": "Ignored",
}

IN_BUTTONS = ["cli", "ref", "own", "ign"]
OUT_BUTTONS = ["con", "exp", "own", "ign"]


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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                signature    TEXT,
                telegram_id  INTEGER,
                direction    TEXT,
                amount       REAL,
                token        TEXT,
                counterparty TEXT,
                timestamp    INTEGER,
                category     TEXT,
                UNIQUE (signature, telegram_id, token, counterparty)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS labels (
                telegram_id INTEGER,
                address     TEXT,
                name        TEXT,
                category    TEXT,
                PRIMARY KEY (telegram_id, address)
            )
        """)


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
    """Insert and return the row id, or None if it was already stored."""
    with db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (signature, telegram_id, direction, amount, token,
                counterparty, timestamp, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["signature"], t["telegram_id"], t["direction"], t["amount"],
             t["token"], t["counterparty"], t["timestamp"], t.get("category")),
        )
        return cur.lastrowid if cur.rowcount else None


def set_category(row_id, category):
    with db() as conn:
        conn.execute(
            "UPDATE transactions SET category = ? WHERE id = ?",
            (category, row_id),
        )


def get_transaction(row_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (row_id,)
        ).fetchone()


def get_label(telegram_id, address):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM labels WHERE telegram_id = ? AND address = ?",
            (telegram_id, address),
        ).fetchone()


def save_label(telegram_id, address, name, category):
    with db() as conn:
        conn.execute(
            """INSERT INTO labels (telegram_id, address, name, category)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id, address) DO UPDATE SET
                   name = excluded.name, category = excluded.category""",
            (telegram_id, address, name, category),
        )


def short(addr):
    return f"{addr[:4]}...{addr[-4:]}" if len(addr) > 10 else addr


# ---------- message building ----------

def format_transfer(t, name=None):
    sign = "+" if t["direction"] == "in" else "−"
    word = "from" if t["direction"] == "in" else "to"
    amount = f"{t['amount']:,.4f}".rstrip("0").rstrip(".")
    who = name or short(t["counterparty"])
    return f"{sign}{amount} {t['token']} {word} {who}"


def category_keyboard(row_id, direction):
    codes = IN_BUTTONS if direction == "in" else OUT_BUTTONS
    buttons = [
        InlineKeyboardButton(CATEGORIES[c], callback_data=f"cat:{row_id}:{c}")
        for c in codes
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


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

    # Waiting for a name for someone the user just categorised?
    pending = context.user_data.pop("naming", None)
    if pending:
        address, category = pending
        save_label(update.effective_user.id, address, text, category)
        await update.message.reply_text(
            f"Got it — {text}. I'll label them automatically from now on."
        )
        return

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


async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, row_id, code = query.data.split(":")
    row_id = int(row_id)

    set_category(row_id, code)
    tx = get_transaction(row_id)
    if not tx:
        await query.edit_message_text("That transaction is gone.")
        return

    label = CATEGORIES[code]
    line = format_transfer(tx)
    await query.edit_message_text(f"{line}\n{label}")

    save_label(query.from_user.id, tx["counterparty"],
               short(tx["counterparty"]), code)

    if code in ("own", "ign"):
        return

    context.user_data["naming"] = (tx["counterparty"], code)
    await query.message.reply_text(
        f"Name {short(tx['counterparty'])} so I can label them properly:"
    )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    with db() as conn:
        rows = conn.execute(
            """SELECT category, token, SUM(amount) AS total
               FROM transactions
               WHERE telegram_id = ? AND category IS NOT NULL
                 AND category != 'ign'
               GROUP BY category, token""",
            (telegram_id,),
        ).fetchall()
        pending = conn.execute(
            """SELECT COUNT(*) AS n FROM transactions
               WHERE telegram_id = ? AND category IS NULL""",
            (telegram_id,),
        ).fetchone()["n"]

    if not rows and not pending:
        await update.message.reply_text("Nothing recorded yet.")
        return

    lines = []
    for r in rows:
        amount = f"{r['total']:,.4f}".rstrip("0").rstrip(".")
        lines.append(f"{CATEGORIES.get(r['category'], r['category'])}: "
                     f"{amount} {r['token']}")
    if pending:
        lines.append(f"\n{pending} transaction(s) still unlabelled")

    await update.message.reply_text("\n".join(lines))


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


async def fetch_transaction(client, signature):
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
            delta += fee
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


# ---------- watcher ----------

async def announce(app, t):
    """Store a transfer and tell the user, with buttons if it's unknown."""
    known = get_label(t["telegram_id"], t["counterparty"])
    if known:
        t["category"] = known["category"]

    row_id = save_transfer(t)
    if row_id is None:
        return                      # already seen

    if known:
        await app.bot.send_message(
            t["telegram_id"],
            f"{format_transfer(t, known['name'])}\n{CATEGORIES.get(known['category'], '')}",
        )
        return

    await app.bot.send_message(
        t["telegram_id"],
        format_transfer(t),
        reply_markup=category_keyboard(row_id, t["direction"]),
    )


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
                        save_cursor(user["telegram_id"], newest)
                        continue

                    for sig in reversed(sigs):
                        try:
                            tx = await fetch_transaction(client, sig)
                        except Exception as e:
                            print("tx fetch error:", e)
                            continue
                        if not tx:
                            continue
                        for t in extract_transfers(
                            tx, sig, wallet, user["telegram_id"]
                        ):
                            try:
                                await announce(app, t)
                            except Exception as e:
                                print("announce error:", e)

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
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CallbackQueryHandler(on_currency, pattern=r"^cur:"))
    app.add_handler(CallbackQueryHandler(on_category, pattern=r"^cat:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()

