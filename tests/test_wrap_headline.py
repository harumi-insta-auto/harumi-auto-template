#!/usr/bin/env python3
"""見出しの折り返し（wrap_japanese）の回帰テスト。

    python tests/test_wrap_headline.py

よくある崩れ方を、そのまま検査項目にしています。API は使いません。
新しい崩れを見つけたら BAD_CASES に見出しを1行足してください。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content_generator import (          # noqa: E402
    DIGITS,
    LINE_HEAD_FORBIDDEN,
    LINE_TAIL_FORBIDDEN,
    wrap_japanese,
)

MAX = 9   # render_image() が見出しに使っている値

# 崩れやすい見出し
BAD_CASES = [
    "『仕』と『組み』の話",
    "休日も動いてる、仕組みの話",
    "「動いてる」と「できてる」は別",
    "エージェントに任せた日の話",
    "毎朝8時、ぼくは寝ている",
    "AIが“事実”を作った日の記録",
    "設定を間違えて止まった（けど直った）",
    "時間がない人の生存戦略",
    "エラー0件でも起きた話",
    "朝のうちに1本、勝手に出る",
    "エージェントって何？",
    "AIエージェントって何？",
]

# 「この見出しは、必ずこう割れてほしい」まで決めている項目（上と違い出力を固定する）。
EXPECTED = {
    "時間がない人の生存戦略": ["時間がない人の", "生存戦略"],
    "エラー0件でも起きた話": ["エラー0件でも", "起きた話"],
    "朝のうちに1本、勝手に出る": ["朝のうちに1本、", "勝手に出る"],
    "エージェントって何？": ["エージェントって", "何？"],
    # 2行では語の途中か「っ」の前後でしか割れない（12字）→ 3行に増やす
    "AIエージェントって何？": ["AI", "エージェントって", "何？"],
}


def check(headline: str) -> list[str]:
    lines = wrap_japanese(headline, MAX)
    errs = []
    if "".join(lines) != headline.replace("\n", ""):
        errs.append("文字が増減している")
    want = EXPECTED.get(headline)
    if want is not None and lines != want:
        errs.append(f"期待＝{' / '.join(want)}")
    for i, ln in enumerate(lines):
        if len(ln) > MAX:
            errs.append(f"{i + 1}行目が{len(ln)}字（上限{MAX}）")
        if i < len(lines) - 1 and ln and ln[-1] in LINE_TAIL_FORBIDDEN:
            errs.append(f"{i + 1}行目の行末が開き括弧「{ln[-1]}」")
        if i > 0 and ln and ln[0] in LINE_HEAD_FORBIDDEN:
            errs.append(f"{i + 1}行目の行頭が禁則文字「{ln[0]}」")
        if i < len(lines) - 1 and ln and ln[-1] in DIGITS:
            errs.append(f"{i + 1}行目の行末が数字「{ln[-1]}」（助数詞と割れている）")
    return errs


def main() -> int:
    ng = 0
    for h in BAD_CASES:
        lines = wrap_japanese(h, MAX)
        errs = check(h)
        mark = "✅" if not errs else "❌"
        print(f"{mark} {h}  →  {' / '.join(lines)}")
        for e in errs:
            print(f"    - {e}")
            ng += 1
    print("\n" + ("すべてOK" if ng == 0 else f"{ng}件の崩れ"))
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
