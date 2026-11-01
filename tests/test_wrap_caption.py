#!/usr/bin/env python3
"""キャプションの折り返し（_wrap_caption_text）のテスト。API は使いません。

    python tests/test_wrap_caption.py

目ざす形：1文ごとに段落／読点のあとで改行／1行は短め／
かぎかっこの中と、閉じかぎ直後の助詞では割らない。

🔴 いちばん大事なのは **文字が1字も変わらないこと**。入れてよいのは改行だけです。
新しい崩れを見つけたら EXPECTED に1件足してください。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content_generator import (          # noqa: E402
    CAPTION_WRAP_WIDTH,
    DIGITS,
    LINE_HEAD_FORBIDDEN,
    LINE_TAIL_FORBIDDEN,
    _display_width,
    _emphasize_opening_beat,
    _ensure_paragraphs,
    _one_sentence_per_paragraph,
    _wrap_caption_text,
)

# 折り返す前の本文。複数段落のものを混ぜてある。
CASES = [
    "今日は休みの日で、私はまだ布団の中です。この投稿が表に出た瞬間も、"
    "たぶんまだ目が覚めていません。\n\n"
    "それでもこの投稿は決まった時刻に出ています。私が何もしなくても仕組みが動いて"
    "公開してくれる、というだけの話です。\n\n"
    "こういう「代わりに作業してくれるAI」の話、もう少し知りたい方はフォローして"
    "次の投稿も見てみてください。",
    "私が触っているClaude Codeというのは、質問に答えるだけのAIチャットとは少し違います。"
    "頼んだことを実際に手を動かしてやってくれる「エージェント」というタイプです。",
    "投稿が丸一日止まりました。直したのは私です。"
    "この仕組みに自動で復旧する機能はありません（そこだけは誤解されがちです）。",
    "今日はここまでです。",
]

# 「この本文は必ずこう折れてほしい」まで決めている項目（出力を固定する）。
EXPECTED = {
    "画像を作るのも、私はこの瞬間タッチしていません。":
        ["画像を作るのも、", "私はこの瞬間タッチしていません。"],
    "こういう「代わりに作業してくれるAI」の話、もう少し知りたい方はフォローして次の投稿も見てみてください。":
        ["こういう",
         "「代わりに作業してくれるAI」の話、",
         "もう少し知りたい方は",
         "フォローして次の投稿も",
         "見てみてください。"],
    "ただ『投稿文を毎回自分で考える』をやらないと決めただけで、続けられる余白ができました。":
        ["ただ『投稿文を毎回自分で考える』を",
         "やらないと決めただけで、",
         "続けられる余白ができました。"],
    "働いている日に使う言葉を弾く仕組みが効きすぎて、その日は用意していた別の文章に"
    "入れ替わってしまったことがあります。":
        ["働いている日に使う言葉を",
         "弾く仕組みが効きすぎて、",
         "その日は用意していた別の文章に",
         "入れ替わってしまったことがあります。"],
    "AIチャットは会話止まり、エージェントは手を動かすところまで。":
        ["AIチャットは会話止まり、",
         "エージェントは",
         "手を動かすところまで。"],
    "浮いた時間で、こうして仕組み自体を少しずつ直していく方に力を使っています。":
        ["浮いた時間で、",
         "こうして仕組み自体を",
         "少しずつ直していく方に",
         "力を使っています。"],
}

# 語の途中で割ってはいけない並び
BAD_SPLITS = [
    ("てい", "ません"),
    ("この", "瞬間"),
    ("ありま", "す"),
    ("置き", "換"),
    ("入れ", "替"),
    ("置き換", "わ"),
]

PARA_SEP = "\n\n"
failures = []


def bare(text: str) -> str:
    return re.sub(r"\s", "", text)


def check_lines(label: str, lines: list[str]) -> None:
    for i, ln in enumerate(lines):
        if not ln:
            continue                      # 段落のあいだの空行
        w = _display_width(ln)
        if w > CAPTION_WRAP_WIDTH:
            failures.append(f"[{label}] 幅超過 {w} > {CAPTION_WRAP_WIDTH}：{ln}")
        if ln[-1] in LINE_TAIL_FORBIDDEN:
            failures.append(f"[{label}] 行末が開き括弧：{ln}")
        if ln[-1] in DIGITS and i + 1 < len(lines) and lines[i + 1]:
            failures.append(f"[{label}] 行末が数字：{ln}")
        if ln[0] in LINE_HEAD_FORBIDDEN:
            failures.append(f"[{label}] 行頭が禁則文字：{ln}")


for n, text in enumerate(CASES, 1):
    label = f"case{n}"
    out = _wrap_caption_text(text)
    lines = out.split("\n")
    check_lines(label, lines)
    if bare(out) != bare(text):
        failures.append(f"[{label}] 文字が変わった")
    if text.count(PARA_SEP) != out.count(PARA_SEP):
        failures.append(f"[{label}] 段落の数が変わった")
    for i in range(len(lines) - 1):
        if not lines[i] or not lines[i + 1]:
            continue
        for tail, head in BAD_SPLITS:
            if lines[i].endswith(tail) and lines[i + 1].startswith(head):
                failures.append(f"[{label}] 語を割った：{lines[i]} ／ {lines[i + 1]}")
        if lines[i][-1] in "」』" and "ぁ" <= lines[i + 1][0] <= "ゖ":
            failures.append(f"[{label}] 閉じかぎの直後で割った：{lines[i]} ／ {lines[i + 1]}")

for src, want in EXPECTED.items():
    got = _wrap_caption_text(src).split("\n")
    if got != want:
        failures.append(f"[expected] {src}\n    期待: {want}\n    実際: {got}")

# 書き出しの「受け」を独立した段落にする
BEAT_CASES = [
    ("何もできなかったこと、ありませんか。私はよくあります。",
     ["何もできなかったこと、ありませんか。", "私はよくあります。"],
     "問いかけ＋受け"),
    ("どう思いますか。\n\n私は違います。",
     ["どう思いますか。", "私は違います。"],
     "1段落目が1文なら触らない"),
    ("今日はここまでです。自由に使える時間はびっくりするくらい短くて、正直ほとんど残りません。",
     ["今日はここまでです。自由に使える時間はびっくりするくらい短くて、正直ほとんど残りません。"],
     "最後の文が長ければ触らない"),
]
for src, want, why in BEAT_CASES:
    got = _emphasize_opening_beat(src)
    if got.split(PARA_SEP) != want:
        failures.append(f"[beat/{why}]\n    期待: {want}\n    実際: {got.split(PARA_SEP)}")
    if bare(got) != bare(src):
        failures.append(f"[beat/{why}] 文字が変わった")

# 1文ごとに段落を分ける
SENT_SRC = "私もそうです。余力はほとんど残っていません。\n\nでも投稿は出ています。"
SENT_WANT = ["私もそうです。", "余力はほとんど残っていません。", "でも投稿は出ています。"]
got = _one_sentence_per_paragraph(SENT_SRC)
if got.split(PARA_SEP) != SENT_WANT:
    failures.append(f"[sentence] 期待: {SENT_WANT}\n    実際: {got.split(PARA_SEP)}")
if bare(got) != bare(SENT_SRC):
    failures.append("[sentence] 文字が変わった")

# 改行のない本文にも段落ができる
FLAT = "一文目です。二文目です。三文目です。四文目です。フォローしてください。"
got = _ensure_paragraphs(FLAT)
if PARA_SEP not in got or bare(got) != bare(FLAT):
    failures.append(f"[paragraph] 段落ができていない：{got}")

total = len(CASES) + len(EXPECTED) + len(BEAT_CASES) + 2
if failures:
    print(f"❌ {len(failures)} 件の問題（検査 {total} 件）")
    for f in failures:
        print("  -", f)
    sys.exit(1)

print(f"✅ 全 {total} 件 OK（幅 {CAPTION_WRAP_WIDTH} 以内・文字は不変・語を割らない）")
