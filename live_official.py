# -*- coding: utf-8 -*-
"""官方直播源采集器（咪咕 / 央视 / CGTN / 移动 IPTV 等广电正版源）

每天抓取多个公开维护的"官方直链"列表，解析 -> 过滤官方域名 -> 归一化频道名 ->
按 URL 去重 -> 输出分级结果：
  - live_official.m3u    （m3u 格式，group-title 按频道分组，可直接导入播放器）
  - live_official.json   （TVBox lives 格式，含 group / source / tier 分级字段，
                          供 merged.json 并入）

分级说明（tier）：
  1 = 公网直连官方源（咪咕 miguvideo/cmvideo、央视 cctv/cntv、CGTN）
  2 = 网络受限官方源（中国移动 IPTV dbiptv.*.chinamobile.com，通常需移动宽带）

用法:
  python live_official.py            # 只生成 live_official.m3u / live_official.json
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 10

# 官方直播源数据源（每行一个：名称,URL；均为公开维护的"官方直链"列表）
OFFICIAL_SOURCES = [
    "rm_dream,https://gitea.com/rm_dream/migu_video/raw/branch/main/interface.txt",
    "F-zyoom,https://gitee.com/F-zyoom/iptv/raw/main/%E5%92%AA%E5%92%95.txt",
    "ioptu,https://raw.githubusercontent.com/ioptu/migu_video/refs/heads/main/migu.m3u",
    "baocaien,https://gitee.com/baocaien/tvbox/raw/master/README.txt",
]

# GitHub raw 国内镜像（ioptu 源在 raw.githubusercontent.com，本地抓取时自动回退）
GH_MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://gh.ddlc.top/",
]

# 只保留这些官方域名（广电正版授权流），其余一律丢弃
OFFICIAL_HOST_RULES = [
    "miguvideo.com",        # 咪咕（中国移动）官方 HLS/PLTV
    "cmvideo.cn",           # 中国移动视频
    "chinamobile.com",      # 移动 IPTV PLTV（dbiptv.*.chinamobile.com）
    "cctv.com",             # 央视官方（live-play.cctvnews.cctv.com 等）
    "cntv",                 # 央视官方 CDN（hls.cntv.myhwcdn.cn 等）
    "cgtn.com",             # 中国国际电视台官方（live.cgtn.com 等）
]

# 卫视名单（用于分组）
SAT_NAMES = "湖南|浙江|江苏|东方|北京|广东|深圳|山东|安徽|四川|湖北|天津|重庆|辽宁|江西|黑龙江|河北|河南|陕西|山西|福建|广西|云南|贵州|吉林|内蒙古|新疆|西藏|甘肃|青海|宁夏|海南|厦门|大连|青岛|宁波|东南"


def grab(url):
    """抓取文本；raw.githubusercontent.com 失败时自动回退国内镜像。"""
    urls = [url]
    if "raw.githubusercontent.com/" in url:
        tail = url[url.index("raw.githubusercontent.com/"):]
        for m in GH_MIRRORS:
            urls.append(m + tail)
    last_exc = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last_exc = e
    raise last_exc


def parse_list(text):
    """解析直播列表。支持两种格式：
       A) txt 逗号格式：'频道名,url[#url2#url3...]'（# 分隔多备用地址，逐条展开）
       B) m3u 格式：'#EXTINF:-1 ... ,频道名' 换行 'url'
       #genre# 分组行与 # 注释行自动跳过。
    """
    entries = []
    pending_name = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            # m3u 频道行：取最后一个逗号后的名字
            name = line.rsplit(",", 1)[-1].strip()
            if name:
                pending_name = name
            continue
        if line.startswith("#"):
            continue
        if line.startswith("http"):
            # m3u 的 URL 行，或"url"单条
            for u in line.split()[0].split("#"):
                u = u.strip()
                if u.startswith("http"):
                    entries.append((pending_name or "", u))
            pending_name = None
            continue
        # 逗号格式：名字,url#url2...
        parts = line.split(",", 1)
        if len(parts) >= 2:
            name = parts[0].strip()
            for u in parts[1].split("#"):
                u = u.strip()
                if u.startswith("http"):
                    entries.append((name, u))
    return entries


def is_official(url):
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return False
    return any(rule in host for rule in OFFICIAL_HOST_RULES)


def tier_of(url):
    """分级：1=公网直连官方源，2=网络受限官方源（移动 IPTV）。"""
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return "2"
    if "chinamobile.com" in host:
        return "2"
    return "1"


def norm_name(name):
    """归一化频道名：去掉码率/清晰度/综合/高清等后缀，用于去重与分组。"""
    n = name.strip()
    n = re.sub(r"\d+(M|K)\d+", "", n)          # 38M2160 / 4K
    n = re.sub(r"\b(4K|1080|720|576|480|SD|HD)\b", "", n, flags=re.I)
    n = n.replace("综合", "").replace("高清", "").replace("标清", "").replace("直播", "")
    n = re.sub(r"\s+", "", n)
    return n


def group_of(name):
    if "CCTV" in name.upper() or "中央" in name:
        return "央视频道"
    if re.search(SAT_NAMES, name):
        return "卫视"
    if re.search("体育|足球|篮球|网球|高尔夫", name):
        return "体育"
    if re.search("新闻|NEWS", name, re.I):
        return "新闻"
    return "其他"


def probe(url):
    """探测 m3u8 直链。返回三态：
       True    可播（返回 HLS 内容）
       "lock"  源存在但 IP 受限（403 等，家庭网络可能可播，保留）
       False   确定死亡（404/410/DNS 失败等，剔除）
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=7) as r:
            head = r.read(1024)
            ct = r.headers.get("Content-Type", "").lower()
            if b"#EXTM3U" in head:
                return True
            if "mpegurl" in ct or "mpeg-url" in ct:
                return True
            if head[:1] == b"{" or b"<html" in head[:200].lower():
                return False
            return len(head) > 50
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False
        return "lock"   # 403 等：IP 受限，数据中心/云 IP 常被官方源拒绝，家庭宽带可播
    except Exception:
        return False


