"""Paper-only signal outcomes and auditable performance.

No orders, position size, commissions or actual fills exist here. Live outcomes
use executable-side cTrader spot BID for a BUY exit and ASK for a SELL exit.
Historical outcomes use completed M1 OHLC bars and cannot establish an exact
intra-candle hit time. On ambiguous bars STOP takes priority. Missing bars
are flagged UNVERIFIED rather than filled in optimistically.
"""
from datetime import datetime, timezone
import math

OUTCOMES = {"OPEN", "TARGET", "STOP", "UNVERIFIED"}
MODES = {"active": "هجومي", "conservative": "محافظ"}
STRATEGIES = ("Trend Pullback", "Mean Reversion", "Compression Breakout")


def utc_iso_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def infer_mode(row):
    value = row.get("mode")
    if value in MODES:
        return value
    label = str(row.get("mode_label", ""))
    if label in ("هجومي", "نشط"):
        return "active"
    if label == "محافظ":
        return "conservative"
    try:
        ratio = float(row.get("rr"))
    except (ValueError, TypeError):
        return "unknown"
    if abs(ratio - 1.3) < 0.02:
        return "active"
    if abs(ratio - 2.0) < 0.02:
        return "conservative"
    return "unknown"


def planned_risk_pct(row):
    try:
        n = float(row.get("risk_pct"))
        if 0 < n <= 2:
            return n
    except (ValueError, TypeError):
        pass
    # Used only for explicitly labeled legacy presets.
    mode = infer_mode(row)
    return 0.25 if mode == "active" else 0.15 if mode == "conservative" else None


def _valid_levels(row):
    try:
        e = float(row["entry"])
        s = float(row["stop"])
        t = float(row["target"])
        if not all(math.isfinite(x) and 0 < x < 10**10 for x in (e,s,t)):
            return False
        return (s < e < t) if row.get("side") == "BUY" else (t < e < s)
    except (KeyError, ValueError, TypeError):
        return False


def _finalize(row, outcome, closed_at, source, observed_price=None,
              ambiguous=False):
    """Finalize exactly once, preserving provenance and theoretical P/L."""
    if row.get("status", "OPEN") != "OPEN":
        return False
    if outcome not in ("TARGET", "STOP") or not _valid_levels(row):
        return False
    entry = float(row["entry"])
    target = float(row["target"])
    stop = float(row["stop"])
    exit_price = target if outcome == "TARGET" else stop
    risk_per_unit = abs(entry - stop)
    signed_move = (exit_price - entry) * (1 if row["side"] == "BUY" else -1)
    result_r = round(signed_move / risk_per_unit, 5)
    row["status"] = outcome
    row["closed_at"] = closed_at
    row["exit_price"] = round(exit_price, 6)
    row["observed_exit_quote"] = round(float(observed_price), 6) if observed_price is not None else None
    row["result_r"] = result_r
    row["price_move"] = round(signed_move, 6)
    row["price_move_pct"] = round(100 * signed_move / entry, 5)
    risk = planned_risk_pct(row)
    row["risk_pct"] = risk
    row["theoretical_equity_pct"] = round(result_r * risk, 5) if risk is not None else None
    row["resolution_source"] = source
    row["ambiguous_candle"] = bool(ambiguous)
    row["outcome_reason"] = (
        "الوقف والهدف لُمسا في الشمعة نفسها؛ اعتُمد الوقف تحفظًا"
        if ambiguous else (
            "وصول سعر الخروج النظري وفق تحديث BID/ASK مباشر؛ ليس تنفيذًا حقيقيًا"
            if source == "spot" else
            "تقييم نظري بشمعة M1 مكتملة؛ وقت اللمس داخل الدقيقة غير معروف"
        )
    )
    return True


