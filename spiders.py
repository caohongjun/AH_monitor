# -*- coding: utf-8 -*-
"""spiders.py — 数据抓取层：财经快讯 / 腾讯行情 / RSS(Atom) / HN / ai-bot"""
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from config import (CN_TZ, NOW, TODAY, TODAY_START, MAX_EVENTS, OUTPUT_HTML,
                    QUOTE_BATCH, KEYWORD_GROUPS, TAG_COLORS,
                    SOURCE_STATS, HIGH_RISK_TAGS, MARKET_CORE_KEYWORDS,
                    MARKET_KEYWORDS, MARKET_GLOBAL_KEYWORDS, BRIEF_GROUP_MAX,
                    HEADLINES_MAX, NAV_ITEMS, WATCHLIST, WATCH_RELATED_MAX,
                    GLOBAL_TAG_RULES, AI_CORE, AI_PRODUCT_VERBS, source_rule)
from utils import (SESSION, http_get, log, strip_html, parse_dt, ts_to_dt,
                   ms_to_dt, is_today, norm_title, dedupe_and_sort,
                   is_ai_item, is_ai_product, zh_tags, filter_by_time_rule,
                   parse_sina_stocks, parse_em_stock_list)


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


# ============================ AI 资讯与海外产品页 ============================

# RSS/HN 条目统一结构：{title, url, time(北京时间), summary, source}

import email.utils as _email_utils


def fetch_rss(url: str, source_name: str, limit: int = 20,
              time_rule: str = "today_yesterday") -> List[dict]:
    """通用 RSS 2.0 / Atom 抓取解析：返回条目列表，时间统一转为北京时间。
    时间策略 time_rule 见 config.SOURCE_RULES：today_yesterday 只保留当天（无则前一天，
    再无则空），realtime 不过滤。为避免先截断后过滤导致丢数据，解析时多取若干条，
    过滤完成后再截到 limit。"""
    # 解析上限放大：feed 里可能混有更早的条目，多取才能覆盖今天/昨天的全量
    fetch_cap = max(50, limit * 3)
    try:
        resp = SESSION.get(url, timeout=15,
                           headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        print(f"  [RSS] {source_name} 抓取失败: {exc}")
        return []
    items: List[dict] = []
    if root.tag == "{http://www.w3.org/2005/Atom}feed":
        # —— Atom 格式（如 Product Hunt）：entry/title/link@href/published ——
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href") or "").strip() if link_el is not None else ""
            pub = (entry.findtext("{http://www.w3.org/2005/Atom}published")
                   or entry.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()
            desc = html.unescape(re.sub(r"<[^>]+>", " ",
                                        entry.findtext("{http://www.w3.org/2005/Atom}content")
                                        or entry.findtext("{http://www.w3.org/2005/Atom}summary") or "")).strip()
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(CN_TZ) if pub else None
            except Exception:
                dt = None
            if not title:
                continue
            items.append({"title": title, "url": link, "time": dt,
                          "summary": desc[:120], "source": source_name})
            if len(items) >= fetch_cap:
                break
    else:
        # —— RSS 2.0 格式 ——
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or "")).strip()
            # RFC2822 时间解析并统一到北京时间
            try:
                dt = _email_utils.parsedate_to_datetime(pub).astimezone(CN_TZ) if pub else None
            except Exception:
                dt = None
            if not title:
                continue
            items.append({"title": title, "url": link, "time": dt,
                          "summary": desc[:120], "source": source_name})
            if len(items) >= fetch_cap:
                break
    # 按该源独立的时间窗口策略过滤（今天 → 昨天 → 空），再截断到展示上限
    items = filter_by_time_rule(items, time_rule, source_name)[:limit]
    print(f"  [RSS] {source_name}: {len(items)} 条")
    return items


def fetch_hn(limit: int = 25) -> List[dict]:
    """Hacker News 热点抓取：topstories 取前 N 个 id，逐条拉取详情"""
    try:
        resp = SESSION.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15)
        ids = resp.json()[:limit]
    except Exception as exc:
        print(f"  [HN] 抓取失败: {exc}")
        return []
    items = []
    for i in ids:
        try:
            r = SESSION.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=10)
            d = r.json() or {}
            if not d.get("title"):
                continue
            items.append({
                "title": d["title"],
                "url": d.get("url") or f"https://news.ycombinator.com/item?id={i}",
                "time": datetime.fromtimestamp(d.get("time", 0), tz=timezone.utc).astimezone(CN_TZ),
                "summary": f"HN 热度 {d.get('score', 0)} 分 / {d.get('descendants', 0)} 评论",
                "source": "Hacker News",
            })
        except Exception:
            continue
    print(f"  [HN] Hacker News: {len(items)} 条")
    return items


