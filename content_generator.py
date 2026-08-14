#!/usr/bin/env python3
"""
@あなたのユーザーネーム 用コンテンツ生成器（IMAGE フィード投稿）。

このテンプレートは外部データ源を持たず、ネタ自体を Claude に生成させる。

処理の流れ:
  1. 柱の決定          : 日付で 柱A/B/C(/D) を決定論的にローテーション（比率は PILLAR_SCHEDULE で調整）
  2. ネタ生成          : その日の柱に沿った見出し・キャプション・ハッシュタグ・表情を Claude に生成させる
  3. 表情画像の選択    : Claude の mood → assets/character/expr_*.png
  4. 画像合成          : ブランドカラーの 1080x1350 カード（ポートレート4:5）に見出し＋キャラを合成
  5. 出力              : outputs/post.png / post.json / caption.txt / hashtags.txt

出力 post.json スキーマ（instagram_poster.py が受け取る契約）:
  { caption, hashtags, post_text, image, media_type, pillar, mood }

必要な環境変数:
  ANTHROPIC_API_KEY   ネタ・キャプション生成（無ければフォールバックのネタで継続）
任意:
  POST_PILLAR         "A"/"B"/"C"/"D" を明示（テスト用。未指定なら日付で自動決定）
  PROMO_ENABLED       "true" で柱D（販促）を解禁。既定は false（初期フェーズは抑制）
  POST_DATE           "YYYY-MM-DD" を明示（テスト用。未指定なら今日 JST）

ローカル実行時は validation/.env があれば自動で読み込む（ANTHROPIC_API_KEY 再利用）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "outputs"
OUT_DIR.mkdir(exist_ok=True)

JST = ZoneInfo("Asia/Tokyo")

# --- アセット解決 ---------------------------------------------------------------
# 背景透過済みの transparent/ を最優先（用意してあればそちらを使う）。
# 無ければ背景つきの原画を使い、実行時に切り抜く。
def _resolve_assets_dir() -> Path:
    candidates = [
        ROOT / "assets" / "character" / "transparent",          # リポジトリ配置・透過済み
        ROOT.parent / "assets" / "character" / "transparent",    # ローカル配置・透過済み
        ROOT / "assets" / "character",                           # リポジトリ配置・原画
        ROOT.parent / "assets" / "character",                    # ローカル配置・原画
    ]
    for c in candidates:
        if c.exists() and any(c.glob("expr_*.png")):
            return c
    return candidates[-1]

CHAR_DIR = _resolve_assets_dir()

# --- ブランドカラー -------------------------------------------------------------
#
# ★ここは書き換える場所です。
#   下の色は**ただの仮置き**（グレー系）です。そのまま使うと、味気ないうえに
#   他の人の投稿と見分けがつきません。**あなたのアカウントの色に変えてください。**
#
#   決め方に迷ったら、ベース1色＋アクセント1色の2色だけ決めれば十分です。
#   ベース＝背景に敷く濃い色／アクセント＝目立たせたい一点に使う明るい色。
#   Claude Code に「この2色でカードを作って」と伝えれば、残りは調整してくれます。
BASE = (38, 42, 48)         # ベース色（背景の上端）
BASE_DARK = (26, 29, 34)    # ベース色の暗いほう（背景の下端。グラデーションになる）
ACCENT = (120, 140, 170)    # アクセント色（ユーザー名のピル・中心メッセージ・見出しの縦線）
WHITE = (245, 249, 252)     # 見出しの文字色
TEXT_SUB = (170, 180, 195)  # 補足（柱名）の文字色

BRAND_HANDLE = "@あなたのユーザーネーム"

# 投稿画像の下端に必ず入る「中心メッセージ」。アカウントの一行の看板です。
# ★ここも書き換える場所です。（例：「仕事してる間に、AIが毎日投稿。」）
BRAND_MESSAGE = "（あなたの中心メッセージ）"

# 文章に出してよい生活の時刻。上の ALLOWED_HOURS と揃えて書きます。
# ★ここも書き換える場所です。（例：「起床6:30 / 帰宅22:30 前後」）
LIFE_HOURS_NOTE = "（起床◯:◯◯ / 帰宅◯◯:◯◯ 前後）"

# Instagram 推奨のポートレート比率（4:5）。正方形だとフィードで上下が見切れるため。
CANVAS_W = 1080
CANVAS_H = 1350

# --- 日本語フォント（CIではNoto Sans JPをDL、ローカルはSystemフォント） ----------
FONTS_DIR = ROOT / "inputs" / "fonts"
FONT_REGULAR_CANDIDATES = [
    str(FONTS_DIR / "NotoSansJP-Regular.otf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
]
FONT_BOLD_CANDIDATES = [
    str(FONTS_DIR / "NotoSansJP-Bold.otf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
]


def load_font(size, bold=False):
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ==========================================================================
# 柱（ピラー）と表情
# ==========================================================================
# 柱の定義（＝投稿テーマの大きな軸。第4章）
#
# ★ここは書き換える場所です。
#   name と desc を、あなたのアカウントのテーマに置き換えてください。
#   desc は「その柱の日に、何を書いてほしいか」をAIに伝える説明文です。
#   具体的に書くほど、生成される文章も具体的になります。
#   柱は3〜4本が扱いやすい（増やしすぎるとアカウントの印象がぼやけます）。
PILLARS = {
    "A": {
        "name": "（柱Aのテーマ名。例：自動化の舞台裏）",
        "desc": "（この柱の日に何を書いてほしいかを説明する。"
                "例：今日の投稿がどう作られているか／失敗談やエラー対処／"
                "『自分が動けない時間にこれが動いている』というメタ視点。）",
        "default_mood": "smug",
    },
    "B": {
        "name": "（柱Bのテーマ名。例：時間がない会社員の生存戦略）",
        "desc": "（例：平日フル勤務の過ごし方／自動化で生まれた時間の使い方／"
                "『やらないこと』を決める考え方。共感で広がる層に向けて。）",
        "default_mood": "sleepy",
    },
    "C": {
        "name": "（柱Cのテーマ名。例：AIエージェント入門）",
        "desc": "（例：Claude Codeで何ができるか／AIチャットとエージェントの違い／"
                "専門用語は必ず噛み砕く。学び・保存されやすい層に向けて。）",
        "default_mood": "thinking",
    },
    "D": {
        "name": "（柱Dのテーマ名。例：販促・予告）",
        "desc": "（例：作っているものの予告。露骨にせず予告にとどめる。）",
        "default_mood": "happy",
    },
}

# 10日周期で 柱比率 A4:B3:C2:D1（=40/30/20/10）を厳密に満たす並び。
# POST_DATE の年内通算日 % 10 で引く。初期は PROMO_ENABLED=false で D→A に置換。
PILLAR_SCHEDULE = ["A", "B", "C", "A", "B", "A", "C", "B", "A", "D"]

# mood（表情）→ assets/character/expr_*.png
MOODS = {
    "happy":    "expr_happy.png",    # 成功・達成系
    "troubled": "expr_troubled.png", # 失敗・つまずき系
    "sleepy":   "expr_sleepy.png",   # 残業・週末・気力ゼロ系（柱B共感）
    "smug":     "expr_smug.png",     # 仕組み紹介・成果見せ・品質担保（ドヤ系）
    "thinking": "expr_thinking.png", # 思案・コード・図解・入門解説
    "working":  "expr_working.png",  # 中心物語の看板（自己紹介・オフィス）
}

# ハッシュタグ方針:
#   現在の Instagram は 3〜5 個が推奨。毎回同一タグの羅列はスパム判定リスクもある。
#   → ブランド核タグ（CORE）を常設し、残りを投稿固有タグで埋め、合計 MAX_HASHTAGS 個に制限。
MAX_HASHTAGS = 5
# ブランド核ハッシュタグ（毎回必ず付与・アカウントの軸）
# ★ここも書き換える場所です。アカウントの軸になるタグを1〜2個。
CORE_HASHTAGS = ["#タグ1", "#タグ2"]

# 人格（ペルソナ）— Claude への system 指示に使う
#
# ★★ ここが、この仕組みでいちばん大事な書き換え箇所です。★★
#
# 毎日の投稿の「誰が書いているか」を決めるのが、この1つの文字列です。
# ここが空っぽだと、当たりさわりのない一般論しか出てきません。
# 逆にここが具体的であるほど、読んで「この人だ」と分かる文章になります。
#
# 書くときのコツ（詳しくは第4章）:
#   1. プロフィール … 生活が想像できる具体性で（勤務形態・帰宅時刻・使える時間）
#   2. 口調と人格   … 一人称／丁寧さ／「先生」ではなく「一緒に学んでいる人」など立ち位置
#   3. 【厳守】ルール … ★これが効きます。
#      AIは放っておくと「実態と違うが、それっぽい話」を書きます。
#      自分の生活と食い違う表現を、具体的に名指しで禁止してください。
#      例：投稿が出る時刻と、その時刻に自分が何をしているか（平日と土日で別々に）／
#          使ってほしくない言い回し／勝手に作られると困る事実（架空の時刻など）
#
# ⚠️ 一度に全部書こうとしなくて構いません。運用しながら、
#    「実態と違う」と気づいた投稿が出るたびに【厳守】を1行ずつ足していくのが現実的です。
PERSONA = (
    "あなたは『（あなたの名前）』というInstagramアカウント @（あなたのユーザーネーム） の"
    "中の人として投稿文を書く。"
    "プロフィール：（例：平日フル勤務の会社員、帰宅は22:30前後、可処分時間はほぼゼロ）。"
    "（何を作って、何を発信しているアカウントなのかを1〜2文で）。"
    "口調：一人称は『私』。丁寧だが堅すぎず親しみやすい。"
    "人格：失敗も正直に話し強がらない。技術用語は必ず噛み砕く。"
    "立ち位置は『先生』ではなく『一緒に学んでいる人』。"
    "成功例だけ見せる完璧な人にはしない。等身大の信頼感を最優先する。"
    #
    # --- ここから下は【厳守】ルールの例。自分の実態に合わせて書き換える ---
    #
    # ↓ 時刻は下の TIME_SLOTS の label と必ず揃えてください
    "【厳守・実態】投稿は毎日（1つ目の時刻）と（2つ目の時刻）（日本時間）の2回だけ自動で出る。"
    "平日の（1つ目の時刻）は（その時刻にあなたが何をしているか）、"
    "平日の（2つ目の時刻）は（同上）。"
    "土日の（1つ目の時刻）は（同上）、"
    "土日の（2つ目の時刻）は（同上）。"
    "これ以外の時刻（『毎朝9時』『10時に』など）を勝手に作って書かない。"
    "【厳守】土日の投稿では『会議』『勤務中』『出社』『平日の午前中』など、"
    "その日が平日・仕事中であることを前提にした表現は使わない（実態と矛盾するため）。"
    "【厳守・実態】この文章は投稿が公開されるまさにその時刻に自動生成されている。"
    "前の晩に書き溜めておいたものではないので、"
    "『昨日の夜に書いておいた』『前もってセットしておいた文章』のようには書かない。"
    "【厳守・実態】今日の投稿を自分で用意することはない。"
    "毎日の投稿はネタ出しから公開まで全部自動なので、"
    "『今日の分を考えてセットした』『予約投稿しておいた』のような、"
    "その日に自分が手を動かしたことにする表現は使わない"
    "（過去の話として『昔は手動で投稿していた』と振り返るのは可）。"
)

# アカウントの中心メッセージに反する NG 表現。
#
# ★ここも書き換える場所です。
#   PERSONA で禁止しても、AIはたまに書いてきます。ここに書いた語が含まれていたら、
#   生成をやり直させる（機械的な最後の砦）。
#   例：中心メッセージが「仕事してる間に」なら、睡眠中に動く体で書かれると困るので
#       ["寝てる間", "寝ている間", "睡眠中", "寝ながら"] のように並べる。
#   自分の中心メッセージと矛盾する語を入れてください。空リストでも動きます。
NG_PHRASES = []

# 土日の投稿で使うと実態と矛盾する「平日・勤務中前提」の表現。
# その日が仕事中であることを前提にした語のみを挙げる。『仕事』『会社員』など
# ブランド/属性として土日でも自然に使える語は含めない（誤検出を避ける）。
WEEKEND_NG_PHRASES = [
    "会議", "勤務", "出社", "退勤", "残業", "通勤",
    "平日", "業務中", "仕事中", "会社で働", "会社にい", "オフィスで",
]

JP_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

# 平日だが会社が休みの日。
#
# ★ここも書き換える場所です（年に数回・手で足すだけ）。
#   祝日・お盆・年末年始・有給など「暦の上では平日だが自分は出勤していない日」を
#   "YYYY-MM-DD" 形式で並べると、土日と同じ「休日モード」で書かせます
#   （『会議』『出社』などの平日前提の語を禁じる）。
#   ⚠️ 祝日の自動判定は入れていません。次の連休が決まったら追記してください。
#   ★ 期間が過ぎた日付は消してよい。空でも動きます。
DAY_OFF_DATES = {
    # 例：
    # "2027-01-01", "2027-01-02", "2027-01-03",
}

# 表現の言い換え。
# 生成後に機械的に直す。プロンプトでも避けるよう指示しているが、
# 毎回確実に揃えたい言い回しはここで最終的に上書きする。
WORDING_FIXES = [
    # 「手で投稿」→「手動で投稿」（『手で』は口語すぎるため）
    (re.compile(r"手で(投稿|運用|更新|作っ|書い|上げ)"), r"手動で\1"),
    (re.compile(r"手作業で(投稿|運用|更新)"), r"手動で\1"),
    # 「3日で崩れました」→「3日も持ちませんでした」（続かなかった話の定型）
    (re.compile(r"(\d+日|数日|一週間|1週間|1か月|1ヶ月)で崩れ(?:まし|)た"),
     r"\1も持ちませんでした"),
    (re.compile(r"(\d+日|数日|一週間|1週間)で(?:続かなくなり|途切れ)ました"),
     r"\1も持ちませんでした"),
]

# --- 投稿スロット ---------------------------------------------------------------
# cron-job.org が1日2回発火する。生成側が「どちらの枠か」を知らないと、
# 本文の時間帯と、その時間の自分の行動が実態とズレます
# （例：昼の投稿に「今朝9時に会社に着いた」＝存在しない時刻を創作する）。
#
# ★★ ここが、このファイルの投稿時刻の定義です。★★
#
#   label = 投稿が出る時刻そのもの。**cron-job.org のジョブの時刻と必ず揃えます。**
#   weekday / weekend = その時刻に、あなたが実際に何をしているか。
#     平日と土日は違うはずなので、必ず別々に書いてください。
#     ここを埋めておかないと、AIは土曜の朝の投稿に
#     「会議が始まる前に」のような文章を平気で書きます。
#
# ★投稿時刻を変えるときは、ここだけでなく**次の場所も揃えます**：
#     ・cron-job.org のジョブの時刻（これが本体。ここを変えないと投稿時刻は変わりません）
#     ・PERSONA の【厳守・実態】に書いた時刻
#     ・ALLOWED_HOURS（文章に出してよい「◯時」）
#     ・analytics_fetcher.py の SLOT_WINDOWS ／ analytics_report.py の SLOT_SPEC
#       （直し忘れると、集計だけ古い時刻のままになり A/B が合わなくなります）
TIME_SLOTS = {
    "A": {
        "label": "8:00",
        "weekday": "（平日のこの時刻、あなたは何をしていますか。例：すでに出社していて仕事が始まったところ）",
        "weekend": "（休みの日のこの時刻は。例：休みの日の朝で、たいていまだ寝ている）",
    },
    "B": {
        "label": "12:30",
        "weekday": "（例：昼休みで、食事や休憩をとっている）",
        "weekend": "（例：休みの日の昼で、たいてい昼ごはんを食べている）",
    },
}

# 実在する時刻（生活と仕組みの実態）。これ以外の「◯時」が出てきたら創作とみなす。
#
# ★ここも書き換える場所です。
#   投稿時刻（上の label）＋ あなたの起床・帰宅など、文章に出てよい「時」だけを並べます。
#   ここに無い時刻をAIが書いたら、創作とみなして生成をやり直します。
ALLOWED_HOURS = {0, 6, 8, 12, 22}
# 「2時間」「数時間」は時刻ではないので (?!間) で除外する。
CLOCK_RE = re.compile(r"(\d{1,2})\s*時(?!間)|(\d{1,2}):(\d{2})")

# その枠の時間帯と矛盾する表現（枠ごと）。土日 NG 語と同じ考え方で、
# 「今まさにその時間である」ことを前提にした語だけを挙げる。
TIME_NG_PHRASES = {
    "A": ["昼休み", "ランチ", "お昼休憩", "今は昼", "昼下がり"],
    "B": ["出社したばかり", "始業前", "朝礼", "今は朝", "これから出社"],
}
# どちらの枠でも実態と矛盾する表現（投稿が出るのは朝と昼だけ／文章は公開時刻に生成）。
TIME_NG_COMMON = [
    "今夜", "帰宅した今", "一日が終わった今",
    "昨日の夜に", "昨日の夜のこと", "昨夜のうち", "昨夜のこと",
    "前の晩に", "前の晩のこと", "夜のうちに書", "書き溜め",
    "前日に書", "昨日書いた",
]

# 「その日の投稿を自分で用意した」ことにする表現。
# ネタ出しから公開まで全自動なので、平日・土日を問わず事実に反する
# （特に土日に『自分で投稿を考えて設定した』と書かれるのが実態と食い違う）。
# 過去の振り返り（『昔は手で投稿していた』）は潰さないよう、
# 「今日／今」の話として書かれている形だけを拾う。
SELF_WORK_RES = [
    re.compile(r"今日(?:の|も|は)?[^。\n]{0,12}(?:投稿|ネタ)[^。\n]{0,12}"
               r"(?:考え|用意|セット|設定|仕込)"),
    re.compile(r"(?:投稿|ネタ)を[^。\n]{0,6}"
               r"(?:考え|用意し|セットし|設定し|仕込ん)(?:て|で)(?:おい|あ|い)"),
    re.compile(r"予約投稿"),
]


def is_weekend(date: datetime) -> bool:
    """私が休みの日なら True（土日、または DAY_OFF_DATES の休業日）。

    暦の上では平日でも出勤していない日は
    土日と同じ「休日モード」で書かせる（『会議』『出社』などを禁じる）。
    """
    return date.weekday() >= 5 or date.strftime("%Y-%m-%d") in DAY_OFF_DATES


def is_extra_day_off(date: datetime) -> bool:
    """平日だが会社が休みの日（祝日・お盆・夏季休暇）なら True。"""
    return date.weekday() < 5 and date.strftime("%Y-%m-%d") in DAY_OFF_DATES


def pick_time_slot(date: datetime) -> str:
    """投稿スロット A / B（＝ TIME_SLOTS のキー）を決める。

    ワークフローが渡す TIME_SLOT（cron-job.org の body 由来。"A"/"B" でも
    TIME_SLOTS の label と同じ時刻表記でも可）を優先し、無ければ実行時刻から推定する。
    """
    raw = os.environ.get("TIME_SLOT", "").strip().upper()
    if raw:
        if raw in TIME_SLOTS:
            return raw
        if "12" in raw:
            return "B"
        if "8" in raw:
            return "A"
    return "A" if date.hour < 10 else "B"


def pick_pillar(date: datetime) -> str:
    forced = os.environ.get("POST_PILLAR", "").strip().upper()
    if forced in PILLARS:
        return forced
    idx = date.timetuple().tm_yday % len(PILLAR_SCHEDULE)
    pillar = PILLAR_SCHEDULE[idx]
    promo_enabled = os.environ.get("PROMO_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    if pillar == "D" and not promo_enabled:
        pillar = "A"  # 初期フェーズは販促を抑制し、柱Aへ振り替え
    return pillar


# ==========================================================================
# Claude 呼び出し（流用元の claude_client / claude_json を流用）
# ==========================================================================
def claude_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY 未設定 → フォールバックのネタを使用")
        return None
    try:
        import anthropic
    except ImportError:
        print("⚠️  anthropic 未インストール → フォールバックのネタを使用")
        return None
    return anthropic.Anthropic(api_key=api_key)


def _first_text(msg) -> str:
    """レスポンスから最初のテキストブロックを取り出す。

    ⚠️ content[0] を決め打ちしないこと。claude-sonnet-5 は思考が既定でONで、
    レスポンスの先頭に ThinkingBlock（.text を持たない）が入る。決め打ちすると
    AttributeError になり、フォールバックのネタで静かに投稿され続ける
    （ワークフローは緑のまま・エラー通知も飛ばない）。
    """
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError(f"テキストブロックが無い: {[getattr(b, 'type', '?') for b in msg.content]}")


# max_tokens は「思考＋出力」の合計上限。思考が既定でONのモデルでは、
# 小さすぎると JSON が途中で切れて json.loads に失敗する。→ 余裕を持たせる
def claude_json(client, prompt, max_tokens=8000):
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=max_tokens,
        system=PERSONA,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _first_text(msg).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    return json.loads(raw)


def _day_context(date: datetime, strict: bool = False) -> str:
    """その日の曜日に応じた執筆コンテキスト。土日は平日前提の表現を禁じる。"""
    wd = JP_WEEKDAYS[date.weekday()]
    if not is_weekend(date):
        return (
            f"今日は{wd}曜日（平日）です。会社で働いている平日として自然に書いてよい。\n"
        )
    if is_extra_day_off(date):
        # 暦は平日でも会社が休みの日（祝日・お盆・夏季休暇）。
        # 曜日名だけ渡すと「平日だから出社している」と書かれてしまうので明示する。
        head = (f"今日は{wd}曜日ですが、会社が休み（祝日・お盆などの休暇）で、"
                "私は出勤していません。\n")
    else:
        head = f"今日は{wd}曜日（休日）です。\n"
    base = (
        head +
        "【重要・厳守】今日は休みの日です。次の表現は実態と矛盾するので絶対に使わないでください：\n"
        "　『会議』『勤務中』『勤務時間』『出社』『退勤』『残業』『通勤』『平日』『業務中』『仕事中』など、\n"
        "　その日が平日・仕事中であることを前提にした言い回し全般。\n"
        "今日の角度：休日で自分は仕事をしていない／何もしていないのに、"
        "仕組みが曜日に関係なく今日もこの投稿を出している、というリアルさを書く。\n"
    )
    if strict:
        base += (
            "【再厳守】前回の生成で平日・勤務前提の語が混入しました。"
            "見出し・本文ともに、休日であることと矛盾する語を一切使わないでください。\n"
        )
    return base


def _time_context(date: datetime, slot: str, strict: bool = False) -> str:
    """その投稿が出る時刻（TIME_SLOTS の label）に応じた執筆コンテキスト。

    生成側が枠を知らないと、時間帯の描写や「その時間の私の行動」が実態とズレる
    曜日コンテキストと同じく、事実を渡したうえで創作を禁じる。
    """
    s = TIME_SLOTS[slot]
    me = s["weekend" if is_weekend(date) else "weekday"]
    base = (
        f"この投稿が公開されるのは今日の {s['label']}（日本時間）です。その時刻、{me}。\n"
        "【重要・厳守】時間帯に触れるときは、この事実と矛盾しないように書いてください。\n"
        f"　・投稿が自動で出るのは毎日 "
        f"{' と '.join(v['label'] for v in TIME_SLOTS.values())} の2回だけです。\n"
        "　・『毎朝9時ごろ』『10時に』など、実在しない時刻を作らないでください。\n"
        f"　（私の生活で出せる時刻は {LIFE_HOURS_NOTE} だけです）\n"
        "　・この文章は今この公開時刻に生成されています。"
        "『昨日の夜に書いておいた』『前もってセットしておいた』とは書かないでください。\n"
        "　・今日の投稿を私が手で用意することはありません（ネタ出しから公開まで全部自動です）。"
        "『今日の分を考えてセットした』『予約投稿しておいた』のようには書かないでください。\n"
    )
    if is_weekend(date):
        # 土日の「その時間の私」を勝手に作られないよう、軸になる行動を明示する。
        # 軸になる行動は TIME_SLOTS の weekend にあなたが書いたものをそのまま使う。
        base += (
            f"　・その時間の私に触れるときは【{me}】ことを軸に書いてください。\n"
        )
        # ★NG_PHRASES に語を入れている場合は、言い換え先もここで指示しておくと安全です。
        #   例：中心メッセージが「仕事してる間に」で、土日朝の実態が「まだ寝ている」なら、
        #       『寝ている間に』は品質チェックで置換されて文が壊れるので、
        #       『まだ布団の中』『私はまだ起きていません』のように書かせる。
        if NG_PHRASES:
            base += (
                "　　ただし次の言い回しは中心メッセージとぶつかるので使わないでください："
                + "／".join(NG_PHRASES) + "。\n"
            )
    if strict:
        base += (
            "【再厳守】前回の生成で、時刻や時間帯が実態と矛盾する表現が混入しました。"
            "実在しない時刻を出さず、今が上記の時間帯であることと矛盾する言い回しを一切使わないでください。\n"
        )
    return base


def build_prompt(pillar_key: str, date: datetime, slot: str = "A",
                 strict_weekend: bool = False, strict_time: bool = False) -> str:
    p = PILLARS[pillar_key]
    moods = "／".join(MOODS.keys())
    return textwrap.dedent(f"""\
        今日の Instagram フィード投稿を1本作ってください。
        {_day_context(date, strict=strict_weekend)}{_time_context(date, slot, strict=strict_time)}今日の「柱」は【柱{pillar_key}：{p['name']}】です。
        この柱の内容：{p['desc']}

        次の JSON だけを出力してください（前後に説明文やコードフェンスを付けない）:
        {{
          "headline": "画像に大きく載せる見出し。全角16字以内。体言止め/短い問いかけで目を引く。煽りすぎない。",
          "caption": "本文。280〜480字程度。一人称『私』。具体的な情景や数字を1つは入れる。技術用語は噛み砕く。失敗や弱さも正直に。最後に軽いCTA（フォロー/保存の促し）を1文。絵文字は0〜2個まで。ハッシュタグは本文に含めない。",
          "extra_hashtags": ["#この投稿固有のタグ", "..."],
          "mood": "{moods} のいずれか1つ。内容の感情に合うもの（成功=happy/失敗=troubled/疲れ共感=sleepy/ドヤ=smug/思案解説=thinking/自己紹介・オフィス=working）"
        }}

        制約:
        - extra_hashtags はちょうど3個。今の投稿内容に最も関連する具体的なタグを選ぶ。日本語中心。スパム的な巨大タグの羅列は避ける。ブランド核タグ（#ClaudeCode / #AI自動化）は付けないでよい（こちらで足す）。
        - 既存アカウントや個人名・本名・他SNSの言及はしない。
    """)


def _fallback_content(pillar_key: str, weekend: bool = False) -> dict:
    """ANTHROPIC_API_KEY 不在やパース失敗時の最低限のネタ（柱ごと）。

    weekend=True のときは平日・勤務前提の語を含まない休日版を返す。

    ★ここも書き換える場所です。
      ここは「予備の文章」（フォールバック）です。AIの生成に失敗した日でも
      投稿を止めないために、あらかじめ用意しておく文章です（第4章）。
      柱ごと・平日/休日ごとに1本ずつ、当たりさわりのない文章を書いておいてください。
      ⚠️ 平日版に『会議』『出社』などを書くと、休日に使い回されたとき実態と矛盾します。
         必ず平日版と休日版を別々に書いてください。
      ⚠️ NG_PHRASES に入れた語をここに書かないこと（品質チェックで置換され、文が壊れます）。
    """
    pillar = PILLARS.get(pillar_key, PILLARS["A"])
    when = "休みの日ですが" if weekend else "私は自分では手を動かしていませんが"
    return {
        "headline": f"（{pillar['name']}の予備の見出し）",
        "caption": (
            f"（{pillar['name']}の予備の本文をここに書いておきます。{when}、"
            "この投稿は仕組みが自動で出しています。"
            "AIの生成に失敗した日に、この文章が代わりに使われます。）\n\n"
            "（最後にフォローや保存への一言を添えておくとよいです。）"
        ),
        "mood": pillar["default_mood"],
        "extra_hashtags": [],
    }

def _normalize_content(data: dict, pillar_key: str, weekend: bool) -> dict:
    """Claude/フォールバックの生データを投稿用 dict に整形する。"""
    mood = str(data.get("mood", "")).strip().lower()
    if mood not in MOODS:
        mood = PILLARS[pillar_key]["default_mood"]
    headline = str(data.get("headline", "")).strip() or PILLARS[pillar_key]["name"]
    caption = str(data.get("caption", "")).strip()
    if not caption:
        caption = _fallback_content(pillar_key, weekend)["caption"]

    extra = data.get("extra_hashtags") or []
    extra = [_normalize_tag(t) for t in extra if str(t).strip()]
    # ブランド核タグ＋投稿固有タグを重複なく結合し、合計 MAX_HASHTAGS 個に制限。
    # 核タグを先頭に置き、残り枠を固有タグで埋める（現Instagramの3〜5個推奨に合わせる）。
    seen = set()
    hashtags = []
    for t in CORE_HASHTAGS + extra:
        if not t:
            continue
        if t.lower() not in seen:
            seen.add(t.lower())
            hashtags.append(t)
        if len(hashtags) >= MAX_HASHTAGS:
            break

    # 品質チェック：中心メッセージに反する NG 表現を検出
    headline = _refine_wording(_enforce_central_message(headline, "headline", pillar_key))
    caption = _refine_wording(_enforce_central_message(caption, "caption", pillar_key))

    return {
        "pillar": pillar_key,
        "headline": headline,
        "caption": caption,
        "hashtags": hashtags,
        "mood": mood,
    }


def _weekend_violation(content: dict) -> str | None:
    """土日投稿に平日・勤務前提の語が混入していれば、その最初の語を返す。"""
    text = f"{content['headline']}\n{content['caption']}"
    return next((ng for ng in WEEKEND_NG_PHRASES if ng in text), None)


def _time_violation(content: dict, slot: str) -> str | None:
    """その枠の時間帯と矛盾する表現・実在しない時刻があれば、最初の1つを返す。"""
    text = f"{content['headline']}\n{content['caption']}"
    hit = next((ng for ng in TIME_NG_COMMON + TIME_NG_PHRASES[slot] if ng in text), None)
    if hit:
        return hit
    for rx in SELF_WORK_RES:                    # 「今日の投稿を私が用意した」系
        m = rx.search(text)
        if m:
            return m.group(0)
    for m in CLOCK_RE.finditer(text):
        hour = int(m.group(1) or m.group(2)) % 24
        if hour not in ALLOWED_HOURS:
            return m.group(0)
    return None


def _find_violation(content: dict, weekend: bool, slot: str) -> tuple[str, str] | None:
    """土日ガードと時間帯ガードをまとめて評価し、(種別, 検出語) を返す。"""
    if weekend:
        hit = _weekend_violation(content)
        if hit:
            return ("土日", hit)
    hit = _time_violation(content, slot)
    if hit:
        return ("時間帯", hit)
    return None


def generate_content(pillar_key: str, date: datetime, slot: str = "A") -> dict:
    weekend = is_weekend(date)
    client = claude_client()

    if client is None:
        content = _normalize_content(_fallback_content(pillar_key, weekend), pillar_key, weekend)
        print(f"📝 フォールバックのネタを使用（柱{pillar_key}{'・休日版' if weekend else ''}）")
        return content

    try:
        data = claude_json(client, build_prompt(pillar_key, date, slot))
        print(f"📝 Claude がネタを生成（柱{pillar_key}・{TIME_SLOTS[slot]['label']}枠）")
    except Exception as e:
        print(f"⚠️  Claude 生成に失敗 → フォールバック: {type(e).__name__}: {e}")
        return _normalize_content(_fallback_content(pillar_key, weekend), pillar_key, weekend)

    content = _normalize_content(data, pillar_key, weekend)

    # ガード：土日に平日・勤務前提の語、またはその枠の時間帯と矛盾する表現が
    # 混入していたら一度だけ厳しめに再生成し、それでも残るならフォールバックに差し替える。
    found = _find_violation(content, weekend, slot)
    if found:
        kind, hit = found
        print(f"⚠️  {kind}チェック: 実態と矛盾する表現「{hit}」を検出 → 再生成します")
        try:
            data2 = claude_json(client, build_prompt(
                pillar_key, date, slot, strict_weekend=True, strict_time=True))
            content2 = _normalize_content(data2, pillar_key, weekend)
        except Exception as e:
            print(f"⚠️  再生成に失敗: {type(e).__name__}: {e}")
            content2 = None
        if content2 is not None and _find_violation(content2, weekend, slot) is None:
            print("   → 再生成で解消")
            content = content2
        else:
            still = content2 and _find_violation(content2, weekend, slot)
            print(f"   → 再生成でも残存（{still}）→ フォールバックに差し替え")
            content = _normalize_content(_fallback_content(pillar_key, weekend), pillar_key, weekend)

    return content


def _enforce_central_message(text: str, field: str, pillar_key: str) -> str:
    """『寝てる間に』系の NG 表現が混入していたら警告し、見出しは安全な既定に差し替える。

    本文は語句置換で救済、見出しは短く差し替えが安全なのでフォールバック見出しに置換する。
    """
    hit = next((ng for ng in NG_PHRASES if ng in text), None)
    if not hit:
        return text
    print(f"⚠️  品質チェック: {field} に中心メッセージ違反の表現「{hit}」を検出")
    if field == "headline":
        safe = _fallback_content(pillar_key)["headline"]
        print(f"   → 見出しを安全な既定に差し替え: {safe}")
        return safe
    # caption は最小限の語句置換で救済
    fixed = text
    for ng in NG_PHRASES:
        fixed = fixed.replace(ng, "仕事してる間")
    print("   → 本文の該当表現を『仕事してる間』に置換")
    return fixed


def _refine_wording(text: str) -> str:
    """ユーザー指定の言い換え（WORDING_FIXES）を最終的に当てる。"""
    fixed = text
    for rx, repl in WORDING_FIXES:
        fixed = rx.sub(repl, fixed)
    if fixed != text:
        print("✍️  表現を統一（手で→手動で／〜で崩れました→〜も持ちませんでした）")
    return fixed


def _normalize_tag(t: str) -> str:
    t = str(t).strip()
    t = t.lstrip("#")
    t = re.sub(r"\s+", "", t)
    return "#" + t if t else ""


# ==========================================================================
# 画像合成
# ==========================================================================
def cutout_background(img: Image.Image, thresh: int = 28) -> Image.Image:
    """キャラ画像の背景を四隅から flood-fill して透過にする（rembg不使用）。

    ★この関数が前提にしていること：
      **背景がべた塗りの一色**であること（生成AIで作った立ち絵はたいていそうなります）。
      四隅を種にして「背景色 ±thresh の、つながっている領域」だけを透過にするので、
      キャラの内側にある同系色は、背景とつながっていなければ残ります。

    ★thresh は書き換える場所です。
      背景と地続きの色が抜けきらないときは少し上げ、
      キャラの一部（目の白・歯・靴など背景色に近い部分）まで抜けてしまうときは下げます。
      ⚠️ **上げすぎると、キャラの内側が抜けて穴になります。**
         背景を白で確認すると気づけないので、**必ず投稿カードの背景色に重ねて確認**してください。
      あらかじめ背景を透過したPNG（`assets/character/transparent/`）を置いておけば、
      この処理は走りません。そちらのほうが確実です。
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    seed = (255, 0, 255)  # 背景マーカー（画像内に存在しない色）
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        ImageDraw.floodfill(rgb, corner, seed, thresh=thresh)
    arr = np.array(rgb)
    mask = (arr[:, :, 0] == 255) & (arr[:, :, 1] == 0) & (arr[:, :, 2] == 255)
    rgba = np.array(img.convert("RGBA"))
    rgba[mask, 3] = 0
    return Image.fromarray(rgba)


