import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import material
from app.utils import logging_utils


@contextmanager
def _capture_task_scoped_logs():
    """Collect logs according to the same rules as WebUI task logs: only keep records belonging to the current thread."""
    messages = []
    root_thread_id = threading.get_ident()
    handler_id = logger.add(
        lambda message: messages.append(message.record["message"]),
        level="DEBUG",
        filter=lambda record: (
            logging_utils.log_scope_thread_id(record["thread"].id) == root_thread_id
        ),
    )
    try:
        yield messages
    finally:
        logger.remove(handler_id)


class _FakeVideoDownloadResponse:
    def __init__(self, content: bytes):
        self.content_bytes = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content_bytes


class TestMaterialTlsVerification(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_search_pexels_uses_tls_verification_by_default(self):
        """
        The default path must enable TLS verification to avoid material API keys and returned material URLs.
        Intercepted or tampered with by a man-in-the-middle attack on a public network or in an untrusted proxy environment.
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "id": 321,
                        "url": "https://www.pexels.com/video/example-321/?token=drop",
                        "duration": 8,
                        "user": {
                            "id": 654,
                            "name": "Pexels Creator",
                            "url": "https://www.pexels.com/@creator/?key=drop",
                        },
                        "video_files": [
                            {
                                "id": 987,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/video.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertTrue(get.call_args.kwargs["verify"])
        self.assertEqual(results[0].source_info["asset_id"], "321")
        self.assertEqual(
            results[0].source_info["source_page"],
            "https://www.pexels.com/video/example-321/",
        )
        self.assertEqual(
            results[0].source_info["creator"]["profile_page"],
            "https://www.pexels.com/@creator/",
        )
        self.assertEqual(results[0].source_info["rendition"]["id"], "987")

    def test_search_pexels_skips_malformed_entries_without_losing_good_videos(self):
        config.app["pexels_api_keys"] = ["pexels-key"]
        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {"id": 1, "duration": "unknown", "video_files": []},
                    {
                        "id": 10,
                        "duration": 8,
                        "video_files": [
                            {
                                "width": float("inf"),
                                "height": 1920,
                                "link": "https://example.com/overflow.mp4",
                            }
                        ],
                    },
                    {
                        "id": 2,
                        "duration": 8,
                        "video_files": [
                            {"width": None, "height": 1920, "link": "bad"},
                            {
                                "id": 22,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/first.mp4",
                            },
                        ],
                    },
                    {
                        "id": 3,
                        "duration": 8,
                        "video_files": [
                            {
                                "id": 33,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/second.mp4",
                            }
                        ],
                    },
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response):
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/first.mp4", "https://example.com/second.mp4"],
        )

    def test_search_pixabay_skips_malformed_hit_without_losing_good_video(self):
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {"duration": "unknown", "videos": {}},
                    {
                        "id": 10,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": float("inf"),
                                "height": 1920,
                                "url": "https://example.com/overflow.mp4",
                            }
                        },
                    },
                    {
                        "id": 2,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/pixabay-good.mp4",
                            }
                        },
                    },
                ]
            },
        )

        with patch("app.services.material.requests.get", return_value=fake_response):
            results = material.search_videos_pixabay("cat", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/pixabay-good.mp4"],
        )

    def test_search_coverr_skips_malformed_hit_without_losing_good_video(self):
        config.app["coverr_api_keys"] = ["coverr-key"]
        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {"id": "bad", "duration": 8, "urls": ["unexpected"]},
                    {
                        "id": "overflow",
                        "duration": 8,
                        "max_width": float("inf"),
                        "max_height": 1920,
                        "urls": {
                            "mp4_download": "https://example.com/overflow.mp4"
                        },
                    },
                    {
                        "id": "good",
                        "duration": 8,
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {
                            "mp4_download": "https://example.com/coverr-good.mp4"
                        },
                    },
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response):
            results = material.search_videos_coverr("cat", minimum_duration=1)

        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/coverr-good.mp4"],
        )

    def test_search_pixabay_allows_explicit_tls_disable_for_proxy(self):
        """
        A few corporate agents use self-signed certificates. This scenario must be explicitly configured to turn off TLS verification.
        Default shutdown can no longer be hard-coded by code.
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/video.mp4",
                            }
                        },
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pixabay(
                "cat",
                minimum_duration=1,
                video_aspect=material.VideoAspect.landscape,
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(get.call_args.kwargs["verify"])

    def test_remote_searches_only_return_requested_orientation(self):
        """
        All three material sources must only return materials in the target direction to avoid vertical screen tasks being mixed with horizontal screen materials.
        Produce obvious black edges through letterbox. Pexels uses remote parameters and verifies them locally,
        Pixabay and Coverr use responsive sizes for local filtering.
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()

        pexels_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "id": 1,
                        "duration": 8,
                        "video_files": [
                            {
                                "id": 11,
                                "width": 1920,
                                "height": 1080,
                                "link": "https://example.com/landscape.mp4",
                            }
                        ],
                    },
                    {
                        "id": 2,
                        "duration": 8,
                        "video_files": [
                            {
                                "id": 22,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    },
                ]
            }
        )
        pixabay_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {
                        "id": 1,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/landscape.mp4",
                            }
                        },
                    },
                    {
                        "id": 2,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait.mp4",
                            }
                        },
                    },
                ]
            },
        )
        coverr_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "landscape",
                        "duration": 8,
                        "max_width": 1920,
                        "max_height": 1080,
                        "urls": {
                            "mp4_download": "https://example.com/landscape.mp4"
                        },
                    },
                    {
                        "id": "portrait",
                        "duration": 8,
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {
                            "mp4_download": "https://example.com/portrait.mp4"
                        },
                    },
                    {
                        "id": "unknown",
                        "duration": 8,
                        "urls": {"mp4_download": "https://example.com/unknown.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get",
            return_value=pexels_response,
        ) as get:
            pexels_results = material.search_videos_pexels(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.portrait,
            )
            pexels_url = get.call_args.args[0]
        with patch(
            "app.services.material.requests.get",
            return_value=pixabay_response,
        ):
            pixabay_results = material.search_videos_pixabay(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.portrait,
            )
        with patch(
            "app.services.material.requests.get",
            return_value=coverr_response,
        ) as get:
            coverr_results = material.search_videos_coverr(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.portrait,
            )
            coverr_url = get.call_args.args[0]

        self.assertIn("/v1/videos/search?", pexels_url)
        self.assertIn("orientation=portrait", pexels_url)
        self.assertIn("page_size=20", coverr_url)
        self.assertIn("filter=is_vertical%3Atrue", coverr_url)
        for results in (pexels_results, pixabay_results, coverr_results):
            self.assertEqual(
                [item.url for item in results],
                ["https://example.com/portrait.mp4"],
            )

    def test_video_aspect_matching_rejects_unknown_dimensions(self):
        """Materials whose orientation cannot be confirmed cannot be entered into the strict horizontal and vertical screen candidate list."""
        self.assertTrue(
            material._matches_video_aspect(
                1080,
                1920,
                material.VideoAspect.portrait,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                1920,
                1080,
                material.VideoAspect.portrait,
            )
        )
        self.assertTrue(
            material._matches_video_aspect(
                None,
                None,
                material.VideoAspect.portrait,
                is_vertical=True,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                None,
                None,
                material.VideoAspect.portrait,
            )
        )
        self.assertTrue(
            material._matches_video_aspect(
                1080,
                1080,
                material.VideoAspect.square,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                1080,
                1920,
                material.VideoAspect.square,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                float("inf"), 1920, material.VideoAspect.portrait
            )
        )

    def test_coverr_passes_orientation_filter_to_remote_search(self):
        """Coverr horizontal and vertical screen searches should be filtered on the server side, and square materials continue to use local size verification."""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"hits": []})
        cases = (
            (material.VideoAspect.portrait, "filter=is_vertical%3Atrue"),
            (material.VideoAspect.landscape, "filter=is_vertical%3Afalse"),
            (material.VideoAspect.square, None),
        )

        for aspect, expected_filter in cases:
            with self.subTest(aspect=aspect), patch(
                "app.services.material.requests.get",
                return_value=fake_response,
            ) as get:
                material.search_videos_coverr(
                    "city",
                    minimum_duration=1,
                    video_aspect=aspect,
                )
                request_url = get.call_args.args[0]

            self.assertIn("page_size=20", request_url)
            if expected_filter:
                self.assertIn(expected_filter, request_url)
            else:
                self.assertNotIn("filter=", request_url)

    def test_square_search_preserves_crop_compatible_materials(self):
        """
        Pixabay and Coverr rarely offer native square videos. Square output must continue to accept croppable
        Horizontal material, otherwise you will get an empty list directly during the search phase when selecting these two sources.
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        pixabay_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {
                        "id": 1,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/pixabay-landscape.mp4",
                            }
                        },
                    }
                ]
            },
        )
        coverr_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "landscape",
                        "duration": 8,
                        "max_width": 1920,
                        "max_height": 1080,
                        "urls": {
                            "mp4_download": "https://example.com/coverr-landscape.mp4"
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.material.requests.get",
            return_value=pixabay_response,
        ):
            pixabay_results = material.search_videos_pixabay(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.square,
            )
        with patch(
            "app.services.material.requests.get",
            return_value=coverr_response,
        ):
            coverr_results = material.search_videos_coverr(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.square,
            )

        self.assertEqual(
            [item.url for item in pixabay_results],
            ["https://example.com/pixabay-landscape.mp4"],
        )
        self.assertEqual(
            [item.url for item in coverr_results],
            ["https://example.com/coverr-landscape.mp4"],
        )

    def test_search_pixabay_does_not_log_api_key(self):
        config.app["pixabay_api_keys"] = ["pixabay-secret-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {"hits": []},
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.info") as log:
            material.search_videos_pixabay("cat", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertNotIn("pixabay-secret-key", logged_messages)

    def test_search_pixabay_reports_cloudflare_challenge(self):
        """
        The Cloudflare Challenge returns HTML, not the JSON of the Pixabay API.
        The reason for server-side interception should be stated directly to avoid users only seeing JSON parsing errors without context.
        """
        config.app["pixabay_api_keys"] = ["pixabay-secret-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=429,
            headers={
                "content-type": "text/html; charset=UTF-8",
                "cf-mitigated": "challenge",
                "cf-ray": "test-ray",
            },
            text="<html><title>Just a moment...</title></html>",
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("Cloudflare challenge", logged_messages)
        self.assertIn("cf_ray=test-ray", logged_messages)
        self.assertNotIn("pixabay-secret-key", logged_messages)
        self.assertNotIn("Just a moment", logged_messages)

    def test_search_pixabay_reports_api_rate_limit(self):
        """
        Pixabay's own 429 throttling is a different issue than the Cloudflare HTML Challenge.
        Keeping Retry-After helps users determine when to retry without logging the response body.
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=429,
            headers={
                "content-type": "text/plain; charset=UTF-8",
                "retry-after": "60",
            },
            text="API rate limit exceeded",
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("API rate limit exceeded", logged_messages)
        self.assertIn("retry_after=60", logged_messages)

    def test_search_pixabay_reports_non_json_response(self):
        """
        Even if the status code is 200, the upstream proxy may return a login page or other non-JSON content.
        This scenario should log the response type rather than exposing the underlying JSONDecodeError.
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()

        def raise_invalid_json():
            raise ValueError("Expecting value: line 1 column 1")

        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="unexpected response",
            json=raise_invalid_json,
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("unexpected non-JSON response", logged_messages)
        self.assertNotIn("Expecting value", logged_messages)

    def test_search_pixabay_redacts_api_key_from_network_error(self):
        """
        Connection exceptions for requests may echo the full request URL. Exception details should still be retained for troubleshooting,
        However, the Pixabay API Key in the URL query parameters must be desensitized before writing to the log.
        """
        api_key = "pixabay-secret-key"
        config.app["pixabay_api_keys"] = [api_key]
        config.proxy.clear()
        error = requests.ConnectionError(
            "request failed for "
            f"https://pixabay.com/api/videos/?q=nature&key={api_key}"
        )

        with patch(
            "app.services.material.requests.get", side_effect=error
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("ConnectionError", logged_messages)
        self.assertIn("key=***", logged_messages)
        self.assertNotIn(api_key, logged_messages)

    def test_search_pixabay_redacts_proxy_credentials_from_network_error(self):
        """
        Proxy connection exceptions may echo the full proxy URL including authentication information. Logs should retain exception types,
        However, the agent username and password cannot be persisted to the log file.
        """
        proxy_url = "http://proxy-user:proxy-password@proxy.example.com:8080"
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()
        config.proxy["http"] = proxy_url
        error = requests.exceptions.ProxyError(
            f"failed to connect to proxy {proxy_url}"
        )

        with patch(
            "app.services.material.requests.get", side_effect=error
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("ProxyError", logged_messages)
        self.assertNotIn("proxy-user", logged_messages)
        self.assertNotIn("proxy-password", logged_messages)

    def test_save_video_uses_tls_verification_by_default(self):
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = _FakeVideoDownloadResponse(b"fake-video")

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/video.mp4?token=abc", save_dir=temp_dir
                )

            self.assertTrue(os.path.exists(video_path))
            self.assertTrue(get.call_args.kwargs["verify"])

    def test_save_video_streams_chunks_without_materializing_response_content(self):
        class StreamingResponse:
            def __init__(self):
                self.closed = False
                self.chunk_size = None

            @property
            def content(self):
                raise AssertionError("the complete video must not be buffered in memory")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"first"
                yield b"second"

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                pass

            def close(self):
                pass

        response = StreamingResponse()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/large.mp4", save_dir=temp_dir
                )

            self.assertEqual(Path(video_path).read_bytes(), b"firstsecond")
            self.assertTrue(response.closed)
            self.assertLessEqual(response.chunk_size, 1024 * 1024)
            self.assertTrue(get.call_args.kwargs["stream"])

    def _save_video_with_chunks(self, chunks, headers=None):
        """Run save_video with a fake response and return the info log written during the process."""

        class ChunkedResponse(_FakeVideoDownloadResponse):
            def __init__(self):
                super().__init__(b"")
                self.headers = headers or {}

            def iter_content(self, chunk_size):
                yield from chunks

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "app.services.material.requests.get",
                    return_value=ChunkedResponse(),
                ),
                patch("app.services.material.VideoFileClip", FakeVideoFileClip),
                patch.object(material.logger, "info") as info,
            ):
                video_path = material.save_video(
                    "https://example.com/large.mp4?key=secret", save_dir=temp_dir
                )
            self.assertTrue(video_path)
        return [str(call.args[0]) for call in info.call_args_list]

    def test_save_video_reports_progress_during_a_slow_download(self):
        """
        A single 4K clip takes several minutes to download over a slow network. There are no logs during this time. The task looks like
        Stuck. After the heartbeat interval is exceeded, the downloaded size, total size and speed must be recorded; only the
        The cache file name and download address may contain keys and cannot be written to the log.
        """
        megabyte = b"x" * (1024 * 1024)
        with patch.object(material, "_DOWNLOAD_HEARTBEAT_SECONDS", 0):
            messages = self._save_video_with_chunks(
                [megabyte, megabyte, megabyte],
                headers={"Content-Length": str(3 * 1024 * 1024)},
            )

        heartbeats = [m for m in messages if m.startswith("downloading video")]
        self.assertEqual(len(heartbeats), 3)
        self.assertRegex(
            heartbeats[0],
            r"^downloading video vid-[0-9a-f]{32}\.mp4: "
            r"1\.0 of 3\.0 MB \(33%\), \d+\.\d{2} MB/s$",
        )
        self.assertIn("3.0 of 3.0 MB (100%)", heartbeats[2])
        self.assertFalse([m for m in messages if "secret" in m or "example.com" in m])

    def test_save_video_reports_progress_without_a_declared_size(self):
        """Without Content-Length, the downloaded size is still reported, but the percentage is not given."""
        megabyte = b"x" * (1024 * 1024)
        with patch.object(material, "_DOWNLOAD_HEARTBEAT_SECONDS", 0):
            messages = self._save_video_with_chunks([megabyte, megabyte])

        heartbeats = [m for m in messages if m.startswith("downloading video")]
        self.assertEqual(len(heartbeats), 2)
        self.assertRegex(heartbeats[1], r": 2\.0 MB, \d+\.\d{2} MB/s$")

    def test_save_video_stays_quiet_for_fast_downloads(self):
        """Downloads completed within the heartbeat interval should not generate additional logs to avoid flushing the screen with small files."""
        messages = self._save_video_with_chunks([b"small", b"video"])

        self.assertEqual([m for m in messages if m.startswith("downloading video")], [])

    def test_save_video_distinguishes_assets_in_download_query(self):
        """Different paid assets can share a /download path and differ only by query."""
        first_url = "https://cdn.example.com/download?file_id=first"
        second_url = "https://cdn.example.com/download?file_id=second"

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get",
                side_effect=[
                    _FakeVideoDownloadResponse(b"first generated scene"),
                    _FakeVideoDownloadResponse(b"second generated scene"),
                ],
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                first_path = material.save_video(first_url, save_dir=temp_dir)
                second_path = material.save_video(second_url, save_dir=temp_dir)
                cached_path = material.save_video(first_url, save_dir=temp_dir)

            self.assertNotEqual(first_path, second_path)
            self.assertEqual(Path(first_path).read_bytes(), b"first generated scene")
            self.assertEqual(Path(second_path).read_bytes(), b"second generated scene")
            self.assertEqual(cached_path, first_path)
            self.assertEqual(get.call_count, 2)

    def test_save_video_cleans_partial_stream_when_download_fails(self):
        class FailingResponse(_FakeVideoDownloadResponse):
            def __init__(self):
                super().__init__(b"partial")
                self.closed = False

            def __exit__(self, *args):
                self.closed = True

            def iter_content(self, chunk_size):
                yield b"partial"
                raise requests.ConnectionError("connection dropped")

        response = FailingResponse()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.material.requests.get", return_value=response):
                with self.assertRaises(requests.ConnectionError):
                    material.save_video(
                        "https://example.com/interrupted.mp4", save_dir=temp_dir
                    )

            self.assertTrue(response.closed)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_save_video_rejects_declared_oversized_download_before_streaming(self):
        class OversizedResponse(_FakeVideoDownloadResponse):
            headers = {"Content-Length": "9"}

            def iter_content(self, chunk_size):
                raise AssertionError("oversized body should not be read")

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("app.services.material.MAX_VIDEO_DOWNLOAD_BYTES", 8, create=True),
                patch(
                    "app.services.material.requests.get",
                    return_value=OversizedResponse(b""),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "download exceeds"):
                    material.save_video("https://example.com/large.mp4", temp_dir)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_save_video_stops_undeclared_oversized_stream_and_cleans_temp(self):
        class StreamingResponse(_FakeVideoDownloadResponse):
            def iter_content(self, chunk_size):
                yield b"first"
                yield b"second"

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("app.services.material.MAX_VIDEO_DOWNLOAD_BYTES", 8, create=True),
                patch(
                    "app.services.material.requests.get",
                    return_value=StreamingResponse(b""),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "download exceeds"):
                    material.save_video("https://example.com/stream.mp4", temp_dir)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_invalid_download_is_not_reused_as_cached_video(self):
        url = "https://example.com/broken-then-valid.mp4"
        cached_name = f"vid-{material.utils.md5(url)}.mp4"

        class FakeVideoFileClip:
            fps = 24

            def __init__(self, path):
                self.duration = 0 if Path(path).read_bytes() == b"broken" else 1

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get",
                side_effect=[
                    _FakeVideoDownloadResponse(b"broken"),
                    _FakeVideoDownloadResponse(b"valid"),
                ],
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                self.assertEqual(material.save_video(url, save_dir=temp_dir), "")
                self.assertFalse((Path(temp_dir) / cached_name).exists())
                self.assertEqual(
                    material.save_video(url, save_dir=temp_dir),
                    str(Path(temp_dir) / cached_name),
                )

            self.assertEqual(get.call_count, 2)
            self.assertEqual((Path(temp_dir) / cached_name).read_bytes(), b"valid")

    def test_nonfinite_video_metadata_is_not_published(self):
        class FakeVideoFileClip:
            duration = float("nan")
            fps = 24

            def __init__(self, path):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get",
                return_value=_FakeVideoDownloadResponse(b"bad metadata"),
            ), patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                self.assertEqual(
                    material.save_video("https://example.com/nan.mp4", save_dir=temp_dir),
                    "",
                )
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_download_videos_accepts_plain_string_concat_mode(self):
        """
        download_videos may be passed directly to the string pattern by the service layer or test instead of
        VideoConcatMode enumeration. Use empty search terms here to avoid real network requests and only verify
        The string "random" will no longer throw an AttributeError when accessing `.value`.
        """
        result = material.download_videos(
            task_id="string-concat-mode",
            search_terms=[],
            video_concat_mode="random",
        )

        self.assertEqual(result, [])

    def test_material_source_record_uses_public_whitelist(self):
        """
        The task list should only contain traceable public fields and cannot write signature parameters, download addresses,
        Extra fields or native absolute paths passed in by the caller.
        """
        item = material.MaterialInfo(
            provider="pixabay",
            url="https://cdn.example.com/video.mp4?token=secret",
            duration=12,
            source_info={
                "provider": "pixabay",
                "search_term": "city",
                "asset_id": 123,
                "source_page": "https://pixabay.com/videos/city-123/?key=secret",
                "creator": {
                    "id": 456,
                    "name": "Creator",
                    "profile_page": "https://pixabay.com/users/creator/?token=secret",
                    "email": "private@example.com",
                },
                "rendition": {
                    "id": "large",
                    "width": 1920,
                    "height": 1080,
                    "download_url": "https://cdn.example.com/private",
                },
                "api_key": "must-not-persist",
            },
        )

        record = material._material_source_record(
            item,
            "/Users/example/private/task/vid-123.mp4",
        )
        serialized = str(record)

        self.assertEqual(record["local_file"], "vid-123.mp4")
        self.assertEqual(
            record["source_page"],
            "https://pixabay.com/videos/city-123/",
        )
        self.assertEqual(
            record["creator"]["profile_page"],
            "https://pixabay.com/users/creator/",
        )
        self.assertEqual(
            record["rendition"],
            {"id": "large", "width": 1920, "height": 1080},
        )
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/Users/example", serialized)
        self.assertNotIn("private@example.com", serialized)

    def test_download_videos_can_round_robin_terms_in_script_order(self):
        """
        After turning on matching materials in copywriting order, multiple candidates for the first keyword cannot be matched first.
        The audio duration is filled. It is simulated here that each of the two keywords has multiple candidates, and the download order is verified to be
        term1-the 1st, term2-the 1st, term1-the 2nd, close to the narrative order of the script.
        """
        search_results = {
            "opening city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/a1.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "opening city",
                        "asset_id": "a1",
                    },
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/a2.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "opening city",
                        "asset_id": "a2",
                    },
                ),
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/b1.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "middle office",
                        "asset_id": "b1",
                    },
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/b2.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "middle office",
                        "asset_id": "b2",
                    },
                ),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ) as patch_script,
        ):
            result = material.download_videos(
                task_id="ordered-materials",
                search_terms=["opening city", "middle office"],
                source="pexels",
                audio_duration=7,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertCountEqual(
            downloaded_urls,
            [
                "https://v.example/a1.mp4",
                "https://v.example/b1.mp4",
                "https://v.example/a2.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/a1.mp4", "/tmp/b1.mp4", "/tmp/a2.mp4"])
        recorded_sources = patch_script.call_args.kwargs["material_sources"]
        self.assertEqual(
            [source["asset_id"] for source in recorded_sources],
            ["a1", "b1", "a2"],
        )
        self.assertEqual(
            [source["local_file"] for source in recorded_sources],
            ["a1.mp4", "b1.mp4", "a2.mp4"],
        )

    def test_script_order_uses_next_candidate_after_failed_download(self):
        """
        Regression: When the first candidate download fails in script sequence mode, the same keyword must be tried
        next candidate instead of returning an empty result. Download exceptions cannot interrupt the entire process.
        """
        search_results = {
            "city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/bad.mp4",
                    duration=5,
                    source_info={"provider": "pexels", "asset_id": "bad"},
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/good.mp4",
                    duration=5,
                    source_info={"provider": "pexels", "asset_id": "good"},
                ),
            ],
        }
        attempted_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            attempted_urls.append(video_url)
            if "bad" in video_url:
                raise RuntimeError("network down")
            return "/tmp/good.mp4"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "material_concurrency": 4},
            ),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ),
        ):
            result = material.download_videos(
                task_id="failed-first-candidate",
                search_terms=["city"],
                source="pexels",
                audio_duration=8,
                max_clip_duration=5,
                match_script_order=True,
            )

        self.assertEqual(
            attempted_urls,
            ["https://v.example/bad.mp4", "https://v.example/good.mp4"],
        )
        self.assertEqual(result, ["/tmp/good.mp4"])

    def test_script_order_does_not_skip_unattempted_candidates(self):
        """
        Regression: In script sequence mode, candidates that are not selected in this round cannot advance the subscript.
        When there are 1 candidate for each of the three keywords, and only the first two are selected in the first round and both of them fail to download,
        The third candidate must be tried in the next round instead of being skipped by a uniform subscript throughout the round
        Causes an empty result to be returned directly.
        """
        search_results = {
            "t1": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/x.mp4",
                    duration=5,
                    source_info={"provider": "pexels", "asset_id": "x"},
                ),
            ],
            "t2": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/y.mp4",
                    duration=5,
                    source_info={"provider": "pexels", "asset_id": "y"},
                ),
            ],
            "t3": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/z.mp4",
                    duration=5,
                    source_info={"provider": "pexels", "asset_id": "z"},
                ),
            ],
        }
        attempted_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            attempted_urls.append(video_url)
            if "z.mp4" not in video_url:
                raise RuntimeError("network down")
            return "/tmp/z.mp4"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "material_concurrency": 4},
            ),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ),
        ):
            result = material.download_videos(
                task_id="unattempted-candidate",
                search_terms=["t1", "t2", "t3"],
                source="pexels",
                audio_duration=6,
                max_clip_duration=5,
                match_script_order=True,
            )

        # x and y were selected in the first round (a total of 10 seconds and more than 6 seconds of dubbing), z was not tried; after x and y failed to download,
        # Before the repair, z will be directly skipped by the unified subscript for the entire round, and the function will return an empty result;
        # After the fix z must be tried in the second round and downloaded successfully.
        self.assertCountEqual(
            attempted_urls,
            [
                "https://v.example/x.mp4",
                "https://v.example/y.mp4",
                "https://v.example/z.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/z.mp4"])

    def test_default_path_single_material_failure_does_not_abort(self):
        """
        Regression: Under the default path, even if the concurrency configuration is 4, a single candidate batch is downloaded serially
        Download exceptions must also be caught and cannot interrupt the entire generation process.
        """
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/only.mp4",
            duration=5,
            source_info={"provider": "pexels", "asset_id": "only"},
        )

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "material_concurrency": 4},
            ),
            patch.object(material, "search_videos_pexels", return_value=[item]),
            patch.object(
                material, "save_video", side_effect=RuntimeError("boom")
            ),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ),
        ):
            result = material.download_videos(
                task_id="single-failure",
                search_terms=["city"],
                source="pexels",
                video_concat_mode=material.VideoConcatMode.sequential,
                audio_duration=10,
                max_clip_duration=5,
            )

        self.assertEqual(result, [])

    def test_default_path_serial_fallback_keeps_selection_order(self):
        """
        When concurrency is configured as 1, the default path remains strictly serial: download in candidate order,
        Stop when the duration is covered, skip and continue to the next candidate if the download fails.
        """
        items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{name}.mp4",
                duration=6,
                source_info={"provider": "pexels", "asset_id": name},
            )
            for name in ("a", "b", "c")
        ]
        attempted_urls = []

        def fake_save_video(video_url, save_dir=""):
            attempted_urls.append(video_url)
            if "/b.mp4" in video_url:
                raise RuntimeError("network down")
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "material_concurrency": 1},
            ),
            patch.object(material, "search_videos_pexels", return_value=items),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ),
        ):
            result = material.download_videos(
                task_id="serial-fallback",
                search_terms=["city"],
                source="pexels",
                video_concat_mode=material.VideoConcatMode.sequential,
                audio_duration=10,
                max_clip_duration=6,
            )

        # Serial semantics: a is successful (6s), b is skipped if failed, c is successful (a total of 12s covers 10s and then stops).
        self.assertEqual(
            attempted_urls,
            [
                "https://v.example/a.mp4",
                "https://v.example/b.mp4",
                "https://v.example/c.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/a.mp4", "/tmp/c.mp4"])

    def test_default_path_parallel_download_matches_serial_selection(self):
        """
        The default path parallel download maintains the same selection semantics as serial: sequential accumulation, stop when covered;
        Failed candidates are supplemented with subsequent candidates, and the final download set is consistent with the serial.
        """
        items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{name}.mp4",
                duration=6,
                source_info={"provider": "pexels", "asset_id": name},
            )
            for name in ("a", "b", "c", "d")
        ]

        def fake_save_video(video_url, save_dir=""):
            if "/b.mp4" in video_url:
                raise RuntimeError("network down")
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "material_concurrency": 4},
            ),
            patch.object(material, "search_videos_pexels", return_value=items),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ),
        ):
            result = material.download_videos(
                task_id="parallel-selection",
                search_terms=["city"],
                source="pexels",
                video_concat_mode=material.VideoConcatMode.sequential,
                audio_duration=10,
                max_clip_duration=6,
            )

        # First round [a, b]: a succeeds (6s), b fails; second round [c] succeeds (covered and stopped after a total of 12s).
        # Consistent with the download set of serial logic: {a, c}, d will not be downloaded more.
        self.assertEqual(result, ["/tmp/a.mp4", "/tmp/c.mp4"])

    def _download_with_progress(self, *, concurrency, progress_callback, **kwargs):
        """
        Four 6-second candidates, 10-second dubbing: b fails in the first round [a, b], and is made up with c in the second round.
        Returns the download results, which are shared by the three paths of serial, parallel and text order.
        """
        items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{name}.mp4",
                duration=6,
                source_info={"provider": "pexels", "asset_id": name},
            )
            for name in ("a", "b", "c", "d")
        ]

        def fake_save_video(video_url, save_dir=""):
            if "/b.mp4" in video_url:
                raise RuntimeError("network down")
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(
                config.app,
                {"material_directory": "", "material_concurrency": concurrency},
            ),
            patch.object(material, "search_videos_pexels", return_value=items),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ),
        ):
            return material.download_videos(
                task_id="download-progress",
                search_terms=["city"],
                source="pexels",
                video_concat_mode=material.VideoConcatMode.sequential,
                audio_duration=10,
                max_clip_duration=6,
                progress_callback=progress_callback,
                **kwargs,
            )

    def test_download_videos_reports_covered_duration_as_progress(self):
        """
        Previously, the download stage only set the progress to 40% before starting, and jumped to 50% after all the materials were downloaded, which was slow.
        The progress bar will stop at 40% for a long time under the network. Every time a piece of material is downloaded, it will be reported that the dubbing has been covered.
        Proportion of duration; failed candidates are not counted and capped at 1.0 when coverage exceeds need.
        """
        for concurrency in (1, 4):
            with self.subTest(concurrency=concurrency):
                fractions = []
                result = self._download_with_progress(
                    concurrency=concurrency,
                    progress_callback=fractions.append,
                )

                self.assertEqual(result, ["/tmp/a.mp4", "/tmp/c.mp4"])
                self.assertEqual(fractions, [0.6, 1.0])

    def test_script_order_download_reports_progress(self):
        """Paths that match materials in copywriting order also report download progress."""
        fractions = []
        result = self._download_with_progress(
            concurrency=1,
            progress_callback=fractions.append,
            match_script_order=True,
        )

        self.assertTrue(result)
        self.assertEqual(fractions[-1], 1.0)
        self.assertEqual(fractions, sorted(fractions))

    def test_failing_progress_callback_does_not_break_download(self):
        """The progress is just for displaying information. Callback errors (such as the status backend being unavailable) cannot cause the download to fail."""

        def broken_callback(_fraction):
            raise RuntimeError("state backend unavailable")

        with patch.object(material.logger, "warning") as warning:
            result = self._download_with_progress(
                concurrency=1,
                progress_callback=broken_callback,
            )

        self.assertEqual(result, ["/tmp/a.mp4", "/tmp/c.mp4"])
        self.assertTrue(
            [
                call
                for call in warning.call_args_list
                if "progress" in str(call.args[0])
            ]
        )

    def test_each_finished_material_is_logged_with_its_position(self):
        """
        There is no file-by-file log before the end of a download round. After each material is downloaded, it must be recorded that it is the current round.
        At which number, users can judge from the log that the download is progressing and how much is left.
        """
        items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{name}.mp4",
                duration=6,
            )
            for name in ("a", "b", "c")
        ]
        downloaded = []

        def fake_save_video(video_url, save_dir=""):
            if "/b.mp4" in video_url:
                return ""
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_concurrency": 1}),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(material.logger, "info") as info,
        ):
            material._download_materials_in_parallel(
                materials=[("city", item) for item in items],
                material_directory="",
                on_downloaded=lambda item: downloaded.append(item.url),
            )

        messages = [str(call.args[0]) for call in info.call_args_list]
        self.assertEqual(
            [m for m in messages if m.startswith("downloaded material")],
            [
                "downloaded material 1/3: a.mp4",
                "downloaded material 3/3: c.mp4",
            ],
        )
        self.assertEqual(
            downloaded,
            ["https://v.example/a.mp4", "https://v.example/c.mp4"],
        )

    def test_parallel_download_logs_belong_to_the_task_log_scope(self):
        """
        When the concurrency number is greater than 1, the material is downloaded in the material-download thread pool. These threads write
        The log must belong to the task thread that initiated the download, otherwise the WebUI will not be able to see the download after increasing the concurrency.
        any output from the process.
        """
        items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{name}.mp4",
                duration=6,
            )
            for name in ("a", "b")
        ]

        def fake_save_video(video_url, save_dir=""):
            name = video_url.rsplit("/", 1)[-1]
            logger.info(f"worker downloading {name}")
            return f"/tmp/{name}"

        with (
            patch.dict(config.app, {"material_concurrency": 4}),
            patch.object(material, "save_video", side_effect=fake_save_video),
            _capture_task_scoped_logs() as messages,
        ):
            material._download_materials_in_parallel(
                materials=[("city", item) for item in items],
                material_directory="",
            )

        self.assertEqual(
            sorted(m for m in messages if m.startswith("worker downloading")),
            ["worker downloading a.mp4", "worker downloading b.mp4"],
        )

    def test_material_concurrency_is_clamped(self):
        """The material concurrency configuration is clamped at 1~8, and illegal values fall back to the serial default value."""
        with patch.dict(config.app, {}, clear=True):
            self.assertEqual(material._get_material_concurrency(), 1)
        with patch.dict(config.app, {"material_concurrency": 0}):
            self.assertEqual(material._get_material_concurrency(), 1)
        with patch.dict(config.app, {"material_concurrency": 99}):
            self.assertEqual(material._get_material_concurrency(), 8)
        with patch.dict(config.app, {"material_concurrency": "bad"}):
            self.assertEqual(material._get_material_concurrency(), 1)
        with patch.dict(config.app, {"material_concurrency": 2}):
            self.assertEqual(material._get_material_concurrency(), 2)

    def test_material_source_persistence_failure_does_not_break_download(self):
        """When the auxiliary task recording fails, the successfully downloaded materials should still be returned to the main film production process normally."""
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/a1.mp4",
            duration=5,
            source_info={"provider": "pexels", "asset_id": "a1"},
        )

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=[item]),
            patch.object(material, "save_video", return_value="/tmp/a1.mp4"),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                side_effect=OSError("disk unavailable"),
            ),
            patch.object(material.logger, "warning") as warning,
        ):
            result = material.download_videos(
                task_id="persist-failure",
                search_terms=["city"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["/tmp/a1.mp4"])
        self.assertTrue(warning.called)


class TestCoverrProvider(unittest.TestCase):
    """
    Coverr video source (spec: 2026-06-09-coverr-video-provider-design.md).
    Replace all requests with unittest.mock to ensure that CI does not rely on the real network and real API key.
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    # ---------------- Tests for search_videos_coverr ----------------

    def test_search_coverr_uses_mp4_download_url(self):
        """
        search_videos_coverr should convert each hit into MaterialInfo and urls.mp4_download
        Directly as MaterialInfo.url.
        According to Coverr official documentation (api.coverr.co/docs/videos/#download-a-video),
        GET mp4_download itself is included in the download statistics by Coverr, no additional PATCH ping is required.
        Also verify that the Authorization header uses the Bearer scheme.
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "page": 0,
                "pages": 50,
                "page_size": 20,
                "total": 1,
                "hits": [
                    {
                        "id": "S1YbPl1NfI",
                        "duration": 11.625,
                        "aspect_ratio": "16:9",
                        "canonical_url": "https://coverr.co/videos/example?token=drop",
                        "creator": {
                            "id": "creator-1",
                            "name": "Coverr Creator",
                            "profile_url": "https://coverr.co/creators/example?key=drop",
                        },
                        "max_width": 3840,
                        "max_height": 2160,
                        "urls": {
                            "mp4": "https://storage.coverr.co/videos/abc?token=xyz",
                            "mp4_preview": "https://storage.coverr.co/videos/abc/preview?token=xyz",
                            "mp4_download": "https://storage.coverr.co/videos/abc/download?token=xyz",
                        },
                    }
                ],
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            results = material.search_videos_coverr(
                "nature",
                minimum_duration=5,
                video_aspect=material.VideoAspect.landscape,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "coverr")
        self.assertEqual(item.duration, 11)
        # The url field is the mp4_download URL, and coverr://id|url encoding is no longer required.
        self.assertEqual(
            item.url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )
        self.assertEqual(item.source_info["asset_id"], "S1YbPl1NfI")
        self.assertEqual(
            item.source_info["source_page"],
            "https://coverr.co/videos/example",
        )
        self.assertEqual(
            item.source_info["creator"]["profile_page"],
            "https://coverr.co/creators/example",
        )
        # Bearer auth + TLS verify on by default
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer coverr-key"
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_uses_tls_verification_by_default(self):
        """Consistent with pexels/pixabay: TLS verification is enabled by default when not explicitly configured."""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_allows_explicit_tls_disable_for_proxy(self):
        """Enterprise self-signed certificate proxy scenarios must be able to explicitly turn off TLS verification."""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertFalse(get.call_args.kwargs["verify"])

    def test_search_coverr_filters_by_min_duration_and_accepts_string(self):
        """
        Coverr duration field may be number or string in different responses,
        Both formats are accepted; those below minimum_duration should be filtered.
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "shortvid",
                        "duration": 3,  # below minimum
                        "urls": {"mp4_download": "https://example.com/a.mp4"},
                    },
                    {
                        "id": "stringdur",
                        "duration": "10.500000",  # string accepted
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {"mp4_download": "https://example.com/b.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].duration, 10)
        self.assertEqual(results[0].url, "https://example.com/b.mp4")

    def test_search_coverr_skips_invalid_items(self):
        """Entries with missing id or missing urls.mp4_download should be skipped and no exception should be thrown."""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {  # missing urls.mp4_download
                        "id": "no-download",
                        "duration": 10,
                        "urls": {"mp4_preview": "https://example.com/preview.mp4"},
                    },
                    {  # missing id
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/x.mp4"},
                    },
                    {  # valid baseline
                        "id": "good",
                        "duration": 10,
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {"mp4_download": "https://example.com/good.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/good.mp4")

    def test_search_coverr_returns_empty_on_failure(self):
        """
        When responding to structural exceptions/network exceptions, the function must return [] instead of throwing an exception.
        Consistent with pexels/pixabay behavior.
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        # Subtest A: malformed response (no "hits" key)
        with self.subTest("malformed response"):
            fake_response = SimpleNamespace(
                json=lambda: {"error": "rate limited"}
            )
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

        # Subtest B: network exception bubbles up from requests.get
        with self.subTest("network exception"):
            with patch(
                "app.services.material.requests.get",
                side_effect=requests.ConnectionError("boom"),
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

    # ---------------- Tests for download_videos coverr branch ----------------

    def test_download_videos_passes_mp4_download_url_to_save_video(self):
        """
        When source="coverr":
          1. dispatch to search_videos_coverr
          2. The coverr item takes the general download path: save_video and receives the mp4_download URL.
             (No more coverr://id|url encoding, no more PATCH ping calls)
          3. Return to the save path
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.app.pop("material_directory", None)
        config.proxy.clear()

        fake_item = material.MaterialInfo()
        fake_item.provider = "coverr"
        fake_item.url = "https://storage.coverr.co/videos/abc/download?token=xyz"
        fake_item.duration = 10

        with patch(
            "app.services.material.search_videos_coverr",
            return_value=[fake_item],
        ) as search, patch(
            "app.services.material.save_video",
            return_value="/tmp/coverr-saved.mp4",
        ) as save, patch(
            "app.services.material.material_cache.load_material_search_cache",
            return_value=None,
        ), patch(
            "app.services.material.material_cache.save_material_search_cache",
        ):
            result = material.download_videos(
                task_id="t-coverr",
                search_terms=["nature"],
                source="coverr",
                audio_duration=5,
                max_clip_duration=5,
            )

        # 1. dispatch
        self.assertEqual(search.call_count, 1)

        # 2. What save_video receives is the mp4_download URL, which is passed in as it is.
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(
            save_url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )

        # 3. The return value is correct
        self.assertEqual(result, ["/tmp/coverr-saved.mp4"])


class TestWaveSpeedProvider(unittest.TestCase):
    """
    WaveSpeed ​​AI Vincent video material source. Consistent with other material source tests, all use unittest.mock
    Replacing requests and time.sleep, CI does not rely on real networks, real API keys, and real billing.
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["wavespeed_api_keys"] = ["wavespeed-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    @staticmethod
    def _json_response(payload):
        return SimpleNamespace(json=lambda: payload)

    def test_generate_wavespeed_submits_and_polls_to_completion(self):
        """
        The submission request must carry Bearer authentication, model ID path and prompt/aspect_ratio/duration
        Three generation parameters; after polling to completed, outputs are converted into MaterialInfo.
        """
        submit_response = self._json_response(
            {"code": 200, "message": "success", "data": {"id": "pred-123"}}
        )
        poll_responses = [
            self._json_response(
                {"code": 200, "data": {"id": "pred-123", "status": "processing"}}
            ),
            self._json_response(
                {
                    "code": 200,
                    "data": {
                        "id": "pred-123",
                        "status": "completed",
                        "outputs": ["https://cdn.example.com/out.mp4?sig=abc"],
                    },
                }
            ),
        ]

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch(
                "app.services.material.requests.get", side_effect=poll_responses
            ) as get,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_videos_wavespeed(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "wavespeed")
        # The signed URL must be left intact and the query parameters cannot be stripped, otherwise the download will result in a 403
        self.assertEqual(item.url, "https://cdn.example.com/out.mp4?sig=abc")
        self.assertEqual(item.duration, 5)
        self.assertEqual(item.source_info["asset_id"], "pred-123")
        self.assertEqual(item.source_info["search_term"], "sunrise over mountains")
        # The generated product address is a temporary signed URL, and source records are not allowed to be written.
        self.assertNotIn("source_page", item.source_info)

        self.assertIn(
            "/api/v3/bytedance/seedance-2.0-fast/text-to-video",
            post.call_args.args[0],
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer wavespeed-key",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "prompt": "sunrise over mountains",
                "aspect_ratio": "9:16",
                "duration": 5,
            },
        )
        self.assertTrue(post.call_args.kwargs["verify"])
        self.assertIn("/api/v3/predictions/pred-123/result", get.call_args.args[0])
        # In the processing state, you must wait for the polling interval and cannot idle to fill up the remote interface.
        self.assertEqual(sleep.call_count, 1)

    def test_generate_wavespeed_uses_configured_model_id(self):
        """Users can switch any WaveSpeed video model in the configuration."""
        config.app["wavespeed_text_to_video_model"] = "wavespeed-ai/custom-t2v"
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-9"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-9",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/a.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed(
                "city timelapse",
                minimum_duration=3,
                video_aspect=material.VideoAspect.landscape,
            )

        self.assertEqual(len(results), 1)
        self.assertIn("/api/v3/wavespeed-ai/custom-t2v", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["aspect_ratio"], "16:9")

    def test_generate_wavespeed_returns_empty_on_failed_prediction(self):
        """failed/cancelled/timeout are all returned as empty results, allowing the upper layer to skip this keyword and continue."""
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-fail"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-fail",
                    "status": "failed",
                    "error": "content policy",
                },
            }
        )

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(results, [])

    def test_generate_wavespeed_returns_empty_on_rejected_submission(self):
        """Non-200 envelopes (such as the key is invalid) cannot enter polling and return empty results directly."""
        submit_response = self._json_response({"code": 401, "message": "invalid api key"})

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch("app.services.material.requests.get") as get,
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(results, [])
        get.assert_not_called()

    def test_generate_wavespeed_never_retries_submission_on_network_error(self):
        """
        Submitting without receiving a response does not mean that the task has not been created. Resending the POST will result in repeated generation and repeated deductions.
        Therefore, the submission will never be automatically retried, and will be thrown up according to "unknown status" to allow the upper level to stop placing orders.
        """
        with patch(
            "app.services.material.requests.post",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ) as post:
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError):
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(post.call_count, 1)

    def test_generate_wavespeed_treats_server_error_submission_as_unconfirmed(self):
        """5xx may occur after the task is created, the status is unknown, and cannot be continued as "no deduction"."""
        submit_response = SimpleNamespace(
            status_code=502, json=lambda: {"code": 502, "message": "bad gateway"}
        )

        with patch("app.services.material.requests.post", return_value=submit_response):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError):
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

    def test_generate_wavespeed_retries_transient_poll_failures_on_same_task(self):
        """
        When polling encounters 429/5xx or network exception, you must back off and try again with the original prediction id.
        A paid build job must never be resubmitted.
        """
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-r1"}})
        rate_limited = SimpleNamespace(status_code=429, json=lambda: {"code": 429})
        completed = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-r1",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/r1.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch(
                "app.services.material.requests.get",
                side_effect=[
                    rate_limited,
                    requests.exceptions.ConnectionError("boom"),
                    completed,
                ],
            ) as get,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(len(results), 1)
        # Submit only once; three GETs all point to the same prediction id
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 3)
        for call in get.call_args_list:
            self.assertIn("/api/v3/predictions/pred-r1/result", call.args[0])
        # Linear backoff: nth retry wait base * n
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    def test_generate_wavespeed_raises_unconfirmed_after_poll_retries_exhausted(self):
        """
        After the continuous temporary failures exceed the upper limit, the task status remains unknown: the task may still be running remotely.
        It must be thrown up and bring the prediction id, instead of treating it as a failure and allowing the process to continue placing orders.
        """
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-r2"}})

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch(
                "app.services.material.requests.get",
                side_effect=requests.exceptions.ConnectionError("boom"),
            ) as get,
            patch("app.services.material.time.sleep"),
        ):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError) as ctx:
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(ctx.exception.prediction_id, "pred-r2")
        self.assertEqual(get.call_count, material.WAVESPEED_MAX_POLL_RETRIES + 1)

    def test_generate_wavespeed_raises_unconfirmed_on_local_wait_timeout(self):
        """The local wait times out, the remote task is still running, the status is unknown, and new tasks cannot be submitted."""
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-r3"}})
        processing = self._json_response(
            {"code": 200, "data": {"id": "pred-r3", "status": "processing"}}
        )
        clock = iter([0.0, 0.0, material.WAVESPEED_RUN_TIMEOUT_SECONDS + 1])

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch("app.services.material.requests.get", return_value=processing),
            patch(
                "app.services.material.time.monotonic", side_effect=lambda: next(clock)
            ),
            patch("app.services.material.time.sleep"),
        ):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError) as ctx:
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(ctx.exception.prediction_id, "pred-r3")

    def test_download_videos_wavespeed_stops_submitting_after_unconfirmed_task(self):
        """
        Regression: When the task status of a certain fragment is unknown, subsequent keywords can never trigger new paid generation.
        Request - otherwise the first task may still be running/completed, causing duplicate generation and additional charges.
        Materials that have been successfully downloaded cannot have the entire task incorrectly marked as completed.
        """
        first_item = self._generated_item("term-1", "https://cdn.example.com/1.mp4")

        def fake_generate(search_term, minimum_duration, video_aspect):
            if search_term == "term-1":
                return [first_item]
            raise material.WaveSpeedUnconfirmedTaskError(
                "state unknown", prediction_id="pred-stuck"
            )

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                return_value="/tmp/1.mp4",
            ),
        ):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError) as ctx:
                material.download_videos(
                    task_id="test-wavespeed-unconfirmed",
                    search_terms=["term-1", "term-2", "term-3"],
                    source="wavespeed",
                    audio_duration=100,
                    max_clip_duration=5,
                )

        # term-2 stops immediately after throwing an unknown status, term-3 can no longer generate generation requests.
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(ctx.exception.prediction_id, "pred-stuck")

    def test_download_videos_wavespeed_stops_after_paid_download_failure(self):
        """After the download is exhausted and you try again, you cannot quietly pay again for the next keyword."""
        item = self._generated_item("term-1", "https://cdn.example.com/1.mp4")
        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                return_value=[item],
            ) as generate,
            patch("app.services.material.save_video", return_value="") as save,
            patch("app.services.material.time.sleep"),
        ):
            with self.assertRaises(material.WaveSpeedDownloadError) as ctx:
                material.download_videos(
                    task_id="test-wavespeed-paid-download-failure",
                    search_terms=["term-1", "term-2"],
                    source="wavespeed",
                    audio_duration=10,
                    max_clip_duration=5,
                )

        self.assertEqual(ctx.exception.prediction_id, "pred-term-1")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(
            save.call_count, material.WAVESPEED_MAX_DOWNLOAD_RETRIES + 1
        )

    def test_download_videos_wavespeed_retries_original_download_url(self):
        """
        The product has been generated for a fee. Download jitter must give priority to retrying the same signature address instead of retrying.
        Submit a one-time paid build task.
        """
        item = self._generated_item("term-1", "https://cdn.example.com/1.mp4")

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                return_value=[item],
            ) as generate,
            patch(
                "app.services.material.save_video",
                side_effect=[
                    requests.exceptions.ConnectionError("boom"),
                    "/tmp/1.mp4",
                ],
            ) as save,
            patch("app.services.material.time.sleep"),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-download-retry",
                search_terms=["term-1"],
                source="wavespeed",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["/tmp/1.mp4"])
        # Retry to hit the same address, and the second payment generation is not triggered.
        self.assertEqual(save.call_count, 2)
        self.assertEqual(generate.call_count, 1)
        for call in save.call_args_list:
            self.assertEqual(
                call.kwargs.get("video_url") or call.args[0],
                "https://cdn.example.com/1.mp4",
            )

    def test_download_videos_wavespeed_bypasses_search_cache(self):
        """
        The generated source does not participate in the 24-hour search cache: signed URLs will expire, and reusing the cache will make different
        The task repeatedly gets the same generated video. download_videos must call the generated function directly.
        """
        generated_item = material.MaterialInfo()
        generated_item.provider = "wavespeed"
        generated_item.url = "https://cdn.example.com/out.mp4?sig=abc"
        generated_item.duration = 5
        generated_item.source_info = {
            "provider": "wavespeed",
            "search_term": "sunrise",
            "asset_id": "pred-123",
        }

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                return_value=[generated_item],
            ) as generate,
            patch("app.services.material._search_videos_with_cache") as cached_search,
            patch(
                "app.services.material.save_video",
                return_value="/tmp/wavespeed-saved.mp4",
            ) as save,
        ):
            result = material.download_videos(
                task_id="test-wavespeed",
                search_terms=["sunrise"],
                source="wavespeed",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 1)
        cached_search.assert_not_called()
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(save_url, "https://cdn.example.com/out.mp4?sig=abc")
        self.assertEqual(result, ["/tmp/wavespeed-saved.mp4"])

    def test_generate_wavespeed_clamps_duration_to_model_minimum(self):
        """
        The default fragment duration of WebUI is 3 seconds, while the default model only accepts 4-15 seconds; direct transparent transmission will be blocked by the API
        Refuse. The request must converge to the lower limit of the model, and the excess duration will be trimmed by the existing editing process according to the duration of the clip.
        """
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-c1"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-c1",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/c1.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=3)

        self.assertEqual(post.call_args.kwargs["json"]["duration"], 4)
        # MaterialInfo records the actual generation time, and time calculation and editing are based on the actual material length.
        self.assertEqual(results[0].duration, 4)

    def test_generate_wavespeed_clamps_duration_to_model_maximum(self):
        """Requests that exceed the upper limit of the model converge to the upper limit, and remote requests that must fail cannot be submitted."""
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-c2"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-c2",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/c2.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=20)

        self.assertEqual(post.call_args.kwargs["json"]["duration"], 15)
        self.assertEqual(results[0].duration, 15)

    def test_generate_wavespeed_duration_bounds_are_configurable(self):
        """When switching to other models, users can simultaneously adjust the supported time range in the configuration."""
        config.app["wavespeed_min_duration"] = 2
        config.app["wavespeed_max_duration"] = 8
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-c3"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-c3",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/c3.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            material.generate_videos_wavespeed("sunrise", minimum_duration=3)

        self.assertEqual(post.call_args.kwargs["json"]["duration"], 3)

    @staticmethod
    def _generated_item(term, url, duration=5):
        item = material.MaterialInfo()
        item.provider = "wavespeed"
        item.url = url
        item.duration = duration
        item.source_info = {
            "provider": "wavespeed",
            "search_term": term,
            "asset_id": f"pred-{term}",
        }
        return item

    def test_download_videos_wavespeed_generates_on_demand_and_stops(self):
        """
        The generation is billed on a per-item basis, and you cannot generate all keywords first and then select them. Materials must be generated piece by piece on demand,
        After the cumulative effective time (capped by clip length) exceeds the required dubbing time, subsequent keywords will no longer
        Trigger any build request.
        """
        generated = {
            "term-1": [self._generated_item("term-1", "https://cdn.example.com/1.mp4")],
            "term-2": [self._generated_item("term-2", "https://cdn.example.com/2.mp4")],
            "term-3": [self._generated_item("term-3", "https://cdn.example.com/3.mp4")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                side_effect=lambda video_url, save_dir="": f"/tmp/{video_url.rsplit('/', 1)[-1]}",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-lazy",
                search_terms=["term-1", "term-2", "term-3"],
                source="wavespeed",
                audio_duration=8,
                max_clip_duration=5,
            )

        # 5s + 5s > 8s, the third keyword can no longer generate paid generation requests
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call.kwargs["search_term"] for call in generate.call_args_list],
            ["term-1", "term-2"],
        )
        self.assertEqual(result, ["/tmp/1.mp4", "/tmp/2.mp4"])

    def test_download_videos_wavespeed_stops_when_duration_exactly_covered(self):
        """
        Boundary return: When the dubbing is 10 seconds and each segment is 5 seconds, the total time is exactly equal to the required time, which is enough.
        The third keyword can no longer trigger paid generation requests (the stop judgment must be >= instead of >).
        """
        generated = {
            "term-1": [self._generated_item("term-1", "https://cdn.example.com/1.mp4")],
            "term-2": [self._generated_item("term-2", "https://cdn.example.com/2.mp4")],
            "term-3": [self._generated_item("term-3", "https://cdn.example.com/3.mp4")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                side_effect=lambda video_url, save_dir="": f"/tmp/{video_url.rsplit('/', 1)[-1]}",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-exact",
                search_terms=["term-1", "term-2", "term-3"],
                source="wavespeed",
                audio_duration=10,
                max_clip_duration=5,
            )

        # 5s + 5s == 10s, exactly covered, the 3rd paragraph must not be generated
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/1.mp4", "/tmp/2.mp4"])

    def test_download_videos_wavespeed_rejects_nonfinite_duration_before_submission(self):
        """NaN/Infinity must not buy a video for every script keyword."""
        for duration in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(duration=duration):
                with patch("app.services.material.generate_videos_wavespeed") as generate:
                    with self.assertRaisesRegex(ValueError, "finite"):
                        material.download_videos(
                            task_id="test-wavespeed-invalid-duration",
                            search_terms=["term-1", "term-2"],
                            source="wavespeed",
                            audio_duration=duration,
                            max_clip_duration=5,
                        )
                    generate.assert_not_called()

    def test_download_videos_wavespeed_rejects_nonpositive_clip_duration(self):
        """A zero clip duration cannot advance paid coverage."""
        with patch("app.services.material.generate_videos_wavespeed") as generate:
            with self.assertRaisesRegex(ValueError, "clip duration"):
                material.download_videos(
                    task_id="test-wavespeed-invalid-clip",
                    search_terms=["term-1", "term-2"],
                    source="wavespeed",
                    audio_duration=10,
                    max_clip_duration=0,
                )
            generate.assert_not_called()

    def test_download_videos_wavespeed_skips_nonpositive_audio_duration(self):
        """An empty narration must not start a paid generation request."""
        with (
            patch("app.services.material.generate_videos_wavespeed") as generate,
            patch("app.services.material._persist_material_sources"),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-empty-audio",
                search_terms=["term-1", "term-2"],
                source="wavespeed",
                audio_duration=0,
                max_clip_duration=5,
            )
        self.assertEqual(result, [])
        generate.assert_not_called()

    def test_download_videos_wavespeed_skips_failed_segment_and_continues(self):
        """When the generation of a single fragment fails (empty result), skip this keyword and continue to generate subsequent fragments."""
        generated = {
            "term-1": [],
            "term-2": [self._generated_item("term-2", "https://cdn.example.com/2.mp4")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                return_value="/tmp/wavespeed-2.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-skip",
                search_terms=["term-1", "term-2"],
                source="wavespeed",
                audio_duration=4,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/wavespeed-2.mp4"])


if __name__ == "__main__":
    unittest.main()
