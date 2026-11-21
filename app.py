"""Quantum Simons — independent read-only cTrader website. No order placement."""
import asyncio
import hmac
import json
import logging
import re
import os
import secrets
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from pydantic import BaseModel
from engine import PRESETS, signal
from telegram_alerts import TelegramAlerts, TelegramError
from session_store import COOKIE_NAME, attach_cookie, deserialize_auth, delete_cookie
from paper_journal import update_paper_journal, sanitize_client_backup, settle_on_quote, compute_performance
from scan_scheduler import can_start_scan, note_scan_attempt, note_scan_success, note_scan_failure, scan_diagnostics

app=FastAPI(title='Quantum Simons',docs_url=None,redoc_url=None)
logger=logging.getLogger('quantum-simons')
ROOT=Path(__file__).parent
ID=os.getenv('CTRADER_CLIENT_ID','').strip()
SECRET=os.getenv('CTRADER_CLIENT_SECRET','').strip()
OWNER_PASSWORD=os.getenv('SITE_PASSWORD','')
SITE_SECRET=os.getenv('SITE_SECRET','').strip() or secrets.token_urlsafe(32)
PUBLIC_URL=os.getenv('PUBLIC_URL','').rstrip('/')
signer=TimestampSigner(SITE_SECRET)
telegram=TelegramAlerts()
SERVER_INSTANCE_ID=uuid.uuid4().hex[:12]

memory={
 'token':None,'refresh_token':None,'token_expires':0,
 'accounts':[],'selected_account':None,'oauth_state':None,
 'connected':False,'error':'لم يتم ربط cTrader بعد',
 'phase':'idle','market_health':{},
 'mode':'active','history':[],'quotes':{},'candles':{},
 'decisions':{'XAUUSD':{'side':'WAIT','reason':'بانتظار الاتصال'},'BTCUSD':{'side':'WAIT','reason':'بانتظار الاتصال'}},
 'symbols':{},'last_seen':{},'last_signal_ts':{},'last_eval':{},
}
worker=None
auth_lock=asyncio.Lock()

def require_owner(req:Request):
    if not OWNER_PASSWORD:raise HTTPException(503,'كلمة مرور الموقع لم تُضبط بعد لدى الاستضافة')
    value=req.cookies.get('qs_session','')
    try:
        raw=signer.unsign(value,max_age=604800).decode()
        if raw!='owner':raise ValueError()
    except (BadSignature,SignatureExpired,ValueError):
        raise HTTPException(401,'يرجى تسجيل الدخول أولاً')

def require_origin(req:Request):
    origin=req.headers.get('origin','')
    if origin and PUBLIC_URL and origin!=PUBLIC_URL:raise HTTPException(403,'Origin mismatch')

class OwnerLogin(BaseModel):password:str
class ModeChoice(BaseModel):mode:str
class AccountChoice(BaseModel):id:int

