from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from designtoolkit.chroma_keyer.models import KeyerParams
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples
from designtoolkit.chroma_keyer.video_export import key_video_to_png_sequence
from designtoolkit.chroma_keyer.video_models import VideoKeyOptions, VideoMetadata


class _SingleFrameDecoder:
    metadata = VideoMetadata(
        width=1,
        height=1,
        fps=1.0,
        duration_seconds=1.0,
        estimated_frame_count=1,
        codec="test",
    )

    def probe(self, _path: Path) -> VideoMetadata:
        return self.metadata

    def iter_frames(
        self,
        _path: Path,
    ) -> tuple[VideoMetadata, object]:
        frame = np.array([[[0, 255, 0]]], dtype=np.uint8)

        def frames():
            yield frame

        return self.metadata, frames()


def test_long_video_stem_uses_portable_output_and_temporary_names(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / f"{'a' * 220}.mp4"
    input_path.write_bytes(b"video")

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path, output_root=tmp_path),
        _SingleFrameDecoder(),  # type: ignore[arg-type]
        screen_model_from_samples([[0, 255, 0]]),
        KeyerParams(),
    )

    frame = next(
        path for path in result.output_dir.iterdir() if path.suffix == ".png"
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert len(result.output_dir.name.encode("utf-8")) <= 255
    assert len(frame.name.encode("utf-8")) <= 255
    assert len(manifest["pattern"].replace("%06d", "000001").encode("utf-8")) <= 255
    assert not list(tmp_path.glob(".*.tmp"))