def main():
    raw_entries = []      # (source_name, channel_name, url)
    for src in OFFICIAL_SOURCES:
        name, url = src.split(",", 1)
        try:
            txt = grab(url)
        except Exception as e:
            print("[FAIL] %s %s" % (name, str(e)[:60]))
            continue
        es = [(name, n, u) for n, u in parse_list(txt)]
        raw_entries += es
        print("[OK  ] %-10s 原始频道 %d" % (name, len(es)))
        sys.stdout.flush()

    # 1) 只留官方域名
    official = [e for e in raw_entries if is_official(e[2])]
    print("官方域名过滤后:", len(official), "/", len(raw_entries))

    # 2) 按 url 去重
    seen_url = set()
    uniq = []
    for src, name, url in official:
        if url in seen_url:
            continue
        seen_url.add(url)
        uniq.append((src, name, url))
    print("URL 去重后:", len(uniq))

    # 3) 官方域名源跳过服务器探测、全部保留：
    #    官方直链对数据中心/海外 IP 一律 403 或 404（防盗链），只有家庭宽带才准；
    #    服务器端探测只会误杀，所以对官方域名不做剔除，交由用户播放器实测。
    alive = {}
    locked = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(probe, url): (src, name, url) for src, name, url in uniq}
        for fut in as_completed(futs):
            src, name, url = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = False
            # 官方域名：无论探测结果如何都保留（探测仅作展示统计，不剔除）
            key = norm_name(name)
            if key not in alive:
                alive[key] = (src, name, url)
    print("官方频道（全部保留）:", len(alive))

    # 4) 输出：m3u（按分组排序）+ json（含 group/source/tier 分级字段）
    groups = OrderedDict()
    for key, (src, name, url) in sorted(alive.items(), key=lambda x: x[1][1]):
        g = group_of(name)
        groups.setdefault(g, []).append((src, name, url))

    m3u = ["#EXTM3U"]
    for g, items in groups.items():
        for src, name, url in items:
            m3u.append('#EXTINF:-1 group-title="%s",%s' % (g, name))
            m3u.append(url)
    with open("live_official.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")

    lives = [{"name": n, "url": u, "type": 0,
              "group": g, "source": s, "tier": tier_of(u)}
             for g, items in groups.items() for (s, n, u) in items]
    with open("live_official.json", "w", encoding="utf-8") as f:
        json.dump(lives, f, ensure_ascii=False, indent=1)

    print("== 分组统计 ==")
    for g, items in groups.items():
        print("  %-6s %d" % (g, len(items)))
    tiers = {"tier1 公网直连": 0, "tier2 网络受限": 0}
    for lv in lives:
        tiers["tier1 公网直连" if lv["tier"] == "1" else "tier2 网络受限"] += 1
    print("== 分级统计 ==")
    for k, v in tiers.items():
        print("  %-16s %d" % (k, v))
    print("已写入 live_official.m3u (%d 频道) 与 live_official.json" % len(lives))


if __name__ == "__main__":
    main()
