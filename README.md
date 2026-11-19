# Paper Signal Bot

This repository runs a **paper-trading simulation only**. It does not place real orders.

## Telegram commands

The bot keeps the Telegram slash-command menu and `/help` text synchronized from the same command list:

- `/start` — start and show help
- `/status` — open simulated positions
- `/stats` — virtual wallet and results
- `/plan` — current strategy conditions for each asset
- `/diagnostics` — why each asset passed or failed the entry checks
- `/help` — list commands

The first successful run after a command-list change calls Telegram's `setMyCommands` API. The version is stored in `state.json`, so the menu is not reset on every scheduled run.

## Entry diagnostics

`/diagnostics` reports, per asset:

- number of completed-candle evaluations;
- how many times the strategy conditions passed;
- how many simulated positions were opened;
- the latest score, regime, setup, rejected filters, and strategy reasons;
- common execution blockers such as cooldown, stale signals, price-chasing, wallet halt, and position sizing;
- data-fetch or analysis errors, counted separately from strategy rejections.

Diagnostics are stored under the `diagnostics` key in `state.json`. Existing wallet state is migrated by adding defaults; the wallet balance and open positions are not reset.

## Tests

Run all unit tests with:

```bash
python -m unittest -v
```

The tests include a synthetic qualifying uptrend to verify that the strategy can produce a paper entry signal. This is a logic smoke test, not evidence of profitability.
