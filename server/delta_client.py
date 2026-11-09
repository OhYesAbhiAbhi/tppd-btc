"""Minimal signed REST client for Delta Exchange India (production and testnet).

Signature = HMAC-SHA256(secret, method + timestamp + path + query_string + body), hex.
Only GET requests are retried on network errors; order placement is never
auto-retried, so a flaky connection can't create duplicate orders.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import socket

import requests
import urllib3.util.connection as _urllib3_conn

# Force IPv4: Delta's API-key whitelist holds the server's static IPv4 address.
_urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

BASE_URLS = {
    "prod": "https://api.india.delta.exchange",
    "testnet": "https://cdn-ind.testnet.deltaex.org",
}


class DeltaAPIError(Exception):
    pass


class DeltaClient:
    def __init__(self, api_key=None, api_secret=None, env="prod", timeout=10):
        if env not in BASE_URLS:
            raise ValueError(f"env must be one of {list(BASE_URLS)}")
        self.base = BASE_URLS[env]
        self.key = api_key
        self.secret = api_secret
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------ core
    def _request(self, method, path, params=None, body=None, auth=False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        qs = ("?" + urlencode(params)) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        attempts = 3 if method == "GET" else 1

        for attempt in range(attempts):
            headers = {
                "User-Agent": "tppd-btc-bot/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if auth:
                if not (self.key and self.secret):
                    raise DeltaAPIError("API key/secret missing (check .env)")
                ts = str(int(time.time()))
                msg = method + ts + path + qs + payload
                sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
                headers.update({"api-key": self.key, "timestamp": ts, "signature": sig})
            try:
                r = self.session.request(
                    method, self.base + path + qs, data=payload or None,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt == attempts - 1:
                    raise DeltaAPIError(f"network error on {method} {path}: {e}")
                time.sleep(1 + attempt)
                continue

            if r.status_code == 429 and attempt < attempts - 1:
                reset_ms = int(r.headers.get("X-RATE-LIMIT-RESET", "1000"))
                time.sleep(reset_ms / 1000 + 0.5)
                continue
            try:
                data = r.json()
            except ValueError:
                raise DeltaAPIError(f"HTTP {r.status_code} on {path}: {r.text[:200]}")
            if not data.get("success", False):
                raise DeltaAPIError(f"{method} {path} failed: {data.get('error') or data}")
            return data.get("result")
        raise DeltaAPIError(f"{method} {path}: retries exhausted")

    # ---------------------------------------------------------------- public
    def candles(self, symbol, resolution, start, end):
        """OHLCV candles; start/end are unix seconds. Returned oldest-first."""
        res = self._request("GET", "/v2/history/candles", {
            "resolution": resolution, "symbol": symbol, "start": int(start), "end": int(end)})
        return sorted(res or [], key=lambda c: c["time"])

    def option_chain(self, underlying, expiry_ddmmyyyy):
        return self._request("GET", "/v2/tickers", {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
            "expiry_date": expiry_ddmmyyyy,
        }) or []

    def ticker(self, symbol):
        return self._request("GET", f"/v2/tickers/{symbol}")

    def product(self, symbol):
        return self._request("GET", f"/v2/products/{symbol}")

    # --------------------------------------------------------------- private
    def place_order(self, **order):
        return self._request("POST", "/v2/orders", body=order, auth=True)

    def cancel_order(self, order_id, product_id):
        return self._request("DELETE", "/v2/orders",
                             body={"id": order_id, "product_id": product_id}, auth=True)

    def get_order(self, order_id):
        return self._request("GET", f"/v2/orders/{order_id}", auth=True)

    def positions(self):
        return self._request("GET", "/v2/positions/margined", auth=True) or []

    def wallet(self):
        return self._request("GET", "/v2/wallet/balances", auth=True)
