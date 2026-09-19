# -*- coding: utf-8 -*-
"""spiders.py — 数据抓取层：财经快讯 / 腾讯行情 / RSS(Atom) / HN / ai-bot"""
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from config import (CN_TZ, NOW, TODAY, TODAY_START, MAX_EVENTS, OUTPUT_HTML,
                    QUOTE_BATCH, BROWSER_HEADERS, KEYWORD_GROUPS, TAG_COLORS,
                    SOURCE_STATS, HIGH_RISK_TAGS, MARKET_CORE_KEYWORDS,
                    MARKET_KEYWORDS, MARKET_GLOBAL_KEYWORDS, BRIEF_GROUP_MAX,
                    HEADLINES_MAX, NAV_ITEMS, WATCHLIST, WATCH_RELATED_MAX,
                    GLOBAL_TAG_RULES, AI_CORE, AI_PRODUCT_VERBS)
from utils import (SESSION, http_get, log, strip_html, parse_dt, ts_to_dt,
                   ms_to_dt, is_today, norm_title, dedupe_and_sort,
                   is_ai_item, is_ai_product, zh_tags,
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


def fetch_rss(url: str, source_name: str, limit: int = 30, today_only: bool = True) -> List[dict]:
    """通用 RSS 2.0 / Atom 抓取解析：返回条目列表，时间统一转为北京时间。
    today_only=True 时仅保留北京时间当天的条目（无时间的条目保留，无法判定日期时不丢弃）"""
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
            if len(items) >= limit:
                break
        print(f"  [RSS] {source_name}: {len(items)} 条（Atom）")
        # 不 return，统一走下方三级回退过滤
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
        if len(items) >= limit:
            break
    print(f"  [RSS] {source_name}: {len(items)} 条")
    # 三级回退过滤：当天 → 最近24小时 → 首页全部
    if today_only:
        from datetime import timedelta
        now_cn = datetime.now(CN_TZ)
        # 第一级：北京时间当天
        today_items = [it for it in items if it["time"] is not None and is_today(it["time"])]
        if today_items:
            items = today_items
        else:
            # 第二级：最近24小时
            cutoff = now_cn - timedelta(hours=24)
            recent = [it for it in items if it["time"] is not None and it["time"] >= cutoff]
            if recent:
                items = recent
                print(f"  [RSS] {source_name}: 当日为空，取最近24小时 {len(items)} 条")
            else:
                # 第三级：首页全部（含无时间字段的）
                print(f"  [RSS] {source_name}: 近24小时为空，取首页全部 {len(items)} 条")
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


def _parse_ph_from_text(text: str) -> List[dict]:
    """从 PH leaderboard 页面全文本中兜底解析产品数据。
    按行扫描：遇到 /products/{slug} 链接后，取后续行作为 tagline，遇到 vote 行取投票数。"""
    if not text:
        return []
    results: List[dict] = []
    seen = set()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        # 匹配产品链接行
        m = re.search(r"/products/([a-z0-9-]+)", line)
        if m and m.group(1) not in seen:
            slug = m.group(1)
            seen.add(slug)
            name = line.split("/")[-1].replace("-", " ").title() if "/" not in line else line
            # 尝试从当前行提取产品名（去掉 URL 部分）
            name_clean = re.sub(r"https?://\S+|/\S+", "", line).strip() or slug.replace("-", " ").title()
            tagline = ""
            votes = ""
            # 向后扫描最多 5 行，找 tagline 和投票数
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = lines[j]
                vm = re.match(r"^([\d,]+)\s*vote", nxt, re.IGNORECASE)
                if vm:
                    votes = vm.group(1).replace(",", "")
                    break
                if not tagline and len(nxt) > 8 and not re.match(r"^[\d,]+$", nxt) \
                        and "/products/" not in nxt and len(nxt) < 200:
                    tagline = nxt
            results.append({
                "name": name_clean,
                "slug": slug,
                "tagline": tagline,
                "votes": votes,
                "url": f"https://www.producthunt.com/products/{slug}",
            })
        i += 1
    return results


def fetch_ph_leaderboard(limit: int = 20) -> List[dict]:
    """抓取 Product Hunt 昨日每日榜单（按投票数排序）。
    用 Playwright 无头浏览器渲染 leaderboard 页面并提取产品名 / tagline / 投票数；
    若 Playwright 不可用或抓取失败，回退到官方 RSS feed（数据非榜单，仅作降级）。"""
    yesterday = datetime.now(CN_TZ) - timedelta(days=1)
    url = (f"https://www.producthunt.com/leaderboard/daily/"
           f"{yesterday.year}/{yesterday.month}/{yesterday.day}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [PH榜单] playwright 未安装，回退 RSS feed")
        return _ph_rss_fallback(limit)

    items: List[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        args=["--no-sandbox", "--disable-setuid-sandbox"])
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            # 轮询等待内容渲染完成（避开 Cloudflare 挑战页）
            for _ in range(20):
                time.sleep(1.5)
                content = page.content()
                if "Just a moment" not in content and len(content) > 50000:
                    break
            else:
                raise RuntimeError("页面未在预期时间内渲染完成或被 Cloudflare 拦截")

            # 在浏览器端提取榜单数据：遍历所有指向 /products/ 的链接，
            # 向上找最近的列表项容器，从中读取产品名、tagline、投票数
            data = page.evaluate(r"""() => {
                const results = [];
                const seen = new Set();
                const links = document.querySelectorAll('a[href*="/products/"]');
                links.forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\/products\/([a-z0-9-]+)/);
                    if (!m) return;
                    const slug = m[1];
                    if (seen.has(slug)) return;
                    // 向上找包含投票数的祖先容器（限制大小，避免取到整个页面）
                    let container = null;
                    let el = a;
                    for (let i = 0; i < 6 && el.parentElement; i++) {
                        el = el.parentElement;
                        const txt = el.innerText || '';
                        // 容器文本在 30~400 字之间，且包含 vote 关键字，认为是产品卡片
                        if (txt.length > 30 && txt.length < 400 && /vote/i.test(txt)) {
                            container = el;
                            break;
                        }
                    }
                    if (!container) container = a.parentElement?.parentElement;
                    if (!container) return;
                    const text = container.innerText || '';
                    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
                    // 产品名：取链接文本
                    const name = (a.innerText || a.textContent || '').trim();
                    // tagline：容器内非产品名、非纯数字、非 vote 的第一行长文本
                    let tagline = '';
                    for (const line of lines) {
                        if (line !== name && line.length > 8 && !/^\d+$/.test(line)
                            && !/^[\d,]+\s*vote/i.test(line) && line.length < 200) {
                            tagline = line;
                            break;
                        }
                    }
                    // 投票数：匹配 "数字 vote" 格式
                    const voteMatch = text.match(/([\d,]+)\s*vote/i);
                    const votes = voteMatch ? voteMatch[1].replace(/,/g, '') : '';
                    results.push({
                        name: name || slug,
                        slug: slug,
                        tagline: tagline,
                        votes: votes,
                        url: 'https://www.producthunt.com/products/' + slug,
                    });
                    seen.add(slug);
                });
                return results;
            }""")

            if not data:
                # 兜底：从页面全文本中正则解析产品行（产品链接 + 投票数）
                full_text = page.inner_text("body")
                browser.close()
                data = _parse_ph_from_text(full_text)
            else:
                browser.close()

            if not data:
                raise RuntimeError("页面解析未提取到产品数据")

            print(f"  [PH榜单] 原始提取 {len(data)} 条，样例: "
                  + str(data[:3])[:200])

            # 按投票数排序（榜单顺序），取前 limit 条
            def _vote_int(x):
                try:
                    return int(x.get("votes") or 0)
                except (ValueError, TypeError):
                    return 0
            data.sort(key=_vote_int, reverse=True)
            for d in data[:limit]:
                items.append({
                    "title": d["name"],
                    "url": d["url"],
                    "tagline": d["tagline"],
                    "summary": d["tagline"],
                    "source": "Product Hunt",
                    "time": yesterday,
                })
            print(f"  [PH榜单] Playwright 抓取 {len(items)} 条（昨日 {yesterday.strftime('%Y-%m-%d')}）")
    except Exception as exc:
        print(f"  [PH榜单] Playwright 抓取失败: {exc}，回退 RSS feed")
        return _ph_rss_fallback(limit)
    return items


def _ph_rss_fallback(limit: int = 20) -> List[dict]:
    """PH 数据降级方案：从官方 RSS feed 取最新产品（非榜单排序，仅作保底）"""
    ph_items = dedupe_and_sort(
        fetch_rss("https://www.producthunt.com/feed", "Product Hunt", 30, today_only=False), limit)
    for it in ph_items:
        t = it["title"]
        for sep in (": ", " – ", " - "):
            if sep in t:
                name, tagline = t.split(sep, 1)
                it["title"], it["tagline"] = name.strip(), tagline.strip()
                break
        else:
            it["tagline"] = (it["summary"] or "")[:60]
        it["tagline_en"] = it["tagline"]
        it["tagline"] = translate_en2zh(it["tagline"])
    return ph_items


def build_global_data() -> dict:
    """收集海外产品页数据：PH 独立成栏；Steam/GameLook 归游戏类；白鲸/HN/TechCrunch 按规则分类"""
    # PH 榜单：用 Playwright 抓取昨日每日榜单，失败时回退 RSS
    ph_items = fetch_ph_leaderboard(20)
    # 翻译 tagline 为中文（Playwright 路径也需翻译；RSS 回退路径已在 _ph_rss_fallback 内处理）
    for it in ph_items:
        if "tagline_en" not in it and it.get("tagline"):
            it["tagline_en"] = it["tagline"]
            it["tagline"] = translate_en2zh(it["tagline"])
    items = (fetch_rss("https://store.steampowered.com/feeds/newreleases.xml", "Steam 新品", 30)
             + fetch_rss("http://www.gamelook.com.cn/feed", "GameLook", 30)
             + fetch_baijing_home(30)
             + fetch_rss("https://techcrunch.com/feed/", "TechCrunch", 30)
             + fetch_hn(25))
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

def fetch_toutiao_hot() -> List[dict]:
    """今日头条热榜：官方 hot-board 接口返回 JSON，含 Title/Url/热度"""
    try:
        resp = SESSION.get("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
                           timeout=12)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        items = []
        for it in data[:20]:
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


def fetch_weibo_hot() -> List[dict]:
    """微博热搜：优先用第三方聚合 API（oioweb / tenapi），失败则回退到
    weibo.com 访客 Cookie 两步法。本地网络可能拿不到，线上 Actions 通常能成功"""
    # 方案 A：oioweb 聚合 API
    try:
        resp = SESSION.get("https://api.oioweb.cn/api/common/HotList?type=weibo", timeout=10)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            data = resp.json().get("data", [])
            if isinstance(data, list) and data:
                items = [{"title": it.get("title", "").strip(),
                          "url": it.get("url", ""),
                          "hot": it.get("hot", it.get("num", "")),
                          "source": "微博热搜"} for it in data[:20]]
                print(f"  [微博热搜] oioweb 源 {len(items)} 条")
                return items
    except Exception:
        pass
    # 方案 B：tenapi
    try:
        resp = SESSION.get("https://tenapi.cn/v2/weibohot", timeout=10)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            data = resp.json().get("data", [])
            if isinstance(data, list) and data:
                items = [{"title": it.get("name", it.get("title", "")).strip(),
                          "url": it.get("url", ""),
                          "hot": it.get("hot", ""),
                          "source": "微博热搜"} for it in data[:20]]
                print(f"  [微博热搜] tenapi 源 {len(items)} 条")
                return items
    except Exception:
        pass
    # 方案 C：weibo.com 访客 Cookie 两步法
    try:
        SESSION.get("https://weibo.com/", timeout=8)
        resp = SESSION.get("https://weibo.com/ajax/side/hotSearch", timeout=10)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            realtime = resp.json().get("data", {}).get("realtime", [])
            items = [{"title": it.get("word", "").strip(),
                      "url": f"https://s.weibo.com/weibo?q=%23{it.get('word', '')}%23",
                      "hot": it.get("num", ""),
                      "source": "微博热搜"} for it in realtime[:20]]
            print(f"  [微博热搜] weibo 官方源 {len(items)} 条")
            return items
    except Exception as e:
        print(f"  [微博热搜] 全部方案失败（降级）: {e}")
    return []


def fetch_baidu_hot() -> List[dict]:
    """百度热搜：官方 board API 返回 JSON，嵌套结构 cards[0].content[0].content"""
    try:
        resp = SESSION.get("https://top.baidu.com/api/board?platform=wise&tab=realtime", timeout=12)
        resp.raise_for_status()
        content = resp.json()["data"]["cards"][0]["content"][0]["content"]
        items = []
        for it in content[:20]:
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


def fetch_36kr_rss(limit: int = 15) -> List[dict]:
    """36kr 官方 RSS（www.36kr.com/feed），用内置 ElementTree 解析"""
    try:
        resp = SESSION.get("https://www.36kr.com/feed", timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:limit]:
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
        print(f"  [36kr RSS] {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [36kr RSS] 抓取失败: {e}")
        return []


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
            "url": f"https://www.baijing.cn/article/{a.get('id')}.html",
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


def fetch_aibot_news(limit: int = 15) -> List[dict]:
    """ai-bot.cn 每日 AI 新闻页：页面按 .news-date 日期标签分组（工作日更新），
    只抓当天分组；当天无更新（如周六、周日）则回退到最近一个有数据的工作日，
    其他日期一律不取。日期分组在嵌套的 .news-list 中，需递归扁平化处理。"""
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

    # 将流聚合为 [(datetime, [条目元素])]，保持文档倒序
    groups: List[tuple] = []
    cur_date: Optional[datetime] = None
    cur_items: list = []
    for kind, payload in stream:
        if kind == "date":
            if cur_date is not None:
                groups.append((cur_date, cur_items))
            cur_date = _parse_aibot_date(payload, NOW)
            cur_items = []
        elif cur_date is not None:
            cur_items.append(payload)
    if cur_date is not None:
        groups.append((cur_date, cur_items))

    if not groups:
        print("  [ai-bot 新闻] 未解析到任何日期分组")
        return []

    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    # 优先匹配当天分组；没有则取文档中第一个不晚于今天的分组（即最近更新日，如周五）
    target = next((g for g in groups if g[0].date() == today.date()), None)
    if target is None:
        target = next((g for g in groups if g[0] <= today), None)
    if target is None:
        print("  [ai-bot 新闻] 当天及历史分组均未找到")
        return []

    target_date, boxes = target
    # 保护：最近分组距今超过 7 天视为站点长期停更，不展示陈旧数据
    if (today - target_date).days > 7:
        print(f"  [ai-bot 新闻] 最新数据为 {target_date:%m月%d日}，已超过 7 天，跳过")
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
            "time": target_date,
        })
    print(f"  [ai-bot 新闻] {target_date:%m月%d日} 分组 {len(items)} 条")
    return items



