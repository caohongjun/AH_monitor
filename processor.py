# -*- coding: utf-8 -*-
"""processor.py — 数据加工层：标的提取 / 负面匹配 / 事件组装 / 速览与关注计算"""
import hashlib
import re
from datetime import datetime
from typing import Dict, List, Optional

from config import (CN_TZ, NOW, TODAY, TODAY_START, MAX_EVENTS, OUTPUT_HTML,
                    QUOTE_BATCH, BROWSER_HEADERS, KEYWORD_GROUPS, TAG_COLORS,
                    SOURCE_STATS, HIGH_RISK_TAGS, MARKET_CORE_KEYWORDS,
                    MARKET_KEYWORDS, MARKET_DOMESTIC_KEYWORDS,
                    MARKET_GLOBAL_KEYWORDS, BRIEF_GROUP_MAX, HEADLINES_MAX,
                    WATCHLIST, WATCH_RELATED_MAX,
                    GLOBAL_TAG_RULES, AI_CORE, AI_COMPANIES, AI_PRODUCT_VERBS)
from utils import (log, norm_title, is_today, ts_to_dt, ms_to_dt,
                   strip_html, parse_dt, dedupe_and_sort, is_ai_item,
                   is_ai_product, zh_tags)
from spiders import fetch_quotes


# ============================ 关键词与股票代码提取 ============================

# 带交易所后缀：00700.HK / 600519.SH / 000001.sz
RE_HK_SUFFIX = re.compile(r"(?<!\d)(\d{1,6})[._]?[Hh][Kk](?!\d)")
RE_A_SUFFIX = re.compile(r"(?<!\d)(\d{6})[._]?([Ss][HhZz]|[Bb][Jj])(?!\d)")
# 新三板（NEEQ）显式后缀：833585.NQ
RE_NQ_SUFFIX = re.compile(r"(?<!\d)(\d{6})[._]?[Nn][Qq](?!\d)")
# 括号/书名号中的纯数字代码：（600519）、(00700)、【09988】
RE_BRACKET_CODE = re.compile(r"[（(\[【]\s*(\d{4,6})(?:\.(?:HK|hk|SH|sh|SZ|sz|BJ|bj))?\s*[）)\]】]")
# A 股裸 6 位代码（仅匹配沪深北交易所合法号段，降低普通数字误报）
RE_A_BARE = re.compile(
    r"(?<!\d)("
    r"60\d{4}|68\d{4}|"            # 沪市主板 / 科创板
    r"00\d{4}|30\d{4}|"            # 深市主板 / 创业板
    r"43\d{4}|83\d{4}|87\d{4}|92\d{4}"  # 北交所
    r")(?!\d)"
)
# 港股裸 5 位代码：仅接受 0 开头的补零形态（如 00700/09988/03690），
# 非 0 开头的 5 位数字与普通数字无法区分，按"不臆造"原则放弃
RE_HK_BARE = re.compile(r"(?<!\d)(0\d{4})(?!\d)")


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


def merge_stocks(hints: List[dict], text: str) -> List[dict]:
    """合并"数据源结构化标的"与"正文正则标的"，按展示代码去重（结构化优先）"""
    merged: Dict[str, dict] = {}
    for stock in hints:
        merged[stock["display"]] = stock
    for stock in extract_stocks(text):
        merged.setdefault(stock["display"], stock)
    return list(merged.values())


def extract_stocks(text: str) -> List[dict]:
    """从文本中提取股票代码，返回去重后的标的列表（只提取、不猜测）"""
    found: Dict[str, dict] = {}

    def add(stock: Optional[dict]) -> None:
        if stock:
            found[stock["display"]] = stock

    # 1) 带 .HK 显式后缀：1~5 位为港股；6 位 200/900 开头实为沪深 B 股（部分源误标 .HK）
    for m in RE_HK_SUFFIX.finditer(text):
        raw = m.group(1)
        if len(raw) <= 5:
            code = raw.zfill(5)
            add({"market": "港股", "code": code, "display": f"{code}.HK",
                 "tcode": f"hk{code}"})
        elif len(raw) == 6 and raw.startswith(("200", "900")):
            add(classify_a_share(raw, "SZ" if raw.startswith("200") else "SH"))

    # 2) 带 .SH/.SZ/.BJ 显式后缀
    for m in RE_A_SUFFIX.finditer(text):
        add(classify_a_share(m.group(1), m.group(2)))

    # 2.5) 带 .NQ 显式后缀：新三板挂牌公司（非北交所上市公司，不查行情）
    for m in RE_NQ_SUFFIX.finditer(text):
        code = m.group(1)
        if code[:2] in ("40", "43", "83", "87", "88", "92"):
            add({"market": "新三板", "code": code, "display": f"{code}.NQ",
                 "tcode": ""})

    # 3) 括号内代码：6 位按 A 股归类，4~5 位按港股补零
    for m in RE_BRACKET_CODE.finditer(text):
        raw = m.group(1)
        if len(raw) == 6:
            add(classify_a_share(raw))
        else:
            code = raw.zfill(5)
            add({"market": "港股", "code": code, "display": f"{code}.HK",
                 "tcode": f"hk{code}"})

    # 4) 裸代码（前面已提取过的会被 display 去重自然跳过）
    for m in RE_A_BARE.finditer(text):
        add(classify_a_share(m.group(1)))
    for m in RE_HK_BARE.finditer(text):
        code = m.group(1)
        add({"market": "港股", "code": code, "display": f"{code}.HK",
             "tcode": f"hk{code}"})

    return list(found.values())


