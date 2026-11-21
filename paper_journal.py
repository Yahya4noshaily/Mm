"""Paper-only journal: track strategy signals against later CLOSED M1 bars.

No orders are sent, and these hypothetical outcomes ignore real fills,
slippage and intrabar path. If both stop and target touch the same candle,
we deliberately count STOP first (conservative, not proof of execution).
"""
from datetime import datetime, timezone

ALLOWED_OUTCOMES = {'OPEN', 'TARGET', 'STOP', 'UNVERIFIED'}


def utc_iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def update_paper_journal(journal, market, closed_bars):
    if not closed_bars:
        return 0
    closed_bars = sorted(closed_bars, key=lambda item: int(item['t']))
    earliest = int(closed_bars[0]['t'])
    changed = 0

    for row in journal:
        if row.get('market') != market or row.get('status', 'OPEN') != 'OPEN':
            continue
        if row.get('side') not in ('BUY', 'SELL'):
            continue
        start = int(row.get('bar_time') or 0)
        cursor = int(row.get('checked_bar_time') or start)
        if start <= 0:
            continue
        # Historical gaps cannot be used to assert that a stop or target
        # was never hit. Flag uncertain, never fabricate a winner.
        if earliest > cursor + 60000:
            row['status'] = 'UNVERIFIED'
            row['closed_at'] = None
            row['outcome_reason'] = 'فجوة في تاريخ الشموع؛ لا يمكن التحقق من النتيجة'
            changed += 1
            continue
        entry = float(row['entry'])
        stop = float(row['stop'])
        target = float(row['target'])
        side = row['side']
        if side == 'BUY' and not (stop < entry < target):
            continue
        if side == 'SELL' and not (target < entry < stop):
            continue
        for bar in closed_bars:
            t = int(bar['t'])
            if t <= max(start, cursor):
                continue
            stop_hit = float(bar['l']) <= stop if side == 'BUY' else float(bar['h']) >= stop
            target_hit = float(bar['h']) >= target if side == 'BUY' else float(bar['l']) <= target
            row['checked_bar_time'] = t
            if stop_hit or target_hit:
                ambiguous = bool(stop_hit and target_hit)
                row['status'] = 'STOP' if stop_hit else 'TARGET'
                row['closed_at'] = utc_iso(t + 60000)
                row['exit_price'] = stop if stop_hit else target
                row['result_r'] = -1.0 if stop_hit else round(float(row.get('rr', 1.0)), 2)
                row['ambiguous_candle'] = ambiguous
                row['outcome_reason'] = (
                    'لم يمكن تحديد ترتيب الوقف والهدف في الشمعة نفسها؛ احتُسب الوقف أولًا'
                    if ambiguous else 'نتيجة نظرية بالاعتماد على نطاق الشمعة المكتملة'
                )
                changed += 1
                break
    return changed


def sanitize_client_backup(rows, account_id):
    """Restore only validated paper records for the SAME cTrader account."""
    if not isinstance(rows, list):
        return []
    accepted = []
    for item in rows[-200:]:
        if not isinstance(item, dict):
            continue
        ident = str(item.get('id', ''))
        if len(ident) != 12 or any(c not in '0123456789abcdef' for c in ident):
            continue
        if item.get('side') not in ('BUY', 'SELL'):
            continue
        if item.get('market') not in ('XAUUSD', 'BTCUSD'):
            continue
        if str(item.get('account_id') or '') != str(account_id):
            continue
        try:
            entry = float(item['entry'])
            stop = float(item['stop'])
            target = float(item['target'])
            rr = float(item['rr'])
            bar_time = int(item['bar_time'])
            checked = int(item.get('checked_bar_time', bar_time))
        except (ValueError, KeyError, TypeError):
            continue
        if not (0 < entry < 10**10 and 0 < stop < 10**10
                and 0 < target < 10**10 and 0 < rr < 20 and bar_time > 1_500_000_000_000):
            continue
        side = item['side']
        if side == 'BUY' and not (stop < entry < target):
            continue
        if side == 'SELL' and not (target < entry < stop):
            continue
        status = item.get('status', 'OPEN')
        if status not in ALLOWED_OUTCOMES:
            status = 'UNVERIFIED'
        safe = {
            'id': ident, 'side': side, 'market': item['market'],
            'account_id': int(account_id), 'entry': entry,
            'stop': stop, 'target': target, 'rr': rr,
            'bar_time': bar_time, 'checked_bar_time': checked,
            'status': status,
            'created_at': str(item.get('created_at', ''))[:40],
            'closed_at': str(item.get('closed_at', ''))[:40] if item.get('closed_at') else None,
            'strategy': str(item.get('strategy', ''))[:80],
            'reason': str(item.get('reason', ''))[:240],
            'mode_label': str(item.get('mode_label', ''))[:25],
            'exit_price': item.get('exit_price'),
            'result_r': item.get('result_r'),
            'ambiguous_candle': bool(item.get('ambiguous_candle', False)),
        }
        accepted.append(safe)
    return accepted
