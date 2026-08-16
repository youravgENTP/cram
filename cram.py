import argparse
import csv
import json
import shlex
import subprocess
import tempfile
import time
from datetime import datetime
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path
from typing import Any, Optional

import yaml
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)

from notifications.ntfy import (
    NtfyError,
    send_ntfy,
)

from runtime_logs import RuntimeLogger


PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / ".state.json"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "ipad_goodnotes.yaml"
BENCHMARK_CSV = (
    PROJECT_ROOT
    / "experiments"
    / "benchmark_results.csv"
)

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
        "--benchmark",
        type=str,
        help=(
            "Record this execution as a benchmark "
            "with the given experiment ID."
        ),
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run FFmpeg. Without this flag, cram performs a dry run.",
    )

    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send an ntfy notification when encoding finishes.",
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

def get_file_size_bytes(
    path: Path,
) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None

# 파일 크기를 MB 단위로 반환
def get_file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


# benchmark 결과를 CSV에 기록
def append_benchmark_result(
    experiment_id: str,
    input_directory: Path,
    config_path: Path,
    config: dict[str, Any],
    workers: int,
    file_count: int,
    total_input_mb: float,
    total_output_mb: float,
    elapsed_seconds: float,
    space_saved_percent: float,
    result: str,
) -> None:
    video = config["video"]
    audio = config["audio"]

    row = {
        "experiment_id": experiment_id,
        "date": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "input_set": input_directory.name,
        "config": config_path.name,
        "workers": workers,
        "video_codec": video["codec"],
        "video_bitrate": video["bitrate"],
        "fps": video["fps"],
        "audio_bitrate": audio["bitrate"],
        "file_count": file_count,
        "total_input_mb": f"{total_input_mb:.2f}",
        "total_output_mb": f"{total_output_mb:.2f}",
        "elapsed_seconds": f"{elapsed_seconds:.2f}",
        "throughput_x": "",
        "space_saved_percent": (
            f"{space_saved_percent:.2f}"
        ),
        "result": result,
        "notes": "",
    }

    fieldnames = [
        "experiment_id",
        "date",
        "input_set",
        "config",
        "workers",
        "video_codec",
        "video_bitrate",
        "fps",
        "audio_bitrate",
        "file_count",
        "total_input_mb",
        "total_output_mb",
        "elapsed_seconds",
        "throughput_x",
        "space_saved_percent",
        "result",
        "notes",
    ]

    with BENCHMARK_CSV.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writerow(row)

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


# ffprobe를 이용해 영상 파일 유효성 확인
def probe_video(
    video_path: Path,
) -> tuple[
    bool,
    Optional[float],
    str,
]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            (
                "default="
                "noprint_wrappers=1:"
                "nokey=1"
            ),
            str(video_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return (
            False,
            None,
            result.stderr.strip(),
        )

    try:
        duration = float(
            result.stdout.strip()
        )
    except ValueError:
        return (
            False,
            None,
            "Invalid duration returned by ffprobe",
        )

    if duration <= 0:
        return (
            False,
            duration,
            "Video duration is not positive",
        )

    return (
        True,
        duration,
        "",
    )

# ffprobe를 이용해 영상 길이 확인
def get_video_duration(
    video_path: Path,
) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed: {video_path}"
        )

    return float(
        result.stdout.strip()
    )


# FFmpeg HH:MM:SS 문자열을 초 단위로 변환
def parse_ffmpeg_time(
    value: str,
) -> float:
    parts = value.split(":")

    if len(parts) != 3:
        return 0.0

    hours = float(parts[0])
    minutes = float(parts[1])
    seconds = float(parts[2])

    return (
        hours * 3600
        + minutes * 60
        + seconds
    )


# 초 단위를 HH:MM:SS 문자열로 변환
def format_duration(
    seconds: float,
) -> str:
    if seconds < 0:
        seconds = 0

    total_seconds = int(
        round(seconds)
    )

    hours, remainder = divmod(
        total_seconds,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{seconds:02d}"
    )


