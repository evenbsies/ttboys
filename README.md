# TVBox 每日源爬虫

每天自动抓取公网上公开的 TVBox 订阅源，合并去重后生成**你自己的每日汇总配置**，推回本仓库。
你只需要把 `merged.json` 的 raw 链接填进 TVBox/影视仓 即可，源每天自动更新。

## 目录结构

```
tvbox-daily/
├── fetch_sources.py                 # 主爬虫（Python 3，仅标准库，零依赖）
├── sources.txt                      # 种子列表：索引 + 可追加的配置源
├── blocklist.txt                    # 广告/赌博域名黑名单（并入 merged.json 的 ads）
├── merged.json                      # 产物①：合并去重后的订阅配置（每日更新）
├── status.json                      # 产物②：每个来源的抓取状态/统计
├── 反赌博广告手册.md                 # 识别/拦截赌博引流源的操作手册
└── .github/workflows/daily.yml      # 定时任务：每天 UTC 22:00 跑 + 可手动触发
```

## 工作原理

```
sources.txt 种子（索引 + 精选配置）
      │  每日定时(GitHub Actions)
      ▼
抓取每个 URL ──► 是配置(sites/lives) ──► 直接合并
      │
      └─► 是索引(源地址列表) ──► 递归展开到第 2 层 ──► 逐个抓取合并
      │
      ▼
按 api 去重 sites / 按 url 去重 parses·lives·doh / 按 name 去重 rules·headers
      │
      ▼
merged.json（可直接订阅） + status.json（来源健康报告）
```

- **去重规则**：sites 按 `api` 去重（同一接口只保留一条）；不同源同名 key 自动加 `_2`、`_3` 后缀，避免互相覆盖。
- **容量控制**：每个配置源取前 120 个站点，合并后默认上限 500（`--max-sites` 可调），保证配置轻量。
- **容错**：失效的源自动跳过，不会中断整体抓取，失败明细写入 `status.json`。
- **种子组成**：`sources.txt` 含 hkuc 公开配置索引（高天流云/老白/heroaku 等）+ 聚玩盒子（juwanhezi.com）单仓源精选（心魔在线/小马/牛二/嗷呜/宝盒备用等，已实测存活并标注站点数）。

## 部署步骤（约 5 分钟）

1. **建仓库**：GitHub 上新建一个空仓库（如 `tvbox-daily`，Public 即可）。
2. **上传本目录全部文件**：`fetch_sources.py`、`sources.txt`、`.github/workflows/daily.yml`（注意保留 `.github/workflows` 目录结构）。
3. **开启 Actions**：仓库 → Actions 页，首次需要点「I understand my workflows, go ahead and enable them」启用。
4. **立即跑一次**：Actions → daily-fetch → Run workflow（手动触发），等 1~2 分钟跑完，仓库根目录会出现 `merged.json`。
5. **订阅链接**（二选一）：
   - 官方 raw：`https://raw.githubusercontent.com/你的用户名/tvbox-daily/main/merged.json`
   - 国内加速（可选）：`https://cdn.jsdelivr.net/gh/你的用户名/tvbox-daily@main/merged.json`
6. **填进播放器**：TVBox/影视仓/OK影视 → 设置 → 配置地址 → 粘贴上面的链接 → 确定导入。

之后每天北京时间 06:00 自动更新；也可以在 Actions 页面随时手动触发。

## 本地运行

```bash
python fetch_sources.py                # 生成 merged.json + status.json
python fetch_sources.py --max-sites 300
python fetch_sources.py --timeout 12
python fetch_sources.py --mirror "https://ghfast.top/https://raw.githubusercontent.com/"
```

- **网络**：脚本内置 GitHub raw 加速镜像自动回退（ghfast.top / gh-proxy.com / gh.ddlc.top），国内本地直接跑即可；GitHub Actions 上直连优先，无需配置。
- **镜像自选**：`--mirror` 传逗号分隔的前缀列表可覆盖内置镜像。

## 维护你自己的源

- **加源**：在 `sources.txt` 追加一行配置源 URL（或加一个索引 URL 批量引入）。
- **去源**：删掉对应行即可。
- **拦截赌博/广告域名**：在 `blocklist.txt` 每行记一个域名，运行后自动并入 `merged.json` 的 `ads` 字段（播放器直接拦截）。识别与排查方法见 `反赌博广告手册.md`。
- **裁剪**：在 `fetch_sources.py` 的 `merge_configs` 里按需过滤，或在生成后手动编辑 `merged.json`。

## 免责声明

- 本脚本仅做**结构化抓取与合并**，不下载、不存储任何影视内容，不执行任何源内代码。
- 所有片源来自第三方公开配置，内容的版权归原权利人所有；请仅用于个人学习与测试，遵守当地法律法规。
- 第三方源随时可能失效，`status.json` 会如实记录失败项；公开仓库请勿存放任何个人账号/Token。
