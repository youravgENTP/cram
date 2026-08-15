from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / ".state.json"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "ipad_goodnotes.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cram",
        description="Config-driven video compression CLI powered by FFmpeg.",
    )

    parser.add_argument(
        "--config",
        type=Path,
        help="Path to a YAML config file.",
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path.cwd(),
        help="Input directory. Defaults to the current working directory.",
    )

    return parser.parse_args()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(config_path: Path) -> None:
    try:
        relative_path = config_path.resolve().relative_to(PROJECT_ROOT)
        stored_path = str(relative_path)
    except ValueError:
        stored_path = str(config_path.resolve())

    state = {
        "last_config": stored_path,
    }

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)


def resolve_config_path(
    cli_config: Path | None,
    state: dict[str, Any],
) -> Path:
    if cli_config is not None:
        config_path = cli_config.expanduser()

        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()

        return config_path

    last_config = state.get("last_config")

    if last_config:
        config_path = Path(last_config).expanduser()

        if not config_path.is_absolute():
            config_path = (PROJECT_ROOT / config_path).resolve()

        return config_path

    return DEFAULT_CONFIG.resolve()


# config.yaml 문서 로딩
def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}"
        )

    if not config_path.is_file():
        raise ValueError(
            f"Config path is not a file: {config_path}"
        )

    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(
            f"Config must contain a YAML mapping: {config_path}"
        )

    return config

# config 문서의 타당성 검증
def validate_config(config: dict[str, Any]) -> None:
    required_sections = [
        "video",
        "audio",
        "output",
        "source",
    ]

    for section in required_sections:
        if section not in config:
            raise ValueError(
                f"Missing required config section: {section}"
            )

        if not isinstance(config[section], dict):
            raise ValueError(
                f"Config section must be a mapping: {section}"
            )

    video = config["video"]
    audio = config["audio"]
    output = config["output"]
    source = config["source"]

    required_video_fields = [
        "codec",
        "fps",
        "bitrate",
        "keep_resolution",
    ]

    required_audio_fields = [
        "codec",
        "bitrate",
    ]

    required_output_fields = [
        "directory",
        "suffix",
        "skip_existing",
        "preserve_filename",
    ]

    for field in required_video_fields:
        if field not in video:
            raise ValueError(
                f"Missing video config value: {field}"
            )

    for field in required_audio_fields:
        if field not in audio:
            raise ValueError(
                f"Missing audio config value: {field}"
            )

    for field in required_output_fields:
        if field not in output:
            raise ValueError(
                f"Missing output config value: {field}"
            )

    extensions = source.get("extensions")

    if not isinstance(extensions, list) or not extensions:
        raise ValueError(
            "source.extensions must be a non-empty list"
        )

    if not all(isinstance(extension, str) for extension in extensions):
        raise ValueError(
            "Every source extension must be a string"
        )

    fps = video["fps"]

    if not isinstance(fps, (int, float)) or fps <= 0:
        raise ValueError(
            "video.fps must be a positive number"
        )

# 디렉토리 내 영상탐색 함수
def discover_videos(
    input_directory: Path,
    config: dict[str, Any],
) -> list[Path]:
    extensions = {
        extension.lower()
        for extension in config["source"]["extensions"]
    }

    videos = [
        path
        for path in input_directory.iterdir()
        if path.is_file()
        and not path.name.startswith("._")
        and path.suffix.lower() in extensions
    ]

    return sorted(
        videos,
        key=lambda path: path.name.casefold(),
    )


def print_config(
    config_path: Path,
    input_directory: Path,
    config: dict[str, Any],
) -> None:
    video = config.get("video", {})
    audio = config.get("audio", {})
    output = config.get("output", {})

    resolution = (
        "original"
        if video.get("keep_resolution", False)
        else "configured"
    )

    print()
    print("cram")
    print()
    print("Input")
    print(f"  {input_directory}")
    print()
    print("Config")
    print(f"  {config_path}")
    print()
    print("Video")
    print(f"  codec:      {video.get('codec')}")
    print(f"  fps:        {video.get('fps')}")
    print(f"  bitrate:    {video.get('bitrate')}")
    print(f"  resolution: {resolution}")
    print()
    print("Audio")
    print(f"  codec:      {audio.get('codec')}")
    print(f"  bitrate:    {audio.get('bitrate')}")
    print()
    print("Output")
    print(f"  directory:  {output.get('directory')}")
    print(f"  suffix:     {output.get('suffix')}")
    print(f"  skip existing: {output.get('skip_existing')}")
    print()


def main() -> None:
    args = parse_args()

    input_directory = args.input.expanduser().resolve()

    if not input_directory.exists():
        raise FileNotFoundError(
            f"Input directory not found: {input_directory}"
        )

    if not input_directory.is_dir():
        raise ValueError(
            f"Input path is not a directory: {input_directory}"
        )

    state = load_state()
    config_path = resolve_config_path(args.config, state)
    config = load_config(config_path)

    validate_config(config)

    save_state(config_path)

    videos = discover_videos(
        input_directory=input_directory,
        config=config,
    )

    print_config(
        config_path=config_path,
        input_directory=input_directory,
        config=config,
    )

    print(f"Found {len(videos)} video file(s).")
    print()

    for index, video_path in enumerate(videos, start=1):
        print(
            f"[{index}/{len(videos)}] "
            f"{video_path.name}"
        )


if __name__ == "__main__":
    main()