def gradient_background(w: int, h: int, top, bottom) -> Image.Image:
    img = Image.new("RGBA", (w, h), top + (255,))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        d.line([(0, y), (w, y)], fill=(r, g, b, 255))
    return img


# 行頭禁則文字（行の先頭に置けない文字。閉じ括弧・句読点・終止記号・小書きかな・長音）
LINE_HEAD_FORBIDDEN = set(
    "、。，．・：；！？」』）｝】〉》〕］!?,.)]}"
    "ぁぃぅぇぉっゃゅょゎゐゑ"
    "ァィゥェォッャュョヮ"
    "ーゝゞ々〜"
)
# 行末に来ると自然な区切り文字（この文字の直後での改行を優先する）
# ※長音「ー」は語の途中なので入れない（「エー／ジェント」のような分断を避ける）
LINE_BREAK_PREFERRED = set("、。，．！？!?…―」』）】")


def wrap_japanese(text: str, max_chars: int) -> list[str]:
    """見出しを読みやすく折り返す。

    従来は max_chars ちょうどで貪欲に改行していたため「休日も動いてる、仕／組みの話」
    のように文節の途中でぶつ切りになっていた。これを次の3点で改善する：
      1. 行数をまず決め（総字数 ÷ max_chars を切り上げ）、各行の字数を均等に近づける。
      2. 句読点（、。！？など）の直後を改行位置として優先する。
      3. 行頭禁則文字が行頭に落ちる位置では改行しない（前行にぶら下げる）。
    明示改行（\\n）はそのまま尊重する。
    """
    lines: list[str] = []
    for segment in text.split("\n"):
        lines.extend(_wrap_segment(segment, max_chars))
    return lines


