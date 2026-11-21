const {test}=require("node:test");
const assert=require("node:assert/strict");
const {accuracy,STRATEGIES}=require("../accuracy_policy.js");
function row(id,status,extra={}){
 return {id,status,account_id:123,market:"BTCUSD",mode:"active",strategy:"Trend Pullback",...extra};
}
test("all three named strategies available and unchanged",()=>{
 assert.deepEqual(STRATEGIES,["Trend Pullback","Mean Reversion","Compression Breakout"]);
});
test("0 settled trade is unavailable, not 0% or invented 95%",()=>{
 const a=accuracy([row("1","OPEN")],123,"BTCUSD","active","Trend Pullback");
 assert.equal(a.win_rate_pct,null);
 assert.equal(a.resolved,0); assert.equal(a.open,1);
});
test("2/2 resolved is 100% descriptive but with wide Wilson interval",()=>{
 const a=accuracy([row("1","TARGET"),row("2","TARGET")],123,"BTCUSD","active","Trend Pullback");
 assert.equal(a.win_rate_pct,100);assert.equal(a.resolved,2);
 assert.equal(a.sample_status,"small");
 assert.ok(a.ci_lower_pct>=34 && a.ci_lower_pct<35);
});
test("unverified/open excluded and STOP included",()=>{
 const a=accuracy([row("1","TARGET"),row("2","STOP"),row("3","OPEN"),row("4","UNVERIFIED")],123,"BTCUSD","active","Trend Pullback");
 assert.equal(a.resolved,2);assert.equal(a.win_rate_pct,50);assert.equal(a.unverified,1);
});
test("markets, presets, strategies and accounts never get mixed",()=>{
 const rows=[row("1","TARGET"),row("2","STOP",{market:"XAUUSD"}),
 row("3","STOP",{mode:"conservative"}),row("4","STOP",{strategy:"Mean Reversion"}),
 row("5","STOP",{account_id:999})];
 const a=accuracy(rows,123,"BTCUSD","active","Trend Pullback");
 assert.equal(a.resolved,1);assert.equal(a.win_rate_pct,100);
 const noAccount=accuracy(rows,null,"BTCUSD","active","Trend Pullback");
 assert.equal(noAccount.resolved,0);
});
test("legacy entry mode inferred from mode_label/rr",()=>{
 const legacy=row("1","TARGET",{mode:undefined,mode_label:"هجومي",rr:1.3});
 assert.equal(accuracy([legacy],123,"BTCUSD","active","Trend Pullback").resolved,1);
});
test("threshold labels are descriptive",()=>{
 for(const [n,name] of [[19,"small"],[20,"limited"],[49,"limited"],[50,"observational"]]){
  const data=Array.from({length:n},(_,i)=>row(i.toString(),"STOP"));
  assert.equal(accuracy(data,123,"BTCUSD","active","Trend Pullback").sample_status,name);
 }
});
