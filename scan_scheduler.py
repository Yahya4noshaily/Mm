"""Independent scan scheduler; never changes quantitative strategy predicates.

Last-evaluated minute advances only after a successful candle/strategy scan.
Transient broker data errors are retried within the same minute. Separate
market tasks mean BTCUSD cannot block XAUUSD, and vice versa.
"""
MAX_ATTEMPTS_PER_MINUTE = 3
RETRY_DELAY_SECONDS = 6.0


def can_start_scan(health, now, minute, last_evaluated_minute, subscribed):
    if not subscribed:
        return False
    if (now % 60) < 2:
        return False
    if last_evaluated_minute == minute:
        return False
    if bool(health.get("scan_inflight")):
        return False
    if float(health.get("next_retry_at") or 0) > now:
        return False
    if int(health.get("scan_attempt_minute") or -1) == minute and \
       int(health.get("scan_attempts_this_minute") or 0) >= MAX_ATTEMPTS_PER_MINUTE:
        return False
    return True


def note_scan_attempt(health, minute, now):
    if int(health.get("scan_attempt_minute") or -1) != minute:
        health["scan_attempts_this_minute"] = 0
        health["next_retry_at"] = 0
    health["scan_attempt_minute"] = minute
    health["scan_attempts_this_minute"] += 1
    health["scan_attempts_total"] = int(health.get("scan_attempts_total") or 0) + 1
    health["last_scan_attempt_at"] = now
    health["scan_inflight"] = True


def note_scan_success(health, now):
    health["scan_inflight"] = False
    health["last_scan_success_at"] = now
    health["scan_successes_total"] = int(health.get("scan_successes_total") or 0) + 1
    health["next_retry_at"] = 0
    health["analysis_error"] = None
    health["last_scan_error_code"] = None


def note_scan_failure(health, now, reason):
    health["scan_inflight"] = False
    health["last_scan_failure_at"] = now
    health["scan_failures_total"] = int(health.get("scan_failures_total") or 0) + 1
    health["analysis_error"] = reason
    health["last_scan_error_code"] = reason
    health["next_retry_at"] = now + RETRY_DELAY_SECONDS


def scan_diagnostics(health, now, last_eval, last_bar_time, decision, quote_fresh):
    last_success = health.get("last_scan_success_at")
    last_attempt = health.get("last_scan_attempt_at")
    attempts = int(health.get("scan_attempts_total") or 0)
    failures = int(health.get("scan_failures_total") or 0)
    last_bar_age = None
    if last_bar_time is not None:
        try:
            last_bar_age = max(0, round(now - (int(last_bar_time) / 1000 + 60)))
        except (TypeError, ValueError):
            pass
    return {
        "last_scan_success_at": last_success,
        "last_scan_attempt_at": last_attempt,
        "scan_attempts": attempts,
        "scan_failures": failures,
        "scan_inflight": bool(health.get("scan_inflight")),
        "last_scanned_minute": last_eval,
        "bar_age_seconds": last_bar_age,
        "last_scan_error_code": health.get("last_scan_error_code"),
        "next_retry_at": health.get("next_retry_at"),
        "reason": str(decision.get("reason", "لم تُفحص الاستراتيجية بعد"))[:220],
        "scan_state": (
            "ERROR_RETRY" if health.get("analysis_error") else
            "WAIT_QUOTES" if not quote_fresh else
            "WAIT_CANDLES" if last_bar_age is None or last_bar_age > 180 else
            "SCANNING" if health.get("scan_inflight") else
            "READY" if last_success else "WARMUP"
        ),
    }
