import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from app.services.social_account import SocialAccountService


@pytest.fixture
def temp_service():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = SocialAccountService(storage_dir=tmpdir)
        yield svc


def test_add_and_list_accounts(temp_service):
    acc1 = {
        "id": "yt_chan_1",
        "platform": "youtube",
        "account_name": "MyNewsChannel",
        "handle": "@vietnam_news_yt",
        "status": "connected",
    }
    temp_service.save_account(acc1)

    accounts = temp_service.list_accounts()
    assert any(a["id"] == "yt_chan_1" for a in accounts)

    # Filter by platform
    yt_accs = temp_service.list_accounts("youtube")
    assert all(a["platform"] == "youtube" for a in yt_accs)
    assert any(a["id"] == "yt_chan_1" for a in yt_accs)


def test_update_existing_account(temp_service):
    acc = {
        "id": "tt_1",
        "platform": "tiktok",
        "account_name": "Old TikTok Name",
        "handle": "@vietnam_daily",
        "status": "connected",
    }
    temp_service.save_account(acc)

    # Update with new display name
    acc_updated = {
        "id": "tt_1",
        "platform": "tiktok",
        "account_name": "Vietnam Daily News",
        "handle": "@vietnam_daily",
        "status": "connected",
    }
    temp_service.save_account(acc_updated)

    updated = temp_service.get_account("tt_1")
    assert updated is not None
    assert updated["account_name"] == "Vietnam Daily News"


def test_delete_account(temp_service):
    acc = {
        "id": "acc_to_remove",
        "platform": "youtube",
        "account_name": "ToRemove",
        "handle": "@remove_me",
    }
    temp_service.save_account(acc)
    assert temp_service.get_account("acc_to_remove") is not None

    removed = temp_service.delete_account("acc_to_remove")
    assert removed is True
    assert temp_service.get_account("acc_to_remove") is None

    # Removing non-existent account returns False
    assert temp_service.delete_account("acc_to_remove") is False


def test_publish_history(temp_service):
    temp_service.add_publish_history_entry({
        "video_path": "output/test.mp4",
        "title": "Bản tin 24h",
        "platforms": ["youtube", "tiktok"],
        "success": True,
        "article_url": "https://vnexpress.net/test-post",
    })

    history = temp_service.get_publish_history()
    assert len(history) == 1
    assert history[0]["title"] == "Bản tin 24h"
    assert history[0]["article_url"] == "https://vnexpress.net/test-post"
    assert history[0]["success"] is True

    temp_service.clear_publish_history()
    assert len(temp_service.get_publish_history()) == 0


def test_publish_video_injects_article_url(temp_service, tmp_path):
    video_file = tmp_path / "sample.mp4"
    video_file.write_bytes(b"dummy video data")

    with patch("app.services.social_account.upload_post_service") as mock_up:
        mock_up.upload_video.return_value = {
            "success": True,
            "request_id": "req-12345",
            "results": {"youtube": {"success": True}, "tiktok": {"success": True}},
        }

        res = temp_service.publish_video(
            video_path=str(video_file),
            title="Tin Nóng Hôm Nay",
            description="Mô tả tóm tắt sự kiện",
            hashtags="#tintuc #vietnam",
            platforms=["youtube", "tiktok"],
            article_url="https://dantri.com.vn/xa-hoi/tin-moi.htm",
        )

        assert res["success"] is True
        mock_up.upload_video.assert_called_once()
        call_kwargs = mock_up.upload_video.call_args[1]

        # Verify article_url is formatted into YouTube extra description
        yt_extra = call_kwargs.get("youtube_extra", {})
        assert "📰 Nguồn bài báo: https://dantri.com.vn/xa-hoi/tin-moi.htm" in yt_extra.get("youtube_description", "")

        # Verify article_url is formatted into TikTok title/caption
        assert "https://dantri.com.vn/xa-hoi/tin-moi.htm" in call_kwargs.get("title", "")
