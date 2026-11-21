/* Quantum Simons — browser notification policy.
   No changes to trading strategies or server Telegram dispatch.
   A browser alert is ONLY for a fresh, currently actionable signal.
   Old journal entries and STOP/TARGET/UNVERIFIED records must never alert.
*/
(function(root,factory){
  const api=factory();
  if(typeof module==='object' && module.exports)module.exports=api;
  root.QuantumAlertPolicy=api;
})(typeof globalThis==='object'?globalThis:this,function(){
  'use strict';
  const MAX_SEEN=300;
  const MAX_AGE_MS=75*1000;
  const ID=/^[a-f0-9]{12}$/;

  function nextAlertState(history,marketStatus,seenBefore,initialized,serverNow){
    const seen=new Set(
      (Array.isArray(seenBefore)?seenBefore:[])
        .filter(id=>typeof id==='string'&&ID.test(id))
        .slice(-MAX_SEEN)
    );
    const previous=new Set(seen);
    const now=Date.parse(serverNow);
    const notifications=[];
    const rows=Array.isArray(history)?history:[];
    for(const rec of rows){
      if(!rec||typeof rec!=='object'||typeof rec.id!=='string'||!ID.test(rec.id))continue;
      const id=rec.id;
      seen.add(id);
      if(!initialized||previous.has(id))continue;
      if(rec.side!=='BUY'&&rec.side!=='SELL')continue;
      if(rec.status!=='OPEN')continue;
      if(rec.market!=='XAUUSD'&&rec.market!=='BTCUSD')continue;
      const created=Date.parse(rec.created_at);
      const age=now-created;
      if(!Number.isFinite(age)||age<0||age>MAX_AGE_MS)continue;
      const status=(marketStatus||{})[rec.market]||{};
      const decision=status.decision||{};
      // A journal BUY is not an immediate BUY if the scanner says WAIT.
      if(!status.analysis_ready||decision.id!==id||decision.side!==rec.side)continue;
      notifications.push(rec);
    }
    const ids=Array.from(seen).slice(-MAX_SEEN);
    return{ids,alerts:notifications.slice(0,4)};
  }

  return Object.freeze({nextAlertState,MAX_AGE_MS});
});
