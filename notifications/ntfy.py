import argparse
import json
import os
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_SERVER = "https://ntfy.sh"
DEFAULT_TITLE = "cram"
LOCAL_CONFIG_PATH = (
    Path(__file__).resolve().parent
    / "ntfy.local.json"
)


class NtfyError(RuntimeError):
    """Raised when an ntfy notification cannot be sent."""


def load_local_config() -> dict:
    if not LOCAL_CONFIG_PATH.exists():
        return {}

    try:
        with LOCAL_CONFIG_PATH.open(
            "r",
            encoding="utf-8",
        ) as file:
            config = json.load(file)

    except (
        OSError,
        json.JSONDecodeError,
    ) as error:
        raise NtfyError(
            "Could not read local ntfy config: "
            f"{error}"
        ) from error

    if not isinstance(config, dict):
        raise NtfyError(
            "ntfy.local.json must contain "
            "a JSON object."
        )

    return config

def send_ntfy(
    message: str,
    *,
    title: str = DEFAULT_TITLE,
    tags: Optional[list[str]] = None,
    priority: str = "default",
    topic: Optional[str] = None,
    server: Optional[str] = None,
    timeout: float = 10.0,
) -> None:
    local_config = load_local_config()

    resolved_topic = (
        topic
        or os.environ.get("NTFY_TOPIC")
        or local_config.get("topic")
    )

    if not resolved_topic:
        raise NtfyError(
            "ntfy topic is not configured. "
            "Set NTFY_TOPIC or create "
            "notifications/ntfy.local.json."
        )

    resolved_server = (
        server
        or os.environ.get("NTFY_SERVER")
        or local_config.get("server")
        or DEFAULT_SERVER
    ).rstrip("/")

    priority_values = {
        "min": 1,
        "low": 2,
        "default": 3,
        "high": 4,
        "urgent": 5,
    }

    payload = {
        "topic": resolved_topic,
        "message": message,
        "title": title,
        "priority": priority_values[priority],
    }

    if tags:
        payload["tags"] = tags

    request = Request(
        f"{resolved_server}/",
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(
            request,
            timeout=timeout,
        ) as response:
            if not 200 <= response.status < 300:
                raise NtfyError(
                    "ntfy returned HTTP "
                    f"{response.status}"
                )

    except HTTPError as error:
        raise NtfyError(
            "ntfy returned HTTP "
            f"{error.code}"
        ) from error

    except URLError as error:
        raise NtfyError(
            f"Could not reach ntfy: {error.reason}"
        ) from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a test notification through ntfy.",
    )

    parser.add_argument(
        "message",
        help="Notification message.",
    )

    parser.add_argument(
        "--title",
        default=DEFAULT_TITLE,
        help="Notification title.",
    )

    parser.add_argument(
        "--priority",
        default="default",
        choices=[
            "min",
            "low",
            "default",
            "high",
            "urgent",
        ],
        help="ntfy notification priority.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        send_ntfy(
            message=args.message,
            title=args.title,
            priority=args.priority,
            tags=["movie_camera"],
        )
    except NtfyError as error:
        raise SystemExit(
            f"ntfy error: {error}"
        ) from error

    print("ntfy notification sent.")


if __name__ == "__main__":
    main()