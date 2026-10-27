# -*- coding: utf-8 -*-
"""spiders.py — 数据抓取层：财经快讯 / 腾讯行情 / RSS / 热榜 / 资讯源"""
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from config import (CN_TZ, NOW, TODAY, TODAY_START, MAX_EVENTS, OUTPUT_HTML,
                    QUOTE_BATCH, KEYWORD_GROUPS, TAG_COLORS,
                    SOURCE_STATS, HIGH_RISK_TAGS, MARKET_CORE_KEYWORDS,
                    MARKET_KEYWORDS, MARKET_GLOBAL_KEYWORDS, BRIEF_GROUP_MAX,
                    HEADLINES_MAX, NAV_ITEMS, WATCHLIST, WATCH_RELATED_MAX,
                    source_rule)
from utils import (SESSION, http_get, log, strip_html, parse_dt, ts_to_dt,
                   ms_to_dt, is_today, filter_by_time_rule,
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


# ============================ RSS 与资讯源 ============================

# RSS 条目统一结构：{title, url, time(北京时间), summary, source}

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
        # —— Atom 格式：entry/title/link@href/published ——
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


_QBITAI_POSTS_API = "https://www.qbitai.com/wp-json/wp/v2/posts"


def _qbitai_plain(value) -> str:
    """WordPress REST 的 title/excerpt 可能是 {rendered: html}，转成纯文本"""
    if isinstance(value, dict):
        value = value.get("rendered") or ""
    return strip_html(value)


def _parse_qbitai_wp_date(value: str) -> Optional[datetime]:
    """解析量子位 REST 的 date。站点时区为北京时间，无偏移的时间按北京时间处理"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(CN_TZ) if dt.tzinfo else dt.replace(tzinfo=CN_TZ)


def _parse_qbitai_reltime(text: str) -> Optional[datetime]:
    """解析首页相对时间：刚刚 / N分钟前 / N小时前 / N天前 / 昨天 HH:MM / 前天 HH:MM / YYYY-MM-DD"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    if t == "刚刚" or re.fullmatch(r"\d+分钟前", t):
        return NOW
    m = re.fullmatch(r"(\d+)小时前", t)
    if m:
        return NOW - timedelta(hours=int(m.group(1)))
    m = re.fullmatch(r"(\d+)天前", t)
    if m:
        return NOW - timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"(昨天|前天) (\d{1,2}):(\d{2})", t)
    if m:
        days = 1 if m.group(1) == "昨天" else 2
        d = (NOW.astimezone(CN_TZ) - timedelta(days=days)).date()
        try:
            return datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)), tzinfo=CN_TZ)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0, tzinfo=CN_TZ)
        except ValueError:
            return None
    return None