def fetch_aibot(limit: int = 25) -> List[dict]:
    """ai-bot.cn AI 快讯日报页解析（该站关闭了 RSS，改爬 /daily 页面）：
    .news-item 内 h2>a 为标题与链接，p 为摘要；条目无精确时间，用抓取时刻近似"""
    try:
        resp = SESSION.get("https://ai-bot.cn/daily/", timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"  # 该页未声明 charset，requests 会误用 latin-1 导致中文乱码
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        print(f"  [ai-bot] 抓取失败: {exc}")
        return []
    items = []
    approx_time = NOW  # 同一时刻 + sorted 稳定性 = 保持页面原有的新旧顺序
    for box in soup.select(".news-item")[:limit]:
        a = box.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        p = box.select_one("p")
        summary = p.get_text(" ", strip=True)[:120] if p else ""
        items.append({"title": title, "url": a.get("href") or "",
                      "time": approx_time, "summary": summary, "source": "ai-bot快讯"})
    print(f"  [ai-bot] ai-bot.cn: {len(items)} 条")
    return items


def build_ai_data() -> tuple:
    """收集 AI 页数据：量子位 + ai-bot（国内）、TechCrunch + HN（海外），筛出资讯流与产品发布"""
    items = (fetch_rss("https://www.qbitai.com/feed", "量子位", 30)
             + fetch_rss("https://techcrunch.com/feed/", "TechCrunch", 30)
             + fetch_hn(25)
             + fetch_aibot(25))
    ai_items = [it for it in items if is_ai_item(f"{it['title']} {it['summary']}")]
    # 国内 / 海外按来源划分（量子位与 ai-bot 为中文源）
    DOMESTIC_SOURCES = {"量子位", "ai-bot快讯"}
    domestic = [it for it in ai_items if it["source"] in DOMESTIC_SOURCES]
    overseas = [it for it in ai_items if it["source"] not in DOMESTIC_SOURCES]
    products = [it for it in ai_items if is_ai_product(f"{it['title']} {it['summary']}")]
    dom = dedupe_and_sort(domestic, 15)
    ovs = dedupe_and_sort(overseas, 15)
    prods = dedupe_and_sort(products, 12)
    print(f"  [AI] 国内 {len(dom)} 条 / 海外 {len(ovs)} 条 / 产品发布 {len(prods)} 条")
    return dom, ovs, prods


def translate_en2zh(text: str) -> str:
    """英文短句翻译为中文（Google translate_a 公开接口，无需密钥）；失败时降级返回原文"""
    if not text or not re.search(r"[a-zA-Z]{3,}", text):
        return text  # 无英文内容或空串直接返回
    try:
        resp = SESSION.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # 返回结构 [[["译句","原句",...],...]...]，拼接所有翻译分段
        zh = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
        return zh or text
    except Exception:
        return text


def fetch_ph_leaderboard(limit: int = 20) -> List[dict]:
    """抓取 Product Hunt 昨日每日榜单（中文数据）。
    数据源：开源项目 ViggoZ/producthunt-daily-hot 每日生成的 Markdown，
    直接拉取 raw.githubusercontent.com 上的 data/producthunt-daily-YYYY-MM-DD.md。
    内容已包含中文标语/介绍/关键词/票数，无需再翻译；时间规则沿用昨日榜。"""
    yesterday = datetime.now(CN_TZ) - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    items = _fetch_ph_markdown(date_str)
    # 当日文件尚未生成（GitHub Action 每日 15:01 BJ 才产出）时，回退到前一天
    if not items:
        prev = (yesterday - timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"  [PH榜单] {date_str} 数据未生成，尝试 {prev}")
        items = _fetch_ph_markdown(prev)
        date_str = prev
    if items:
        print(f"  [PH榜单] GitHub raw 抓取 {len(items)} 条（{date_str} 榜单），"
              f"榜首: {items[0]['title']}（{items[0].get('votes')} 票）")
    return items[:limit]


def _fetch_ph_markdown(date_str: str) -> List[dict]:
    """从 GitHub raw 拉取指定日期的 PH 每日 Markdown 并解析为榜单条目。
    返回空列表表示文件不存在或抓取失败。"""
    raw_url = (f"https://raw.githubusercontent.com/ViggoZ/producthunt-daily-hot/main/"
               f"data/producthunt-daily-{date_str}.md")
    try:
        resp = SESSION.get(raw_url, timeout=20)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [PH榜单] GitHub raw 请求失败 ({date_str}): {exc}")
        return []
    return _parse_ph_markdown(resp.text, date_str)


def _parse_ph_markdown(md_text: str, date_str: str) -> List[dict]:
    """解析 producthunt-daily-hot 生成的 Markdown，提取榜单产品（排名 / 名称 /
    中文标语 / 中文介绍 / 票数 / 产品官网链接）。源数据已为中文，无需翻译。"""
    items: List[dict] = []
    # 每个产品以 "## [N. 名称](PH链接)" 开头，按此切分
    blocks = re.split(r"\n##\s+\[", md_text)
    base_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    for block in blocks[1:]:
        head = re.match(r"(\d+)\.\s+([^\]]+)\]\(([^)]+)\)", block)
        if not head:
            continue
        rank = int(head.group(1))
        name = head.group(2).strip()
        ph_url = head.group(3).strip()
        # 中文标语
        tagline_m = re.search(r"\*\*标语\*\*[：:]\s*(.+)", block)
        tagline = tagline_m.group(1).strip() if tagline_m else ""
        # 中文介绍
        intro_m = re.search(r"\*\*介绍\*\*[：:]\s*(.+)", block)
        intro = intro_m.group(1).strip() if intro_m else ""
        # 票数（形如 "🔺506"）
        votes_m = re.search(r"\*\*票数\*\*[：:]\s*🔺?\s*([\d,]+)", block)
        votes = votes_m.group(1).replace(",", "") if votes_m else ""
        # 产品官网（优先于 PH 链接）
        site_m = re.search(r"\*\*产品网站\*\*[：:]\s*\[立即访问\]\(([^)]+)\)", block)
        site_url = site_m.group(1).strip() if site_m else ph_url
        items.append({
            "title": name,
            "url": site_url,
            "tagline": tagline,        # 中文标语
            "tagline_en": "",          # 源数据无英文标语，留空
            "summary": intro,          # 中文介绍（用于标签生成）
            "votes": votes,
            "source": "Product Hunt",
            "time": base_date,
        })
    return items


def build_global_data() -> dict:
    """收集海外产品页数据：PH 独立成栏；Steam/GameLook 归游戏类；白鲸/HN/TechCrunch 按规则分类"""
    # PH 榜单：从 producthunt-daily-hot 开源项目拉取昨日中文榜单（标语/介绍已为中文）
    ph_items = fetch_ph_leaderboard(20)
    items = (fetch_rss("https://store.steampowered.com/feeds/newreleases.xml", "Steam 新品",
                       source_rule("Steam 新品", "limit", 20), source_rule("Steam 新品", "time_rule", "today_yesterday"))
             + fetch_rss("http://www.gamelook.com.cn/feed", "GameLook",
                         source_rule("GameLook", "limit", 20), source_rule("GameLook", "time_rule", "today_yesterday"))
             + fetch_baijing_home(source_rule("白鲸出海", "limit", 20))
             + fetch_rss("https://techcrunch.com/feed/", "TechCrunch",
                         source_rule("TechCrunch", "limit", 20), source_rule("TechCrunch", "time_rule", "today_yesterday"))
             + fetch_hn(source_rule("Hacker News", "limit", 25)))
    groups = {"游戏": [], "工具": [], "应用": [], "更多": []}
    seen = set()
    for it in items:
        key = norm_title(it["title"])
        if key in seen:
            continue
        seen.add(key)
        # 游戏类：Steam 官方新品与 GameLook（游戏行业垂直媒体）直接归组
        if it["source"] in ("Steam", "GameLook") or "游戏" in zh_tags(it["title"]):
            groups["游戏"].append(it)
            continue
        tags = zh_tags(f"{it['title']} {it['summary']}")
        if "工具" in tags:
            groups["工具"].append(it)
        elif "应用" in tags:
            groups["应用"].append(it)
        else:
            groups["更多"].append(it)
    for name in groups:
        groups[name] = dedupe_and_sort(groups[name], 20)
    groups["PH精选"] = ph_items  # Product Hunt 独立栏（不参与其余分类）
    print("  [海外] " + " / ".join(f"{k} {len(v)}" for k, v in groups.items()))
    return groups


# ========================= 热榜类数据源 =========================

def fetch_toutiao_hot(limit: int = 20) -> List[dict]:
    """今日头条热榜：官方 hot-board 接口返回 JSON，含 Title/Url/热度（实时榜单，不按日期过滤）"""
    try:
        resp = SESSION.get("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
                           timeout=12)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        items = []
        for it in data[:limit]:
            items.append({
                "title": it.get("Title", "").strip(),
                "url": it.get("Url", ""),
                "hot": it.get("HotValue", ""),
                "source": "今日头条",
            })
        print(f"  [头条热榜] {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [头条热榜] 抓取失败: {e}")
        return []


def fetch_weibo_hot(limit: int = 20) -> List[dict]:
    """微博热搜：优先用 weibo.com 官方访客接口（必须先访问首页拿访客 Cookie，
    且 ajax 请求必须带 Referer，否则返回 403 Forbidden）；
    失败再回退第三方聚合 API（oioweb / tenapi）。实时榜单，不按日期过滤"""
    # 方案 A：weibo.com 官方访客接口（2026-09 验证：不带 Referer 必 403，带后返回 50+ 条）
    try:
        SESSION.get("https://weibo.com/", timeout=8)
        resp = SESSION.get("https://weibo.com/ajax/side/hotSearch", timeout=10,
                           headers={"Referer": "https://weibo.com/",
                                    "X-Requested-With": "XMLHttpRequest",
                                    "Accept": "application/json"})
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            realtime = resp.json().get("data", {}).get("realtime", [])
            items = []
            for it in realtime:
                word = (it.get("word") or "").strip()
                if not word:
                    continue
                items.append({
                    "title": word,
                    "url": "https://s.weibo.com/weibo?q=%23" + quote(word) + "%23",
                    "hot": it.get("num", ""),
                    "source": "微博热搜",
                })
                if len(items) >= limit:
                    break
            if items:
                print(f"  [微博热搜] weibo 官方源 {len(items)} 条")
                return items
    except Exception as e:
        print(f"  [微博热搜] 官方源失败: {str(e)[:100]}")
    # 方案 B：oioweb 聚合 API（部分网络环境存在证书拦截，关闭校验兜底）
    try:
        resp = SESSION.get("https://api.oioweb.cn/api/common/HotList?type=weibo",
                           timeout=10, verify=False)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            data = resp.json().get("data", [])
            if isinstance(data, list) and data:
                items = [{"title": it.get("title", "").strip(),
                          "url": it.get("url", ""),
                          "hot": it.get("hot", it.get("num", "")),
                          "source": "微博热搜"} for it in data[:limit]
                         if it.get("title")]
                if items:
                    print(f"  [微博热搜] oioweb 源 {len(items)} 条")
                    return items
    except Exception:
        pass
    # 方案 C：tenapi
    try:
        resp = SESSION.get("https://tenapi.cn/v2/weibohot", timeout=10)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            data = resp.json().get("data", [])
            if isinstance(data, list) and data:
                items = [{"title": it.get("name", it.get("title", "")).strip(),
                          "url": it.get("url", ""),
                          "hot": it.get("hot", ""),
                          "source": "微博热搜"} for it in data[:limit]
                         if it.get("name") or it.get("title")]
                if items:
                    print(f"  [微博热搜] tenapi 源 {len(items)} 条")
                    return items
    except Exception as e:
        print(f"  [微博热搜] 全部方案失败（降级）: {str(e)[:100]}")
    return []


def fetch_baidu_hot(limit: int = 20) -> List[dict]:
    """百度热搜：官方 board API 返回 JSON，嵌套结构 cards[0].content[0].content（实时榜单）"""
    try:
        resp = SESSION.get("https://top.baidu.com/api/board?platform=wise&tab=realtime", timeout=12)
        resp.raise_for_status()
        content = resp.json()["data"]["cards"][0]["content"][0]["content"]
        items = []
        for it in content[:limit]:
            items.append({
                "title": it.get("word", "").strip(),
                "url": it.get("url", ""),
                "hot": it.get("hotScore", ""),
                "source": "百度热搜",
            })
        print(f"  [百度热搜] {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [百度热搜] 抓取失败: {e}")
        return []


# Reddit RSS：r/all 每日热门（top?t=day），未登录即可访问，返回 Atom XML
_REDDIT_RSS_URL = "https://www.reddit.com/r/all/top/.rss?t=day&limit={cap}"
# Reddit 要求独特的 User-Agent，推荐格式：平台:应用名:版本 (by /u/用户名)
_REDDIT_UA = "python:ah-monitor-hotlist:v1.0 (by /u/ah_monitor_reader)"


def fetch_reddit(limit: int = 20) -> List[dict]:
    """Reddit r/all 每日热门讨论：通过官方 RSS（Atom）接口获取，免登录。
    取当日热度最高的帖子，标题译为中文，subreddit 作为来源标签展示。
    Reddit 未认证请求有速率限制，内置重试；RSS 不含点赞数，subreddit 作为副标题展示。"""
    cap = max(30, limit * 2)
    url = _REDDIT_RSS_URL.format(cap=cap)
    resp = None
    for attempt in range(3):
        try:
            resp = SESSION.get(url, headers={"User-Agent": _REDDIT_UA,
                                              "Accept": "application/atom+xml, application/xml"},
                                timeout=15)
            if resp.status_code == 200 and resp.content:
                break
            # 429/403 等限流：指数退避重试
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    if resp is None or resp.status_code != 200 or not resp.content:
        code = resp.status_code if resp is not None else "no-response"
        print(f"  [Reddit] RSS 抓取失败: HTTP {code}")
        return []
    try:
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"  [Reddit] XML 解析失败: {e}")
        return []
    atom = "{http://www.w3.org/2005/Atom}"
    items = []
    for entry in root.findall(f"{atom}entry"):
        title = (entry.findtext(f"{atom}title") or "").strip()
        if not title:
            continue
        link_el = entry.find(f"{atom}link")
        link = (link_el.get("href") or "").strip() if link_el is not None else ""
        # subreddit 在 category 的 label 属性中，如 "r/pics"
        cat_el = entry.find(f"{atom}category")
        subreddit = (cat_el.get("label") or "").strip() if cat_el is not None else ""
        # 标题译为中文，原文保留作补充；翻译失败则用原文
        zh_title = translate_en2zh(title)
        display = zh_title if zh_title and zh_title != title else title
        items.append({
            "title": display,
            "url": link,
            "tagline": subreddit,  # subreddit 作为副标题展示（如 r/pics）
            "source": "Reddit",
        })
        if len(items) >= limit:
            break
    print(f"  [Reddit] {len(items)} 条（r/all 每日热门）")
    return items


