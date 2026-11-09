# TPPD BTC options-writing bot (Delta Exchange India)

Sells next-day-expiry BTC options in the direction of the score **D**, naked or as a credit spread, and takes **3% of premium after fees**. A strong winner is upgraded to a **runner** and can be held to expiry, where it expires worthless with no buy-back fee.

## The logic

**Direction score** (BTC perpetual, 15-min candles):

    D = 0.5·tanh(T) + 0.3·M − 0.2·tanh(V)
    T = (EMA20 − EMA50) / ATR14     M = (RSI14 − 50) / 50     V = (Price − VWAP) / ATR14

| D | Action | Entry trigger (5-min RSI) |
|---|---|---|
| > +0.30 | Sell put, delta 0.25–0.35 | RSI ≤ 40 |
| < −0.30 | Sell call, delta 0.25–0.35 | RSI ≥ 60 |
| otherwise | No trade | — |

**Sideways days (`STRANGLE=true`):** when D is between −0.30 and +0.30, the bot sells **both** a put and a call (|delta| ≈ 0.20), entering when the 5-min RSI is calm (40–60).
- `HEDGE=off`: **short strangle**, two naked legs, each with an exchange-side backstop that fires if that leg's premium doubles.
- `HEDGE=on`: **iron condor**, the strangle plus a protective wing on each side (delta ≈ 0.08). The maximum loss is capped, because only one side can finish in the money.
- Same 3%-after-fees target and stop loss on the combined premium. It exits early if a trend starts (|D| ≥ 0.45).
- Runner: held to expiry when D stays quiet, P(BTC settles between the sold strikes) ≥ 75%, and both sold deltas ≤ 0.25.
- `--report` splits results into trend and sideways trades.

**Hedge (`HEDGE` in .env):**
- `off`: naked single option, with an exchange-side backstop stop at +25%
- `on`: credit spread. Buys a further out-of-the-money option (delta ~0.08) first, then sells the main one. Maximum loss = strike gap − credit. No backstop is needed.
- `compare`: paper mode only. Runs both on the same signals in separate books, so `--report` shows which earns more after fees.

**Gates before selling:**
- Two-sided quote, spread ≤ 6%
- Net credit ≥ 50% of the sold premium (for spreads)
- Fees mustn't push the needed decay above 25% of credit
- Time decay must deliver at least half of it within 4 hours

**Scalp exits:**
- Take profit when the cost to close ≤ credit × (1 − 3% − round-trip fees)
- Stop when cost ≥ credit × 1.10
- D flips sign, 4 hours held, or 1 hour before expiry (17:30 IST)

**Runner upgrade.** Checked the moment a scalp reaches its 3% target. All three must hold:
1. D still agrees strongly: ≥ +0.40 for a sold put, ≤ −0.40 for a sold call.
2. P(expires worthless) = N(z) ≥ 75%, where z = ln(spot/strike) / (σ·√t) for a put (inverted for a call). σ is the larger of the option's implied vol and BTC's realised vol.
3. The sold option's |delta| ≤ 0.25.

If not, it takes the 3%. If yes, it holds and manages the runner like this:
- **Floor:** exit if cost rises to `min(breakeven, 2 × lowest cost seen)`. Worst case is roughly breakeven, and the floor ratchets down as the premium melts. For example, if the premium falls to 60, the bot exits if it rebounds to 120.
- **Spot guard:** exit if P(expires worthless) drops below ~62%.
- **Direction guard:** exit if D swings the wrong way past 0.10.
- **Last hour:** hold into settlement only if P(expires worthless) ≥ 97%. Settlement uses the 30-min TWAP up to 17:30 IST.
- A runner doesn't block new scalps: each book can hold one runner plus one scalp.

**Daily limits (per book):** 3 trades, 2 losses, $20 net loss, 15-minute cooldown. Counters reset at midnight IST.

### Fees matter more than anything else here
Delta India options fees are reported as 0.03% of notional per side, capped at 3.5% of premium, plus 18% GST. On a 0.30-delta next-day BTC option that's roughly **8% of premium per round trip**. A spread pays fees on both legs, so its round trip costs a bigger share of a smaller credit.

An option that expires worthless has no premium to charge, so runners that make it to expiry skip the exit fee entirely.

Fee figures came from a sponsored article, not Delta's official schedule. Compare `fees_usd_est` in `data/trades.csv` with your testnet fills and adjust `FEE_RATE_NOTIONAL` / `FEE_CAP_PREMIUM` if they differ.

## Setup on the Lightsail server (same box as the Zerodha bot is fine)

```bash
# 1. copy the folder to the server (from your laptop)
scp -r btc_bot ubuntu@<LIGHTSAIL_IP>:~/

# 2. on the server
cd ~/btc_bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_bot.py          # 32 offline tests should pass
cp .env.example .env && chmod 600 .env
nano .env                             # paste key + secret here, nowhere else
```

**API key:** create it on Delta with *Read* and *Trading* permissions, and whitelist the Lightsail **static IP** (Delta only allows trading keys from whitelisted IPs). A testnet key comes from `demo.delta.exchange` and only works with `DELTA_ENV=testnet`.

```bash
# 3. one-off check: prints D, RSI and the option it WOULD sell. Never trades.
.venv/bin/python bot.py --once

# 4. run it permanently (restarts on crash/reboot)
sudo cp tppd-btc.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tppd-btc
journalctl -u tppd-btc -f             # live log;  also data/bot.log
```

## Going live, in three stages
1. `MODE=paper`, `DELTA_ENV=prod`, `HEDGE=compare` — real prices, simulated fills, naked and spread side by side. Run until each book has 20–30 trades, then check `.venv/bin/python bot.py --report` and pick `HEDGE=off` or `on`.
2. `MODE=live`, `DELTA_ENV=testnet` with a demo key — confirms orders, stops and fees behave as expected.
3. `MODE=live`, `DELTA_ENV=prod` with `ORDER_SIZE=1`.

After editing `.env`: `sudo systemctl restart tppd-btc`.

**Emergency stop:** `sudo systemctl stop tppd-btc`, then close any open position in the Delta app. The backstop stop order stays on the exchange until you cancel it.

## Phone alerts (optional)
Create a bot with @BotFather on Telegram, message it once, get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`, then fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. You'll get a message on every entry, exit and warning.

## Files
| File | Purpose |
|---|---|
| `bot.py` | Main loop, entries, exits, runners, settlement, limits, `--once`, `--report` |
| `strategy.py` | D score, RSI trigger, fee model, option/wing picker, runner test |
| `delta_client.py` | Signed Delta API client (prod/testnet) |
| `config.py` | All settings; override any of them in `.env` |
| `test_bot.py` | Offline tests against a simulated exchange |
| `data/` | `bot.log`, `trades.csv`, `state.json` (created at runtime) |
