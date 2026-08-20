# Bookend


Bookend is a Telegram bot that watches your Solana wallet and asks what
each payment was for. Tap once, and it remembers — so the same client or
contractor gets labelled automatically next time.
The primary goal of bookend is to eliminate thr need for spreadshreet for crypto payments.

The chain already knows the amounts and addresses. It doesn't know which
payment was revenue and which was a cost. That part lives in your head
until you forget it. Bookend asks while you still remember.

## How it works

1. Give the bot a wallet address. Read-only — it never asks for keys.
2. When money moves, it messages you with buttons: Client payment,
   Contractor, Expense, and so on.
3. Tap one. Give the address a name if you want.
4. Future transfers from that address are labelled automatically.
5. `/report` totals everything up.

## Commands

| Command | What it does |
| --- | --- |
| `/start` | Set up a wallet to watch |
| `/wallet` | Show the wallet currently being watched |
| `/report` | Totals by category, money in and money out |

Sending a bare wallet address at any time replaces the watched wallet.

## Running it

Requires Python 3 and a bot token from [@BotFather](https://t.me/BotFather).

```bash
pip install python-telegram-bot httpx

export BOT_TOKEN="your-token-from-botfather"
export RPC_URL="https://api.mainnet-beta.solana.com"

python bot.py
```

For testing against devnet, use `https://api.devnet.solana.com` instead.

Data lives in `bookend.db`, a SQLite file created on first run. It's
gitignored — it holds wallet addresses.

## How the watcher works

Every 60 seconds the bot asks the RPC for new signatures on the watched
wallet, fetches each transaction, and works out the balance change for
that wallet — both native SOL and SPL tokens. Native moves under 0.001
SOL are skipped, since those are usually fees rather than payments.

On first run it records the latest signature and reports nothing. Only
transactions after startup get announced, so adding a wallet doesn't
replay its entire history at you.

## Known gaps

- Tokens other than USDC and USDT show as a truncated mint address
- One wallet per user
- A swap arrives as two messages, not one event
- No hosting — it runs wherever you start it