# 热门 AI / 产品 类微信公众号（科技动态板块补充源）
# 选取未被其他源覆盖的账号：机器之心/新智元(AI)、人人都是产品经理(产品)、差评(科技评论)、
# 笔记侠(商业笔记)、游戏那点事(游戏资讯)
_WECHAT_ACCOUNTS = ["机器之心", "新智元", "人人都是产品经理", "差评", "笔记侠", "游戏那点事"]


def fetch_wechat(limit: int = 15) -> List[dict]:
    """微信公众号热门文章：通过搜狗微信文章搜索(type=2)按账号名检索，每个公众号直接取其
    最新一篇文章（无时间窗过滤，避免搜狗索引滞后导致板块长期为空）。
    链接为搜狗跳转页，点击后由 JS 跳转至 mp.weixin.qq.com 原文。
    搜狗反爬较严：每个账号用独立 session（避开共享 cookie 累积触发风控），
    请求间加 3 秒间隔，0 结果时退避重试最多 3 次。
    注意：必须用 Mac UA，搜狗微信搜索会把 Windows UA 重定向到搜狗首页（无微信结果）。"""
    year = str(TODAY.year)
    # 搜狗微信搜索对 UA 敏感：Windows UA 会被重定向到搜狗首页，必须用 Mac UA
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    candidates: List[dict] = []
    seen_titles = set()
    per_query_cap = 8  # 每个查询多取几条候选，便于找到该账号真正最新一篇
    # 当前年月，用于构造"账号+当月"查询，提高命中当月最新文章的概率
    month_query = f"{year}年{NOW.month}月"
    for account in _WECHAT_ACCOUNTS:
        # 每个账号用独立 session，避免共享 cookie 累积触发搜狗风控
        acc_session = requests.Session()
        acc_session.headers.update({"User-Agent": ua})
        # 三组查询合并去重：当月 > 当年 > 账号名，覆盖面递增
        for query in (f"{account} {month_query}", f"{account} {year}", account):
            lis: list = []
            # 0 结果时退避重试最多 1 次（搜狗常对连续请求返回空页）
            # 注意：重试次数不宜多，6 账号 × 3 查询 × N 重试 容易触发搜狗 IP 风控
            for attempt in range(2):
                try:
                    resp = acc_session.get(
                        "https://weixin.sogou.com/weixin",
                        params={"type": 2, "query": query, "ie": "utf8"},
                        timeout=12,
                        verify=False,
                    )
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "html.parser")
                    lis = soup.select(".news-list li")
                    if lis:
                        break  # 拿到结果，跳出重试
                except Exception as e:
                    print(f"  [微信公众号] 搜索 '{query}' 失败(第{attempt+1}次): {e}")
                if attempt < 1:
                    time.sleep(3)  # 3s 退避
            got = 0
            for it in lis:
                if got >= per_query_cap:
                    break
                acc_el = it.select_one(".all-time-y2")
                acc = (acc_el.get_text(strip=True) if acc_el else "").strip()
                # 只保留该账号发布的文章（搜狗会混入提到该账号名的其他文章）
                if acc != account:
                    continue
                # 解析发布时间：搜狗把时间戳塞在 .s2 的 <script> timeConvert('xxx') </script> 中
                # 注意：BeautifulSoup 的 get_text() 会丢弃 <script> 内容，必须直接读 script.string
                s2_el = it.select_one(".s2")
                sc_el = s2_el.find("script") if s2_el else None
                sc_text = (sc_el.string if sc_el and sc_el.string
                           else (s2_el.decode_contents() if s2_el else ""))
                m_ts = re.search(r"timeConvert\('(\d+)'\)", sc_text)
                if not m_ts:
                    continue
                pub_dt = datetime.fromtimestamp(int(m_ts.group(1)), tz=CN_TZ)
                title_el = it.select_one("h3 a")
                if not title_el:
                    continue
                # 标题含 <em> 高亮标签会插入空格：先去掉汉字间多余空格，再去掉账号名重复
                raw_title = title_el.get_text(" ", strip=True)
                raw_title = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", raw_title)
                raw_title = re.sub(r"\s+", " ", raw_title).strip()
                title = raw_title.replace(account, "").strip(" ，,")
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                href = title_el.get("href", "")
                url = ("https://weixin.sogou.com" + href) if href.startswith("/link") else href
                summary_el = it.select_one("p.txt-info")
                summary = ""
                if summary_el:
                    summary = summary_el.get_text(" ", strip=True)
                    summary = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", summary)
                    summary = re.sub(r"\s+", " ", summary).strip()
                candidates.append({
                    "title": title,
                    "url": url,
                    "summary": summary[:120],
                    "tagline": account,  # 公众号名作为来源标签
                    "source": "微信公众号",
                    "pub_dt": pub_dt,  # 发布时间（北京时间），用于排序
                })
                got += 1
            # 查询间间隔，避免触发搜狗限流
            time.sleep(3)
    # 时间倒序，便于"每账号取最新一篇"的去重
    candidates.sort(key=lambda x: x["pub_dt"], reverse=True)
    # 每个公众号最多贡献 1 条（最新一篇），让更多账号有露脸机会
    seen_accounts: set = set()
    items: List[dict] = []
    for x in candidates:
        if x["tagline"] in seen_accounts:
            continue
        seen_accounts.add(x["tagline"])
        items.append(x)
    for x in items:
        x.pop("pub_dt", None)  # 排序后移除内部字段，避免污染下游渲染
    print(f"  [微信公众号] {len(items)} 条（{len(_WECHAT_ACCOUNTS)} 个账号，每账号取最新 1 篇）")
    return items[:limit]


