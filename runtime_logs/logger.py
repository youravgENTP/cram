import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = (
    Path(__file__).resolve().parent.parent
)

LOG_DIRECTORY = (
    PROJECT_ROOT
    / "logs"
)

DEFAULT_LOG_PATH = (
    LOG_DIRECTORY
    / "runs.jsonl"
)


class RuntimeLogger:
    def __init__(
        self,
        log_path: Path = DEFAULT_LOG_PATH,
    ) -> None:
        timestamp = datetime.now().strftime(
            "%Y%m%dT%H%M%S"
        )

        self.run_id = (
            f"{timestamp}-"
            f"{uuid.uuid4().hex[:8]}"
        )

        self.log_path = log_path

        self._lock = threading.Lock()
        self._warning_shown = False

    def event(
        self,
        event: str,
        **fields: Any,
    ) -> None:
        record = {
            "timestamp": (
                datetime.now()
                .astimezone()
                .isoformat(
                    timespec="seconds"
                )
            ),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }

        try:
            self.log_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            line = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            with self._lock:
                with self.log_path.open(
                    "a",
                    encoding="utf-8",
                ) as file:
                    file.write(line)
                    file.write("\n")

        except OSError as error:
            if not self._warning_shown:
                print(
                    "WARNING: runtime log "
                    f"could not be written: {error}"
                )

                self._warning_shown = True