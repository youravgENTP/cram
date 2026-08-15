import argparse
import json
import shlex
import subprocess
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path
from typing import Any, Optional

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

    parser.add_argument(
        "--only",
        type=str,
        help="Process only the video with this exact filename.",
    )

    parser.add_argument(
        "--workers",
        type=int,
        help="Number of videos to encode concurrently.",
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run FFmpeg. Without this flag, cram performs a dry run.",
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
    cli_config: Optional[Path],
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

    processing = config.get(
        "processing",
        {},
    )

    workers = processing.get(
        "workers",
        1,
    )

    if not isinstance(workers, int) or workers <= 0:
        raise ValueError(
            "processing.workers must be a positive integer"
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


# 원본 영상에 대응하는 출력 파일 경로 계산
def build_output_path(
    video_path: Path,
    input_directory: Path,
    config: dict[str, Any],
) -> Path:
    output = config["output"]

    output_directory = (
        input_directory
        / output["directory"]
    )

    suffix = output["suffix"]

    output_filename = (
        f"{video_path.stem}"
        f"{suffix}"
        f"{video_path.suffix.lower()}"
    )

    return output_directory / output_filename

# config를 바탕으로 FFmpeg 명령 생성
def build_ffmpeg_command(
    video_path: Path,
    output_path: Path,
    config: dict[str, Any],
) -> list[str]:
    video = config["video"]
    audio = config["audio"]

    command = [
        "ffmpeg",
        "-noautorotate",
        "-i",
        str(video_path),
        "-vf",
        f"fps={video['fps']}",
        "-c:v",
        str(video["codec"]),
        "-b:v",
        str(video["bitrate"]),
        "-c:a",
        str(audio["codec"]),
        "-b:a",
        str(audio["bitrate"]),
        str(output_path),
    ]

    return command

# CLI 또는 config에서 worker 수 결정
def resolve_workers(
    cli_workers: Optional[int],
    config: dict[str, Any],
) -> int:
    if cli_workers is not None:
        workers = cli_workers
    else:
        processing = config.get(
            "processing",
            {},
        )

        workers = processing.get(
            "workers",
            1,
        )

    if not isinstance(workers, int) or workers <= 0:
        raise ValueError(
            "workers must be a positive integer"
        )

    return workers

# FFmpeg 명령 실제 실행
def run_ffmpeg(
    command: list[str],
    output_path: Path,
) -> tuple[bool, str]:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode == 0:
        return True, ""

    if output_path.exists():
        output_path.unlink()

    return False, result.stderr


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

    if args.only is not None:
        videos = [
            video
            for video in videos
            if video.name == args.only
        ]

        if not videos:
            raise FileNotFoundError(
                f"Video not found in input directory: {args.only}"
            )

    workers = resolve_workers(
        cli_workers=args.workers,
        config=config,
    )

    print_config(
        config_path=config_path,
        input_directory=input_directory,
        config=config,
    )

    print(f"Workers")
    print(f"  {workers}")
    print()
    print(f"Found {len(videos)} video file(s).")
    print()

    jobs = []

    for index, video_path in enumerate(videos, start=1):
        output_path = build_output_path(
            video_path=video_path,
            input_directory=input_directory,
            config=config,
        )

        should_skip = (
            config["output"]["skip_existing"]
            and output_path.exists()
        )

        if should_skip:
            print(
                f"[{index}/{len(videos)}] "
                f"SKIP "
                f"{video_path.name}"
            )

            print(
                f"  -> {output_path}"
            )

            continue

        command = build_ffmpeg_command(
            video_path=video_path,
            output_path=output_path,
            config=config,
        )

        if not args.execute:
            print(
                f"[{index}/{len(videos)}] "
                f"READY "
                f"{video_path.name}"
            )

            print(
                f"  -> {output_path}"
            )

            print(
                f"  ffmpeg: {shlex.join(command)}"
            )

            continue

        jobs.append(
            (
                index,
                video_path,
                output_path,
                command,
            )
        )

    if not args.execute:
        return

    if not jobs:
        print("No videos to encode.")
        return

    print()
    print(
        f"Starting {len(jobs)} encoding job(s) "
        f"with {workers} worker(s)."
    )
    print()

    with ThreadPoolExecutor(
        max_workers=workers,
    ) as executor:
        future_to_job = {}

        for (
            index,
            video_path,
            output_path,
            command,
        ) in jobs:
            print(
                f"[{index}/{len(videos)}] "
                f"START "
                f"{video_path.name}"
            )

            future = executor.submit(
                run_ffmpeg,
                command,
                output_path,
            )

            future_to_job[future] = (
                index,
                video_path,
                output_path,
            )

        for future in as_completed(future_to_job):
            (
                index,
                video_path,
                output_path,
            ) = future_to_job[future]

            success, error_output = future.result()

            if success:
                print(
                    f"[{index}/{len(videos)}] "
                    f"DONE "
                    f"{video_path.name}"
                )

                print(
                    f"  -> {output_path}"
                )
            else:
                print(
                    f"[{index}/{len(videos)}] "
                    f"FAILED "
                    f"{video_path.name}"
                )

                if error_output:
                    print("  FFmpeg error:")

                    for line in error_output.splitlines()[-10:]:
                        print(
                            f"    {line}"
                        )

            print()

if __name__ == "__main__":
    main()