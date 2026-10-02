# -*- coding: utf-8 -*-
"""官方直播源采集器（咪咕等广电正版源）

每天抓取多个公开维护的"官方直链"列表（miguvideo.com 等官方域名），
解析 -> 过滤官方域名 -> 归一化频道名 -> 并发存活探测 -> 输出：
  - live_official.m3u    （m3u 格式，可直接导入播放器）
  - live_official.json   （TVBox lives 格式，供 merged.json 并入）

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
]

# 只保留这些官方域名（广电正版授权流），其余一律丢弃
OFFICIAL_HOST_RULES = [
    "miguvideo.com",        # 咪咕（中国移动）官方 HLS/PLTV
    "cmvideo.cn",           # 中国移动视频
    "chinamobile.com",      # 移动 IPTV PLTV
]

# 卫视名单（用于分组）
SAT_NAMES = "湖南|浙江|江苏|东方|北京|广东|深圳|山东|安徽|四川|湖北|天津|重庆|辽宁|江西|黑龙江|河北|河南|陕西|山西|福建|广西|云南|贵州|吉林|内蒙古|新疆|西藏|甘肃|青海|宁夏|海南|厦门|大连|青岛|宁波|东南"


def grab(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def parse_list(text):
    """解析 '频道名,url' 文本列表；支持 # 注释与 #genre# 分组行。"""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("#EXTM3U"):
            continue
        if "#genre#" in line:
            continue
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip().startswith("http"):
            entries.append((parts[0].strip(), parts[1].strip()))
    return entries


def is_official(url):
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return False
    return any(rule in host for rule in OFFICIAL_HOST_RULES)


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
        if e.code == 403:
            return "lock"   # IP 受限：数据中心/云 IP 常被官方源拒绝，家庭宽带可播
        return "lock"
    except Exception:
        return False


def main():
    raw_entries = []
    for src in OFFICIAL_SOURCES:
        name, url = src.split(",", 1)
        try:
            txt = grab(url)
        except Exception as e:
            print("[FAIL] %s %s" % (name, str(e)[:60]))
            continue
        es = parse_list(txt)
        raw_entries += es
        print("[OK  ] %-10s 原始频道 %d" % (name, len(es)))
        sys.stdout.flush()

    # 1) 只留官方域名
    official = [e for e in raw_entries if is_official(e[1])]
    print("官方域名过滤后:", len(official), "/", len(raw_entries))

    # 2) 按 url 去重
    seen_url = set()
    uniq = []
    for name, url in official:
        if url in seen_url:
            continue
        seen_url.add(url)
        uniq.append((name, url))
    print("URL 去重后:", len(uniq))

    # 3) 官方域名源跳过服务器探测、全部保留：
    #    咪咕等官方直链对数据中心/海外 IP 一律 403 或 404（防盗链），只有家庭宽带才准；
    #    服务器端探测只会误杀，所以对官方域名不做剔除，交由用户播放器实测。
    alive = {}
    locked = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(probe, url): (name, url) for name, url in uniq}
        for fut in as_completed(futs):
            name, url = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = False
            if is_official(url):
                # 官方域名：无论探测结果如何都保留（探测仅作参考，不剔除）
                key = norm_name(name)
                if key not in alive:
                    alive[key] = (name, url)
                continue
            if res is False:
                continue
            key = norm_name(name)
            if key not in (alive if res is True else locked):
                (alive if res is True else locked)[key] = (name, url)
    print("官方频道（全部保留）:", len(alive), "| 非官方可播:", len(locked))

    # 4) 输出 m3u（可播在前，IP受限在后；按分组排序）
    groups = OrderedDict()
    all_ch = sorted(alive.items(), key=lambda x: x[1][0]) + \
             sorted(locked.items(), key=lambda x: x[1][0])
    for key, (name, url) in all_ch:
        g = group_of(name)
        groups.setdefault(g, []).append((name, url))

    m3u = ["#EXTM3U"]
    for g, items in groups.items():
        for name, url in items:
            m3u.append('#EXTINF:-1 group-title="%s",%s' % (g, name))
            m3u.append(url)
    with open("live_official.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")

    lives = [{"name": n, "url": u, "type": 0} for _, (n, u) in all_ch]
    with open("live_official.json", "w", encoding="utf-8") as f:
        json.dump(lives, f, ensure_ascii=False, indent=1)

    print("== 分组统计 ==")
    for g, items in groups.items():
        print("  %-6s %d" % (g, len(items)))
    print("已写入 live_official.m3u (%d 频道) 与 live_official.json" % len(all_ch))


if __name__ == "__main__":
    main()
