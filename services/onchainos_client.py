"""Thin HTTP client for OKX OnchainOS REST API with HMAC authentication."""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger('relay.onchainos')


class OnchainOSClient:
    BASE_URL = "https://web3.okx.com"

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 project_id: str = ""):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.project_id = project_id

    def _sign(self, timestamp: str, method: str, path: str,
              body: str = "") -> str:
        prehash = timestamp + method.upper() + path + body
        sig = hmac.new(
            self.secret_key.encode(), prehash.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-PROJECT": self.project_id,
            "Content-Type": "application/json",
        }

    def post(self, path: str, data: dict) -> dict:
        body = json.dumps(data)
        headers = self._headers("POST", path, body)
        url = self.BASE_URL + path
        resp = requests.post(url, headers=headers, data=body, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != "0":
            raise RuntimeError(
                f"OnchainOS error: code={result.get('code')} msg={result.get('msg')}")
        return result

    def get(self, path: str, params: dict = None) -> dict:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        full_path = path + query
        headers = self._headers("GET", full_path)
        url = self.BASE_URL + full_path
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != "0":
            raise RuntimeError(
                f"OnchainOS error: code={result.get('code')} msg={result.get('msg')}")
        return result
