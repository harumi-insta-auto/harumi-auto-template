"""週次レポート生成（測定 → 分析 → 改善提案）。@あなたのユーザーネーム 用。

`analytics/*.jsonl` を集計し、
  1) 決定論的なデータセクション
     （アカウント推移・**A/Bテスト 8:00 vs 12:30**・曜日別・投稿ランキング）
  2) Claude による「今週の所見と改善提案」
を1つの markdown にまとめて
  - analytics/latest_report.md       （最新版・メール本文にも使う）
  - analytics/reports/YYYY-MM-DD.md  （週次アーカイブ）
へ書き出す。ANTHROPIC_API_KEY が無い場合は所見セクションをスキップして続行。

週次レポートを生成する。設計上のポイントは
**A/Bテスト（投稿スロット）セクションを主役に据えた**こと。@あなたのユーザーネーム は
cron-job.org の2ジョブ（8:00 / 12:30 JST）で毎日2本出しており、
「どちらの時間帯が効くのか」に決着をつけるのが観測の第一目的だから。
投稿タイプはほぼ FEED 一択なのでタイプ別集計は従属的な扱いにしてある。

改善は「提案」まで。反映は人が読んで判断する（自動フィードバックはしない）。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ANALYTICS_DIR = ROOT / "analytics"
ACCOUNT_HISTORY = ANALYTICS_DIR / "account_history.jsonl"
MEDIA_HISTORY = ANALYTICS_DIR / "media_history.jsonl"
LATEST_REPORT = ANALYTICS_DIR / "latest_report.md"
REPORTS_DIR = ANALYTICS_DIR / "reports"

JST = timezone(timedelta(hours=9))

SLOT_JP = {
    "08:00": "朝 8:00 枠",
    "12:30": "昼 12:30 枠",
    "other": "その他（告知・手動）",
    "unknown": "時刻不明",
}
PRODUCT_TYPE_JP = {
    "FEED": "フィード画像",
    "REELS": "リール",
    "STORY": "ストーリー",
    "CAROUSEL_CONTAINER": "カルーセル",
}
WEEKDAY_JP = {
    "Mon": "月", "Tue": "火", "Wed": "水", "Thu": "木",
    "Fri": "金", "Sat": "土", "Sun": "日",
}


# --------------------------------------------------------------------------
# 読み込みと小道具
# --------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("+0000", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def post_engagement(p: dict) -> int:
    """反応の総量。total_interactions が取れていればそれ、無ければ内訳の合計。"""
    ti = _num(p.get("total_interactions"))
    if ti is not None:
        return int(ti)
    s = 0
    for k in ("like_count", "comments_count", "saved", "shares"):
        v = _num(p.get(k))
        if v is not None:
            s += v
    return int(s)


def _rate(numer, denom) -> float | None:
    n, d = _num(numer), _num(denom)
    if n is None or d is None or d == 0:
        return None
    return n / d


def _pct(numer, denom) -> str:
    r = _rate(numer, denom)
    return f"{r * 100:.1f}%" if r is not None else "―"


def _avg(xs: list) -> float | None:
    vals = [x for x in (_num(v) for v in xs) if x is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v, digits: int = 0) -> str:
    n = _num(v)
    if n is None:
        return "―"
    return f"{n:,.{digits}f}"


def _fmt_delta(now, prev) -> str:
    n, p = _num(now), _num(prev)
    if n is None:
        return "―"
    if p is None:
        return f"{int(n):,}"
    return f"{int(n):,} ({int(n - p):+,})"


def latest_per_media(media_rows: list[dict]) -> dict[str, dict]:
    """media_id ごとに最新スナップショット（ts 最大）を返す。"""
    best: dict[str, dict] = {}
    for r in media_rows:
        mid = r.get("media_id")
        if not mid:
            continue
        if mid not in best or (r.get("ts", "") > best[mid].get("ts", "")):
            best[mid] = r
    return best


def weekly_trend(account_rows: list[dict], weeks: int = 8) -> list[dict]:
    """日付ごとに最新スナップショットへ畳み込み、古い順に直近 weeks 件を返す。"""
    by_date: dict[str, dict] = {}
    for r in sorted([x for x in account_rows if x.get("ts")], key=lambda x: x["ts"]):
        d = r.get("date") or (r["ts"][:10])
        by_date[d] = r  # 同日複数回計測なら最後（＝最新）で上書き
    return [by_date[k] for k in sorted(by_date)][-weeks:]


def account_delta(account_rows: list[dict]) -> tuple[dict | None, dict | None]:
    """最新スナップショットと、約7日前に最も近い過去スナップショットを返す。"""
    rows = sorted([r for r in account_rows if r.get("ts")], key=lambda r: r["ts"])
    if not rows:
        return None, None
    latest = rows[-1]
    latest_dt = _parse_dt(latest["ts"])
    prev = None
    if latest_dt:
        target = latest_dt - timedelta(days=6)
        candidates = [r for r in rows[:-1] if (_parse_dt(r["ts"]) or latest_dt) <= target]
        if candidates:
            prev = candidates[-1]
        elif len(rows) > 1:
            prev = rows[0]
    return latest, prev


def _slot_of(p: dict) -> str:
    """古い行（slot 未記録）でも created_at から復元できるようにする。"""
    slot = p.get("slot")
    if slot:
        return slot
    created = _parse_dt(p.get("created_at"))
    if created is None:
        return "unknown"
    jst = created.astimezone(JST)
    minutes = jst.hour * 60 + jst.minute
    if 7 * 60 + 30 <= minutes <= 9 * 60 + 30:
        return "08:00"
    if 12 * 60 <= minutes <= 13 * 60 + 30:
        return "12:30"
    return "other"


def _is_weekend(p: dict) -> bool | None:
    wd = p.get("weekday_jst")
    if not wd:
        created = _parse_dt(p.get("created_at"))
        if created is None:
            return None
        wd = created.astimezone(JST).strftime("%a")
    return wd in ("Sat", "Sun")


def _slot_stats(posts: list[dict]) -> dict:
    """1グループ分の集計。リーチが欠損していても件数と反応は出す。"""
    reaches = [p.get("reach") for p in posts]
    sum_reach = sum(x for x in (_num(r) for r in reaches) if x is not None)
    sum_eng = sum(post_engagement(p) for p in posts)
    sum_saved = sum(x for x in (_num(p.get("saved")) for p in posts) if x is not None)
    return {
        "count": len(posts),
        "avg_reach": _avg(reaches),
        "avg_like": _avg([p.get("like_count") for p in posts]),
        "avg_comment": _avg([p.get("comments_count") for p in posts]),
        "avg_saved": _avg([p.get("saved") for p in posts]),
        "avg_engagement": _avg([post_engagement(p) for p in posts]),
        "sum_reach": sum_reach,
        "sum_engagement": sum_eng,
        "sum_saved": sum_saved,
        "engagement_rate": _rate(sum_eng, sum_reach),
        "save_rate": _rate(sum_saved, sum_reach),
    }


# --------------------------------------------------------------------------
# データセクション
# --------------------------------------------------------------------------

def build_data_section(account_rows, media_rows) -> tuple[str, dict]:
    latest, prev = account_delta(account_rows)
    posts = list(latest_per_media(media_rows).values())

    now_dt = _parse_dt(latest["ts"]) if latest else datetime.now(timezone.utc)
    week_ago = now_dt - timedelta(days=7)
    two_weeks_ago = now_dt - timedelta(days=14)

    def created(p):
        return _parse_dt(p.get("created_at")) or now_dt

    this_week = [p for p in posts if created(p) >= week_ago]
    prev_week = [p for p in posts if two_weeks_ago <= created(p) < week_ago]

    lines: list[str] = []
    uname = (latest or {}).get("username") or "your_account"
    report_date = (latest or {}).get("date") or datetime.now(JST).strftime("%Y-%m-%d")
    lines.append(f"# 週次インサイトレポート — @{uname}")
    lines.append(f"_生成日: {report_date}（JST） / 保有データ: 投稿 {len(posts)} 件_\n")

    # --- アカウントサマリ ---
    lines.append("## アカウントサマリ")
    if latest:
        p = prev or {}
        lines.append("| 指標 | 今回（前回比） |")
        lines.append("|---|---|")
        lines.append(f"| フォロワー | {_fmt_delta(latest.get('followers_count'), p.get('followers_count'))} |")
        lines.append(f"| フォロー中 | {_fmt_delta(latest.get('follows_count'), p.get('follows_count'))} |")
        lines.append(f"| 投稿数 | {_fmt_delta(latest.get('media_count'), p.get('media_count'))} |")
        if _num(latest.get("reach_7d")) is not None:
            lines.append(f"| リーチ(7日) | {_fmt_delta(latest.get('reach_7d'), p.get('reach_7d'))} |")
        if not prev:
            lines.append("\n> 比較対象の過去スナップショットがまだありません（初回 or 1週間未満）。")
    else:
        lines.append("> アカウント履歴がありません。")
    lines.append("")

    # --- ★A/Bテスト（このアカウント観測の主目的） ---
    auto_posts = [p for p in posts if _slot_of(p) in ("08:00", "12:30")]
    by_slot: dict[str, list[dict]] = defaultdict(list)
    for p in auto_posts:
        by_slot[_slot_of(p)].append(p)

    lines.append("## ★A/Bテスト — 朝 8:00 枠 vs 昼 12:30 枠（全期間）")
    if auto_posts:
        lines.append(f"- 自動投稿 **{len(auto_posts)}** 件が対象"
                     f"（告知・手動投稿 {len(posts) - len(auto_posts)} 件は除外）")
        lines.append("")
        lines.append("| 枠 | 本数 | 平均リーチ | 平均いいね | 平均コメント | 平均保存 | 平均エンゲージ | エンゲージ率 | 保存率 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for slot in ("08:00", "12:30"):
            ps = by_slot.get(slot, [])
            if not ps:
                continue
            s = _slot_stats(ps)
            lines.append(
                f"| **{SLOT_JP[slot]}** | {s['count']} | {_fmt(s['avg_reach'], 1)} | "
                f"{_fmt(s['avg_like'], 1)} | {_fmt(s['avg_comment'], 1)} | {_fmt(s['avg_saved'], 1)} | "
                f"{_fmt(s['avg_engagement'], 1)} | {_pct(s['sum_engagement'], s['sum_reach'])} | "
                f"{_pct(s['sum_saved'], s['sum_reach'])} |"
            )
        lines.append("")

        # 平日 / 土日でさらに割る（休日は行動が違うので混ぜると読み違える）
        lines.append("**平日 / 土日で分けた場合**")
        lines.append("")
        lines.append("| 枠 | 区分 | 本数 | 平均リーチ | 平均いいね | 平均エンゲージ |")
        lines.append("|---|---|---|---|---|---|")
        for slot in ("08:00", "12:30"):
            for label, want_weekend in (("平日", False), ("土日", True)):
                ps = [p for p in by_slot.get(slot, []) if _is_weekend(p) is want_weekend]
                if not ps:
                    continue
                s = _slot_stats(ps)
                lines.append(f"| {SLOT_JP[slot]} | {label} | {s['count']} | "
                             f"{_fmt(s['avg_reach'], 1)} | {_fmt(s['avg_like'], 1)} | "
                             f"{_fmt(s['avg_engagement'], 1)} |")
        lines.append("")
        lines.append("> ⚠️ 平均だけで勝ち負けを決めない。**本数が偏っている枠**や、"
                     "**告知直後で全体が底上げされた週**があると差が出やすい。")
    else:
        lines.append("> まだ自動投稿の指標がありません（バックフィル未実行の可能性）。")
    lines.append("")

    # --- 曜日別 ---
    by_wd: dict[str, list[dict]] = defaultdict(list)
    for p in auto_posts:
        wd = p.get("weekday_jst")
        if not wd:
            c = _parse_dt(p.get("created_at"))
            wd = c.astimezone(JST).strftime("%a") if c else None
        if wd:
            by_wd[wd].append(p)
    if by_wd:
        lines.append("## 曜日別（自動投稿のみ・全期間）")
        lines.append("| 曜日 | 本数 | 平均リーチ | 平均いいね | 平均エンゲージ |")
        lines.append("|---|---|---|---|---|")
        for wd in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            ps = by_wd.get(wd, [])
            if not ps:
                continue
            s = _slot_stats(ps)
            lines.append(f"| {WEEKDAY_JP[wd]} | {s['count']} | {_fmt(s['avg_reach'], 1)} | "
                         f"{_fmt(s['avg_like'], 1)} | {_fmt(s['avg_engagement'], 1)} |")
        lines.append("")

    # --- 今週の投稿 ---
    followers_now = _num((latest or {}).get("followers_count"))
    sum_reach = sum(x for x in (_num(p.get("reach")) for p in this_week) if x is not None)
    sum_eng = sum(post_engagement(p) for p in this_week)
    sum_saved = sum(x for x in (_num(p.get("saved")) for p in this_week) if x is not None)

    lines.append("## 今週の投稿")
    lines.append(f"- 投稿本数: **{len(this_week)}** 本（前週: {len(prev_week)} 本）")
    if this_week:
        avg_reach_per_post = sum_reach / len(this_week)
        lines.append("")
        lines.append("| 指標 | 値 | 意味 |")
        lines.append("|---|---|---|")
        if followers_now:
            lines.append(f"| リーチ率 | {_pct(avg_reach_per_post, followers_now)} | 平均リーチ ÷ フォロワー（どれだけ届いたか） |")
        lines.append(f"| エンゲージ率 | {_pct(sum_eng, sum_reach)} | 反応 ÷ リーチ（届いた人の反応率） |")
        lines.append(f"| 保存率 | {_pct(sum_saved, sum_reach)} | 保存 ÷ リーチ（伸びの先行指標） |")
    lines.append("")

    # --- 投稿ランキング ---
    ranked = sorted(posts, key=post_engagement, reverse=True)[:5]
    if ranked:
        lines.append("## 反応の大きかった投稿 TOP5（全期間）")
        lines.append("| エンゲージ | リーチ | いいね | コメント | 保存 | 枠 | 投稿日 | 内容 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for p in ranked:
            def cell(k):
                v = _num(p.get(k))
                return f"{int(v):,}" if v is not None else "―"
            head = (p.get("caption_head") or "").replace("|", "/")[:32]
            link = p.get("permalink") or ""
            head_md = f"[{head}]({link})" if link else head
            lines.append(
                f"| {post_engagement(p):,} | {cell('reach')} | {cell('like_count')} | "
                f"{cell('comments_count')} | {cell('saved')} | {SLOT_JP.get(_slot_of(p), '―')} | "
                f"{p.get('created_date_jst') or (p.get('created_at') or '')[:10]} | {head_md} |"
            )
        lines.append("")

    # --- タイプ別（従属的：現状ほぼ FEED 一択） ---
    by_type: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        by_type[p.get("media_product_type") or "?"].append(p)
    if len(by_type) > 1:
        lines.append("## 投稿タイプ別（全期間）")
        lines.append("| タイプ | 本数 | 平均リーチ | 平均エンゲージ | 平均保存 |")
        lines.append("|---|---|---|---|---|")
        for t, ps in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            s = _slot_stats(ps)
            lines.append(f"| {PRODUCT_TYPE_JP.get(t, t)} | {s['count']} | "
                         f"{_fmt(s['avg_reach'], 1)} | {_fmt(s['avg_engagement'], 1)} | "
                         f"{_fmt(s['avg_saved'], 1)} |")
        lines.append("")

    # --- 推移トレンド ---
    trend = weekly_trend(account_rows, weeks=8)
    if len(trend) >= 2:
        lines.append("## フォロワー推移")
        lines.append("| 計測日 | フォロワー | 前回比 | フォロー中 | 投稿数 | リーチ(7日) | 出所 |")
        lines.append("|---|---|---|---|---|---|---|")
        prev_f = None
        for r in trend:
            f = _num(r.get("followers_count"))
            delta = f"{int(f - prev_f):+,}" if (f is not None and prev_f is not None) else "―"
            prev_f = f if f is not None else prev_f
            src = "手動転記" if r.get("source") == "manual" else "API"
            lines.append(
                f"| {r.get('date') or r.get('ts', '')[:10]} | {_fmt(f)} | {delta} | "
                f"{_fmt(r.get('follows_count'))} | {_fmt(r.get('media_count'))} | "
                f"{_fmt(r.get('reach_7d'))} | {src} |"
            )
        lines.append("")
        lines.append("> 「手動転記」の行は `docs/反応観測記録.md` から取り込んだ過去の観測値。"
                     "当時 API を叩いていないため reach 等は空欄。")
        lines.append("")

    # Claude に渡す構造化サマリ
    summary = {
        "report_date": report_date,
        "account": {
            "followers_now": (latest or {}).get("followers_count"),
            "followers_prev": (prev or {}).get("followers_count"),
            "media_count": (latest or {}).get("media_count"),
            "reach_7d": (latest or {}).get("reach_7d"),
        },
        "ab_test_slots": {
            SLOT_JP[slot]: _slot_stats(by_slot[slot])
            for slot in ("08:00", "12:30") if by_slot.get(slot)
        },
        "ab_test_weekday_split": {
            f"{SLOT_JP[slot]}／{label}": _slot_stats(
                [p for p in by_slot.get(slot, []) if _is_weekend(p) is want_weekend])
            for slot in ("08:00", "12:30")
            for label, want_weekend in (("平日", False), ("土日", True))
            if [p for p in by_slot.get(slot, []) if _is_weekend(p) is want_weekend]
        },
        "by_weekday": {
            WEEKDAY_JP[wd]: _slot_stats(by_wd[wd])
            for wd in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") if by_wd.get(wd)
        },
        "this_week_posts": len(this_week),
        "prev_week_posts": len(prev_week),
        "rates_this_week": {
            "reach_rate": _rate(sum_reach / len(this_week), followers_now)
            if this_week and followers_now else None,
            "engagement_rate": _rate(sum_eng, sum_reach),
            "save_rate": _rate(sum_saved, sum_reach),
        },
        "follower_trend": [
            {
                "date": r.get("date") or r.get("ts", "")[:10],
                "followers": _num(r.get("followers_count")),
                "source": r.get("source", "api"),
            } for r in trend
        ],
        "top_posts": [
            {
                "slot": SLOT_JP.get(_slot_of(p)),
                "date": p.get("created_date_jst") or (p.get("created_at") or "")[:10],
                "engagement": post_engagement(p),
                "reach": _num(p.get("reach")),
                "like": _num(p.get("like_count")),
                "comment": _num(p.get("comments_count")),
                "saved": _num(p.get("saved")),
                "caption": p.get("caption_head"),
            } for p in ranked
        ],
    }
    return "\n".join(lines), summary


# --------------------------------------------------------------------------
# Claude の所見
# --------------------------------------------------------------------------

def build_insight_section(summary: dict) -> str:
    """Claude で所見と改善提案を生成。API キーが無ければプレースホルダ。"""
    try:
        from content_generator import claude_client
    except Exception as e:  # noqa: BLE001
        return f"## 所見と改善提案\n\n> 生成スキップ（content_generator 読み込み失敗: {e}）\n"

    client = claude_client()
    if client is None:
        return ("## 所見と改善提案\n\n"
                "> ANTHROPIC_API_KEY 未設定のため自動所見はスキップしました。"
                "上のデータを見て来週の方針を決めてください。\n")

    prompt = f"""あなたは Instagram アカウント @あなたのユーザーネーム のグロース担当アナリストです。