def settle_on_quote(journal, market, bid=None, ask=None, received_at=None,
                    changed_side=None, account_id=None):
    """Close paper signals on LIVE executable-side quotes.

    Only evaluate fresh updates of BID for a long or ASK for a short.
    Returns NEWLY closed records for optional independent notifications.
    """
    try:
        quote_time = float(received_at)
    except (ValueError, TypeError):
        return []
    if not math.isfinite(quote_time) or quote_time <= 0:
        return []
    changed = []
    for row in journal:
        if row.get("market") != market or row.get("status", "OPEN") != "OPEN":
            continue
        if account_id is not None and str(row.get("account_id")) != str(account_id):
            continue
        if not _valid_levels(row):
            continue
        side = row["side"]
        if changed_side and changed_side != ("bid" if side == "BUY" else "ask"):
            continue
        try:
            issued = datetime.fromisoformat(row.get("created_at", "")).timestamp()
        except (ValueError, TypeError):
            continue
        # Do not attribute a pre-entry spot quote to a new signal.
        if quote_time < issued:
            continue
        # A restored order must be reconciled with historical M1 bars first,
        # otherwise a restart might misattribute an old hit to today's quote.
        cursor = int(row.get("checked_bar_time") or row.get("bar_time") or 0)
        if cursor and quote_time * 1000 - (cursor + 60000) > 180000:
            continue
        raw = bid if side == "BUY" else ask
        try:
            price = float(raw)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        stop = float(row["stop"])
        target = float(row["target"])
        stop_hit = price <= stop if side == "BUY" else price >= stop
        target_hit = price >= target if side == "BUY" else price <= target
        if stop_hit or target_hit:
            outcome = "STOP" if stop_hit else "TARGET"
            if _finalize(row,outcome,
                         datetime.fromtimestamp(quote_time,tz=timezone.utc).isoformat(),
                         "spot",price):
                changed.append(row.copy())
    return changed


def update_paper_journal(journal, market, closed_bars, account_id=None):
    """Reconcile past bars without making up any data across gaps."""
    if not closed_bars:
        return []
    bars=sorted(closed_bars,key=lambda b: int(b["t"]))
    changed=[]
    for row in journal:
        if row.get("market")!=market or row.get("status","OPEN")!="OPEN":
            continue
        if account_id is not None and str(row.get("account_id"))!=str(account_id):
            continue
        if not _valid_levels(row):
            continue
        start=int(row.get("bar_time") or 0)
        if start<=0:
            continue
        last=int(row.get("checked_bar_time") or start)
        last=max(last,start)
        upcoming=[b for b in bars if int(b["t"])>last]
        if not upcoming:
            # Historical API has not yet provided a NEW completed candle.
            # Do not silently close or mark a winner.
            if int(bars[0]["t"]) > last+60000:
                row["status"]="UNVERIFIED"
                row["outcome_reason"]="شموع تاريخية مفقودة بعد توقف المراقبة؛ لا يمكن حسم الصفقة"
                row["closed_at"]=None
                changed.append(row.copy())
            continue
        for bar in upcoming:
            bar_time=int(bar["t"])
            if bar_time-last>180000:
                row["status"]="UNVERIFIED"
                row["closed_at"]=None
                row["outcome_reason"]="فجوة في تسلسل الشموع؛ لا يمكن تحديد إن كان السعر لمس الوقف أو الهدف"
                changed.append(row.copy())
                break
            row["checked_bar_time"]=bar_time
            last=bar_time
            stop=float(row["stop"])
            target=float(row["target"])
            if row["side"]=="BUY":
                stop_hit=float(bar["l"])<=stop
                target_hit=float(bar["h"])>=target
            else:
                stop_hit=float(bar["h"])>=stop
                target_hit=float(bar["l"])<=target
            if stop_hit or target_hit:
                ambiguous=bool(stop_hit and target_hit)
                source="m1_candle"
                if _finalize(row,"STOP" if stop_hit else "TARGET",
                             utc_iso_ms(bar_time+60000),source,None,ambiguous):
                    changed.append(row.copy())
                break
    return changed


def _stat_rows(rows):
    overall={"total":len(rows),"open":0,"unverified":0,"wins":0,
             "losses":0,"resolved":0,"win_rate_pct":None,"net_r":0.0,
             "avg_r":None,"theoretical_equity_pct":None,
             "equity_sample_count":0}
    net=0.0
    pct=0.0
    for row in rows:
        status=row.get("status","OPEN")
        if status=="OPEN":
            overall["open"]+=1
        elif status=="UNVERIFIED":
            overall["unverified"]+=1
        elif status in ("TARGET","STOP"):
            overall["resolved"]+=1
            overall["wins" if status=="TARGET" else "losses"]+=1
            try:
                r=float(row["result_r"])
            except (KeyError,ValueError,TypeError):
                continue
            if math.isfinite(r):
                net+=r
                risk=planned_risk_pct(row)
                if risk is not None:
                    pct+=r*risk
                    overall["equity_sample_count"]+=1
    if overall["resolved"]:
        overall["win_rate_pct"]=round(100*overall["wins"]/overall["resolved"],2)
        overall["avg_r"]=round(net/overall["resolved"],3)
    overall["net_r"]=round(net,3)
    if overall["equity_sample_count"]:
        overall["theoretical_equity_pct"]=round(pct,4)
    return overall


