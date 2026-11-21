"""Encrypted client-held cTrader authorization state.

A Free Render service is ephemeral. An encrypted HttpOnly, Secure cookie
allows the owner's browser to rehydrate a read-only cTrader connection after
a restart without storing OAuth credentials in JavaScript/localStorage.
The browser must open the site again after spin-down; it does NOT provide
unattended 24/7 monitoring.
"""
import base64
import hashlib
import json
import os
from cryptography.fernet import Fernet, InvalidToken

COOKIE_NAME = 'qs_ctrader_auth_v2'
COOKIE_MAX_AGE = 90 * 24 * 3600


def _cipher():
    secret = os.environ.get('SITE_SECRET', '')
    if len(secret) < 20:
        raise ValueError('SITE_SECRET must be a stable, strong private value in Render Environment')
    key = base64.urlsafe_b64encode(hashlib.sha256(
        ('QuantumSimons:OAuthCookie:v2:' + secret).encode('utf-8')
    ).digest())
    return Fernet(key)


def serialize_auth(access_token, refresh_token, expires_at, accounts, account):
    payload = {
        'access': access_token or '',
        'refresh': refresh_token or '',
        'expires': float(expires_at or 0),
        'accounts': [
            {'id': int(row['id']), 'isLive': bool(row.get('isLive')),
             'brokerTitle': str(row.get('brokerTitle', ''))[:80]}
            for row in (accounts or [])[:30]
        ],
        'selected': int(account['id']) if account else None,
    }
    data = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return _cipher().encrypt(data).decode('ascii')


def deserialize_auth(cookie):
    if not cookie:
        return None
    try:
        raw = _cipher().decrypt(cookie.encode('ascii'), ttl=COOKIE_MAX_AGE)
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('access'), str):
            return None
        if not isinstance(data.get('accounts'), list):
            return None
        return data
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError, KeyError):
        return None


def attach_cookie(response, token, refresh, expires, accounts, selected_account):
    packed = serialize_auth(token, refresh, expires, accounts, selected_account)
    response.set_cookie(COOKIE_NAME, packed, max_age=COOKIE_MAX_AGE,
                        secure=True, httponly=True, samesite='lax', path='/')
    return response


def delete_cookie(response):
    response.delete_cookie(COOKIE_NAME, path='/', secure=True, httponly=True, samesite='lax')
    return response
