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
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from episodes import EPISODE_KEYS, episode_years, episodes_prompt_block

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
# ★違反は「重い」「軽い」の二層に分けています。理由は、ガードが厳しすぎると
#   AIが書いた文章が丸ごと予備の文章（フォールバック）に差し替わってしまうからです。
#   たとえば「平日は時間がないので、休みの日にまとめてやります」は、土曜に書いても
#   事実として正しい“対比”です。ところが「平日」という語があるだけで弾いていると、
#   これが毎回フォールバックに落ち、しかもAIの呼び出し回数が倍になります。
#   → 「今日は平日だ」と断定する形だけを弾き、対比は通す。

# 重い違反：その日の事実と食い違う。再生成しても直らなければフォールバックに落とす。
WEEKEND_NG_HEAVY = [
    "出社", "退勤", "通勤", "業務中", "仕事中", "会社で働", "会社にい", "オフィスで",
    # 休みの「理由」を特定する語。DAY_OFF_DATES は祝日・お盆・有給を区別せずに
    # 持っているので、理由を書かせると実際とは違う日に「今日は祝日で」と出ます。
    "祝日", "お盆", "有給",
]

# 「今日は平日だ」と断定する言い回しだけを弾く。対比の「平日は〜」「平日の朝は〜」は通す。
WEEKEND_NG_HEAVY_RES = [
    re.compile(r"(?:今日|きょう|本日)(?:は|も|が)平日"),
    re.compile(r"平日(?:の|である)(?:今日|本日)"),
]

# 軽い違反：対比としてなら休日でも正当に使える語。再生成は促すが、残っていても
# フォールバックには落とさない（平板な予備の文章を出すほうが害が大きいため）。
WEEKEND_NG_LIGHT = ["会議", "勤務", "残業"]

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

# --- 創作ガード（起きていない出来事を書かせない） ---------------------------------
# プロンプト側（_facts_context）で「一覧にあるものだけ」と縛っても、それだけでは
# 同じことが起きたときに**静かに通ってしまいます**。エラーは出ず、ワークフローは
# 成功のまま終わるからです。ここは最後の砦で、過去形の目印と不具合の語が
# ひとつの文に並んでいたら「作った話」の疑いとして拾い、一度だけ再生成させます。
#
# episodes.EPISODE_KEYS（実在の出来事を指す語）が同じ文にあれば通します。
# 一覧から選んで書いた場合まで弾くと、書ける具体が何も残らなくなるためです。
# （一覧が空のうちは EPISODE_KEYS も空＝除外なし。まだ実在の出来事が1つも無いので、
#   過去形＋不具合語の文はすべて疑ってよい、という意味になります。）
FACT_PAST_MARKERS = [
    "昨日", "一昨日", "先週", "先月", "この前", "先日", "前回", "以前",
    "数日前", "何日か前", "少し前", "つい最近", "先ほど",
    "最初", "当初", "はじめのころ", "初めのころ", "組んだとき", "組んだ時",
    "動かしたとき", "動かした時", "作った直後", "始めたころ", "始めた頃",
]
FACT_PAST_RES = [
    re.compile(r"\d+\s*(?:日|週間|か月|ヶ月)前"),
    re.compile(r"\d+\s*(?:日|週間|か月|ヶ月)(?:くらい|ほど|も)?(?:気づ|悩)"),
    # 「投稿が止まったことがあります」＝目印の語を持たない体験談の形。
    re.compile(r"こと(?:が|も)あ(?:り|る|っ)"),
]
# 「システムに何か起きた」ことを指す語。心境や一般論だけの文は拾いません。
# 部分一致なので、別の意味を巻き込む語は入れないでください
# （「落ち」＝落ち着く／落ち込む、「直し」＝見直し が引っかかりました）。
FACT_TROUBLE_WORDS = [
    "バグ", "エラー", "不具合", "止まっ", "止まり", "動かな", "失敗し",
    "ミス", "間違え", "間違っ", "ずれて", "ずれた", "ズレて", "ズレた",
    "おかしく", "壊れ", "文字化け", "飛びまし", "飛んだ",
]
# 「勝手に直った」と書かせないための語。
# **この仕組みに、止まったあと自動で復旧する機能はありません**（直すのは人です）。
# 「止まっても自動で戻る」と書かれると、読む人に無い機能を約束することになります。
# ※「自動投稿」「自動で出す」は通常運転なので巻き込みません（「自動で」＋回復の語のみ）。
SELF_HEAL_AUTO_WORDS = ["自動で", "勝手に", "ひとりでに", "自然に", "放っておいたら"]
SELF_HEAL_RECOVER_WORDS = ["復旧", "復帰", "直っ", "直り", "元に戻", "立ち直"]
# 文の区切り（。！？と改行）。
SENTENCE_SPLIT_RE = re.compile(r"[。！？\n]+")

# --- 年の取り違えガード -----------------------------------------------------
# プロンプトに曜日を渡していても、今日が何年かを渡していないと、AIは年を推測します。
# 今年の出来事を「去年」と書いても、エラーは出ません。対策は2つです。
#   ①プロンプトに今日の日付と「今年の6月」のような言い方を渡す（_facts_context）
#   ②仕組みの話の文に「去年」「◯年前」が出たら、その年に実際の出来事が無いかぎり弾く
# ※「去年の自分とは全然違う」のような、仕組みに触れない心境の文は拾いません。
YEAR_PAST_RE = re.compile(
    r"(一昨年|おととし|去年|昨年|前年|([0-9０-９]+|[一二三四五六七八九十])\s*年(?:以上)?(?:も)?前)")
