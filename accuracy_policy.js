/* Descriptive paper accuracy only — NOT a forecast of the next signal.
   Pure calculations: same mode/market/strategy/account, TARGET or STOP only.
   Does not change the signal engine, filtering, or entry conditions.
*/
(function(root,factory){
  const api=factory();
  if(typeof module==="object" && module.exports) module.exports=api;
  root.QuantumAccuracy=api;
})(typeof globalThis==="object"?globalThis:this,function(){
  "use strict";
  const Z=1.959963984540054;
  const STRATEGIES=["Trend Pullback","Mean Reversion","Compression Breakout"];
  function inferMode(r){
    if(r.mode==="active"||r.mode==="conservative")return r.mode;
    if(r.mode_label==="هجومي"||r.mode_label==="نشط")return "active";
    if(r.mode_label==="محافظ")return "conservative";
    const rr=Number(r.rr);
    if(Number.isFinite(rr)&&Math.abs(rr-1.3)<0.02)return "active";
    if(Number.isFinite(rr)&&Math.abs(rr-2.0)<0.02)return "conservative";
    return "unknown";
  }
  function accuracy(records,account,market,mode,strategy){
    const relevant=(Array.isArray(records)?records:[]).filter(r=>
      r && typeof r==="object" &&
      account != null && String(r.account_id)===String(account) &&
      r.market===market && inferMode(r)===mode && r.strategy===strategy
    );
    const settled=relevant.filter(r=>r.status==="TARGET"||r.status==="STOP");
    const wins=settled.filter(r=>r.status==="TARGET").length,n=settled.length;
    let low=null,high=null;
    if(n>0){
      const p=wins/n,z2=Z*Z,d=1+z2/n;
      const center=(p+z2/(2*n))/d;
      const spread=Z/d*Math.sqrt(p*(1-p)/n+z2/(4*n*n));
      low=Math.round(Math.max(0,center-spread)*1000)/10;
      high=Math.round(Math.min(1,center+spread)*1000)/10;
    }
    const pct=n?Math.round(1000*wins/n)/10:null;
    return Object.freeze({
      wins,losses:n-wins,resolved:n,
      open:relevant.filter(r=>(r.status||"OPEN")==="OPEN").length,
      unverified:relevant.filter(r=>r.status==="UNVERIFIED").length,
      win_rate_pct:pct,ci_lower_pct:low,ci_upper_pct:high,
      sample_status:!n?"no_data":n<20?"small":n<50?"limited":"observational",
      paper_only:true
    });
  }
  return Object.freeze({accuracy,STRATEGIES});
});
