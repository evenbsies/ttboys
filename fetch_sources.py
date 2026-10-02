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
from urllib.parse import urlsplit, urlunsplit, quote

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
DEFAULT_TIMEOUT = 10
MAX_DEPTH = 2            # 索引递归深度
MAX_PER_SOURCE = 120     # 单个配置源最多取前 N 个站点（保留作者主推源）
DEFAULT_MAX_SITES = 500  # 合并后站点总数上限

KEY_SECTIONS = ("sites", "parses", "lives", "doh", "rules", "ads", "headers")
BLOCKLIST_FILE = "blocklist.txt"   # 广告/赌博域名黑名单，每行一个，并入 merged.json 的 ads 字段

# 国内常用 GitHub raw 加速镜像（按优先级尝试；Actions 海外服务器直连优先）
DEFAULT_MIRRORS = [
    "https://ghfast.top/https://raw.githubusercontent.com/",
    "https://gh-proxy.com/https://raw.githubusercontent.com/",
    "https://gh.ddlc.top/https://raw.githubusercontent.com/",
]


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
    return "index", parse_text_to_urls(text)


def merge_configs(config_list, max_per_source, max_sites):
    """合并多个 TVBox 配置。返回 (merged_config, dedup_stats)。"""
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

    for cfg in config_list:
        # 品牌字段取第一个非空的
        for field in ("spider", "wallpaper", "logo"):
            if not merged[field] and cfg.get(field):
                merged[field] = cfg[field]

        # sites：按 api 去重；key 冲突加序号
        for s in cfg.get("sites", [])[:max_per_source]:
            if not isinstance(s, dict):
                continue
            api = s.get("api", "")
            if not api:
                continue
            if api in seen_api:
                continue
            if len(merged["sites"]) >= max_sites:
                break
            key = str(s.get("key", "") or api[:12])
            new_s = dict(s)
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

        # lives：按 url 去重
        for lv in cfg.get("lives", []):
            if not isinstance(lv, dict) or not lv.get("url"):
                continue
            if lv["url"] in seen_live:
                continue
            seen_live.add(lv["url"])
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


def main():
    ap = argparse.ArgumentParser(description="TVBox 每日源爬虫")
    ap.add_argument("--max-sites", type=int, default=DEFAULT_MAX_SITES, help="合并后站点上限")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单请求超时秒数")
    ap.add_argument("--sources", default="sources.txt", help="种子列表文件")
    ap.add_argument("--mirror", default="", help="自定义镜像前缀(逗号分隔)；默认使用内置镜像列表")
    args = ap.parse_args()

    mirror_list = [m.strip() for m in args.mirror.split(",") if m.strip()] if args.mirror else list(DEFAULT_MIRRORS)

    with open(args.sources, "r", encoding="utf-8") as f:
        seed_lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    if not seed_lines:
        print("sources.txt 为空或没有有效 URL，请先添加种子。", file=sys.stderr)
        sys.exit(1)

    status = {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"), "indexes": [], "configs": [], "failed": []}
    config_list = []
    index_count = 0
    visited = set()

    def process(url, depth):
        nonlocal index_count
        if url in visited or depth > MAX_DEPTH:
            return
        visited.add(url)
        t0 = time.time()
        kind, payload = fetch_kind(url, args.timeout, mirror_list)
        elapsed = int((time.time() - t0) * 1000)
        if kind == "fail":
            status["failed"].append({"url": url, "error": payload, "elapsed_ms": elapsed})
            print("[FAIL] %s  %s" % (url, payload))
            return
        if kind == "config":
            sites_n = len(payload.get("sites", []))
            status["configs"].append({"url": url, "ok": True, "sites": sites_n, "elapsed_ms": elapsed})
            print("[OK  ] %s  sites=%d (%dms)" % (url, sites_n, elapsed))
            config_list.append(payload)
            return
        # index
        urls = payload
        status["indexes"].append({"url": url, "ok": True, "found": len(urls), "elapsed_ms": elapsed})
        print("[IDX ] %s 展开 %d 个子源 (%dms)" % (url, len(urls), elapsed))
        for u in urls:
            process(u, depth + 1)
        index_count += 1

    for seed in seed_lines:
        process(seed, 0)

    if not config_list:
        print("没有抓到任何可用配置，未生成 merged.json。", file=sys.stderr)
        _write_status(status, {})
        sys.exit(1)

    merged, stats = merge_configs(config_list, MAX_PER_SOURCE, args.max_sites)

    # 并入用户维护的广告/赌博域名黑名单（TVBox 播放器据此拦截）
    blocklist = load_blocklist()
    for d in blocklist:
        if d not in merged["ads"]:
            merged["ads"].append(d)
    stats["blocklist_added"] = len(blocklist)

    merged["update_time"] = status["run_at"]
    merged["_sources"] = {"indexes": len(status["indexes"]),
                          "configs_ok": len(status["configs"]),
                          "failed": len(status["failed"])}

    with open("merged.json", "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)

    _write_status(status, stats)
    print("\n完成: sites=%d parses=%d lives=%d rules=%d  (配置源 %d 个, 失败 %d 个)" % (
        stats["sites_final"], stats["parses_final"], stats["lives_final"],
        stats["rules_final"], len(status["configs"]), len(status["failed"])))


def _write_status(status, stats):
    status["summary"] = stats
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
