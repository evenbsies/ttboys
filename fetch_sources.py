# -*- coding: utf-8 -*-
"""
TVBox 每日源爬虫
================
抓取公网上公开的 TVBox 订阅源，解析、合并、去重后生成每日汇总配置。

用法:
    python fetch_sources.py                 # 按 sources.txt 抓取并生成 merged.json / status.json
    python fetch_sources.py --max-sites 300 # 限制合并后站点总数上限
    python fetch_sources.py --timeout 12    # 单请求超时（秒）

种子来源:
    1. sources.txt 中每行一个 URL（# 开头为注释）
    2. 每个 URL 可能是:
       - TVBox 配置(含 sites/parses/lives 字段) -> 直接合并
       - 源地址索引(JSON 数组 [{url,name},...] / 纯文本每行一个 URL) -> 递归展开
    3. 递归深度限制 2 层（索引 -> 配置），防止循环

输出:
    merged.json  合并去重后的订阅配置（TVBox 可直接订阅）
    status.json  每个来源抓取状态与统计

说明: 只做结构化合并，不执行任何源内容；失效的源自动跳过并记录。
"""
import argparse
import json
import sys
import time
import urllib.request
from collections import OrderedDict
from datetime import date
from urllib.parse import urlsplit, urlunsplit, quote

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
DEFAULT_TIMEOUT = 10
MAX_DEPTH = 2            # 索引递归深度
MAX_PER_SOURCE = 120     # 单个配置源最多取前 N 个站点（保留作者主推源）
DEFAULT_MAX_SITES = 500  # 合并后站点总数上限

KEY_SECTIONS = ("sites", "parses", "lives", "doh", "rules", "ads", "headers")
BLOCKLIST_FILE = "blocklist.txt"   # 广告/赌博域名黑名单，每行一个，并入 merged.json 的 ads 字段
QUALITY_FILE = "quality.json"      # 源质量历史（每日 ok/fail 计数，驱动自动淘汰）
EXCLUDE_FILE = "excluded_sources.txt"  # 被自动淘汰的源（连续失败达阈值后写入）
FAIL_LIMIT = 3                     # 连续失败 N 天后自动淘汰
RETRY_DAYS = 7                     # 被淘汰源每 N 天自动复活探测一次

# 国内常用 GitHub raw 加速镜像（按优先级尝试；Actions 海外服务器直连优先）
DEFAULT_MIRRORS = [
    "https://ghfast.top/https://raw.githubusercontent.com/",
    "https://gh-proxy.com/https://raw.githubusercontent.com/",
    "https://gh.ddlc.top/https://raw.githubusercontent.com/",
]

# 实测不可达/失效的外部 js 爬虫域名（合并时默认剔除其站点，避免 jar load err）
# 2026-10-02 实测：notabug 404 / catbox 连接重置 / yylx 404 / jundie666 401 / bitbucket 返回非内容
JAR_HOST_BLOCKLIST = {
    "notabug.org",
    "files.catbox.moe",
    "git.yylx.win",
    "home.jundie.top:666",
    "bitbucket.org",
}


def host_of(api):
    try:
        return urlsplit(api).netloc
    except Exception:
        return ""


def safe_url(u):
    """含中文域名/路径的 URL 转成 IDNA + percent-encode，避免 urllib 编码错误。"""
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    netloc = parts.netloc
    if parts.hostname and not parts.hostname.isascii():
        try:
            host = parts.hostname.encode("idna").decode("ascii")
        except Exception:
            host = parts.hostname
        netloc = host
        if parts.port:
            netloc += ":%d" % parts.port
        if parts.username:
            netloc = (parts.username + (":" + parts.password if parts.password else "") + "@") + netloc
    path = parts.path
    if path and not path.isascii():
        path = quote(path, safe="/%:@&=+$,;~*'()[]!-_.~")
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


