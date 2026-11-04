"""Feed deterministic keyed pixels through the actual production packed exporter."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
import numpy as np

from designtoolkit.chroma_keyer.models import KeyerParams
from designtoolkit.chroma_keyer.packed_video_export import key_video_to_packed_mp4
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples
from designtoolkit.chroma_keyer.video_models import VideoKeyOptions, VideoMetadata


class Frames:
    def __init__(self):
        self.closed = False

    def __iter__(self):
        for index in range(40):
            rgb = np.full((33, 65, 3), 64 if index < 20 else 32, dtype=np.uint8)
            rgb[:16] = 192 if index < 20 else 96
            yield rgb

    def close(self):
        self.closed = True


class Decoder:
    def __init__(self):
        self.frames = Frames()
        self.metadata = VideoMetadata(65, 33, 20, 2, 40, "h264")

    def probe(self, path):
        return self.metadata

    def iter_frames(self, path):
        return self.metadata, self.frames


def main():
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "production-source.mp4"
    destination = directory / "production-packed.mp4"
    for path in (destination, destination.with_suffix(".json")):
        path.unlink(missing_ok=True)
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-y", "-f", "lavfi", "-i", "color=c=green:s=64x32:r=20:d=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
        timeout=30,
    )
    row = np.repeat(np.array([0, 128, 255], dtype=np.uint8), [20, 24, 21])
    alpha = np.tile(row, (33, 1))
    decoder = Decoder()
    # Isolate only keying; packing, encoding and metadata use production code.
    with patch("designtoolkit.chroma_keyer.packed_video_export.core.key_rgb",
               side_effect=lambda rgb, model, params: (rgb, alpha)):
        result = key_video_to_packed_mp4(
            VideoKeyOptions(
                source, output_format="packed_alpha_mp4", output_path=destination
            ),
            decoder, screen_model_from_samples(((0, 128, 0),)), KeyerParams(),
        )
    assert result.frame_count == 40 and decoder.frames.closed
    frames = imageio_ffmpeg.read_frames(str(result.output_path), pix_fmt="rgb24")
    try:
        info = next(frames)
    finally:
        frames.close()
    assert info["codec"] == "h264"
    assert info["pix_fmt"].split("(")[0] == "yuv420p"
    assert info["size"] == (132, 34)
    print(json.dumps({"mp4": str(result.output_path), "json": str(result.metadata_path),
                      "codec": info["codec"], "pix_fmt": info["pix_fmt"],
                      "size": info["size"], "fps": info["fps"]}))


if __name__ == "__main__":
    main()
