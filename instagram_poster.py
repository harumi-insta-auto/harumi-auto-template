"""
outputs/post.json と公開済みのメディア URL を使って Instagram に自動投稿する。

Instagram Graph API（Pageトークン方式）でフィード投稿を行う。
初期は IMAGE（フィード画像投稿）のみ使用する想定だが、STORIES / REELS の
分岐も流用元のまま残している（将来の任意機能）。

⚠️ 流用元との最大の違い：Graph API のベース URL。
  - Instagram ログイン方式の場合は graph.instagram.com になる。
  - @あなたのユーザーネーム は「Facebook ログインによる API 設定」で取得した
    **Page トークン + instagram_business_account ID** 方式のため graph.facebook.com を使う。
    （§4 検証 validate.py も graph.facebook.com で username 疎通を確認済み。）
  必要なら環境変数 GRAPH_API_BASE で上書き可能。

必要な環境変数:
  - INSTAGRAM_ACCESS_TOKEN  Graph API の長期（永続）ページトークン
  - INSTAGRAM_ACCOUNT_ID    ビジネスアカウントの IG User ID（@あなたのユーザーネーム）
  - POST_IMAGE_URL          IMAGE/画像 STORIES 用の公開 URL
  - POST_VIDEO_URL          REELS/動画 STORIES 用の公開 URL
任意:
  - POST_COVER_URL          REELS のカバー画像 URL
  - REELS_SHARE_TO_FEED     "true"/"false" (デフォルト "true")
  - GRAPH_API_BASE          Graph API ベース URL の上書き（デフォルト graph.facebook.com）
  - GRAPH_API_VERSION       API バージョン（デフォルト v21.0）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "outputs"
POST_JSON = OUT_DIR / "post.json"

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
# @あなたのユーザーネーム は Page トークン方式のため graph.facebook.com を既定にする。
GRAPH_API_HOST = os.environ.get("GRAPH_API_BASE", "https://graph.facebook.com")
GRAPH_BASE = f"{GRAPH_API_HOST.rstrip('/')}/{GRAPH_API_VERSION}"

# REELS / 動画 STORIES は処理が長い (Meta 側のトランスコード)。
# 画像系の 60s だとタイムアウトしがちなので分けておく。
WAIT_TIMEOUT_IMAGE = 60
WAIT_TIMEOUT_VIDEO = 180


def _check_response(res: requests.Response, label: str) -> dict:
    """Graph API のレスポンスを検査し、失敗時はエラーボディを表示してから raise"""
    if not res.ok:
        print(f"❌ {label} failed: HTTP {res.status_code}")
        try:
            print(f"   response: {json.dumps(res.json(), ensure_ascii=False)}")
        except ValueError:
            print(f"   response: {res.text}")
        res.raise_for_status()
    return res.json()


def create_media_container(
    ig_user_id: str,
    access_token: str,
    *,
    image_url: str | None = None,
    video_url: str | None = None,
    cover_url: str | None = None,
    caption: str = "",
    media_type: str = "IMAGE",
    share_to_feed: bool | None = None,
) -> str:
    """Instagram media container を作成し creation_id を返す"""
    print(f"🧱 Instagram media container 作成中... (media_type={media_type})")

    data: dict[str, str] = {"access_token": access_token}

    if media_type == "REELS":
        if not video_url:
            raise ValueError("REELS requires video_url")
        print(f"   video_url: {video_url}")
        print(f"   caption length: {len(caption)} chars")
        data["media_type"] = "REELS"
        data["video_url"] = video_url
        if caption:
            data["caption"] = caption
        if cover_url:
            print(f"   cover_url: {cover_url}")
            data["cover_url"] = cover_url
        if share_to_feed is not None:
            data["share_to_feed"] = "true" if share_to_feed else "false"
            print(f"   share_to_feed: {data['share_to_feed']}")

    elif media_type == "STORIES":
        data["media_type"] = "STORIES"
        if video_url:
            print(f"   video_url: {video_url}  (video story)")
            data["video_url"] = video_url
        elif image_url:
            print(f"   image_url: {image_url}  (image story)")
            data["image_url"] = image_url
        else:
            raise ValueError("STORIES requires image_url or video_url")
        # ストーリーズは caption 非対応

    else:  # IMAGE (default)
        if not image_url:
            raise ValueError("IMAGE requires image_url")
        print(f"   image_url: {image_url}")
        print(f"   caption length: {len(caption)} chars")
        data["image_url"] = image_url
        if caption:
            data["caption"] = caption

    res = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        data=data,
        timeout=60,
    )
    body = _check_response(res, "create_media_container")
    creation_id = body["id"]
    print(f"✅ creation_id: {creation_id}")
    return creation_id


def wait_until_ready(creation_id: str, access_token: str,
                     max_wait_sec: int = WAIT_TIMEOUT_IMAGE) -> None:
    """container が FINISHED になるまで待機"""
    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        res = requests.get(
            f"{GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=30,
        )
        body = _check_response(res, "container status")
        status = body.get("status_code")
        print(f"   status: {status}")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Container processing error: {body}")
        time.sleep(3)
    raise TimeoutError(f"Container did not become FINISHED in {max_wait_sec}s")


def publish_media(ig_user_id: str, access_token: str,
                  creation_id: str) -> str:
    """media_publish を呼び出して投稿を確定。Instagram media ID を返す"""
    print("🚀 投稿を公開中...")
    res = requests.post(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        data={
            "creation_id": creation_id,
            "access_token": access_token,
        },
        timeout=60,
    )
    body = _check_response(res, "media_publish")
    media_id = body["id"]
    print(f"✅ 投稿完了 media_id: {media_id}")
    return media_id


def _resolve_share_to_feed() -> bool:
    raw = os.environ.get("REELS_SHARE_TO_FEED", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _label(media_type: str, is_video: bool) -> str:
    if media_type == "REELS":
        return "リール"
    if media_type == "STORIES":
        return "動画ストーリー" if is_video else "ストーリー"
    return "フィード"


def main():
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    ig_user_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    image_url = os.environ.get("POST_IMAGE_URL")
    video_url = os.environ.get("POST_VIDEO_URL")
    cover_url = os.environ.get("POST_COVER_URL")

    base_required = {
        "INSTAGRAM_ACCESS_TOKEN": access_token,
        "INSTAGRAM_ACCOUNT_ID": ig_user_id,
    }
    missing = [k for k, v in base_required.items() if not v]
    if missing:
        print(f"❌ 環境変数が未設定: {', '.join(missing)}")
        sys.exit(1)

    if not POST_JSON.exists():
        print("❌ outputs/post.json が見つかりません。"
              "先に content_generator.py を実行してください")
        sys.exit(1)

    post = json.loads(POST_JSON.read_text(encoding="utf-8"))
    media_type = post.get("media_type", "IMAGE").upper()
    has_video_field = bool(post.get("video"))
    is_video = media_type == "REELS" or (media_type == "STORIES" and has_video_field)

    # 必須 URL を media_type ごとにチェック
    if is_video and not video_url:
        print("❌ POST_VIDEO_URL が未設定です "
              f"(media_type={media_type}, post.json video={post.get('video')!r})")
        sys.exit(1)
    if not is_video and not image_url:
        print(f"❌ POST_IMAGE_URL が未設定です (media_type={media_type})")
        sys.exit(1)

    # キャプション
    if media_type == "STORIES":
        caption = ""
    else:
        # IMAGE / REELS は post_text (caption + ハッシュタグ整形済み)
        caption = post.get("post_text") or post.get("caption") or ""

    # share_to_feed は REELS 専用
    share_to_feed = _resolve_share_to_feed() if media_type == "REELS" else None

    creation_id = create_media_container(
        ig_user_id, access_token,
        image_url=image_url if not is_video else None,
        video_url=video_url if is_video else None,
        cover_url=cover_url if media_type == "REELS" else None,
        caption=caption,
        media_type=media_type,
        share_to_feed=share_to_feed,
    )

    wait_timeout = WAIT_TIMEOUT_VIDEO if is_video else WAIT_TIMEOUT_IMAGE
    wait_until_ready(creation_id, access_token, max_wait_sec=wait_timeout)
    publish_media(ig_user_id, access_token, creation_id)
    print(f"\n🎉 Instagram への自動{_label(media_type, is_video)}投稿が完了しました")


if __name__ == "__main__":
    main()