def _char_class(ch: str) -> str:
    """文字種をざっくり分類（語境界推定に使う）。ひらがな↔カタカナ↔漢字の切替は
    語の切れ目であることが多い。長音「ー」はカタカナ扱いにして語内に留める。"""
    o = ord(ch)
    if ch == "ー" or 0x30A0 <= o <= 0x30FF or 0xFF66 <= o <= 0xFF9F:
        return "kata"
    if 0x3040 <= o <= 0x309F:
        return "hira"
    if 0x4E00 <= o <= 0x9FFF or 0x3005 <= o <= 0x3007:
        return "kanji"
    if ch.isalnum():
        return "alnum"
    return "other"


def _wrap_segment(text: str, max_chars: int) -> list[str]:
    n = len(text)
    if n <= max_chars:
        return [text]
    n_lines = -(-n // max_chars)          # 切り上げ除算＝必要な行数
    ideal_step = n / n_lines              # バランスを取るための1行あたり目安字数
    lines: list[str] = []
    start = 0
    for li in range(n_lines - 1):
        rest_lines = n_lines - li - 1     # この行より後に残る行数
        ideal = start + ideal_step        # この行の理想的な終端位置
        # 早く切りすぎると後ろの行が max_chars を超えて画面からはみ出すので、
        # 「残りが残り行数に収まる」位置から探す（最終行も必ず max_chars 以内になる）。
        lo = max(start + 1, n - rest_lines * max_chars)
        hi = min(start + max_chars, n - rest_lines)  # 残り行に最低1字ずつ残す
        best, best_score = None, None
        for b in range(lo, hi + 1):       # b＝この行に含める字数（次行は text[b:]）
            nxt = text[b] if b < n else ""
            if nxt and nxt in LINE_HEAD_FORBIDDEN:
                continue                  # 次行頭が禁則になる位置では切らない
            score = -abs(b - ideal)       # 理想位置に近いほど高評価
            if text[b - 1] in LINE_BREAK_PREFERRED:
                score += 3                # 句読点などの直後で切れると読みやすい
            elif _char_class(text[b - 1]) != _char_class(text[b]):
                # 文字種の切替は語境界のことが多いが、漢字・カナ→ひらがなは
                # 送りがな（仕組/み）や助詞の途中で割れやすいので加点しない。
                score += -1 if _char_class(text[b]) == "hira" else 2
            if best_score is None or score > best_score:
                best, best_score = b, score
        if best is None:                  # 全候補が禁則で不可なら素直に最大字数で切る
            best = min(start + max_chars, n)
        lines.append(text[start:best])
        start = best
    lines.append(text[start:])
    return lines


def get_character(mood: str) -> Image.Image | None:
    fname = MOODS.get(mood, MOODS["thinking"])
    path = CHAR_DIR / fname
    if not path.exists():
        print(f"⚠️  キャラ画像が見つかりません: {path}")
        return None
    img = Image.open(path)
    if _has_transparency(img):
        # transparent/ の透過済み画像はそのまま使う（ハロー・隙間が綺麗に抜けている）
        return img.convert("RGBA")
    # 背景つきの原画の場合は実行時に切り抜く（簡易・縁連結のみ）
    return cutout_background(img)


def _has_transparency(img: Image.Image) -> bool:
    if img.mode not in ("RGBA", "LA") and "transparency" not in img.info:
        return False
    alpha = img.convert("RGBA").getchannel("A")
    return alpha.getextrema()[0] < 250


def render_image(content: dict, out_path: Path) -> None:
    W, H = CANVAS_W, CANVAS_H
    img = gradient_background(W, H, BASE, BASE_DARK)
    draw = ImageDraw.Draw(img)

    # --- キャラ（右下）。文字に被らない位置・サイズ ---
    # キャラの大きさは横幅基準。縦長にしても見た目のサイズが変わらないようにする。
    char = get_character(content["mood"])
    if char is not None:
        target_h = int(W * 0.56)
        ratio = target_h / char.height
        char = char.resize((int(char.width * ratio), target_h), Image.LANCZOS)
        cx = W - char.width + 40          # 少し右に逃がす
        cy = H - char.height + 30         # 足元を画面下に
        img.paste(char, (cx, cy), char)

    # --- ヘッダー：ブランドハンドル（オレンジのピル） ---
    handle_font = load_font(34, bold=True)
    pad_x, pad_y = 28, 14
    hb = draw.textbbox((0, 0), BRAND_HANDLE, font=handle_font)
    hw, hh = hb[2] - hb[0], hb[3] - hb[1]
    pill = (60, 60, 60 + hw + pad_x * 2, 60 + hh + pad_y * 2)
    draw.rounded_rectangle(pill, radius=(hh + pad_y * 2) // 2, fill=ACCENT)
    draw.text((60 + pad_x, 60 + pad_y - hb[1]), BRAND_HANDLE, font=handle_font, fill=BASE)

    # --- 見出し（大・太字・白、オレンジのアクセントバー付き） ---
    headline = content["headline"]
    head_font = load_font(82, bold=True)
    # 16字想定→1行8〜9字で2行程度に。長ければ折り返し。
    lines = wrap_japanese(headline, 9)[:3]
    y = 200
    bar_x = 64
    # アクセントバー（左の縦線）
    line_h = 104
    draw.rounded_rectangle(
        (bar_x, y + 6, bar_x + 12, y + 6 + line_h * len(lines) - 18),
        radius=6, fill=ACCENT,
    )
    tx = bar_x + 34
    for ln in lines:
        draw.text((tx, y), ln, font=head_font, fill=WHITE)
        y += line_h

    # --- カテゴリラベル（小・サブカラー）。内部符号「柱A」は出さずテーマ名のみ ---
    pillar = content["pillar"]
    label = PILLARS[pillar]["name"]
    sub_font = load_font(34, bold=False)
    draw.text((tx, y + 8), label, font=sub_font, fill=TEXT_SUB)

    # --- フッター：中心メッセージ ---
    foot_font = load_font(30, bold=True)
    foot = BRAND_MESSAGE
    fb = draw.textbbox((0, 0), foot, font=foot_font)
    draw.text((64, H - 70 - (fb[3] - fb[1])), foot, font=foot_font, fill=ACCENT)

    img.convert("RGB").save(out_path, "PNG")
    print(f"🖼  画像を生成: {out_path}")


# ==========================================================================
# 出力（流用元 write_post_json / format_post_text を流用）
# ==========================================================================
def format_post_text(caption: str, hashtags: list[str]) -> str:
    tags = " ".join(hashtags)
    return f"{caption.strip()}\n\n.\n.\n.\n{tags}"


def write_post_json(content: dict, image_path: Path) -> None:
    caption = content["caption"]
    hashtags = content["hashtags"]
    post_text = format_post_text(caption, hashtags)

    (OUT_DIR / "caption.txt").write_text(caption, encoding="utf-8")
    (OUT_DIR / "hashtags.txt").write_text(" ".join(hashtags), encoding="utf-8")

    payload = {
        "caption": caption,
        "hashtags": hashtags,
        "post_text": post_text,
        "image": Path(image_path).name,
        "media_type": "IMAGE",
        "pillar": content["pillar"],
        "mood": content["mood"],
    }
    (OUT_DIR / "post.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print("\n" + "=" * 50)
    print(post_text)
    print("=" * 50)
    print(f"\n✅ 出力先: {OUT_DIR}/  (柱{content['pillar']} / mood={content['mood']})")


# ==========================================================================
def _load_local_env() -> None:
    """ローカル実行の利便: src/validation/.env があれば未設定の環境変数だけ補う。"""
    env_path = ROOT / "validation" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main():
    parser = argparse.ArgumentParser(description="@あなたのユーザーネーム コンテンツ生成")
    parser.add_argument("--pillar", choices=list(PILLARS), help="柱を明示（既定は日付で自動）")
    parser.add_argument("--dry-run", action="store_true", help="画像のみ生成して post.json は書かない")
    args = parser.parse_args()

    _load_local_env()

    if args.pillar:
        os.environ["POST_PILLAR"] = args.pillar

    date_str = os.environ.get("POST_DATE", "").strip()
    if date_str:
        date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    else:
        date = datetime.now(JST)

    pillar = pick_pillar(date)
    slot = pick_time_slot(date)
    wd = JP_WEEKDAYS[date.weekday()]
    kind = "休日" if is_weekend(date) else "平日"
    print(f"📅 {date.strftime('%Y-%m-%d')}（{wd}・{kind}）"
          f" {TIME_SLOTS[slot]['label']}枠 → 柱{pillar}（{PILLARS[pillar]['name']}）")

    content = generate_content(pillar, date, slot)
    out_png = OUT_DIR / "post.png"
    render_image(content, out_png)

    if not args.dry_run:
        write_post_json(content, out_png)


if __name__ == "__main__":
    main()