def fetch_github_trending(limit: int = 25) -> List[dict]:
    """GitHub Trending：爬取 trending 页面，提取仓库名/描述/今日 star 数。
    带 3 次重试，应对网络抖动；选择器做了兼容以适配 GitHub 页面结构变更。"""
    for attempt in range(3):
        try:
            resp = SESSION.get("https://github.com/trending", timeout=40)
            resp.raise_for_status()
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  [GitHub Trending] 抓取失败(重试3次): {e}")
            return []
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for article in soup.select("article.Box-row")[:limit]:
        h2 = article.select_one("h2 a")
        if not h2:
            continue
        # 仓库名格式 "owner / repo"，去掉空白和斜杠周围空格
        name = h2.get_text(strip=True).replace("\n", "").replace(" ", "")
        desc_tag = article.select_one("p")
        desc = desc_tag.get_text(strip=True) if desc_tag else ""
        # 今日 star 数：兼容多种 class 写法
        star_tag = (article.select_one("span.d-inline-block.float-sm-right")
                    or article.select_one("span.float-sm-right"))
        stars_today = star_tag.get_text(strip=True) if star_tag else ""
        items.append({
            "title": name,
            "url": f"https://github.com/{name}",
            "summary": desc,
            "hot": stars_today,
            "source": "GitHub Trending",
        })
    print(f"  [GitHub Trending] {len(items)} 条")
    return items


