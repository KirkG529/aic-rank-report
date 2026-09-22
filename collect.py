#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIC 排行榜抓取：调用榜单接口，落盘成一个「整点快照」文件。

用法:
    python3 collect.py                # 抓取当前时刻（自动对齐到整点槽位）
    python3 collect.py --dry-run      # 只打印，不写文件
    python3 collect.py --outdir DIR

快照文件: <outdir>/data/snapshots/<YYYY-MM-DD>/<HH>.json
同一小时内重复运行会覆盖该整点槽位，保证每小时只有一个样本。
"""
import json
import os
import sys
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")

# 东八区（设备时区 Asia/Shanghai）
TZ = timezone(timedelta(hours=8))


def load_cfg(path=CFG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def now():
    return datetime.now(TZ)


def http_post_json(url, payload, timeout=30, retries=3, ua=None):
    """带重试的 POST JSON。返回解析后的 dict。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://reg.aicomp.cn",
        "Referer": "https://reg.aicomp.cn/",
        "User-Agent": ua or ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    }
    last = None
    for i in range(max(1, retries)):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError("请求失败 %s: %s" % (url, last))


def num(v, cast=float):
    """容错转数字，失败返回 None。"""
    try:
        s = str(v).strip()
        if s == "" or s.lower() == "null":
            return None
        return cast(s)
    except Exception:  # noqa: BLE001
        return None


def fetch_stage(cfg, stage):
    """抓一个赛段的全部队伍（自动翻页兜底）。"""
    rows, page, size = [], 0, int(cfg.get("page_size", 2000))
    total = None
    while True:
        payload = {
            "pageNo": page,
            "pageSize": size,
            "type": cfg.get("type", "JSDF"),
            "rwId": cfg["rwId"],
            "stbh": cfg["stbh"],
            "jd": stage,
        }
        js = http_post_json(cfg["api"], payload,
                            timeout=cfg.get("timeout", 30),
                            retries=cfg.get("retries", 3))
        if not js.get("success", True):
            raise RuntimeError("接口返回失败: %s" % js.get("msg"))
        data = js.get("data") or []
        if total is None:
            total = num(js.get("total"), int) or len(data)
        rows.extend(data)
        if not data or len(rows) >= total or len(data) < size:
            break
        page += 1
    return rows, (total or len(rows))


def slim(stage, rows):
    """精简字段，按名次排序。"""
    out = []
    for r in rows:
        out.append({
            "csbh": r.get("CSBH_", ""),                     # 参赛编号
            "team": (r.get("TDMC_") or "").strip(),          # 团队名称
            "rank": num(r.get("XH_"), int),                  # 排名
            "score": num(r.get("FS_")),                      # 分数
            "submit": r.get("ZPZHTJSJ_") or "",              # 提交时间
            "scored_at": r.get("DFSJ_") or "",               # 打分时间
            "stage": r.get("DQJD_") or stage,
        })
    out.sort(key=lambda x: (x["rank"] is None, x["rank"] if x["rank"] is not None else 10 ** 9))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=CFG_PATH)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stages", default=None, help="逗号分隔，覆盖配置")
    ap.add_argument("--at", default=None, help="手动指定时间 HH 槽位(测试用), 如 17")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    outdir = args.outdir or cfg.get("outdir") or HERE
    stages = [s.strip() for s in args.stages.split(",")] if args.stages else cfg["stages"]

    t = now()
    if args.at:
        t = t.replace(hour=int(args.at), minute=0, second=0, microsecond=0)
    hour_key = t.strftime("%Y-%m-%d %H:00")
    snap = {
        "hour": hour_key,
        "collected_at": t.strftime("%Y-%m-%d %H:%M:%S"),
        "name": cfg["name"],
        "page_url": cfg["page_url"],
        "stages": {},
        "totals": {},
        "errors": {},
    }

    for st in stages:
        try:
            rows, total = fetch_stage(cfg, st)
            snap["stages"][st] = slim(st, rows)
            snap["totals"][st] = len(snap["stages"][st])
        except Exception as e:  # noqa: BLE001
            snap["errors"][st] = str(e)
            snap["stages"][st] = []
            snap["totals"][st] = 0

    if args.dry_run:
        print(json.dumps({k: snap["totals"][k] for k in snap["totals"]}, ensure_ascii=False))
        return 0

    day_dir = os.path.join(outdir, "data", "snapshots", t.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, t.strftime("%H") + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, path)          # 原子替换，避免读到半截文件

    # 最新快照副本，供其它程序快速读取
    os.makedirs(os.path.join(outdir, "data"), exist_ok=True)
    latest = os.path.join(outdir, "data", "latest.json")
    with open(latest + ".tmp", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(latest + ".tmp", latest)

    summary = " ".join("%s=%d" % (k, v) for k, v in snap["totals"].items())
    print("[%s] 快照已保存 %s | %s" % (snap["collected_at"], os.path.relpath(path, outdir), summary))
    for st, err in snap["errors"].items():
        print("  ! %s 抓取失败: %s" % (st, err), file=sys.stderr)
    return 0 if not snap["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
