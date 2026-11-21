"""Entry-zone activation on top of unchanged quantitative strategy signals.

A raw engine BUY/SELL creates a WATCH, never an immediate trading alert.
Entry needs a more favorable quote zone AND a new completed M1 rejection bar.
This heuristic is unvalidated and can miss winning moves. Signals only.
"""
from datetime import datetime,timezone
import math

ZONE_FRACTIONS={
    "Compression Breakout":(0.18,0.55),
    "Trend Pullback":(0.12,0.45),
    "Mean Reversion":(0.10,0.35),
}
MAX_WATCH_SECONDS=600
ENTRY_VALID_SECONDS=45
MIN_OBSERVATION_SECONDS=55

def iso_utc(ts):
    return datetime.fromtimestamp(float(ts),timezone.utc).isoformat()

def create_watch(setup,market,mode,account_id,now,spread):
    if not isinstance(setup,dict) or setup.get("side") not in ("BUY","SELL"):
        return None
    strategy=setup.get("strategy")
    if strategy not in ZONE_FRACTIONS:return None
    try:
        price=float(setup["entry"]);stop=float(setup["stop"]);ratio=float(setup["rr"])
        t=int(setup["bar_time"]);spread=float(spread)
    except (KeyError,TypeError,ValueError,OverflowError):return None
    if not all(math.isfinite(v) for v in (price,stop,ratio,spread)):return None
    if price<=0 or stop<=0 or spread<0 or not 1<=ratio<=3:return None
    risk=abs(price-stop);side=setup["side"]
    if (side=="SELL" and stop<=price) or (side=="BUY" and stop>=price):return None
    if risk<max(6*spread,1e-6):return None
    low_fraction,high_fraction=ZONE_FRACTIONS[strategy]
    if side=="SELL":
        low=price+low_fraction*risk
        high=price+high_fraction*risk
    else:
        low=price-high_fraction*risk
        high=price-low_fraction*risk
    mid=(low+high)/2
    if low<=0 or high<=low or high-low<1.7*spread:return None
    if side=="SELL" and stop-high<max(2*spread,0.2*risk):return None
    if side=="BUY" and low-stop<max(2*spread,0.2*risk):return None
    return {"market":market,"side":side,"strategy":strategy,"mode":mode,
      "account_id":int(account_id),"setup_reason":str(setup.get("reason") or ""),
      "origin_bar_time":t,"created_ts":float(now),"created_at":iso_utc(now),
      "expires_ts":float(now)+MAX_WATCH_SECONDS,
      "initial_entry":round(price,5),"stop":round(stop,5),
      "original_risk":risk,"rr":ratio,
      "zone_lower":round(low,5),"zone_upper":round(high,5),
      "zone_mid":round(mid,5),"reference_spread":spread}

def public_watch(w):
    if not w:return None
    keys=("side","market","strategy","mode","created_at","zone_lower",
          "zone_upper","initial_entry","stop","setup_reason")
    return {k:w[k] for k in keys if k in w}|{
      "expires_at":iso_utc(w["expires_ts"]),"action":"WAIT_ZONE",
      "notice":"ترقّب فقط: لا تدخل حتى تأكيد إعادة الاختبار."}

