# TVBox 每日源爬虫

每天自动抓取公网上公开的 TVBox 订阅源，合并去重后生成**你自己的每日汇总配置**，推回本仓库。
你只需要把 `merged.json` 的 raw 链接填进 TVBox/影视仓 即可，源每天自动更新。

## 目录结构

```
tvbox-daily/
├── fetch_sources.py                 # 主爬虫（Python 3，仅标准库，零依赖）
├── live_official.py                 # 官方直播源采集器（咪咕/央视/CGTN/移动IPTV 正版直链）
├── sources.txt                      # 种子列表：索引 + 可追加的配置源
├── blocklist.txt                    # 广告/赌博域名黑名单（并入 merged.json 的 ads）
├── quality.json                     # 源质量历史（每日 ok/fail 计数，驱动自动淘汰）
├── excluded_sources.txt             # 被自动淘汰的源（连续失败达阈值，7 天自动复活探测）
├── merged.json                      # 产物①：合并去重后的订阅配置（每日更新）
├── m.json                           # 产物①短名副本（供短订阅地址使用）
├── live_official.m3u                # 产物②：官方直播源 m3u 列表（央视/卫视/地方）
├── status.json                      # 产物③：每个来源的抓取状态/统计
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

## 官方直播源（咪咕/央视/CGTN/移动 IPTV 等广电正版）

`live_official.py` 每天抓取公开维护的**官方直链列表**（`miguvideo.com` / `cctv.com` / `cgtn.com` / `chinamobile.com` 等广电正版域名），解析 → 官方域名过滤 → 频道名归一化去重 → 输出：

- `live_official.m3u`：m3u 格式（央视频道/卫视/新闻/体育分组），可直接导入任意播放器；
- `live_official.json`：含 **group / source / tier** 分级字段，并入 `merged.json` 的 `lives`（主爬虫带 `--official-live` 自动并入，默认 Actions 已开启）。

```bash
python live_official.py                    # 只生成 live_official.m3u + live_official.json
python fetch_sources.py --official-live    # 主爬虫 + 并入官方直播源
```

**分级（tier）**：

| tier | 含义 | 说明 |
|---|---|---|
| 1 | 公网直连官方源 | 咪咕（miguvideo/cmvideo）、央视（cctv/cntv）、CGTN，家庭宽带一般直接可播 |
| 2 | 网络受限官方源 | 中国移动 IPTV（dbiptv.\*.chinamobile.com），通常需移动宽带网络 |

**家庭宽带自测方法**（官方源对服务器/云 IP 一律 403/404 防盗链，只有你的家庭网络才准）：

1. **命令行速测**（Windows PowerShell 或 CMD，5 秒看结果）：
   ```bat
   curl -m 8 -A "Mozilla/5.0" "https://live-play.cctvnews.cctv.com/cctv/merge_cctv13.m3u8"
   ```
   返回内容以 `#EXTM3U` 开头 = 可播；`403/404` = 该源在你的网络不可用。
2. **播放器实测**（推荐）：TVBox → 直播 → 输入 `live_official.m3u` 的订阅地址，逐个频道点开验证；不可播的频道看 `live_official.json` 里它的 `tier` 与 `source`，反馈给我后我会从对应数据源剔除或换源。
3. **批量自测**（可选）：把 `live_official.m3u` 拖进 VLC / PotPlayer，播放列表自动加载后逐台验证。

- **探测策略**：官方域名源**跳过服务器探测、全部保留**（官方源对数据中心/海外 IP 一律 403/404 防盗链，服务器探测只会误杀，交由你的播放器实测）；非官方源仍按 404 剔除。
- **数据源**：`OFFICIAL_SOURCES` 目前 4 个——rm_dream（咪咕 120）、F-zyoom（咪咕 132）、ioptu（咪咕 m3u 99）、baocaien（央视/CGTN/移动IPTV 498）；追加 `名称,URL` 一行即可扩容。

## 源质量自动淘汰（保留高质量、剔除低质量）

`fetch_sources.py` 每天把每个种子的抓取结果记入 `quality.json`（按自然日累计 ok/fail），自动维护：

| 规则 | 行为 |
|---|---|
| 连续失败 ≥ 3 天 | 自动移入 `excluded_sources.txt`，次日开始跳过不抓（避免拖慢整体抓取） |
| 被淘汰源满 7 天 | 自动"复活探测"一次：成功 → 自动移出淘汰名单；失败 → 重新计时 7 天 |
| 你在 `sources.txt` 删掉的源 | 同步清出淘汰名单 |

- 查看当前淘汰名单：打开 `excluded_sources.txt`（每行 `URL  # 排除于 日期`）。
- **手动恢复**：删掉对应行即可，下次运行会重新纳入。
- 阈值可调：`--fail-limit 3`（连续失败天数）、`--retry-days 7`（复活周期）；`status.json → summary.quality` 记录本次淘汰/复活统计。

## 秒播/4K 站点 jar 恢复（自定义类站点）

部分站点（蜗牛4K、盘迷4K、逸动4K、韩剧/瓜子/独播/文才/小枫/贱片/师兄/伊影等"秒播"站）依赖源作者自定义 jar 提供的爬虫类（`csp_Wex*`/`csp_Ai*`/`csp_SheQu*` 等）。这些 jar 常伪装成 `.jpg/.png` 扩展名以躲避封锁，但本身是可达的 dex jar。

合并时自动做两件事：

1. **顶层 spider 自动恢复**：自动挑选"绝对可达 + 覆盖站点最多"的源 jar 作为顶层 `spider`（TVBox 原版靠顶层加载自定义类；置空会杀死全部 jar 类站点）。作者删除 jar 后会自动切换到下一个可用候选，不会报 `jar load err`（只有不可达地址才触发该报错）。
2. **站点级 jar 注入**：源配置的 `spider` 为绝对 URL 时，同时注入到该源所有无 `jar` 字段的站点（保留 `;md5;` 校验），兼容支持站点级 jar 的播放器（影视等）。

> `--spider <URL>` 可手动指定顶层 jar；`--no-inject-jar` 关闭站点级注入。

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
