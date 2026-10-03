import math
import os
import time
from typing import Any, Mapping
from urllib.parse import quote_plus

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect


DEFAULT_BASE_URL = "https://api.ofox.ai/v1"
DEFAULT_MODEL_ID = "bytedance/seedance-2.0-fast"
DEFAULT_RESOLUTION = "720p"
# The international manufacturer channel is fixed by default: the content policy is more consistent when facing a global audience; if the explicit configuration is empty, it will be returned.
# Gateways are distributed among available vendors by weight.
DEFAULT_PROVIDER_TYPE = "byteplus"
# The default model bytedance/seedance-2.0-fast only accepts 4-15 seconds (server-side measured verification value).
# Other optional models have different intervals (for example, alibaba/wan-2.7 is 2-15 seconds), and they should be synchronized when switching models.
# Adjust the range in the configuration; requests outside the range will be rejected by the API with a clear 400 and will not be billed.
DEFAULT_MIN_DURATION_SECONDS = 4
DEFAULT_MAX_DURATION_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
MAX_POLL_RETRIES = 5
RETRY_BASE_SECONDS = 1.0
MAX_ERROR_TEXT_LENGTH = 500
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
TERMINAL_SUCCESS_STATUSES = frozenset({"completed", "succeeded"})
TERMINAL_FAILURE_STATUSES = frozenset(
    {"failed", "error", "cancelled", "canceled", "expired"}
)
# Official success path: pending (acquisition to be submitted by upstream) → queued → in_progress → completed
ACTIVE_STATUSES = frozenset({"pending", "queued", "in_progress"})


class OFoxError(RuntimeError):
    """Deterministic configuration, request, or response error."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        # As long as the remote task has been created, all error types carry the task ID. No need for upper level
        # The recovery logic is maintained separately according to exception subcategories, and the WebUI/API can also stably display the basis for troubleshooting.
        self.task_id = task_id


class OFoxUnconfirmedTaskError(OFoxError):
    """The remote end may have created a paid task, but the local machine cannot confirm its final status."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message, task_id=task_id)


class OFoxDownloadError(OFoxError):
    """The remote payment task was successful, but the generated video failed to be downloaded to the local machine."""

    def __init__(self, message: str, task_id: str):
        super().__init__(message, task_id=task_id)


def get_api_key(settings: Mapping[str, Any] | None = None) -> str:
    """
    Read OFox credentials with clear and unique priority.

    Private keys in configuration files have highest priority; the only supported runtime environment variables are semantically explicit
    ``OFOX_API_KEY``。
    """
    settings = config.app if settings is None else settings
    configured = str(settings.get("ofox_api_key", "") or "").strip()
    environment_key = os.getenv("OFOX_API_KEY", "").strip()
    return configured or environment_key


def is_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    return bool(get_api_key(settings))


