# -*- coding: utf-8 -*-
"""utils.py — 基础工具层：会话 / 日志 / 时间转换 / 文本处理 / 通用筛选"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from config import (CN_TZ, NOW, TODAY, TODAY_START, MAX_EVENTS, OUTPUT_HTML,
                    QUOTE_BATCH, BROWSER_HEADERS, KEYWORD_GROUPS, TAG_COLORS,
                    GLOBAL_TAG_RULES, AI_CORE, AI_COMPANIES, AI_PRODUCT_VERBS)


SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)


def log(msg: str) -> None:
    """打印带时间戳的结构化日志，便于在 GitHub Actions 中排查问题"""
    print(f"[{datetime.now(CN_TZ):%H:%M:%S}] {msg}", flush=True)


# ============================ 通用工具函数 ============================

def http_get(url: str, referer: str = "", timeout: int = 12,
             encoding: Optional[str] = None) -> requests.Response:
    """发起带浏览器头的 GET 请求；referer 单独注入以应对简单防盗链"""
    headers = {}
    if referer:
        headers["Referer"] = referer
    resp = SESSION.get(url, headers=headers, timeout=timeout, verify=False)
    if encoding:
        resp.encoding = encoding
    resp.raise_for_status()
    return resp


def strip_html(raw) -> str:
    """HTML/富文本转纯文本，并折叠多余空白；非字符串与无标签文本直接折叠"""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    if "<" in raw and ">" in raw:  # 仅在确有标签时才走 BS4，避免短文本触发告警
        raw = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", raw).strip()


def parse_dt(value: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
    """把字符串时间解析为北京时间 datetime；解析失败返回 None（不做 now 兜底）"""
    try:
        return datetime.strptime(value, fmt).replace(tzinfo=CN_TZ)
    except (ValueError, TypeError):
        return None


def ts_to_dt(seconds: (int | float | str)) -> Optional[datetime]:
    """秒级 Unix 时间戳转北京时间"""
    try:
        return datetime.fromtimestamp(int(seconds), CN_TZ)
    except (ValueError, TypeError, OSError):
        return None


def ms_to_dt(milliseconds: (int | float | str)) -> Optional[datetime]:
    """毫秒级 Unix 时间戳转北京时间"""
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000, CN_TZ)
    except (ValueError, TypeError, OSError):
        return None


def is_today(dt: Optional[datetime]) -> bool:
    """判断时间是否属于北京时间今天（None 一律视为不属于，直接丢弃）"""
    return dt is not None and dt >= TODAY_START


def norm_title(title: str) -> str:
    """标题归一化：去空白与标点，用于跨源同稿去重"""
    return re.sub(r"[\s\u3000\W_]+", "", title or "").lower()
def zh_tags(text: str) -> List[str]:
    """按规则给条目打中文标签（最多 2 个），辅助快速理解英文内容"""
    low = f" {text.lower()} "
    tags = [tag for tag, kws in GLOBAL_TAG_RULES if any(kw in low for kw in kws)]
    return tags[:2]


def _kw_hit(low: str, kw: str) -> bool:
    """关键词命中判定：英文短词（<=3字符，如 ai）用词边界匹配，避免误伤 airbnb/detail 等；其余做包含匹配"""
    if kw.isascii() and len(kw) <= 3 and kw.isalpha():
        return re.search(rf"\b{re.escape(kw)}\b", low) is not None
    return kw in low


def is_ai_item(text: str) -> bool:
    """判断是否 AI 相关资讯：泛 AI 词或 AI 公司名命中（大小写不敏感）"""
    low = text.lower()
    return any(_kw_hit(low, kw) for kw in AI_CORE) or any(_kw_hit(low, kw) for kw in AI_COMPANIES)


def is_ai_product(text: str) -> bool:
    """判断是否 AI 产品发布动态：AI 公司/产品名 + 发布动词双命中"""
    low = text.lower()
    has_company = any(_kw_hit(low, kw) for kw in AI_COMPANIES)
    return has_company and any(_kw_hit(low, kw) for kw in AI_PRODUCT_VERBS)


def dedupe_and_sort(items: List[dict], limit: int) -> List[dict]:
    """按归一化标题精确去重并按时间倒序取前 N 条"""
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["time"] or datetime.min.replace(tzinfo=CN_TZ), reverse=True):
        key = norm_title(it["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= limit:
            break
    return out


# ============================ 股票代码结构化解析 ============================

def classify_a_share(code: str, suffix_hint: str = "") -> Optional[dict]:
    """把 6 位 A 股代码归类到 sh/sz/bj 并返回标准化结构；非股票号段返回 None"""
    suffix = suffix_hint.upper()
    if suffix in ("SH", "SZ", "BJ"):
        market = suffix
    elif code.startswith(("60", "68", "90")):
        market = "SH"
    elif code.startswith(("00", "30", "20")):
        market = "SZ"
    elif code.startswith(("43", "83", "87", "92")):
        market = "BJ"
    else:
        return None
    return {
        "market": "A股",
        "code": code,
        "display": f"{code}.{market}",
        "tcode": f"{market.lower()}{code}",
    }


def parse_sina_stocks(ext_raw: Optional[str]) -> List[dict]:
    """解析新浪 7x24 的 ext.stocks 字段（JSON 字符串），只取 A 股与港股个股"""
    if not ext_raw:
        return []
    try:
        ext = json.loads(ext_raw)
    except (ValueError, TypeError):
        return []
    stocks: List[dict] = []
    for item in ext.get("stocks", []):
        market, symbol = item.get("market", ""), item.get("symbol", "")
        if market == "hk" and re.fullmatch(r"\d{1,5}", symbol):
            code = symbol.zfill(5)
            stocks.append({"market": "港股", "code": code,
                           "display": f"{code}.HK", "tcode": f"hk{code}"})
        elif market == "cn":
            # symbol 形如 sh688836 / sz300024；si/sih 开头的是概念指数，忽略
            m = re.fullmatch(r"(sh|sz|bj)(\d{6})", symbol)
            if m:
                mapped = classify_a_share(m.group(2), m.group(1).upper())
                if mapped:
                    stocks.append(mapped)
    return stocks


def parse_em_stock_list(stock_list: Optional[List[str]]) -> List[dict]:
    """解析东财快讯 stockList（"市场号.代码"）；实测 1=沪 0=深/北 116=港股，其余为美股/板块/基金"""
    stocks: List[dict] = []
    for entry in stock_list or []:
        if "." not in entry:
            continue
        market_id, code = entry.split(".", 1)
        if market_id == "116" and re.fullmatch(r"\d{1,5}", code):
            code = code.zfill(5)
            stocks.append({"market": "港股", "code": code,
                           "display": f"{code}.HK", "tcode": f"hk{code}"})
        elif market_id == "1" and re.fullmatch(r"\d{6}", code):
            mapped = classify_a_share(code, "SH")
            if mapped:
                stocks.append(mapped)
        elif market_id == "0" and re.fullmatch(r"\d{6}", code):
            mapped = classify_a_share(code)  # 60/00/30 归沪深，43/83/87/92 归北交所
            if mapped:
                stocks.append(mapped)
        # 105/106/153 美股、90/1007 板块、150 港股ETF、999 指数等均不在 A/港股个股范围
    return stocks