def fetch_36kr_rss(limit: int = 20, time_rule: str = "today_yesterday") -> List[dict]:
    """36kr 官方 RSS（www.36kr.com/feed），用内置 ElementTree 解析。
    解析时多取条目，再按时间窗口策略（今天→昨天→空）过滤后截断"""
    try:
        resp = SESSION.get("https://www.36kr.com/feed", timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:max(50, limit * 3)]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
            pub = item.findtext("pubDate") or ""
            items.append({
                "title": title,
                "url": link,
                "summary": desc[:120],
                "time": parse_dt(pub) if pub else None,
                "source": "36kr",
            })
        items = filter_by_time_rule(items, time_rule, "36kr")[:limit]
        print(f"  [36kr RSS] {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [36kr RSS] 抓取失败: {e}")
        return []


def fetch_a16z(limit: int = 20, time_rule: str = "today_yesterday") -> List[dict]:
    """a16z（Andreessen Horowitz）官网深度文章抓取。

    该站 2025 改版后关闭了 RSS 与 WP REST API（/feed/、/wp-json/ 均 404），
    但 Yoast 生成的 sitemap 仍可访问：sitemap_index.xml → 各 post-sitemapN.xml 分片，
    每个条目含 loc 与 lastmod（新文章的 lastmod 即发布时间；旧分片是迁移时间戳）。
    流程：汇总全部分片 → 按 lastmod 取最新若干候选 → 逐篇打开文章页，
    从 JSON-LD 提取精确的 datePublished / headline，再从 div.js-article-content
    提取完整正文（仅保留有实质内容的段落，正文过薄的播客/视频页丢弃）→
    按时间窗口策略过滤（该源为周更，配置为近 7 天窗口）后截断。"""
    try:
        resp = SESSION.get("https://a16z.com/sitemap_index.xml", timeout=15)
        resp.raise_for_status()
        iroot = ET.fromstring(resp.content)
    except Exception as e:
        print(f"  [a16z] sitemap 索引抓取失败: {e}")
        return []
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    shard_urls = [s.findtext(f"{ns}loc") for s in iroot.findall(f"{ns}sitemap")
                  if "post-sitemap" in (s.findtext(f"{ns}loc") or "")]
    candidates: List[tuple] = []
    for su in shard_urls:
        try:
            page = SESSION.get(su, timeout=20)
            page.raise_for_status()
            sroot = ET.fromstring(page.content)
            for u in sroot.findall(f"{ns}url"):
                loc = u.findtext(f"{ns}loc")
                if loc:
                    candidates.append((u.findtext(f"{ns}lastmod") or "", loc))
        except Exception as e:
            print(f"  [a16z] 分片 {su} 抓取失败: {e}")
    # 最新分片的 2026 时间戳自然排在旧分片 2023 迁移戳之前；只核对前 12 个候选
    candidates.sort(key=lambda x: x[0], reverse=True)
    items: List[dict] = []
    for _, loc in candidates[:12]:
        try:
            art = SESSION.get(loc, timeout=20)
            art.raise_for_status()
            text = art.text
            pub = (re.findall(r'"datePublished"\s*:\s*"([^"]+)"', text) or [""])[0]
            title = (re.findall(r'"headline"\s*:\s*"([^"]+)"', text)
                     or re.findall(r"<title>([^|<]+)", text) or [""])[0]
            title = html.unescape(title).strip()
            if not title:
                continue
            # 完整正文：div.js-article-content 内全部段落，剔除脚本/图片等非正文节点
            asoup = BeautifulSoup(text, "html.parser")
            body = asoup.select_one("div.js-article-content")
            if body is None:
                continue
            for junk in body.select("script, style, figure, noscript, aside"):
                junk.decompose()
            paras = []
            for p in body.find_all("p"):
                t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
                if t and len(t) >= 10:
                    paras.append(t)
            if sum(len(p) for p in paras) < 300:
                continue  # 正文过薄：多为播客/视频/专题页，不计入
            content = "\n\n".join(paras)
            if len(content) > 6000:
                content = content[:6000].rstrip() + "…"
            try:
                dt = datetime.fromisoformat(pub) if pub else None
            except ValueError:
                dt = None
            items.append({
                "title": title,
                "url": loc,
                "summary": paras[0][:200],          # 列表展示的实质摘要
                "content": content,                 # 完整正文（hover 展开）
                "time": dt.astimezone(CN_TZ) if dt is not None else None,
                "source": "a16z",
            })
        except Exception:
            continue
    items = filter_by_time_rule(items, time_rule, "a16z")[:limit]
    print(f"  [a16z] {len(items)} 条")
    return items


