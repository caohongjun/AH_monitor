#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/港股"伪负面"舆情监控看板 —— 核心构建脚本

流程：
    1. 并发抓取 7 个财经快讯源（新浪 7x24 / 东方财富 7x24 / 华尔街见闻 /
       金十数据 / 新浪滚动 / 证券时报 / 每经）
    2. 正则提取 A 股 6 位代码与港股 5 位代码（只提取、不臆造）
    3. 按负面/突发关键词词库命中过滤
    4. 调用腾讯财经免费行情接口批量补充现价与涨跌幅
    5. 渲染单页响应式 index.html（Tailwind CSS CDN，暗色财经终端风格）

设计约束：
    - 无数据库、无历史文件：每次运行独立，只保留"当天"数据，运行内去重
    - 任一数据源失败不影响整体（逐个 try/except 降级）
    - 所有时间统一为北京时间（UTC+8），不依赖运行机器时区
"""

import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# 抑制 InsecureRequestWarning（部分站点证书链不全，仅做只读公开数据抓取）
requests.packages.urllib3.disable_warnings()

# ============================ 全局配置 ============================

CN_TZ = timezone(timedelta(hours=8))          # 北京时间
NOW = datetime.now(CN_TZ)                     # 脚本运行时刻（北京时间）
TODAY = NOW.date()                            # 当天日期，用于"仅保留今日快讯"
TODAY_START = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=CN_TZ)

MAX_EVENTS = 100                              # 看板最多展示条数
OUTPUT_HTML = "index.html"                    # 输出文件
QUOTE_BATCH = 60                              # 腾讯行情单次批量查询数量

# 统一浏览器请求头，降低被简单反爬拦截的概率
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 负面/突发关键词词库：分组名即看板上展示的标签
# 注意：命中逻辑为"标题+正文"子串包含，词库刻意保持宽口径，宁可多报
KEYWORD_GROUPS: Dict[str, List[str]] = {
    "监管调查": [
        "被查", "调查", "立案", "处罚", "罚单", "警示函", "监管函", "关注函",
        "问询函", "约谈", "整改", "通报批评", "公开谴责", "留置", "被捕",
        "逮捕", "刑拘", "刑事拘留", "公诉", "判刑", "被执行", "失信",
        "退市风险", "终止上市", "暂停上市", "内控被否",
    ],
    "诉讼纠纷": [
        "诉讼", "仲裁", "纠纷", "索赔", "起诉", "被告", "侵权", "违约",
        "合同纠纷", "股权纠纷", "财产保全", "冻结", "查封", "专利侵权",
    ],
    "传闻辟谣": [
        "传闻", "辟谣", "澄清", "否认", "网传", "谣言", "不实", "紧急回应",
        "回应称", "假的", "市场传言",
    ],
    "举报公关": [
        "举报", "实名举报", "绯闻", "出轨", "性骚扰", "性侵", "情妇",
        "争议", "抵制", "翻车", "道歉", "虚假宣传", "欺诈", "造假",
        "财务造假", "抄袭", "剽窃", "维权",
    ],
    "经营风险": [
        "宕机", "爆雷", "暴雷", "裁员", "破产", "停产", "跑路", "资金链",
        "债务违约", "债券违约", "商誉减值", "巨亏", "预亏", "业绩变脸",
        "业绩下修", "召回", "安全事故", "火灾", "爆炸", "停产整顿",
        "环保处罚", "欠薪",
    ],
    "高管异动": [
        "辞职", "辞去", "离职", "坠楼", "身亡", "去世", "猝死", "失联",
        "被抓", "被带走", "配合调查", "董事长出事",
    ],
    "股价异动": [
        "暴跌", "闪崩", "跌停", "大跌", "跳水", "跌超", "熔断",
    ],
}

# 标签对应的 Tailwind 配色（与 HTML 模板中保持一致）
TAG_COLORS: Dict[str, str] = {
    "监管调查": "bg-red-500/15 text-red-300 border-red-500/30",
    "诉讼纠纷": "bg-orange-500/15 text-orange-300 border-orange-500/30",
    "传闻辟谣": "bg-yellow-500/15 text-yellow-300 border-yellow-500/30",
    "举报公关": "bg-pink-500/15 text-pink-300 border-pink-500/30",
    "经营风险": "bg-rose-500/15 text-rose-300 border-rose-500/30",
    "高管异动": "bg-purple-500/15 text-purple-300 border-purple-500/30",
    "股价异动": "bg-amber-500/15 text-amber-300 border-amber-500/30",
}

# 全局长连接会话
SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)

# 运行统计：每个源抓到几条 / 命中几条，最终展示在页脚
SOURCE_STATS: Dict[str, Dict[str, int]] = {}


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


# ============================ 数据源 1：新浪财经 7x24 ============================

def fetch_sina7x24() -> List[dict]:
    """抓取新浪财经 7x24 全球直播，翻页直到遇到昨天及更早内容（最多 5 页）"""
    items: List[dict] = []
    for page in range(1, 6):
        url = (
            "https://zhibo.sina.com.cn/api/zhibo/feed"
            f"?page={page}&page_size=50&zhibo_id=152&tag_id=0&type=0"
        )
        data = http_get(url, referer="https://finance.sina.com.cn/").json()
        rows = data["result"]["data"]["feed"]["list"]
        if not rows:
            break
        page_has_today = False
        for row in rows:
            dt = parse_dt(row.get("create_time", ""))
            if not is_today(dt):
                continue
            page_has_today = True
            items.append({
                "time": dt,
                "title": "",  # 7x24 快讯无独立标题
                "content": row.get("rich_text", ""),
                "url": row.get("docurl") or "https://finance.sina.com.cn/7x24/",
                # 新浪在 ext.stocks 里直接给出关联标的（hk/cn 市场），比正则准
                "hints": parse_sina_stocks(row.get("ext")),
            })
        if not page_has_today:
            break
        time.sleep(0.3)
    return items


# ============================ 数据源 2：东方财富 7x24 ============================

def _em_get_page(sort_end: str) -> Optional[dict]:
    """请求东财快讯单页；该接口偶发返回 data=null，空响应时重试最多 3 次"""
    for attempt in range(3):
        url = (
            "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
            f"?client=web&biz=web_724&fastColumn=102&sortEnd={sort_end}&pageSize=50"
            f"&req_trace={int(time.time() * 1000)}"
        )
        data = http_get(url, referer="https://kuaixun.eastmoney.com/").json()
        if (data.get("data") or {}).get("fastNewsList"):
            return data["data"]
        time.sleep(1.0)
    return None


def fetch_eastmoney() -> List[dict]:
    """抓取东方财富 7x24 快讯，使用 sortEnd 游标向后翻页（最多 5 页）"""
    items: List[dict] = []
    sort_end = ""
    for _ in range(5):
        page_data = _em_get_page(sort_end)
        if not page_data:
            break
        rows = page_data.get("fastNewsList") or []
        page_has_today = False
        for row in rows:
            dt = parse_dt(row.get("showTime", ""))
            if not is_today(dt):
                continue
            page_has_today = True
            news_code = row.get("code", "")
            items.append({
                "time": dt,
                "title": strip_html(row.get("title", "")),
                "content": strip_html(row.get("summary", "")),
                "url": f"https://finance.eastmoney.com/a/{news_code}.html" if news_code
                       else "https://kuaixun.eastmoney.com/",
                # stockList 形如 ["1.688836", "116.00700", "90.BK0464"]，仅取个股
                "hints": parse_em_stock_list(row.get("stockList")),
            })
        if not page_has_today:
            break
        sort_end = page_data.get("sortEnd", "")
        if not sort_end:
            break
        time.sleep(0.3)
    return items


# ============================ 数据源 3：华尔街见闻 7x24 ============================

def fetch_wallstreetcn() -> List[dict]:
    """抓取华尔街见闻全球直播频道（一次取 100 条，按今日过滤）"""
    url = "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=100"
    data = http_get(url, referer="https://wallstreetcn.com/").json()
    items: List[dict] = []
    for row in data.get("data", {}).get("items", []):
        dt = ts_to_dt(row.get("display_time"))
        if not is_today(dt):
            continue
        content = strip_html(row.get("content_text") or row.get("content") or "")
        items.append({
            "time": dt,
            "title": strip_html(row.get("title") or ""),
            "content": content,
            "url": row.get("uri") or "https://wallstreetcn.com/live",
        })
    return items


# ============================ 数据源 4：金十数据快讯 ============================

def fetch_jin10() -> List[dict]:
    """抓取金十数据 flash_newest.js（var newest = [...] 的 JS 变量）"""
    resp = http_get("https://www.jin10.com/flash_newest.js",
                    referer="https://www.jin10.com/")
    match = re.search(r"var\s+newest\s*=\s*(\[.*\])\s*;?\s*$", resp.text.strip(), re.S)
    if not match:
        return []
    rows = json.loads(match.group(1))
    items: List[dict] = []
    for row in rows:
        dt = parse_dt(row.get("time", ""))
        if not is_today(dt):
            continue
        payload = row.get("data") or {}
        content = strip_html(payload.get("content") or "")
        items.append({
            "time": dt,
            "title": strip_html(payload.get("title") or ""),
            "content": content,
            "url": f"https://flash.jin10.com/detail/{row['id']}",
        })
    return items


# ============================ 数据源 5：新浪滚动财经新闻 ============================

def fetch_sina_roll() -> List[dict]:
    """抓取新浪财经滚动新闻（lid=2516 财经频道），翻页直到非今日（最多 3 页）"""
    items: List[dict] = []
    for page in range(1, 4):
        url = (
            "https://feed.mix.sina.com.cn/api/roll/get"
            f"?pageid=153&lid=2516&k=&num=50&page={page}"
        )
        data = http_get(url, referer="https://finance.sina.com.cn/").json()
        rows = data["result"]["data"]
        if not rows:
            break
        page_has_today = False
        for row in rows:
            dt = ts_to_dt(row.get("ctime"))
            if not is_today(dt):
                continue
            page_has_today = True
            items.append({
                "time": dt,
                "title": strip_html(row.get("title", "")),
                "content": strip_html(row.get("intro") or ""),
                "url": row.get("url") or "https://finance.sina.com.cn/",
            })
        if not page_has_today:
            break
        time.sleep(0.3)
    return items


# ============================ 数据源 6：证券时报快讯 ============================

def fetch_stcn() -> List[dict]:
    """抓取证券时报快讯接口（/article/list.html?type=kx），翻页直到非今日"""
    items: List[dict] = []
    for page in range(1, 6):
        url = f"https://www.stcn.com/article/list.html?type=kx&page={page}"
        # 该接口要求 X-Requested-With 才返回 JSON，否则回退为 HTML 页面
        resp = SESSION.get(
            url,
            headers={
                "Referer": "https://www.stcn.com/article/list/kx.html",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=12,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data") or []
        if not rows:
            break
        page_has_today = False
        for row in rows:
            dt = ms_to_dt(row.get("time"))
            if not is_today(dt):
                continue
            page_has_today = True
            detail = row.get("web_url") or row.get("url") or ""
            items.append({
                "time": dt,
                "title": strip_html(row.get("title", "")),
                "content": strip_html(row.get("content", "")),
                "url": "https://www.stcn.com" + detail if detail.startswith("/")
                       else (detail or "https://www.stcn.com/article/list/kx.html"),
            })
        if not page_has_today:
            break
        time.sleep(0.3)
    return items


# ============================ 数据源 7：每日经济新闻 ============================

def _fetch_nbd_column(column_id: str) -> List[dict]:
    """抓取每经单个栏目列表页（HTML），只保留链接日期为今天的文章"""
    resp = http_get(f"https://www.nbd.com.cn/columns/{column_id}",
                    referer="https://www.nbd.com.cn/", encoding="utf-8")
    soup = BeautifulSoup(resp.text, "html.parser")
    items: List[dict] = []
    today_str = TODAY.strftime("%Y-%m-%d")
    for a_tag in soup.find_all("a", href=True, title=True):
        href = a_tag["href"]
        title = a_tag["title"].strip()
        if "/articles/" not in href or not title:
            continue
        # 文章 URL 形如 /articles/2026-09-14/4580000.html
        if f"/articles/{today_str}/" not in href:
            continue
        # 时间取同一 <li> 内文本中的 "YYYY-MM-DD HH:MM:SS"
        parent_li = a_tag.find_parent("li")
        dt = None
        if parent_li:
            tm = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
                           parent_li.get_text(" ", strip=True))
            if tm:
                dt = parse_dt(tm.group(1))
        items.append({
            "time": dt or TODAY_START.replace(hour=18),  # 列表无精确时间时归入当日 18:00
            "title": title,
            "content": title,
            "url": href if href.startswith("http") else "https://www.nbd.com.cn" + href,
        })
    return items


def fetch_nbd() -> List[dict]:
    """抓取每经"宏观"(310) 与"热点公司"(346) 两个栏目并按 URL 合并去重"""
    merged: Dict[str, dict] = {}
    for column_id in ("310", "346"):
        try:
            for item in _fetch_nbd_column(column_id):
                merged.setdefault(item["url"], item)
        except Exception as exc:  # noqa: BLE001 - 单栏目失败不影响另一栏目
            log(f"[源] 每经栏目 {column_id} 失败：{type(exc).__name__}: {exc}")
    return list(merged.values())


# 数据源注册表：顺序即优先级（同一条快讯多源转载时，保留排在前面的源）
SOURCE_REGISTRY: List[tuple] = [
    ("东方财富", fetch_eastmoney),
    ("证券时报", fetch_stcn),
    ("新浪7x24", fetch_sina7x24),
    ("华尔街见闻", fetch_wallstreetcn),
    ("金十数据", fetch_jin10),
    ("新浪滚动", fetch_sina_roll),
    ("每经", fetch_nbd),
]


def collect_all_news() -> List[dict]:
    """依次执行全部数据源抓取；单源异常只记录、不中断整体"""
    all_news: List[dict] = []
    for source_name, fetcher in SOURCE_REGISTRY:
        try:
            rows = fetcher()
            for row in rows:
                row["source"] = source_name
            SOURCE_STATS[source_name] = {"fetched": len(rows), "matched": 0}
            all_news.extend(rows)
            log(f"[源] {source_name:<6} 今日抓取 {len(rows)} 条")
        except Exception as exc:  # noqa: BLE001 - 公开接口任何异常都要降级
            SOURCE_STATS[source_name] = {"fetched": 0, "matched": 0}
            log(f"[源] {source_name:<6} 抓取失败：{type(exc).__name__}: {exc}")
    log(f"合计今日候选快讯 {len(all_news)} 条")
    return all_news


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


# ============================ 腾讯行情补充 ============================

def fetch_quotes(tcodes: List[str]) -> Dict[str, dict]:
    """批量查询腾讯行情，返回 {tcode: {name, price, pct}}；失败返回空字典"""
    result: Dict[str, dict] = {}
    for i in range(0, len(tcodes), QUOTE_BATCH):
        batch = tcodes[i:i + QUOTE_BATCH]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = SESSION.get(url, headers={"Referer": "https://gu.qq.com/"},
                               timeout=12, verify=False)
            resp.encoding = "gbk"  # 腾讯行情接口固定 GBK 编码
            for m in re.finditer(r'v_([a-z0-9]+)="([^"]*)"', resp.text):
                tcode, body = m.group(1), m.group(2)
                fields = body.split("~")
                if len(fields) <= 33 or not fields[1]:
                    continue
                # 字段约定：[1]名称 [3]现价 [4]昨收 [31]涨跌额 [32]涨跌幅%
                price = fields[3]
                pct = None
                try:
                    pct = float(fields[32])
                except (ValueError, IndexError):
                    pass
                if pct is None:
                    try:  # 极端情况下用现价/昨收兜底重算
                        pct = round(
                            (float(fields[3]) - float(fields[4]))
                            / float(fields[4]) * 100, 2
                        )
                    except (ValueError, ZeroDivisionError, IndexError):
                        pct = None
                result[tcode] = {"name": fields[1], "price": price, "pct": pct}
        except Exception as exc:  # noqa: BLE001
            log(f"[行情] 第 {i // QUOTE_BATCH + 1} 批查询失败：{type(exc).__name__}: {exc}")
        time.sleep(0.2)
    return result


# ============================ 事件组装 ============================

def norm_title(title: str) -> str:
    """标题归一化：去空白与标点，用于跨源同稿去重"""
    return re.sub(r"[\s\u3000\W_]+", "", title or "").lower()


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


# ============================ 今日速览（综合摘要） ============================

# 高危标签：在重点事件评分中额外加权，更容易进入速览
HIGH_RISK_TAGS = {"监管调查", "经营风险", "高管异动"}


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


def render_summary_block(summary: dict) -> str:
    """渲染"今日速览"三栏区块：跌幅榜 / 事件类型分布 / 重点事件"""
    # —— 栏 1：跌幅榜 ——
    if summary["losers"]:
        loser_rows = "".join(
            f'<a href="#evt-{e["eid"]}" class="flex items-center justify-between gap-2 '
            f'py-1.5 border-b border-slate-800/60 last:border-0 hover:bg-slate-800/40 '
            f'rounded px-1 -mx-1 transition-colors">'
            f'<span class="text-sm text-slate-200 truncate">{e["quote"]["name"]}'
            f'<span class="text-[10px] font-mono text-slate-500 ml-1">{e["stock"]["display"]}</span></span>'
            f'<span class="text-sm font-mono shrink-0 {pct_classes(e["quote"]["pct"])} '
            f'px-1.5 py-0.5 rounded border">{fmt_pct(e["quote"]["pct"])}</span></a>'
            for e in summary["losers"]
        )
    else:
        loser_rows = '<div class="text-xs text-slate-600 py-3">今日暂无带行情标的</div>'

    # —— 栏 2：事件类型分布（CSS 条形） ——
    if summary["tag_rank"]:
        max_count = max(c for _, c in summary["tag_rank"])
        tag_rows = "".join(
            f'<div class="flex items-center gap-2 py-1">'
            f'<span class="text-xs text-slate-400 w-16 shrink-0 text-right">#{html.escape(tag)}</span>'
            f'<div class="flex-1 h-3 bg-slate-800/60 rounded overflow-hidden">'
            f'<div class="h-full rounded" style="width:{cnt / max_count * 100:.0f}%;'
            f'background:linear-gradient(90deg,#b45309,#f59e0b)"></div></div>'
            f'<span class="text-xs font-mono text-slate-300 w-6 shrink-0">{cnt}</span></div>'
            for tag, cnt in summary["tag_rank"]
        )
    else:
        tag_rows = '<div class="text-xs text-slate-600 py-3">今日无命中标签</div>'

    # —— 栏 3：重点事件 Top5 ——
    if summary["top_events"]:
        top_rows = "".join(
            f'<a href="#evt-{e["eid"]}" class="block py-1.5 border-b border-slate-800/60 '
            f'last:border-0 hover:bg-slate-800/40 rounded px-1 -mx-1 transition-colors">'
            f'<div class="flex items-center gap-1.5 text-[11px] text-slate-500 mb-0.5">'
            f'<span class="font-mono">{e["time"]:%H:%M}</span>'
            + "".join(
                f'<span class="px-1 rounded {TAG_COLORS.get(t, "bg-slate-800 text-slate-400")} '
                f'border border-slate-800">#{html.escape(t)}</span>'
                for t in e["tags"][:2]
            )
            + (f'<span class="font-mono {pct_classes(e["quote"]["pct"])} px-1 rounded border shrink-0">'
               f'{fmt_pct(e["quote"]["pct"])}</span>' if e["quote"] and e["quote"]["pct"] is not None else "")
            + f'</div>'
            f'<div class="text-sm text-slate-200 truncate">'
            f'{html.escape(e["title"] or e["content"][:40])}</div></a>'
            for e in summary["top_events"]
        )
    else:
        top_rows = '<div class="text-xs text-slate-600 py-3">今日无事件</div>'

    return f"""
    <section class="mb-6 bg-gradient-to-br from-slate-900 to-slate-900/40 border border-slate-800
                    rounded-xl p-4 sm:p-5">
      <div class="flex items-center gap-2 mb-4">
        <span class="text-amber-400 text-lg">📌</span>
        <h2 class="text-base font-bold text-slate-100">今日速览</h2>
        <span class="text-xs text-slate-500">点击条目可跳转到对应事件详情</span>
      </div>
      <div class="grid md:grid-cols-3 gap-5">
        <div>
          <div class="text-xs font-semibold text-rose-400/90 mb-2 tracking-wide">跌幅榜 TOP5（同一标的取最新）</div>
          {loser_rows}
        </div>
        <div>
          <div class="text-xs font-semibold text-amber-400/90 mb-2 tracking-wide">事件类型分布</div>
          {tag_rows}
        </div>
        <div>
          <div class="text-xs font-semibold text-cyan-400/90 mb-2 tracking-wide">重点事件 TOP5（规则评分）</div>
          {top_rows}
        </div>
      </div>
    </section>
    """


# ============================ HTML 渲染 ============================

def fmt_pct(pct: Optional[float]) -> str:
    """涨跌幅格式化：+1.23% / -1.49%"""
    if pct is None:
        return "--"
    return f"{pct:+.2f}%"


def pct_classes(pct: Optional[float]) -> str:
    """按涨跌幅返回徽章样式：下跌红色警示，跌幅超 3% 加底加粗"""
    if pct is None:
        return "bg-slate-500/10 text-slate-400 border-slate-500/20"
    if pct <= -3:
        return "bg-rose-500/20 text-rose-300 border-rose-500/50 font-bold"
    if pct < 0:
        return "bg-rose-500/10 text-rose-400 border-rose-500/25"
    if pct > 0:
        return "bg-emerald-500/10 text-emerald-400 border-emerald-500/25"
    return "bg-slate-500/10 text-slate-300 border-slate-500/25"


def card_accent(pct: Optional[float]) -> str:
    """卡片左侧强调条：跌幅超 3% 高亮红色"""
    if pct is not None and pct <= -3:
        return "border-l-rose-500"
    return "border-l-slate-800"


def render_card(event: dict) -> str:
    """渲染单条事件卡片 HTML"""
    stock = event["stock"]
    quote = event["quote"]
    pct = quote["pct"] if quote else None

    # 标的区块：名称优先取行情接口返回的官方简称；新三板无行情接口，用中性占位
    if stock:
        if quote:
            name = html.escape(quote["name"])
        elif stock["market"] == "新三板":
            name = '<span class="text-slate-400 font-normal text-sm">新三板挂牌公司</span>'
        else:
            name = '<span class="text-slate-500">未知名称</span>'
        stock_html = (
            f'<span class="font-semibold text-cyan-300">{name}</span>'
            f'<span class="ml-1.5 text-xs font-mono text-cyan-500/80">'
            f'{html.escape(stock["display"])}</span>'
        )
        if stock["market"] == "A股":
            market_badge = (
                '<span class="text-[10px] px-1.5 py-0.5 rounded border '
                'border-cyan-500/30 bg-cyan-500/10 text-cyan-300">A股</span>'
            )
        elif stock["market"] == "港股":
            market_badge = (
                '<span class="text-[10px] px-1.5 py-0.5 rounded border '
                'border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-300">港股</span>'
            )
        else:  # 新三板
            market_badge = (
                '<span class="text-[10px] px-1.5 py-0.5 rounded border '
                'border-amber-500/30 bg-amber-500/10 text-amber-300">新三板</span>'
            )
    else:
        stock_html = '<span class="text-slate-500 text-sm">未识别标的</span>'
        market_badge = (
            '<span class="text-[10px] px-1.5 py-0.5 rounded border '
            'border-slate-600 bg-slate-700/30 text-slate-400">无代码</span>'
        )

    # 行情区块：现价 + 涨跌幅
    if quote:
        quote_html = (
            f'<span class="text-sm font-mono text-slate-300 mr-2">'
            f'¥{html.escape(quote["price"])}</span>'
            f'<span class="inline-flex items-center px-2 py-0.5 rounded-md border '
            f'text-sm font-mono {pct_classes(pct)}">{fmt_pct(pct)}</span>'
        )
    else:
        quote_html = (
            '<span class="inline-flex items-center px-2 py-0.5 rounded-md border '
            'text-xs font-mono bg-slate-500/10 text-slate-500 border-slate-500/20">'
            '行情暂无</span>'
        )

    # 关键词标签
    tag_html = "".join(
        f'<span class="inline-block px-2 py-0.5 rounded border text-xs '
        f'{TAG_COLORS.get(tag, "bg-slate-500/15 text-slate-300 border-slate-500/30")}">'
        f'#{html.escape(tag)}</span>'
        for tag in event["tags"]
    )

    # 标题 + 摘要：无独立标题时只显示摘要
    title_html = ""
    if event["title"]:
        title_html = (
            f'<h3 class="text-[15px] font-semibold text-slate-100 leading-snug">'
            f'{html.escape(event["title"])}</h3>'
        )
    summary = event["content"]
    if len(summary) > 220:
        summary = summary[:220].rstrip() + "…"
    # 摘要若与标题完全重复则不再重复展示
    if summary and summary.strip() == event["title"].strip():
        summary = ""

    safe_url = event["url"] if event["url"].startswith(("http://", "https://")) else "#"
    search_text = html.escape(
        f"{event['title']} {event['content']} "
        f"{quote['name'] if quote else ''} {stock['display'] if stock else ''}"
        .lower()
    )

    return f"""
    <article id="evt-{event['eid']}" class="event-card bg-slate-900/70 border border-slate-800 border-l-2 {card_accent(pct)}
                    rounded-lg p-4 hover:border-slate-700 transition-colors"
             data-market="{stock['market'] if stock else 'NONE'}"
             data-tags="{' '.join(event['tags'])}"
             data-search="{search_text}">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs mb-2">
        <span class="font-mono text-slate-400">{event['time']:%Y-%m-%d %H:%M}</span>
        <span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">{html.escape(event['source'])}</span>
        {market_badge}
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
        <div class="flex items-center flex-wrap gap-1.5">{stock_html}</div>
        <div class="flex items-center whitespace-nowrap">{quote_html}</div>
      </div>
      {title_html}
      {f'<p class="text-sm text-slate-400 leading-relaxed mt-1.5">{html.escape(summary)}</p>' if summary else ''}
      <div class="flex flex-wrap items-center justify-between gap-2 mt-3">
        <div class="flex flex-wrap gap-1.5">{tag_html}</div>
        <a href="{html.escape(safe_url)}" target="_blank" rel="noopener noreferrer"
           class="text-xs px-2.5 py-1 rounded-md bg-slate-800 hover:bg-amber-500/20
                  hover:text-amber-300 text-slate-300 border border-slate-700
                  hover:border-amber-500/40 transition-colors shrink-0">
          查看原文 ↗
        </a>
      </div>
    </article>
    """


def render_html(events: List[dict]) -> str:
    """把事件列表渲染进完整 HTML 模板（Tailwind CDN + 原生 JS 筛选）"""
    # 统计卡片数据
    a_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "A股"}
    hk_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "港股"}

    cards_html = "\n".join(render_card(e) for e in events)
    if not events:
        cards_html = """
        <div class="text-center py-24 text-slate-500">
          <div class="text-5xl mb-4">🛡️</div>
          <p class="text-lg">今日各数据源暂无命中负面/突发关键词的舆情</p>
          <p class="text-sm mt-2 text-slate-600">下次构建时间：北京时间 18:00（也可在 GitHub Actions 手动触发）</p>
        </div>
        """

    # 页脚：各数据源抓取/命中明细
    source_lines = "".join(
        f'<span class="inline-flex items-center gap-1 text-xs px-2 py-1 rounded '
        f'bg-slate-900 border border-slate-800 text-slate-400">'
        f'{html.escape(name)}'
        f'<b class="text-slate-200 font-mono">{st["fetched"]}</b>/'
        f'<b class="text-amber-400/80 font-mono">{st["matched"]}</b></span>'
        for name, st in SOURCE_STATS.items()
    )

    # 所有出现过的标签（用于顶部标签筛选按钮）
    all_tags = sorted({tag for e in events for tag in e["tags"]})
    tag_buttons = "".join(
        f'<button onclick="toggleTag(this,\'{tag}\')" '
        f'class="tag-filter text-xs px-2.5 py-1 rounded-full border border-slate-700 '
        f'text-slate-400 hover:border-amber-500/50 hover:text-amber-300 transition-colors">'
        f'#{tag}</button>'
        for tag in all_tags
    )

    template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A/港股 舆情突发与伪负面监控看板</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
         "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
  .event-card[hidden] { display: none; }
  html { scroll-behavior: smooth; }
  /* 速览区点击跳转后，目标卡片高亮闪烁提示 */
  .event-card:target { border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,.35); }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
</style>
</head>
<body class="bg-slate-950 text-slate-200 min-h-screen">
<div class="max-w-6xl mx-auto px-4 sm:px-6 py-6">

  <!-- 页头 -->
  <header class="border-b border-slate-800 pb-5 mb-6">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-xl sm:text-2xl font-bold tracking-tight">
          <span class="text-amber-400">A/港股</span>
          <span class="text-slate-100">舆情突发与伪负面监控看板</span>
        </h1>
        <p class="text-xs text-slate-500 mt-1.5">
          多源财经快讯 · 负面关键词命中 · 腾讯行情叠加 · 每日 18:00（北京时间）自动构建
        </p>
      </div>
      <div class="text-right text-xs text-slate-500">
        <div>数据更新时间</div>
        <div class="font-mono text-sm text-slate-300 mt-0.5">__BUILD_TIME__ <span class="text-slate-600">UTC+8</span></div>
      </div>
    </div>
  </header>

  <!-- 统计卡片 -->
  <section class="grid grid-cols-3 gap-3 sm:gap-4 mb-6">
    <div class="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
      <div class="text-xs text-slate-500 mb-1">今日捕获事件</div>
      <div class="text-2xl sm:text-3xl font-bold font-mono text-amber-400">__TOTAL__</div>
    </div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
      <div class="text-xs text-slate-500 mb-1">涉及 A 股标的</div>
      <div class="text-2xl sm:text-3xl font-bold font-mono text-cyan-300">__A_COUNT__</div>
    </div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
      <div class="text-xs text-slate-500 mb-1">涉及港股标的</div>
      <div class="text-2xl sm:text-3xl font-bold font-mono text-fuchsia-300">__HK_COUNT__</div>
    </div>
  </section>

  <!-- 今日速览（综合摘要：跌幅榜 / 类型分布 / 重点事件） -->
__SUMMARY__

  <!-- 筛选栏 -->
  <section class="mb-5 space-y-3">
    <div class="flex flex-wrap items-center gap-2">
      <div class="flex rounded-lg overflow-hidden border border-slate-700 text-xs">
        <button onclick="setMarket('ALL',this)" data-market-btn="ALL"
                class="market-btn px-3 py-1.5 bg-amber-500/20 text-amber-300">全部</button>
        <button onclick="setMarket('A股',this)" data-market-btn="A股"
                class="market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200">A股</button>
        <button onclick="setMarket('港股',this)" data-market-btn="港股"
                class="market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200">港股</button>
        <button onclick="setMarket('NONE',this)" data-market-btn="NONE"
                class="market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200">未识别</button>
      </div>
      <input id="searchBox" type="text" placeholder="搜索公司 / 代码 / 关键词…"
             oninput="applyFilters()"
             class="flex-1 min-w-[180px] bg-slate-900 border border-slate-800 rounded-lg
                    px-3 py-1.5 text-sm text-slate-200 placeholder-slate-600
                    focus:outline-none focus:border-amber-500/50">
      <button onclick="resetFilters()"
              class="text-xs px-3 py-1.5 rounded-lg border border-slate-700
                     text-slate-400 hover:text-slate-200">重置</button>
    </div>
    <div class="flex flex-wrap gap-1.5" id="tagBar">__TAG_BUTTONS__</div>
  </section>

  <!-- 事件列表 -->
  <main id="eventList" class="space-y-3">
__CARDS__
  </main>

  <div id="noResult" hidden
       class="text-center py-16 text-slate-600 text-sm">当前筛选条件下没有事件</div>

  <!-- 页脚 -->
  <footer class="mt-10 pt-5 border-t border-slate-800 text-xs text-slate-500 space-y-3">
    <div class="flex flex-wrap gap-1.5">
      <span class="text-slate-600 mr-1 py-1">数据源（抓取/命中）：</span>
      __SOURCE_LINES__
    </div>
    <p class="leading-relaxed">
      说明：本看板由 GitHub Actions 定时自动抓取公开财经快讯并经关键词规则生成，
      内容版权归原作者所有；"伪负面"指传闻、辟谣、澄清等未经证实或已被否认的信息，
      本页不对信息真实性负责，<span class="text-slate-400">不构成任何投资建议</span>。
    </p>
  </footer>
</div>

<script>
// 当前筛选状态：市场维度 + 标签集合 + 搜索词
var state = { market: 'ALL', tags: new Set(), q: '' };

// 切换市场筛选
function setMarket(m, btn) {
  state.market = m;
  document.querySelectorAll('.market-btn').forEach(function (b) {
    b.className = 'market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200';
  });
  btn.className = 'market-btn px-3 py-1.5 bg-amber-500/20 text-amber-300';
  applyFilters();
}

// 切换关键词标签（支持多选，再点一次取消）
function toggleTag(btn, tag) {
  if (state.tags.has(tag)) {
    state.tags.delete(tag);
    btn.className = 'tag-filter text-xs px-2.5 py-1 rounded-full border border-slate-700 '
      + 'text-slate-400 hover:border-amber-500/50 hover:text-amber-300 transition-colors';
  } else {
    state.tags.add(tag);
    btn.className = 'tag-filter text-xs px-2.5 py-1 rounded-full border '
      + 'border-amber-500/50 bg-amber-500/15 text-amber-300 transition-colors';
  }
  applyFilters();
}

// 根据当前状态过滤全部卡片
function applyFilters() {
  state.q = document.getElementById('searchBox').value.trim().toLowerCase();
  var visible = 0;
  document.querySelectorAll('.event-card').forEach(function (card) {
    var okMarket = state.market === 'ALL' || card.dataset.market === state.market;
    var cardTags = (card.dataset.tags || '').split(' ');
    var okTag = true;
    state.tags.forEach(function (t) { if (cardTags.indexOf(t) === -1) okTag = false; });
    var okQ = !state.q || (card.dataset.search || '').indexOf(state.q) !== -1;
    var show = okMarket && okTag && okQ;
    card.hidden = !show;
    if (show) visible++;
  });
  document.getElementById('noResult').hidden = visible !== 0;
}

// 一键重置筛选
function resetFilters() {
  state = { market: 'ALL', tags: new Set(), q: '' };
  document.getElementById('searchBox').value = '';
  document.querySelectorAll('.tag-filter').forEach(function (b) {
    b.className = 'tag-filter text-xs px-2.5 py-1 rounded-full border border-slate-700 '
      + 'text-slate-400 hover:border-amber-500/50 hover:text-amber-300 transition-colors';
  });
  var btns = document.querySelectorAll('.market-btn');
  btns.forEach(function (b) {
    if (b.dataset.marketBtn === 'ALL') {
      b.className = 'market-btn px-3 py-1.5 bg-amber-500/20 text-amber-300';
    } else {
      b.className = 'market-btn px-3 py-1.5 text-slate-400 hover:text-slate-200';
    }
  });
  applyFilters();
}
</script>
</body>
</html>
"""

    return (
        template
        .replace("__BUILD_TIME__", NOW.strftime("%Y-%m-%d %H:%M"))
        .replace("__TOTAL__", str(len(events)))
        .replace("__A_COUNT__", str(len(a_codes)))
        .replace("__HK_COUNT__", str(len(hk_codes)))
        .replace("__TAG_BUTTONS__", tag_buttons)
        .replace("__SUMMARY__", render_summary_block(summarize(events)))
        .replace("__CARDS__", cards_html)
        .replace("__SOURCE_LINES__", source_lines)
    )


# ============================ 入口 ============================

def main() -> int:
    """主流程：抓取 → 组装 → 渲染 → 写文件"""
    log(f"开始构建舆情看板，北京时间 {NOW:%Y-%m-%d %H:%M:%S}")
    news_rows = collect_all_news()
    events = build_events(news_rows)

    a_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "A股"}
    hk_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "港股"}
    log(f"生成事件 {len(events)} 条 | A股标的 {len(a_codes)} 个 | 港股标的 {len(hk_codes)} 个")

    page = render_html(events)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"看板已写入 {OUTPUT_HTML}（{len(page) / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
