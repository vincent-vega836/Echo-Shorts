"""Turn a set of vertical Shorts into one horizontal long-form compilation.

Long videos earn far more per view than Shorts and count toward the 4,000
watch-hour YouTube Partner Program path, so a monthly compilation of the best
Shorts adds revenue at almost no cost. Each 9:16 clip is centred on a blurred,
zoomed copy of itself to fill a 1920x1080 frame, then all clips are joined.
"""

from __future__ import annotations

import os
import shutil
import subprocess

WIDTH, HEIGHT, FPS = 1920, 1080, 30
MIN_CLIPS = 2


class CompileError(RuntimeError):
    pass


def build_command(inputs: list[str], output: str, ffmpeg: str = "ffmpeg") -> list[str]:
    if len(inputs) < MIN_CLIPS:
        raise CompileError(f"a compilation needs at least {MIN_CLIPS} clips")
    filters = []
    for i in range(len(inputs)):
        filters.append(
            f"[{i}:v]split[bg{i}][fg{i}];"
            f"[bg{i}]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},boxblur=20:5[blur{i}];"
            f"[fg{i}]scale=-2:{HEIGHT}[front{i}];"
            f"[blur{i}][front{i}]overlay=(W-w)/2:0,setsar=1,fps={FPS}[v{i}];"
            f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo[a{i}]"
        )
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(inputs)))
    filters.append(f"{joined}concat=n={len(inputs)}:v=1:a=1[v][a]")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in inputs:
        command += ["-i", path]
    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output,
    ]
    return command


def compile_shorts(inputs: list[str], output: str, run=subprocess.run) -> str:
    missing = [path for path in inputs if not os.path.isfile(path)]
    if missing:
        raise CompileError(f"missing clip(s): {', '.join(missing)}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CompileError("ffmpeg is not installed")
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    partial = output + ".partial.mp4"
    completed = run(build_command(inputs, partial, ffmpeg), capture_output=True, text=True)
    if completed.returncode != 0:
        if os.path.exists(partial):
            os.remove(partial)
        raise CompileError(f"ffmpeg failed: {(completed.stderr or '').strip()[-500:]}")
    os.replace(partial, output)
    return os.path.abspath(output)
