#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把整点快照渲染成 Web 实时报表（自包含单文件 HTML + CSV 导出）。

用法:
    python3 report.py                 # 读取 data/snapshots/**，生成 report/index.html
    python3 report.py --snapdir DIR   # 指定快照目录（便于预览）
    python3 report.py --open          # 生成后尝试用 in-app 浏览器打开
"""
import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))


def load_cfg():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def load_snapshots(snapdir):
    snaps = []
    for p in sorted(glob.glob(os.path.join(snapdir, "*", "*.json"))):
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("hour", os.path.basename(p)[:2].rjust(2, "0"))
            s["_path"] = p
            snaps.append(s)
        except Exception as e:  # noqa: BLE001
            print("  ! 跳过损坏快照 %s: %s" % (p, e), file=sys.stderr)
    # 按整点去重（同一天同一小时只保留最后一次写入）
    uniq = {}
    for s in snaps:
        uniq[s["hour"]] = s
    return [uniq[k] for k in sorted(uniq)]


def build_model(snaps, stage):
    hours = [s["hour"] for s in snaps]
    hist = {}      # csbh -> {team, points:{hour: (rank, score)}, submit, scored_at}
    order = []
    for s in snaps:
        for r in s.get("stages", {}).get(stage, []):
            key = r["csbh"] or ("team:" + r["team"])
            if key not in hist:
                hist[key] = {"csbh": r["csbh"], "team": r["team"], "points": {}}
                order.append(key)
            h = hist[key]
            h["team"] = r["team"] or h["team"]
            h["points"][s["hour"]] = (r["rank"], r["score"])
            h["submit"] = r["submit"]
            h["scored_at"] = r["scored_at"]

    last_hour = hours[-1] if hours else None
    prev_hour = hours[-2] if len(hours) > 1 else None

    teams = []
    for key in order:
        h = hist[key]
        cur = h["points"].get(last_hour)
        if cur is None:
            continue                       # 本轮未出现（退榜/删除）
        rank, score = cur
        prev = h["points"].get(prev_hour) if prev_hour else None
        prev_rank, prev_score = (prev if prev else (None, None))
        series = [[i, h["points"][hr][0], h["points"][hr][1]]
                  for i, hr in enumerate(hours) if hr in h["points"]]
        teams.append({
            "csbh": h["csbh"], "team": h["team"],
            "rank": rank, "score": score,
            "submit": h.get("submit", ""), "scored_at": h.get("scored_at", ""),
            "prev_rank": prev_rank, "prev_score": prev_score,
            "rank_delta": (prev_rank - rank) if (prev_rank is not None and rank is not None) else None,
            "score_delta": (round(score - prev_score, 6)
                            if (prev_score is not None and score is not None) else None),
            "series": series,
        })
    teams.sort(key=lambda t: (t["rank"] is None, t["rank"] if t["rank"] is not None else 10 ** 9))

    moved = [t for t in teams if t["rank_delta"]]
    up = sorted(moved, key=lambda t: -t["rank_delta"])
    dn = sorted(moved, key=lambda t: t["rank_delta"])
    sc = [t for t in teams if t["score_delta"] is not None and abs(t["score_delta"]) > 0.00005]
    stats = {
        "total": len(teams),
        "moved": len(moved),
        "topUp": up[0] if up and up[0]["rank_delta"] > 0 else None,
        "topDown": dn[0] if dn and dn[0]["rank_delta"] < 0 else None,
        "topScore": (max(sc, key=lambda t: t["score_delta"]) if sc else None),
        "leader": teams[0] if teams else None,
    }
    return hours, teams, stats


def write_csv(outdir, stage, hours, teams):
    exp = os.path.join(outdir, "data", "export")
    os.makedirs(exp, exist_ok=True)
    safe = "".join(ch for ch in stage if ch not in "\\/:*?\"<>|") or "stage"

    with open(os.path.join(exp, "standings_%s.csv" % safe), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["排名", "名次变动", "团队名称", "参赛编号", "分数", "上一整点分数", "分数变动",
                    "提交时间", "打分时间", "快照时间"])
        for t in teams:
            w.writerow([t["rank"], "" if t["rank_delta"] is None else t["rank_delta"],
                        t["team"], t["csbh"], t["score"], t["prev_score"],
                        "" if t["score_delta"] is None else round(t["score_delta"], 6),
                        t["submit"], t["scored_at"], hours[-1] if hours else ""])

    with open(os.path.join(exp, "history_%s.csv" % safe), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["快照时间", "排名", "团队名称", "参赛编号", "分数"])
        for t in teams:
            for i, r, s in t["series"]:
                w.writerow([hours[i], r, t["team"], t["csbh"], s])
    return exp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapdir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--stage", default=None)
    ap.add_argument("--outdir", default=None, help="项目根目录（覆盖 config.json，CI 里用 .）")
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    outdir = args.outdir or os.environ.get("AICOMP_OUTDIR") or cfg.get("outdir") or HERE
    stage = args.stage or cfg["stages"][0]
    snapdir = args.snapdir or os.path.join(outdir, "data", "snapshots")

    snaps = load_snapshots(snapdir)
    if not snaps:
        print("没有找到任何快照，先跑 collect.py", file=sys.stderr)
        return 1
    hours, teams, stats = build_model(snaps, stage)

    tz_now = datetime.now(TZ)
    nxt = (tz_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    model = {
        "title": cfg["name"],
        "stage": stage,
        "page_url": cfg["page_url"],
        "generated_at": tz_now.strftime("%Y-%m-%d %H:%M:%S"),
        "next_run": nxt.strftime("%m-%d %H:00"),
        "hours": hours,
        "has_prev": len(hours) > 1,
        "teams": teams,
        "stats": stats,
    }

    with open(os.path.join(HERE, "template.html"), "r", encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("__DATA__",
                       json.dumps(model, ensure_ascii=False).replace("</", "<\\/"))

    outdir_report = os.path.join(outdir, "report")
    os.makedirs(outdir_report, exist_ok=True)
    out = args.out or os.path.join(outdir_report, "index.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(out + ".tmp", out)

    exp = write_csv(outdir, stage, hours, teams)
    print("报表已生成: %s" % out)
    print("  赛段=%s 队伍=%d 快照=%d 个（%s → %s）" %
          (stage, len(teams), len(hours), hours[0] if hours else "-", hours[-1] if hours else "-"))
    print("  CSV 导出: %s" % exp)
    if args.open:
        os.system("minis-open %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
