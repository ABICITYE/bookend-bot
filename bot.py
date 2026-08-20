import os
import re
import sqlite3

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


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                wallet      TEXT,
                currency    TEXT
            )
        """)


def get_user(telegram_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def save_wallet(telegram_id, wallet):
    with db() as conn:
        conn.execute(
            """INSERT INTO users (telegram_id, wallet) VALUES (?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET wallet = excluded.wallet""",
            (telegram_id, wallet),
        )


def save_currency(telegram_id, currency):
    with db() as conn:
        conn.execute(
            "UPDATE users SET currency = ? WHERE telegram_id = ?",
            (currency, telegram_id),
        )


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


def short(addr):
    return f"{addr[:4]}...{addr[-4:]}"


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
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CallbackQueryHandler(on_currency, pattern=r"^cur:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()