def compute_performance(journal, account_id=None):
    """Calculate honest, resolution-only success rates by mode and strategy."""
    rows=[r for r in journal if account_id is None or str(r.get("account_id"))==str(account_id)]
    by_mode={}
    by_strategy={}
    by_mode_strategy={}
    for row in rows:
        mode=infer_mode(row)
        strategy=str(row.get("strategy") or "غير محددة")
        by_mode.setdefault(mode,[]).append(row)
        by_strategy.setdefault(strategy,[]).append(row)
        key=mode+"|"+strategy
        by_mode_strategy.setdefault(key,[]).append(row)
    return {
        "overall":_stat_rows(rows),
        "by_mode":{key:_stat_rows(items) for key,items in by_mode.items()},
        "by_strategy":{key:_stat_rows(items) for key,items in by_strategy.items()},
        "by_mode_strategy":{key:_stat_rows(items) for key,items in by_mode_strategy.items()},
        "unit":"R",
        "disclaimer":"Theoretical paper performance from available spots and OHLC bars. Open/unverified excluded from win rate. No actual fills, costs or monetary P/L."
    }


def sanitize_client_backup(rows, account_id):
    """Use validated browser backup records only for the same cTrader account."""
    if not isinstance(rows,list):
        return []
    accepted=[]
    seen=set()
    for raw in rows[-200:]:
        if not isinstance(raw,dict):
            continue
        key=str(raw.get("id",""))
        if len(key)!=12 or any(c not in "0123456789abcdef" for c in key) or key in seen:
            continue
        if raw.get("side") not in ("BUY","SELL") or raw.get("market") not in ("XAUUSD","BTCUSD"):
            continue
        if str(raw.get("account_id"))!=str(account_id):
            continue
        try:
            row={
                "id":key,"account_id":int(account_id),
                "side":raw["side"],"market":raw["market"],
                "entry":float(raw["entry"]),"stop":float(raw["stop"]),
                "target":float(raw["target"]),"rr":float(raw["rr"]),
                "bar_time":int(raw["bar_time"]),
                "checked_bar_time":int(raw.get("checked_bar_time") or raw["bar_time"]),
            }
        except (TypeError,KeyError,ValueError,OverflowError):
            continue
        if not _valid_levels(row) or not 0<row["rr"]<20 or row["bar_time"]<1_500_000_000_000:
            continue
        row["status"]=raw.get("status") if raw.get("status") in OUTCOMES else "UNVERIFIED"
        row["created_at"]=str(raw.get("created_at",""))[:45]
        row["closed_at"]=str(raw.get("closed_at",""))[:45] if raw.get("closed_at") else None
        row["strategy"]=str(raw.get("strategy",""))[:90]
        row["reason"]=str(raw.get("reason",""))[:240]
        row["mode_label"]=str(raw.get("mode_label",""))[:25]
        row["mode"]=raw.get("mode") if raw.get("mode") in MODES else infer_mode(raw)
        row["risk_pct"]=planned_risk_pct({**raw,**row})
        row["ambiguous_candle"]=bool(raw.get("ambiguous_candle",False))
        row["outcome_reason"]=str(raw.get("outcome_reason",""))[:250]
        row["resolution_source"]=str(raw.get("resolution_source",""))[:40]
        for field in ("exit_price","observed_exit_quote","result_r","price_move",
                      "price_move_pct","theoretical_equity_pct"):
            value=raw.get(field)
            try:
                n=float(value) if value is not None else None
                row[field]=n if n is not None and math.isfinite(n) else None
            except (ValueError,TypeError):
                row[field]=None
        # Support old v1.2 backups that stored a settled outcome but
        # did not include all derived P/L fields.
        if row["status"] in ("TARGET","STOP"):
            exit_price=row["target"] if row["status"]=="TARGET" else row["stop"]
            signed_move=(exit_price-row["entry"])*(1 if row["side"]=="BUY" else -1)
            risk=abs(row["entry"]-row["stop"])
            row["exit_price"]=round(exit_price,6)
            row["result_r"]=round(signed_move/risk,5)
            row["price_move"]=round(signed_move,6)
            row["price_move_pct"]=round(signed_move/row["entry"]*100,5)
            rp=row["risk_pct"]
            row["theoretical_equity_pct"]=round(row["result_r"]*rp,5) if rp is not None else None
            if not row["closed_at"]:
                row["status"]="UNVERIFIED"
        seen.add(key)
        accepted.append(row)
    return accepted
