# -*- coding: utf-8 -*-
import base64
import io
import os
import shutil
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from app.config import config
from app.services import material


def _png_bytes(width=64, height=96, color=(120, 40, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _image_response(payload, status_code=200):
    return SimpleNamespace(json=lambda: payload, status_code=status_code)


def _download_response(content, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        content=content,
        headers={"Content-Length": str(len(content))},
        iter_content=lambda chunk_size: iter((content,)),
        close=lambda: None,
    )


class TestOpenAIImageProvider(unittest.TestCase):
    """
    OpenAI compatible Vincent picture material source. Consistent with other material source tests, all use unittest.mock
    Replacing requests and time.sleep, CI does not rely on real networks, real API keys, and real billing.
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        # The assertion needs to be made after the generate call returns, and the temporary directory cannot be destroyed in advance with the with block.
        # Therefore use mkdtemp + addCleanup to manage the life cycle.
        self.save_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)
        config.app["openai_image_base_url"] = "https://img.example.com/v1"
        config.app["openai_image_api_keys"] = ["sk-test-key"]
        config.app["openai_image_model"] = "test-image-model"
        # Prompt word templates and custom sizes are turned off by default, and the use cases that need to be covered are configured by themselves to avoid developers
        # Settings in the local config.toml affect assertions for default behavior scenarios.
        config.app.pop("openai_image_prompt_template", None)
        config.app.pop("openai_image_size", None)
        config.app.pop("tls_verify", None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    @staticmethod
    def _generated_item(term, image_path, duration=5):
        item = material.MaterialInfo()
        item.provider = "openai_image"
        item.url = image_path
        item.duration = duration
        item.source_info = {
            "provider": "openai_image",
            "search_term": term,
            "rendition": {"id": None, "width": 736, "height": 1312},
        }
        return item

    # ------------------------------------------------------------------
    # path to success
    # ------------------------------------------------------------------

    def test_generate_images_openai_with_b64_json_response(self):
        """
        The b64_json response must be decoded and written into a legal PNG, and written into the rendition according to the real image size.
        (Compatible with the situation where the size returned by the transfer service is inconsistent with the request), duration records the duration of the target fragment.
        """
        image_data = _png_bytes(width=736, height=1312)
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            results = material.generate_images_openai(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
                save_dir=self.save_dir,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "openai_image")
        self.assertEqual(item.duration, 5)
        # Request size according to the frame, take the official OpenAI compatible size, do not directly use the video resolution
        self.assertEqual(
            post.call_args.args[0],
            "https://img.example.com/v1/images/generations",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "test-image-model",
                "prompt": "sunrise over mountains",
                "n": 1,
                "size": "1024x1536",
            },
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-test-key",
        )
        # The dropped file is a decodable PNG
        self.assertTrue(item.url.endswith(".png"))
        self.assertTrue(os.path.isfile(item.url))
        with Image.open(item.url) as saved:
            self.assertEqual(saved.size, (736, 1312))
        # rendition records the true size of the image and does not rely on request parameters.
        self.assertEqual(
            item.source_info["rendition"],
            {"id": None, "width": 736, "height": 1312},
        )
        self.assertEqual(item.source_info["search_term"], "sunrise over mountains")

    def test_generate_images_openai_with_url_response(self):
        """The url response must immediately download the temporary address and place it on disk."""
        response = _image_response(
            {"data": [{"url": "https://cdn.example.com/generated/abc.png?sig=1"}]}
        )
        download = _download_response(_png_bytes(width=200, height=300))

        with (
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.requests.get", return_value=download) as get,
        ):
            results = material.generate_images_openai(
                "city at night", minimum_duration=3, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertTrue(os.path.isfile(results[0].url))
        # The temporary URL is downloaded as is, and the signature query parameters cannot be stripped.
        self.assertEqual(
            get.call_args.args[0],
            "https://cdn.example.com/generated/abc.png?sig=1",
        )

    def test_generate_images_openai_skips_b64_json_with_invalid_image(self):
        """
        The compatibility layer returns 200 but the body is not a decodable image (such as an HTML error page disguised as JSON)
        When , an empty list must be returned according to the material source agreement to let the upper layer skip the keyword instead of allowing decoding.
        Exception interrupts the entire task.
        """
        fake_content = b"<html><body>gateway degraded</body></html>"
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(fake_content).decode("ascii")}]}
        )

        with patch("app.services.material.requests.post", return_value=response):
            results = material.generate_images_openai(
                "sunrise over mountains", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generate_images_openai_skips_url_download_with_invalid_content(self):
        """The temporary URL also takes the skip path when downloading non-image content of 200."""
        response = _image_response(
            {"data": [{"url": "https://cdn.example.com/generated/abc.png?sig=1"}]}
        )
        download = _download_response(b"\x89PNG\r\n\x1a\nnot-really-a-png")

        with (
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.requests.get", return_value=download),
        ):
            results = material.generate_images_openai(
                "city at night", minimum_duration=3, save_dir=self.save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generate_images_openai_propagates_image_write_failure(self):
        """
        The task must be interrupted when the image has been successfully decoded but the PNG writing fails. This type of failure usually persists
        It affects subsequent keywords. If it is mistakenly determined that the content of a single page is abnormal and continues, a payment request that cannot be placed will be generated.
        """
        response = _image_response(
            {
                "data": [
                    {"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}
                ]
            }
        )

        with (
            patch("app.services.material.requests.post", return_value=response),
            patch.object(
                Image.Image,
                "save",
                side_effect=OSError("no space left on device"),
            ),
            self.assertRaisesRegex(OSError, "no space left on device"),
        ):
            material.generate_images_openai(
                "city at night", minimum_duration=3, save_dir=self.save_dir
            )

        self.assertEqual(os.listdir(self.save_dir), [])

    # ------------------------------------------------------------------
    # Backoff retries and key rotation
    # ------------------------------------------------------------------

    def test_generate_images_openai_retries_429_with_backoff(self):
        """429 is a temporary current limit. You must back off and try again instead of killing the task."""
        image_data = _png_bytes()
        responses = [
            _image_response({"error": {"message": "rate limited"}}, status_code=429),
            _image_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "ocean waves", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)
        # You must wait for linear backoff before retrying for the first time, and the remote interface cannot be filled immediately.
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(
            sleep.call_args.args[0],
            material.OPENAI_IMAGE_RETRY_BACKOFF_SECONDS[0],
        )

    def test_generate_images_openai_rotates_key_on_401(self):
        """
        401 means the current key is rejected. When multiple keys are configured, retry must use get_api_key
        The rotation mechanism switches to the next key instead of repeatedly using the same rejected key.
        """
        config.app["openai_image_api_keys"] = ["sk-bad-key", "sk-good-key"]
        image_data = _png_bytes()

        responses = [
            _image_response({"error": {"message": "unauthorized"}}, status_code=401),
            _image_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_openai(
                "forest fog", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)
        used_keys = [
            call.kwargs["headers"]["Authorization"] for call in post.call_args_list
        ]
        # Two consecutive requests must use different keys, and both come from the configuration list
        self.assertNotEqual(used_keys[0], used_keys[1])
        for auth in used_keys:
            self.assertIn(auth.replace("Bearer ", ""), ["sk-bad-key", "sk-good-key"])

    def test_generate_images_openai_fails_fast_on_401_with_single_key(self):
        """When there is only one key, 401 retry is meaningless, and it must fail quickly and return an empty result."""
        response = _image_response(
            {"error": {"message": "unauthorized"}}, status_code=401
        )

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "desert dunes", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_generate_images_openai_returns_empty_after_retries_exhausted(self):
        """
        After all retries are exhausted, an empty list will be returned according to the material source agreement, and the keyword will be passed to the upper layer to skip; and the order will not be dropped.
        Any residual files.
        """
        response = _image_response(
            {"error": {"message": "rate limited"}}, status_code=429
        )

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "storm clouds", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(post.call_count, material.OPENAI_IMAGE_MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, material.OPENAI_IMAGE_MAX_ATTEMPTS - 1)
        # No residual files are generated
        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generate_images_openai_redacts_api_key_in_failure_detail(self):
        """Failure details cannot be written in clear text of the API key to the log."""
        config.app["openai_image_api_keys"] = ["sk-secret-123"]
        response = _image_response(
            {"error": {"message": "invalid key sk-secret-123 provided"}},
            status_code=401,
        )

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.logger") as logger,
        ):
            results = material.generate_images_openai(
                "redacted term", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(results, [])
        logged = [str(call) for call in logger.error.call_args_list]
        self.assertTrue(logged)
        for message in logged:
            self.assertNotIn("sk-secret-123", message)

    def test_generate_images_openai_retries_generated_image_download(self):
        """
        The picture has been charged on a per-picture basis. The same URL must be retried to download the jitter and cannot be rolled back to regeneration.
        The same picture results in double billing.
        """
        response = _image_response(
            {"data": [{"url": "https://cdn.example.com/generated/x.png"}]}
        )
        downloads = [
            _download_response(b"", status_code=502),
            _download_response(_png_bytes()),
        ]

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", side_effect=downloads) as get,
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_openai(
                "aurora", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(len(results), 1)
        # The download was retried at the same address and did not trigger the second payment generation.
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertEqual(call.args[0], "https://cdn.example.com/generated/x.png")

    def test_generated_image_download_rejects_oversize_body(self):
        """A generated image URL must not buffer an unbounded provider body."""
        response = _download_response(b"too-large")
        response.headers = {}
        closed = []
        response.close = lambda: closed.append(True)
        with (
            patch("app.services.material.requests.get", return_value=response) as get,
            patch("app.services.material.OPENAI_IMAGE_MAX_BYTES", 5, create=True),
            patch("app.services.material.time.sleep"),
        ):
            image_bytes, error = material._openai_image_download_bytes(
                "https://cdn.example.com/generated/x.png", "test-key"
            )

        self.assertIsNone(image_bytes)
        self.assertIn("limit", error)
        self.assertTrue(all(call.kwargs.get("stream") is True for call in get.call_args_list))
        self.assertEqual(closed, [True])

    def test_generated_image_download_rejects_declared_oversize_body(self):
        """An advertised oversize image should be rejected before reading bytes."""
        response = _download_response(b"small")
        response.headers = {"Content-Length": "100"}
        response.iter_content = lambda chunk_size: self.fail("body should not be read")
        with (
            patch("app.services.material.requests.get", return_value=response),
            patch("app.services.material.OPENAI_IMAGE_MAX_BYTES", 5),
        ):
            image_bytes, error = material._openai_image_download_bytes(
                "https://cdn.example.com/generated/x.png", "test-key"
            )

        self.assertIsNone(image_bytes)
        self.assertIn("limit", error)

    def test_generated_image_response_rejects_oversize_base64(self):
        """Do not decode an unbounded inline image from a provider response."""
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(b"too-large").decode()}]}
        )
        with patch("app.services.material.OPENAI_IMAGE_MAX_BYTES", 5):
            image_bytes, error = material._parse_openai_image_response(response, "test-key")

        self.assertIsNone(image_bytes)
        self.assertIn("limit", error)

    def test_generated_image_decode_rejects_pillow_bomb_warning(self):
        """Small compressed bytes can still expand to too many image pixels."""
        image_bytes = _png_bytes(width=12, height=12)
        with (
            patch.object(Image, "MAX_IMAGE_PIXELS", 100),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with self.assertRaises(material._OpenAIImageDecodeError):
                material._save_openai_image_file(image_bytes, self.save_dir)

        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generated_image_download_failure_does_not_buy_another_image(self):
        """A confirmed paid result remains a task failure if its URL cannot download."""
        signed_url = "https://cdn.example.com/generated/x.png?token=private"
        response = _image_response(
            {"data": [{"url": signed_url}]}
        )
        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch(
                "app.services.material.requests.get",
                side_effect=requests.exceptions.ConnectionError(
                    f"download failed for {signed_url}"
                ),
            ) as get,
            patch("app.services.material.time.sleep"),
            patch("app.services.material.logger") as logger,
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be downloaded"):
                material.download_videos(
                    task_id="test-openai-image-download-failure",
                    search_terms=["first", "second"],
                    source="openai_image",
                    audio_duration=10,
                    max_clip_duration=5,
                )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, material.OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS)
        self.assertNotIn("token=private", str(logger.warning.call_args_list))

    def test_generate_images_openai_returns_empty_on_rejected_request(self):
        """Business rejection (such as content policy) returns an empty result and retry without backoff."""
        response = _image_response(
            {"error": {"message": "content policy violation"}}, status_code=400
        )

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "blocked term", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    # ------------------------------------------------------------------
    # Configuration switches
    # ------------------------------------------------------------------

    def test_is_openai_image_enabled_requires_full_configuration(self):
        """
        base_url and model are required; API key is allowed to be empty - completely local
        ComfyUI/SD gateway usually does not require authentication.
        """
        self.assertTrue(material.is_openai_image_enabled())

        config.app["openai_image_base_url"] = ""
        self.assertFalse(material.is_openai_image_enabled())
        config.app["openai_image_base_url"] = "https://img.example.com/v1"

        # Local authentication-free gateway: even without key, it is considered enabled
        config.app["openai_image_api_keys"] = []
        self.assertTrue(material.is_openai_image_enabled())
        config.app["openai_image_api_keys"] = ["sk-test-key"]

        config.app["openai_image_model"] = ""
        self.assertFalse(material.is_openai_image_enabled())

    def test_generate_images_openai_sends_no_authorization_without_key(self):
        """
        When the API key is not configured, it must be generated as usual, and the request does not include the Authorization header.
        For use by authentication-free local ComfyUI/SD gateways.
        """
        config.app["openai_image_api_keys"] = []
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
        ):
            results = material.generate_images_openai(
                "local gateway", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_generate_images_openai_retries_connect_timeout(self):
        """
        A timeout in the connection phase indicates that the request has not been delivered to the server, and the accounting task cannot have been created.
        Allow backoff retries.
        """
        image_data = _png_bytes()
        responses = [
            requests.exceptions.ConnectTimeout("connect timed out"),
            _image_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
            ),
        ]

        with (
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_openai(
                "connect timeout term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)

    def test_generate_images_openai_does_not_retry_unconfirmed_errors(self):
        """
        Read timeout/connection interruption belongs to the "unconfirmed" state: the server may have generated and deducted the fee, but
        No response was returned. Automatic resubmission will cause repeated generation and repeated billing, and must be directly
        The task fails and is terminated, and you cannot continue to submit payment requests for subsequent keywords.
        """
        for error in (
            requests.exceptions.ReadTimeout("read timed out"),
            requests.exceptions.ConnectionError("connection dropped"),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    patch(
                        "app.services.material.requests.post", side_effect=error
                    ) as post,
                    patch("app.services.material.time.sleep") as sleep,
                ):
                    with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                        material.generate_images_openai(
                            "unconfirmed term",
                            minimum_duration=5,
                            save_dir=self.save_dir,
                        )

                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()

    def test_download_videos_openai_image_stops_after_unconfirmed_paid_request(self):
        """Earlier images must not hide a later ambiguous paid submission."""
        image_response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )
        config.app["material_directory"] = self.save_dir

        with (
            patch(
                "app.services.material.requests.post",
                side_effect=[
                    image_response,
                    requests.exceptions.ReadTimeout("response lost"),
                    image_response,
                ],
            ) as post,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/rendered.mp4",
            ),
            patch("app.services.material._persist_material_sources"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                material.download_videos(
                    task_id="test-openai-image-unconfirmed",
                    search_terms=["first", "uncertain", "third"],
                    source="openai_image",
                    audio_duration=20,
                    max_clip_duration=5,
                )

        self.assertEqual(post.call_count, 2)

    def test_generate_images_openai_size_defaults_and_override(self):
        """
        By default, the official OpenAI compatible size is taken according to the frame (portrait 1024x1536 /
        landscape 1536x1024); completely covered after configuring openai_image_size,
        For use by local gateways supporting any resolution.
        """
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as default_post:
            material.generate_images_openai(
                "landscape term",
                minimum_duration=5,
                video_aspect=material.VideoAspect.landscape,
                save_dir=self.save_dir,
            )
        # Horizontal screen defaults to OpenAI official compatible size
        self.assertEqual(default_post.call_args.kwargs["json"]["size"], "1536x1024")

        config.app["openai_image_size"] = "1080x1920"
        self.addCleanup(config.app.pop, "openai_image_size", None)

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "custom size term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(
            post.call_args.kwargs["json"]["size"],
            "1080x1920",
        )

    def test_generate_images_openai_raises_without_base_url(self):
        """When calling directly and base_url is not configured, an error with configuration guidance must be thrown."""
        config.app["openai_image_base_url"] = ""
        with self.assertRaises(ValueError):
            material.generate_images_openai("term", minimum_duration=5)

    # ------------------------------------------------------------------
    # prompt word template
    # ------------------------------------------------------------------

    def test_generate_images_openai_applies_prompt_template(self):
        """
        When a template containing {term} placeholder is configured, the requested prompt must be the template replacement result.
        Unified additional style modifications improve the matching of images and texts.
        """
        config.app["openai_image_prompt_template"] = (
            "cinematic photo of {term}, photorealistic, high detail"
        )
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "晨光中的玻璃杯", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(
            post.call_args.kwargs["json"]["prompt"],
            "cinematic photo of 晨光中的玻璃杯, photorealistic, high detail",
        )

    def test_generate_images_openai_sends_raw_term_without_template(self):
        """When the template is not configured, the prompt must be the original text of the keyword, and the behavior is consistent with the old version."""
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "raw term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(post.call_args.kwargs["json"]["prompt"], "raw term")

    def test_generate_images_openai_falls_back_when_template_lacks_placeholder(self):
        """When the template does not contain the {term} placeholder, keywords cannot be injected and the original text must be returned."""
        config.app["openai_image_prompt_template"] = "no placeholder here"
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "fallback term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(post.call_args.kwargs["json"]["prompt"], "fallback term")

    # ------------------------------------------------------------------
    # download_videos distribution and on-demand generation
    # ------------------------------------------------------------------

    def test_download_videos_openai_image_generates_on_demand_and_stops(self):
        """
        Vincent pictures are charged on a per-picture basis and cannot be generated for all keywords first and then selected. Materials must be on demand one by one
        Generate, after the cumulative effective duration (capped by segment duration) reaches the required dubbing duration, subsequent keywords
        No more paid requests are triggered.
        """
        generated = {
            "term-1": [self._generated_item("term-1", "/tmp/img-1.png")],
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        def fake_render(image_path, clip_duration):
            return f"{image_path}.mp4"

        with (
            patch(
                "app.services.material.generate_images_openai",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                side_effect=fake_render,
            ) as render,
        ):
            result = material.download_videos(
                task_id="test-openai-image-lazy",
                search_terms=["term-1", "term-2", "term-3"],
                source="openai_image",
                audio_duration=8,
                max_clip_duration=5,
            )

        # 5s + 5s > 8s, the third keyword can no longer generate paid generation requests
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call.kwargs["search_term"] for call in generate.call_args_list],
            ["term-1", "term-2"],
        )
        # Each image is rendered into an mp4 clip and is not included in the duration.
        self.assertEqual(render.call_count, 2)
        self.assertEqual(result, ["/tmp/img-1.png.mp4", "/tmp/img-2.png.mp4"])

    def test_download_videos_openai_image_continues_after_invalid_image(self):
        """
        When the first compatible interface response cannot be decoded, only the corresponding keyword is skipped, and a subsequent legal picture can still be
        Complete placement and rendering, and verify that the fix covers the real on-demand generation call chain and not just a single function.
        """
        invalid_response = _image_response(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(
                            b"<html>gateway degraded</html>"
                        ).decode("ascii")
                    }
                ]
            }
        )
        valid_response = _image_response(
            {
                "data": [
                    {"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}
                ]
            }
        )
        config.app["material_directory"] = self.save_dir

        with (
            patch(
                "app.services.material.requests.post",
                side_effect=[invalid_response, valid_response],
            ) as post,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/rendered-openai-image.mp4",
            ) as render,
            patch("app.services.material._persist_material_sources"),
        ):
            result = material.download_videos(
                task_id="test-openai-image-invalid-then-valid",
                search_terms=["invalid term", "valid term"],
                source="openai_image",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(result, ["/tmp/rendered-openai-image.mp4"])

    def test_download_videos_openai_image_stops_when_duration_exactly_covered(self):
        """Boundary regression: Just enough time is enough, the stopping judgment must be >= instead of >."""
        generated = {
            "term-1": [self._generated_item("term-1", "/tmp/img-1.png")],
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_images_openai",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/rendered.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-openai-image-exact",
                search_terms=["term-1", "term-2", "term-3"],
                source="openai_image",
                audio_duration=10,
                max_clip_duration=5,
            )

        # 5s + 5s == 10s, exactly covered, the 3rd paragraph must not be generated
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(len(result), 2)

    def test_download_videos_openai_image_bypasses_search_cache(self):
        """
        The generated results are one-time image files and do not participate in the 24-hour search cache - caching will make a difference
        The task is to get the same picture repeatedly. download_videos must go directly to the on-demand build branch.
        """
        with (
            patch(
                "app.services.material.generate_images_openai",
                return_value=[self._generated_item("sunrise", "/tmp/img-1.png")],
            ) as generate,
            patch("app.services.material._search_videos_with_cache") as cached_search,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/img-1.png.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-openai-image-cache-bypass",
                search_terms=["sunrise"],
                source="openai_image",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 1)
        cached_search.assert_not_called()
        self.assertEqual(result, ["/tmp/img-1.png.mp4"])

    def test_download_videos_openai_image_skips_rejected_segment(self):
        """
        If the generated result of explicit rejection is empty, skip this keyword and continue processing the next fragment.
        """
        generated = {
            "term-1": [],  # Build failed
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        def fake_render(image_path, clip_duration):
            return f"{image_path}.mp4"

        with (
            patch(
                "app.services.material.generate_images_openai",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                side_effect=fake_render,
            ),
        ):
            result = material.download_videos(
                task_id="test-openai-image-skip",
                search_terms=["term-1", "term-2", "term-3"],
                source="openai_image",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/img-2.png.mp4"])

    def test_download_videos_openai_image_stops_after_render_failure(self):
        """A paid image saved locally should not trigger another purchase if render fails."""
        generated_item = self._generated_item("first", "/tmp/img-1.png")
        with (
            patch(
                "app.services.material.generate_images_openai",
                return_value=[generated_item],
            ) as generate,
            patch("app.services.material._render_openai_image_video", return_value=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be rendered"):
                material.download_videos(
                    task_id="test-openai-image-render-failure",
                    search_terms=["first", "second"],
                    source="openai_image",
                    audio_duration=10,
                    max_clip_duration=5,
                )

        self.assertEqual(generate.call_count, 1)

    def test_download_videos_openai_image_skips_generation_without_audio(self):
        """If the dubbing duration is not positive, you will return empty-handed, and you will not pay per picture for impossible tasks."""
        with patch("app.services.material.generate_images_openai") as generate:
            result = material.download_videos(
                task_id="test-openai-image-no-audio",
                search_terms=["term-1"],
                source="openai_image",
                audio_duration=0,
                max_clip_duration=5,
            )

        generate.assert_not_called()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
