#!/usr/bin/env python3
"""
セカスト新着ウォッチャー

- 設定に書いたキーワード（ブランド）ごとに、セカスト オンラインストアの
  新着順検索結果（1ページ目・60件）をチェックします。
- 前回までに見ていない商品で、上限価格以下のものを Discord に通知します。
- 新着を見つけた時刻（日本時間）を CSV に記録し、--report で集計します。

使い方:
  python sekasuto_watch.py                       # ずっと動かし続ける（PC用）
  python sekasuto_watch.py --once                # 1回だけチェック（GitHub Actions / cron 用）
  python sekasuto_watch.py --test                # Discord へのテスト通知
  python sekasuto_watch.py --report              # 新着の時間帯・曜日を集計して画面に表示
  python sekasuto_watch.py --report --to-discord # 集計結果と記録CSVを Discord に送る

設定の読み込み先（上が優先）:
  環境変数 CONFIG_JSON（GitHub Actions のシークレット用） → config.json
  Discord の Webhook URL は環境変数 DISCORD_WEBHOOK_URL があればそちらを使います。
状態ファイルの置き場所は環境変数 STATE_DIR で変えられます（既定はこのフォルダ）。
環境変数 MINIMAL_LOG=1 のときは、画面にブランド名や商品名を出しません
（公開リポジトリの実行ログに監視内容が残らないようにするため）。

Python 3.8 以上、標準ライブラリのみで動きます。
"""

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://www.2ndstreet.jp"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_DIR = Path(os.environ.get("STATE_DIR") or HERE)
if not STATE_DIR.is_absolute():
    STATE_DIR = Path.cwd() / STATE_DIR
STATE_PATH = STATE_DIR / "seen.json"
LOG_PATH = STATE_DIR / "new_items_log.csv"
MINIMAL_LOG = os.environ.get("MINIMAL_LOG") == "1"

JST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LOG_FIELDS = [
    "first_seen", "weekday", "hour", "watch", "goods_id", "shop_id",
    "brand", "name", "size", "condition", "price", "notified", "url",
]

WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def now_jst():
    return datetime.now(JST)


# ---------------------------------------------------------------- 設定・状態

def load_config():
    raw = os.environ.get("CONFIG_JSON", "").strip()
    if raw:
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"CONFIG_JSON の書き方に誤りがあります（{e}）")
    elif CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        sys.exit(
            "設定が見つかりません。config.example.json をコピーして config.json を作るか、"
            "GitHub のシークレット CONFIG_JSON に設定を入れてください。"
        )
    env_hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env_hook:
        cfg["discord_webhook_url"] = env_hook
    if not str(cfg.get("discord_webhook_url", "")).startswith("https://"):
        cfg["discord_webhook_url"] = ""
    cfg.setdefault("interval_minutes", 10)
    cfg.setdefault("quiet_hours", [])          # 例: [1, 2, 3, 4, 5, 6] はチェックしない（日本時間）
    cfg.setdefault("mercari_fee_rate", 0.10)
    cfg.setdefault("shipping_cost", 750)
    cfg.setdefault("global_exclude", [])
    if not cfg.get("watches"):
        sys.exit("設定の watches が空です。監視するブランドを書いてください。")
    for w in cfg["watches"]:
        w.setdefault("label", w["keyword"])
    return cfg


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    # 1キーワードあたり直近 600 件だけ覚えておけば十分
    for k, ids in state.items():
        state[k] = ids[-600:]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    tmp.replace(STATE_PATH)


def say(msg_full, msg_minimal):
    print(msg_minimal if MINIMAL_LOG else msg_full)


# ---------------------------------------------------------------- 取得・解析

def search_url(keyword):
    q = urllib.parse.urlencode({"keyword": keyword, "sortBy": "arrival"})
    return f"{BASE}/search?{q}"


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def _field(block, cls):
    m = re.search(r'<p class="[^"]*\b' + cls + r'\b[^"]*">(.*?)</p>', block, re.S)
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_items(page):
    items = []
    blocks = re.split(r'(?=<li[^>]*class="[^"]*\bitemCard\b)', page)
    for block in blocks[1:]:
        link = re.search(r'href="(/goods/detail/goodsId/(\d+)/shopsId/(\d+)[^"]*)"', block)
        if not link:
            continue
        price_text = _field(block, "itemCard_price")
        prices = [int(p.replace(",", "")) for p in re.findall(r"¥\s*([\d,]+)", price_text)]
        img = re.search(r'<img[^>]+src="([^"]+)"', block)
        items.append({
            "url": BASE + link.group(1),
            "goods_id": link.group(2),
            "shop_id": link.group(3),
            "brand": _field(block, "itemCard_brand"),
            "name": _field(block, "itemCard_name"),
            "size": _field(block, "itemCard_size"),
            "condition": _field(block, "itemCard_status").replace("商品の状態 :", "").strip(),
            # 値下げ表示があると価格が2つ並ぶので、安い方（現在価格）を採用
            "price": min(prices) if prices else None,
            "image": img.group(1) if img else "",
        })
    return items