def assess_watch(w,bars_m1,bid,ask,now,spread):
    """WAIT/CANCEL/READY; use executable-side quotes and completed candles only."""
    try:
        side=w["side"];stop=float(w["stop"]);now=float(now)
        bid=float(bid);ask=float(ask);spread=float(spread)
        low=float(w["zone_lower"]);high=float(w["zone_upper"])
        mid=float(w["zone_mid"]);created=float(w["created_ts"])
        expires=float(w["expires_ts"]);original_risk=float(w["original_risk"])
    except (TypeError,KeyError,ValueError,OverflowError):
        return {"action":"CANCEL","reason":"بيانات منطقة الدخول غير صالحة"}
    if now>=expires:return {"action":"CANCEL","reason":"انتهت مهلة إعادة الاختبار دون تأكيد صالح"}
    if not all(math.isfinite(v) for v in (now,bid,ask,spread,stop,low,high)):
        return {"action":"WAIT","reason":"بيانات السعر غير صالحة"}
    if bid<=0 or ask<=bid or spread<0:
        return {"action":"WAIT","reason":"بانتظار Bid/Ask صالحين"}
    if (side=="SELL" and ask>=stop) or (side=="BUY" and bid<=stop):
        return {"action":"CANCEL","reason":"وصل السعر إلى مستوى الإلغاء قبل تأكيد الدخول؛ لا تدخل"}
    if spread>max(1.7*float(w["reference_spread"]),0.05*original_risk):
        return {"action":"WAIT","reason":"توسع السبريد؛ يمنع الدخول رغم اكتمال التحليل"}
    if now-created<MIN_OBSERVATION_SECONDS:
        return {"action":"WAIT","reason":"انتظار اختبار المنطقة بعد إشارة التحليل الأصلية"}
    if not bars_m1:return {"action":"WAIT","reason":"بانتظار شمعة M1 مغلقة بعد الإشارة"}
    bar=bars_m1[-1]
    try:
        opened=float(bar["o"]);closed=float(bar["c"]);high_price=float(bar["h"])
        low_price=float(bar["l"]);bar_time=int(bar["t"])
    except (KeyError,TypeError,ValueError):
        return {"action":"WAIT","reason":"الشمعة الأخيرة غير مكتملة البيانات"}
    if bar_time<=int(w["origin_bar_time"]):
        return {"action":"WAIT","reason":"لا يمكن الدخول من نفس شمعة الإشارة الأصلية"}
    bar_closed_at=bar_time/1000+60
    if now<bar_closed_at or now-bar_closed_at>95:
        return {"action":"WAIT","reason":"بانتظار شمعة تأكيد حديثة ومكتملة"}
    if (side=="SELL" and high_price>=stop) or (side=="BUY" and low_price<=stop):
        return {"action":"CANCEL","reason":"الشمعة وصلت إلى مستوى الإلغاء قبل تأكيد الدخول"}
    min_body=max(spread*0.9,0.045*original_risk)
    body=abs(closed-opened)
    if side=="SELL":
        rejected=(high_price>=mid and low<=closed<=mid and
                  closed<opened and body>=min_body)
        execution=bid
        in_zone=low<=execution<=mid
    else:
        rejected=(low_price<=mid and mid<=closed<=high and
                  closed>opened and body>=min_body)
        execution=ask
        in_zone=mid<=execution<=high
    if not rejected:
        return {"action":"WAIT","reason":"لم تظهر شمعة رفض مؤكدة داخل منطقة الدخول؛ لا تدخل"}
    if not in_zone:
        return {"action":"WAIT","reason":"شمعة التأكيد ظهرت لكن سعر التنفيذ خارج المنطقة؛ لا تطارد السعر"}
    risk=stop-execution if side=="SELL" else execution-stop
    if risk<=max(2.5*spread,0.12*original_risk):
        return {"action":"CANCEL","reason":"الوقف قريب من سعر الدخول بعد الارتداد؛ تم إلغاء الفرصة"}
    rr=float(w["rr"])
    target=execution-rr*risk if side=="SELL" else execution+rr*risk
    return {"action":"READY","signal":{
      "side":side,"market":w["market"],"strategy":w["strategy"],
      "reason":w["setup_reason"]+" • تأكد الدخول بعد إعادة اختبار المنطقة ورفض M1",
      "entry":round(execution,5),"stop":round(stop,5),"target":round(target,5),
      "rr":rr,"bar_time":bar_time,"initial_signal_bar_time":int(w["origin_bar_time"]),
      "setup_created_at":w["created_at"],"zone_lower":round(low,5),
      "zone_upper":round(high,5),"zone_confirmed_at":iso_utc(now),
      "valid_until":iso_utc(now+ENTRY_VALID_SECONDS),
      "confirmation":"M1 retest + rejection candle + executable price in zone",
      "entry_timing_version":"retest_v1",
      "notice":"إشارة تجريبية مشروطة بصلاحية المنطقة؛ ليست ضمان ربح."}}

def still_actionable(rec,bid,ask,now):
    if not rec or rec.get("side") not in ("BUY","SELL"):return False
    try:
        until=datetime.fromisoformat(rec["valid_until"]).timestamp()
        now=float(now);bid=float(bid);ask=float(ask)
        entry=float(rec["entry"]);stop=float(rec["stop"])
        low=float(rec["zone_lower"]);high=float(rec["zone_upper"])
    except (KeyError,ValueError,TypeError):return False
    execution=ask if rec["side"]=="BUY" else bid
    return (now<=until and bid>0 and ask>bid and
            low<=execution<=high and abs(execution-entry)<=0.20*abs(entry-stop))
