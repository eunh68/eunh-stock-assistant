"""Fetch current prices for the watchlist from the KIS (한국투자증권) Open API
and write the result to data/quotes.json.

Requires KIS_APP_KEY / KIS_APP_SECRET in the environment (populated from
GitHub Secrets by the update-quotes workflow). Never hardcode real keys here.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "data" / "watchlist.json"
OUTPUT_PATH = ROOT / "data" / "quotes.json"

BASE_URL = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.environ["KIS_APP_KEY"]
APP_SECRET = os.environ["KIS_APP_SECRET"]

KST = timezone(timedelta(hours=9))
UP_SIGNS = {"1", "2"}
DOWN_SIGNS = {"4", "5"}


def get_access_token() -> str:
    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_price(token: str, code: str) -> dict:
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers={
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "FHKST01010100",
            "custtype": "P",
        },
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("rt_cd") != "0":
        raise RuntimeError(f"{code} 조회 실패: {body.get('msg1')}")
    return body["output"]


def main() -> None:
    watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    token = get_access_token()

    stocks = []
    for item in watchlist:
        output = fetch_price(token, item["code"])

        sign_code = output["prdy_vrss_sign"]
        direction = "up" if sign_code in UP_SIGNS else "down" if sign_code in DOWN_SIGNS else "flat"

        price = int(output["stck_prpr"])
        raw_diff = abs(int(float(output["prdy_vrss"])))
        raw_rate = abs(float(output["prdy_ctrt"]))

        stocks.append({
            "name": item["name"],
            "code": item["code"],
            "price": price,
            "diff": raw_diff if direction == "up" else -raw_diff if direction == "down" else 0,
            "rate": raw_rate if direction == "up" else -raw_rate if direction == "down" else 0.0,
            "direction": direction,
        })

        time.sleep(0.25)  # stay well under the KIS per-second rate limit

    payload = {
        "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "stocks": stocks,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(stocks)} quotes to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