# ---------------------------------------------------------------- 判定

def matches(item, watch, cfg):
    if item["price"] is None:
        return False, "価格不明"
    if item["price"] > watch["max_price"]:
        return False, "上限超え"
    want_brand = watch.get("brand_contains")
    if want_brand and want_brand.lower() not in item["brand"].lower():
        return False, "別ブランド"
    text = f'{item["brand"]} {item["name"]}'.lower()
    for word in cfg["global_exclude"] + watch.get("exclude", []):
        if word.lower() in text:
            return False, f"除外ワード「{word}」"
    include = watch.get("include_any")
    if include and not any(w.lower() in text for w in include):
        return False, "必須ワードなし"
    return True, ""


def profit_estimate(item, watch, cfg):
    sell = watch.get("expected_sell_price")
    if not sell:
        return None
    return int(sell * (1 - cfg["mercari_fee_rate"]) - cfg["shipping_cost"] - item["price"])


# ---------------------------------------------------------------- 通知

def notify_discord(webhook, item, watch, profit):
    margin = watch["max_price"] - item["price"]
    fields = [
        {"name": "価格", "value": f'¥{item["price"]:,}', "inline": True},
        {"name": "状態", "value": item["condition"] or "-", "inline": True},
        {"name": "サイズ", "value": item["size"] or "-", "inline": True},
        {"name": "上限まで", "value": f"¥{margin:,} 安い", "inline": True},
    ]
    if profit is not None:
        sign = "+" if profit >= 0 else "-"
        fields.append({"name": "想定利益", "value": f"{sign}¥{abs(profit):,}", "inline": True})
    embed = {
        "title": f'{item["brand"]} {item["name"]}'[:250],
        "url": item["url"],
        "description": f'監視: {watch["label"]}',
        "fields": fields,
        "color": 0x2E7D32 if (profit or 0) >= 0 else 0x9E9E9E,
    }
    if item["image"]:
        embed["thumbnail"] = {"url": item["image"]}
    send_discord(webhook, {"username": "セカスト新着", "embeds": [embed]})


def _post(req):
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:            # 送りすぎ → 少し待って再送
                time.sleep(2 + attempt * 2)
                continue
            raise


def send_discord(webhook, payload):
    data = json.dumps(payload).encode("utf-8")
    _post(urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    ))


def send_discord_file(webhook, content, file_path):
    boundary = uuid.uuid4().hex
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps({"username": "セカスト新着", "content": content}).encode("utf-8"),
        f"\r\n--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="files[0]"; filename="{Path(file_path).name}"\r\n'.encode(),
        b"Content-Type: text/csv\r\n\r\n",
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    _post(urllib.request.Request(
        webhook, data=b"".join(parts),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
    ))


# ---------------------------------------------------------------- 記録

def append_log(rows):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- 1回分のチェック

def check_once(cfg, state):
    now = now_jst()
    webhook = cfg.get("discord_webhook_url", "")
    total_new = total_hits = 0

    for i, watch in enumerate(cfg["watches"]):
        key = watch["keyword"]
        tag = f"#{i + 1}"
        try:
            items = parse_items(fetch(search_url(key)))
        except Exception as e:
            say(f"[{now:%H:%M}] {key}: 取得失敗 ({e})", f"[{now:%H:%M}] {tag}: 取得失敗 ({type(e).__name__})")
            continue

        if not items:
            say(f"[{now:%H:%M}] {key}: 商品が読み取れませんでした（サイトの作りが変わった可能性）",
                f"[{now:%H:%M}] {tag}: 商品が読み取れませんでした")
            continue

        first_run = key not in state
        seen = set(state.get(key, []))
        new_items = [it for it in items if it["goods_id"] not in seen]

        rows = []
        for it in new_items:
            ok, _reason = matches(it, watch, cfg)
            notified = False
            if ok and not first_run and webhook:
                try:
                    notify_discord(webhook, it, watch, profit_estimate(it, watch, cfg))
                    notified = True
                    total_hits += 1
                    time.sleep(1)
                except Exception as e:
                    print(f"  通知失敗: {type(e).__name__}")
            if not first_run:
                rows.append({
                    "first_seen": now.strftime("%Y-%m-%d %H:%M"),
                    "weekday": WEEKDAYS_JA[now.weekday()],
                    "hour": now.hour,
                    "watch": watch["label"],
                    "goods_id": it["goods_id"], "shop_id": it["shop_id"],
                    "brand": it["brand"], "name": it["name"], "size": it["size"],
                    "condition": it["condition"], "price": it["price"],
                    "notified": "yes" if notified else ("hit" if ok else "no"),
                    "url": it["url"],
                })

        if rows:
            append_log(rows)
        state[key] = state.get(key, []) + [it["goods_id"] for it in new_items]

        if first_run:
            say(f"[{now:%H:%M}] {key}: 初回のため現在の {len(items)} 件を記憶しました（通知なし）",
                f"[{now:%H:%M}] {tag}: 初回のため {len(items)} 件を記憶（通知なし）")
        else:
            total_new += len(new_items)
            say(f"[{now:%H:%M}] {key}: 新着 {len(new_items)} 件",
                f"[{now:%H:%M}] {tag}: 新着 {len(new_items)} 件")

        if i < len(cfg["watches"]) - 1:
            time.sleep(3 + random.random() * 3)

    save_state(state)
    print(f"完了: 新着 {total_new} 件 / 通知 {total_hits} 件")
    return total_new, total_hits