def http_get(url, timeout):
    req = urllib.request.Request(safe_url(url), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def normalize_github_url(url):
    """把 github.com/{owner}/{repo}/raw/{ref}/{path} 规范化为 raw.githubusercontent.com 直链，
    提高镜像转发成功率（分支名含斜杠的罕见情况会解析不准，交给 404 容错）。"""
    if url.startswith("https://github.com/") and "/raw/" in url:
        rest = url[len("https://github.com/"):]
        try:
            owner, repo, _raw, ref, path = rest.split("/", 4)
        except ValueError:
            return url
        return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, ref, path)
    return url


def url_candidates(url, mirror_list):
    """返回尝试顺序：规范化后的原始 URL 在前（Actions 直连优先），镜像依次在后。
    镜像同时覆盖 raw.githubusercontent.com 与 github.com 两种链接格式。"""
    url = normalize_github_url(url)
    cands = [url]
    prefix = "https://raw.githubusercontent.com/"
    if url.startswith(prefix):
        tail = url[len(prefix):]
        for m in mirror_list:
            if m:
                cands.append(m + tail)
    return cands


def http_get_any(url, timeout, mirror_list):
    """依次尝试直连与镜像，全部失败才抛最后一个异常。"""
    last_exc = None
    for u in url_candidates(url, mirror_list):
        try:
            return http_get(u, timeout)
        except Exception as e:
            last_exc = e
    raise last_exc


def parse_text_to_urls(text):
    """把抓到的文本解析为 URL 列表。支持 JSON 数组/对象和纯文本两种形式。"""
    urls = []
    text = (text or "").strip()
    if not text:
        return urls

    # 先尝试 JSON
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None

    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                urls.append(item["url"])
            elif isinstance(item, str) and item.startswith("http"):
                urls.append(item)
    elif isinstance(obj, dict):
        for key in ("urls", "list", "sources", "subs"):
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict) and isinstance(item.get("url"), str):
                        urls.append(item["url"])
                    elif isinstance(item, str) and item.startswith("http"):
                        urls.append(item)
                break
    else:
        # 纯文本：每行一个 URL
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http"):
                urls.append(line.split()[0])

    # 去重保序
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_kind(url, timeout, mirror_list):
    """抓取一个 URL，返回 (kind, payload)。kind: config / index / fail"""
    try:
        data = http_get_any(url, timeout, mirror_list)
    except Exception as e:
        return "fail", str(e)

    text = data.decode("utf-8", "replace").lstrip("\ufeff")
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None

    if isinstance(obj, dict) and ("sites" in obj or "spider" in obj or "lives" in obj):
        return "config", obj
    if text.startswith("#EXTM3U"):
        # m3u 直播列表：作为单个直播源合并，不再当"每行 URL"的索引递归展开
        return "live", text
    return "index", parse_text_to_urls(text)


def usable_spider_url(sp):
    """返回可注入站点 jar 的绝对 URL；相对路径/黑名单域名返回 None。

    源配置的 spider 若为绝对可达 URL（含作者伪装成 .jpg/.png 的 dex jar），
    下放到站点级 jar 字段，恢复 csp_Wex*/csp_Ai*/csp_SheQu* 等自定义类站点；
    相对路径（./jar/xxx.jar）URL 订阅无法下载，跳过避免 jar load err。
    """
    if not sp:
        return None
    sp = sp.strip()
    if sp.startswith("./") or sp.startswith("/"):
        return None
    if not (sp.startswith("http://") or sp.startswith("https://")):
        return None
    if host_of(sp) in JAR_HOST_BLOCKLIST:
        return None
    return sp