def _parse_baijing_reltime(text: str) -> Optional[datetime]:
    """解析白鲸首页接口的相对时间（'刚刚'/'N 分钟前'/'N 小时前'/'N 天前'）
    为北京时间 datetime；无法识别时返回 None"""
    t = (text or "").strip()
    if not t:
        return None
    if "刚刚" in t or "分钟" in t:
        return NOW
    m = re.search(r"(\d+)\s*小时前", t)
    if m:
        return NOW - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*天前", t)
    if m:
        return NOW - timedelta(days=int(m.group(1)))
    return None


def fetch_baijing_home(limit: int = 20, max_pages: int = 3) -> List[dict]:
    """白鲸出海首页文章：调用首页同款 AJAX 接口（POST /index/ajax/get_article/，
    参数 pn 为页码），接口返回的 add_time 是相对时间。
    只保留当天文章；当天没有更新（如周末）则回退前一天，其余日期不取。
    文章按时间倒序，翻页直到出现大前天及更早的文章即可覆盖今天/昨天全量。"""
    headers = {"X-Requested-With": "XMLHttpRequest", "Referer": "https://www.baijing.cn/"}
    today = NOW.astimezone(CN_TZ).date()
    yesterday = today - timedelta(days=1)
    cutoff = today - timedelta(days=2)   # 大前天：见到此日期即停止翻页
    buckets: Dict[str, list] = {"today": [], "yesterday": []}
    seen_ids = set()
    try:
        for pn in range(1, max_pages + 1):
            resp = SESSION.post("https://www.baijing.cn/index/ajax/get_article/",
                                data={"pn": pn}, headers=headers, timeout=15)
            resp.raise_for_status()
            arts = (resp.json().get("data") or {}).get("article_list") or []
            if not arts:
                break
            page_dates = []
            for a in arts:
                aid = a.get("id")
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                dt = _parse_baijing_reltime(a.get("add_time", ""))
                if dt is None:
                    continue
                d = dt.astimezone(CN_TZ).date()
                page_dates.append(d)
                target_key = ("today" if d == today
                              else "yesterday" if d == yesterday else None)
                if target_key:
                    buckets[target_key].append((d, a))
            # 已翻到大前天或更早，今天/昨天的文章必然收集完整
            if page_dates and min(page_dates) <= cutoff:
                break
            time.sleep(0.3)
    except Exception as e:
        print(f"  [白鲸出海] 首页接口抓取失败: {e}")
        return []

    day_articles = buckets["today"] or buckets["yesterday"]
    chosen = "当天" if buckets["today"] else "前一天"
    items: List[dict] = []
    for d, a in day_articles[:limit]:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            # 站内真实路由为 /article/{id}（无 .html 后缀；带后缀会落到"请稍候"错误页）
            "url": f"https://www.baijing.cn/article/{a.get('id')}",
            "summary": (a.get("synopsis") or "").strip()[:120],
            "time": datetime(d.year, d.month, d.day, 12, tzinfo=CN_TZ),
            "source": "白鲸出海",
        })
    print(f"  [白鲸出海] 首页{chosen}文章 {len(items)} 条")
    return items


