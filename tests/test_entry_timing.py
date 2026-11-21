import unittest
from entry_timing import create_watch,assess_watch,still_actionable,public_watch,MAX_WATCH_SECONDS,ENTRY_VALID_SECONDS

START=1800000000.0
BAR=int(START//60)*60000
SELL={"side":"SELL","strategy":"Compression Breakout","reason":"اختراق هابط",
      "entry":100.0,"stop":110.0,"target":87.0,"rr":1.3,"bar_time":BAR}
BUY={"side":"BUY","strategy":"Trend Pullback","reason":"تصحيح صاعد",
     "entry":100.0,"stop":90.0,"target":113.0,"rr":1.3,"bar_time":BAR}
def bar(t,o,h,l,c):return {"t":t,"o":o,"h":h,"l":l,"c":c}
def bearish():return bar(BAR+60000,104.2,105,102,103.2)
def bullish():return bar(BAR+60000,95.3,97.2,95,96.8)

class EntryTimingTests(unittest.TestCase):
    def sell(self):
        return create_watch(SELL,"BTCUSD","active",123,START,0.2)
    def buy(self):
        return create_watch(BUY,"BTCUSD","active",123,START,0.2)
    def test_original_signal_is_watch_not_immediate_entry(self):
        w=self.sell();self.assertIsNotNone(w)
        self.assertGreater(w["zone_lower"],SELL["entry"])
        self.assertLess(w["zone_upper"],SELL["stop"])
        self.assertEqual(assess_watch(w,[bearish()],103.3,103.5,START+2,.2)["action"],"WAIT")
    def test_confirmed_sell_retest_reprices_stop_and_target(self):
        w=self.sell()
        out=assess_watch(w,[bearish()],103.3,103.5,START+130,.2)
        self.assertEqual(out["action"],"READY")
        s=out["signal"]
        self.assertEqual(s["side"],"SELL")
        self.assertGreater(s["entry"],SELL["entry"])
        self.assertEqual(s["stop"],SELL["stop"])
        self.assertAlmostEqual((s["stop"]-s["entry"])/(s["entry"]-s["target"]),1/1.3,places=4)
        self.assertIn("valid_until",s)
    def test_confirmed_buy_retest_improves_entry(self):
        w=self.buy();self.assertIsNotNone(w)
        out=assess_watch(w,[bullish()],96.6,96.8,START+130,.2)
        self.assertEqual(out["action"],"READY")
        self.assertLess(out["signal"]["entry"],BUY["entry"])
        self.assertEqual(out["signal"]["stop"],BUY["stop"])
    def test_wrong_direction_no_entry(self):
        w=self.sell()
        wrong=bar(BAR+60000,103,105,102.1,104.1)
        self.assertEqual(assess_watch(w,[wrong],103.2,103.4,START+130,.2)["action"],"WAIT")
    def test_same_candle_no_confirmation(self):
        w=self.sell()
        self.assertEqual(assess_watch(w,[bar(BAR,104.2,105,102,103.2)],103.3,103.5,START+130,.2)["action"],"WAIT")
    def test_stop_hit_before_entry_cancels(self):
        w=self.sell()
        r=assess_watch(w,[],108.9,110,START+65,.2)
        self.assertEqual(r["action"],"CANCEL")
    def test_candle_violating_stop_cancels(self):
        w=self.sell()
        r=assess_watch(w,[bar(BAR+60000,109,111,103,104)],103.6,103.8,START+130,.2)
        self.assertEqual(r["action"],"CANCEL")
    def test_expiry_no_entry(self):
        w=self.sell()
        self.assertEqual(assess_watch(w,[],103,103.2,START+MAX_WATCH_SECONDS,.2)["action"],"CANCEL")
    def test_dont_chase_price_beyond_zone(self):
        w=self.sell()
        self.assertEqual(assess_watch(w,[bearish()],99,99.2,START+130,.2)["action"],"WAIT")
    def test_tight_spread_adjusted_stop_rejected(self):
        self.assertIsNone(create_watch(SELL,"BTCUSD","active",123,START,3))
    def test_spread_spike_blocks_confirmation(self):
        w=self.sell()
        self.assertEqual(assess_watch(w,[bearish()],103.3,104.3,START+130,1)["action"],"WAIT")
    def test_signal_expiration_and_drift(self):
        w=self.sell();s=assess_watch(w,[bearish()],103.3,103.5,START+130,.2)["signal"]
        self.assertTrue(still_actionable(s,103.3,103.5,START+132))
        self.assertFalse(still_actionable(s,103.3,103.5,START+130+ENTRY_VALID_SECONDS+1))
        self.assertFalse(still_actionable(s,99,99.2,START+132))
    def test_watch_is_visible_not_a_trade(self):
        w=public_watch(self.sell())
        self.assertEqual(w["action"],"WAIT_ZONE")
        self.assertNotIn("target",w)
        self.assertNotIn("account_id",w)
if __name__=="__main__":
    unittest.main()
