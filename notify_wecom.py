#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排名变化推送到企业微信群机器人。

企业微信「群机器人」就是一条 webhook，POST 一个 JSON 即可发消息，无需企业认证、无需 App 授权。

准备（1 分钟）：
  手机企业微信 → 进任意一个群（可以自己建一个"排名播报"群）→ 右上角 ···
  → 群机器人 → 添加机器人 → 起个名字 → 得到 Webhook 地址：
  https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx

配置：
  环境变量 WECOM_WEBHOOK=<上面那条完整地址>   （GitHub 上放 repository secret，本机放 Minis 环境变量）

用法：
  python3 notify_wecom.py --dry-run     # 只打印将要发送的内容
  python3 notify_wecom.py --test        # 发一条测试消息
  python3 notify_wecom.py --outdir .    # 正常推送（有变化且本小时未推送过才发）
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from report import load_snapshots, build_model  # noqa: E402

TZ = timezone(timedelta(hours=8))
MOVED_EPS = 0.00005


def post_json(url, payload, timeout=20):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def build_message(hours, teams, stats, url, max_items=5):
    """生成企业微信 markdown 消息（内容上限 4096 字节）"""
    cur = hours[-1]
    prev = hours[-2]
    up = sorted([t for t in teams if t["rank_delta"] and t["rank_delta"] > 0],
                key=lambda t: -t["rank_delta"])
    dn = sorted([t for t in teams if t["rank_delta"] and t["rank_delta"] < 0],
                key=lambda t: t["rank_delta"])
    sc = sorted([t for t in teams if t["score_delta"] is not None and abs(t["score_delta"]) > MOVED_EPS],
                key=lambda t: -t["score_delta"])
    new = [t for t in teams if t["rank_delta"] is None]

    L = []
    L.append("**AIC 复赛排名变化** <font color=\"comment\">%s</font>" % cur[5:])
    L.append("> 共 **%d** 支队 · 本轮 **%d** 支名次变动%s"
             % (stats["total"], stats["moved"],
                ("、**%d** 支新进榜" % len(new)) if new else ""))
    L.append("> 最高分：**%s**（%s）" % (stats["leader"]["score"], stats["leader"]["team"]) if stats["leader"] else "")

    if up:
        L.append("**🔼 上升**")
        for t in up[:max_items]:
            L.append("> %s <font color=\"info\">▲%d</font> → 第 %d 名（%.4f）"
                     % (t["team"], t["rank_delta"], t["rank"], t["score"]))
    if dn:
        L.append("**🔽 下降**")
        for t in dn[:max_items]:
            L.append("> %s <font color=\"warning\">▼%d</font> → 第 %d 名（%.4f）"
                     % (t["team"], abs(t["rank_delta"]), t["rank"], t["score"]))
    if sc:
        L.append("**📈 分数提升**")
        for t in sc[:max_items]:
            L.append("> %s +%.4f → %.4f" % (t["team"], t["score_delta"], t["score"]))
    if new:
        L.append("**🆕 新进榜**")
        for t in new[:max_items]:
            L.append("> %s 第 %d 名（%.4f）" % (t["team"], t["rank"], t["score"]))
    if not (up or dn or sc or new):
        L.append("> 本轮没有名次或分数变化")

    if url:
        L.append("[查看完整报表](%s)" % url)
    return "\n".join([x for x in L if x])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--stage", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--webhook", default=None)
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    outdir = args.outdir or os.environ.get("AICOMP_OUTDIR") or cfg.get("outdir") or HERE
    stage = args.stage or cfg["stages"][0]
    url = os.environ.get("PAGES_URL", "").strip()
    hook = args.webhook or os.environ.get("WECOM_WEBHOOK", "").strip()

    if args.test:
        if not hook:
            print("未设置 WECOM_WEBHOOK", file=sys.stderr)
            return 2
        r = post_json(hook, {"msgtype": "markdown", "markdown": {
            "content": "**AIC 排名播报已接通** ✅\n> 本机时间 %s\n> 之后有名次变化会自动推送到这里"
                       % datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}})
        print("测试消息结果:", r)
        return 0 if r.get("errcode") == 0 else 1

    snapdir = os.path.join(outdir, "data", "snapshots")
    snaps = load_snapshots(snapdir)
    if len(snaps) < 2:
        print("快照不足 2 个，跳过推送")
        return 0

    hours, teams, stats = build_model(snaps, stage)
    msg = build_message(hours, teams, stats, url)

    # 去重：同一小时且变化集合相同则不重复推送
    changed = sorted([(t["csbh"], t["rank_delta"], t["score_delta"])
                      for t in teams
                      if t["rank_delta"] not in (None, 0)
                      or (t["score_delta"] is not None and abs(t["score_delta"]) > MOVED_EPS)])
    digest = hashlib.md5(json.dumps(changed, ensure_ascii=False).encode()).hexdigest()[:12]
    state_path = os.path.join(outdir, "data", "notify_state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    if state.get("hour") == hours[-1] and state.get("hash") == digest:
        print("本小时（%s）已推送过相同变化，跳过" % hours[-1])
        return 0

    print("---- 消息预览 ----")
    print(msg)
    print("------------------")

    if args.dry_run:
        print("(dry-run，未发送)")
        return 0
    if not hook:
        print("未设置 WECOM_WEBHOOK，跳过发送（用 --dry-run 预览即可）")
        return 0

    r = post_json(hook, {"msgtype": "markdown", "markdown": {"content": msg}})
    print("推送结果:", r)
    if r.get("errcode") == 0:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        json.dump({"hour": hours[-1], "hash": digest,
                   "sent_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")},
                  open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
