"""Descriptive paper-signal accuracy at the time of emission.

This is NOT the probability of success of the next trade. Only theoretical
paper positions that have reached TARGET or STOP count in the denominator.
Open/UNVERIFIED positions are excluded and cannot be labeled as wins.
"""
import math
from paper_journal import infer_mode

Z_95 = 1.959963984540054

def _wilson(wins, total):
    if total <= 0:
        return None, None
    proportion = wins / total
    z2 = Z_95 ** 2
    denom = 1 + z2 / total
    center = (proportion + z2 / (2 * total)) / denom
    span = (Z_95 / denom) * math.sqrt(
        proportion * (1 - proportion) / total + z2 / (4 * total ** 2)
    )
    return round(100 * max(0.0, center - span), 1), round(100 * min(1.0, center + span), 1)

def accuracy_snapshot(records, account_id, market, mode, strategy):
    """Snapshot from history BEFORE storing a new signal; never look ahead."""
    eligible = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("account_id")) != str(account_id):
            continue
        if item.get("market") != market or infer_mode(item) != mode:
            continue
        if item.get("strategy") != strategy:
            continue
        eligible.append(item)
    settled = [x for x in eligible if x.get("status") in ("TARGET","STOP")]
    wins = sum(1 for x in settled if x.get("status") == "TARGET")
    n = len(settled)
    low,high = _wilson(wins,n)
    return {
        "strategy": strategy,
        "market": market,
        "mode": mode,
        "wins": wins,
        "losses": n - wins,
        "resolved": n,
        "open": sum(1 for x in eligible if x.get("status","OPEN") == "OPEN"),
        "unverified": sum(1 for x in eligible if x.get("status") == "UNVERIFIED"),
        "win_rate_pct": round(100 * wins / n, 1) if n else None,
        "ci_lower_pct": low,
        "ci_upper_pct": high,
        "sample_status": "no_data" if not n else "small" if n < 20 else "limited" if n < 50 else "observational",
        "paper_only": True,
        "source": "last_200_server_paper_signals",
    }
