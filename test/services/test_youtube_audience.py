"""Validate configuration of child audience claims, task snapshots, and real HTTP form encoding without connecting to external platforms."""

from concurrent.futures import Future
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.config import config
from app.models.schema import VideoParams
from app.services import task
from app.services.state import MemoryState
from app.services.upload_post import UploadPostService


@pytest.fixture
def upload_config():
    """The test only uses fictitious credentials and cannot read the user's real publishing target or initiate real uploads."""
    values = {
        "upload_post_enabled": True,
        "upload_post_api_key": "local-test-key",
        "upload_post_username": "local-test-user",
        "upload_post_platforms": ["youtube"],
    }
    with patch.object(config, "app", values):
        yield values


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "extra",
    [None, {}, {"selfDeclaredMadeForKids": False}, {"selfDeclaredMadeForKids": True}],
)
def test_audience_payload_and_snapshot_override(
    tmp_path, upload_config, configured, extra
):
    """Explicit task snapshots take precedence over the current configuration; claims must also be sent without metadata."""
    upload_config["upload_post_youtube_made_for_kids"] = configured
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test-media")
    with patch("app.services.upload_post.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "success": True,
            "results": {"youtube": {"success": True}},
        }
        result = UploadPostService().upload_video(
            str(video), "test", youtube_extra=extra
        )
    assert result["success"] is True
    data = post.call_args.kwargs["data"]
    expected = (extra or {}).get("selfDeclaredMadeForKids", configured)
    assert [v for k, v in data if k == "selfDeclaredMadeForKids"] == [
        str(expected).lower()
    ]


@pytest.mark.parametrize("invalid", ["false", "true", "", 0, 1, None, [], {}])
def test_invalid_audience_never_uploads(tmp_path, upload_config, invalid):
    """Illegal configurations cannot be implicitly converted into audience declarations and cannot be uploaded further."""
    upload_config["upload_post_youtube_made_for_kids"] = invalid
    video = tmp_path / "video.mp4"
    video.touch()
    with patch("app.services.upload_post.requests.post") as post:
        result = UploadPostService().upload_video(str(video), "test")
    assert result["success"] is False
    assert "boolean" in result["error"]
    post.assert_not_called()


def test_other_platforms_ignore_youtube_audience(tmp_path, upload_config):
    """Non-YouTube publishing does not read or verify this setting, and incorrect audience configuration cannot affect them."""
    upload_config["upload_post_youtube_made_for_kids"] = "invalid"
    video = tmp_path / "video.mp4"
    video.touch()
    with patch("app.services.upload_post.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "success": True,
            "results": {
                "tiktok": {"success": True},
                "instagram": {"success": True},
            },
        }
        result = UploadPostService().upload_video(
            str(video),
            "test",
            platforms=["tiktok", "instagram"],
            youtube_extra={"selfDeclaredMadeForKids": "invalid"},
        )
    assert result["success"] is True
    assert "selfDeclaredMadeForKids" not in dict(post.call_args.kwargs["data"])


@pytest.mark.parametrize("selected", [False, True])
def test_queued_audience_survives_config_change(upload_config, selected):
    """Delay the execution of the real work function to verify that configuration changes during queue waiting will not affect submitted tasks."""
    upload_config["upload_post_youtube_made_for_kids"] = selected
    state = MemoryState()
    state.update_task("audience-snapshot", state=task.const.TASK_STATE_COMPLETE)
    future = Future()
    with (
        patch.object(task.sm, "state", state),
        patch.object(task, "_cross_post_slots", MagicMock()),
        patch.object(
            task._cross_post_executor, "submit", return_value=future
        ) as submit,
        patch.object(task.llm, "generate_social_metadata", return_value={}),
        patch.object(
            task.upload_post, "cross_post_video", return_value={"success": True}
        ) as upload,
    ):
        assert (
            task._schedule_cross_post(
                "audience-snapshot",
                ["one.mp4", "two.mp4"],
                VideoParams(video_subject="test"),
                "test",
                ["youtube"],
                "private",
                UploadPostService().youtube_made_for_kids,
            )
            is None
        )
        upload_config["upload_post_youtube_made_for_kids"] = not selected
        fn, *args = submit.call_args.args
        fn(*args)
        future.set_result(None)
    assert upload.call_count == 2
    assert all(
        c.kwargs["youtube_extra"]["selfDeclaredMadeForKids"] is selected
        for c in upload.call_args_list
    )


@pytest.mark.parametrize("selected", [None, False, True])
def test_real_http_multipart_audience(tmp_path, upload_config, selected):
    """Receives multipart requests over native real HTTP, validating old configurations and true/false encoding."""
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
            )
            received.append(
                {
                    part.get_param(
                        "name", header="content-disposition"
                    ): part.get_payload(decode=True)
                    for part in message.iter_parts()
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success": true, "results": {"youtube": {"success": true}}}')

        def log_message(self, *_args):
            # The native receiver does not print request headers, and the test output does not need to contain authentication information.
            pass

    if selected is not None:
        upload_config["upload_post_youtube_made_for_kids"] = selected
    video = tmp_path / "video.mp4"
    video.write_bytes(b"multipart-test-media")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        # Only access the loopback address, bypassing the development machine proxy and avoiding handing test requests to external proxy services.
        with (
            requests.Session() as session,
            patch.object(
                UploadPostService, "API_BASE", f"http://127.0.0.1:{server.server_port}"
            ),
        ):
            session.trust_env = False
            with patch(
                "app.services.upload_post.requests.post", side_effect=session.post
            ):
                result = UploadPostService().upload_video(str(video), "test")
        assert result["success"] is True
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert received[0]["selfDeclaredMadeForKids"] == (b"true" if selected else b"false")
    assert received[0]["video"] == b"multipart-test-media"
