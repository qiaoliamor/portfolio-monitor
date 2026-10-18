"""Curated identifiers plus provider-confirmed search. No arbitrary URL fetching."""
import csv
import hashlib
import io
import math
import os
import time
from datetime import date, datetime, timedelta, timezone

import httpx


def product(id, name, kind, source, symbol, currency, unit, metric="price", overview=False):
    return dict(id=id, name=name, kind=kind, source=source, symbol=symbol,
                currency=currency, unit=unit, metric=metric, overview=overview)


CATALOG = [
    product("us:SPY", "标普 500 ETF", "美国 ETF", "twelve", "SPY", "USD", "美元/份", overview=True),
    product("us:QQQ", "纳斯达克 100 ETF", "美国 ETF", "twelve", "QQQ", "USD", "美元/份", overview=True),
    product("us:AAPL", "苹果 Apple", "美国股票", "twelve", "AAPL", "USD", "美元/股"),
    product("cn:000001.SH", "上证指数", "中国指数", "tushare", "000001.SH", "CNY", "点", overview=True),
    product("cn:510300.SH", "沪深 300 ETF", "中国 ETF", "tushare", "510300.SH", "CNY", "元/份"),
    product("fund:000001.OF", "华夏成长混合", "公募基金", "tushare", "000001.OF", "CNY", "元/份", "nav"),
    product("commodity:XAU/USD", "现货黄金", "贵金属现货", "twelve", "XAU/USD", "USD", "美元/盎司", overview=True),
    product("commodity:WTI/USD", "WTI 原油参考报价", "大宗商品", "twelve", "WTI/USD", "USD", "美元/桶", overview=True),
    *[product(f"fred:DGS{term}", f"美国 {term} 年期国债收益率", "国债收益率", "fred", f"DGS{term}", "", "%", "yield", True)
      for term in (2, 10, 30)],
]


class ProviderError(RuntimeError):
    pass


