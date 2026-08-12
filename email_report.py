"""週次レポートをメール送信（標準ライブラリ smtplib のみ）。@あなたのユーザーネーム 用。

`analytics/latest_report.md` を読み、本文（プレーンテキスト）として送る。
.md ファイルも添付する。

既存の失敗通知（notify.py）と**同じ SMTP secrets を流用**するので、
GitHub 側の追加設定は不要。宛先だけ REPORT_EMAIL_TO で分けられる。

  SMTP_HOST         （既定 smtp.gmail.com）
  SMTP_PORT         （既定 587 / STARTTLS）
  SMTP_USER         送信元アカウント（your-address@example.com）
  SMTP_PASSWORD     Gmail アプリパスワード
  REPORT_EMAIL_TO   宛先（既定 NOTIFY_EMAIL_TO → SMTP_USER）
  REPORT_EMAIL_FROM 差出人（既定 SMTP_USER）

SMTP 設定が無い場合はスキップ（exit 0）。レポート自体はリポジトリに
コミット済みなので、メールが送れないことでワークフローを止めない。
"""

from __future__ import annotations

import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LATEST_REPORT = ROOT / "analytics" / "latest_report.md"

JST = timezone(timedelta(hours=9))


def _env(name: str, default: str | None = None) -> str | None:
    """未登録の secret は空文字で渡ってくるため、空も「未設定」として扱う。"""
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def main() -> None:
    host = _env("SMTP_HOST", "smtp.gmail.com")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    to_addr = _env("REPORT_EMAIL_TO") or _env("NOTIFY_EMAIL_TO") or user
    from_addr = _env("REPORT_EMAIL_FROM") or user

    if not user or not password:
        print("⚠️  SMTP_USER / SMTP_PASSWORD 未設定 → メール送信をスキップ"
              "（レポートはリポジトリにコミット済み）")
        sys.exit(0)

    try:
        port = int(_env("SMTP_PORT", "587"))
    except (TypeError, ValueError):
        port = 587
    if not to_addr:
        print("⚠️  宛先（REPORT_EMAIL_TO）が決まりません → スキップ")
        sys.exit(0)

    if not LATEST_REPORT.exists():
        print(f"❌ レポートが見つかりません: {LATEST_REPORT}")
        sys.exit(1)

    body = LATEST_REPORT.read_text(encoding="utf-8")
    date_str = datetime.now(JST).strftime("%Y-%m-%d")

    msg = EmailMessage()
    msg["Subject"] = f"[あなたのユーザーネーム] 週次インサイト {date_str}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    msg.add_attachment(
        body.encode("utf-8"),
        maintype="text", subtype="markdown",
        filename=f"weekly_report_{date_str}.md",
    )

    print(f"✉️  送信中: {from_addr} → {to_addr} via {host}:{port}")
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    print("✅ メール送信完了")


if __name__ == "__main__":
    main()