class CTrader:
    def __init__(self,endpoint,on_event=None):
        self.endpoint=endpoint
        self.ws=None
        self.pending={}
        self.receiver=None
        self.heartbeat_task=None
        self.on_event=on_event
    async def open(self):
        self.ws=await websockets.connect('wss://'+self.endpoint+'.ctraderapi.com:5036/',
            ping_interval=20,ping_timeout=20,max_size=6000000,open_timeout=14)
        self.receiver=asyncio.create_task(self.read_loop())
        response=await self.request(2100,{'clientId':ID,'clientSecret':SECRET})
        self.heartbeat_task=asyncio.create_task(self.heartbeat_loop())
        return response
    async def heartbeat_loop(self):
        try:
            while self.ws:
                await asyncio.sleep(9)
                await self.ws.send(json.dumps({
                    'clientMsgId':'heartbeat-'+uuid.uuid4().hex,
                    'payloadType':51,'payload':{}}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Heartbeat interrupted: %s',type(exc).__name__)

    async def request(self,kind,payload,timeout=18):
        if not self.ws:raise RuntimeError('No cTrader connection')
        mid=uuid.uuid4().hex
        fut=asyncio.get_running_loop().create_future()
        self.pending[mid]=fut
        data=json.dumps({'clientMsgId':mid,'payloadType':kind,'payload':payload},separators=(',',':'))
        try:
            await self.ws.send(data)
            response=await asyncio.wait_for(fut,timeout)
            if response.get('payloadType') in (50,2142):
                p=response.get('payload') or {}
                raise RuntimeError(str(p.get('errorCode','API error')))
            return response.get('payload',{}) or {}
        finally:self.pending.pop(mid,None)
    async def read_loop(self):
        try:
            async for raw in self.ws:
                message=json.loads(raw)
                mid=message.get('clientMsgId')
                fut=self.pending.get(mid)
                if fut and not fut.done():fut.set_result(message)
                elif self.on_event:await self.on_event(message)
        except asyncio.CancelledError:raise
        except Exception as exc:
            logger.warning('cTrader reader exited: %s',type(exc).__name__)
        finally:
            for future in list(self.pending.values()):
                if not future.done():future.set_exception(ConnectionError('cTrader disconnected'))
    async def close(self):
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:await self.heartbeat_task
            except (asyncio.CancelledError,Exception):pass
            self.heartbeat_task=None
        if self.ws:await self.ws.close()
        if self.receiver:
            self.receiver.cancel()
            try:await self.receiver
            except (asyncio.CancelledError,Exception):pass
        self.pending.clear()

def bar_to_dict(bar):
    low=int(bar['low']);t=int(bar['utcTimestampInMinutes'])*60000
    return {'t':t,'o':(low+int(bar.get('deltaOpen',0)))/1e5,
        'h':(low+int(bar.get('deltaHigh',0)))/1e5,
        'l':low/1e5,'c':(low+int(bar.get('deltaClose',0)))/1e5}

def normalize_symbol(x):return ''.join(c for c in x.upper() if c.isalnum())

def choose_symbols(items):
    out={}
    for item in items:
        name=item.get('symbolName') or item.get('name') or ''
        norm=normalize_symbol(name)
        if norm.startswith('XAUUSD') and 'XAUUSD' not in out:
            out['XAUUSD']={'symbolId':int(item['symbolId']),'name':name}
        if norm.startswith('BTCUSD') and 'BTCUSD' not in out:
            out['BTCUSD']={'symbolId':int(item['symbolId']),'name':name}
    return out

class Monitor:
    def __init__(self,account):
        self.account=account
        self.conn=None
        self.task=None
        self.scan_tasks={}
        self.spreads={'XAUUSD':deque(maxlen=100),'BTCUSD':deque(maxlen=100)}
    async def on_event(self,msg):
        if msg.get('payloadType')!=2131:return
        item=msg.get('payload') or {}
        try:sid=int(item.get('symbolId',-1))
        except (ValueError,TypeError):return
        for market,sym in list(memory['symbols'].items()):
            if int(sym['symbolId'])!=sid:continue
            q=memory['quotes'].get(market,{})
            touched=[]
            received=time.time()
            if item.get('bid') is not None:
                q['bid']=int(item['bid'])/1e5
                q['bid_at']=received
                touched.append('bid')
            if item.get('ask') is not None:
                q['ask']=int(item['ask'])/1e5
                q['ask_at']=received
                touched.append('ask')
            if q.get('ask') and q.get('bid') and q['ask']>q['bid']:
                self.spreads[market].append(q['ask']-q['bid'])
            if q.get('bid') and q.get('ask'):
                q['received_at']=received
                memory['market_health'].setdefault(market,{})['last_quote_at']=received
            if item.get('timestamp'):
                stamp=int(item['timestamp'])
                q['market_ts']=stamp/1000 if stamp>100_000_000_000 else stamp
            memory['quotes'][market]=q
            health=memory['market_health'].get(market,{})
            # Only allow live exits after initial historical reconciliation,
            # so a restart cannot label an old TP/SL as a current tick hit.
            if health.get('candles_loaded') and getattr(app.state,'journal_tests_passed',False) and touched:
                for updated in touched:
                    settle_on_quote(memory['history'],market,q.get('bid'),q.get('ask'),
                                    received,changed_side=updated,
                                    account_id=self.account['id'])
    async def bars(self,market,period):
        symbol_id=memory['symbols'][market]['symbolId']
        now=time.time();lookback=86400*3 if period==1 else 86400*9
        data=await self.conn.request(2137,{
            'ctidTraderAccountId':int(self.account['id']),'symbolId':int(symbol_id),
            'period':period,'fromTimestamp':int((now-lookback)*1000),
            'toTimestamp':int(now*1000),'count':360},timeout=25)
        records=[]
        for b in data.get('trendbar',[]):
            try:records.append(bar_to_dict(b))
            except (KeyError,ValueError,TypeError):continue
        current_ms=int(now//(60*period))*60*period*1000
        records=sorted([b for b in records if b['t']<current_ms],key=lambda x:x['t'])
        return list({b['t']:b for b in records}.values())[-340:]
    async def scan_once(self,market,minute):
        """Try a completed-bar evaluation independently for each market."""
        health=memory['market_health'].setdefault(market,{})
        started=time.time()
        note_scan_attempt(health,minute,started)
        try:
            await self.update_market(market)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raw=str(exc).strip()
            code=raw if re.fullmatch(r'[A-Z0-9_:-]{2,80}',raw) else type(exc).__name__
            reason='خطأ بيانات cTrader: '+code
            note_scan_failure(health,time.time(),reason)
            health['scan_duration_ms']=round((time.time()-started)*1000)
            memory['decisions'][market]={'side':'WAIT','reason':reason+'؛ إعادة المحاولة خلال ثوانٍ','market':market}
            logger.warning('Market scan %s failed: %s; attempt=%d of 3',
                           market,code,health.get('scan_attempts_this_minute',0))
            # Crucial: DO NOT mark this minute as evaluated when the API fails.
        else:
            note_scan_success(health,time.time())
            memory['last_eval'][market]=minute
            health['scan_duration_ms']=round((time.time()-started)*1000)
            health['last_scanned_reason']=memory['decisions'].get(market,{}).get('reason','')
            bars=memory['candles'].get(market,{}).get('1',[])
            if bars:
                health['last_scanned_bar_time']=bars[-1]['t']
            if minute % 10 == 0:
                logger.info('Market scan OK %s side=%s M1=%d M5=%d delay=%dms',
                            market,memory['decisions'].get(market,{}).get('side','WAIT'),
                            len(bars),len(memory['candles'].get(market,{}).get('5',[])),
                            health['scan_duration_ms'])
        finally:
            health['scan_inflight']=False

    async def start(self):
        while True:
            try:
                await self.connect_one()
                while True:
                    if not self.conn or not self.conn.ws or self.conn.receiver.done():
                        raise ConnectionError('cTrader socket reader stopped')
                    now=time.time()
                    minute=int(now//60)
                    for market in list(memory['symbols']):
                        health=memory['market_health'].get(market,{})
                        pending=self.scan_tasks.get(market)
                        if pending and not pending.done():
                            continue
                        if not can_start_scan(
                            health,now,minute,memory['last_eval'].get(market),
                            health.get('spot_subscribed',False)
                        ):
                            continue
                        # Never await one market's history request while
                        # scheduling the other market's evaluation.
                        self.scan_tasks[market]=asyncio.create_task(
                            self.scan_once(market,minute))
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                raw=str(exc).strip()
                tag=raw if re.fullmatch(r'[A-Z0-9_:-]{2,80}',raw) else type(exc).__name__
                logger.warning('cTrader monitor reconnect: %s',tag)
                memory['error']='جاري إعادة اتصال cTrader: '+tag
            finally:
                tasks=list(self.scan_tasks.values())
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks,return_exceptions=True)
                self.scan_tasks.clear()
                memory['connected']=False
                memory['phase']='reconnecting'
                for market in ('XAUUSD','BTCUSD'):
                    memory['decisions'][market]={'side':'WAIT','reason':'جارٍ إعادة اتصال cTrader'}
                    memory['market_health'].setdefault(market,{})['spot_subscribed']=False
                if self.conn:
                    await self.conn.close()
                self.conn=None
            await asyncio.sleep(5)

    async def connect_one(self):
        endpoint='live' if self.account['isLive'] else 'demo'
        memory['phase']='authorizing'
        memory['connected']=False
        memory['market_health']={}
        memory['quotes']={}
        self.conn=CTrader(endpoint,self.on_event)
        await self.conn.open()
        await self.conn.request(2102,{
            'ctidTraderAccountId':int(self.account['id']),
            'accessToken':memory['token']})
        memory['phase']='finding_symbols'
        symbols=await self.conn.request(2114,{
            'ctidTraderAccountId':int(self.account['id']),
            'includeArchivedSymbols':False})
        memory['symbols']=choose_symbols(symbols.get('symbol',[]))
        if not memory['symbols']:
            raise RuntimeError('XAUUSD/BTCUSD not available on broker account')
        # Account authentication confirms the socket connection. Candle
        # fetching must not block the connection indicator.
        memory['connected']=True
        memory['phase']='subscribing_spots'
        subscribed=[]
        for market,sym in memory['symbols'].items():
            memory['market_health'][market]={'spot_subscribed':False}
            try:
                await self.conn.request(2127,{
                    'ctidTraderAccountId':int(self.account['id']),
                    'symbolId':[int(sym['symbolId'])],
                    'subscribeToSpotTimestamp':True},timeout=12)
                memory['market_health'][market]['spot_subscribed']=True
                subscribed.append(market)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Spot subscription failed %s: %s',market,type(exc).__name__)
                memory['market_health'][market]['analysis_error']='فشل الاشتراك بأسعار '+market
        if not subscribed:
            raise RuntimeError('No successful spot subscriptions')
        memory['phase']='warming_candles'
        for market in subscribed:
            # Historical closed candles (2137) are sufficient for signals;
            # a live-trendbar (2135) acknowledgement is not required.
            try:
                memory['candles'][market]={
                    '1':await self.bars(market,1),
                    '5':await self.bars(market,5)}
                update_paper_journal(memory['history'],market,
                                     memory['candles'][market]['1'],
                                     account_id=self.account['id'])
                memory['market_health'][market]['candles_loaded']=True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Initial candles failed %s: %s',market,type(exc).__name__)
                memory['market_health'][market]['candles_loaded']=False
                memory['market_health'][market]['analysis_error']='جارٍ إعادة تحميل شموع '+market
        memory['phase']='monitoring'
        memory['error']=None
        logger.info('cTrader authenticated and spot feeds subscribed: %s',','.join(subscribed))
    async def update_market(self,market):
        # Reconcile completed candles even when a market stops quoting.
        # Quote freshness is required for NEW signals, not for the journal.
        q=memory['quotes'].get(market,{})
        bars1=await self.bars(market,1)
        bars5=await self.bars(market,5) if int(time.time()//60)%5==0 else memory['candles'].get(market,{}).get('5',[])
        memory['candles'][market]={'1':bars1,'5':bars5}
        update_paper_journal(memory['history'],market,bars1,account_id=self.account['id'])
        now=time.time()
        if not q.get('received_at') or now-q['received_at']>25:
            memory['decisions'][market]={'side':'WAIT','reason':'لا توجد أسعار مباشرة حديثة؛ تم تحديث سجل الصفقات من الشموع'}
            return
        if not bars1 or now-(bars1[-1]['t']/1000+60)>185:
            memory['decisions'][market]={'side':'WAIT','reason':'شموع M1 ليست حديثة؛ السوق مغلق أو التاريخ متأخر'}
            memory['market_health'].setdefault(market,{})['last_analysis_at']=now
            return
        result=signal(bars1,bars5,q.get('bid'),q.get('ask'),list(self.spreads[market])[:-1],memory['mode'])
        result['market']=market
        if result['side'] in ('BUY','SELL'):
            last=memory['last_signal_ts'].get(market,0)
            t=bars1[-1]['t'] if bars1 else 0
            if t==last or time.time()-last/1000<180:
                result={'side':'WAIT','reason':'فترة منع تكرار الإشارات','market':market}
            else:
                result['id']=uuid.uuid4().hex[:12]
                result['created_at']=datetime.now(timezone.utc).isoformat()
                result['account_id']=int(self.account['id'])
                result['status']='OPEN'
                result['mode']=memory['mode']
                result['risk_pct']=PRESETS[memory['mode']]['risk_pct']
                result['checked_bar_time']=t
                result['closed_at']=None
                result['exit_price']=None
                result['result_r']=None
                memory['last_signal_ts'][market]=t
                result['mode_label']=PRESETS[memory['mode']]['label']
                memory['history'].insert(0,result.copy())
                memory['history']=memory['history'][:200]
                # Notify only real, unique BUY/SELL signals, never WAIT.
                asyncio.create_task(telegram.send_signal(result.copy()))
        memory['decisions'][market]=result
        memory['market_health'].setdefault(market,{})['last_analysis_at']=time.time()

async def refresh_ctrader_token():
    """Refresh only when requested from an authenticated owner browser."""
    if not memory.get('refresh_token'):
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        response=await client.get('https://openapi.ctrader.com/apps/token',params={
            'grant_type':'refresh_token',
            'refresh_token':memory['refresh_token'],
            'client_id':ID,'client_secret':SECRET})
        response.raise_for_status()
        tokens=response.json()
    if not tokens.get('accessToken'):
        return False
    memory['token']=tokens['accessToken']
    memory['refresh_token']=tokens.get('refreshToken') or memory['refresh_token']
    memory['token_expires']=time.time()+int(tokens.get('expiresIn',2628000))
    return True


async def restore_auth_from_cookie(request:Request):
    """Restore account and resume monitor on visit after Render restart.

    Returns True only when a refreshed access token requires a new cookie.
    Authentication remains behind SITE_PASSWORD.
    """
    global worker
    data=deserialize_auth(request.cookies.get(COOKIE_NAME))
    if not data:
        return False
    async with auth_lock:
        if not memory['token']:
            memory['token']=data['access']
            memory['refresh_token']=data.get('refresh') or ''
            memory['token_expires']=float(data.get('expires') or 0)
            memory['accounts']=data.get('accounts') or []
            selected=data.get('selected')
            memory['selected_account']=next(
                (a for a in memory['accounts'] if str(a['id'])==str(selected)),None)
            memory['phase']='restoring' if memory['selected_account'] else 'needs_account'
            memory['error']='جارٍ استعادة جلسة cTrader المحفوظة'
            logger.info('Restoring owner account selection from encrypted cookie')
        changed=False
        if memory.get('refresh_token') and memory['token_expires'] < time.time()+3600:
            try:
                changed=await refresh_ctrader_token()
            except Exception as exc:
                logger.warning('cTrader refresh token failed: %s',type(exc).__name__)
                memory['error']='انتهى التفويض؛ اضغط ربط cTrader لتجديد الإذن'
                memory['phase']='needs_authorization'
                return False
        chosen=memory.get('selected_account')
        if chosen and (worker is None or worker.done()):
            memory['phase']='starting'
            memory['error']='جارٍ استعادة اتصال cTrader من الجلسة المحفوظة'
            worker=asyncio.create_task(Monitor(chosen).start())
        return changed


def set_ctrader_cookie(response):
    if memory.get('token'):
        attach_cookie(response,memory['token'],memory['refresh_token'],
                      memory['token_expires'],memory['accounts'],
                      memory['selected_account'])
    return response


class JournalRestore(BaseModel):
    records: list[dict] = []

@app.post('/api/journal/restore')
async def restore_journal(data:JournalRestore,request:Request):
    require_owner(request)
    require_origin(request)
    await restore_auth_from_cookie(request)
    selected=memory.get('selected_account')
    if not selected:
        raise HTTPException(409,'اختر الحساب أولًا لاستعادة سجل الإشارات التجريبية')
    existing={str(item.get('id')) for item in memory['history']}
    restored=[]
    for item in sanitize_client_backup(data.records,selected['id']):
        if item['id'] in existing:
            continue
        restored.append(item)
        existing.add(item['id'])
    memory['history']=sorted(memory['history']+restored,
                             key=lambda row:row.get('created_at') or '',reverse=True)[:200]
    for market in ('XAUUSD','BTCUSD'):
        bars=memory['candles'].get(market,{}).get('1',[])
        if bars:
            update_paper_journal(memory['history'],market,bars,
                                 account_id=selected['id'])
    for row in memory['history']:
        if row.get('account_id')==selected['id'] and row.get('bar_time'):
            market=row['market']
            memory['last_signal_ts'][market]=max(
                int(memory['last_signal_ts'].get(market,0)),
                int(row.get('bar_time',0)))
    return {'ok':True,'restored':len(restored),'total':len(memory['history'])}

@app.post('/api/restart-monitor')
async def restart_monitor(request:Request):
    global worker
    require_owner(request)
    require_origin(request)
    await restore_auth_from_cookie(request)
    chosen=memory.get('selected_account')
    if not chosen:
        raise HTTPException(409,'يلزم ربط cTrader واختيار الحساب أولًا')
    if worker:
        worker.cancel()
        try:await worker
        except (asyncio.CancelledError,Exception):pass
    memory['phase']='starting'
    memory['error']='جارٍ إعادة بدء المراقبة'
    worker=asyncio.create_task(Monitor(chosen).start())
    return {'ok':True,'phase':memory['phase']}


async def discover_accounts():
    con=CTrader('live')
    try:
        await con.open()
        res=await con.request(2149,{'accessToken':memory['token']})
        found=[]
        for a in res.get('ctidTraderAccount',[]):
            aid=a.get('ctidTraderAccountId')
            if aid is not None:
                found.append({'id':int(aid),'isLive':bool(a.get('isLive')),'brokerTitle':a.get('brokerTitle','')})
        return found
    finally:await con.close()

@app.get('/')
async def index():return FileResponse(ROOT/'index.html')
@app.get('/healthz')
async def health():return {'ok':True,'service':'Quantum Simons','api_configured':bool(ID and SECRET),'journal_tests_passed':getattr(app.state,'journal_tests_passed',False)}
@app.get('/api/session')
async def session(request:Request):
    try:require_owner(request);ok=True
    except HTTPException:ok=False
    return {'authenticated':ok,'password_configured':bool(OWNER_PASSWORD),
        'api_configured':bool(ID and SECRET),
        'redirect_uri':(PUBLIC_URL+'/api/oauth/callback') if PUBLIC_URL else 'Set PUBLIC_URL',
        'version':'1.3.0'}
@app.post('/api/login')
async def login(payload:OwnerLogin,request:Request):
    require_origin(request)
    if not OWNER_PASSWORD:raise HTTPException(503,'Site password not configured')
    if not hmac.compare_digest(payload.password,OWNER_PASSWORD):raise HTTPException(401,'كلمة المرور غير صحيحة')
    res=JSONResponse({'ok':True})
    res.set_cookie('qs_session',signer.sign('owner').decode(),max_age=604800,httponly=True,secure=True,samesite='lax')
    return res
@app.post('/api/logout')
async def logout(request:Request):
    require_owner(request);require_origin(request)
    r=JSONResponse({'ok':True});r.delete_cookie('qs_session');delete_cookie(r);return r
@app.get('/api/status')
async def status(request:Request):
    require_owner(request)
    rotated=await restore_auth_from_cookie(request)
    market_data={}
    now=time.time()
    for market in ('XAUUSD','BTCUSD'):
        q=memory['quotes'].get(market,{})
        health=memory['market_health'].get(market,{})
        bars1=memory['candles'].get(market,{}).get('1',[])
        bars5=memory['candles'].get(market,{}).get('5',[])
        quote_fresh=bool(memory['connected'] and health.get('spot_subscribed') and q.get('bid') and q.get('ask') and q.get('received_at') and now-q['received_at']<30)
        candle_fresh=bool(bars1 and now-(bars1[-1]['t']/1000+60)<185)
        analysis_ready=bool(quote_fresh and candle_fresh and len(bars1)>=145 and len(bars5)>=65 and health.get('last_analysis_at') and now-health['last_analysis_at']<185 and not health.get('analysis_error'))
        if analysis_ready:
            status_text='الأسعار والشموع متزامنة؛ تحليل الإشارات جاهز'
        elif quote_fresh and not candle_fresh:
            status_text='الأسعار تتحدث لكن شموع M1 غير حديثة أو لم تصل'
        elif quote_fresh:
            status_text='الأسعار تتحدث؛ جارٍ تهيئة تحليل الشموع'
        elif memory['connected']:
            status_text='اتصال cTrader قائم؛ لا توجد تسعيرات جديدة لهذا السوق'
        else:
            status_text='اتصال cTrader يعيد المحاولة أو متوقف'
        market_data[market]={
            'quote_fresh':quote_fresh,'candle_fresh':candle_fresh,
            'analysis_ready':analysis_ready,'status_text':status_text,
            'analysis_error':health.get('analysis_error'),
            'scanner':scan_diagnostics(health,now,memory['last_eval'].get(market),
                    bars1[-1]['t'] if bars1 else None,
                    memory['decisions'].get(market,{}),quote_fresh),
            'scan_duration_ms':health.get('scan_duration_ms'),
            'quote':q,'decision':memory['decisions'][market],
            'spread':round(q.get('ask',0)-q.get('bid',0),5) if q.get('bid') and q.get('ask') else None,
            'bar_count_m1':len(memory['candles'].get(market,{}).get('1',[])),
            'bar_count_m5':len(memory['candles'].get(market,{}).get('5',[])),
            'symbol':memory['symbols'].get(market),
            'spark':[round(b['c'],5) for b in memory['candles'].get(market,{}).get('1',[])[-40:]]}
    payload={'connected':memory['connected'],'phase':memory['phase'],'error':memory['error'],
        'server_instance_id':SERVER_INSTANCE_ID,
        'authenticated_to_ctrader':bool(memory['token']),
        'account':memory['selected_account'],'accounts':memory['accounts'],
        'mode':memory['mode'],'presets':PRESETS,'markets':market_data,
        'signals':memory['history'][:200],
        'performance':compute_performance(memory['history'],
                    memory['selected_account']['id'] if memory['selected_account'] else None),
        'server_time':datetime.now(timezone.utc).isoformat(),
        'background_push_enabled':False,'telegram':telegram.public_status()}
    response=JSONResponse(payload)
    if rotated:set_ctrader_cookie(response)
    return response
@app.post('/api/mode')
async def set_mode(data:ModeChoice,request:Request):
    require_owner(request);require_origin(request)
    if data.mode not in PRESETS:raise HTTPException(400,'Invalid mode')
    memory['mode']=data.mode
    return {'ok':True,'mode':data.mode}
@app.get('/api/oauth/start')
async def oauth_start(request:Request):
    require_owner(request)
    if not ID or not SECRET or not PUBLIC_URL:raise HTTPException(503,'Set CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET and PUBLIC_URL on host')
    nonce=secrets.token_urlsafe(24)
    memory['oauth_state']=(nonce,time.time())
    params={'client_id':ID,'redirect_uri':PUBLIC_URL+'/api/oauth/callback','scope':'accounts','product':'web','state':nonce}
    return RedirectResponse('https://id.ctrader.com/my/settings/openapi/grantingaccess/?'+urlencode(params))
@app.get('/api/oauth/callback',name='oauth_callback')
async def oauth_callback(request:Request,code:str='',state:str='',error:str=''):
    require_owner(request)
    valid=memory.get('oauth_state')
    memory['oauth_state']=None
    if error:raise HTTPException(400,'cTrader authorization was denied')
    if not valid or not code or not state or not hmac.compare_digest(valid[0],state) or time.time()-valid[1]>300:
        raise HTTPException(400,'Invalid or expired OAuth state')
    try:
        async with httpx.AsyncClient(timeout=12) as http:
            response=await http.get('https://openapi.ctrader.com/apps/token',params={
                'grant_type':'authorization_code','code':code,'redirect_uri':PUBLIC_URL+'/api/oauth/callback',
                'client_id':ID,'client_secret':SECRET})
            response.raise_for_status()
            token_response=response.json()
        if not token_response.get('accessToken'):raise ValueError('No access token')
        memory['token']=token_response['accessToken']
        memory['refresh_token']=token_response.get('refreshToken')
        memory['token_expires']=time.time()+int(token_response.get('expiresIn',3600))
        memory['accounts']=await discover_accounts()
        memory['selected_account']=None
        memory['error']=None if memory['accounts'] else 'تم الربط ولكن لم توجد حسابات مصرح بها'
        if len(memory['accounts'])==1:
            memory['selected_account']=memory['accounts'][0]
        redirect=RedirectResponse('/?linked=1')
        set_ctrader_cookie(redirect)
        if memory['selected_account']:
            global worker
            if worker:
                worker.cancel()
            worker=asyncio.create_task(Monitor(memory['selected_account']).start())
        return redirect
    except Exception:
        memory['token']=None;memory['refresh_token']=None
        raise HTTPException(502,'تعذر إتمام ربط cTrader. تأكد من تفعيل التطبيق وتطابق Redirect URI')
@app.post('/api/account')
async def set_account(data:AccountChoice,request:Request):
    global worker
    require_owner(request);require_origin(request)
    await restore_auth_from_cookie(request)
    if not memory['token']:raise HTTPException(400,'ربط cTrader مطلوب أولاً')
    account=next((a for a in memory['accounts'] if int(a['id'])==data.id),None)
    if not account:raise HTTPException(400,'الحساب غير مصرح به')
    if worker:
        worker.cancel()
        try:await worker
        except (asyncio.CancelledError,Exception):pass
    memory['selected_account']=account;memory['symbols']={}
    memory['quotes']={};memory['candles']={};memory['last_eval']={}
    memory['connected']=False;memory['phase']='starting';memory['market_health']={}
    memory['error']='جارٍ تأسيس اتصال السوق وجلب الشموع'
    worker=asyncio.create_task(Monitor(account).start())
    return set_ctrader_cookie(JSONResponse({'ok':True,'connecting':True}))

class TelegramConnect(BaseModel):
    bot_token: str = ""
    chat_id: str
    markets: list[str] = ["XAUUSD", "BTCUSD"]

class TelegramDiscover(BaseModel):
    bot_token: str = ""

class TelegramToggle(BaseModel):
    enabled: bool

@app.post('/api/telegram/discover')
async def telegram_discover(data: TelegramDiscover, request: Request):
    require_owner(request)
    require_origin(request)
    try:
        return await telegram.discover(data.bot_token)
    except TelegramError as e:
        raise HTTPException(400, str(e))

@app.post('/api/telegram/connect')
async def telegram_connect(data: TelegramConnect, request: Request):
    require_owner(request)
    require_origin(request)
    try:
        return {"ok": True, "telegram": await telegram.configure(
            data.bot_token, data.chat_id, data.markets
        )}
    except TelegramError as e:
        raise HTTPException(400, str(e))

@app.post('/api/telegram/test')
async def telegram_test(request: Request):
    require_owner(request)
    require_origin(request)
    try:
        return await telegram.send_test()
    except TelegramError as e:
        raise HTTPException(400, str(e))

@app.post('/api/telegram/toggle')
async def telegram_toggle(data: TelegramToggle, request: Request):
    require_owner(request)
    require_origin(request)
    try:
        return {"ok": True, "telegram": telegram.toggle(data.enabled)}
    except TelegramError as e:
        raise HTTPException(400, str(e))


@app.on_event('startup')
async def run_journal_regression_tests():
    """Small deterministic tests run on every deployment, no external accounts."""
    import io
    import unittest
    output=io.StringIO()
    try:
        suite=unittest.defaultTestLoader.discover(
            start_dir=str(ROOT/'tests'),pattern='test_paper_journal.py')
        result=unittest.TextTestRunner(stream=output,verbosity=1).run(suite)
        app.state.journal_tests_passed=bool(result.wasSuccessful() and result.testsRun>=7)
        if app.state.journal_tests_passed:
            logger.warning('Quantum Simons paper journal regression OK: %s tests',result.testsRun)
        else:
            logger.error('Quantum Simons paper journal regression failed: %s',output.getvalue()[-3500:])
    except Exception:
        app.state.journal_tests_passed=False
        logger.exception('Quantum Simons paper journal regression runner failed')

@app.on_event('shutdown')
async def shutdown():
    global worker
    if worker:
        worker.cancel()
        try:await worker
        except (asyncio.CancelledError,Exception):pass
if __name__=='__main__':
    uvicorn.run('app:app',host='0.0.0.0',port=int(os.getenv('PORT','8000')),workers=1)
