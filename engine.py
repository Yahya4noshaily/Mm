"""Independent systematic signal engine. Signals are setups, NOT estimated win probabilities."""
import statistics

PRESETS = {
    "active": {"rr": 1.3, "risk_pct": 0.25, "label": "نشط"},
    "conservative": {"rr": 2.0, "risk_pct": 0.15, "label": "محافظ"},
}

def ema(xs, n):
    if not xs: return []
    k = 2/(n+1)
    out = [float(xs[0])]
    for x in xs[1:]: out.append(k*x + (1-k)*out[-1])
    return out

def rsi(closes, n=14):
    if len(closes) < n+2: return None
    chg = [closes[i] - closes[i-1] for i in range(1,len(closes))]
    up = sum(max(x,0) for x in chg[:n])/n
    down = sum(max(-x,0) for x in chg[:n])/n
    for d in chg[n:]:
        up = ((n-1)*up + max(d,0))/n
        down = ((n-1)*down + max(-d,0))/n
    return 100 if down == 0 else 100 - 100/(1+up/down)

def atr_series(bars,n=14):
    if len(bars)<n+2:return []
    tr=[]
    for i in range(1,len(bars)):
        b=bars[i];prev=bars[i-1]
        tr.append(max(b['h']-b['l'],abs(b['h']-prev['c']),abs(b['l']-prev['c'])))
    sm=sum(tr[:n])/n
    vals=[None]*(n) + [sm]
    for v in tr[n:]: sm=((n-1)*sm+v)/n;vals.append(sm)
    return vals

def adx(bars,n=14):
    """Wilder ADX on completed bars only; None while insufficient bars."""
    if len(bars)<2*n+2:return None
    tr=[];pd=[];md=[]
    for i in range(1,len(bars)):
        cur,prev=bars[i],bars[i-1]
        rise=cur['h']-prev['h'];fall=prev['l']-cur['l']
        pd.append(rise if rise>fall and rise>0 else 0)
        md.append(fall if fall>rise and fall>0 else 0)
        tr.append(max(cur['h']-cur['l'],abs(cur['h']-prev['c']),abs(cur['l']-prev['c'])))
    atrv=sum(tr[:n]);pdm=sum(pd[:n]);mdm=sum(md[:n]);dx=[]
    for i in range(n-1,len(tr)):
        if i>=n:
            atrv=atrv-atrv/n+tr[i];pdm=pdm-pdm/n+pd[i];mdm=mdm-mdm/n+md[i]
        plus=100*pdm/atrv if atrv else 0;minus=100*mdm/atrv if atrv else 0
        dx.append(100*abs(plus-minus)/(plus+minus) if (plus+minus)>0 else 0)
    if len(dx)<n:return None
    ans=sum(dx[:n])/n
    for d in dx[n:]:ans=((n-1)*ans+d)/n
    return ans

