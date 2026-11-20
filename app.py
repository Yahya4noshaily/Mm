"""Quantum Simons — independent read-only cTrader website. No order placement."""
import asyncio
import hmac
import json
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

app=FastAPI(title='Quantum Simons',docs_url=None,redoc_url=None)
ROOT=Path(__file__).parent
ID=os.getenv('CTRADER_CLIENT_ID','').strip()
SECRET=os.getenv('CTRADER_CLIENT_SECRET','').strip()
OWNER_PASSWORD=os.getenv('SITE_PASSWORD','')
SITE_SECRET=os.getenv('SITE_SECRET','').strip() or secrets.token_urlsafe(32)
PUBLIC_URL=os.getenv('PUBLIC_URL','').rstrip('/')
signer=TimestampSigner(SITE_SECRET)
telegram=TelegramAlerts()

memory={
 'token':None,'refresh_token':None,'token_expires':0,
 'accounts':[],'selected_account':None,'oauth_state':None,
 'connected':False,'error':'لم يتم ربط cTrader بعد',
 'mode':'active','history':[],'quotes':{},'candles':{},
 'decisions':{'XAUUSD':{'side':'WAIT','reason':'بانتظار الاتصال'},'BTCUSD':{'side':'WAIT','reason':'بانتظار الاتصال'}},
 'symbols':{},'last_seen':{},'last_signal_ts':{},'last_eval':{},
}
worker=None

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
        self.on_event=on_event
    async def open(self):
        self.ws=await websockets.connect('wss://'+self.endpoint+'.ctraderapi.com:5036/',
            ping_interval=20,ping_timeout=20,max_size=6000000,open_timeout=14)
        self.receiver=asyncio.create_task(self.read_loop())
        return await self.request(2100,{'clientId':ID,'clientSecret':SECRET})
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
        except Exception:pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():future.set_exception(ConnectionError('cTrader disconnected'))
    async def close(self):
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
        self.spreads={'XAUUSD':deque(maxlen=100),'BTCUSD':deque(maxlen=100)}
    async def on_event(self,msg):
        if msg.get('payloadType')!=2131:return
        item=msg.get('payload') or {}
        sid=int(item.get('symbolId',-1))
        for market,sym in memory['symbols'].items():
            if int(sym['symbolId'])!=sid:continue
            q=memory['quotes'].get(market,{})
            if item.get('bid') is not None:q['bid']=int(item['bid'])/1e5
            if item.get('ask') is not None:q['ask']=int(item['ask'])/1e5
            if q.get('ask') and q.get('bid') and q['ask']>q['bid']:
                self.spreads[market].append(q['ask']-q['bid'])
            q['received_at']=time.time()
            if item.get('timestamp'):
                stamp=int(item['timestamp'])
                q['market_ts']=stamp/1000 if stamp>100_000_000_000 else stamp
            memory['quotes'][market]=q
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
    async def start(self):
        while True:
            try:
                await self.connect_one()
                while True:
                    if not self.conn or not self.conn.ws or self.conn.receiver.done():break
                    minute=int(time.time()//60)
                    for market in list(memory['symbols']):
                        if memory['last_eval'].get(market)==minute:continue
                        if time.time()%60<2:continue
                        memory['last_eval'][market]=minute
                        await self.update_market(market)
                    await asyncio.sleep(1.25)
            except asyncio.CancelledError:break
            except Exception as e:
                memory['error']='انقطع اتصال cTrader أو تعذرت قراءة البيانات: '+str(e)[:110]
            finally:
                memory['connected']=False
                for market in ('XAUUSD','BTCUSD'):
                    memory['decisions'][market]={'side':'WAIT','reason':'اتصال غير متاح؛ لا يمكن إصدار إشارة'}
                if self.conn:await self.conn.close()
                self.conn=None
            await asyncio.sleep(5)
    async def connect_one(self):
        endpoint='live' if self.account['isLive'] else 'demo'
        self.conn=CTrader(endpoint,self.on_event)
        await self.conn.open()
        await self.conn.request(2102,{'ctidTraderAccountId':int(self.account['id']),'accessToken':memory['token']})
        symbols=await self.conn.request(2114,{'ctidTraderAccountId':int(self.account['id']),'includeArchivedSymbols':False})
        memory['symbols']=choose_symbols(symbols.get('symbol',[]))
        if not memory['symbols']:raise RuntimeError('XAUUSD/BTCUSD symbols unavailable on broker account')
        for market in memory['symbols']:
            memory['candles'][market]={'1':await self.bars(market,1),'5':await self.bars(market,5)}
            await self.conn.request(2127,{'ctidTraderAccountId':int(self.account['id']),
                'symbolId':[int(memory['symbols'][market]['symbolId'])],'subscribeToSpotTimestamp':True})
            await self.conn.request(2135,{'ctidTraderAccountId':int(self.account['id']),
                'symbolId':int(memory['symbols'][market]['symbolId']),'period':1})
        memory['connected']=True
        memory['error']=None
    async def update_market(self,market):
        q=memory['quotes'].get(market,{})
        if not q.get('received_at') or time.time()-q['received_at']>25:
            memory['decisions'][market]={'side':'WAIT','reason':'لا توجد أسعار حديثة؛ السوق مغلق أو الاتصال متأخر'}
            return
        bars1=await self.bars(market,1)
        bars5=await self.bars(market,5) if int(time.time()//60)%5==0 else memory['candles'].get(market,{}).get('5',[])
        memory['candles'][market]={'1':bars1,'5':bars5}
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
                memory['last_signal_ts'][market]=t
                result['mode_label']=PRESETS[memory['mode']]['label']
                memory['history'].insert(0,result.copy())
                memory['history']=memory['history'][:100]
                # Notify only real, unique BUY/SELL signals, never WAIT.
                asyncio.create_task(telegram.send_signal(result.copy()))
        memory['decisions'][market]=result

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
async def health():return {'ok':True,'service':'Quantum Simons','api_configured':bool(ID and SECRET)}
@app.get('/api/session')
async def session(request:Request):
    try:require_owner(request);ok=True
    except HTTPException:ok=False
    return {'authenticated':ok,'password_configured':bool(OWNER_PASSWORD),
        'api_configured':bool(ID and SECRET),
        'redirect_uri':(PUBLIC_URL+'/api/oauth/callback') if PUBLIC_URL else 'Set PUBLIC_URL',
        'version':'1.0.0'}
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
    r=JSONResponse({'ok':True});r.delete_cookie('qs_session');return r
@app.get('/api/status')
async def status(request:Request):
    require_owner(request)
    market_data={}
    for market in ('XAUUSD','BTCUSD'):
        q=memory['quotes'].get(market,{})
        market_data[market]={
            'quote':q,'decision':memory['decisions'][market],
            'spread':round(q.get('ask',0)-q.get('bid',0),5) if q.get('bid') and q.get('ask') else None,
            'bar_count_m1':len(memory['candles'].get(market,{}).get('1',[])),
            'bar_count_m5':len(memory['candles'].get(market,{}).get('5',[])),
            'symbol':memory['symbols'].get(market),
            'spark':[round(b['c'],5) for b in memory['candles'].get(market,{}).get('1',[])[-40:]]}
    return {'connected':memory['connected'],'error':memory['error'],
        'authenticated_to_ctrader':bool(memory['token']),
        'account':memory['selected_account'],'accounts':memory['accounts'],
        'mode':memory['mode'],'presets':PRESETS,'markets':market_data,
        'signals':memory['history'][:35],'server_time':datetime.now(timezone.utc).isoformat(),
        'background_push_enabled':False,'telegram':telegram.public_status()}
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
        memory['error']=None if memory['accounts'] else 'تم الربط ولكن لم توجد حسابات مصرح بها'
        return RedirectResponse('/?linked=1')
    except Exception:
        memory['token']=None;memory['refresh_token']=None
        raise HTTPException(502,'تعذر إتمام ربط cTrader. تأكد من تفعيل التطبيق وتطابق Redirect URI')
@app.post('/api/account')
async def set_account(data:AccountChoice,request:Request):
    global worker
    require_owner(request);require_origin(request)
    if not memory['token']:raise HTTPException(400,'ربط cTrader مطلوب أولاً')
    account=next((a for a in memory['accounts'] if int(a['id'])==data.id),None)
    if not account:raise HTTPException(400,'الحساب غير مصرح به')
    if worker:
        worker.cancel()
        try:await worker
        except (asyncio.CancelledError,Exception):pass
    memory['selected_account']=account;memory['symbols']={}
    memory['quotes']={};memory['candles']={};memory['last_eval']={}
    memory['error']='جارٍ تأسيس اتصال السوق وجلب الشموع'
    worker=asyncio.create_task(Monitor(account).start())
    return {'ok':True,'connecting':True}

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

@app.on_event('shutdown')
async def shutdown():
    global worker
    if worker:
        worker.cancel()
        try:await worker
        except (asyncio.CancelledError,Exception):pass
if __name__=='__main__':
    uvicorn.run('app:app',host='0.0.0.0',port=int(os.getenv('PORT','8000')),workers=1)
