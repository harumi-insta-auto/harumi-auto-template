"""失敗通知メールを送る（標準ライブラリ smtplib のみ）。

週次レポート送信（email_report.py）の SMTP 部分を
@あなたのユーザーネーム 用に転用したもの。用途は「自動投稿が失敗したときの通知」。

GitHub Actions の daily_post ワークフローで、投稿ステップが失敗したとき
（if: failure()）にこのスクリプトを呼ぶ。実行ログの URL を本文に載せる。

設定は環境変数（GitHub secrets）で渡す:
  SMTP_HOST        (既定 smtp.gmail.com)
  SMTP_PORT        (既定 587 / STARTTLS)
  SMTP_USER        送信元アカウント（your-address@example.com）
  SMTP_PASSWORD    Gmail アプリパスワード（2段階認証オンで発行したもの）
  NOTIFY_EMAIL_TO  宛先（既定 SMTP_USER）
任意（本文に含める文脈・ワークフローから渡す）:
  NOTIFY_SUBJECT   件名の上書き
  NOTIFY_BODY      本文の上書き／追記
  GH_RUN_URL       失敗した GitHub Actions 実行のURL

SMTP 設定が無い場合はスキップ（exit 0）。通知が送れないこと自体で
ワークフローをさらにこかせない。
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

JST = timezone(timedelta(hours=9))


def _env(name: str, default: str | None = None) -> str | None:
    """未登録の secret は空文字で渡ってくるため、空も「未設定」として扱う。"""
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def main() -> None:
    host = _env("SMTP_HOST", "smtp.gmail.com")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    to_addr = _env("NOTIFY_EMAIL_TO") or user
    from_addr = user

    if not user or not password:
        print("⚠️  SMTP_USER / SMTP_PASSWORD 未設定 → 失敗通知メールをスキップ")
        sys.exit(0)

    try:
        port = int(_env("SMTP_PORT", "587"))
    except (TypeError, ValueError):
        port = 587
    if not to_addr:
        print("⚠️  宛先 (NOTIFY_EMAIL_TO) が決まりません → スキップ")
        sys.exit(0)

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    subject = _env("NOTIFY_SUBJECT") or f"[あなたのユーザーネーム] 自動投稿が失敗しました {now}"

    run_url = _env("GH_RUN_URL", "")
    body_lines = [
        "@あなたのユーザーネーム の自動投稿ワークフローが失敗しました。",
        f"発生時刻: {now}",
    ]
    if run_url:
        body_lines += ["", f"実行ログ: {run_url}"]
    extra = _env("NOTIFY_BODY")
    if extra:
        body_lines += ["", extra]
    body_lines += [
        "",
        "確認の目安:",
        "  - INSTAGRAM_ACCESS_TOKEN の有効期限（永続ページトークンだが念のため）",
        "  - IMAGES_REPO_PAT の有効期限（期限つきの鍵です）",
        "  - ANTHROPIC_API_KEY の残高・レート",
        "  - Graph API の仕様変更（v21.0 廃止など）",
    ]
    body = "\n".join(body_lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    print(f"✉️  失敗通知を送信中: {from_addr} → {to_addr} via {host}:{port}")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=ctx)
        server.login(user, password)
        server.send_message(msg)
    print("✅ 失敗通知メール送信完了")


if __name__ == "__main__":
    main()