YEAR_SYSTEM_WORDS = [
    "稼働", "仕組み", "自動", "投稿", "本番", "作っ", "作り", "組ん", "組み",
    "動かし", "動き始め", "始め", "運用", "システム", "コード", "AI",
]
_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


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
        # ⚠️ 休みの理由（祝日・お盆など）は書かない。候補を並べるとモデルがそこから
        # 1つ選んで「今日は祝日で」と書いてしまい、実際は祝日でない日に嘘が出る。
        head = (f"今日は{wd}曜日ですが、会社が休みで、私は出勤していません。\n"
                "※休みの理由（祝日・お盆・有給など）は特定できないので書かないこと。\n")
    else:
        head = f"今日は{wd}曜日（休日）です。\n"
    base = (
        head +
        "【重要・厳守】今日は休みの日です。次の表現は実態と矛盾するので絶対に使わないでください：\n"
        "　『会議』『勤務中』『勤務時間』『出社』『退勤』『残業』『通勤』『平日』『業務中』『仕事中』など、\n"
        "　その日が平日・仕事中であることを前提にした言い回し全般。\n"
        "　あわせて『祝日』『お盆』『有給』など、休みの理由を特定する語も使わないこと"
        "（実際と違う理由を書いてしまうため）。『休みの日』『今日は休み』と書けばよい。\n"
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


def _facts_context(date: datetime | None = None, strict: bool = False) -> str:
    """過去の出来事を創作させないためのコンテキスト。

    曜日・時間帯のコンテキストと同じ考え方＝**事実を渡したうえで創作を禁じる**。
    禁じるだけだと「具体を入れろ」という指示とぶつかって、AIはそれらしい話を
    作るほうへ倒れます。書いてよい具体を先に渡すのが要点です（episodes.py）。
    """
    base = (
        "【最重要・事実の厳守】この投稿は実在の仕組みの記録です。"
        "起きていない出来事を書かないでください。\n"
        "　・裏づけのない出来事・日付・期間・数値を作らないでください"
        "（『先週こんなバグが』『1週間気づかなかった』『数値が1桁違っていた』など）。\n"
        "　・弱さや戸惑いは正直に書いてよいですが、それは"
        "『起きていない不具合を書いてよい』という意味ではありません。\n"
        "　・extra_hashtags も同じです。本文に書いていない出来事を指すタグ"
        "（例：#バグとの戦い）を付けないでください。\n"
        "　・この仕組みには、止まったあと自動で復旧する機能はありません。"
        "『放っておいたら直った』のようには書かないでください。\n"
    )

    if date is not None:
        base += (
            f"　・今日は {date.year}年{date.month}月{date.day}日 です。年を推測しないでください。"
            "裏づけのない年について『去年』『昨年』『◯年前』とは書かないでください。\n"
        )

    block = episodes_prompt_block(date.date() if date is not None else None)
    if block:
        base += (
            "　・システムに起きた出来事（不具合・エラー・修正・気づき）に触れてよいのは、"
            "下の【実際に起きたこと】にあるものだけです。\n"
            "　・具体を入れたいときは、次のどれかにしてください。"
            "①下の一覧から1つ選ぶ ②いま動いている仕組みの説明 ③今の時間帯の私の様子。\n"
            "　・過去の出来事に触れないほうが自然なら、触れなくてかまいません。"
            "無理に失敗談を入れる必要はありません。\n"
            "【実際に起きたこと（過去に触れるならここから）】\n"
        )
        base += block
        if date is not None:
            base += "　※一覧の出来事を書くときは、かっこの中の言い方（今年の6月 など）に合わせてください。\n"
    else:
        # episodes.py がまだ雛形のまま＝渡せる過去がひとつも無い状態。
        # 「一覧から選べ」と言いながら一覧を出さないのがいちばん危ないので、
        # そのときは「過去には触れない」と言い切ります。
        base += (
            "　・過去に起きた出来事には、いっさい触れないでください。"
            "いま動いている仕組みの説明と、今の時間帯の私の様子だけで書いてください。\n"
            "　・無理に失敗談やエピソードを入れる必要はありません。\n"
        )

    if strict:
        base += (
            "【再厳守】前回の生成で、実際には起きていない出来事が本文に混入しました。"
            "今回は過去の出来事に触れず、いま動いている仕組みと今の時間帯の私の様子だけで"
            "書いてください。\n"
        )
    return base


def build_prompt(pillar_key: str, date: datetime, slot: str = "A",
                 strict_weekend: bool = False, strict_time: bool = False,
                 strict_facts: bool = False) -> str:
    p = PILLARS[pillar_key]
    moods = "／".join(MOODS.keys())
    return textwrap.dedent(f"""\
        今日の Instagram フィード投稿を1本作ってください。
        {_day_context(date, strict=strict_weekend)}{_time_context(date, slot, strict=strict_time)}{_facts_context(date, strict=strict_facts)}今日の「柱」は【柱{pillar_key}：{p['name']}】です。
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


def _fallback_content(pillar_key: str, weekend: bool = False,
                      slot: str | None = None) -> dict:
    """ANTHROPIC_API_KEY 不在やパース失敗時の最低限のネタ（柱ごと）。

    weekend=True のときは平日・勤務前提の語を含まない休日版を返す。
    slot（"A"/"B"）を渡すと、その枠ぶんの文章を返す。

    ★ここも書き換える場所です。
      ここは「予備の文章」（フォールバック）です。AIの生成に失敗した日でも
      投稿を止めないために、あらかじめ用意しておく文章です（第4章）。
      柱ごと・平日/休日ごと・**枠ごと**に1本ずつ、当たりさわりのない文章を
      書いておいてください。
      ⚠️ 平日版に『会議』『出社』などを書くと、休日に使い回されたとき実態と矛盾します。
         必ず平日版と休日版を別々に書いてください。
      ⚠️ NG_PHRASES に入れた語をここに書かないこと（品質チェックで置換され、文が壊れます）。
      ⚠️ **枠ごとに必ず違う文章にしてください。** 柱は日付で決まるので、同じ日に
         2つの枠が両方ともここに落ちると、見出しも本文も完全に同じになります。
         見出しから画像を描いているので画像まで1バイトも変わらず、
         「同じ写真と同じ文章を1日2回投稿する」ことになります。
         （さらに、画像を置くリポジトリへの git commit が「差分なし」で失敗し、
         ワークフローごと落ちます。第8章のトラブルシュートを参照。）
    """
    pillar = PILLARS.get(pillar_key, PILLARS["A"])
    when = "休みの日ですが" if weekend else "私は自分では手を動かしていませんが"
    # 枠が分かるときは、その時刻を文章に織り込んで枠ごとに別の文章にする。
    at = f"{TIME_SLOTS[slot]['label']}の" if slot in TIME_SLOTS else ""
    return {
        "headline": f"（{pillar['name']}の{at}予備の見出し）",
        "caption": (
            f"（{pillar['name']}の{at}予備の本文をここに書いておきます。{when}、"
            "この投稿は仕組みが自動で出しています。"
            "AIの生成に失敗した日に、この文章が代わりに使われます。）\n\n"
            "（最後にフォローや保存への一言を添えておくとよいです。）"
        ),
        "mood": pillar["default_mood"],
        "extra_hashtags": [],
    }

# 1段落あたりの目安字数。この前後で意味の切れ目を探して段落を割ります。
PARAGRAPH_TARGET_CHARS = 70

# 文の終わりの判定。閉じ括弧は文末側に含める（「〜でした。」）。
# 「…」は文末記号にしない（『書いている…わけではなく』のように途中で使うため）。
_SENTENCE_RE = re.compile(r"[^。！？!?\n]*[。！？!?]+[」』）\)]*|[^。！？!?\n]+")


def _ensure_paragraphs(caption: str) -> str:
    """本文に段落（空行）が無ければ、文の切れ目で段落に割り直す。

    ★なぜ必要か：段落の分け方はモデルによって変わります。モデルを切り替えただけで、
      改行のない一続きの文章が返ってくることがあります。ここで最終的に段落を
      保証します。**文字は一切書き換えません**（言い回しの決めごとや
      時間帯の表現を壊さないため、入れるのは改行だけです）。
    """
    text = caption.strip()
    if not text:
        return text
    # 3行以上の空行は1つの空行に詰める
    text = re.sub(r"\n{3,}", "\n\n", text)
    if "\n\n" in text:
        return text
    if "\n" in text:
        # 改行はあるが空行が無い＝段落の意図はあるので、空行に格上げするだけ
        return "\n\n".join(s.strip() for s in text.split("\n") if s.strip())

    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    sentences = [s for s in sentences if s]
    if len(sentences) < 3:
        return text  # 2文以下は割らない

    # 最後の1文は CTA（フォロー/保存の促し）なので独立した段落にする
    body, cta = sentences[:-1], sentences[-1]
    total = sum(len(s) for s in body)
    # 本文パートを 1〜6 段落に。CTA と合わせて 2〜7 段落になる
    n = min(6, len(body), max(1, round(total / PARAGRAPH_TARGET_CHARS)))
    target = total / n

    paragraphs, cur = [], ""
    for i, s in enumerate(body):
        cur += s
        rest = len(body) - i - 1            # この文より後に残っている文数
        need = n - len(paragraphs) - 1      # cur を確定したあと、まだ作る段落数
        # 目安の長さに達したら区切る。残り文数がちょうど残り段落数になったら、
        # 空段落を作らないためにここで必ず区切る。
        if need > 0 and (len(cur) >= target or rest <= need):
            paragraphs.append(cur)
            cur = ""
    if cur:
        paragraphs.append(cur)
    paragraphs.append(cta)

    print(f"📐 本文に段落が無かったので {len(paragraphs)} 段落に割り直しました")
    return "\n\n".join(paragraphs)


# ── キャプションの折り返し ───────────────────────────────────────────────
#
# ★なぜ機械で折るのか
#   モデルは字数を数えられません。「1行20字以内で」と頼んでも守られないので、
#   「文章はモデル／改行は機械」に分けます。
#
# ★幅の根拠
#   スマホのキャプションで1行に収まる目安が20字です。
#   ⚠️ 文字数ではなく全角換算の幅で数えます（半角は 0.5）。
#
# ★ここも変えてかまいません（下の値は書き換える場所ではなく、調整できる場所です）。
#   長い行が好みなら CAPTION_SOFT_WIDTH を上げてください。
#
# ★守ること
#   文字は1字も足さない・消さない（入れるのは改行だけ）。_wrap_caption_text() の
#   最後で必ず突き合わせます。言い回しの決めごとや時間帯の表現を壊さないためです。
CAPTION_WRAP_WIDTH = 20.0
# 文の終わり。いちばん強い切れ目として扱う。
SENTENCE_END = set("。！？!?")

# 目ざす形：読点のあとで改行／1行は短め／かぎかっこの中では割らない。
# 下の値は、人が手で組んだキャプションと突き合わせて決めたものです。
#
# 1行の目安の幅。これを超えると減点（上限は CAPTION_WRAP_WIDTH のまま）。
CAPTION_SOFT_WIDTH = 16.0
# 目安を超えた分への重み（超過幅の2乗にかける）。
CAPTION_OVER_WEIGHT = 2.0
# 行の途中に読点が残ったときの減点。＝「読点のあとで改行しなかった」。
CAPTION_INNER_COMMA_COST = 4.0
# 短すぎる行（読点で終わる行は除く）への減点。「ただ、」のような転換語は単独行でよい。
CAPTION_SHORT_MIN = 5.0
CAPTION_SHORT_WEIGHT = 1.0
# 開きかぎの手前は切ってよい（「先月ここで／「…」と」）。
CAPTION_OPEN_QUOTE_PENALTY = 2.0
# この幅以内のかぎかっこ（『自分のための時間』など）は中で割らない。
CAPTION_QUOTE_MAX_WIDTH = 14.0
CAPTION_QUOTE_PENALTY = 25.0
# 閉じかぎの直後の助詞で割らない（「先週のバグ」／について、にしない）。
CAPTION_CLOSE_QUOTE_PARTICLE_PENALTY = 30.0
# 漢字＋送りがな＋漢字の複合語（入れ替わる・置き換える・取り組む）を割らない。
CAPTION_COMPOUND_PENALTY = 30.0
# 複合語の後ろ側の送りがなで割らない（『文章に置き換／わって』）。
CAPTION_COMPOUND_TAIL_PENALTY = 40.0
# 短い文節を行末に置き去りにしません。動詞に掛かる短い文節（手を・力を・先に）だけが
# 行末に残ると読みにくいので（手を／動かす）、「手を動かす」の前で折ります。
# 助詞で終わる文節がこの幅以内なら、そこで折ると減点。「の」は文節の区切りに数えません。
# かぎかっこはまるごと1つの文節に数えます。
CAPTION_SHORT_PHRASE_WIDTH = 3.0
CAPTION_SHORT_PHRASE_PENALTY = 3.0
# 指示語の手前は、ひらがなが続いていても文節の切れ目（食べながら／これを）。
_CAPTION_DEMONSTRATIVES = {"これ", "それ", "あれ", "この", "その", "あの", "ここ", "そこ"}
# 助詞のあとに続くひらがなの副詞の手前も文節の切れ目（余力は／ほぼ残っていません）。
_CAPTION_HIRA_ADVERBS = ("ほぼ", "ほとんど", "まだ", "もう", "ずっと", "とても", "かなり",
                         "あまり", "たぶん", "きっと", "すぐ", "もっと", "ちょっと", "なかなか",
                         "ちゃんと")
# 副詞の直後で折ると、副詞だけが行末に残ります（今日もちゃんと／出ています）。
_CAPTION_OPEN_QUOTES = set("「『（(")
_CAPTION_CLOSE_QUOTES = set("」』）)")
_CAPTION_QUOTE_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")"}


def _display_width(text: str) -> float:
    """全角換算の表示幅。半角（ASCII・半角カナ）は 0.5 として数える。"""
    return sum(0.5 if unicodedata.east_asian_width(ch) in "NaH" else 1.0
               for ch in text)


# 書き出しの「受け」を立てる最大の幅。これより長い文は独立させない。
OPENING_BEAT_MAX_WIDTH = 12.0


def _emphasize_opening_beat(caption: str) -> str:
    """書き出しの短い「受け」を独立した段落にする。

    ★何のために
      書き出しが「読み手への問いかけ ＋ 自分の短い受け」の形のとき、

        なにかやらなきゃと思って結局何もできなかったこと、
        ありませんか。私はよくあります。          ← 受けが埋もれている

      この「私はよくあります。」を空行で立てると、読み手の側に立った書き出しが
      はっきりします。

    ★かける範囲は1段落目だけ
      この言い回しは書き出しの型なので、本文の途中まで広げると段落が細切れになる。

    🔴 入れるのは改行だけ。文字は1字も足さない・消さない。
    """
    text = caption.strip()
    if not text:
        return text
    paras = re.split(r"\n\s*\n", text)
    first = " ".join(paras[0].split("\n")).strip()
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(first)]
    sentences = [s for s in sentences if s]
    if len(sentences) < 2:
        return text                       # 1文だけの段落は触らない
    last = sentences[-1]
    if _display_width(last) > OPENING_BEAT_MAX_WIDTH:
        return text                       # 長い文は「受け」ではない
    head = "".join(sentences[:-1]).strip()
    if not head:
        return text
    paras[0] = head + "\n\n" + last
    return "\n\n".join(paras)


def _one_sentence_per_paragraph(caption: str) -> str:
    """1文ごとに段落を分ける。

    ★何のために
      スマホでは、2〜4文を1段落にまとめると塊が大きく見えます。「1段落＝1文」に
      すると読みやすくなります。

    🔴 入れるのは改行だけ。文字は1字も足さない・消さない。
    """
    text = caption.strip()
    if not text:
        return text
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        joined = "".join(ln.strip() for ln in para.split("\n"))
        if not joined:
            continue
        sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(joined)]
        sentences = [s for s in sentences if s]
        out.extend(sentences or [joined])
    return "\n\n".join(out)


def _break_allowed(text: str, j: int) -> bool:
    """text を text[:j] / text[j:] で折ってよいか（禁則）。"""
    if j <= 0 or j >= len(text):
        return False
    if text[j] in LINE_HEAD_FORBIDDEN:
        return False                      # 次の行頭が句読点・閉じ括弧・小書き文字
    if text[j - 1] in LINE_TAIL_FORBIDDEN:
        return False                      # 行末に開き括弧が取り残される
    if text[j - 1] in DIGITS:
        return False                      # 数字と助数詞が割れる（0／件）
    return True


def _break_penalty(text: str, j: int) -> float:
    """そこで折ったときの読みにくさ。小さいほど自然な切れ目。

    段落の末尾（j == len(text)）は折り目ではないので 0。
    """
    n = len(text)
    if j >= n:
        return 0.0
    prev, nxt = text[j - 1], text[j]
    if prev in SENTENCE_END:
        return 0.0                        # 文の終わり
    if prev in LINE_BREAK_PREFERRED:
        return 1.0                        # 読点・閉じ括弧のあと
    if (prev in PARTICLES and _char_class(nxt) != "hira"
            and not (prev == "の" and j >= 2 and text[j - 2] in "こそあど")):
        return 4.0                        # 助詞の直後＝文節の切れ目
    cls_prev, cls_next = _char_class(prev), _char_class(nxt)
    if cls_prev == "hira" and cls_next == "hira":
        return 26.0                       # ひらがなの途中＝語を割る（いま／す）
    if cls_prev != cls_next:
        # 文字種の切替。ひらがなへ移るところは送りがな（置き／換わる）が多い。
        return 20.0 if cls_next == "hira" else 8.0
    return 22.0                           # 同じ文字種が続く途中


def _caption_quote_inside(text: str, pref: list[float]) -> list[bool]:
    """短いかぎかっこの「中」にあたる折り目の位置を True にする。"""
    inside = [False] * (len(text) + 1)
    stack: list[tuple[str, int]] = []
    for i, ch in enumerate(text):
        if ch in _CAPTION_QUOTE_PAIRS:
            stack.append((ch, i))
        elif stack and ch == _CAPTION_QUOTE_PAIRS[stack[-1][0]]:
            _, s0 = stack.pop()
            if pref[i + 1] - pref[s0] <= CAPTION_QUOTE_MAX_WIDTH:
                for j in range(s0 + 1, i + 1):     # 開きの後〜閉じの前
                    inside[j] = True
    return inside


def _wrap_caption_line(text: str, width: float) -> list[str]:
    """1段落を幅 width 以内の行に折る。段落全体で「いちばん読みやすい割り方」を選ぶ。

    ★なぜ幅いっぱいまで詰めないのか
      幅いっぱいまで詰めてから切り位置を探すと、その範囲に自然な切れ目が1つも
      無い段落で語尾が割れる（『ことがありま／す。』『文章に置き／換わって』）。
      そこで、行ごとのコストと切れ目の悪さを足した合計が最小になる割り方を、
      段落全体から選びます（Knuth-Plass と同じ考え方）。

    ★点数のつけ方
        - 目安の幅 CAPTION_SOFT_WIDTH を超えた分だけ減点
        - 行の途中に読点が残ったら減点（＝読点のあとで改行する）
        - 短いかぎかっこの中、閉じかぎの直後の助詞では割らない
        - 短い文節（手を）を行末に置き去りにしない／複合語（置き換わる）を割らない
    """
    text = text.strip()
    if not text or _display_width(text) <= CAPTION_SOFT_WIDTH:
        return [text] if text else []

    n = len(text)
    pref = [0.0] * (n + 1)
    comma = [0] * (n + 1)
    for i, ch in enumerate(text):
        pref[i + 1] = pref[i] + _display_width(ch)
        comma[i + 1] = comma[i] + (1 if ch in "、，" else 0)
    inside = _caption_quote_inside(text, pref)

    def penalty(j: int) -> float:
        if j >= n:
            return 0.0
        p = _break_penalty(text, j)
        if text[j - 1] in _CAPTION_CLOSE_QUOTES and _char_class(text[j]) == "hira":
            return CAPTION_CLOSE_QUOTE_PARTICLE_PENALTY
        if text[j] in _CAPTION_OPEN_QUOTES and text[j - 1] not in _CAPTION_OPEN_QUOTES:
            p = min(p, CAPTION_OPEN_QUOTE_PENALTY)
        if inside[j]:
            p += CAPTION_QUOTE_PENALTY
        if (j >= 2 and _char_class(text[j - 2]) == "kanji"
                and _char_class(text[j - 1]) == "hira" and text[j - 1] not in PARTICLES
                and _char_class(text[j]) == "kanji"):
            p = max(p, CAPTION_COMPOUND_PENALTY)   # 入れ／替わる
        if (j >= 3 and _char_class(text[j - 3]) == "kanji"
                and _char_class(text[j - 2]) == "hira" and text[j - 2] not in PARTICLES
                and _char_class(text[j - 1]) == "kanji" and _char_class(text[j]) == "hira"):
            p = max(p, CAPTION_COMPOUND_TAIL_PENALTY)   # 置き換／わる
        if (text[j:j + 2] in _CAPTION_DEMONSTRATIVES and _char_class(text[j - 1]) == "hira"
                and text[j - 1] not in "、，"):
            p = min(p, 4.0)                              # 食べながら／これを
        if (text[j - 1] in PARTICLES and text[j - 1] != "の"
                and text.startswith(_CAPTION_HIRA_ADVERBS, j)):
            p = min(p, 4.0)                              # 余力は／ほぼ残って
        if any(text.endswith(a, 0, j) for a in _CAPTION_HIRA_ADVERBS):
            p += CAPTION_SHORT_PHRASE_PENALTY             # ちゃんと／出ています
        if text[j - 1] in PARTICLES and _char_class(text[j]) != "hira":
            if short_phrase_before(j):
                p += CAPTION_SHORT_PHRASE_PENALTY         # 手を／動かす
        return p

    def short_phrase_before(j: int) -> bool:
        """j の手前の文節（助詞で終わる）が CAPTION_SHORT_PHRASE_WIDTH 以内か。"""
        b, depth = j - 1, 0
        while b > 0:
            ch = text[b - 1]
            if ch in _CAPTION_CLOSE_QUOTES:
                depth += 1                # かぎかっこはまるごと1つの文節に数える
            elif ch in _CAPTION_OPEN_QUOTES and depth:
                depth -= 1
            elif depth == 0 and (ch in "、，。！？"
                                 or text[b:b + 2] in _CAPTION_DEMONSTRATIVES
                                 or (ch in PARTICLES and ch != "の"
                                     and _char_class(text[b]) != "hira")):
                break
            b -= 1
        return pref[j] - pref[b] <= CAPTION_SHORT_PHRASE_WIDTH

    def solve(strict: bool) -> list[str] | None:
        INF = float("inf")
        best = [INF] * (n + 1)
        back = [0] * (n + 1)
        best[0] = 0.0
        for j in range(1, n + 1):
            if j < n and strict and not _break_allowed(text, j):
                continue                  # ここでは折れない
            pen = penalty(j)
            for i in range(j - 1, -1, -1):
                if best[i] == INF:
                    continue
                lw = pref[j] - pref[i]
                if lw > width:
                    break                 # これ以上さかのぼると行が幅を超える
                over = max(0.0, lw - CAPTION_SOFT_WIDTH)
                cost = over * over * CAPTION_OVER_WEIGHT
                # 行の途中（末尾以外）に読点が残っている＝そこで改行しなかった
                cost += (comma[j - 1] - comma[i]) * CAPTION_INNER_COMMA_COST if j > i + 1 else 0.0
                if lw < CAPTION_SHORT_MIN and text[j - 1] not in "、，" and j < n:
                    cost += (CAPTION_SHORT_MIN - lw) ** 2 * CAPTION_SHORT_WEIGHT
                total = best[i] + pen + cost
                if total < best[j]:
                    best[j] = total
                    back[j] = i
        if best[n] == INF:
            return None
        out, j = [], n
        while j > 0:
            out.append(text[back[j]:j])
            j = back[j]
        return out[::-1]

    lines = solve(strict=True) or solve(strict=False)
    if lines is None:                     # 1文字が幅を超えるなど、理論上ここには来ない
        return [text]
    return [ln for ln in lines if ln]


def _wrap_caption_text(caption: str, width: float = CAPTION_WRAP_WIDTH) -> str:
    """段落ごとに折り返す。段落の区切り（空行）はそのまま残す。

    🔴 文字は1字も足さない・消さない。最後に突き合わせて、違っていたら
       折り返す前の本文をそのまま返す（投稿を止めない）。
    """
    before = caption.strip()
    if not before:
        return before

    out: list[str] = []
    for para in re.split(r"\n\s*\n", before):
        para = " ".join(para.split("\n")).strip()
        if not para:
            continue
        out.append("\n".join(_wrap_caption_line(para, width)))
    wrapped = "\n\n".join(out)

    # 文字の同一性チェック（改行と、段落結合で落ちる空白だけを無視する）
    def _bare(t: str) -> str:
        return re.sub(r"[\s]", "", t)
    if _bare(wrapped) != _bare(before):
        print("⚠️ キャプションの折り返しで文字が変わったので、折り返さずに出します")
        return before
    return wrapped


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
    # 語句の置換がすべて終わってから段落を保証する（置換で文がつながることがあるため）
    caption = _ensure_paragraphs(caption)
    # 書き出しの短い「受け」を独立させてから折り返す（段落の分割が先）
    caption = _emphasize_opening_beat(caption)
    # 1文ごとに段落を分ける
    caption = _one_sentence_per_paragraph(caption)
    # 段落が確定してから機械で折り返す（順序を逆にすると、折り返しで入れた改行を
    # _ensure_paragraphs が段落の区切りと誤解して空行に格上げしてしまう）
    caption = _wrap_caption_text(caption)

    return {
        "pillar": pillar_key,
        "headline": headline,
        "caption": caption,
        "hashtags": hashtags,
        "mood": mood,
    }


def _weekend_violation(content: dict) -> tuple[str, str] | None:
    """土日投稿に平日・勤務前提の語があれば (重さ, 検出語) を返す。

    重さ＝"heavy"（その日の事実と食い違う）／"light"（対比としてなら正当）。
    """
    text = f"{content['headline']}\n{content['caption']}"
    hit = next((ng for ng in WEEKEND_NG_HEAVY if ng in text), None)
    if hit:
        return ("heavy", hit)
    for rx in WEEKEND_NG_HEAVY_RES:
        m = rx.search(text)
        if m:
            return ("heavy", m.group(0))
    hit = next((ng for ng in WEEKEND_NG_LIGHT if ng in text), None)
    if hit:
        return ("light", hit)
    return None


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


def _fabrication_violation(content: dict) -> str | None:
    """実際には起きていない出来事を書いた疑いがあれば、その文の一部を返す。

    判定＝ひとつの文の中に「過去の目印」と「不具合の語」が並んでいること。
    ただし `EPISODE_KEYS`（実在の出来事を指す語）が同じ文にあれば通します
    ＝一覧から選んで書いた場合まで弾くと、書ける具体が何も残らないためです。

    ★これは網ではなく最後の砦です。本命はプロンプト側（_facts_context）で、
      ここは同じ形の創作が**静かに通る**のを防ぐためのものです。
    """
    text = f"{content['headline']}\n{content['caption']}"
    for sentence in SENTENCE_SPLIT_RE.split(text):
        if not sentence.strip():
            continue
        if any(k in sentence for k in EPISODE_KEYS):
            continue                       # 実在の出来事の話とみなす
        marker = next((m for m in FACT_PAST_MARKERS if m in sentence), None)
        if marker is None:
            for rx in FACT_PAST_RES:
                m = rx.search(sentence)
                if m:
                    marker = m.group(0)
                    break
        if marker is None:
            continue
        trouble = next((w for w in FACT_TROUBLE_WORDS if w in sentence), None)
        if trouble:
            return f"{marker}…{trouble}"
    return None


def _self_heal_violation(content: dict) -> str | None:
    """「止まっても自動で復旧する」と書いた疑いがあれば、その語の組を返す。

    そんな仕組みはありません（止まったときに直しているのは人です）。
    実在の出来事を指す語があっても通しません
    ＝ EPISODE_KEYS による除外は、ここでは行いません。
    """
    text = f"{content['headline']}\n{content['caption']}"
    for sentence in SENTENCE_SPLIT_RE.split(text):
        if not sentence.strip():
            continue
        auto = next((w for w in SELF_HEAL_AUTO_WORDS if w in sentence), None)
        if auto is None:
            continue
        rec = next((w for w in SELF_HEAL_RECOVER_WORDS if w in sentence), None)
        if rec:
            return f"{auto}…{rec}"
    return None


def _years_back(word: str) -> int:
    """「去年」→1、「一昨年」→2、「3年前」→3。"""
    if word in ("去年", "昨年", "前年"):
        return 1
    if word in ("一昨年", "おととし"):
        return 2
    m = re.match(r"([0-9０-９]+|[一二三四五六七八九十])", word)
    n = m.group(1)
    return _KANJI_DIGITS.get(n) or int(unicodedata.normalize("NFKC", n))


def _year_violation(content: dict, today: datetime | None) -> str | None:
    """仕組みの話の文に、実際の出来事が無い年（去年・◯年前）が出ていれば、その語を返す。

    かぎかっこの中（「去年6月」と書いてしまった、のような引用）は通します。
    ★これも網ではなく最後の砦です。本命はプロンプトに今日の日付を渡すこと（_facts_context）。
    """
    if today is None:
        return None
    years = episode_years()
    text = f"{content['headline']}\n{content['caption']}"
    for sentence in SENTENCE_SPLIT_RE.split(text):
        if not sentence.strip():
            continue
        bare = re.sub(r"「[^」]*」|『[^』]*』", "", sentence)
        if not any(w in bare for w in YEAR_SYSTEM_WORDS):
            continue
        for m in YEAR_PAST_RE.finditer(bare):
            if today.year - _years_back(m.group(1)) not in years:
                return m.group(1)
    return None


def _find_violation(content: dict, weekend: bool, slot: str,
                    today: datetime | None = None) -> tuple[str, str, str] | None:
    """土日ガード・時間帯ガード・創作ガードをまとめて評価し、(種別, 検出語, 重さ) を返す。

    時間帯の違反は「存在しない時刻を書いた」等の事実誤りなので常に heavy。
    創作（起きていない出来事）も同じ理由で常に heavy。
    """
    if weekend:
        found = _weekend_violation(content)
        if found:
            severity, hit = found
            return ("土日", hit, severity)
    hit = _time_violation(content, slot)
    if hit:
        return ("時間帯", hit, "heavy")
    hit = _fabrication_violation(content)
    if hit:
        return ("事実", hit, "heavy")
    hit = _self_heal_violation(content)
    if hit:
        return ("事実", hit, "heavy")
    hit = _year_violation(content, today)
    if hit:
        return ("事実", hit, "heavy")
    return None


def generate_content(pillar_key: str, date: datetime, slot: str = "A") -> dict:
    weekend = is_weekend(date)
    client = claude_client()

    if client is None:
        content = _normalize_content(
            _fallback_content(pillar_key, weekend, slot), pillar_key, weekend)
        print(f"📝 フォールバックのネタを使用（柱{pillar_key}{'・休日版' if weekend else ''}）")
        return content

    try:
        data = claude_json(client, build_prompt(pillar_key, date, slot))
        print(f"📝 Claude がネタを生成（柱{pillar_key}・{TIME_SLOTS[slot]['label']}枠）")
    except Exception as e:
        print(f"⚠️  Claude 生成に失敗 → フォールバック: {type(e).__name__}: {e}")
        return _normalize_content(
            _fallback_content(pillar_key, weekend, slot), pillar_key, weekend)

    content = _normalize_content(data, pillar_key, weekend)

    # ガード：土日に平日・勤務前提の語、またはその枠の時間帯と矛盾する表現が
    # 混入していたら一度だけ厳しめに再生成する。
    # ★そのあとの扱いは「違反の重さ」で分けています。フォールバックに落とすのは
    #   重い違反が残ったときだけです。軽い違反（対比としてなら正当な語）で
    #   予備の文章に落としてしまうと、AIが書いた文章より確実に平板になるうえ、
    #   同じ日の2つの枠が同じ文章になってしまうことがあります。
    found = _find_violation(content, weekend, slot, date)
    if found:
        kind, hit, severity = found
        why = ("起きていない出来事を書いた疑いのある" if kind == "事実"
               else "実態と矛盾する" if severity == "heavy"
               else "対比なら正当だが紛らわしい")
        print(f"⚠️  {kind}チェック: {why}表現「{hit}」を検出 → 再生成します")
        try:
            data2 = claude_json(client, build_prompt(
                pillar_key, date, slot, strict_weekend=True, strict_time=True,
                strict_facts=(kind == "事実")))
            content2 = _normalize_content(data2, pillar_key, weekend)
        except Exception as e:
            print(f"⚠️  再生成に失敗: {type(e).__name__}: {e}")
            content2 = None

        if content2 is None:
            still = found          # 再生成できなかった＝初回の判定のまま
        else:
            content = content2
            still = _find_violation(content2, weekend, slot, date)

        if still is None:
            print("   → 再生成で解消")
        elif still[2] == "light":
            print(f"   → 再生成後も「{still[1]}」が残るが、対比表現として許容し採用する")
        else:
            print(f"   → 再生成でも重い違反が残存（{still[0]}／{still[1]}）→ フォールバックに差し替え")
            content = _normalize_content(
                _fallback_content(pillar_key, weekend, slot), pillar_key, weekend)

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
# 行末禁則文字（行の末尾に置けない文字＝開き括弧。直後で改行すると
# 「『仕』と『／組み』の話」のように開き括弧だけが行末に取り残されます）
LINE_TAIL_FORBIDDEN = set("「『（｛【〈《〔［‘“([{")
# 行末に来ると自然な区切り文字（この文字の直後での改行を優先する）
# ※長音「ー」は語の途中なので入れない（「エー／ジェント」のような分断を避ける）
LINE_BREAK_PREFERRED = set("、。，．！？!?…―」』）】")

# 数字の直後では改行しません（数字＋助数詞・単位を1語として扱う。「エラー0／件」を防ぐ）。
DIGITS = set("0123456789０１２３４５６７８９")
# 助詞の直後は文節の切れ目なので改行してかまいません。ただし次の文字がひらがなのときは
# 除きます（助詞でない同じ字を拾って「という」を「と／いう」で割ってしまうため）。
# 「か」「ね」「よ」は文末寄りで語中に紛れやすいので入れていません。
PARTICLES = set("のはがをにへとでもやらば")


def wrap_japanese(text: str, max_chars: int, max_lines: int = 3) -> list[str]:
    """見出しを読みやすく折り返す。

    従来は max_chars ちょうどで貪欲に改行していたため「休日も動いてる、仕／組みの話」
    のように文節の途中でぶつ切りになっていた。これを次の3点で改善する：
      1. 行数をまず決め（総字数 ÷ max_chars を切り上げ）、各行の字数を均等に近づける。
      2. 句読点（、。！？など）の直後を改行位置として優先する。
      3. 行頭禁則文字が行頭に落ちる位置では改行しない（前行にぶら下げる）。
      4. 開き括弧が行末に取り残される位置でも改行しない。
      5. 3・4を守れる切り位置が1つも無いときは、行を1本増やして切り直す
         （増やせる上限＝max_lines。見出しの描画は3行までなので既定は3）。
    明示改行（\\n）はそのまま尊重する。
    """
    lines: list[str] = []
    for segment in text.split("\n"):
        lines.extend(_wrap_segment(segment, max_chars, max_lines))
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


def _wrap_segment(text: str, max_chars: int, max_lines: int = 3) -> list[str]:
    n = len(text)
    if n <= max_chars:
        return [text]
    base = -(-n // max_chars)             # 切り上げ除算＝必要な最小行数
    # まず最小行数で、禁則を1つも破らずに切れるか試す。無理なら1行増やして試す
    # （「設定を間違えて止まった（けど直った）」のように 9字×2 では
    #   合法な切れ目が存在しない見出しがある）。
    for n_lines in range(base, max(base, max_lines) + 1):
        lines = _split(text, max_chars, n_lines, strict=True)
        if lines is not None:
            return lines
    return _split(text, max_chars, base, strict=False)


def _split(text: str, max_chars: int, n_lines: int, strict: bool) -> list[str] | None:
    """n_lines 行ちょうどに割る。strict のとき、禁則を破らないと割れなければ None。"""
    n = len(text)
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
            if text[b - 1] in LINE_TAIL_FORBIDDEN:
                continue                  # 行末が開き括弧になる位置では切らない
            if text[b - 1] in DIGITS:
                continue                  # 数字と助数詞のあいだでは切らない（0／件）
            if text[b - 1] in "っッ":
                continue                  # 促音で行を終えない（エージェントっ／て何？）
            if (strict and _char_class(text[b - 1]) == _char_class(text[b])
                    and _char_class(text[b]) in ("kata", "alnum")):
                # カタカナ語・英単語の途中では切らない（「エージェン／トって何？」）。
                # この行数で割れなければ、_wrap_segment が1行増やして切り直す。
                continue
            score = -abs(b - ideal)       # 理想位置に近いほど高評価
            if text[b - 1] in LINE_BREAK_PREFERRED:
                score += 5                # 句読点などの直後で切れると読みやすい
            elif text[b - 1] in PARTICLES and _char_class(text[b]) != "hira":
                score += 4                # 助詞の直後＝文節の切れ目（人の／生存戦略）
            elif _char_class(text[b - 1]) != _char_class(text[b]):
                # 文字種の切替は語境界のことが多いが、漢字・カナ→ひらがなは
                # 送りがな（仕組/み）や助詞の途中で割れやすいので加点しない。
                score += -1 if _char_class(text[b]) == "hira" else 2
            if best_score is None or score > best_score:
                best, best_score = b, score
        if best is None:
            if strict:
                return None               # この行数では禁則を破らずに割れない
            best = min(start + max_chars, n)  # 最後の手段（はみ出しだけは避ける）
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
