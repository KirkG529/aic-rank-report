#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把报表与快照上传到腾讯云 COS（对象存储）。

依赖: pip install cos-python-sdk-v5   （已验证可在本机 iSH 内安装）

凭证从环境变量读取，不写进代码、不进日志:
    COS_SECRET_ID    访问密钥 ID   (控制台 → 访问管理 → API 密钥管理)
    COS_SECRET_KEY   访问密钥 Key
    COS_REGION       存储桶地域    例如 ap-guangzhou / ap-shanghai / ap-beijing
    COS_BUCKET       存储桶名称    必须带 APPID 后缀，例如 aic-rank-1250000000
    COS_PREFIX       对象前缀(可选) 默认 aic-rank
    COS_TOKEN        临时密钥 Token (可选，用 STS 临时密钥时需要)

用法:
    python3 upload_cos.py --check                 # 只校验凭证/桶是否可用
    python3 upload_cos.py                         # 上传报表 + 最新快照 + 今日快照 + CSV
    python3 upload_cos.py --only report           # 只传 report/index.html
    python3 upload_cos.py --dry-run               # 只打印将要上传什么
    python3 upload_cos.py --list                  # 列出桶内已有对象
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
OUTDIR = CONFIG.get("outdir") or HERE

PREFIX = os.environ.get("COS_PREFIX", "aic-rank").strip("/")
REGION = os.environ.get("COS_REGION", "")
BUCKET = os.environ.get("COS_BUCKET", "")


def env_ready():
    missing = [k for k in ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION", "COS_BUCKET")
               if not os.environ.get(k)]
    return missing


def make_client():
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        print("缺少 SDK：python3 -m pip install cos-python-sdk-v5", file=sys.stderr)
        raise SystemExit(2)
    kw = dict(Region=os.environ["COS_REGION"],
              SecretId=os.environ["COS_SECRET_ID"],
              SecretKey=os.environ["COS_SECRET_KEY"],
              Scheme="https", Timeout=15)
    if os.environ.get("COS_TOKEN"):          # 临时密钥（STS）
        kw["Token"] = os.environ["COS_TOKEN"]
    return CosS3Client(CosConfig(**kw))


def build_tasks():
    """返回 [(本地路径, 对象键, Content-Type, Cache-Control)]"""
    tasks = []
    rep = os.path.join(OUTDIR, "report", "index.html")
    if os.path.exists(rep):
        tasks.append((rep, "index.html", "text/html; charset=utf-8", "no-cache, max-age=0"))

    latest = os.path.join(OUTDIR, "data", "latest.json")
    if os.path.exists(latest):
        tasks.append((latest, "data/latest.json", "application/json; charset=utf-8", "no-cache, max-age=0"))

    for p in sorted(glob.glob(os.path.join(OUTDIR, "data", "snapshots", "*", "*.json"))):
        day = os.path.basename(os.path.dirname(p))
        tasks.append((p, "data/snapshots/%s/%s" % (day, os.path.basename(p)),
                      "application/json; charset=utf-8", "public, max-age=3600"))

    for p in sorted(glob.glob(os.path.join(OUTDIR, "data", "export", "*.csv"))):
        tasks.append((p, "data/export/" + os.path.basename(p), "text/csv; charset=utf-8", "public, max-age=600"))
    return tasks