def _parse_aibot_date(text: str, now: datetime) -> Optional[datetime]:
    """解析 ai-bot 日期标签（形如 '9月18·周五'）为北京时间日期对象。
    标签无年份：月份大于当前月时判定为去年（处理 1 月初看到去年 12 月数据的情况）"""
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    year = now.year if month <= now.month else now.year - 1
    try:
        return datetime(year, month, day, 12, 0, tzinfo=CN_TZ)
    except ValueError:
        return None


def fetch_aibot_news(limit: int = 20) -> List[dict]:
    """ai-bot.cn 每日 AI 新闻页：页面按 .news-date 日期标签分组（工作日更新），
    日期分组在嵌套的 .news-list 中，需递归扁平化处理。
    时间规则（见 config.SOURCE_RULES）：只抓当天分组；当天没有则抓昨天分组；
    昨天也没有（如周一、周日运行且周末未更新）则返回空，绝不抓更早分组，防止历史数据爆炸。"""
    try:
        resp = SESSION.get("https://ai-bot.cn/daily-ai-news/", timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"  [ai-bot 新闻] 抓取失败: {e}")
        return []

    root = soup.select_one(".news-list")
    if not root:
        print("  [ai-bot 新闻] 页面结构变更：未找到 .news-list")
        return []

    # 递归扁平化嵌套 .news-list，得到按文档顺序（时间倒序）的日期/条目流
    stream: List[tuple] = []

    def _flatten(list_el) -> None:
        """递归遍历 news-list：收集 ('date', 日期文本) 与 ('item', 条目元素)"""
        for c in list_el.children:
            if not hasattr(c, "name") or not c.name:
                continue
            cls = c.get("class", [])
            if "news-date" in cls:
                stream.append(("date", c.get_text(strip=True)))
            elif "news-item" in cls:
                stream.append(("item", c))
            elif "news-list" in cls:
                _flatten(c)

    _flatten(root)

    # 将流聚合为 {日期: [条目元素]} 字典，便于按今天/昨天精确选取
    groups: Dict[str, list] = {}
    cur_key: Optional[str] = None
    for kind, payload in stream:
        if kind == "date":
            cur_date = _parse_aibot_date(payload, NOW)
            cur_key = cur_date.strftime("%Y-%m-%d") if cur_date else None
            groups.setdefault(cur_key, [])
        elif cur_key is not None:
            groups.setdefault(cur_key, []).append(payload)

    today = NOW.astimezone(CN_TZ).date()
    yesterday = today - timedelta(days=1)
    # 只在"今天 / 昨天"两个分组里选：今天优先，今天没有才取昨天，其余日期一律不看
    boxes = groups.get(today.strftime("%Y-%m-%d"))
    chosen_date = datetime(today.year, today.month, today.day, 12, tzinfo=CN_TZ)
    if not boxes:
        boxes = groups.get(yesterday.strftime("%Y-%m-%d"))
        chosen_date = datetime(yesterday.year, yesterday.month, yesterday.day, 12, tzinfo=CN_TZ)
        if boxes:
            print(f"  [ai-bot 新闻] 当天无更新，展示前一天（{yesterday:%m月%d日}）数据")
    if not boxes:
        print("  [ai-bot 新闻] 今天与昨天均无分组，模块为空")
        return []

    items: List[dict] = []
    for box in boxes[:limit]:
        a = box.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if len(title) < 6 or not href:
            continue
        # 摘要在 p 中，其中 .news-time 是"来源：xxx"尾巴，提取前先剔除
        summary = ""
        p_tag = box.select_one("p")
        if p_tag:
            src = p_tag.select_one(".news-time")
            if src:
                src.extract()
            summary = p_tag.get_text(" ", strip=True)[:120]
        items.append({
            "title": title,
            "url": href,
            "summary": summary,
            "source": "ai-bot",
            "time": chosen_date,
        })
    print(f"  [ai-bot 新闻] {chosen_date:%m月%d日} 分组 {len(items)} 条")
    return items



