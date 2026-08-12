#!/usr/bin/env python3
"""§4 登録後の検証：4つの疎通テストをまとめて実行する。

同ディレクトリの .env から値を読む（.env.example をコピーして実値を記入）。
値が無いテストは自動でスキップする。secrets は標準出力に出さない（末尾4文字のみ表示）。

使い方:
    python3 src/validation/validate.py            # 全テスト
    python3 src/validation/validate.py token      # 指定テストのみ (token/image/smtp/claude)
"""
import os
import sys
import ssl
import smtplib
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")
GRAPH_VER = "v21.0"


def load_env():
    if not ENV_PATH.exists():
        sys.exit(f"❌ {ENV_PATH} がありません。.env.example をコピーして実値を記入してください。")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def mask(v):
    if not v:
        return "(空)"
    return f"…{v[-4:]} (len={len(v)})"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "insta-auto-validate"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- ① トークン疎通
def test_token():
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    acct = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
    if not token or not acct:
        return None, "INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_ACCOUNT_ID 未設定"
    url = (f"https://graph.facebook.com/{GRAPH_VER}/{acct}"
           f"?fields=username&access_token={token}")
    try:
        status, body = http_get(url)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if '"username"' not in body:
        return False, f"username が返ってこない: {body[:300]}"
    # ★EXPECTED_USERNAME を設定しておくと「宛先を取り違えていないか」まで確認できます。
    #   未設定なら「鍵と宛先で情報が引けた」ところまでの確認になります。
    expected = os.environ.get("EXPECTED_USERNAME", "").lstrip("@")
    if expected and f'"{expected}"' not in body:
        return False, f"username が {expected} ではない: {body[:300]}"
    return True, f"username を確認 ({body.strip()})"


# ---------------------------------------------------------------- ② 画像 push 疎通
def test_image():
    repo = os.environ.get("IMAGES_REPO", "")
    pat = os.environ.get("IMAGES_REPO_PAT", "")
    if not repo or not pat:
        return None, "IMAGES_REPO / IMAGES_REPO_PAT 未設定"
    remote = f"https://x-access-token:{pat}@github.com/{repo}.git"
    fname = "_validation_ping.txt"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args):
            return subprocess.run(["git", "-C", tmp, *args],
                                  capture_output=True, text=True)
        r = subprocess.run(["git", "clone", "--depth", "1", remote, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"clone 失敗: {r.stderr.strip()[:300]}"
        (Path(tmp) / fname).write_text(f"validation ping {stamp}\n", encoding="utf-8")
        git("config", "user.email", "your-address@example.com")
        git("config", "user.name", "insta-auto-validate")
        git("add", fname)
        c = git("commit", "-m", f"validation ping {stamp}")
        if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
            return False, f"commit 失敗: {(c.stdout + c.stderr).strip()[:300]}"
        p = git("push", "origin", "HEAD")
        if p.returncode != 0:
            return False, f"push 失敗: {p.stderr.strip()[:300]}"
        head = git("rev-parse", "HEAD").stdout.strip()
    raw = f"https://raw.githubusercontent.com/{repo}/{head}/{fname}"
    time.sleep(2)
    try:
        status, body = http_get(raw)
    except Exception as e:
        return False, f"push成功だが raw URL が開けない: {e}\n   {raw}"
    if status == 200 and "validation ping" in body:
        return True, f"push→raw URL 取得OK\n   {raw}"
    return False, f"raw URL の内容が不一致 (status={status}): {raw}"


# ---------------------------------------------------------------- ③ SMTP 疎通
def test_smtp():
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASSWORD", "")
    to = os.environ.get("SMTP_TO", "") or user
    if not user or not pw:
        return None, "SMTP_USER / SMTP_PASSWORD 未設定"
    msg = MIMEText("自動投稿システム：SMTP 疎通テスト。これが届けば通知経路OK。",
                   _charset="utf-8")
    msg["Subject"] = "[あなたのユーザーネーム] SMTP 疎通テスト"
    msg["From"] = user
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"テストメール送信成功 → {to}（受信トレイを確認）"


# ---------------------------------------------------------------- ④ Claude 疎通
def test_claude():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None, "ANTHROPIC_API_KEY 未設定"
    try:
        import anthropic
    except ImportError:
        return False, "anthropic SDK が未インストール (pip install anthropic)"
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=20,
            messages=[{"role": "user", "content": "「疎通OK」とだけ返して"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"応答取得OK: {text!r}"


TESTS = {
    "token": ("① トークン疎通 (Graph API username)", test_token),
    "image": ("② 画像 push 疎通 (PAT→raw URL)", test_image),
    "smtp": ("③ SMTP 疎通 (Gmail テストメール)", test_smtp),
    "claude": ("④ Claude 疎通 (Anthropic API)", test_claude),
}


def main():
    load_env()
    which = sys.argv[1:] or list(TESTS)
    print("=== §4 登録後の検証 ===")
    print(f"INSTAGRAM_ACCESS_TOKEN: {mask(os.environ.get('INSTAGRAM_ACCESS_TOKEN',''))}")
    print(f"IMAGES_REPO_PAT       : {mask(os.environ.get('IMAGES_REPO_PAT',''))}")
    print(f"SMTP_PASSWORD         : {mask(os.environ.get('SMTP_PASSWORD',''))}")
    print(f"ANTHROPIC_API_KEY     : {mask(os.environ.get('ANTHROPIC_API_KEY',''))}")
    print()
    results = {}
    for key in which:
        if key not in TESTS:
            print(f"⚠️ 不明なテスト: {key}")
            continue
        label, fn = TESTS[key]
        print(f"▶ {label}")
        ok, detail = fn()
        icon = {True: "✅ PASS", False: "❌ FAIL", None: "⏭  SKIP"}[ok]
        print(f"   {icon} — {detail}\n")
        results[key] = ok
    passed = sum(1 for v in results.values() if v is True)
    failed = sum(1 for v in results.values() if v is False)
    skipped = sum(1 for v in results.values() if v is None)
    print(f"=== 結果: PASS {passed} / FAIL {failed} / SKIP {skipped} ===")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