def match_keywords(text: str) -> List[str]:
    """返回命中文本的关键词分组标签（按词库顺序去重）"""
    tags = []
    for group, words in KEYWORD_GROUPS.items():
        if any(word in text for word in words):
            tags.append(group)
    return tags

# ============================ 事件组装 ============================

def build_events(news_rows: List[dict]) -> List[dict]:
    """关键词过滤 → 标的提取 → 行情补充 → 运行内去重 → 排序截断"""
    # 第一步：关键词命中，并收集所有待查行情代码
    candidates: List[dict] = []
    tcodes: List[str] = []
    for row in news_rows:
        combined = f"{row['title']} {row['content']}".strip()
        tags = match_keywords(combined)
        if not tags:
            continue
        # 标的 = 数据源自带结构化代码（更准） + 正文正则提取（兜底）
        stocks = merge_stocks(row.get("hints") or [], combined)
        for stock in stocks:
            if stock["tcode"] and stock["tcode"] not in tcodes:
                tcodes.append(stock["tcode"])
        candidates.append({**row, "tags": tags, "stocks": stocks, "combined": combined})
        SOURCE_STATS.setdefault(row["source"], {"fetched": 0, "matched": 0})
        SOURCE_STATS[row["source"]]["matched"] += 1

    log(f"关键词命中快讯 {len(candidates)} 条，涉及 {len(tcodes)} 个标的，开始拉取行情……")
    quotes = fetch_quotes(tcodes)

    # 第二步：一条快讯 × 一个标的 = 一个事件；无标的快讯生成"未识别"事件
    events: List[dict] = []
    seen_keys = set()
    for row in candidates:
        stocks = row["stocks"] or [None]
        title_key = norm_title(row["title"] or row["content"][:40])
        for raw_stock in stocks:
            stock = raw_stock
            quote = quotes.get(stock["tcode"]) if stock else None
            # 43/83/87 号段与新三板共用：腾讯查无行情即视为新三板挂牌公司，
            # 改标 .NQ 且不计入 A 股标的统计（92 号段为北交所专属，保持 A 股）
            if stock and stock["market"] == "A股" and quote is None \
                    and stock["display"].endswith(".BJ") \
                    and stock["code"][:2] in ("43", "83", "87"):
                stock = {**stock, "market": "新三板",
                         "display": f"{stock['code']}.NQ", "tcode": ""}
            stock_key = stock["display"] if stock else "NONE"
            dedupe_md5 = hashlib.md5(f"{title_key}|{stock_key}".encode("utf-8")).hexdigest()
            if dedupe_md5 in seen_keys:
                continue
            seen_keys.add(dedupe_md5)
            events.append({
                "eid": dedupe_md5[:8],  # 卡片锚点 ID，速览区点击可跳转到详情
                "time": row["time"],
                "source": row["source"],
                "tags": row["tags"],
                "title": row["title"],
                "content": row["content"],
                "url": row["url"],
                "stock": stock,
                "quote": quote,
            })

    # 第三步：时间倒序（无标的/时间并列时保持稳定顺序），截断到 MAX_EVENTS
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:MAX_EVENTS]


def headline_score(row: dict) -> int:
    """要闻重要性评分：核心词每命中一个记 2 分，一般词记 1 分"""
    text = f"{row['title']} {row['content']}"
    score = sum(2 for kw in MARKET_CORE_KEYWORDS if kw in text)
    score += sum(1 for kw in MARKET_KEYWORDS if kw in text)
    return score

