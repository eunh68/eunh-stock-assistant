"""Fetch current prices for the watchlist from the KIS (한국투자증권) Open API
and write the result to data/quotes.json.

Also evaluates the sell-review alert rule from data/profile.md ("매도 규칙")
for individual stocks (type == "stock"): if the stock's return since either
its post-purchase peak or its average cost (avg_price in watchlist.json)
has lagged its market index by 10 percentage points or more, it is flagged
with sellAlert = true. This is a notice only — nothing here ever places an
order.

Requires KIS_APP_KEY / KIS_APP_SECRET in the environment (populated from
GitHub Secrets by the update-quotes workflow). Never hardcode real keys here.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "data" / "watchlist.json"
OUTPUT_PATH = ROOT / "data" / "quotes.json"
ALERT_STATE_PATH = ROOT / "data" / "alert_state.json"

BASE_URL = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.environ["KIS_APP_KEY"]
APP_SECRET = os.environ["KIS_APP_SECRET"]

KST = timezone(timedelta(hours=9))
UP_SIGNS = {"1", "2"}
DOWN_SIGNS = {"4", "5"}
SELL_ALERT_GAP = 10.0  # percentage points, from profile.md 매도 규칙

# 코스피 -> KOSPI, 코스닥 -> KOSDAQ (profile.md 지수 자동매칭)
MARKET_INDEX_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}


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
        raise RuntimeError(f"{code} 시세 조회 실패: {body.get('msg1')}")
    return body["output"]


def fetch_index_level(token: str, index_code: str) -> float:
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price",
        headers={
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "FHPUP02100000",
            "custtype": "P",
        },
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("rt_cd") != "0":
        raise RuntimeError(f"지수({index_code}) 조회 실패: {body.get('msg1')}")
    return float(body["output"]["bstp_nmix_prpr"])


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def evaluate_sell_alert(token: str, item: dict, price: int, state: dict) -> tuple[bool, dict]:
    """Returns (sellAlert, updated_state_entry) for one individual stock.

    Approximation: since we don't have the index level on the actual
    purchase date, "entry" and "peak" index levels are snapshotted the
    first time this script observes them, not retroactively. Treat the
    comparison as relative to when tracking started, not the real
    purchase date.
    """
    avg_price = item.get("avg_price")
    market = item.get("market")
    index_code = MARKET_INDEX_CODE.get(market)

    if avg_price is None or index_code is None:
        return False, state

    index_level = fetch_index_level(token, index_code)

    peak_price = max(state.get("peakPrice", price), price, avg_price)
    peak_index_level = state.get("peakIndexLevel", index_level)
    entry_index_level = state.get("entryIndexLevel", index_level)

    if price >= peak_price:
        peak_price = price
        peak_index_level = index_level

    cost_return = (price - avg_price) / avg_price * 100
    index_cost_return = (index_level - entry_index_level) / entry_index_level * 100
    cost_gap = cost_return - index_cost_return

    peak_return = (price - peak_price) / peak_price * 100
    index_peak_return = (index_level - peak_index_level) / peak_index_level * 100
    peak_gap = peak_return - index_peak_return

    sell_alert = cost_gap <= -SELL_ALERT_GAP or peak_gap <= -SELL_ALERT_GAP

    updated_state = {
        "peakPrice": peak_price,
        "peakIndexLevel": peak_index_level,
        "entryIndexLevel": entry_index_level,
    }
    return sell_alert, updated_state


def main() -> None:
    watchlist = load_json(WATCHLIST_PATH, [])
    alert_state = load_json(ALERT_STATE_PATH, {})
    token = get_access_token()

    stocks = []
    for item in watchlist:
        output = fetch_price(token, item["code"])

        sign_code = output["prdy_vrss_sign"]
        direction = "up" if sign_code in UP_SIGNS else "down" if sign_code in DOWN_SIGNS else "flat"

        price = int(output["stck_prpr"])
        raw_diff = abs(int(float(output["prdy_vrss"])))
        raw_rate = abs(float(output["prdy_ctrt"]))

        entry = {
            "name": item["name"],
            "code": item["code"],
            "type": item.get("type", "stock"),
            "price": price,
            "diff": raw_diff if direction == "up" else -raw_diff if direction == "down" else 0,
            "rate": raw_rate if direction == "up" else -raw_rate if direction == "down" else 0.0,
            "direction": direction,
        }

        if item.get("type") == "stock":
            entry["avgPrice"] = item.get("avg_price")
            try:
                sell_alert, updated_state = evaluate_sell_alert(
                    token, item, price, alert_state.get(item["code"], {})
                )
                entry["sellAlert"] = sell_alert
                alert_state[item["code"]] = updated_state
            except Exception as exc:  # noqa: BLE001 - never let alert logic break the price feed
                print(f"[warn] {item['name']} 매도 알림 계산 실패: {exc}", file=sys.stderr)
                entry["sellAlert"] = False

        stocks.append(entry)
        time.sleep(0.25)  # stay well under the KIS per-second rate limit

    payload = {
        "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "stocks": stocks,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ALERT_STATE_PATH.write_text(json.dumps(alert_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(stocks)} quotes to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
