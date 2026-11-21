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
      const expiry=Date.parse(rec.valid_until);
      // Legacy journal records without a 45-second entry window are NOT
      // actionable alerts. Only new zone-confirmed records can notify.
      if(!Number.isFinite(expiry)||now>expiry||expiry-created>60000)continue;
      const status=(marketStatus||{})[rec.market]||{};
      const decision=status.decision||{};
      if(!status.analysis_ready||decision.id!==id||decision.side!==rec.side)continue;
      const quote=status.quote||{};
      const executable=rec.side==='BUY'?Number(quote.ask):Number(quote.bid);
      const low=Number(rec.zone_lower),high=Number(rec.zone_upper);
      if(!Number.isFinite(executable)||!Number.isFinite(low)||!Number.isFinite(high)
         ||executable<=0||low>=high||executable<low||executable>high)continue;
      notifications.push(rec);
    }
    const ids=Array.from(seen).slice(-MAX_SEEN);
    return{ids,alerts:notifications.slice(0,4)};
  }

  return Object.freeze({nextAlertState,MAX_AGE_MS});
});
