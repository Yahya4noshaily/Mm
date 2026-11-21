import unittest
from connection_health import CTraderApiError, retry_delay_seconds, user_status

class ConnectionHealthTests(unittest.TestCase):
    def test_routing_backoff_instead_of_five_second_hammering(self):
        times=[retry_delay_seconds("CANT_ROUTE_REQUEST",n,now=1000) for n in (1,2,3,4,5,6)]
        self.assertEqual(times,[15,30,60,120,180,180])
    def test_honor_server_retry_after_for_blocked_history(self):
        error=CTraderApiError(2137,{"errorCode":"BLOCKED_PAYLOAD_TYPE","retryAfter":93})
        self.assertEqual(error.code,"BLOCKED_PAYLOAD_TYPE")
        self.assertEqual(error.request_type,2137)
        self.assertEqual(retry_delay_seconds(error.code,1,error.retry_after,now=100),93)
    def test_maintenance_timestamp_is_honored(self):
        self.assertEqual(retry_delay_seconds("SERVER_IS_UNDER_MAINTENANCE",1,0,1300,now=1000),300)
    def test_untrusted_time_is_clamped(self):
        error=CTraderApiError(2137,{"errorCode":"BLOCKED_PAYLOAD_TYPE","retryAfter":"garbled"})
        self.assertEqual(error.retry_after,0)
        self.assertEqual(retry_delay_seconds(error.code,1,error.retry_after,now=1000),30)
    def test_message_never_claims_no_setup_when_router_unavailable(self):
        self.assertIn("cTrader",user_status("CANT_ROUTE_REQUEST"))
        self.assertIn("CANT_ROUTE_REQUEST",user_status("CANT_ROUTE_REQUEST"))
    def test_normal_short_transient_backoff(self):
        self.assertEqual(retry_delay_seconds("CONNECTION_ERROR",1,now=100),5)

if __name__=="__main__":
    unittest.main()