# 현재 FFmpeg speed를 이용해 남은 시간 계산
def calculate_eta(
    duration: float,
    current_time: float,
    speed: str,
) -> float:
    try:
        speed_value = float(
            speed.rstrip("x")
        )
    except ValueError:
        return 0.0

    if speed_value <= 0:
        return 0.0

    remaining_video_time = max(
        duration - current_time,
        0.0,
    )

    return (
        remaining_video_time
        / speed_value
    )

# FFmpeg 명령 실제 실행 및 진행률 갱신
def run_ffmpeg(
    command: list[str],
    output_path: Path,
    video_path: Path,
    index: int,
    total_videos: int,
    progress: Progress,
    task_id: TaskID,
) -> tuple[
    bool,
    str,
    float,
    float,
]:

    duration = get_video_duration(
        video_path
    )

    progress.update(
        task_id,
        total=duration,
        completed=0,
        visible=True,
        description=(
            f"[{index}/{total_videos}] "
            f"{video_path.name}"
        ),
        current="00:00:00",
        total_text=format_duration(
            duration
        ),
        speed="--",
        eta="--:--:--",
    )

    progress_command = (
        command[:-1]
        + [
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        + [command[-1]]
    )

    with tempfile.TemporaryFile(
        mode="w+",
        encoding="utf-8",
    ) as error_file:
        encode_start = time.perf_counter()

        process = subprocess.Popen(
            progress_command,
            stdout=subprocess.PIPE,
            stderr=error_file,
            text=True,
            bufsize=1,
        )

        if process.stdout is None:
            raise RuntimeError(
                "Failed to open FFmpeg progress stream"
            )

        progress_data = {}

        for raw_line in process.stdout:
            line = raw_line.strip()

            if "=" not in line:
                continue

            key, value = line.split(
                "=",
                1,
            )

            progress_data[key] = value

            if key != "progress":
                continue

            out_time_seconds = (
                parse_ffmpeg_time(
                    progress_data.get(
                        "out_time",
                        "00:00:00",
                    )
                )
            )

            speed = progress_data.get(
                "speed",
                "--",
            )

            eta_seconds = calculate_eta(
                duration=duration,
                current_time=out_time_seconds,
                speed=speed,
            )

            progress.update(
                task_id,
                completed=min(
                    out_time_seconds,
                    duration,
                ),
                current=format_duration(
                    out_time_seconds
                ),
                total_text=format_duration(
                    duration
                ),
                speed=speed,
                eta=format_duration(
                    eta_seconds
                ),
            )

        return_code = process.wait()

        encode_elapsed = (
            time.perf_counter()
            - encode_start
        )

        error_file.seek(0)

        error_file.seek(0)
        error_output = error_file.read()

    if return_code == 0:
        progress.update(
            task_id,
            completed=duration,
            description=(
                f"[{index}/{total_videos}] "
                f"DONE {video_path.name}"
            ),
            current=format_duration(
                duration
            ),
            total_text=format_duration(
                duration
            ),
            speed="done",
            eta="00:00:00",
        )

        return (
            True,
            "",
            duration,
            encode_elapsed,
        )

    if output_path.exists():
        output_path.unlink()

    progress.update(
        task_id,
        description=(
            f"[{index}/{total_videos}] "
            f"FAILED {video_path.name}"
        ),
        speed="failed",
        eta="--:--:--",
    )

    return (
        False,
        error_output,
        duration,
        encode_elapsed,
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

    runtime_logger = RuntimeLogger()

    run_start_time = (
        time.perf_counter()
    )

    if args.execute:
        runtime_logger.event(
            "run_start",
            input_directory=(
                str(input_directory)
            ),
            config=str(config_path),
            workers=workers,
            video_codec=(
                config["video"]["codec"]
            ),
            video_bitrate=(
                config["video"]["bitrate"]
            ),
            fps=config["video"]["fps"],
            audio_codec=(
                config["audio"]["codec"]
            ),
            audio_bitrate=(
                config["audio"]["bitrate"]
            ),
            discovered_files=len(videos),
        )

    if args.benchmark is not None:
        if not args.execute:
            raise ValueError(
                "--benchmark requires --execute"
            )

        existing_outputs = [
            build_output_path(
                video_path=video_path,
                input_directory=input_directory,
                config=config,
            )
            for video_path in videos
            if build_output_path(
                video_path=video_path,
                input_directory=input_directory,
                config=config,
            ).exists()
        ]

        if existing_outputs:
            raise FileExistsError(
                "Benchmark aborted because existing "
                "compressed output files were found. "
                "Remove the output directory before "
                "running a benchmark."
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

        should_skip = False

        if (
            config["output"]["skip_existing"]
            and output_path.exists()
        ):
            (
                output_valid,
                output_duration,
                probe_error,
            ) = probe_video(
                output_path
            )

            if output_valid:
                should_skip = True

                runtime_logger.event(
                    "file_skip",
                    input_file=str(video_path),
                    output_file=str(output_path),
                    reason=(
                        "valid_existing_output"
                    ),
                    output_duration_seconds=(
                        output_duration
                    ),
                    output_bytes=(
                        get_file_size_bytes(
                            output_path
                        )
                    ),
                )

            else:
                print(
                    f"[{index}/{len(videos)}] "
                    f"INVALID "
                    f"{output_path.name}"
                )

                print(
                    "  -> existing output "
                    "will be re-encoded"
                )

                runtime_logger.event(
                    "file_invalid",
                    input_file=str(video_path),
                    output_file=str(output_path),
                    reason=probe_error,
                    output_bytes=(
                        get_file_size_bytes(
                            output_path
                        )
                    ),
                )

                if args.execute:
                    try:
                        output_path.unlink()

                        runtime_logger.event(
                            "file_invalid_removed",
                            output_file=(
                                str(output_path)
                            ),
                        )

                    except OSError as error:
                        runtime_logger.event(
                            "file_remove_failed",
                            output_file=(
                                str(output_path)
                            ),
                            error=str(error),
                        )

                        raise RuntimeError(
                            "Could not remove "
                            "invalid output: "
                            f"{output_path}"
                        ) from error

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

        runtime_logger.event(
            "run_complete",
            status="nothing_to_encode",
            discovered_files=len(videos),
            encoded_files=0,
            failed_files=0,
            elapsed_seconds=(
                time.perf_counter()
                - run_start_time
            ),
        )

        return

    print()
    print(
        f"Starting {len(jobs)} encoding job(s) "
        f"with {workers} worker(s)."
    )
    print()

    benchmark_start = time.perf_counter()

    failed_jobs = []

    with Progress(
        TextColumn(
            "{task.description}"
        ),
        BarColumn(
            bar_width=24,
        ),
        TaskProgressColumn(),
        TextColumn(
            "{task.fields[current]}"
            " / "
            "{task.fields[total_text]}"
        ),
        TextColumn(
            "{task.fields[speed]}"
        ),
        TextColumn(
            "ETA {task.fields[eta]}"
        ),
        refresh_per_second=4,
    ) as progress:
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
                task_id = progress.add_task(
                    (
                        f"[{index}/{len(videos)}] "
                        f"{video_path.name}"
                    ),
                    total=100,
                    visible=False,
                    current="00:00:00",
                    total_text="--:--:--",
                    speed="--",
                    eta="--:--:--",
                )

                future = executor.submit(
                    run_ffmpeg,
                    command,
                    output_path,
                    video_path,
                    index,
                    len(videos),
                    progress,
                    task_id,
                )

                future_to_job[future] = (
                    index,
                    video_path,
                    output_path,
                )

            for future in as_completed(
                future_to_job
            ):
                (
                    index,
                    video_path,
                    output_path,
                ) = future_to_job[future]

                try:
                    (
                        success,
                        error_output,
                        duration,
                        file_elapsed,
                    ) = future.result()

                except Exception as error:
                    success = False
                    error_output = str(error)
                    duration = 0.0
                    file_elapsed = 0.0

                if success:
                    average_speed = (
                        duration / file_elapsed
                        if file_elapsed > 0
                        else 0.0
                    )

                    runtime_logger.event(
                        "file_complete",
                        input_file=(
                            str(video_path)
                        ),
                        output_file=(
                            str(output_path)
                        ),
                        input_bytes=(
                            get_file_size_bytes(
                                video_path
                            )
                        ),
                        output_bytes=(
                            get_file_size_bytes(
                                output_path
                            )
                        ),
                        duration_seconds=duration,
                        elapsed_seconds=(
                            file_elapsed
                        ),
                        average_speed_x=(
                            average_speed
                        ),
                    )

                else:
                    failed_jobs.append(
                        (
                            video_path,
                            error_output,
                        )
                    )

                    runtime_logger.event(
                        "file_failed",
                        input_file=(
                            str(video_path)
                        ),
                        output_file=(
                            str(output_path)
                        ),
                        duration_seconds=duration,
                        elapsed_seconds=(
                            file_elapsed
                        ),
                        error=error_output,
                    )

    if failed_jobs:
        print()
        print("FFmpeg failures")
        print()

        for (
            video_path,
            error_output,
        ) in failed_jobs:
            print(
                f"FAILED: {video_path.name}"
            )

            if error_output:
                for line in (
                    error_output
                    .splitlines()[-10:]
                ):
                    print(
                        f"  {line}"
                    )

            print()

    if args.notify:
        try:
            if failed_jobs:
                send_ntfy(
                    message=(
                        f"{input_directory.name}\n"
                        f"{len(failed_jobs)}개 영상 압축 실패"
                    ),
                    title="cram 압축 실패",
                    tags=["warning"],
                    priority="high",
                )
            else:
                send_ntfy(
                    message=(
                        f"{input_directory.name}\n"
                        f"{len(jobs)}개 영상 압축 완료"
                    ),
                    title="cram 압축 완료",
                    tags=["white_check_mark"],
                )

            runtime_logger.event(
                "notification_sent",
                provider="ntfy",
            )

        except NtfyError as error:
            runtime_logger.event(
                "notification_failed",
                provider="ntfy",
                error=str(error),
            )

            print()
            print(
                f"Notification failed: {error}"
            )


    elapsed_seconds = time.perf_counter() - benchmark_start

    total_run_elapsed = (
        time.perf_counter()
        - run_start_time
    )

    runtime_logger.event(
        "run_complete",
        status=(
            "success"
            if not failed_jobs
            else "partial_failure"
        ),
        discovered_files=len(videos),
        encoded_files=(
            len(jobs)
            - len(failed_jobs)
        ),
        failed_files=len(failed_jobs),
        workers=workers,
        elapsed_seconds=(
            total_run_elapsed
        ),
    )

    if args.benchmark is not None:
        successful_outputs = [
            build_output_path(
                video_path=video_path,
                input_directory=input_directory,
                config=config,
            )
            for video_path in videos
            if build_output_path(
                video_path=video_path,
                input_directory=input_directory,
                config=config,
            ).exists()
        ]

        total_input_mb = sum(
            get_file_size_mb(video_path)
            for video_path in videos
        )

        total_output_mb = sum(
            get_file_size_mb(output_path)
            for output_path in successful_outputs
        )

        failed_count = (
            len(videos)
            - len(successful_outputs)
        )

        if failed_count == 0:
            result = "success"
        elif successful_outputs:
            result = "partial"
        else:
            result = "failed"

        if total_input_mb > 0:
            space_saved_percent = (
                (
                    total_input_mb
                    - total_output_mb
                )
                / total_input_mb
                * 100
            )
        else:
            space_saved_percent = 0.0

        append_benchmark_result(
            experiment_id=args.benchmark,
            input_directory=input_directory,
            config_path=config_path,
            config=config,
            workers=workers,
            file_count=len(videos),
            total_input_mb=total_input_mb,
            total_output_mb=total_output_mb,
            elapsed_seconds=elapsed_seconds,
            space_saved_percent=space_saved_percent,
            result=result,
        )

        print("Benchmark")
        print()
        print(f"  ID:       {args.benchmark}")
        print(f"  Workers:  {workers}")
        print(f"  Files:    {len(videos)}")
        print(f"  Elapsed:  {elapsed_seconds:.1f} s")
        print(f"  Input:    {total_input_mb:.1f} MB")
        print(f"  Output:   {total_output_mb:.1f} MB")
        print(f"  Saved:    {space_saved_percent:.1f}%")
        print(f"  Result:   {result.upper()}")
        print()
        print(
            "Recorded:"
            f"  {BENCHMARK_CSV}"
        )

if __name__ == "__main__":
    main()