def do_check(client, prefix):
    """两级自检：
    ① 服务级 list_buckets（走 service.cos.myqcloud.com，与桶地域无关）→ 验证密钥与签名
    ② 桶级 head_bucket / head_object → 验证对目标桶的权限与网络可达性
    """
    from qcloud_cos.cos_exception import CosClientError, CosServiceError

    print("① 密钥校验（service.cos.myqcloud.com）…")
    key_ok = False
    try:
        r = client.list_buckets()
        key_ok = True
        buckets = r.get("Buckets", {}).get("Bucket", [])
        print("   ✔ 密钥与签名有效；账号下共 %d 个桶：" % len(buckets))
        for b in buckets[:10]:
            loc = b.get("Location", "?")
            flag = "  ← 当前使用的桶" if b.get("Name") == BUCKET else ""
            print("     - %s  地域=%s%s" % (b.get("Name"), loc, flag))
        hit = [b for b in buckets if b.get("Name") == BUCKET]
        if buckets and not hit:
            print("   ⚠ 列表里没有 %s，请核对桶名是否漏了 APPID 后缀" % BUCKET)
        if hit and hit[0].get("Location") and hit[0]["Location"] != REGION:
            print("   ⚠ 桶真实地域是 %s，但 COS_REGION 填的是 %s，需要改成前者"
                  % (hit[0]["Location"], REGION))
    except CosServiceError as e:
        code = e.get_error_code()
        if code in ("AccessDenied", "UnauthorizedOperation", "AuthFailure"):
            key_ok = True          # 签名通过但无该权限，说明密钥本身是对的
            print("   ✔ 密钥与签名有效（无 cos:GetService 权限，属正常）")
        else:
            print("   ✘ 密钥校验失败：%s %s" % (code, e.get_error_message()))
            print("     → 检查 COS_SECRET_ID / COS_SECRET_KEY 是否抄写完整")
            return 1
    except CosClientError as e:
        print("   ✘ 网络异常：%s" % e)
    except Exception as e:  # noqa: BLE001
        print("   ✘ 未知错误：%s" % e)

    print("② 目标桶校验（%s / %s）…" % (BUCKET, REGION))
    try:
        client.head_bucket(Bucket=BUCKET)
        print("   ✔ 桶可访问：HeadBucket 成功")
        print("\n结论：可以上传。执行  python3 upload_cos.py  开始。")
        return 0
    except CosServiceError as e:
        code = e.get_error_code()
        if code in ("AccessDenied", "UnauthorizedOperation"):
            print("   ✔ 桶存在、鉴权通过，但当前策略没有 cos:HeadBucket 权限（不影响上传）")
            print("\n结论：可以上传。执行  python3 upload_cos.py  开始。")
            return 0
        if code in ("NoSuchBucket", "BucketNotFound"):
            print("   ✘ 桶不存在：%s" % e.get_error_message())
            print("     → 核对 COS_BUCKET 是否为「桶名-APPID」完整形式")
            return 1
        print("   ✘ 桶级错误：%s %s" % (code, e.get_error_message()))
        return 1
    except CosClientError as e:
        print("   ✘ 连不上该地域的端点：%s" % e)
        print("     → 说明本机网络到 cos.%s.myqcloud.com 不通。" % REGION)
        print("       实测：上海/北京/南京/成都端点可达，广州端点超时。")
        print("       建议在可达地域新建存储桶，并把 COS_REGION / COS_BUCKET 改成新值。")
        return 1
    except Exception as e:  # noqa: BLE001
        print("   ✘ 未知错误：%s" % e)
        return 1
    finally:
        print("   （上传前缀 %s/）" % prefix)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验凭证与桶")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default=None, choices=["report", "snapshot", "csv"])
    args = ap.parse_args()

    missing = env_ready()
    if missing:
        print("缺少环境变量: %s" % ", ".join(missing))
        print("在 Minis 设置 → 环境变量 里补齐后重试。")
        return 2

    client = make_client()

    if args.check:
        return do_check(client, PREFIX)

    if args.list:
        try:
            r = client.list_objects(Bucket=BUCKET, Prefix=PREFIX + "/", MaxKeys=1000)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "get_error_code", lambda: "")()
            if code in ("AccessDenied", "Unauthorized"):
                print("当前策略没有「桶枚举」（cos:GetBucket）权限，无法列目录；"
                      "这不影响上传，用 --check 自检即可。")
                return 0
            raise
        for c in (r.get("Contents") or []):
            print("%10s  %s" % (c.get("Size"), c["Key"]))
        return 0

    tasks = build_tasks()
    if args.only == "report":
        tasks = [t for t in tasks if t[1].endswith("index.html")]
    elif args.only == "snapshot":
        tasks = [t for t in tasks if "/snapshots/" in t[1]]
    elif args.only == "csv":
        tasks = [t for t in tasks if t[1].endswith(".csv")]

    ok = 0
    for local, key, ctype, cache in tasks:
        size = os.path.getsize(local)
        obj_key = "%s/%s" % (PREFIX, key)
        if args.dry_run:
            print("[dry-run] %6d B  %s  →  %s  (%s)" % (size, os.path.relpath(local, OUTDIR), obj_key, ctype))
            continue
        try:
            client.put_object(Bucket=BUCKET, Key=obj_key, Body=open(local, "rb"),
                              ContentType=ctype, CacheControl=cache)
            ok += 1
            print("↑ %6d B  %s" % (size, obj_key))
        except Exception as e:  # noqa: BLE001
            print("✘ 上传失败 %s: %s" % (obj_key, e), file=sys.stderr)

    if not args.dry_run:
        print("完成：%d/%d 个对象已上传" % (ok, len(tasks)))
        print("访问地址（桶为公有读时）: https://%s.cos.%s.myqcloud.com/%s/index.html" % (BUCKET, REGION, PREFIX))
    return 0 if ok == len(tasks) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