def build_hotboard_data(market_headlines: dict = None) -> dict:
    """收集今日热榜页数据：社会舆情（头条/微博/财经要点）、科技动态（36kr/量子位/ai-bot）、
    游戏与产品（GameLook/PH/GitHub Trending），按板块分组返回。
    每个源的时间策略与条数统一从 config.SOURCE_RULES 读取，互不影响"""
    # 社会舆情板块：头条 + 微博 + 百度（均为实时榜单）+ Reddit 每日热门 + BBC 当日头条 + 财经要点（今天/昨天规则）
    social = {
        "今日头条": fetch_toutiao_hot(source_rule("今日头条", "limit", 20)),
        "微博热搜": fetch_weibo_hot(source_rule("微博热搜", "limit", 20)),
        "百度热搜": fetch_baidu_hot(source_rule("百度热搜", "limit", 20)),
        "Reddit": fetch_reddit(source_rule("Reddit", "limit", 20)),
        "BBC": fetch_rss("https://feeds.bbci.co.uk/news/rss.xml", "BBC",
                         source_rule("BBC", "limit", 20),
                         source_rule("BBC", "time_rule", "today_yesterday")),
    }
    if market_headlines:
        headlines = (market_headlines.get("domestic") or []) + (market_headlines.get("global") or [])
        headlines.sort(key=lambda t: (t.get("score", 0), t.get("time")), reverse=True)
        # 统一格式为热榜条目（保留 time 字段以支持按今天/昨天规则过滤）
        raw_briefs = [{
            "title": (it.get("title") or it.get("content", "")[:60]).strip(),
            "url": it.get("url", ""),
            "summary": (it.get("content") or "")[:80],
            "time": it.get("time"),
            "source": "财经要点",
        } for it in headlines]
        social["财经要点"] = filter_by_time_rule(
            raw_briefs, source_rule("财经要点", "time_rule", "today_yesterday"), "财经要点"
        )[:source_rule("财经要点", "limit", 20)]
    # 科技动态板块
    tech = {
        "36kr": fetch_36kr_rss(source_rule("36kr", "limit", 20),
                               source_rule("36kr", "time_rule", "today_yesterday")),
        "量子位": fetch_rss("https://www.qbitai.com/feed", "量子位",
                           source_rule("量子位", "limit", 20),
                           source_rule("量子位", "time_rule", "today_yesterday")),
        "a16z": fetch_a16z(source_rule("a16z", "limit", 20),
                           source_rule("a16z", "time_rule", "today_yesterday")),
        "ai-bot": fetch_aibot_news(source_rule("ai-bot", "limit", 20)),
        "白鲸出海": fetch_baijing_home(source_rule("白鲸出海", "limit", 20)),
        "微信公众号": fetch_wechat(source_rule("微信公众号", "limit", 15)),
    }
    # 游戏与产品板块
    gaming = {
        "GameLook": fetch_rss("http://www.gamelook.com.cn/feed", "GameLook",
                              source_rule("GameLook", "limit", 20),
                              source_rule("GameLook", "time_rule", "today_yesterday")),
        "Product Hunt": build_global_data()["PH精选"],
        "GitHub Trending": fetch_github_trending(source_rule("GitHub Trending", "limit", 25)),
    }
    print("  [热榜] 社会舆情:" + " ".join(f"{k}{len(v)}" for k, v in social.items())
          + " | 科技:" + " ".join(f"{k}{len(v)}" for k, v in tech.items())
          + " | 游戏产品:" + " ".join(f"{k}{len(v)}" for k, v in gaming.items()))
    return {"social": social, "tech": tech, "gaming": gaming}
