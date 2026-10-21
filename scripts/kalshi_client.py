"""
Authenticated Kalshi API client (shared by the trading scripts).

Supports both key types Kalshi issues: Ed25519 (current default) and RSA
(older keys, signed with PSS / DIGEST_LENGTH salt). Each request signs
"{timestamp_ms}{METHOD}{path without query}".

Credentials come from env (see ~/.kalshi/env):
    KALSHI_API_KEY        key id
    KALSHI_RSA_KEY_PATH   path to the private key PEM (any key type)

    python scripts/kalshi_client.py    # prints balance + open positions
"""

import base64
import json
import os
import sys
import time
from typing import Dict, List, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

KALSHI_HOST = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"


class KalshiClient:
    def __init__(self, api_key: str = "", private_key_path: str = ""):
        from cryptography.hazmat.primitives import serialization

        self.api_key = api_key or os.environ.get("KALSHI_API_KEY", "")
        key_path = private_key_path or os.environ.get("KALSHI_RSA_KEY_PATH", "")
        if not self.api_key or not key_path:
            raise SystemExit("Set KALSHI_API_KEY and KALSHI_RSA_KEY_PATH (source ~/.kalshi/env)")
        with open(os.path.expanduser(key_path), "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _signature(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding

        if isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            return self.private_key.sign(message)
        return self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def _headers(self, method: str, full_path: str) -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method}{full_path.split('?')[0]}".encode()
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(self._signature(message)).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        full_path = f"{API_PREFIX}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"{KALSHI_HOST}{full_path}", data=data,
                      headers=self._headers(method, full_path), method=method)
        try:
            with urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
        except HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Kalshi {method} {path} -> {e.code}: {detail}") from None
        return json.loads(raw) if raw else {}

    # -- Portfolio --

    def balance_dollars(self) -> float:
        return float(self.request("GET", "/portfolio/balance").get("balance_dollars", 0))

    def positions(self) -> List[dict]:
        return self.request("GET", "/portfolio/positions?limit=200").get("market_positions", [])

    def resting_orders(self) -> List[dict]:
        return self.request("GET", "/portfolio/orders?status=resting&limit=200").get("orders", [])

    def fills(self, limit: int = 100) -> List[dict]:
        return self.request("GET", f"/portfolio/fills?limit={limit}").get("fills", [])


if __name__ == "__main__":
    client = KalshiClient()
    print(f"Balance: ${client.balance_dollars():.2f}")
    positions = [p for p in client.positions() if float(p.get("position_fp", p.get("position", 0)) or 0)]
    print(f"Open positions: {len(positions)}")
    for p in positions:
        print(f"  {p.get('ticker')}: {p.get('position_fp', p.get('position'))}")
    print(f"Resting orders: {len(client.resting_orders())}")
    sys.exit(0)