def _fetch_qbitai_rest(max_pages: int = 3, per_page: int = 20) -> List[dict]:
    """用 WordPress REST 按发布时间倒序取最近文章。RSS 默认只回约 10 条且会滞后"""
    items: List[dict] = []
    seen = set()
    yesterday = NOW.astimezone(CN_TZ).date() - timedelta(days=1)
    for page in range(1, max_pages + 1):
        resp = SESSION.get(
            _QBITAI_POSTS_API,
            params={
                "per_page": per_page,
                "page": page,
                "orderby": "date",
                "order": "desc",
                "_fields": "date,link,title,excerpt",
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 400:  # 超出总页数
            break
        resp.raise_for_status()
        posts = resp.json()
        if not isinstance(posts, list) or not posts:
            break
        page_dates = []
        for post in posts:
            title = _qbitai_plain(post.get("title"))
            link = (post.get("link") or "").strip()
            dt = _parse_qbitai_wp_date(post.get("date") or "")
            if not title or not link or dt is None or link in seen:
                continue
            seen.add(link)
            page_dates.append(dt.date())
            items.append({
                "title": title,
                "url": link,
                "time": dt,
                "summary": _qbitai_plain(post.get("excerpt"))[:120],
                "source": "量子位",
            })
        # 本页已经全部早于昨天，今天和昨天的稿件都已收齐
        if page_dates and max(page_dates) < yesterday:
            break
    return items


def _fetch_qbitai_home() -> List[dict]:
    """REST 不可用时解析首页列表。span.time 为相对时间，无时间的推荐位跳过"""
    resp = SESSION.get("https://www.qbitai.com/", timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    items: List[dict] = []
    seen = set()
    for box in soup.select(".picture_text"):
        a = box.select_one("h4 a")
        clock = box.select_one("span.time")
        if a is None or clock is None:
            continue
        title = a.get_text(strip=True)
        link = (a.get("href") or "").strip()
        dt = _parse_qbitai_reltime(clock.get_text())
        if not title or not link or dt is None or link in seen:
            continue
        seen.add(link)
        summary_el = box.select_one(".text_box > p")
        summary = summary_el.get_text(" ", strip=True)[:120] if summary_el else ""
        items.append({
            "title": title,
            "url": link,
            "time": dt,
            "summary": summary,
            "source": "量子位",
        })
    return items


def fetch_qbitai(limit: int = 20, time_rule: str = "latest_day") -> List[dict]:
    """抓取量子位资讯。优先 REST，失败再解析首页，最后退回 RSS。
    时间策略见 config.SOURCE_RULES：默认只保留抓到的数据里最新的那一天。"""
    items: List[dict] = []
    try:
        items = _fetch_qbitai_rest()
    except Exception as exc:
        print(f"  [量子位] REST 抓取失败，改走首页: {exc}")
    if not items:
        try:
            items = _fetch_qbitai_home()
        except Exception as exc:
            print(f"  [量子位] 首页抓取失败，改走 RSS: {exc}")
    if not items:
        return fetch_rss("https://www.qbitai.com/feed", "量子位", limit, time_rule)
    items = filter_by_time_rule(items, time_rule, "量子位")[:limit]
    print(f"  [量子位] {len(items)} 条")
    return items


def translate_en2zh(text: str) -> str:
    """英文短句翻译为中文（Google translate_a 公开接口，无需密钥）；失败时降级返回原文。
    gtx 经常被限流，限流或失败时改用 dict-chrome-ex 再试一次。"""
    if not text or not re.search(r"[a-zA-Z]{3,}", text):
        return text  # 无英文内容或空串直接返回
    url = "https://translate.googleapis.com/translate_a/single"
    for client in ("gtx", "dict-chrome-ex"):
        try:
            resp = SESSION.get(url, params={"client": client, "sl": "en", "tl": "zh-CN",
                                            "dt": "t", "q": text}, timeout=10)
            if resp.status_code == 429:
                continue
            resp.raise_for_status()
            data = resp.json()
            # 返回结构 [[["译句","原句",...],...]...]，拼接所有翻译分段
            zh = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
            if zh:
                return zh
        except Exception:
            continue
    return text


def fetch_ph_leaderboard(limit: int = 20) -> List[dict]:
    """抓取 Product Hunt 最新一日的中文每日榜单。
    数据源：开源项目 ViggoZ/producthunt-daily-hot 每日生成的 Markdown。
    榜单一般对应「昨天」；上游约北京时间 15:00 才写出当天文件。
    从今天往前逐日查找，取到的第一个非空文件就是最新榜，避免固定回退到前天。"""
    today = NOW.astimezone(CN_TZ).date()
    date_str = ""
    items: List[dict] = []
    for offset in range(0, 5):
        candidate = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        items = _fetch_ph_markdown(candidate)
        if items:
            date_str = candidate
            if offset >= 2:
                print(f"  [PH榜单] 最近 {offset} 天文件为空，改用 {candidate}")
            break
        print(f"  [PH榜单] {candidate} 尚无数据，继续往前找")
    if items:
        print(f"  [PH榜单] 抓取 {len(items)} 条（{date_str} 榜单），"
              f"榜首: {items[0]['title']}（{items[0].get('votes')} 票）")
    else:
        print("  [PH榜单] 近 5 天均无可用数据")
    return items[:limit]


def _fetch_ph_markdown(date_str: str) -> List[dict]:
    """拉取指定日期的 PH 每日 Markdown 并解析。raw.githubusercontent.com 失败时改走 jsDelivr。"""
    urls = [
        (f"https://raw.githubusercontent.com/ViggoZ/producthunt-daily-hot/main/"
         f"data/producthunt-daily-{date_str}.md"),
        (f"https://cdn.jsdelivr.net/gh/ViggoZ/producthunt-daily-hot@main/"
         f"data/producthunt-daily-{date_str}.md"),
    ]
    for raw_url in urls:
        try:
            resp = SESSION.get(raw_url, timeout=20)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            text = (resp.text or "").strip()
            if not text or text.startswith("404"):
                continue
            parsed = _parse_ph_markdown(text, date_str)
            if parsed:
                return parsed
        except Exception as exc:
            print(f"  [PH榜单] 请求失败 ({date_str}): {exc}")
    return []


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


# 面向个人开发者 / 创意 idea 的子版块；r/iosapps 固定排第一
_REDDIT_SUBS = [
    "iosapps",
    "SideProject",
    "indiehackers",
    "AppIdeas",
    "SomebodyMakeThis",
    "SaaS",
    "AlphaAndBetaUsers",
    "buildinpublic",
]
_REDDIT_MULTI = "+".join(_REDDIT_SUBS)
_REDDIT_HOME = f"https://www.reddit.com/r/{_REDDIT_MULTI}/hot/"
# Reddit 要求独特的 User-Agent，推荐格式：平台:应用名:版本 (by /u/用户名)
_REDDIT_UA = "python:ah-monitor-hotlist:v1.0 (by /u/ah_monitor_reader)"


def _reddit_rss_entries(url: str, retries: int = 3) -> List[dict]:
    """拉取一个 Reddit Atom RSS，返回 {title,url,tagline}；失败返回空列表。
    遇 429 时加长退避再试，保证多版块合集能拿到，而不是静默只剩 iosapps。"""
    resp = None
    for attempt in range(retries):
        try:
            resp = SESSION.get(
                url,
                headers={"User-Agent": _REDDIT_UA,
                         "Accept": "application/atom+xml, application/xml"},
                timeout=20,
            )
            if resp.status_code == 200 and resp.content:
                break
            if resp.status_code == 429 and attempt < retries - 1:
                time.sleep(12 * (attempt + 1))
                continue
            break
        except Exception:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
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
    rows = []
    for entry in root.findall(f"{atom}entry"):
        title = (entry.findtext(f"{atom}title") or "").strip()
        if not title:
            continue
        link_el = entry.find(f"{atom}link")
        link = (link_el.get("href") or "").strip() if link_el is not None else ""
        cat_el = entry.find(f"{atom}category")
        subreddit = (cat_el.get("label") or "").strip() if cat_el is not None else ""
        rows.append({"title": title, "url": link, "tagline": subreddit})
    return rows


def _reddit_is_iosapps(tagline: str) -> bool:
    t = (tagline or "").strip().lower()
    return t in ("r/iosapps", "iosapps")


def fetch_reddit(limit: int = 20) -> List[dict]:
    """Reddit：一次拉取全部目标子版块 hot 合集，再整理展示顺序。
    r/iosapps 固定排在最前；其余 SideProject / indiehackers / AppIdeas 等按版块轮询均分，
    避免单一活跃版块占满。不再先单拉 iosapps 再拉合集（第二次易 429 导致只剩 iosapps）。"""
    fetch_cap = max(50, limit * 3)
    rows = _reddit_rss_entries(
        f"https://www.reddit.com/r/{_REDDIT_MULTI}/hot/.rss?limit={fetch_cap}"
    )
    if not rows:
        print("  [Reddit] 多版块合集失败，模块为空")
        return []

    ios_rows = [r for r in rows if _reddit_is_iosapps(r.get("tagline", ""))]
    other_rows = [r for r in rows if not _reddit_is_iosapps(r.get("tagline", ""))]
    # iosapps 置顶，最多占一半，给其他创意/独立开发版块留位置
    ios_cap = min(len(ios_rows), max(3, limit // 3))
    remain = limit - ios_cap

    # 其余版块按 tagline 分桶，轮询取帖，保证多版块都有曝光
    buckets: Dict[str, List[dict]] = {}
    order_keys: List[str] = []
    for r in other_rows:
        key = (r.get("tagline") or "r/other").strip() or "r/other"
        if key not in buckets:
            buckets[key] = []
            order_keys.append(key)
        buckets[key].append(r)
    # 按配置里的子版块顺序优先轮询（大小写不敏感）
    preferred = []
    for sub in _REDDIT_SUBS[1:]:
        want = f"r/{sub}".lower()
        for k in order_keys:
            if k.lower() == want and k not in preferred:
                preferred.append(k)
    for k in order_keys:
        if k not in preferred:
            preferred.append(k)

    picked_others: List[dict] = []
    idx = {k: 0 for k in preferred}
    while len(picked_others) < remain and preferred:
        progressed = False
        for k in preferred:
            i = idx[k]
            bucket = buckets.get(k) or []
            if i < len(bucket):
                picked_others.append(bucket[i])
                idx[k] = i + 1
                progressed = True
                if len(picked_others) >= remain:
                    break
        if not progressed:
            break

    ordered = ios_rows[:ios_cap] + picked_others
    # 去重
    seen = set()
    deduped = []
    for row in ordered:
        key = row.get("url") or row.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break

    items = []
    for row in deduped:
        zh_title = translate_en2zh(row["title"])
        display = zh_title if zh_title and zh_title != row["title"] else row["title"]
        items.append({
            "title": display,
            "url": row["url"],
            "tagline": row["tagline"] or "r/iosapps",
            "source": "Reddit",
        })
    sub_counts = {}
    for it in items:
        sub_counts[it["tagline"]] = sub_counts.get(it["tagline"], 0) + 1
    detail = " ".join(f"{k}{v}" for k, v in sub_counts.items())
    print(f"  [Reddit] {len(items)} 条（{detail}）")
    return items


def fetch_bbc(limit: int = 20, time_rule: str = "today_yesterday") -> List[dict]:
    """BBC：主站 News RSS + Travel 子频道 RSS，合并进同一板块。
    News 按当天优先；Travel 更新较慢，取近 7 天，副标题标 Travel。
    预留若干名额给 Travel，避免 News 占满后 Travel 进不来；Travel 为空时名额还给 News。"""
    travel_slots = min(6, max(3, limit // 4))
    news = fetch_rss(
        "https://feeds.bbci.co.uk/news/rss.xml", "BBC",
        limit, time_rule,
    )
    travel = fetch_rss(
        "https://www.bbc.com/travel/feed.rss", "BBC Travel",
        travel_slots, "recent_7d",
    )
    for it in travel:
        it["source"] = "BBC"
        it["tagline"] = "Travel"
    if not travel:
        travel_slots = 0
    news_slots = max(1, limit - travel_slots)
    seen = set()
    items: List[dict] = []
    for it in news[:news_slots] + travel[:travel_slots]:
        key = it.get("url") or it.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(it)
        if len(items) >= limit:
            break
    travel_n = sum(1 for it in items if it.get("tagline") == "Travel")
    print(f"  [BBC] 合并 {len(items)} 条（含 Travel {travel_n}）")
    return items


def fetch_gamelook(limit: int = 20) -> List[dict]:
    """GameLook 首页资讯：直接解析官网首页上的轮播与「最新资讯」列表，
    不再走 RSS / latest_day（RSS 常滞后或只有一天一条）。"""
    resp = None
    for attempt in range(3):
        try:
            resp = SESSION.get("http://www.gamelook.com.cn/", timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            break
        except Exception as exc:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            print(f"  [GameLook] 首页抓取失败: {exc}")
            return []
    soup = BeautifulSoup(resp.text, "html.parser")
    items: List[dict] = []
    seen: Dict[str, int] = {}

    def _parse_day(date_text: str) -> Optional[datetime]:
        if not date_text:
            return None
        try:
            return datetime.strptime(date_text.strip()[:10], "%Y-%m-%d").replace(tzinfo=CN_TZ)
        except ValueError:
            return parse_dt(date_text)

    def _push(title: str, href: str, date_text: str = ""):
        title = (title or "").strip()
        href = (href or "").strip()
        if not title or not href:
            return
        if not re.search(r"/\d{4}/\d{2}/\d+/?", href):
            return
        dt = _parse_day(date_text)
        if href in seen:
            # 轮播常无日期；列表里同 URL 时补上发布时间
            idx = seen[href]
            if dt is not None and items[idx].get("time") is None:
                items[idx]["time"] = dt
            return
        seen[href] = len(items)
        items.append({
            "title": title,
            "url": href,
            "summary": "",
            "time": dt,
            "source": "GameLook",
        })

    # 首页轮播在前，再接「更多资讯 / 最新资讯」主列表
    for slide in soup.select(".slide-item"):
        a = (slide.select_one("h3.slide-title a[href]")
             or slide.select_one("a[href]"))
        if a is None:
            continue
        img = slide.select_one("img[alt]")
        title = (a.get_text(strip=True) or a.get("title")
                 or (img.get("alt") if img else "") or "")
        _push(title, a.get("href", ""))
    for box in soup.select(".home-section-center li.item .item-content, .item-content"):
        a = box.select_one("h2 a[href], h3 a[href], a[href]")
        if a is None:
            continue
        date_m = re.search(r"(\d{4}-\d{2}-\d{2})", box.get_text(" ", strip=True))
        _push(a.get_text(strip=True), a.get("href", ""), date_m.group(1) if date_m else "")
        if len(items) >= limit:
            break
    items = items[:limit]
    print(f"  [GameLook] 首页资讯 {len(items)} 条")
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
        x["time"] = x.pop("pub_dt")  # 保留发布时间，供模块右上角显示最新数据时间
    print(f"  [微信公众号] {len(items)} 条（{len(_WECHAT_ACCOUNTS)} 个账号，每账号取最新 1 篇）")
    return items[:limit]


def fetch_github_trending(limit: int = 25) -> List[dict]:
    """GitHub Trending：爬取 trending 页面，提取仓库名/描述/今日 star 数。
    仓库名保持原文；描述用 translate_en2zh 译成中文，失败则保留英文。
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
        if desc:
            desc = translate_en2zh(desc)
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


_BAIJING_TIME_RE = re.compile(
    r'<time class="timeago"[^>]*>\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*</time>'
)


def _baijing_article_time(article_id) -> Optional[datetime]:
    """文章页 timeago 上的绝对发布时间。列表接口只有「N天前」，日期会偏一天"""
    try:
        resp = SESSION.get(f"https://www.baijing.cn/article/{article_id}", timeout=12)
        resp.raise_for_status()
        m = _BAIJING_TIME_RE.search(resp.text)
        return parse_dt(m.group(1)) if m else None
    except Exception:
        return None


def _parse_baijing_reltime(text: str) -> Optional[datetime]:
    """解析白鲸首页接口的相对时间（'刚刚'/'N 分钟前'/'N 小时前'/'N 天前'）
    为北京时间 datetime；无法识别时返回 None。仅在文章页没有绝对时间时兜底"""
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


def fetch_baijing_home(limit: int = 20, max_pages: int = 4,
                       time_rule: str = "latest_day") -> List[dict]:
    """白鲸出海首页文章：调用首页同款 AJAX 接口（POST /index/ajax/get_article/，
    参数 pn 为页码）。列表里的 add_time 只有相对时间，和正文发布时间可能差一天，
    所以按列表顺序打开文章页，读取 timeago 上的绝对时间。
    时间规则见 config.SOURCE_RULES，默认 latest_day：只保留最新那一天。
    文章按时间倒序，读到比最新一天更早的日期即可停止。"""
    headers = {"X-Requested-With": "XMLHttpRequest", "Referer": "https://www.baijing.cn/"}
    seen_ids = set()
    raw: List[dict] = []
    latest_date = None
    stop = False
    try:
        for pn in range(1, max_pages + 1):
            if stop:
                break
            resp = SESSION.post("https://www.baijing.cn/index/ajax/get_article/",
                                data={"pn": pn}, headers=headers, timeout=15)
            resp.raise_for_status()
            arts = (resp.json().get("data") or {}).get("article_list") or []
            if not arts:
                break
            for a in arts:
                aid = a.get("id")
                title = (a.get("title") or "").strip()
                if not aid or aid in seen_ids or not title:
                    continue
                seen_ids.add(aid)
                dt = _baijing_article_time(aid) or _parse_baijing_reltime(a.get("add_time", ""))
                if dt is None:
                    continue
                d = dt.astimezone(CN_TZ).date()
                if latest_date is None or d > latest_date:
                    latest_date = d
                elif d < latest_date:
                    stop = True
                    break
                raw.append({
                    "title": title,
                    # 站内真实路由为 /article/{id}（无 .html 后缀；带后缀会落到"请稍候"错误页）
                    "url": f"https://www.baijing.cn/article/{aid}",
                    "summary": (a.get("synopsis") or "").strip()[:120],
                    "time": dt,
                    "source": "白鲸出海",
                })
                time.sleep(0.1)
            time.sleep(0.2)
    except Exception as e:
        print(f"  [白鲸出海] 首页接口抓取失败: {e}")
        if not raw:
            return []

    items = filter_by_time_rule(raw, time_rule, "白鲸出海")[:limit]
    print(f"  [白鲸出海] {len(items)} 条")
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
    时间规则（见 config.SOURCE_RULES，today_and_yesterday）：同时抓今天与昨天两天分组并合并展示，
    今天在前、昨天在后；两天均无分组（如周末未更新）则返回空，绝不抓更早分组，防止历史数据爆炸。"""
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
    # 合并今天与昨天两天分组：今天在前（更新），昨天在后；其余日期一律不看
    day_groups: List[tuple] = []
    for d in (today, yesterday):
        boxes = groups.get(d.strftime("%Y-%m-%d"))
        if boxes:
            day_groups.append((d, boxes))
    if not day_groups:
        print("  [ai-bot 新闻] 今天与昨天均无分组，模块为空")
        return []

    items: List[dict] = []
    for d, boxes in day_groups:
        chosen_date = datetime(d.year, d.month, d.day, 12, tzinfo=CN_TZ)
        for box in boxes:
            if len(items) >= limit:
                break
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
        if len(items) >= limit:
            break
    today_n = sum(1 for it in items if it["time"].astimezone(CN_TZ).date() == today)
    yest_n = len(items) - today_n
    print(f"  [ai-bot 新闻] 今昨两天合并 {len(items)} 条（今天 {today_n} / 昨天 {yest_n}）")
    return items


_X_TRENDS_URL = "https://getdaytrends.com/"


def fetch_x_trends(limit: int = 20, time_rule: str = "realtime") -> List[dict]:
    """X 全球热点话题。官方接口收费且网页有登录墙，改读公开汇总页 getdaytrends 的全球实时榜。
    时间策略与条数见 config.SOURCE_RULES，默认 realtime：只取构建时刻的榜单快照，不按日期过滤。
    每条话题打开 X 搜索页。抓取失败返回空列表，不影响其他模块。"""
    try:
        resp = SESSION.get(_X_TRENDS_URL, timeout=15,
                           headers={"Accept": "text/html",
                                    "Accept-Language": "en-US,en;q=0.9"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        print(f"  [X热点] 抓取失败: {exc}")
        return []
    items: List[dict] = []
    seen = set()
    # table.trends 是当前全球排名；「最多讨论」表没有 trends 类，不混入
    for table in soup.select("table.trends"):
        for a in table.select("td.main a"):
            title = a.get_text(strip=True)
            if not title or title in seen:
                continue
            seen.add(title)
            items.append({
                "title": title,
                "url": "https://x.com/search?q=" + quote(title) + "&src=typed_query&f=live",
                "summary": "",
                "source": "X热点话题",
                "time": NOW,
            })
    items = filter_by_time_rule(items, time_rule, "X热点话题")[:limit]
    print(f"  [X热点] {len(items)} 条（全球实时榜）")
    return items


# 用来发现「能不能做成工具站」的种子词：上升查询会落到计算器、生成器、转换器这类需求
_TOOL_TREND_SEEDS = [
    "calculator",
    "generator",
    "converter",
    "tracker",
    "template",
    "planner",
    "checker",
    "maker",
]


def _trends_growth_score(formatted: str) -> int:
    """把上升查询的展示值转成排序分。Breakout 最高，其次是增长百分比"""
    text = (formatted or "").strip()
    if text.lower() == "breakout":
        return 10_000_000
    if "%" not in text and not text.startswith("+"):
        return 0
    match = re.search(r"(\d[\d,]*)", text)
    if not match:
        return 0
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return 0


def _parse_rising_keywords(payload: dict, seed: str) -> List[dict]:
    """从 relatedsearches 响应里只取上升查询，丢掉 0–100 的热门查询"""
    lists = (payload.get("default") or {}).get("rankedList") or []
    rows = []
    for bucket in lists:
        for row in bucket.get("rankedKeyword") or []:
            formatted = str(row.get("formattedValue") or "").strip()
            if _trends_growth_score(formatted) <= 0 and formatted.lower() != "breakout":
                continue
            query = (row.get("query") or "").strip()
            if not query:
                continue
            rows.append({
                "query": query,
                "seed": seed,
                "formatted": formatted,
                "score": _trends_growth_score(formatted),
            })
    return rows


def _trends_json(text: str) -> Optional[dict]:
    """去掉 Google 的 )]}', 前缀后再解析 JSON"""
    raw = (text or "").lstrip()
    start = raw.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(raw[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _trends_get(url: str, params: dict, referer: str) -> Optional[requests.Response]:
    """Google Trends 对机房 IP 容易 429，失败后退避再试两次"""
    for attempt in range(3):
        try:
            resp = SESSION.get(
                url, params=params, timeout=20,
                headers={
                    "Referer": referer,
                    "Accept": "application/json, text/plain, */*",
                },
            )
        except Exception:
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            return None
        if resp.status_code == 200 and resp.text and "Error 429" not in resp.text[:240]:
            return resp
        if attempt < 2 and resp.status_code in (429, 500, 502, 503):
            time.sleep(8 * (attempt + 1))
            continue
        return None
    return None


def _fetch_seed_rising(seed: str) -> List[dict]:
    """一个种子词在美国、最近 30 天、网页搜索下的上升查询"""
    explore_req = {
        "comparisonItem": [{"keyword": seed, "geo": "US", "time": "today 1-m"}],
        "category": 0,
        "property": "",
    }
    referer = ("https://trends.google.com/trends/explore?q="
               + quote(seed) + "&geo=US&date=today%201-m")
    resp = _trends_get(
        "https://trends.google.com/trends/api/explore",
        {"hl": "en-US", "tz": "360",
         "req": json.dumps(explore_req, separators=(",", ":"))},
        referer,
    )
    if resp is None:
        return []
    data = _trends_json(resp.text)
    widget = None
    for item in (data or {}).get("widgets") or []:
        if "RELATED_QUERIES" in (item.get("id") or ""):
            widget = item
            break
    if not widget or not widget.get("token") or not widget.get("request"):
        return []
    time.sleep(1.2)
    related = _trends_get(
        "https://trends.google.com/trends/api/widgetdata/relatedsearches",
        {"hl": "en-US", "tz": "360",
         "req": json.dumps(widget["request"], separators=(",", ":")),
         "token": widget["token"]},
        referer,
    )
    if related is None:
        return []
    payload = _trends_json(related.text)
    if not payload:
        return []
    return _parse_rising_keywords(payload, seed)


# 查询里要出现这类词，才像「提供一项功能的工具站」，而不是新闻或评测
_TOOL_QUERY_TOKENS = (
    "calculator", "generator", "converter", "tracker", "template", "planner",
    "checker", "maker", "builder", "creator", "translator", "editor",
    "compressor", "designer", "scheduler", "formatter",
)


def _is_tool_query(query: str) -> bool:
    text = (query or "").casefold()
    return any(token in text for token in _TOOL_QUERY_TOKENS)


def _select_tool_trends(rows: List[dict], limit: int) -> List[dict]:
    """去掉非工具查询，同一关键词留增长更高的一条，再按种子词轮询，避免一个词占满"""
    merged: Dict[str, dict] = {}
    for row in rows:
        if not _is_tool_query(row.get("query", "")):
            continue
        key = row["query"].casefold()
        prev = merged.get(key)
        if prev is None or row["score"] > prev["score"]:
            merged[key] = row
    buckets: Dict[str, List[dict]] = {}
    for seed in _TOOL_TREND_SEEDS:
        picked = [row for row in merged.values() if row["seed"] == seed]
        picked.sort(key=lambda row: row["score"], reverse=True)
        if picked:
            buckets[seed] = picked
    selected: List[dict] = []
    while len(selected) < limit and buckets:
        for seed in list(_TOOL_TREND_SEEDS):
            bucket = buckets.get(seed)
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            if not bucket:
                buckets.pop(seed, None)
            if len(selected) >= limit:
                break
    return selected


def fetch_tool_trends(limit: int = 20) -> List[dict]:
    """社会舆情「工具热词」：抓工具型种子词的 Google Trends 上升查询。
    只保留查询里带有工具词的结果，供判断能不能做成提供服务的工具站。
    抓取失败返回空列表。"""
    collected: List[dict] = []
    try:
        SESSION.get("https://trends.google.com/trends/", timeout=15)
    except Exception:
        pass
    for index, seed in enumerate(_TOOL_TREND_SEEDS):
        if index:
            time.sleep(2.5)
        try:
            rows = _fetch_seed_rising(seed)
        except Exception as exc:
            print(f"  [工具热词] {seed} 抓取失败: {exc}")
            continue
        print(f"  [工具热词] {seed} 上升查询 {len(rows)} 条")
        collected.extend(rows)
    selected = _select_tool_trends(collected, limit)
    items = []
    for row in selected:
        label = row["formatted"] or "Rising"
        items.append({
            "title": row["query"],
            "url": "https://www.google.com/search?q=" + quote(row["query"]),
            "tagline": f"{label} · {row['seed']}",
            "summary": "",
            "source": "工具热词",
        })
    print(f"  [工具热词] {len(items)} 条")
    return items


def build_hotboard_data(market_headlines: dict = None) -> dict:
    """收集今日热榜页数据：社会舆情（含 X 热点话题）、科技动态、游戏与产品，按板块分组返回。
    每个源的时间策略与条数统一从 config.SOURCE_RULES 读取，互不影响"""
    # 社会舆情：头条/微博/百度实时榜 + Reddit 独立开发版块 + BBC(News+Travel) + 财经要点
    social = {
        "今日头条": fetch_toutiao_hot(source_rule("今日头条", "limit", 20)),
        "微博热搜": fetch_weibo_hot(source_rule("微博热搜", "limit", 20)),
        "百度热搜": fetch_baidu_hot(source_rule("百度热搜", "limit", 20)),
        "Reddit": fetch_reddit(source_rule("Reddit", "limit", 20)),
        "BBC": fetch_bbc(source_rule("BBC", "limit", 20),
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
    # X 热点是社会舆情里的独立模块，规则不跟其他源共用
    social["X热点话题"] = fetch_x_trends(
        source_rule("X热点话题", "limit", 20),
        source_rule("X热点话题", "time_rule", "realtime"))
    social["工具热词"] = fetch_tool_trends(source_rule("工具热词", "limit", 20))
    # 科技动态板块
    tech = {
        "36kr": fetch_36kr_rss(source_rule("36kr", "limit", 20),
                               source_rule("36kr", "time_rule", "today_yesterday")),
        "量子位": fetch_qbitai(source_rule("量子位", "limit", 20),
                           source_rule("量子位", "time_rule", "latest_day")),
        "a16z": fetch_a16z(source_rule("a16z", "limit", 20),
                           source_rule("a16z", "time_rule", "today_yesterday")),
        "ai-bot": fetch_aibot_news(source_rule("ai-bot", "limit", 20)),
        "白鲸出海": fetch_baijing_home(
            source_rule("白鲸出海", "limit", 20),
            time_rule=source_rule("白鲸出海", "time_rule", "latest_day")),
        "微信公众号": fetch_wechat(source_rule("微信公众号", "limit", 15)),
    }
    # 游戏与产品板块
    gaming = {
        "GameLook": fetch_gamelook(source_rule("GameLook", "limit", 20)),
        "Product Hunt": fetch_ph_leaderboard(source_rule("Product Hunt", "limit", 20)),
        "GitHub Trending": fetch_github_trending(source_rule("GitHub Trending", "limit", 25)),
    }
    print("  [热榜] 社会舆情:" + " ".join(f"{k}{len(v)}" for k, v in social.items())
          + " | 科技:" + " ".join(f"{k}{len(v)}" for k, v in tech.items())
          + " | 游戏产品:" + " ".join(f"{k}{len(v)}" for k, v in gaming.items()))
    return {"social": social, "tech": tech, "gaming": gaming}