def build_market_headlines(news_rows: List[dict], events: List[dict]) -> dict:
    """筛选今日要点并按国内/海外分组：命中国内强特征词归国内组，
    其余（海外实体词命中或无地域特征的国际编译稿）一律归海外组，
    排除已进入负面事件列表的快讯，并对同题跨源快讯做精确去重"""
    event_keys = {norm_title(e["title"] or e["content"][:40]) for e in events}
    seen = set()
    domestic, global_ = [], []
    for row in news_rows:
        key = norm_title(row["title"] or row["content"][:40])
        if key in event_keys or key in seen:
            continue
        score = headline_score(row)
        if score <= 0:  # 未命中任何要闻词库的普通快讯不入选
            continue
        seen.add(key)
        text = f"{row['title']} {row['content']}"
        item = {"score": score, **row}
        # 海外实体词优先命中 → 海外组；再判国内强特征词 → 国内组；无地域特征 → 默认海外组
        if any(kw in text for kw in MARKET_GLOBAL_KEYWORDS):
            global_.append(item)
        elif any(kw in text for kw in MARKET_DOMESTIC_KEYWORDS):
            domestic.append(item)
        else:
            global_.append(item)
    # 各组内：评分高的在前；同分按时间倒序（最新优先）
    for group in (domestic, global_):
        group.sort(key=lambda t: (t["score"], t["time"]), reverse=True)
    return {"domestic": domestic[:BRIEF_GROUP_MAX],
            "global": global_[:BRIEF_GROUP_MAX]}


def event_score(event: dict) -> float:
    """重点事件评分（纯规则，无 AI）：
    命中标签数×2 + 跌幅绝对值(封顶10) + 高危标签加成3 + 有标的加成1"""
    score = len(event["tags"]) * 2.0
    pct = event["quote"]["pct"] if event["quote"] else None
    if pct is not None:
        score += min(abs(pct), 10.0)
    score += len(HIGH_RISK_TAGS & set(event["tags"])) * 3.0
    if event["stock"]:
        score += 1.0
    return score


def summarize(events: List[dict]) -> dict:
    """从全部事件中计算速览数据：跌幅榜 Top5 / 标签分布 / 重点事件 Top5"""
    # 跌幅榜：只统计有行情数据的事件，按涨跌幅升序（跌得最多在前），同标的去重
    seen_stock = set()
    losers = []
    for e in events:  # events 已按时间倒序，天然保证同标的取最新事件
        if not e["quote"] or e["quote"]["pct"] is None or not e["stock"]:
            continue
        if e["stock"]["display"] in seen_stock:
            continue
        seen_stock.add(e["stock"]["display"])
        losers.append(e)
    losers.sort(key=lambda e: e["quote"]["pct"])
    losers = losers[:5]

    # 标签分布：各标签命中事件数，降序
    tag_count: Dict[str, int] = {}
    for e in events:
        for tag in e["tags"]:
            tag_count[tag] = tag_count.get(tag, 0) + 1
    tag_rank = sorted(tag_count.items(), key=lambda kv: kv[1], reverse=True)

    # 重点事件：按评分取前 5
    top_events = sorted(events, key=event_score, reverse=True)[:5]

    return {"losers": losers, "tag_rank": tag_rank, "top_events": top_events}

def build_watchlist(events: List[dict], news_rows: List[dict]) -> List[dict]:
    """组装关注板块数据：批量拉取行情 + 负面事件优先、普通快讯回退的信息列表"""
    quotes = fetch_quotes([w["tcode"] for w in WATCHLIST])
    watchlist = []
    for w in WATCHLIST:
        # 第一优先：命中负面词库且标的代码精确匹配的事件，按评分排序
        related = sorted(
            (e for e in events
             if e["stock"] and e["stock"]["code"] == w["match"]),
            key=event_score, reverse=True,
        )[:WATCH_RELATED_MAX]

        # 第二优先：文本提及该标的（代码或别名）的普通快讯，时间倒序去重取最新
        # 用于覆盖"该标的无负面新闻"时的日常动态展示
        related_keys = {norm_title(e["title"] or e["content"][:40]) for e in related}
        seen = set(related_keys)
        latest_news = []
        for row in sorted(news_rows, key=lambda r: r["time"], reverse=True):
            text = f"{row['title']} {row['content']}".lower()
            if not any(a.lower() in text for a in w["aliases"]) and w["match"] not in text:
                continue
            key = norm_title(row["title"] or row["content"][:40])
            if key in seen:
                continue
            seen.add(key)
            latest_news.append(row)
            if len(related) + len(latest_news) >= WATCH_RELATED_MAX + 1:
                break
        watchlist.append({**w, "quote": quotes.get(w["tcode"]),
                          "related": related, "latest_news": latest_news})
    return watchlist

