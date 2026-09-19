# -*- coding: utf-8 -*-
"""多页看板生成器（入口）

页面结构：
    index.html    今日热榜（社会舆情 / 科技动态 / 游戏与产品）
    finance.html  财经与股市（A/港股舆情监控）

模块结构：
    config.py     全部配置与词库
    utils.py      基础工具：会话 / 日志 / 时间 / 文本
    spiders.py    数据抓取：财经快讯 / 行情 / RSS / 热榜 / GitHub
    processor.py  数据加工：标的提取 / 负面匹配 / 事件组装
    render.py     页面渲染
    crawler.py    入口编排（本文件）

依赖：requests, beautifulsoup4（见 requirements.txt）
"""
import sys

from config import NOW
from utils import log
from spiders import collect_all_news, build_hotboard_data
from processor import build_events, build_market_headlines
from render import render_finance_page, render_hotboard_page


# ============================ 入口 ============================

def main() -> int:
    """主流程：抓取 → 组装 → 渲染 → 写文件（各页独立 try/except，单页失败不影响其余）"""
    log(f"开始构建看板，北京时间 {NOW:%Y-%m-%d %H:%M:%S}")

    # —— 1. 财经与股市页（原舆情监控）——
    news_rows = []
    events = []
    market_headlines = {"domestic": [], "global": []}
    try:
        news_rows = collect_all_news()
        events = build_events(news_rows)
        market_headlines = build_market_headlines(news_rows, events)
        a_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "A股"}
        hk_codes = {e["stock"]["display"] for e in events if e["stock"] and e["stock"]["market"] == "港股"}
        log(f"[财经] 事件 {len(events)} 条 | A股标的 {len(a_codes)} 个 | 港股标的 {len(hk_codes)} 个")
        page = render_finance_page(events, news_rows)
        with open("finance.html", "w", encoding="utf-8") as f:
            f.write(page)
        log(f"财经与股市页已写入 finance.html（{len(page) / 1024:.1f} KB）")
    except Exception as exc:
        log(f"财经与股市页生成失败: {exc}")

    # —— 2. 今日热榜页（首页，含财经要点模块）——
    try:
        hot_data = build_hotboard_data(market_headlines)
        page = render_hotboard_page(hot_data)
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(page)
        log(f"今日热榜页已写入 index.html（{len(page) / 1024:.1f} KB）")
    except Exception as exc:
        log(f"今日热榜页生成失败: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