# ---------------------------------------------------------------- 集計

def build_report():
    if not LOG_PATH.exists():
        return None
    with open(LOG_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None

    by_watch = Counter(r["watch"] for r in rows)
    # 複数の監視条件で同じ商品を拾った場合は1件として数える
    unique = {}
    for r in rows:
        unique.setdefault(r["goods_id"], r)
    hit_ids = {r["goods_id"] for r in rows if r["notified"] in ("yes", "hit")}
    rows = list(unique.values())
    hits = [r for r in rows if r["goods_id"] in hit_ids]
    by_hour = Counter(int(r["hour"]) for r in rows)
    by_day = Counter(r["weekday"] for r in rows)
    days = len({r["first_seen"][:10] for r in rows})
    peak = max(by_hour.values())

    lines = [
        f"記録期間: {rows[0]['first_seen'][:10]} 〜 {rows[-1]['first_seen'][:10]}（{days}日分）",
        f"新着の総数: {len(rows)} 件 / うち上限価格以下: {len(hits)} 件",
        "",
        "■ 時間帯別の新着数（日本時間）",
    ]
    for h in range(24):
        n = by_hour.get(h, 0)
        bar = "█" * round(n / peak * 20) if peak else ""
        lines.append(f"{h:2d}時 {n:4d} {bar}")
    lines += ["", "■ 曜日別の新着数"]
    lines.append("  ".join(f"{d}:{by_day.get(d, 0)}" for d in WEEKDAYS_JA))
    lines += ["", "■ ブランド別の新着数"]
    lines += [f"{w}: {n}" for w, n in by_watch.most_common()]
    return "\n".join(lines)


def report(to_discord):
    text = build_report()
    if text is None:
        msg = "まだ新着の記録がありません。しばらく動かしてから実行してください。"
        if to_discord:
            cfg = load_config()
            if cfg["discord_webhook_url"]:
                send_discord(cfg["discord_webhook_url"], {"username": "セカスト新着", "content": msg})
        sys.exit(msg)

    if not to_discord:
        print(text)
        return

    cfg = load_config()
    if not cfg["discord_webhook_url"]:
        sys.exit("Discord の Webhook URL が設定されていません。")
    send_discord_file(cfg["discord_webhook_url"], "```\n" + text[:1900] + "\n```", LOG_PATH)
    print("集計結果と記録CSVを Discord に送りました。")


# ---------------------------------------------------------------- 起動

def main():
    ap = argparse.ArgumentParser(description="セカスト新着ウォッチャー")
    ap.add_argument("--once", action="store_true", help="1回だけチェックして終了")
    ap.add_argument("--test", action="store_true", help="Discord にテスト通知を送る")
    ap.add_argument("--report", action="store_true", help="新着の時間帯・曜日を集計")
    ap.add_argument("--to-discord", action="store_true", help="--report の結果を Discord に送る")
    args = ap.parse_args()

    if args.report:
        report(args.to_discord)
        return

    cfg = load_config()

    if args.test:
        if not cfg["discord_webhook_url"]:
            sys.exit("Discord の Webhook URL が設定されていません。")
        send_discord(cfg["discord_webhook_url"], {
            "username": "セカスト新着",
            "content": "テスト通知です。この通知が届いていれば設定は完了です。",
        })
        print("テスト通知を送りました。Discord を確認してください。")
        return

    state = load_state()

    if args.once:
        if now_jst().hour in cfg["quiet_hours"]:
            print("チェックしない時間帯のためスキップしました。")
            return
        check_once(cfg, state)
        return

    print(f"監視を開始します（{len(cfg['watches'])} ブランド、{cfg['interval_minutes']} 分おき）。止めるには Ctrl+C")
    while True:
        if now_jst().hour in cfg["quiet_hours"]:
            time.sleep(300)
            continue
        try:
            check_once(cfg, state)
        except Exception as e:
            print(f"エラー: {e}（次の回で再試行します）")
        wait = cfg["interval_minutes"] * 60 * (0.9 + random.random() * 0.2)
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n停止しました。")