def merge_configs(config_list, max_per_source, max_sites, drop_jar=True, spider="",
                  keep_lives=False, inject_jar=True):
    """合并多个 TVBox 配置。返回 (merged_config, dedup_stats)。

    drop_jar=True（默认）：剔除网络订阅下必报错的站点——
      1) api 为相对路径（./lib/xxx.js 等，URL 订阅无法下载）
      2) api 指向实测不可达的外部 js 域名（JAR_HOST_BLOCKLIST）
    spider 默认置空（播放器用内置默认，从根上避免 jar load err）；
    高级用户可用 --spider 显式指定一个可达的 jar/http 链接。
    inject_jar=True（默认）：源 spider 为绝对 URL 时注入到该源所有
    无 jar 字段的站点，恢复自定义 jar 类站点（秒播/4K 等）。
    """
    merged = {
        "spider": "", "wallpaper": "", "logo": "",
        "sites": [], "parses": [], "doh": [], "rules": [],
        "ads": [], "lives": [], "headers": [],
    }
    seen_api = {}        # api -> 占用它的 key
    used_keys = set()
    seen_parse = set()
    seen_live = set()
    seen_rule = set()
    seen_doh = set()
    seen_ads = set()
    seen_header = set()
    dropped_jar = 0
    dropped_live = 0
    jar_injected = 0

    for cfg in config_list:
        # 品牌字段：spider 用参数指定的值（默认空）；wallpaper/logo 取第一个非空
        for field in ("wallpaper", "logo"):
            if not merged[field] and cfg.get(field):
                merged[field] = cfg[field]
        merged["spider"] = spider or ""
        # 该源 spider 若为绝对可达 URL，作为本源站点默认 jar（恢复自定义类站点）
        src_spider = usable_spider_url(cfg.get("spider", "")) if inject_jar else None

        # sites：按 api 去重；key 冲突加序号；过滤 jar 报错源
        for s in cfg.get("sites", [])[:max_per_source]:
            if not isinstance(s, dict):
                continue
            api = s.get("api", "")
            if not api:
                continue
            if drop_jar and api.startswith("./"):
                dropped_jar += 1
                continue
            if drop_jar and api.startswith("http") and host_of(api) in JAR_HOST_BLOCKLIST:
                dropped_jar += 1
                continue
            if api in seen_api:
                continue
            if len(merged["sites"]) >= max_sites:
                break
            key = str(s.get("key", "") or api[:12])
            new_s = dict(s)
            if not new_s.get("jar"):
                new_s.pop("jar", None)  # 空串 jar 会让播放器下载空地址，移除
            if src_spider and not new_s.get("jar"):
                new_s["jar"] = src_spider
                jar_injected += 1
            if key in used_keys:
                i = 2
                while "%s_%d" % (key, i) in used_keys:
                    i += 1
                new_s["key"] = "%s_%d" % (key, i)
            used_keys.add(new_s["key"])
            seen_api[api] = new_s["key"]
            merged["sites"].append(new_s)
            if len(merged["sites"]) >= max_sites:
                break

        # parses：按 url 去重
        for p in cfg.get("parses", []):
            if not isinstance(p, dict) or not p.get("url"):
                continue
            if p["url"] in seen_parse:
                continue
            seen_parse.add(p["url"])
            merged["parses"].append(p)

        # lives：按 url 去重；过滤网络订阅下必死的相对路径/本地代理条目
        for lv in cfg.get("lives", []):
            if not isinstance(lv, dict) or not lv.get("url"):
                continue
            lurl = lv["url"]
            if lurl.startswith("./"):
                dropped_live += 1
                continue
            h = host_of(lurl)
            if h.split(":")[0] in ("127.0.0.1", "localhost"):
                dropped_live += 1
                continue
            if lurl in seen_live:
                continue
            seen_live.add(lurl)
            merged["lives"].append(lv)

        # rules：按 name 去重
        for r in cfg.get("rules", []):
            if not isinstance(r, dict) or not r.get("name"):
                continue
            if r["name"] in seen_rule:
                continue
            seen_rule.add(r["name"])
            merged["rules"].append(r)

        # doh：按 url 去重
        for dh in cfg.get("doh", []):
            if not isinstance(dh, dict) or not dh.get("url"):
                continue
            if dh["url"] in seen_doh:
                continue
            seen_doh.add(dh["url"])
            merged["doh"].append(dh)

        # ads：字符串集合
        for a in cfg.get("ads", []):
            if isinstance(a, str) and a and a not in seen_ads:
                seen_ads.add(a)
                merged["ads"].append(a)

        # headers：按 host 去重
        for h in cfg.get("headers", []):
            if not isinstance(h, dict) or not h.get("host"):
                continue
            if h["host"] in seen_header:
                continue
            seen_header.add(h["host"])
            merged["headers"].append(h)

    sites_raw = sum(len(cfg.get("sites", [])) for cfg in config_list)
    dedup_stats = {
        "sites_raw": sites_raw,
        "sites_final": len(merged["sites"]),
        "parses_final": len(merged["parses"]),
        "lives_final": len(merged["lives"]),
        "rules_final": len(merged["rules"]),
        "dropped_jar": dropped_jar,
        "dropped_live": dropped_live,
        "jar_injected": jar_injected,
    }
    return merged, dedup_stats


