#!/usr/bin/env python3
"""年の取り違えガード（_year_violation）と、プロンプトに渡す今日の日付のテスト。

    python tests/test_year_guard.py

API は使いません。episodes.py を書き換えていても、いなくても動きます
（テストの中で一覧を差し替えます）。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import episodes  # noqa: E402
from content_generator import _facts_context, _year_violation  # noqa: E402

TODAY = datetime(2026, 10, 16, 8, 0)
SAMPLE = [{"when": "2026-06-19", "what": "本番稼働を始めた。", "keys": ["稼働"]}]

# 弾く
MUST_FLAG = [
    "去年6月に本番稼働を始めてから、投稿を手で用意したことはありません。",
    "私がやったのは、去年その仕組みを作ったことだけです。",
    "2年前に作った仕組みが、今も動いています。",
    "一昨年から自動投稿を続けています。",
]

# 通す
MUST_PASS = [
    "それだけで、去年の自分とは全然違う。",            # 仕組みに触れない心境の文
    "「自動化って難しそう」と思ってた一年前の自分に見せたい。",
    "今年の6月に本番稼働を始めてから、毎日投稿が出ています。",
    "投稿で今年の6月を「去年6月」と書いてしまったことがあります。",  # 引用
]


def check(text: str, today: datetime = TODAY):
    return _year_violation({"headline": "", "caption": text}, today)


def main() -> int:
    bad = 0
    original = episodes.REAL_EPISODES
    episodes.REAL_EPISODES = SAMPLE
    try:
        for t in MUST_FLAG:
            if check(t) is None:
                print(f"❌ 弾けていない：{t}")
                bad += 1
        for t in MUST_PASS:
            hit = check(t)
            if hit is not None:
                print(f"❌ 誤って弾いた（{hit}）：{t}")
                bad += 1

        # 来年になれば、今年の出来事は「去年」と書いてよい
        if check("去年の6月に本番稼働を始めました。", datetime(2027, 3, 1)) is not None:
            print("❌ 2027年に「去年の6月に本番稼働」を弾いた")
            bad += 1

        # プロンプトに今日の日付と「今年の6月」が入っている
        ctx = _facts_context(TODAY)
        for need in ("2026年10月16日", "2026-06-19（今年の6月）"):
            if need not in ctx:
                print(f"❌ プロンプトに「{need}」が無い")
                bad += 1

        # 一覧が雛形のまま：止まらず、日付は渡し、去年は弾く
        episodes.REAL_EPISODES = [{"when": "YYYY-MM-DD", "what": "（例）", "keys": ["例"]}]
        if "2026年10月16日" not in _facts_context(TODAY):
            print("❌ 一覧が空のときに今日の日付が渡っていない")
            bad += 1
        if check("去年6月に本番稼働を始めました。") is None:
            print("❌ 一覧が空のときに「去年」を弾けていない")
            bad += 1
    finally:
        episodes.REAL_EPISODES = original

    total = len(MUST_FLAG) + len(MUST_PASS) + 5
    print(f"{'✅' if bad == 0 else '❌'} 年の取り違えガード：{total - bad}/{total}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
