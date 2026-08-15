# cram experiments

This directory contains benchmark and tuning results for cram.

## Purpose

The goal is to compare encoding configurations and processing worker counts under repeatable conditions.

## Benchmark rules

When comparing worker counts:

1. Use the same input videos.
2. Use the same YAML encoding config.
3. Remove previous output files before each experiment.
4. Do not run other heavy workloads during the benchmark.
5. Record total wall-clock time, not the sum of individual FFmpeg runtimes.

## Current baseline

- Video codec: `h264_videotoolbox`
- Video bitrate: `1000k`
- FPS: `30`
- Audio codec: `aac`
- Audio bitrate: `96k`
- Resolution: original

## Planned worker benchmark

Test the same input set with:

- `workers = 1`
- `workers = 2`
- `workers = 3`
- `workers = 4`

Compare total elapsed time and stability.