def _base_url() -> str:
    return str(
        config.app.get("ofox_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    ).rstrip("/")


def _model_id() -> str:
    return str(
        config.app.get("ofox_text_to_video_model", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID
    ).strip()


def _resolution() -> str:
    """
    Read generation resolution.

    OFox verifies the resolution on the server side by model (for example, the default Seedance fast model only accepts
    480p/720p), invalid values will get an explicit 400 rejection and no paid task will be created,
    Therefore, only blanks are removed here, and the local whitelist is not maintained - the whitelist will be updated with the remote model directory.
    Expired due to change. NULL values ​​return to default to avoid submitting empty strings to the remote end.
    """
    value = str(config.app.get("ofox_resolution", DEFAULT_RESOLUTION) or "").strip()
    return value or DEFAULT_RESOLUTION


def _provider_type() -> str:
    """
    Read the upstream vendor routing (provider routing).

    Some models of OFox are supplied by multiple upstream manufacturers (such as volcengine and Seedance series
    byteplus), each manufacturer has its own content policies and regional availability. Default pinned to byteplus
    (International manufacturers have more consistent content policies for global audiences and predictable routing); configure the name of other manufacturers
    If it is explicitly configured as an empty string, it will not be pinned, and will be distributed by the gateway according to the weight. Illegal manufacturers
    The name will be rejected by the API with the exclusive 400 ``invalid_provider_type`` and no payment will be created.
    task, so the vendor whitelist is not maintained locally.
    """
    value = config.app.get("ofox_provider", DEFAULT_PROVIDER_TYPE)
    if value is None:
        return DEFAULT_PROVIDER_TYPE
    return str(value).strip()


def _config_bool(key: str, default: bool) -> bool:
    value = config.app.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _bounded_float(key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.app.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)


def _duration_bounds() -> tuple[int, int]:
    def read(key: str, default: int) -> int:
        try:
            value = int(config.app.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 1 else default

    minimum = read("ofox_min_duration", DEFAULT_MIN_DURATION_SECONDS)
    maximum = read("ofox_max_duration", DEFAULT_MAX_DURATION_SECONDS)
    return minimum, max(minimum, maximum)


def _tls_verify() -> bool:
    return _config_bool("tls_verify", True)


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _redact_secret(value: Any, secret: str) -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "***")
        encoded = quote_plus(secret)
        if encoded != secret:
            text = text.replace(encoded, "***")
    for proxy_url in config.proxy.values():
        proxy_secret = str(proxy_url or "")
        if proxy_secret:
            text = text.replace(proxy_secret, "***")
    return text[:MAX_ERROR_TEXT_LENGTH]


def _response_error(response: Any, api_key: str) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {_status_code(response)}"
    if not isinstance(payload, dict):
        return f"HTTP {_status_code(response)}"
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code") or payload.get("code")
        message = error.get("message") or payload.get("message")
    else:
        code = payload.get("code")
        message = payload.get("message") or error
    detail = ": ".join(str(item) for item in (code, message) if item not in (None, ""))
    return _redact_secret(detail or f"HTTP {_status_code(response)}", api_key)


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    response = getattr(error, "response", None)
    return response is not None and _status_code(response) in RETRYABLE_STATUS_CODES


def generate_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    """Submit an OFox video assignment and wait for the downloadable results address."""
    api_key = get_api_key()
    if not api_key:
        raise OFoxError("OFox video generation requires an OFox API key")

    term = str(search_term or "").strip()
    if not term:
        # Empty prompt words may come from upstream script splitting exceptions. Paid generation sources cannot submit it to the remote end,
        # Otherwise, even if the interface accepts the request, you will only get unusable and billed videos.
        raise OFoxError("OFox search term must not be empty")

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    try:
        requested_duration = max(int(minimum_duration), 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OFoxError("OFox clip duration must be a positive integer") from exc
    minimum, maximum = _duration_bounds()
    duration = min(max(requested_duration, minimum), maximum)
    if duration != requested_duration:
        # Generating longer than requested will not affect the final film: the editing process is still trimmed according to the duration of the clip; generating longer than requested
        # Shorter only occurs when the request exceeds the upper limit of the model, and it can only converge to the upper limit at this time.
        logger.info(
            f"ofox clip duration clamped to the configured model range: "
            f"requested={requested_duration}s, using={duration}s "
            f"(configured {minimum}-{maximum}s)"
        )
    payload = {
        "model": _model_id(),
        "prompt": term,
        "duration": duration,
        "resolution": _resolution(),
        "aspect_ratio": aspect.value,
    }
    provider_type = _provider_type()
    if provider_type:
        payload["provider"] = {"type": provider_type}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    videos_url = f"{_base_url()}/videos"
    logger.info(
        "generating video with OFox: "
        f"model={payload['model']}, term={term!r}, duration={duration}s"
    )

    # The submission interface does not automatically retry: timeout or 5xx may occur after the paid task has been created.
    # Blindly retrying will result in repeated deductions. A deterministic failure is determined only when an explicit rejection response is received.
    try:
        response = requests.post(
            videos_url,
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_tls_verify(),
            timeout=(30, 60),
            allow_redirects=False,
        )
    except Exception as exc:
        raise OFoxUnconfirmedTaskError(
            "OFox submission returned no response; a paid task may already "
            "exist remotely: "
            f"error={type(exc).__name__}, detail={_redact_secret(exc, api_key)}"
        ) from exc

    status_code = _status_code(response)
    if 300 <= status_code < 400:
        raise OFoxUnconfirmedTaskError(
            "OFox submission returned a redirect; the paid task state is "
            "unknown and the request was not replayed"
        )
    if status_code >= 500:
        raise OFoxUnconfirmedTaskError(
            f"OFox submission failed with HTTP {status_code}; a paid task may "
            "already exist remotely"
        )
    if not 200 <= status_code < 300:
        # 4xx is explicitly rejected (such as duration/resolution beyond the model support range), the remote
        # There is no task created and there is no risk of double billing; the error message contains the error message given by the server.
        # The legal value range is directly thrown to the user for configuration modification.
        raise OFoxError(
            "OFox video generation request rejected: "
            f"HTTP {status_code}, {_response_error(response, api_key)}"
        )
    try:
        body = response.json()
    except Exception as exc:
        raise OFoxUnconfirmedTaskError(
            "OFox submission returned an unreadable response; a paid task may "
            f"already exist remotely: error={type(exc).__name__}"
        ) from exc
    task_id = str(body.get("id") or "").strip() if isinstance(body, dict) else ""
    if not task_id:
        raise OFoxUnconfirmedTaskError(
            "OFox accepted the submission without returning a task id"
        )
    logger.info(f"OFox video task created: id={task_id}")

    task = _wait_for_task(
        task_id=task_id,
        videos_url=videos_url,
        headers=headers,
        api_key=api_key,
    )
    if task is None:
        return []

    # The official recommendation is to use mirror_urls (OFox CDN persistent signature address) first, and only enable mirroring in the upstream.
    # Return when missing), fall back to unsigned_urls when missing (upstream temporary direct link, may expire within 24 hours
    # period). Both must be retained in their entirety and immediately available for download, with no long-term source_info written to them.
    video_url = ""
    for field in ("mirror_urls", "unsigned_urls"):
        urls = task.get(field)
        for candidate in urls if isinstance(urls, list) else []:
            if isinstance(candidate, str) and candidate.startswith(
                ("http://", "https://")
            ):
                video_url = candidate
                break
        if video_url:
            break
    if not video_url:
        raise OFoxError(
            f"OFox task completed without a downloadable video: id={task_id}",
            task_id=task_id,
        )

    return [
        MaterialInfo(
            provider="ofox",
            url=video_url,
            duration=duration,
            source_info={
                "provider": "ofox",
                "search_term": term,
                "asset_id": task_id,
                "rendition": {
                    "id": task_id,
                    "width": video_width,
                    "height": video_height,
                },
            },
        )
    ]


def _wait_for_task(
    *,
    task_id: str,
    videos_url: str,
    headers: dict[str, str],
    api_key: str,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + _bounded_float(
        "ofox_run_timeout",
        DEFAULT_RUN_TIMEOUT_SECONDS,
        60.0,
        7200.0,
    )
    poll_interval = _bounded_float(
        "ofox_poll_interval",
        DEFAULT_POLL_INTERVAL_SECONDS,
        0.5,
        60.0,
    )
    consecutive_failures = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OFoxUnconfirmedTaskError(
                "OFox task is still running after the configured local wait "
                f"timeout: id={task_id}",
                task_id=task_id,
            )

        # The connect/read timeout of requests are timed separately, so each uses the remaining total time.
        # Half. Even if connections and reads reach the upper limit, a single round of requests will not intentionally exceed the total deadline;
        # The network library may still have a small scheduling error, and the next deadline check will prevent another retry.
        phase_timeout = max(min(remaining / 2.0, 30.0), 0.001)
        try:
            response = requests.get(
                f"{videos_url}/{task_id}",
                headers=headers,
                proxies=config.proxy,
                verify=_tls_verify(),
                timeout=(phase_timeout, phase_timeout),
                allow_redirects=False,
            )
            status_code = _status_code(response)
            if status_code in RETRYABLE_STATUS_CODES:
                raise requests.exceptions.HTTPError(
                    f"HTTP {status_code}", response=response
                )
            if not 200 <= status_code < 300:
                raise OFoxUnconfirmedTaskError(
                    "OFox task status is unknown: "
                    f"http_status={status_code}, "
                    f"detail={_response_error(response, api_key)}",
                    task_id=task_id,
                )
            body = response.json()
            if not isinstance(body, dict):
                raise OFoxUnconfirmedTaskError(
                    "OFox task status response is malformed", task_id=task_id
                )
        except OFoxUnconfirmedTaskError:
            raise
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise OFoxUnconfirmedTaskError(
                    "OFox polling failed and the paid task state is unknown: "
                    f"error={type(exc).__name__}, "
                    f"detail={_redact_secret(exc, api_key)}",
                    task_id=task_id,
                ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OFoxUnconfirmedTaskError(
                    "OFox task is still running after the configured local wait "
                    f"timeout: id={task_id}",
                    task_id=task_id,
                ) from exc
            consecutive_failures += 1
            if consecutive_failures > MAX_POLL_RETRIES:
                raise OFoxUnconfirmedTaskError(
                    "OFox polling failed after retries; the paid task may still "
                    f"be running remotely: id={task_id}",
                    task_id=task_id,
                ) from exc
            delay = min(RETRY_BASE_SECONDS * consecutive_failures, remaining)
            logger.warning(
                "OFox polling hit a transient error; retrying the same task: "
                f"id={task_id}, attempt={consecutive_failures}/{MAX_POLL_RETRIES}, "
                f"retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        consecutive_failures = 0
        status = str(body.get("status") or "").strip().lower()
        if status in TERMINAL_SUCCESS_STATUSES:
            return body
        if status in TERMINAL_FAILURE_STATUSES:
            error_detail = body.get("error")
            logger.error(
                "OFox task did not produce a video: "
                f"id={task_id}, status={status}, "
                f"detail={_redact_secret(error_detail, api_key)}"
            )
            # An explicit failure at the remote end means the task has ended and it is safe to continue with subsequent fragments; by
            # The caller decides whether to try again with a different keyword.
            return None
        if status not in ACTIVE_STATUSES:
            raise OFoxUnconfirmedTaskError(
                f"OFox returned an unknown task status: id={task_id}, "
                f"status={status!r}",
                task_id=task_id,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OFoxUnconfirmedTaskError(
                "OFox task is still running after the configured local wait "
                f"timeout: id={task_id}",
                task_id=task_id,
            )
        time.sleep(min(poll_interval, remaining))
