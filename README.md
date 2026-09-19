# A/港股 舆情突发与伪负面监控看板

全免费、无服务器（Serverless）的自动化舆情监控看板。通过 GitHub Actions 每天定时抓取 A 股和港股相关的财经快讯，提取"上市公司 + 负面/突发词汇"并匹配实时股价，自动生成静态看板页面发布至 GitHub Pages。

**技术栈**：Python 3 (Requests / BeautifulSoup / Re) + GitHub Actions + Tailwind CSS CDN
**运营成本**：0 元（零服务器、零数据库、零 Token 费用）

## 数据源

| 数据源 | 类型 | 说明 |
|--------|------|------|
| 东方财富 | 7×24 快讯 JSON | 自带结构化股票代码 |
| 证券时报 | 快讯 JSON 接口 | 人民财讯等电头 |
| 新浪财经 | 7×24 直播 + 滚动新闻 | 自带结构化股票代码 |
| 华尔街见闻 | 全球直播 API | 宏观/海外快讯 |
| 金十数据 | flash_newest.js | 快讯聚合 |
| 每日经济新闻 | 宏观/热点公司栏目 | 公司深度报道 |

任一数据源失败不影响整体构建（逐源降级容错）。

## 功能特性

- **负面/突发词库命中**：7 大分组（监管调查 / 诉讼纠纷 / 传闻辟谣 / 举报公关 / 经营风险 / 高管异动 / 股价异动），命中即打标签
- **标的识别双通道**：优先使用数据源自带的结构化股票代码（更准），正文正则匹配兜底（A股 6 位号段 / 港股 5 位补零 / 括号代码 / .SH .SZ .BJ .HK .NQ 后缀）；识别不到不臆造
- **行情叠加**：腾讯财经免费接口（`qt.gtimg.cn`）批量拉取现价与涨跌幅，跌幅超 -3% 红色加粗高亮
- **看板交互**：市场筛选（A股/港股/未识别）、关键词标签多选、全文搜索，响应式支持手机端
- **当日数据**：只保留北京时间当天快讯，运行内 MD5 去重（标题+标的），时间倒序，最多 100 条

## 本地运行

```bash
pip install -r requirements.txt
python crawler.py
# 生成 index.html，浏览器直接打开即可
```

## 部署到 GitHub Pages

1. 新建 GitHub 仓库，推送本目录全部文件：

   ```bash
   git init && git add . && git commit -m "init: 舆情监控看板"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

2. 仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**
3. 等待 Actions 运行完成（可手动触发：**Actions → Deploy Dashboard → Run workflow**）
4. 访问 `https://<你的用户名>.github.io/<仓库名>/`

## 定时说明

- 工作流每天 **UTC 01:30 / 10:00（北京时间 09:30 / 18:00）各一次** 自动运行（`.github/workflows/deploy.yml`）
- GitHub Actions 的 cron 有 0~30 分钟的正常调度延迟，属正常现象
- 手动触发不受时间限制，随时可刷新当日数据

## 免责声明

本看板由关键词规则自动生成，内容版权归原作者所有；"伪负面"指传闻、辟谣、澄清等未经证实或已被否认的信息，页面不对信息真实性负责，不构成任何投资建议。

## 目录结构

```text
├── .github/workflows/deploy.yml   # GitHub Actions 定时构建 + Pages 部署
├── crawler.py                     # 入口编排：抓取 → 加工 → 渲染 → 写文件
├── config.py                      # 全部配置与词库（调整词库/自选标的只动它）
├── utils.py                       # 基础工具：会话 / 日志 / 时间 / 文本处理
├── spiders.py                     # 数据抓取：财经快讯 / 行情 / RSS(Atom) / HN / ai-bot
├── processor.py                   # 数据加工：标的提取 / 负面匹配 / 事件组装 / 速览计算
├── render.py                      # 页面渲染：index.html / finance.html / global.html
├── index.html                     # 生成产物：今日热榜（每次构建覆盖）
├── finance.html                   # 生成产物：A/港股财经舆情页
├── global.html                    # 生成产物：海外产品页
├── requirements.txt               # Python 依赖
└── README.md
```

## 自定义

- **调整词库**：编辑 `config.py` 中的 `KEYWORD_GROUPS`（负面词）/ `MARKET_*`（要闻词）/ `AI_*`（AI 词）
- **增删自选关注标的**：编辑 `config.py` 中的 `WATCHLIST` 列表
- **调整条数上限**：`config.py` 中的 `MAX_EVENTS`（默认 100）
- **增删数据源**：在 `spiders.py` 增加抓取函数并在 `build_hotboard_data` / `build_global_data` / 财经采集流程中注册
- **调整运行时间**：修改 `deploy.yml` 中的 cron 表达式（注意填 UTC 时间）
