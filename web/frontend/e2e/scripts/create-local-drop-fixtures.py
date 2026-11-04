"""Create small real binary fixtures for the local HTTP drag/drop suite."""

from pathlib import Path
from subprocess import run

import imageio_ffmpeg
from PIL import Image, ImageDraw

workspace = Path(__file__).resolve().parents[4]
destination = workspace / ".temp" / "local-drop-fixtures"
destination.mkdir(parents=True, exist_ok=True)
image = Image.new("RGB", (64, 48), (0, 255, 0))
ImageDraw.Draw(image).rectangle((20, 12, 44, 36), fill=(255, 0, 0))
image.save(destination / "external-image.png")
run(
    [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-i",
        str(destination / "external-image.png"),
        "-t",
        "1",
        "-r",
        "12",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(destination / "external-video.mp4"),
    ],
    check=True,
)