以下の週次インサイト集計（JSON）を読み、日本語の markdown で簡潔にレポートしてください。

【アカウントの前提】
- 平日フル勤務の会社員が、Claude Code で作った仕組みで **毎日2本**自動投稿している
  （cron-job.org の2ジョブ：朝 8:00 / 昼 12:30 JST）。投稿はすべてフィード画像。
- 観測の第一目的は **「朝 8:00 枠と昼 12:30 枠のどちらが効くか」の A/B テストに決着をつけること**。
- 最終的な目的は Instagram 自体を伸ばすことではなく、note 記事への導線として機能させること。
  ただし過去の観測で「note 記事の告知投稿のリーチは 33 前後で頭打ち」とわかっている。
- フォロワーの伸びには手動交流（いいね返し・コメント）が効いている可能性が高い。
  数字だけでは自動投稿の寄与と手動交流の寄与は分離できない。

【今週の集計データ】
{json.dumps(summary, ensure_ascii=False, indent=2, default=str)}

【出力フォーマット（この見出し構成を厳守 / 前後の余計な文は不要）】
## 今週の所見
- （データから読み取れる事実を3〜5点。数字に基づき、憶測は避ける）

## A/Bテスト（8:00 vs 12:30）の現時点の判定
- （どちらが優勢か、または「まだ差が有意でない」か。**必ず本数（サンプル数）に触れる**。
  平日/土日の分割で傾向が変わるならそこも述べる。結論を急がないこと）

