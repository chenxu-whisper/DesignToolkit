import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import pytest

from designtoolkit.chroma_keyer import packed_video_export
from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams
from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.chroma_keyer.video_models import VideoKeyOptions


def rgba_frame(width=32, height=16, opaque=False):
    frame = np.empty((height, width, 4), np.uint8)
    frame[..., :3] = (20, 220, 40)
    frame[..., 3] = 255
    if not opaque:
        frame[..., 3] = np.concatenate(
            [
                np.full(8, 0),
                np.full(8, 128),
                np.full(8, 255),
                np.linspace(0, 255, width - 24),
            ]
        ).astype(np.uint8)
    return frame


def make_native(
    path: Path,
    codec="qtrle",
    *,
    opaque=False,
    audio=False,
    vfr=False,
    width=32,
    height=16,
    premultiplied=False,
):
    frame = rgba_frame(width, height, opaque)
    if premultiplied:
        frame[..., :3] = np.rint(
            frame[..., :3].astype(float) * frame[..., 3, None] / 255
        ).astype(np.uint8)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{width}x{height}",
        "-r",
        "10",
        "-i",
        "pipe:0",
    ]
    if audio:
        command += [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
        ]
    if vfr:
        command += [
            "-vf",
            "setpts=if(lt(N\\,5)\\,N\\,5+2*(N-5))/(10*TB)",
            "-fps_mode",
            "vfr",
        ]
    command += ["-c:v", codec]
    if codec == "prores_ks":
        command += ["-profile:v", "4", "-pix_fmt", "yuva444p10le"]
    elif codec == "qtrle":
        command += ["-pix_fmt", "argb"]
    else:
        command += ["-pix_fmt", "yuv420p"]
    command += ["-c:a", "pcm_s16le", str(path)]
    subprocess.run(
        command, input=frame.tobytes() * 10, capture_output=True, check=True, timeout=15
    )
    return path, frame


def export_source(path, *, mode="straight", **kwargs):
    return packed_video_export.key_video_to_packed_mp4(
        VideoKeyOptions(
            path,
            output_format="packed_alpha_mp4",
            alpha_source="source",
            source_alpha_mode=mode,
        ),
        ImageioVideoDecoder(),
        None,
        KeyerParams(),
        **kwargs,
    )


@pytest.mark.parametrize("codec", ["qtrle", "prores_ks"])
def test_native_mov_exports_original_mask_green_and_first_aac_audio(
    tmp_path, monkeypatch, codec
):
    path, frame = make_native(tmp_path / "native.mov", codec, audio=True)
    monkeypatch.setattr(
        packed_video_export.core,
        "key_rgb",
        lambda *a: pytest.fail("source must never key/despill"),
    )
    result = export_source(path)
    assert result.frame_count == 10
    meta = json.loads(result.metadata_path.read_text())
    assert meta["alpha_source"] == "source"
    assert meta["source_alpha_mode"] == "straight"
    assert meta["alpha_mode"] == "straight"
    assert meta["layout"] == "alpha_left_color_right"
    assert meta["audio"]["codec"] == "aac"
    frames = imageio_ffmpeg.read_frames(result.output_path, pix_fmt="rgb24")
    next(frames)
    decoded = [np.frombuffer(raw, np.uint8).reshape(16, 64, 3) for raw in frames]
    assert len(decoded) == 10
    for packed in decoded:
        np.testing.assert_allclose(
            packed[8, :32], np.repeat(frame[8, :, 3, None], 3, axis=1), atol=6
        )
        np.testing.assert_allclose(packed[8, 36, :], [20, 220, 40], atol=6)
    audio = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-i",
            str(result.output_path),
            "-map",
            "0:a:0",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    samples = np.frombuffer(audio.stdout, np.float32)
    assert 48000 <= len(samples) <= 49152
    assert np.max(np.abs(samples[:5000])) > 0.05
    assert not list(tmp_path.glob(".packed.*"))


def test_all_opaque_native_alpha_is_accepted(tmp_path):
    path, _ = make_native(tmp_path / "opaque.mov", opaque=True)
    assert export_source(path).frame_count == 10


