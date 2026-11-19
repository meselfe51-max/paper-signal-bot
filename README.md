# Paper Signal Bot

This repository runs **paper-trading simulation only**. It does not place real orders.

## Telegram commands

- `/start` — start and show help
- `/status` — open simulated positions
- `/stats` — virtual wallet and results
- `/plan` — current strategy conditions for each asset
- `/diagnostics` — why each asset passed or failed the entry checks
- `/help` — list commands

The existing Telegram bot, wallet and strategy are not connected to the competition lab. The lab cannot change the bot's state file or decisions.

## Strategy Competition Lab

Run the independent competition on **full historical CSV data**:

```bash
python -m laboratory.run --csv-dir data --out results/competition.json
```

The recent public API mode is only a short-history fallback (Kraken's OHLC endpoint is limited to the latest 720 candles):

```bash
python -m laboratory.run --live --out results/recent-competition.json
```

### Supplying historical data

Kraken publishes downloadable OHLCVT CSV archives containing each pair's available history and 240-minute candles. Download and extract the archive, then point `--csv-dir` at the extracted directory. The loader searches recursively, so the original nested folder structure can be kept. It merges every matching canonical pair/timeframe CSV it finds (useful when full and incremental files are both present), then deduplicates timestamps. It recognizes files such as `XBTUSD_240.csv`; each CSV row may contain the seven official columns:

`timestamp, open, high, low, close, volume, trades`

No header is required. The loader reads the first six columns and ignores the trade-count column. It validates rows, sorts timestamps, removes duplicates, and fills exact missing 4-hour intervals with flat zero-volume candles (Kraken documents that missing intervals mean no trades). Very large gaps are reported instead of being expanded. Every strategy receives the same full available history for each asset; at each decision it sees only candles strictly earlier than that decision candle.

Official archive instructions and quarterly updates: [Kraken downloadable historical OHLCVT data](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data). The full archive is large and split into multiple parts; make sure all parts are joined before extracting. The project does not commit raw market data or store the multi-gigabyte archive in Git.

The report's `data` section records the file used, first/last timestamp, raw candle count, synthetic no-trade candles, and large gaps. Do not compare strategies across runs with materially different data coverage.

### Independent contestants

- `trend_following`
- `mean_reversion`
- `momentum`
- `breakout`
- `volatility_expansion`
- `range_rotation`
- `existing_adaptive_baseline` — current strategy, unchanged, as benchmark

Each strategy/asset pair receives its own $1,000 virtual account. No contestant can read or alter another contestant's balance, positions or signals. All use the same candles, closed-candle signal timing, next-open fill, fee, slippage, position sizing, stop, target and time limit. If stop and target are both touched in one candle, stop is assumed first. This first experiment holds execution and exits constant to isolate entry logic; exit-rule competitions should be a separately versioned second experiment.

### Market-regime intelligence

Each signal is labeled using only candles already closed:

- trend: bullish, bearish, sideways, transition, unknown;
- volatility: high, normal, low, unknown;
- combined labels such as `bullish_high`, `sideways_low`, `bearish_normal`.

The JSON report includes an overall leaderboard, performance by regime and asset, drawdown, trade count, win rate, average R, profit factor, and failure analysis: losing-trade exit reasons, regimes where losses began, MAE and MFE. Regime cells with few trades are insufficient evidence, not winners.

### Hybrid selector research component

`laboratory.analysis.select_strategy(past_trades, regime, as_of, min_trades=12)` selects only from trades closed before `as_of`, requires a minimum sample for that regime and strategy, and abstains (`None`) if evidence is insufficient. It is not connected to the Telegram bot. A future hybrid paper system must be evaluated walk-forward: at each historical decision, use only earlier closed trades, select for the current regime, then test on the next unseen period.

### Evaluation rules

- Never choose by raw return alone; consider drawdown, sample size, profit factor, average R and consistency across assets and periods.
- Separate development from chronological out-of-sample testing; use walk-forward folds and a final untouched holdout.
- Freeze each strategy version during a competition. Any change creates a new version and a new run.
- Regime-performance maps are descriptive, not proof of future predictive power.
- Results depend on data quality and the simplified execution model; they are not evidence of future performance.

## Existing bot diagnostics

`/diagnostics` reports completed-candle evaluations, strategy passes, paper entries, latest score/regime/setup, blockers and data errors. Diagnostics live under `diagnostics` in `state.json`; migration adds defaults without resetting wallet balance or open positions.

## Tests

```bash
python -m unittest -v
```

Synthetic tests verify code behavior only; they do not demonstrate profitability.