def load_blocklist():
    """读取广告/赌博域名黑名单（每行一个域名，# 注释），返回域名集合。"""
    domains = set()
    try:
        with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    domains.add(ln)
    except FileNotFoundError:
        pass
    return domains


# ---------- 源质量自动淘汰 ----------

def days_between(a, b):
    """两个 YYYY-MM-DD 之间的天数差；解析失败返回 0。"""
    try:
        da = date(*map(int, a.split("-")))
        db = date(*map(int, b.split("-")))
        return (db - da).days
    except Exception:
        return 0


def load_quality(path):
    """读取质量历史 quality.json 的 history 部分。结构：
       {url: {"ok_days": N, "fail_days": N, "last": "ok|fail",
              "last_date": "YYYY-MM-DD", "first_seen": "YYYY-MM-DD"}}"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, dict) and "history" in data:
        h = data["history"]
        return h if isinstance(h, dict) else {}
    return data if isinstance(data, dict) else {}


def save_quality(path, history, run_date):
    data = {"updated": run_date, "history": history}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_excluded(path):
    """读取被淘汰源 excluded_sources.txt。返回 {url: 排除日期}。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                url = ln.split()[0]
                m = ln.rfind("排除于 ")
                d = ln[m + len("排除于 "):].strip() if m >= 0 else ""
                out[url] = d
    except FileNotFoundError:
        pass
    return out


def save_excluded(path, excluded):
    """写出被淘汰源列表（url + 注释日期），供人工查看/手动恢复。"""
    lines = ["# 自动淘汰的源（连续抓取失败 >= %d 天）。手动恢复：删除对应行即可。" % FAIL_LIMIT,
             "# 被淘汰的源每 %d 天自动复活探测一次，恢复可用后自动移出本文件。" % RETRY_DAYS,
             ""]
    for url in sorted(excluded):
        d = excluded[url] or "?"
        lines.append("%s  # 排除于 %s" % (url, d))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def bump_history(history, url, ok, today):
    """按天累计 ok/fail：同一 URL 一天内只更新一次，连续失败/成功按自然日计数。"""
    h = history.setdefault(url, {"ok_days": 0, "fail_days": 0, "last": "ok",
                                 "last_date": "", "first_seen": today})
    h.setdefault("first_seen", today)
    if h.get("last_date") == today:
        return h
    if ok:
        h["ok_days"] = h.get("ok_days", 0) + 1
        h["fail_days"] = 0
        h["last"] = "ok"
    else:
        h["ok_days"] = 0
        h["fail_days"] = h.get("fail_days", 0) + 1
        h["last"] = "fail"
    h["last_date"] = today
    return h