class Market:
    def __init__(self, demo=False, client=None):
        self.demo = demo
        self.client = client or httpx.Client(timeout=25, follow_redirects=True)

    def close(self):
        self.client.close()

    def _request(self, method, url, **kwargs):
        for attempt in range(3):
            try:
                response = self.client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(0.5 * (2**attempt))
                        continue
                response.raise_for_status()
                return response
            except httpx.HTTPError:
                if attempt == 2:
                    raise ProviderError("行情服务连接失败或额度受限，请稍后重试") from None
                time.sleep(0.5 * (2**attempt))
        raise ProviderError("行情服务暂不可用")

    def _tushare(self, api, params, fields=""):
        token = os.getenv("TUSHARE_TOKEN")
        if not token:
            raise ProviderError("未配置 TUSHARE_TOKEN；中国行情需验证账号接口权限")
        data = self._request("POST", "https://api.tushare.pro", json={
            "api_name": api, "token": token, "params": params, "fields": fields}).json()
        if data.get("code") != 0 or not data.get("data"):
            raise ProviderError("Tushare 拒绝请求：请检查接口权限、代码和额度")
        body = data["data"]
        return [dict(zip(body["fields"], row, strict=True)) for row in body["items"]]

    def search(self, query):
        query = query.strip()[:80]
        if not query:
            return []
        matches = [p for p in CATALOG if query.lower() in
                   (p["id"] + p["name"] + p["symbol"]).lower()]
        if matches or self.demo:
            return matches
        if query.isdigit() and len(query) == 6:
            # Confirm fund identity/share class against provider metadata.
            rows = self._tushare("fund_basic", {"market": "O"}, "ts_code,name")
            return [product("fund:" + r["ts_code"], r["name"], "公募基金", "tushare",
                            r["ts_code"], "CNY", "元/份", "nav")
                    for r in rows if r["ts_code"].split(".")[0] == query][:20]
        if query.upper().endswith((".SH", ".SZ")):
            rows = self._tushare("stock_basic", {"ts_code": query.upper()}, "ts_code,name")
            if rows:
                return [product("cn:" + r["ts_code"], r["name"], "中国股票", "tushare",
                                r["ts_code"], "CNY", "元/股") for r in rows]
            rows = self._tushare("fund_basic", {"ts_code": query.upper(), "market": "E"},
                                 "ts_code,name")
            return [product("cn:" + r["ts_code"], r["name"], "中国 ETF", "tushare",
                            r["ts_code"], "CNY", "元/份") for r in rows
                    if r["ts_code"] == query.upper()]
        key = os.getenv("TWELVE_DATA_API_KEY")
        if not key:
            raise ProviderError("目录没有匹配；配置 TWELVE_DATA_API_KEY 可搜索更多美国股票和 ETF")
        data = self._request("GET", "https://api.twelvedata.com/symbol_search",
                             params={"symbol": query, "apikey": key}).json()
        results = []
        for r in data.get("data", []):
            if r.get("country") != "United States" or r.get("instrument_type") not in (
                    "Common Stock", "ETF"):
                continue
            symbol, exchange = r["symbol"], r["exchange"]
            p = product(f"us:{exchange}:{symbol}", r["instrument_name"],
                        "美国 ETF" if r["instrument_type"] == "ETF" else "美国股票",
                        "twelve", symbol, r.get("currency", "USD"), "美元/单位")
            p["exchange"] = exchange
            results.append(p)
        return results[:20]

    def fetch(self, p, end=None):
        end = end or date.today()
        start = end - timedelta(days=366)
        now = datetime.now(timezone.utc).isoformat()
        if self.demo:
            seed = int(hashlib.sha256(p["id"].encode()).hexdigest()[:6], 16)
            base = 4 if p["metric"] == "yield" else (seed % 300 + 20)
            pairs = [(start + timedelta(days=i), base + math.sin(i / 9 + seed) * base * .03 + i * base * .0001)
                     for i in range(367) if (start + timedelta(days=i)).weekday() < 5]
        elif p["source"] == "fred":
            if os.getenv("FRED_API_KEY"):
                data = self._request("GET", "https://api.stlouisfed.org/fred/series/observations",
                                     params={"series_id": p["symbol"], "api_key": os.getenv("FRED_API_KEY"),
                                             "file_type": "json", "observation_start": str(start),
                                             "observation_end": str(end)}).json()
                pairs = [(date.fromisoformat(r["date"]), float(r["value"]))
                         for r in data.get("observations", []) if r["value"] != "."]
            else:
                response = self._request("GET", "https://fred.stlouisfed.org/graph/graph.csv",
                                         params={"id": p["symbol"], "cosd": str(start), "coed": str(end)})
                rows = csv.DictReader(io.StringIO(response.text))
                pairs = []
                for r in rows:
                    raw = r.get(p["symbol"], ".")
                    if raw and raw != ".":
                        pairs.append((date.fromisoformat(r.get("observation_date") or r["DATE"]), float(raw)))
        elif p["source"] == "twelve":
            key = os.getenv("TWELVE_DATA_API_KEY")
            if not key:
                raise ProviderError("未配置 TWELVE_DATA_API_KEY；该标的免费覆盖尚未验证")
            params = {"symbol": p["symbol"], "interval": "1day", "start_date": str(start),
                      "end_date": str(end), "outputsize": 400, "apikey": key}
            if p.get("exchange"):
                params["exchange"] = p["exchange"]
            data = self._request("GET", "https://api.twelvedata.com/time_series", params=params).json()
            if data.get("status") == "error" or not data.get("values"):
                raise ProviderError("Twelve Data 未返回行情：代码、免费权限或额度需检查")
            pairs = [(date.fromisoformat(r["datetime"][:10]), float(r["close"])) for r in data["values"]]
        else:
            api = ("fund_nav" if p["metric"] == "nav" else "index_daily" if p["kind"] == "中国指数"
                   else "fund_daily" if p["kind"] == "中国 ETF" else "daily")
            rows = self._tushare(api, {"ts_code": p["symbol"],
                                     "start_date": start.strftime("%Y%m%d"),
                                     "end_date": end.strftime("%Y%m%d")})
            pairs = [(datetime.strptime(r["end_date" if api == "fund_nav" else "trade_date"], "%Y%m%d").date(),
                      float(r["unit_nav" if api == "fund_nav" else "close"])) for r in rows
                     if r.get("unit_nav" if api == "fund_nav" else "close") is not None]
        quotes = [{"product_id": p["id"], "metric": p["metric"], "day": d.isoformat(),
                   "source": "demo" if self.demo else p["source"], "value": v, "fetched_at": now}
                  for d, v in sorted(pairs) if start <= d <= end and math.isfinite(v)]
        if not quotes:
            raise ProviderError("数据源未返回有效日数据")
        return quotes


def provider_status():
    return [
        {"name": "FRED 国债收益率", "ready": True, "detail": "官方公开 CSV；需实际联网采集验证"},
        {"name": "Twelve Data 海外行情", "ready": bool(os.getenv("TWELVE_DATA_API_KEY")),
         "detail": "股票 / ETF / 黄金 / 商品，覆盖由账号权限决定"},
        {"name": "Tushare 中国行情", "ready": bool(os.getenv("TUSHARE_TOKEN")),
         "detail": "股票 / ETF / 指数 / 基金净值，需相应接口权限"},
    ]
