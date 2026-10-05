"""
Social Account Management and Publishing Service for YouTube and TikTok.
Handles account linking (OAuth & Upload-Post profiles), persistent account storage,
video preview publishing, and automated social uploading.
"""
from datetime import datetime, timezone
import json
import os
import threading
from typing import Optional
from uuid import uuid4

from loguru import logger
import requests

from app.config import config
from app.services.upload_post import upload_post_service, UploadPostService
from app.utils import utils


_ACCOUNTS_FILE_NAME = "social_accounts.json"
_PUBLISH_HISTORY_FILE_NAME = "publish_history.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class SocialAccountService:
    def __init__(self, storage_dir: Optional[str] = None):
        self._lock = threading.RLock()
        self._accounts_cache: list[dict] | None = None
        self._custom_storage_dir = storage_dir

    def _get_storage_path(self, filename: str) -> str:
        if self._custom_storage_dir:
            os.makedirs(self._custom_storage_dir, exist_ok=True)
            return os.path.join(self._custom_storage_dir, filename)
        storage_dir = utils.storage_dir(create=True)
        return os.path.join(storage_dir, filename)

    def _load_accounts_unlocked(self) -> list[dict]:
        path = self._get_storage_path(_ACCOUNTS_FILE_NAME)
        if not os.path.exists(path):
            # Seed default accounts from config.app if available
            initial_accounts = self._seed_default_accounts()
            self._save_accounts_unlocked(initial_accounts)
            return initial_accounts
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                return []
        except Exception as exc:
            logger.error(f"Failed to read {path}: {exc}")
            return []

    def _seed_default_accounts(self) -> list[dict]:
        """Create initial placeholder/configured accounts based on config.toml if present."""
        accounts = []
        upload_post_username = config.app.get("upload_post_username", "")
        platforms = config.app.get("upload_post_platforms", ["tiktok", "youtube"])
        is_auto = config.app.get("upload_post_auto_upload", False)
        yt_privacy = config.app.get("upload_post_youtube_privacy_status", "public")
        yt_kids = config.app.get("upload_post_youtube_made_for_kids", False)

        has_yt = "youtube" in platforms
        has_tt = "tiktok" in platforms

        if has_yt or upload_post_username:
            accounts.append({
                "id": str(uuid4())[:8],
                "platform": "youtube",
                "account_name": f"YouTube Channel ({upload_post_username or 'Chính'})",
                "handle": f"@{upload_post_username or 'channel'}",
                "channel_id": "",
                "profile_username": upload_post_username,
                "status": "connected" if upload_post_service.is_configured() else "disconnected",
                "auto_upload": is_auto and has_yt,
                "default_privacy": yt_privacy,
                "made_for_kids": yt_kids,
                "avatar_url": "",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "notes": "Tài khoản YouTube được tạo tự động từ cấu hình hệ thống.",
            })

        if has_tt or upload_post_username:
            accounts.append({
                "id": str(uuid4())[:8],
                "platform": "tiktok",
                "account_name": f"TikTok Profile ({upload_post_username or 'Chính'})",
                "handle": f"@{upload_post_username or 'tiktok'}",
                "channel_id": "",
                "profile_username": upload_post_username,
                "status": "connected" if upload_post_service.is_configured() else "disconnected",
                "auto_upload": is_auto and has_tt,
                "default_privacy": "public",
                "made_for_kids": False,
                "avatar_url": "",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "notes": "Tài khoản TikTok được tạo tự động từ cấu hình hệ thống.",
            })

        return accounts

    def _save_accounts_unlocked(self, accounts: list[dict]) -> bool:
        path = self._get_storage_path(_ACCOUNTS_FILE_NAME)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            self._accounts_cache = accounts
            return True
        except Exception as exc:
            logger.error(f"Failed to write {path}: {exc}")
            return False

    def list_accounts(self, platform: Optional[str] = None) -> list[dict]:
        with self._lock:
            accounts = self._load_accounts_unlocked()
            if platform:
                return [a for a in accounts if a.get("platform") == platform.lower()]
            return accounts

    def get_account(self, account_id: str) -> Optional[dict]:
        with self._lock:
            accounts = self._load_accounts_unlocked()
            for acc in accounts:
                if acc.get("id") == account_id:
                    return acc
            return None

    def save_account(self, account_data: dict) -> dict:
        with self._lock:
            accounts = self._load_accounts_unlocked()
            account_id = account_data.get("id")
            now = _now_iso()

            if account_id:
                # Update existing
                updated = False
                for idx, acc in enumerate(accounts):
                    if acc.get("id") == account_id:
                        merged = {**acc, **account_data, "updated_at": now}
                        accounts[idx] = merged
                        account_data = merged
                        updated = True
                        break
                if not updated:
                    account_data["created_at"] = now
                    account_data["updated_at"] = now
                    accounts.append(account_data)
            else:
                account_data["id"] = str(uuid4())[:8]
                account_data["created_at"] = now
                account_data["updated_at"] = now
                accounts.append(account_data)

            self._save_accounts_unlocked(accounts)
            return account_data

    def delete_account(self, account_id: str) -> bool:
        with self._lock:
            accounts = self._load_accounts_unlocked()
            filtered = [a for a in accounts if a.get("id") != account_id]
            if len(filtered) != len(accounts):
                return self._save_accounts_unlocked(filtered)
            return False

    def toggle_auto_upload(self, account_id: str, enabled: bool) -> bool:
        with self._lock:
            accounts = self._load_accounts_unlocked()
            for acc in accounts:
                if acc.get("id") == account_id:
                    acc["auto_upload"] = bool(enabled)
                    acc["updated_at"] = _now_iso()
                    self._save_accounts_unlocked(accounts)
                    return True
            return False

    def update_account_status(self, account_id: str, status: str) -> bool:
        with self._lock:
            accounts = self._load_accounts_unlocked()
            for acc in accounts:
                if acc.get("id") == account_id:
                    acc["status"] = status
                    acc["updated_at"] = _now_iso()
                    self._save_accounts_unlocked(accounts)
                    return True
            return False

    def get_oauth_start_url(
        self,
        platform: str,
        profile_username: Optional[str] = None,
        redirect_url: Optional[str] = None,
    ) -> dict:
        """
        Request OAuth authorization URL from Upload-Post for connecting YouTube or TikTok.
        Docs: POST https://api.upload-post.com/api/uploadposts/oauth/{platform}/start
        """
        api_key = config.app.get("upload_post_api_key", "").strip()
        if not api_key:
            return {
                "success": False,
                "error": "Chưa cấu hình Upload-Post API Key. Vui lòng nhập API Key trước khi liên kết tài khoản.",
            }

        profile = (
            profile_username
            or config.app.get("upload_post_username", "")
            or "default"
        ).strip()

        platform_clean = platform.lower().strip()
        if platform_clean not in {"youtube", "tiktok", "instagram"}:
            return {
                "success": False,
                "error": f"Nền tảng '{platform}' không được hỗ trợ OAuth trực tiếp.",
            }

        url = f"{UploadPostService.API_BASE}/api/uploadposts/oauth/{platform_clean}/start"
        headers = {
            "Authorization": f"Apikey {api_key}",
            "Content-Type": "application/json",
        }
        payload = {"profile": profile}
        if redirect_url:
            payload["redirect_url"] = redirect_url

        try:
            logger.info(f"Initiating OAuth start for {platform_clean}, profile={profile}")
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if resp.status_code == 401:
                return {
                    "success": False,
                    "error": "API Key không hợp lệ hoặc đã hết hạn (401 Unauthorized).",
                }
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("authorize_url"):
                return {
                    "success": True,
                    "platform": platform_clean,
                    "authorize_url": data.get("authorize_url"),
                    "expires_in": data.get("expires_in", 900),
                    "state": data.get("state"),
                }
            return {
                "success": False,
                "error": data.get("message") or "Không nhận được URL ủy quyền từ máy chủ.",
            }
        except requests.exceptions.RequestException as exc:
            logger.warning(f"OAuth start failed for {platform_clean}: {exc}")
            return {
                "success": False,
                "error": f"Lỗi kết nối tới Upload-Post API: {str(exc)}",
            }

    def verify_api_key(self, api_key: Optional[str] = None) -> dict:
        """
        Verify the Upload-Post API key by calling GET /api/uploadposts/me.
        """
        key = (api_key or config.app.get("upload_post_api_key", "")).strip()
        if not key:
            return {"success": False, "error": "Chưa nhập API Key"}

        try:
            resp = requests.get(
                f"{UploadPostService.API_BASE}/api/uploadposts/me",
                headers={"Authorization": f"Apikey {key}"},
                timeout=15,
            )
            if resp.status_code == 401:
                return {"success": False, "error": "API Key không hợp lệ (401)"}
            resp.raise_for_status()
            data = resp.json()
            return {
                "success": True,
                "email": data.get("email", ""),
                "plan": data.get("plan", ""),
                "message": data.get("message", "API Key hợp lệ!"),
            }
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": f"Không thể kiểm tra API Key: {exc}"}

    def sync_from_upload_post(self) -> dict:
        """
        Sync connected accounts from Upload-Post profiles using GET /api/uploadposts/users.
        """
        api_key = config.app.get("upload_post_api_key", "").strip()
        if not api_key:
            return {"success": False, "error": "Chưa cấu hình API Key"}

        try:
            resp = requests.get(
                f"{UploadPostService.API_BASE}/api/uploadposts/users",
                headers={"Authorization": f"Apikey {api_key}"},
                timeout=20,
            )
            if resp.status_code == 401:
                return {"success": False, "error": "API Key không hợp lệ"}
            resp.raise_for_status()
            profiles = resp.json()

            synced_count = 0
            if isinstance(profiles, list):
                for prof in profiles:
                    prof_name = prof.get("username") or prof.get("profile") or "default"
                    social_accounts = prof.get("social_accounts") or prof.get("accounts") or {}
                    if isinstance(social_accounts, dict):
                        for plat, details in social_accounts.items():
                            plat_lower = plat.lower()
                            if plat_lower in {"youtube", "tiktok"}:
                                handle = ""
                                if isinstance(details, dict):
                                    handle = details.get("username") or details.get("name") or details.get("handle") or ""
                                self.save_account({
                                    "platform": plat_lower,
                                    "account_name": f"{plat.capitalize()} ({handle or prof_name})",
                                    "handle": f"@{handle.lstrip('@')}" if handle else f"@{prof_name}",
                                    "profile_username": prof_name,
                                    "status": "connected",
                                    "auto_upload": False,
                                    "default_privacy": "public",
                                    "notes": f"Đồng bộ từ Upload-Post Profile '{prof_name}'",
                                })
                                synced_count += 1
            return {"success": True, "synced_count": synced_count}
        except requests.exceptions.RequestException as exc:
            logger.warning(f"Failed to sync profiles from Upload-Post: {exc}")
            return {"success": False, "error": str(exc)}

    # ---------------- Publishing Section ----------------

    def publish_video(
        self,
        video_path: str,
        title: str,
        description: str = "",
        hashtags: Optional[list[str] | str] = None,
        platforms: Optional[list[str]] = None,
        youtube_privacy: str = "public",
        youtube_made_for_kids: bool = False,
        tiktok_privacy: str = "PUBLIC_TO_EVERYONE",
        article_url: str = "",
        account_id: Optional[str] = None,
    ) -> dict:
        """
        Publish a video to YouTube, TikTok, or both.
        Includes the news article link in the description automatically.
        """
        if not os.path.exists(video_path):
            return {"success": False, "error": f"Tệp video không tồn tại: {video_path}"}

        if not platforms:
            platforms = ["youtube", "tiktok"]

        # Parse hashtags
        tags_list = []
        if isinstance(hashtags, str):
            tags_list = [t.strip().lstrip("#") for t in hashtags.replace(",", " ").split() if t.strip()]
        elif isinstance(hashtags, list):
            tags_list = [t.strip().lstrip("#") for t in hashtags if t.strip()]

        hashtag_str = " ".join(f"#{t}" for t in tags_list) if tags_list else ""

        # Build clean description with news article link
        clean_desc = (description or "").strip()
        article_link_clean = (article_url or "").strip()
        if article_link_clean and article_link_clean not in clean_desc:
            if clean_desc:
                clean_desc = f"{clean_desc}\n\n📰 Nguồn bài báo: {article_link_clean}"
            else:
                clean_desc = f"Nguồn bài báo: {article_link_clean}"

        if hashtag_str and hashtag_str not in clean_desc:
            clean_desc = f"{clean_desc}\n\n{hashtag_str}".strip()

        # Build YouTube metadata
        has_youtube = any(p.startswith("youtube") for p in platforms)
        youtube_extra = None
        if has_youtube:
            youtube_extra = {
                "youtube_title": title[:100],
                "youtube_description": clean_desc,
                "tags": tags_list,
                "privacyStatus": youtube_privacy,
                "selfDeclaredMadeForKids": youtube_made_for_kids,
                "containsSyntheticMedia": True,
            }

        # Build TikTok caption (TikTok title parameter serves as the caption, max 2200 chars)
        clean_title = title.strip()
        post_title = clean_title
        if "tiktok" in platforms:
            combined_tt = clean_title
            if clean_desc and clean_desc != clean_title:
                combined_tt = f"{clean_title}\n\n{clean_desc}"
            if len(combined_tt) <= 2200:
                post_title = combined_tt
            else:
                post_title = combined_tt[:2190] + "..."

        account_snapshot = None
        if account_id:
            account = self.get_account(account_id)
            if account and account.get("profile_username"):
                account_snapshot = {
                    "upload_post_api_key": config.app.get("upload_post_api_key", ""),
                    "upload_post_username": account.get("profile_username"),
                    "upload_post_enabled": True,
                }

        service = UploadPostService(account_snapshot) if account_snapshot else upload_post_service

        logger.info(f"Publishing video '{video_path}' to platforms: {platforms}")
        result = service.upload_video(
            video_path=video_path,
            title=post_title,
            platforms=platforms,
            privacy_level=tiktok_privacy,
            youtube_extra=youtube_extra,
        )

        # Record into history
        history_entry = {
            "id": str(uuid4())[:8],
            "timestamp": _now_iso(),
            "video_path": video_path,
            "video_filename": os.path.basename(video_path),
            "title": title,
            "platforms": platforms,
            "article_url": article_link_clean,
            "success": result.get("success", False),
            "request_id": result.get("request_id", ""),
            "error": result.get("error") or result.get("message") or "",
            "results": result.get("results"),
        }
        self.add_publish_history_entry(history_entry)

        return result

    # ---------------- History Section ----------------

    def get_publish_history(self, limit: int = 50) -> list[dict]:
        path = self._get_storage_path(_PUBLISH_HISTORY_FILE_NAME)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    # Sort newest first
                    return sorted(data, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]
                return []
        except Exception as exc:
            logger.error(f"Failed to load publish history: {exc}")
            return []

    def add_publish_history_entry(self, entry: dict) -> None:
        path = self._get_storage_path(_PUBLISH_HISTORY_FILE_NAME)
        with self._lock:
            history = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            history = data
                except Exception:
                    history = []
            history.insert(0, entry)
            # Cap at 200 entries
            history = history[:200]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(history, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                logger.error(f"Failed to append publish history: {exc}")

    def clear_publish_history(self) -> bool:
        path = self._get_storage_path(_PUBLISH_HISTORY_FILE_NAME)
        with self._lock:
            try:
                if os.path.exists(path):
                    os.remove(path)
                return True
            except Exception as exc:
                logger.error(f"Failed to remove publish history: {exc}")
                return False


social_account_service = SocialAccountService()
