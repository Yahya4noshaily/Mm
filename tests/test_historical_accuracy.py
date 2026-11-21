import unittest
from historical_accuracy import accuracy_snapshot

class HistoricalAccuracyTests(unittest.TestCase):
    def signal(self, key, status, market="BTCUSD", mode="active", strategy="Trend Pullback", account=123):
        return {"id":key,"status":status,"market":market,"mode":mode,
                "strategy":strategy,"account_id":account}
    def test_empty_history_has_no_accuracy_instead_of_zero(self):
        a=accuracy_snapshot([],123,"BTCUSD","active","Trend Pullback")
        self.assertIsNone(a["win_rate_pct"])
        self.assertIsNone(a["ci_lower_pct"])
        self.assertEqual(a["resolved"],0)
    def test_one_or_two_wins_is_not_marked_reliable(self):
        r=[self.signal("a","TARGET"),self.signal("b","TARGET")]
        a=accuracy_snapshot(r,123,"BTCUSD","active","Trend Pullback")
        self.assertEqual(a["win_rate_pct"],100.0)
        self.assertEqual(a["wins"],2)
        self.assertEqual(a["resolved"],2)
        self.assertEqual(a["sample_status"],"small")
        self.assertAlmostEqual(a["ci_lower_pct"],34.2,delta=.2)
    def test_open_and_unverified_are_not_wins_or_losses(self):
        r=[self.signal("a","TARGET"),self.signal("b","STOP"),
           self.signal("c","OPEN"),self.signal("d","UNVERIFIED")]
        a=accuracy_snapshot(r,123,"BTCUSD","active","Trend Pullback")
        self.assertEqual(a["resolved"],2)
        self.assertEqual(a["wins"],1)
        self.assertEqual(a["losses"],1)
        self.assertEqual(a["open"],1)
        self.assertEqual(a["unverified"],1)
        self.assertEqual(a["win_rate_pct"],50.0)
    def test_market_mode_strategy_and_account_isolation(self):
        r=[self.signal("a","TARGET"),self.signal("b","STOP","XAUUSD"),
           self.signal("c","STOP",mode="conservative"),
           self.signal("d","STOP",strategy="Mean Reversion"),
           self.signal("e","STOP",account=333)]
        a=accuracy_snapshot(r,123,"BTCUSD","active","Trend Pullback")
        self.assertEqual(a["resolved"],1)
        self.assertEqual(a["win_rate_pct"],100.0)
    def test_thresholds_are_descriptive_only(self):
        for n,expected in [(19,"small"),(20,"limited"),(49,"limited"),(50,"observational")]:
            rows=[self.signal(str(i),"STOP") for i in range(n)]
            a=accuracy_snapshot(rows,123,"BTCUSD","active","Trend Pullback")
            self.assertEqual(a["sample_status"],expected)
            self.assertEqual(a["win_rate_pct"],0.0)
    def test_legacy_mode_is_inferred_from_rr(self):
        row=self.signal("a","TARGET")
        row.pop("mode")
        row["rr"]=1.3
        a=accuracy_snapshot([row],123,"BTCUSD","active","Trend Pullback")
        self.assertEqual(a["resolved"],1)

if __name__=="__main__":
    unittest.main()
