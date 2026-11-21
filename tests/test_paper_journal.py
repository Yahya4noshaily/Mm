import unittest
from datetime import datetime, timezone, timedelta
from paper_journal import settle_on_quote, update_paper_journal, compute_performance, sanitize_client_backup

T=1795222800000

def record(side='BUY', mode='active', strategy='Trend Pullback', ident='012345abcdef'):
    entry=100.0
    stop=98.0 if side=='BUY' else 102.0
    target=102.6 if side=='BUY' else 97.4
    return {
        'id':ident,'market':'BTCUSD','side':side,'mode':mode,
        'mode_label':'هجومي' if mode=='active' else 'محافظ',
        'strategy':strategy,'account_id':8001,'entry':entry,'stop':stop,
        'target':target,'rr':1.3,'risk_pct':0.25 if mode=='active' else 0.15,
        'status':'OPEN','bar_time':T,'checked_bar_time':T,
        'created_at':datetime.fromtimestamp((T+60000)/1000,tz=timezone.utc).isoformat(),
        'closed_at':None,
    }

def candle(t,low,high):
    return {'t':t,'l':low,'h':high,'o':(low+high)/2,'c':(low+high)/2}

class PaperJournalTests(unittest.TestCase):
    def test_buy_target_bid_spot_and_metrics(self):
        row=record()
        outcomes=settle_on_quote([row],'BTCUSD',bid=103,ask=103.4,
            received_at=(T+90000)/1000,changed_side='bid',account_id=8001)
        self.assertEqual(len(outcomes),1)
        self.assertEqual(row['status'],'TARGET')
        self.assertAlmostEqual(row['result_r'],1.3)
        self.assertAlmostEqual(row['theoretical_equity_pct'],0.325)
        self.assertEqual(row['resolution_source'],'spot')
        self.assertEqual(row['observed_exit_quote'],103)
        self.assertEqual(settle_on_quote([row],'BTCUSD',bid=104,ask=104.2,
            received_at=(T+100000)/1000,changed_side='bid',account_id=8001),[])

    def test_sell_stop_uses_ask_not_bid(self):
        row=record(side='SELL',mode='conservative',ident='012345abcdee')
        r=settle_on_quote([row],'BTCUSD',bid=100.2,ask=102.5,
            received_at=(T+90000)/1000,changed_side='bid',account_id=8001)
        self.assertEqual(r,[])
        self.assertEqual(row['status'],'OPEN')
        r=settle_on_quote([row],'BTCUSD',bid=100.2,ask=102.5,
            received_at=(T+90001)/1000,changed_side='ask',account_id=8001)
        self.assertEqual(len(r),1)
        self.assertEqual(row['status'],'STOP')
        self.assertEqual(row['result_r'],-1)
        self.assertAlmostEqual(row['theoretical_equity_pct'],-0.15)

    def test_wrong_market_or_wrong_account_never_changes(self):
        row=record()
        r=settle_on_quote([row],'BTCUSD',bid=103,ask=104,
            received_at=(T+90000)/1000,changed_side='bid',account_id=111)
        self.assertEqual(r,[])
        self.assertEqual(row['status'],'OPEN')
        r=settle_on_quote([row],'XAUUSD',bid=103,ask=104,
            received_at=(T+90000)/1000,changed_side='bid',account_id=8001)
        self.assertEqual(r,[])

    def test_closed_candle_ambiguous_stop_first(self):
        row=record()
        events=update_paper_journal([row],'BTCUSD',[
            candle(T,99,101),candle(T+60000,97,104)
        ],account_id=8001)
        self.assertEqual(len(events),1)
        self.assertEqual(row['status'],'STOP')
        self.assertTrue(row['ambiguous_candle'])
        self.assertEqual(row['resolution_source'],'m1_candle')
        self.assertEqual(row['result_r'],-1)

    def test_missing_history_is_unverified_not_profit(self):
        row=record()
        events=update_paper_journal([row],'BTCUSD',[
            candle(T+5*60000,99,105)
        ],account_id=8001)
        self.assertEqual(len(events),1)
        self.assertEqual(row['status'],'UNVERIFIED')
        self.assertIsNone(row['closed_at'])

    def test_closed_only_win_rate_grouped_by_mode_and_strategy(self):
        buy=record(mode='active')
        sell=record(side='SELL',mode='conservative',strategy='Mean Reversion',ident='012345abcdee')
        missing=record(mode='active',ident='012345abcdea')
        missing['status']='UNVERIFIED'
        settle_on_quote([buy],'BTCUSD',bid=103,ask=104,
            received_at=(T+90000)/1000,changed_side='bid',account_id=8001)
        settle_on_quote([sell],'BTCUSD',bid=101.8,ask=102.3,
            received_at=(T+90000)/1000,changed_side='ask',account_id=8001)
        perf=compute_performance([buy,sell,missing],account_id=8001)
        self.assertEqual(perf['overall']['total'],3)
        self.assertEqual(perf['overall']['resolved'],2)
        self.assertEqual(perf['overall']['wins'],1)
        self.assertEqual(perf['overall']['losses'],1)
        self.assertEqual(perf['overall']['unverified'],1)
        self.assertAlmostEqual(perf['overall']['win_rate_pct'],50)
        self.assertAlmostEqual(perf['overall']['net_r'],0.3)
        self.assertAlmostEqual(perf['by_mode']['active']['win_rate_pct'],100)
        self.assertAlmostEqual(perf['by_mode']['conservative']['win_rate_pct'],0)
        self.assertEqual(perf['by_strategy']['Mean Reversion']['losses'],1)

    def test_restore_mode_and_outcome_without_mutating_another_account(self):
        row=record()
        row['status']='STOP'
        row['closed_at']=datetime.fromtimestamp((T+120000)/1000,tz=timezone.utc).isoformat()
        row['result_r']=None
        result=sanitize_client_backup([row],8001)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['status'],'STOP')
        self.assertEqual(result[0]['result_r'],-1)
        self.assertEqual(sanitize_client_backup([row],9999),[])

if __name__=='__main__':
    unittest.main()
