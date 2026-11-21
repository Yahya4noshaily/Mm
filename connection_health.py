"""cTrader Open API upstream-error classification. No trading strategy logic."""
import math
import time

ROUTING_ERRORS = frozenset((
    "CANT_ROUTE_REQUEST", "CH_SERVER_NOT_REACHABLE",
    "SERVER_IS_UNDER_MAINTENANCE"
))
HISTORICAL_RATE_LIMITS = frozenset((
    "BLOCKED_PAYLOAD_TYPE", "REQUEST_FREQUENCY_EXCEEDED"
))
FRIENDLY = {
    "CANT_ROUTE_REQUEST": "cTrader لا يستطيع توجيه طلبات الحساب حاليًا (CANT_ROUTE_REQUEST). لا يمكن إصدار إشارات جديدة دون بيانات موثوقة.",
    "CH_SERVER_NOT_REACHABLE": "خادم التداول المرتبط بحساب cTrader غير متاح حاليًا.",
    "SERVER_IS_UNDER_MAINTENANCE": "خادم cTrader يخضع لصيانة؛ الماسح ينتظر عودة الاتصال.",
    "BLOCKED_PAYLOAD_TYPE": "cTrader قيّد طلبات البيانات مؤقتًا (BLOCKED_PAYLOAD_TYPE). سيتم احترام مهلة إعادة المحاولة.",
    "REQUEST_FREQUENCY_EXCEEDED": "تم تجاوز عدد طلبات cTrader المسموح به؛ إعادة المحاولة بعد التهدئة.",
}
class CTraderApiError(RuntimeError):
    """Safe structured error; never log the token or full account payload."""
    def __init__(self, request_type, payload=None):
        data=payload if isinstance(payload,dict) else {}
        self.request_type=int(request_type)
        self.code=str(data.get("errorCode") or "API_ERROR")[:80]
        try:self.retry_after=max(0,min(3600,float(data.get("retryAfter") or 0)))
        except (ValueError,TypeError):self.retry_after=0.0
        try:self.maintenance_until=float(data.get("maintenanceEndTimestamp") or 0)
        except (ValueError,TypeError):self.maintenance_until=0.0
        super().__init__(self.code)

def retry_delay_seconds(code,consecutive_failures,retry_after=0,maintenance_until=0,now=None):
    """Exponential backoff for server routing failures; honor API retryAfter."""
    now=time.time() if now is None else float(now)
    n=max(1,int(consecutive_failures))
    if code in ROUTING_ERRORS:
        base=15 if code!="SERVER_IS_UNDER_MAINTENANCE" else 45
        delay=min(180,base*2**min(4,n-1))
    elif code in HISTORICAL_RATE_LIMITS:
        delay=30
    else:
        delay=min(40,5*2**min(3,n-1))
    try:
        reported=float(retry_after)
        if math.isfinite(reported):delay=max(delay,min(reported,3600))
    except (TypeError,ValueError):pass
    try:
        maintenance=float(maintenance_until)
        if math.isfinite(maintenance) and maintenance>now:
            delay=max(delay,min(3600,maintenance-now))
    except (TypeError,ValueError):pass
    return max(5,min(3600,round(delay)))

def user_status(code):
    return FRIENDLY.get(str(code),"تعذر الحصول على بيانات cTrader. لا يتم إصدار إشارات أثناء فشل الاتصال.")