def build_hotboard_data(market_headlines: dict = None) -> dict:
    """收集今日热榜页数据：社会舆情（头条/微博/财经要点）、科技动态（36kr/量子位/ai-bot）、
    游戏与产品（GameLook/PH/GitHub Trending），按板块分组返回"""
    # 社会舆情板块：头条 + 微博 + 百度 + 财经要点（国内+海外合并，按评分排序）
    social = {
        "今日头条": fetch_toutiao_hot(),
        "微博热搜": fetch_weibo_hot(),
        "百度热搜": fetch_baidu_hot(),
    }
    if market_headlines:
        headlines = (market_headlines.get("domestic") or []) + (market_headlines.get("global") or [])
        headlines.sort(key=lambda t: (t.get("score", 0), t.get("time")), reverse=True)
        # 统一格式为热榜条目
        social["财经要点"] = [{
            "title": (it.get("title") or it.get("content", "")[:60]).strip(),
            "url": it.get("url", ""),
            "summary": (it.get("content") or "")[:80],
            "source": "财经要点",
        } for it in headlines[:20]]
    # 科技动态板块
    tech = {
        "36kr": fetch_36kr_rss(20),
        "量子位": fetch_rss("https://www.qbitai.com/feed", "量子位", 20),
        "ai-bot": fetch_aibot_news(20),
        "白鲸出海": fetch_baijing_home(20),
    }
    # 游戏与产品板块
    gaming = {
        "GameLook": fetch_rss("http://www.gamelook.com.cn/feed", "GameLook", 20),
        "Product Hunt": build_global_data()["PH精选"],
        "GitHub Trending": fetch_github_trending(25),
    }
    print("  [热榜] 社会舆情:" + " ".join(f"{k}{len(v)}" for k, v in social.items())
          + " | 科技:" + " ".join(f"{k}{len(v)}" for k, v in tech.items())
          + " | 游戏产品:" + " ".join(f"{k}{len(v)}" for k, v in gaming.items()))
    return {"social": social, "tech": tech, "gaming": gaming}