def choose_setup(m1,m5,mode='active'):
    """Returns potential (side, method, reason) or (None, regime, reason)."""
    if len(m1)<145 or len(m5)<65:return None,'WARMUP','الشموع التاريخية غير كافية بعد'
    cls=[b['c'] for b in m1]; c5=[b['c'] for b in m5]
    trend_adx=adx(m5)
    if trend_adx is None:return None,'WARMUP','مؤشر الاتجاه غير جاهز'
    cur,prev=m1[-1],m1[-2]
    # Same three strategies for both presets; conservative requires stronger confirmation.
    conservative = mode == 'conservative'
    squeeze_ratio = 0.70 if conservative else 0.85
    range_adx_limit = 16 if conservative else 20
    trend_adx_min = 24 if conservative else 20
    z_threshold = 2.2 if conservative else 1.65
    rsi_buy_limit = 30 if conservative else 38
    rsi_sell_limit = 70 if conservative else 62
    at=atr_series(m1)
    volatility=at[-1]
    if not volatility or volatility<=0:return None,'WAIT','تعذر قياس التذبذب'
    for j in range(max(21,len(m1)-(4 if conservative else 5)),len(m1)-1):
        prior_atrs=[a for a in at[max(0,j-101):j-1] if a is not None]
        if len(prior_atrs)<80 or at[j-1] is None:continue
        is_compressed=at[j-1] < squeeze_ratio*statistics.mean(prior_atrs)
        if not is_compressed:continue
        upper=max(b['h'] for b in m1[j-20:j]);lower=min(b['l'] for b in m1[j-20:j]);bar=m1[j]
        if bar['c']>upper and cur['l']<=upper and cur['c']>upper and cur['c']>cur['o']:
            return 'BUY','Compression Breakout','اختراق نطاق منكمش مع إعادة اختبار صاعدة'
        if bar['c']<lower and cur['h']>=lower and cur['c']<lower and cur['c']<cur['o']:
            return 'SELL','Compression Breakout','اختراق نطاق منكمش مع إعادة اختبار هابطة'
    if trend_adx<range_adx_limit:
        n=30;avg=statistics.mean(cls[-n:]);sd=statistics.pstdev(cls[-n:])
        z=(cls[-1]-avg)/sd if sd else 0
        mom=rsi(cls)
        if mom is not None and z<=-z_threshold and mom<rsi_buy_limit and cur['c']>cur['o'] and cur['c']>prev['c']:
            return 'BUY','Mean Reversion',f'ارتداد صاعد من انحراف إحصائي {z:.2f} و RSI {mom:.1f}'
        if mom is not None and z>=z_threshold and mom>rsi_sell_limit and cur['c']<cur['o'] and cur['c']<prev['c']:
            return 'SELL','Mean Reversion',f'ارتداد هابط من انحراف إحصائي {z:.2f} و RSI {mom:.1f}'
        return None,'RANGE',f'سوق عرضي؛ لم تؤكد شروط الارتداد (ADX {trend_adx:.1f})'
    if trend_adx>=trend_adx_min:
        e21=ema(c5,21);e55=ema(c5,55);e20=ema(cls,20)
        buy=e21[-1]>e55[-1] and e21[-1]>e21[-3] and prev['l']<=e20[-2]+0.1*volatility and cur['c']>max(prev['h'],e20[-1])
        sell=e21[-1]<e55[-1] and e21[-1]<e21[-3] and prev['h']>=e20[-2]-0.1*volatility and cur['c']<min(prev['l'],e20[-1])
        if buy:return 'BUY','Trend Pullback','تصحيح EMA20 ثم تأكيد استمرار الاتجاه الصاعد'
        if sell:return 'SELL','Trend Pullback','تصحيح EMA20 ثم تأكيد استمرار الاتجاه الهابط'
        return None,'TREND',f'سوق اتجاهي؛ لم تكتمل شروط التصحيح (ADX {trend_adx:.1f})'
    return None,'NEUTRAL',f'منطقة انتقالية؛ لا توجد أفضلية واضحة (ADX {trend_adx:.1f})'

def signal(m1,m5,bid,ask,spreads,mode='active'):
    """Expects sorted, CLOSED bars. Bid/ask are executable side prices."""
    if mode not in PRESETS:mode='active'
    if not bid or not ask or ask<=bid:return {'side':'WAIT','reason':'لا يوجد سعر تنفيذ صالح'}
    if len(spreads)>=8 and ask-bid>1.5*statistics.median(spreads):
        return {'side':'WAIT','reason':'السبريد الحالي أعلى من 1.5 ضعف متوسطه الحديث'}
    side,regime,reason=choose_setup(m1,m5,mode)
    if side is None:return {'side':'WAIT','regime':regime,'reason':reason}
    atrs=atr_series(m1); at=atrs[-1] if atrs else None
    if not at or at<=0:return {'side':'WAIT','reason':'ATR غير صالح'}
    entry=ask if side=='BUY' else bid
    stop=(min(x['l'] for x in m1[-5:])-0.2*at) if side=='BUY' else (max(x['h'] for x in m1[-5:])+0.2*at)
    risk=(entry-stop) if side=='BUY' else (stop-entry)
    if risk<=0 or risk>3*at:return {'side':'WAIT','regime':regime,'reason':'موضع الوقف لا يحقق شروط المخاطرة'}
    rr=PRESETS[mode]['rr'];take=entry+(rr*risk if side=='BUY' else -rr*risk)
    if abs(take-entry)<=2*(ask-bid):return {'side':'WAIT','regime':regime,'reason':'الهدف قريب جدًا مقارنة بتكاليف السبريد'}
    return {
        'side':side,'strategy':regime,'reason':reason,'entry':round(entry,5),
        'stop':round(stop,5),'target':round(take,5),'rr':rr,
        'risk_pct':PRESETS[mode]['risk_pct'],'bar_time':m1[-1]['t'],
        'notice':'إشارة شروط فنية وليست ضمان ربح أو نسبة نجاح مثبتة'
    }
