"""Equal-conditions, isolated paper simulator for strategy competition."""
from .regimes import classify_regime
from .strategies import STRATEGIES, atr

START_EQUITY = 1000.0
RISK_FRACTION = 0.0075
MAX_NOTIONAL_FRACTION = 0.30
STOP_ATR = 2.5
TARGET_R = 2.0
MAX_HOLD_BARS = 30
FEE = 0.0026
SLIPPAGE = 0.0007


def _close_trade(position, raw_exit, reason, equity, ts):
    exit_price = raw_exit * (1 - SLIPPAGE)
    gross = position["qty"] * (exit_price - position["entry"])
    fees = position["qty"] * (position["entry"] + exit_price) * FEE
    net = gross - fees
    risk_cash = max(position["risk_cash"], 1e-12)
    trade = {"pair": position["pair"], "opened": position["opened"], "closed": ts,
             "entry": position["entry"], "exit": exit_price, "net": net,
             "r": net / risk_cash, "regime": position["regime"], "reason": reason,
             "mae_r": position["mae_r"], "mfe_r": position["mfe_r"],
             "bars": position["bars"]}
    return equity + net, trade


def simulate_asset(candles, pair, strategy_name, signal_fn, regime_cache=None):
    """Signal at a completed close, fill next open; never re-enter on an exit bar."""
    equity, peak, max_dd = START_EQUITY, START_EQUITY, 0.0
    position, trades = None, []
    curve = []
    for i in range(210, len(candles)):
        bar = candles[i]
        had_position_at_bar_start = position is not None
        if position:
            position["bars"] += 1
            risk_per_unit = max(position["entry"] - position["stop"], 1e-12)
            position["mae_r"] = min(position["mae_r"], (bar["l"] - position["entry"]) / risk_per_unit)
            position["mfe_r"] = max(position["mfe_r"], (bar["h"] - position["entry"]) / risk_per_unit)
            hit_stop = bar["l"] <= position["stop"]
            hit_target = bar["h"] >= position["target"]
            if hit_stop or hit_target or position["bars"] >= MAX_HOLD_BARS:
                if hit_stop:
                    # A stop crossed by an opening gap fills at the worse opening price.
                    raw_exit = min(bar["o"], position["stop"])
                    reason = "stop (priority if both touched)"
                elif hit_target:
                    raw_exit, reason = position["target"], "target"
                else:
                    raw_exit, reason = bar["c"], "time_exit"
                equity, trade = _close_trade(position, raw_exit, reason, equity, bar["t"])
                trades.append(trade)
                position = None

        # A position closed intrabar cannot be replaced at that same bar's already-passed open.
        if position is None and not had_position_at_bar_start:
            history = candles[:i]
            regime = regime_cache[i] if regime_cache is not None else classify_regime(history)
            if signal_fn(history, regime):
                signal_atr = atr(history)
                if signal_atr and signal_atr > 0:
                    entry = bar["o"] * (1 + SLIPPAGE)
                    stop = entry - STOP_ATR * signal_atr
                    planned_risk = equity * RISK_FRACTION
                    qty = min(planned_risk / (entry - stop),
                              equity * MAX_NOTIONAL_FRACTION / entry)
                    if qty > 0 and entry > stop:
                        actual_risk = qty * (entry - stop)
                        position = {"pair": pair, "opened": bar["t"], "entry": entry,
                                    "stop": stop, "target": entry + (entry - stop) * TARGET_R,
                                    "qty": qty, "risk_cash": actual_risk, "regime": regime["regime"],
                                    "mae_r": 0.0, "mfe_r": 0.0, "bars": 0}
        marked = equity
        if position:
            marked += position["qty"] * (bar["c"] - position["entry"])
        peak = max(peak, marked)
        max_dd = max(max_dd, (peak - marked) / peak * 100 if peak else 0.0)
        curve.append({"t": bar["t"], "equity": marked})
    if position:
        equity, trade = _close_trade(position, candles[-1]["c"], "end_of_data", equity, candles[-1]["t"])
        trades.append(trade)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0.0)
        curve.append({"t": candles[-1]["t"], "equity": equity})
    return {"strategy": strategy_name, "pair": pair, "start_equity": START_EQUITY,
            "final_equity": equity, "return_pct": (equity / START_EQUITY - 1) * 100,
            "max_drawdown_pct": max_dd, "trades": trades, "equity_curve": curve}


def run_competition(data, strategies=None):
    selected = strategies or STRATEGIES
    results = []
    for pair, candles in sorted(data.items()):
        if len(candles) < 211:
            continue
        # Regime is a property of the market history, not of a contestant.
        regime_cache = {i: classify_regime(candles[:i]) for i in range(210, len(candles))}
        for strategy_name, signal_fn in selected.items():
            results.append(simulate_asset(candles, pair, strategy_name, signal_fn, regime_cache))
    return results