## 来週試す改善案
- （具体的なアクションを3〜5点。実行可能な粒度で）

【データの読み方】
- ab_test_slots: 枠ごとの集計。avg_* は平均、engagement_rate=反応÷リーチ、save_rate=保存÷リーチ
- ab_test_weekday_split: 同じ枠を平日/土日に割ったもの。休日は本人の行動も投稿内容も違う
- follower_trend の source が "manual" の行は過去の手動観測値で、reach 等の指標は無い
- サンプル数が少ない指標（数件程度）で優劣を断定しない。その場合は「まだ判断できない」と書く

【ルール】
- 誇張や根拠のない断定をしない。データが少ない場合はその旨を述べる
- 絵文字は使わない
"""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        return text + "\n"
    except Exception as e:  # noqa: BLE001
        return f"## 所見と改善提案\n\n> Claude 生成に失敗: {e}\n"


def main() -> None:
    account_rows = _load_jsonl(ACCOUNT_HISTORY)
    media_rows = _load_jsonl(MEDIA_HISTORY)
    if not account_rows and not media_rows:
        print("⚠️  analytics 履歴が空です。先に analytics_fetcher.py を実行してください。")

    data_md, summary = build_data_section(account_rows, media_rows)
    print("🧮 集計完了。Claude で所見を生成中...")
    insight_md = build_insight_section(summary)

    report = data_md + "\n" + insight_md
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(report, encoding="utf-8")
    dated = REPORTS_DIR / f"{summary['report_date']}.md"
    dated.write_text(report, encoding="utf-8")
    print(f"✅ レポート出力: {LATEST_REPORT}  /  {dated}")


if __name__ == "__main__":
    main()
