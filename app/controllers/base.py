import secrets
from typing import Annotated
from uuid import uuid4

from fastapi import Header, Request

from app.config import config
from app.models.exception import HttpException

MAX_TASK_ID_LENGTH = 128


def normalize_task_id(value: object) -> str:
    """Return a log-safe request ID, replacing invalid client input with a UUID."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TASK_ID_LENGTH
        or not value.isprintable()
    ):
        return str(uuid4())
    return value


def get_task_id(request: Request) -> str:
    return normalize_task_id(request.headers.get("x-task-id"))


def get_api_key(request: Request):
    api_key = request.headers.get("x-api-key")
    return api_key


def get_api_key_values(request: Request) -> list[str]:
    """Returns all API Key Headers in the request, retaining duplicate values ​​for security verification."""

    # Starlette Headers provides getlist(), which can distinguish duplicate headers sent by the proxy or client.
    # The lightweight Request double in unit tests just uses a plain dict, so compatibility fallbacks are preserved.
    get_list = getattr(request.headers, "getlist", None)
    if callable(get_list):
        return [value for value in get_list("x-api-key") if isinstance(value, str)]

    api_key = get_api_key(request)
    return [api_key] if isinstance(api_key, str) else []


def verify_token(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
):
    """Determine whether to verify the API Key according to configuration.

    Empty Key retains the existing local authentication-free mode; after the administrator explicitly configures a non-empty Key, the API
    Routing and task product downloads will require the client to provide the same request header through the ``x-api-key`` request header.
    value. The parameter declaration also allows Swagger to display the request header to facilitate debugging in a protected environment.
    """

    configured_key = config.app.get("api_key", "")
    if configured_key in (None, ""):
        return None

    # Configuration items must be strings. Here, error types such as lists and numbers are rejected to avoid string implicits.
    # The transformation produces authentication behavior that is difficult to detect; the error message also does not contain the actual key.
    if not isinstance(configured_key, str):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=500,
            message="API authentication is misconfigured",
        )

    # FastAPI parameters are used to declare x-api-key in OpenAPI; actual verification always reads Request,
    # Only in this way can the header with the same name be sent repeatedly. Duplicate headers for normal client and reverse proxy pairs
    # may take values in a different order and must therefore be rejected rather than implicitly taking the first or last value.
    token_values = get_api_key_values(request)
    if not token_values and isinstance(x_api_key, str):
        token_values = [x_api_key]

    if len(token_values) != 1:
        raise HttpException(
            task_id=get_task_id(request),
            status_code=401,
            message="invalid API key",
        )

    # compare_digest only supports ASCII for str. The request header is an untrusted input and the attacker
    # It is possible to send Latin-1 characters to trigger a TypeError. Uniformly encoded into UTF-8 bytes and retained
    # Constant time comparison, also supports legal Unicode Key in TOML.
    token = token_values[0]
    if not secrets.compare_digest(
        token.encode("utf-8"), configured_key.encode("utf-8")
    ):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=401,
            message="invalid API key",
        )

    return None
