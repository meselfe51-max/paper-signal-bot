"""Performance matrices, weakness diagnostics and causal hybrid strategy selection."""
from collections import defaultdict
from math import inf


def _metrics(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate_pct": 0.0, "net_pnl": 0.0,
                "avg_r": 0.0, "profit_factor": None}
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [-t["net"] for t in trades if t["net"] < 0]
    gross_loss = sum(losses)
    return {"trades": n, "win_rate_pct": len(wins) / n * 100,
            "net_pnl": sum(t["net"] for t in trades),
            "avg_r": sum(t["r"] for t in trades) / n,
            "profit_factor": sum(wins) / gross_loss if gross_loss else (inf if wins else None)}


def build_report(results):
    by_strategy, by_regime, by_pair, failures = {}, {}, {}, {}
    strategies = sorted({r["strategy"] for r in results})
    all_trades = {s: [t for r in results if r["strategy"] == s for t in r["trades"]] for s in strategies}
    for strategy, trades in all_trades.items():
        runs = [r for r in results if r["strategy"] == strategy]
        by_strategy[strategy] = {**_metrics(trades),
            "mean_asset_return_pct": sum(r["return_pct"] for r in runs) / len(runs) if runs else 0,
            "worst_asset_drawdown_pct": max((r["max_drawdown_pct"] for r in runs), default=0),
            "assets_tested": len(runs)}
        buckets = defaultdict(list)
        for trade in trades: buckets[trade["regime"]].append(trade)
        by_regime[strategy] = {regime: _metrics(items) for regime, items in sorted(buckets.items())}
        pairs = defaultdict(list)
        for trade in trades: pairs[trade["pair"]].append(trade)
        by_pair[strategy] = {pair: _metrics(items) for pair, items in sorted(pairs.items())}
        losses = [t for t in trades if t["net"] < 0]
        reason_counts = defaultdict(int)
        regime_counts = defaultdict(int)
        for trade in losses:
            reason_counts[trade["reason"]] += 1
            regime_counts[trade["regime"]] += 1
        failures[strategy] = {"losing_trades": len(losses),
            "loss_exit_reasons": dict(sorted(reason_counts.items())),
            "loss_entry_regimes": dict(sorted(regime_counts.items())),
            "worst_mae_r": min((t["mae_r"] for t in trades), default=0),
            "average_mfe_r": sum(t["mfe_r"] for t in trades) / len(trades) if trades else 0}
    return {"methodology": {"paper_only": True, "signal_timing": "closed candle; next candle open fill",
            "isolated_account_per_strategy_and_asset": True,
            "common_execution": {"fee_per_side": 0.0026, "slippage_per_side": 0.0007,
                "stop_atr": 2.5, "target_r": 2.0, "max_hold_bars": 30},
            "selection_warning": "Use chronological walk-forward validation; do not choose from in-sample results."},
        "leaderboard": by_strategy, "performance_by_regime": by_regime,
        "performance_by_asset": by_pair, "failure_analysis": failures}


def select_strategy(past_trades, regime, as_of, min_trades=12):
    """Choose using only trades closed before as_of, for the exact regime. None means abstain."""
    candidates = defaultdict(list)
    for trade in past_trades:
        if trade.get("closed", as_of) < as_of and trade.get("regime") == regime:
            candidates[trade["strategy"]].append(trade)
    ranked = []
    for name, trades in candidates.items():
        if len(trades) < min_trades:
            continue
        rs = [float(t["r"]) for t in trades]
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for value in rs:
            equity += value
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        score = sum(rs) / len(rs) - 0.20 * max_dd / len(rs)
        ranked.append((score, name))
    return max(ranked)[1] if ranked else None