def test_rgb_h264_mov_fails_explicitly_without_artifacts(tmp_path):
    path, _ = make_native(tmp_path / "rgb.mov", "libx264")
    with pytest.raises(KeyerError, match="native.*[Aa]lpha"):
        export_source(path)
    assert [p.name for p in tmp_path.iterdir()] == ["rgb.mov"]


def test_source_vfr_is_rejected_without_artifacts(tmp_path):
    path, _ = make_native(tmp_path / "vfr.mov", vfr=True)
    with pytest.raises(KeyerError, match="constant|variable|VFR"):
        export_source(path)
    assert [p.name for p in tmp_path.iterdir()] == ["vfr.mov"]


@pytest.mark.parametrize("collision", ["mp4", "json"])
def test_source_does_not_overwrite(tmp_path, collision):
    path, _ = make_native(tmp_path / "native.mov")
    existing = tmp_path / f"native_keyed_packed.{collision}"
    existing.write_bytes(b"foreign")
    with pytest.raises(KeyerError, match="exists"):
        export_source(path)
    assert existing.read_bytes() == b"foreign"
    assert not list(tmp_path.glob(".packed.*"))


def test_source_encoder_failure_reaps_processes_and_cleans_output(
    tmp_path, monkeypatch
):
    path, _ = make_native(tmp_path / "native.mov")
    original = subprocess.Popen
    processes = []

    def fail_encoder(command, **kwargs):
        if "-crf" in command:
            command = [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('x'*200000+'source encoder failed');"
                "sys.exit(7)",
            ]
        process = original(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(packed_video_export.subprocess, "Popen", fail_encoder)
    with pytest.raises(KeyerError, match="source encoder failed") as error:
        export_source(path)
    assert len(str(error.value)) < 70000
    assert all(p.poll() is not None for p in processes)
    assert all(p.stderr.closed for p in processes)
    assert [p.name for p in tmp_path.iterdir()] == ["native.mov"]


def test_source_odd_dimensions_are_padded(tmp_path):
    path, _ = make_native(tmp_path / "odd.mov", width=33, height=17)
    result = export_source(path)
    metadata = json.loads(result.metadata_path.read_text())
    assert (metadata["original_width"], metadata["original_height"]) == (33, 17)
    assert (metadata["stored_width"], metadata["stored_height"]) == (34, 18)


def test_video_options_keep_chroma_defaults():
    options = VideoKeyOptions(Path("native.mov"))
    assert options.alpha_source == "chroma_key"
    assert options.source_alpha_mode == "straight"


@pytest.mark.parametrize(
    "fields",
    [
        {"alpha_source": "guess"},
        {
            "alpha_source": "source",
            "source_alpha_mode": "guess",
            "output_format": "packed_alpha_mp4",
        },
        {"alpha_source": "source"},
    ],
)
def test_invalid_source_options_are_rejected(fields):
    with pytest.raises(KeyerError, match="alpha_source|mode|packed"):
        VideoKeyOptions(Path("native.mov"), **fields)


def test_real_premultiplied_mov_restores_color_and_preserves_mask(tmp_path):
    path, frame = make_native(tmp_path / "premult.mov", premultiplied=True)
    result = export_source(path, mode="premultiplied")
    metadata = json.loads(result.metadata_path.read_text())
    assert metadata["source_alpha_mode"] == "premultiplied"
    frames = imageio_ffmpeg.read_frames(result.output_path, pix_fmt="rgb24")
    next(frames)
    for raw in frames:
        packed = np.frombuffer(raw, np.uint8).reshape(16, 64, 3)
        np.testing.assert_allclose(packed[8, 44], [20, 220, 40], atol=6)
        np.testing.assert_allclose(packed[8, 36], [0, 0, 0], atol=6)
        np.testing.assert_allclose(
            packed[8, :32], np.repeat(frame[8, :, 3, None], 3, axis=1), atol=6
        )


def test_chroma_export_still_rejects_none_model(tmp_path):
    path, _ = make_native(tmp_path / "native.mov")
    with pytest.raises(KeyerError, match="ScreenColorModel"):
        packed_video_export.key_video_to_packed_mp4(
            VideoKeyOptions(path, output_format="packed_alpha_mp4"),
            ImageioVideoDecoder(),
            None,
            KeyerParams(),
        )
    assert [p.name for p in tmp_path.iterdir()] == ["native.mov"]
