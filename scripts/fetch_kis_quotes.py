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

The KIS access token is valid for 24h and KIS asks that it not be reissued
more often than necessary, so it's cached in TOKEN_CACHE_PATH (restored /
saved by the workflow via actions/cache — never committed to git, since
this repo is public and the token is a live bearer credential).
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
TOKEN_CACHE_PATH = ROOT / ".kis_token_cache.json"

BASE_URL = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.environ["KIS_APP_KEY"]
APP_SECRET = os.environ["KIS_APP_SECRET"]

KST = timezone(timedelta(hours=9))
UP_SIGNS = {"1", "2"}
DOWN_SIGNS = {"4", "5"}
SELL_ALERT_GAP = 10.0  # percentage points, from profile.md 매도 규칙
TOKEN_TTL_SECONDS = 23 * 60 * 60  # KIS 토큰 유효기간은 24h — 여유를 두고 23h로 캐시

# 코스피 -> KOSPI, 코스닥 -> KOSDAQ (profile.md 지수 자동매칭)
MARKET_INDEX_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}

# 매도 알림 규칙이 적용되는 계좌 (일반 위탁만 — ISA/연금저축은 규칙 미적용)
ALERT_ELIGIBLE_ACCOUNTS = {"위탁"}


def get_access_token() -> str:
    cached = load_json(TOKEN_CACHE_PATH, None)
    if cached:
        try:
            issued_at = datetime.fromisoformat(cached["issuedAt"])
            age = (datetime.now(timezone.utc) - issued_at).total_seconds()
            if 0 <= age < TOKEN_TTL_SECONDS:
                return cached["accessToken"]
        except (KeyError, ValueError, TypeError):
            pass  # corrupt/unexpected cache contents — fall through and reissue

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
    token = resp.json()["access_token"]

    TOKEN_CACHE_PATH.write_text(
        json.dumps({"accessToken": token, "issuedAt": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    return token


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


def evaluate_sell_alert(index_level: float, avg_price: float, price: int, state: dict) -> tuple[bool, dict]:
    """Returns (sellAlert, updated_state_entry) for one individual stock.

    Approximation: since we don't have the index level on the actual
    purchase date, "entry" and "peak" index levels are snapshotted the
    first time this script observes them, not retroactively. Treat the
    comparison as relative to when tracking started, not the real
    purchase date.
    """
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

    price_cache: dict[str, dict] = {}
    index_cache: dict[str, float] = {}

    stocks = []
    for item in watchlist:
        code = item["code"]
        if code not in price_cache:
            price_cache[code] = fetch_price(token, code)
            time.sleep(0.25)  # stay well under the KIS per-second rate limit
        output = price_cache[code]

        sign_code = output["prdy_vrss_sign"]
        direction = "up" if sign_code in UP_SIGNS else "down" if sign_code in DOWN_SIGNS else "flat"

        price = int(output["stck_prpr"])
        raw_diff = abs(int(float(output["prdy_vrss"])))
        raw_rate = abs(float(output["prdy_ctrt"]))

        entry = {
            "name": item["name"],
            "code": code,
            "account": item.get("account"),
            "type": item.get("type", "stock"),
            "shares": item.get("shares"),
            "avgPrice": item.get("avg_price"),
            "price": price,
            "diff": raw_diff if direction == "up" else -raw_diff if direction == "down" else 0,
            "rate": raw_rate if direction == "up" else -raw_rate if direction == "down" else 0.0,
            "direction": direction,
        }

        avg_price = item.get("avg_price")
        market = item.get("market")
        index_code = MARKET_INDEX_CODE.get(market)
        alert_eligible = (
            item.get("type") == "stock"
            and item.get("account") in ALERT_ELIGIBLE_ACCOUNTS
            and avg_price is not None
            and index_code is not None
        )

        if alert_eligible:
            state_key = f"{item.get('account')}|{code}"
            try:
                if index_code not in index_cache:
                    index_cache[index_code] = fetch_index_level(token, index_code)
                    time.sleep(0.25)
                sell_alert, updated_state = evaluate_sell_alert(
                    index_cache[index_code], avg_price, price, alert_state.get(state_key, {})
                )
                entry["sellAlert"] = sell_alert
                alert_state[state_key] = updated_state
            except Exception as exc:  # noqa: BLE001 - never let alert logic break the price feed
                print(f"[warn] {item['name']} 매도 알림 계산 실패: {exc}", file=sys.stderr)
                entry["sellAlert"] = False
        elif item.get("type") == "stock":
            entry["sellAlert"] = False

        stocks.append(entry)

    payload = {
        "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "stocks": stocks,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ALERT_STATE_PATH.write_text(json.dumps(alert_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(stocks)} quotes to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
