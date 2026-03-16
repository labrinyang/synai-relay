"""Thin HTTP client for OKX OnchainOS REST API with HMAC authentication."""
import base64
import hashlib
import hmac
import json
import logging
import time as _time
import urllib.parse
from datetime import datetime, timezone

import requests

logger = logging.getLogger('relay.onchainos')


class OnchainOSClient:

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 project_id: str = "", base_url: str = "https://web3.okx.com"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.project_id = project_id
        self._base_url = base_url

    def _sign(self, timestamp: str, method: str, path: str,
              body: str = "") -> str:
        prehash = timestamp + method.upper() + path + body
        sig = hmac.new(
            self.secret_key.encode(), prehash.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        now = datetime.now(timezone.utc)
        ts = now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-PROJECT": self.project_id,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: str = "",
                 params: dict = None) -> dict:
        """Execute HTTP request with retry on transient failures."""
        max_retries = 2
        last_error = None

        # Build full signing path (includes query string for GET)
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        sign_path = path + query

        for attempt in range(max_retries + 1):
            try:
                # Re-sign on each attempt (timestamp must be fresh)
                headers = self._headers(method.upper(), sign_path, body)
                url = f"{self._base_url}{sign_path}"

                if method.upper() == "POST":
                    resp = requests.post(url, headers=headers, data=body,
                                         timeout=30)
                else:
                    resp = requests.get(url, headers=headers, timeout=30)

                if resp.status_code in (429, 502, 503) and attempt < max_retries:
                    logger.warning("OnchainOS %s %s returned %s, retrying "
                                   "(attempt %d/%d)", method.upper(), path,
                                   resp.status_code, attempt + 1, max_retries)
                    _time.sleep(2 ** attempt)
                    continue

                resp.raise_for_status()
                result = resp.json()
                if result.get("code") != "0":
                    raise RuntimeError(
                        f"OnchainOS error: code={result.get('code')} "
                        f"msg={result.get('msg')}")
                return result

            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning("OnchainOS %s %s connection error, "
                                   "retrying (attempt %d/%d): %s",
                                   method.upper(), path, attempt + 1,
                                   max_retries, e)
                    _time.sleep(2 ** attempt)
                    continue
                raise

        raise last_error  # pragma: no cover

    def post(self, path: str, data: dict) -> dict:
        body = json.dumps(data)
        return self._request("POST", path, body=body)

    def get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)