def main():
    ap = argparse.ArgumentParser(description="TVBox 每日源爬虫")
    ap.add_argument("--max-sites", type=int, default=DEFAULT_MAX_SITES, help="合并后站点上限")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单请求超时秒数")
    ap.add_argument("--sources", default="sources.txt", help="种子列表文件")
    ap.add_argument("--mirror", default="", help="自定义镜像前缀(逗号分隔)；默认使用内置镜像列表")
    ap.add_argument("--keep-jar", action="store_true",
                    help="保留相对路径/不可达域名的 js 爬虫源（默认剔除，避免 jar load err）")
    ap.add_argument("--spider", default="",
                    help="显式指定 spider 字段（默认空=播放器内置默认，彻底避免 jar load err）")
    ap.add_argument("--no-inject-jar", action="store_true",
                    help="不把源 spider(绝对URL) 注入站点 jar 字段（默认注入，恢复自定义类站点）")
    ap.add_argument("--official-live", action="store_true",
                    help="并入官方直播源（读取 live_official.json，由 live_official.py 生成）")
    ap.add_argument("--quality-file", default=QUALITY_FILE, help="质量历史文件")
    ap.add_argument("--exclude-file", default=EXCLUDE_FILE, help="被淘汰源列表文件")
    ap.add_argument("--fail-limit", type=int, default=FAIL_LIMIT, help="连续失败 N 天后淘汰")
    ap.add_argument("--retry-days", type=int, default=RETRY_DAYS, help="被淘汰源 N 天后复活探测")
    args = ap.parse_args()

    mirror_list = [m.strip() for m in args.mirror.split(",") if m.strip()] if args.mirror else list(DEFAULT_MIRRORS)

    with open(args.sources, "r", encoding="utf-8") as f:
        seed_lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    if not seed_lines:
        print("sources.txt 为空或没有有效 URL，请先添加种子。", file=sys.stderr)
        sys.exit(1)

    # ---- 源质量自动淘汰：过滤已淘汰种子 + 复活探测 ----
    today = time.strftime("%Y-%m-%d")
    history = load_quality(args.quality_file)
    excluded = load_excluded(args.exclude_file)
    revived = []
    for u in list(excluded):
        if days_between(excluded[u], today) >= args.retry_days:
            revived.append(u)
            del excluded[u]   # 先解除，探测成功则确认，失败则重新计时
    active_excluded = {u for u, d in excluded.items() if days_between(d, today) < args.retry_days}
    seed_pool = []
    for u in seed_lines:
        if u in active_excluded:
            print("[SKIP] %s  已在淘汰列表（%s 排除），%d 天后自动复活探测" %
                  (u, excluded[u], args.retry_days - days_between(excluded[u], today)))
            continue
        seed_pool.append(u)
    seed_pool += revived
    if revived:
        print("[TRY ] 复活探测 %d 个被淘汰源: %s" % (len(revived), ", ".join(revived)))
    print("本次抓取种子 %d 个（含复活 %d 个，跳过已淘汰 %d 个）" %
          (len(seed_pool), len(revived), len(active_excluded)))

    status = {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"), "indexes": [], "configs": [], "failed": []}
    config_list = []
    index_count = 0
    visited = set()

    def process(url, depth):
        nonlocal index_count
        if url in visited or depth > MAX_DEPTH:
            return
        if url in active_excluded:
            visited.add(url)   # 标记为"本次已知仍被排除"，避免清理逻辑误删记录
            print("[SKIP] %s  已在淘汰列表（%s 排除），自动跳过" % (url, excluded.get(url, "?")))
            return
        visited.add(url)
        t0 = time.time()
        kind, payload = fetch_kind(url, args.timeout, mirror_list)
        elapsed = int((time.time() - t0) * 1000)
        if kind == "fail":
            status["failed"].append({"url": url, "error": payload, "elapsed_ms": elapsed})
            bump_history(history, url, False, today)
            print("[FAIL] %s  %s" % (url, payload))
            return
        bump_history(history, url, True, today)
        if kind == "config":
            sites_n = len(payload.get("sites", []))
            status["configs"].append({"url": url, "ok": True, "sites": sites_n, "elapsed_ms": elapsed})
            print("[OK  ] %s  sites=%d (%dms)" % (url, sites_n, elapsed))
            config_list.append(payload)
            return
        if kind == "live":
            # m3u 直播列表 → 包成最小配置并入 lives
            status["configs"].append({"url": url, "ok": True, "sites": 0, "elapsed_ms": elapsed, "live": True})
            print("[LIVE] %s 直播列表 (%dms)" % (url, elapsed))
            name = host_of(url) or "m3u"
            config_list.append({"sites": [], "lives": [{"name": name, "url": url, "type": 0}]})
            return
        # index
        urls = payload
        status["indexes"].append({"url": url, "ok": True, "found": len(urls), "elapsed_ms": elapsed})
        print("[IDX ] %s 展开 %d 个子源 (%dms)" % (url, len(urls), elapsed))
        for u in urls:
            process(u, depth + 1)
        index_count += 1

    for seed in seed_pool:
        process(seed, 0)

    # ---- 淘汰判定与复活确认（覆盖种子 + 索引展开的子源）----
    excluded_new = 0
    revived_ok = 0
    revived_fail = 0
    for u in visited:
        h = history.get(u, {})
        if u in revived:
            if h.get("last") == "ok":
                revived_ok += 1      # 复活成功，已从 excluded 移除
            else:
                revived_fail += 1
                excluded[u] = today   # 复活失败，重新计时
            continue
        if u in active_excluded:
            continue
        if h.get("fail_days", 0) >= args.fail_limit:
            excluded[u] = today
            excluded_new += 1
            print("[OUT ] %s 连续失败 %d 天，自动淘汰（%d 天后复活探测）" %
                  (u, h["fail_days"], args.retry_days))
    # 清理：本次已不再尝试的 URL（如 sources.txt 已删除该源或整个索引）清出淘汰名单
    excluded = {u: d for u, d in excluded.items() if u in visited or u in active_excluded}
    save_excluded(args.exclude_file, excluded)
    save_quality(args.quality_file, history, today)

    if not config_list:
        print("没有抓到任何可用配置，未生成 merged.json。", file=sys.stderr)
        _write_status(status, {})
        sys.exit(1)

    merged, stats = merge_configs(config_list, MAX_PER_SOURCE, args.max_sites,
                                  drop_jar=not args.keep_jar, spider=args.spider,
                                  inject_jar=not args.no_inject_jar)

    # 并入用户维护的广告/赌博域名黑名单（TVBox 播放器据此拦截）
    blocklist = load_blocklist()
    for d in blocklist:
        if d not in merged["ads"]:
            merged["ads"].append(d)
    stats["blocklist_added"] = len(blocklist)

    # 并入官方直播源（live_official.py 生成，咪咕等官方直链）
    if args.official_live:
        try:
            with open("live_official.json", "r", encoding="utf-8") as f:
                off_lives = json.load(f)
            seen_live = set(lv["url"] for lv in merged["lives"])
            added = 0
            for lv in off_lives:
                if not isinstance(lv, dict) or not lv.get("url"):
                    continue
                if lv["url"] in seen_live:
                    continue
                seen_live.add(lv["url"])
                merged["lives"].append(lv)
                added += 1
            stats["official_live_added"] = added
            stats["lives_final"] = len(merged["lives"])
            print("并入官方直播源: %d 个（lives 共 %d）" % (added, len(merged["lives"])))
        except FileNotFoundError:
            print("未找到 live_official.json，跳过官方源并入（先运行 python live_official.py）")

    merged["update_time"] = status["run_at"]
    merged["_sources"] = {"indexes": len(status["indexes"]),
                          "configs_ok": len(status["configs"]),
                          "failed": len(status["failed"])}

    with open("merged.json", "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)

    stats["quality"] = {"excluded_total": len(excluded),
                        "excluded_new": excluded_new,
                        "revived_ok": revived_ok,
                        "revived_fail": revived_fail}
    _write_status(status, stats)
    print("\n完成: sites=%d parses=%d lives=%d rules=%d  (配置源 %d 个, 失败 %d 个)" % (
        stats["sites_final"], stats["parses_final"], stats["lives_final"],
        stats["rules_final"], len(status["configs"]), len(status["failed"])))
    print("质量: 淘汰中 %d 个(本次新增 %d) | 复活成功 %d / 失败 %d" %
          (len(excluded), excluded_new, revived_ok, revived_fail))
    if stats.get("jar_injected"):
        print("jar注入: %d 个站点补上可达的源 jar（恢复秒播/4K 等自定义类站点）" % stats["jar_injected"])


def _write_status(status, stats):
    status["summary"] = stats
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
