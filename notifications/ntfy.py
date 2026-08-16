import argparse
import json
import os
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_SERVER = "https://ntfy.sh"
DEFAULT_TITLE = "cram"


class NtfyError(RuntimeError):
    """Raised when an ntfy notification cannot be sent."""


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
    resolved_topic = (
        topic
        or os.environ.get("NTFY_TOPIC")
    )

    if not resolved_topic:
        raise NtfyError(
            "NTFY_TOPIC is not configured."
        )

    resolved_server = (
        server
        or os.environ.get("NTFY_SERVER")
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