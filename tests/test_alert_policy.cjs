const {test}=require('node:test');
const assert=require('node:assert/strict');
const {nextAlertState}=require('../alert_policy.js');

const now='2026-11-21T02:22:00.000Z';
const recent='2026-11-21T02:21:51.000Z';
const old='2026-11-21T01:50:00.000Z';
const first={id:'abcdef123456',market:'BTCUSD',side:'BUY',status:'STOP',created_at:old};
const second={id:'abcdef123457',market:'BTCUSD',side:'BUY',status:'STOP',created_at:old};
const fresh={id:'abcdef123458',market:'BTCUSD',side:'BUY',status:'OPEN',created_at:recent};
const goodMarket={BTCUSD:{analysis_ready:true,decision:{id:fresh.id,side:'BUY'}}};
const waitMarket={BTCUSD:{analysis_ready:true,decision:{side:'WAIT'}}};

test('first page load never re-alerts old history',()=>{
  const result=nextAlertState([second,first],waitMarket,[],false,now);
  assert.equal(result.alerts.length,0);
  assert.deepEqual(result.ids,[second.id,first.id]);
});
test('two old BUY alerts for the same BTCUSD market never ping-pong',()=>{
  let state=nextAlertState([second,first],waitMarket,[],false,now);
  for(let n=0;n<30;n++){
    state=nextAlertState([second,first],waitMarket,state.ids,true,now);
    assert.equal(state.alerts.length,0);
  }
});
test('fresh currently actionable BUY sends exactly one alert',()=>{
  const initial=nextAlertState([second,first],goodMarket,[],false,now);
  const next=nextAlertState([fresh,second,first],goodMarket,initial.ids,true,now);
  assert.deepEqual(next.alerts.map(x=>x.id),[fresh.id]);
  assert.equal(nextAlertState([first,fresh,second],goodMarket,next.ids,true,now).alerts.length,0);
});
test('WAIT on screen rejects a historical BUY notification',()=>{
  const result=nextAlertState([fresh],waitMarket,[],true,now);
  assert.equal(result.alerts.length,0);
});
test('closed or expired signals never trigger alerts',()=>{
  const closed={...fresh,status:'TARGET'};
  assert.equal(nextAlertState([closed],goodMarket,[],true,now).alerts.length,0);
  const stale={...fresh,created_at:old};
  assert.equal(nextAlertState([stale],goodMarket,[],true,now).alerts.length,0);
});
test('market identity and id must match current scanner output',()=>{
  const wrongMarket={XAUUSD:{analysis_ready:true,decision:{id:fresh.id,side:'BUY'}}};
  assert.equal(nextAlertState([fresh],wrongMarket,[],true,now).alerts.length,0);
  const wrongId={BTCUSD:{analysis_ready:true,decision:{id:'abcdef123459',side:'BUY'}}};
  assert.equal(nextAlertState([fresh],wrongId,[],true,now).alerts.length,0);
});
test('missing and future timestamps are never actionable',()=>{
  assert.equal(nextAlertState([{...fresh,created_at:undefined}],goodMarket,[],true,now).alerts.length,0);
  assert.equal(nextAlertState([{...fresh,created_at:'2026-11-21T02:25:00Z'}],goodMarket,[],true,now).alerts.length,0);
});
test('seen ID store has a bounded size',()=>{
  const known=Array.from({length:600},(_,i)=>i.toString(16).padStart(12,'0'));
  const result=nextAlertState([],{},known,true,now);
  assert.equal(result.ids.length,300);
});
