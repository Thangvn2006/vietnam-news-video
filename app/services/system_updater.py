"""System diagnostic health check and Git auto-update service for VietNamNewsVideo."""

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Optional
from loguru import logger

from app.config import config
from app.utils import utils

GITHUB_REPO_URL = "https://github.com/Thangvn2006/vietnam-news-video"
GITHUB_API_URL = "https://api.github.com/repos/Thangvn2006/vietnam-news-video"


def get_system_health() -> dict[str, Any]:
    """Run comprehensive system diagnostic checks."""
    # 1. Python Check
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)

    # 2. FFmpeg Check
    ffmpeg_path = shutil.which("ffmpeg")
    ffmpeg_ver = ""
    ffmpeg_ok = False
    if ffmpeg_path:
        try:
            res = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                ffmpeg_ok = True
                first_line = res.stdout.splitlines()[0] if res.stdout else "FFmpeg OK"
                ffmpeg_ver = first_line[:50]
        except Exception as e:
            logger.debug(f"FFmpeg check error: {e}")

    # 3. Git Check
    git_path = shutil.which("git")
    git_ver = ""
    git_ok = False
    if git_path:
        try:
            res = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                git_ok = True
                git_ver = res.stdout.strip()
        except Exception as e:
            logger.debug(f"Git check error: {e}")

    # 4. Storage Folders & Permissions
    storage_dir = utils.storage_dir()
    final_videos_dir = utils.output_videos_dir()
    tasks_dir = utils.task_dir()

    storage_ok = True
    storage_notes = []
    for d_path, name in [
        (storage_dir, "storage"),
        (final_videos_dir, "storage/final_videos"),
        (tasks_dir, "storage/tasks"),
    ]:
        try:
            os.makedirs(d_path, exist_ok=True)
            test_file = os.path.join(d_path, ".write_test")
            with open(test_file, "w") as tf:
                tf.write("ok")
            os.remove(test_file)
        except Exception as e:
            storage_ok = False
            storage_notes.append(f"{name}: Lỗi ghi ({e})")

    # 5. AI API Key Configuration Status
    llm_provider = getattr(config.app, "llm_provider", "openai")
    has_llm_key = bool(
        getattr(config.app, f"{llm_provider}_api_key", None)
        or (llm_provider == "gemini" and getattr(config.app, "gemini_api_key", None))
        or (llm_provider == "openai" and getattr(config.app, "openai_api_key", None))
    )
    has_pexels = bool(getattr(config.app, "pexels_api_key", None))

    return {
        "python": {
            "ok": py_ok,
            "version": py_ver,
            "executable": sys.executable,
        },
        "ffmpeg": {
            "ok": ffmpeg_ok,
            "path": ffmpeg_path or "Không tìm thấy trong PATH",
            "version": ffmpeg_ver,
        },
        "git": {
            "ok": git_ok,
            "path": git_path or "Không tìm thấy trong PATH",
            "version": git_ver,
        },
        "storage": {
            "ok": storage_ok,
            "notes": storage_notes,
            "final_videos_dir": final_videos_dir,
            "tasks_dir": tasks_dir,
        },
        "api_keys": {
            "llm_provider": llm_provider,
            "llm_configured": has_llm_key,
            "pexels_configured": has_pexels,
        },
    }


def check_git_updates(cwd: Optional[str] = None) -> dict[str, Any]:
    """Check repository Git status against remote origin/main."""
    if not shutil.which("git"):
        return {
            "ok": False,
            "error": "Git chưa được cài đặt trên hệ thống.",
            "has_update": False,
        }

    work_dir = cwd or utils.root_dir()

    try:
        res_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        current_branch = res_branch.stdout.strip() or "main"

        res_commit = subprocess.run(
            ["git", "log", "-1", "--format=%h - %s (%cr)"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        current_commit = res_commit.stdout.strip()

        res_fetch = subprocess.run(
            ["git", "fetch", "origin", current_branch],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res_fetch.returncode != 0:
            return {
                "ok": False,
                "error": f"Không thể kết nối đến GitHub ({res_fetch.stderr.strip()[:100]})",
                "has_update": False,
                "current_branch": current_branch,
                "current_commit": current_commit,
            }

        res_behind = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..origin/{current_branch}"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commits_behind = int(res_behind.stdout.strip() or "0")

        res_ahead = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{current_branch}..HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commits_ahead = int(res_ahead.stdout.strip() or "0")

        new_commits = []
        if commits_behind > 0:
            res_log = subprocess.run(
                ["git", "log", f"HEAD..origin/{current_branch}", "--oneline", "-n", "10"],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            new_commits = [line.strip() for line in res_log.stdout.splitlines() if line.strip()]

        return {
            "ok": True,
            "has_update": commits_behind > 0,
            "commits_behind": commits_behind,
            "commits_ahead": commits_ahead,
            "current_branch": current_branch,
            "current_commit": current_commit,
            "new_commits": new_commits,
            "repo_url": GITHUB_REPO_URL,
        }
    except Exception as exc:
        logger.warning(f"Error checking git updates: {exc}")
        return {
            "ok": False,
            "error": str(exc),
            "has_update": False,
        }


def perform_git_update(cwd: Optional[str] = None) -> tuple[bool, str]:
    """Execute git pull origin main to update codebase."""
    if not shutil.which("git"):
        return False, "Git chưa được cài đặt trên hệ thống."

    work_dir = cwd or utils.root_dir()

    try:
        res_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = res_branch.stdout.strip() or "main"

        res_stash = subprocess.run(
            ["git", "stash"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )

        res_pull = subprocess.run(
            ["git", "pull", "origin", branch],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output_msg = (res_pull.stdout or "") + "\n" + (res_pull.stderr or "")
        success = res_pull.returncode == 0

        if "No local changes to save" not in (res_stash.stdout or ""):
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )

        return success, output_msg.strip()
    except Exception as exc:
        return False, f"Lỗi khi thực hiện cập nhật: {exc}"
