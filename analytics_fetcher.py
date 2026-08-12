"""Instagram インサイト測定（@あなたのユーザーネーム）。

Graph API から
  - アカウント指標（followers_count / follows_count / media_count + 取れれば reach）
  - 各投稿の指標（いいね / コメント / リーチ / 保存 / シェア / 表示）
を取得し、`analytics/` に時系列（JSONL）で1行ずつ追記する。週次ワークフローから実行。

Instagram インサイト（Graph API）から数字を取得する。ポイントは3つ：

  1. ⚠️ **ベース URL**。流用元は graph.instagram.com（Instagram ログイン方式）だが、
     @あなたのユーザーネーム は「Facebook ログインによる API 設定」で取得した
     **Page トークン + instagram_business_account ID** 方式のため
     **graph.facebook.com** を使う（instagram_poster.py と同じ）。
  2. **A/B テストのスロット判定**を追加。投稿時刻（JST）から "08:00" / "12:30" を
     判定して各レコードに `slot` として持たせる。cron-job.org の2ジョブ
     （8:00 / 12:30 JST）のどちらが効いているかを、後から集計できるようにするため。
  3. **バックフィルモード**。`ANALYTICS_BACKFILL=true` で全投稿を遡って取得する
     （ページネーション対応）。稼働開始以降の全データを一度で取り込む用。

必要な環境変数（投稿ワークフローと同じものを流用）:
  - INSTAGRAM_ACCESS_TOKEN   永続ページトークン
  - INSTAGRAM_ACCOUNT_ID     IG User ID（@あなたのユーザーネーム）
任意:
  - ANALYTICS_BACKFILL       "true" で全投稿を遡る（既定 false ＝直近14日のみ）
  - GRAPH_API_BASE           Graph API ベース URL の上書き（既定 graph.facebook.com）
  - GRAPH_API_VERSION        API バージョン（既定 v21.0）

インサイト取得にはトークンに instagram_manage_insights 相当の権限が必要。
権限不足や指標名の変更で個別指標が取れなくても、**その指標だけスキップして続行**する
（取れた分だけ記録し、レポート側で欠損に強く作る）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
# @あなたのユーザーネーム は Page トークン方式のため graph.facebook.com を既定にする。
GRAPH_API_HOST = os.environ.get("GRAPH_API_BASE", "https://graph.facebook.com")
GRAPH_BASE = f"{GRAPH_API_HOST.rstrip('/')}/{GRAPH_API_VERSION}"

ROOT = Path(__file__).resolve().parent
ANALYTICS_DIR = ROOT / "analytics"
ACCOUNT_HISTORY = ANALYTICS_DIR / "account_history.jsonl"
MEDIA_HISTORY = ANALYTICS_DIR / "media_history.jsonl"

# 通常運用で指標を取りに行く範囲。古い投稿はエンゲージが固まっており
# 毎週取り直す必要が薄いので、直近だけにして API 呼び出しを節約する。
MEDIA_LOOKBACK_DAYS = 14
MEDIA_LIST_LIMIT = 50
# バックフィル時の安全弁（ページネーションの無限ループ防止）
MAX_PAGES = 40

JST = timezone(timedelta(hours=9))

# media_product_type ごとに取得を試みる insights 指標。
# まとめて1回で叩き、ダメなら1指標ずつ叩き直す（指標名は API バージョンで変わるため）。
INSIGHT_METRICS = {
    "REELS": ["reach", "saved", "shares", "total_interactions", "views"],
    "FEED": ["reach", "saved", "shares", "total_interactions", "views"],
    "CAROUSEL_CONTAINER": ["reach", "saved", "shares", "total_interactions"],
    "STORY": ["reach", "replies", "shares", "total_interactions"],
    "_default": ["reach", "saved", "shares", "total_interactions"],
}


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _get(path: str, params: dict) -> dict:
    url = f"{GRAPH_BASE}/{path}"
    res = requests.get(url, params=params, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"GET {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


def fetch_account(token: str, ig_id: str) -> dict:
    data = _get(ig_id, {
        "fields": "username,followers_count,follows_count,media_count",
        "access_token": token,
    })
    return {
        "username": data.get("username"),
        "followers_count": data.get("followers_count"),
        "follows_count": data.get("follows_count"),
        "media_count": data.get("media_count"),
    }


def fetch_account_reach(token: str, ig_id: str) -> int | None:
    """直近7日のアカウント reach。取れなければ None（権限/仕様差に強く）。

    API バージョンによって `period` 方式と `metric_type=total_value` 方式が
    混在するため、順に試して最初に取れたものを使う。
    """
    attempts = (
        {"metric": "reach", "period": "week", "access_token": token},
        {"metric": "reach", "period": "day", "metric_type": "total_value",
         "access_token": token},
        {"metric": "reach", "period": "days_28", "access_token": token},
    )
    for params in attempts:
        try:
            data = _get(f"{ig_id}/insights", params)
            rows = data.get("data", [])
            if not rows:
                continue
            row = rows[0]
            if row.get("values"):
                return row["values"][-1].get("value")
            if "total_value" in row:
                return row["total_value"].get("value")
        except Exception as e:  # noqa: BLE001
            print(f"   （account reach {params.get('period')} 取得不可: {e}）")
    return None


def fetch_media(token: str, ig_id: str, *, backfill: bool) -> list[dict]:
    """投稿一覧を取得する。backfill=True なら次ページを辿って全件取る。"""
    fields = ("id,caption,media_type,media_product_type,timestamp,"
              "permalink,like_count,comments_count")
    data = _get(f"{ig_id}/media", {
        "fields": fields,
        "limit": MEDIA_LIST_LIMIT,
        "access_token": token,
    })
    items = list(data.get("data", []))
    if not backfill:
        return items

    pages = 1
    next_url = (data.get("paging") or {}).get("next")
    while next_url and pages < MAX_PAGES:
        res = requests.get(next_url, timeout=30)
        if res.status_code != 200:
            print(f"   （ページネーション中断: {res.status_code}）")
            break
        page = res.json()
        rows = page.get("data", [])
        if not rows:
            break
        items.extend(rows)
        next_url = (page.get("paging") or {}).get("next")
        pages += 1
    print(f"   投稿一覧 {len(items)} 件（{pages} ページ）")
    return items


# 投稿指標の失敗は件数ぶん出ると煩いので握り潰すが、**最初の1件だけは必ず表示**する。
# これが無いと「権限が無い」のか「指標名が変わった」のかがログから切り分けられない。
_insight_error_shown = False


def _report_insight_error(scope: str, err: Exception) -> None:
    global _insight_error_shown
    if not _insight_error_shown:
        _insight_error_shown = True
        print(f"   ⚠️ 投稿指標の取得に失敗（{scope}・以降は同種のエラーを抑制）: {err}")


def fetch_media_insights(token: str, media_id: str, product_type: str) -> dict:
    """指標をまとめて1回で取り、失敗したら1指標ずつ取り直す。"""
    metrics = INSIGHT_METRICS.get(product_type, INSIGHT_METRICS["_default"])
    out: dict[str, int] = {}

    def _absorb(rows: list[dict]) -> None:
        for row in rows:
            name = row.get("name")
            if not name:
                continue
            if row.get("values"):
                out[name] = row["values"][0].get("value")
            elif "total_value" in row:
                out[name] = row["total_value"].get("value")

    try:
        data = _get(f"{media_id}/insights", {
            "metric": ",".join(metrics), "access_token": token,
        })
        _absorb(data.get("data", []))
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        _report_insight_error("まとめて取得", e)  # → 1指標ずつへフォールバック

    for metric in metrics:
        if metric in out:
            continue
        try:
            data = _get(f"{media_id}/insights", {
                "metric": metric, "access_token": token,
            })
            _absorb(data.get("data", []))
        except Exception as e:  # noqa: BLE001
            _report_insight_error(f"metric={metric}", e)
            continue  # この指標は取れない → スキップ
    return out


def _parse_created(ts_raw: str | None) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("+0000", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def classify_slot(created: datetime | None) -> str:
    """投稿時刻（JST）から A/B テストのスロットを判定する。

    cron-job.org は 8:00 / 12:30 JST に発火するが、Actions の実行と Meta 側の
    処理で数分〜十数分ずれる。前後に幅を持たせて拾う。
    手動投稿・告知投稿はどちらにも入らないので "other" になる。
    """
    if created is None:
        return "unknown"
    jst = created.astimezone(JST)
    minutes = jst.hour * 60 + jst.minute
    if 7 * 60 + 30 <= minutes <= 9 * 60 + 30:      # 07:30–09:30
        return "08:00"
    if 12 * 60 <= minutes <= 13 * 60 + 30:         # 12:00–13:30
        return "12:30"
    return "other"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    ig_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    missing = [k for k, v in {
        "INSTAGRAM_ACCESS_TOKEN": token,
        "INSTAGRAM_ACCOUNT_ID": ig_id,
    }.items() if not v]
    if missing:
        print(f"❌ 環境変数が未設定: {', '.join(missing)}")
        sys.exit(1)

    backfill = _truthy(os.environ.get("ANALYTICS_BACKFILL"))
    now = datetime.now(timezone.utc)
    run_ts = now.isoformat()
    run_date = now.astimezone(JST).strftime("%Y-%m-%d")
    print(f"🔗 {GRAPH_BASE}  /  モード: {'バックフィル（全投稿）' if backfill else '通常（直近14日）'}")

    print("📊 アカウント指標を取得中...")
    account = fetch_account(token, ig_id)
    account["reach_7d"] = fetch_account_reach(token, ig_id)
    account.update({"ts": run_ts, "date": run_date, "source": "api"})
    _append_jsonl(ACCOUNT_HISTORY, account)
    print(f"   followers={account.get('followers_count')} "
          f"media={account.get('media_count')} reach_7d={account.get('reach_7d')}")

    print("📈 投稿指標を取得中...")
    cutoff = now - timedelta(days=MEDIA_LOOKBACK_DAYS)
    media = fetch_media(token, ig_id, backfill=backfill)
    recorded = 0
    slot_count: dict[str, int] = {}
    for m in media:
        ts_raw = m.get("timestamp")
        created = _parse_created(ts_raw)
        if not backfill and (created or now) < cutoff:
            continue
        product_type = m.get("media_product_type") or m.get("media_type") or "_default"
        insights = fetch_media_insights(token, m["id"], product_type)
        caption = (m.get("caption") or "").strip().replace("\n", " ")
        jst_created = created.astimezone(JST) if created else None
        slot = classify_slot(created)
        slot_count[slot] = slot_count.get(slot, 0) + 1
        record = {
            "ts": run_ts,
            "date": run_date,
            "media_id": m.get("id"),
            "created_at": ts_raw,
            # ↓ A/B テスト集計用（レポート側はこの3つだけ見れば済む）
            "created_date_jst": jst_created.strftime("%Y-%m-%d") if jst_created else None,
            "created_time_jst": jst_created.strftime("%H:%M") if jst_created else None,
            "weekday_jst": jst_created.strftime("%a") if jst_created else None,
            "slot": slot,
            "media_type": m.get("media_type"),
            "media_product_type": m.get("media_product_type"),
            "permalink": m.get("permalink"),
            "caption_head": caption[:80],
            "like_count": m.get("like_count"),
            "comments_count": m.get("comments_count"),
            **insights,
        }
        _append_jsonl(MEDIA_HISTORY, record)
        recorded += 1

    scope = "全期間" if backfill else f"直近{MEDIA_LOOKBACK_DAYS}日"
    slots = " / ".join(f"{k}={v}" for k, v in sorted(slot_count.items()))
    print(f"   {recorded} 件の投稿指標を記録（{scope}）  スロット内訳: {slots or '―'}")
    print(f"\n✅ 測定完了: {ACCOUNT_HISTORY.name} / {MEDIA_HISTORY.name}")


if __name__ == "__main__":
    main()
