import importlib
import json
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from threading import enumerate as enumerate_threads

import imageio_ffmpeg
import numpy as np
import pytest

from designtoolkit.chroma_keyer import video_models
from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples
from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.chroma_keyer.video_models import VideoKeyOptions, VideoMetadata


def test_video_options_keep_legacy_defaults_and_accept_packed_destination():
    options = VideoKeyOptions(Path("input.mp4"))
    assert options.output_format == "png_sequence"
    assert options.output_path is None
    packed = VideoKeyOptions(
        Path("input.mp4"),
        output_format="packed_alpha_mp4",
        output_path=Path("packed.mp4"),
    )
    assert packed.output_path == Path("packed.mp4")


def test_packed_result_exposes_bundle_contract_and_validates_frame_count():
    metadata = VideoMetadata(5, 3, 24, 1, 24, "h264")
    result_type = video_models.PackedVideoKeyResult
    result = result_type(
        output_path=Path("out/packed.mp4"),
        metadata_path=Path("out/packed.json"),
        output_dir=Path("out"),
        frame_count=24,
        metadata=metadata,
    )
    assert result.output_path.name == "packed.mp4"
    assert result.metadata_path.name == "packed.json"
    assert result.output_dir == Path("out")
    assert result.metadata is metadata
    assert result.frame_count == 24
    with pytest.raises(KeyerError, match="frame_count"):
        result_type(Path("a.mp4"), Path("a.json"), Path("."), 0, metadata)


def exporter():
    return importlib.import_module("designtoolkit.chroma_keyer.packed_video_export")


def ffmpeg(*args):
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *map(str, args)],
        capture_output=True,
        check=True,
        timeout=15,
    )


def make_source(tmp_path, *, audio=None, vfr=False):
    path = tmp_path / "input.mp4"
    args = ["-f", "lavfi", "-i", "color=c=green:s=64x32:r=10:d=1"]
    if audio is not None:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={audio}",
        ]
    if vfr:
        args += [
            "-vf",
            "setpts=if(lt(N\\,5)\\,N\\,5+2*(N-5))/(10*TB)",
            "-fps_mode",
            "vfr",
        ]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path]
    ffmpeg(*args)
    return path


def export(path, decoder=None, **kwargs):
    return exporter().key_video_to_packed_mp4(
        VideoKeyOptions(path, output_format="packed_alpha_mp4"),
        decoder or ImageioVideoDecoder(),
        screen_model_from_samples(((0, 128, 0),)),
        KeyerParams(),
        **kwargs,
    )


class FrameStream:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.frames)

    def close(self):
        self.closed = True


class ArrayDecoder:
    def __init__(self, width=65, height=33, count=10):
        self.metadata = VideoMetadata(width, height, 10, 1, 10, "h264")
        self.stream = FrameStream(
            np.full((height, width, 3), 200, dtype=np.uint8) for _ in range(count)
        )

    def probe(self, path):
        return self.metadata

    def iter_frames(self, path):
        return self.metadata, self.stream


def test_real_encode_decode_odd_dimensions_and_alpha(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path)
    decoder = ArrayDecoder()
    alpha = np.repeat(np.array([0, 128, 255], dtype=np.uint8), [22, 22, 21])
    alpha = np.tile(alpha, (33, 1))
    monkeypatch.setattr(module.core, "key_rgb", lambda rgb, model, params: (rgb, alpha))
    progress = []
    result = export(
        path, decoder, progress_callback=lambda *args: progress.append(args)
    )
    assert result.output_path == tmp_path / "input_keyed_packed.mp4"
    assert result.metadata_path == result.output_path.with_suffix(".json")
    assert result.output_dir == tmp_path
    assert result.frame_count == 10
    assert decoder.stream.closed
    assert [entry[0] for entry in progress] == list(range(1, 11))
    assert all(entry[2] == result.output_path for entry in progress)
    data = json.loads(result.metadata_path.read_text())
    assert data["version"] == 1
    assert data["layout"] == "alpha_left_color_right"
    assert data["alpha_mode"] == "straight"
    assert (data["original_width"], data["original_height"]) == (65, 33)
    assert (data["stored_width"], data["stored_height"]) == (66, 34)
    assert data["duration_seconds"] == 1
    frames = imageio_ffmpeg.read_frames(result.output_path, pix_fmt="rgb24")
    meta = next(frames)
    decoded = [
        np.frombuffer(frame, dtype=np.uint8).reshape(34, 132, 3) for frame in frames
    ]
    assert meta["codec"] == "h264"
    assert meta["pix_fmt"].split("(")[0] == "yuv420p"
    assert meta["fps"] == 10
    assert len(decoded) == 10
    for frame in decoded:
        # Sample interiors; lossy ringing at alpha transitions is not pixel-exact.
        for x, expected in [(10, 0), (32, 128), (54, 255)]:
            assert abs(int(frame[16, x, 0]) - expected) <= 4
            assert np.ptp(frame[16, x].astype(int)) <= 2
        assert abs(int(frame[16, 76, 0]) - 200) <= 4
    encoded = result.output_path.read_bytes()
    assert encoded.index(b"moov") < encoded.index(b"mdat")
    assert not list(tmp_path.glob(".packed.*"))


@pytest.mark.parametrize("audio", [None, 0.2, 2.0])
def test_real_audio_normalized_padded_or_truncated_to_video(tmp_path, audio):
    result = export(make_source(tmp_path, audio=audio))
    info = ffmpeg("-i", result.output_path, "-f", "null", "-").stderr.decode()
    data = json.loads(result.metadata_path.read_text())
    assert result.frame_count == 10
    assert data["audio"]["present"] is (audio is not None)
    if audio is None:
        assert "Audio:" not in info
    else:
        assert "Audio: aac" in info
        pcm = ffmpeg(
            "-i",
            result.output_path,
            "-map",
            "0:a:0",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-f",
            "f32le",
            "-",
        ).stdout
        samples = np.frombuffer(pcm, dtype=np.float32)
        assert 48000 <= len(samples) <= 49152
        assert np.max(np.abs(samples[:5000])) > 0.05
        if audio == 0.2:
            assert np.max(np.abs(samples[24000:47000])) < 0.001


def test_vfr_is_rejected_before_keying_and_leaves_no_outputs(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path, vfr=True)

    def no_key(*args):
        pytest.fail("VFR must be rejected before keying")

    monkeypatch.setattr(module.core, "key_rgb", no_key)
    with pytest.raises(KeyerError, match="constant|variable|VFR"):
        export(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


@pytest.mark.parametrize("collision", ["mp4", "json"])
def test_output_collision_preserves_existing_files(tmp_path, collision):
    path = make_source(tmp_path)
    existing = tmp_path / f"input_keyed_packed.{collision}"
    existing.write_bytes(b"existing")
    with pytest.raises(KeyerError, match="already exists"):
        export(path)
    assert existing.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".packed.*"))


def test_zero_frames_closes_decoder_and_cleans_temp(tmp_path):
    path = make_source(tmp_path)
    decoder = ArrayDecoder(count=0)
    with pytest.raises(KeyerError, match="no frames"):
        export(path, decoder)
    assert decoder.stream.closed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_callback_failure_closes_decoder_and_removes_partial_encode(tmp_path):
    path = make_source(tmp_path)
    decoder = ArrayDecoder()

    def fail(*args):
        raise KeyerError("callback guard failed")

    with pytest.raises(KeyerError, match="callback guard failed"):
        export(path, decoder, progress_callback=fail)
    assert decoder.stream.closed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_output_guard_checks_while_frame_production_is_stalled(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path)
    decoder = ArrayDecoder()
    blocked = Event()
    released = Event()

    def slow_key(rgb, model, params):
        blocked.set()
        assert released.wait(3), "output guard was not checked while keying stalled"
        return rgb, np.zeros(rgb.shape[:2], dtype=np.uint8)

    def check():
        if blocked.is_set():
            released.set()
            raise KeyerError("periodic quota exceeded")

    monkeypatch.setattr(module.core, "key_rgb", slow_key)
    with pytest.raises(KeyerError, match="periodic quota exceeded"):
        export(path, decoder, output_check=check)
    assert decoder.stream.closed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_real_encoder_failure_drains_bounded_stderr_and_cleans(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path)
    original = subprocess.Popen
    processes = []

    def failing_encoder(command, **kwargs):
        if "rawvideo" in command:
            command = [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('x'*200000+'encoder exploded');"
                "sys.exit(7)",
            ]
        process = original(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", failing_encoder)
    with pytest.raises(KeyerError, match="encoder exploded") as exc:
        export(path, ArrayDecoder(width=256, height=128))
    assert len(str(exc.value)) < 70000
    assert all(p.poll() is not None for p in processes)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_output_guard_checks_during_final_mux(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path, audio=0.2)
    original = subprocess.Popen
    muxing = Event()
    processes = []

    def stalled_mux(command, **kwargs):
        if "-c:v" in command and command[command.index("-c:v") + 1] == "copy":
            muxing.set()
            command = [
                sys.executable,
                "-c",
                "import pathlib,sys,time;"
                "pathlib.Path(sys.argv[1]).write_bytes(b'mux');time.sleep(10)",
                command[-1],
            ]
        process = original(command, **kwargs)
        processes.append(process)
        return process

    def check():
        if muxing.is_set():
            raise KeyerError("mux quota exceeded")

    monkeypatch.setattr(module.subprocess, "Popen", stalled_mux)
    with pytest.raises(KeyerError, match="mux quota exceeded"):
        export(path, output_check=check)
    assert all(p.poll() is not None for p in processes)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_failure_after_partial_publication_rolls_back_only_owned_output(
    tmp_path, monkeypatch
):
    module = exporter()
    path = make_source(tmp_path)
    original = module.os.link

    def collide(source, destination):
        if str(destination).endswith(".json"):
            Path(destination).write_bytes(b"foreign metadata")
            raise FileExistsError("metadata collision")
        return original(source, destination)

    monkeypatch.setattr(module.os, "link", collide)
    with pytest.raises(KeyerError, match="already exists"):
        export(path)
    assert not (tmp_path / "input_keyed_packed.mp4").exists()
    assert (tmp_path / "input_keyed_packed.json").read_bytes() == b"foreign metadata"
    assert not list(tmp_path.glob(".packed.*"))


@pytest.mark.parametrize("destination", ["input.mp4", "out.mov", "missing/out.mp4"])
def test_invalid_destinations_are_rejected(tmp_path, destination):
    path = make_source(tmp_path)
    with pytest.raises(KeyerError, match="differ|exists|mp4|parent"):
        exporter().key_video_to_packed_mp4(
            VideoKeyOptions(path, output_path=tmp_path / destination),
            ImageioVideoDecoder(),
            screen_model_from_samples(((0, 128, 0),)),
            KeyerParams(),
        )
    assert not list(tmp_path.glob(".packed.*"))


def test_stage_callback_reports_finishing_audio_and_publication(tmp_path):
    path = make_source(tmp_path, audio=0.2)
    (tmp_path / "input_keyed_packed_player").mkdir()
    stages = []
    export(path, stage_callback=stages.append)
    assert stages == ["validating", "keying", "encoding", "audio", "publishing"]
    assert (tmp_path / "input_keyed_packed_player").is_dir()


@pytest.mark.parametrize(
    "stage", ["validating", "keying", "encoding", "audio", "publishing"]
)
def test_stage_callback_failure_never_publishes_success(tmp_path, stage):
    path = make_source(tmp_path, audio=0.2)

    def fail(current):
        if current == stage:
            raise KeyerError(f"{stage} stage failed")

    with pytest.raises(KeyerError, match=f"{stage} stage failed"):
        export(path, stage_callback=fail)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_oversized_packed_width_rejected_before_decoder_iteration(tmp_path):
    path = make_source(tmp_path)
    decoder = ArrayDecoder(width=4097, height=1, count=0)
    with pytest.raises(KeyerError, match="8192.*encoder limit"):
        export(path, decoder)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_corrupt_successful_encoder_output_is_not_published(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path)
    original = subprocess.Popen

    def corrupt_encoder(command, **kwargs):
        if "-crf" in command:
            command = [
                sys.executable,
                "-c",
                "import sys,pathlib;sys.stdin.buffer.read();"
                "pathlib.Path(sys.argv[1]).write_bytes(b'corrupt')",
                command[-1],
            ]
        return original(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", corrupt_encoder)
    with pytest.raises(KeyerError, match="FFmpeg|invalid|Invalid"):
        export(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


@pytest.mark.parametrize("replace_final", [False, True])
def test_cleanup_failure_rolls_back_owned_publication_preserving_replacements(
    tmp_path, monkeypatch, replace_final
):
    module = exporter()
    path = make_source(tmp_path)
    original = module._remove_directory
    calls = []
    output = tmp_path / "input_keyed_packed.mp4"

    def fail_cleanup(directory):
        calls.append(directory)
        if len(calls) == 1:
            # A partial cleanup may already have removed the temporary hardlinks.
            original(directory)
            if replace_final:
                foreign = tmp_path / "foreign.mp4"
                foreign.write_bytes(b"foreign video")
                foreign.replace(output)
            raise KeyerError("temporary cleanup failed")
        original(directory)

    monkeypatch.setattr(module, "_remove_directory", fail_cleanup)
    with pytest.raises(KeyerError, match="temporary cleanup failed"):
        export(path)
    if replace_final:
        assert output.read_bytes() == b"foreign video"
    else:
        assert not output.exists()
    assert not output.with_suffix(".json").exists()
    assert not list(tmp_path.glob(".packed.*"))


def test_pipe_cleanup_error_does_not_mask_primary_guard_failure(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path)
    original = subprocess.Popen

    class FailingClose:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            return self.stream.write(data)

        def close(self):
            self.stream.close()
            raise BrokenPipeError("pipe cleanup error")

    def failing_close(command, **kwargs):
        process = original(command, **kwargs)
        if "-crf" in command:
            process.stdin = FailingClose(process.stdin)
        return process

    def guard(*args):
        raise KeyerError("primary guard failure")

    monkeypatch.setattr(module.subprocess, "Popen", failing_close)
    with pytest.raises(KeyerError, match="primary guard failure"):
        export(path, ArrayDecoder(), progress_callback=guard)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_missing_avi_slot_is_rejected_before_keying(tmp_path, monkeypatch):
    path = tmp_path / "missing.avi"
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=green:s=64x32:r=10:d=1",
        "-vf", "select=not(eq(n\\,2))", "-fps_mode", "passthrough",
        "-c:v", "mpeg4", path,
    )
    info = ffmpeg(
        "-copyts", "-i", path, "-vf", "showinfo",
        "-fps_mode", "passthrough", "-f", "null", "-",
    ).stderr.decode()
    assert "pts:      3 pts_time:0.3" in info
    calls = []
    original = exporter().core.key_rgb

    def key(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(exporter().core, "key_rgb", key)
    with pytest.raises(KeyerError, match="constant|variable|VFR"):
        export(path)
    assert not calls
    assert sorted(p.name for p in tmp_path.iterdir()) == ["missing.avi"]


@pytest.mark.parametrize("count", [9, 11])
def test_decoded_count_must_match_preflight(tmp_path, count):
    path = make_source(tmp_path)
    decoder = ArrayDecoder(count=count)
    with pytest.raises(KeyerError, match="frame count.*preflight"):
        export(path, decoder)
    assert decoder.stream.closed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def run_stalled_export(path, decoder, check, cleanup, verify=None):
    finished = Event()
    quota_failed = Event()
    errors = []

    def checked():
        try:
            return check()
        except Exception:
            quota_failed.set()
            raise

    def run():
        try:
            export(path, decoder, output_check=checked)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = Thread(target=run, daemon=True)
    worker.start()
    try:
        assert quota_failed.wait(15), "stalled decoder did not trigger quota guard"
        assert finished.wait(2), "quota failure did not return promptly"
        assert len(errors) == 1
        assert isinstance(errors[0], KeyerError)
        assert "decode quota exceeded" in str(errors[0])
        if verify is not None:
            verify()
    finally:
        cleanup()
        worker.join(5)
    assert not worker.is_alive()


def test_blocking_injected_decode_is_closed_on_quota_failure(tmp_path):
    path = make_source(tmp_path)
    entered = Event()
    released = Event()

    class BlockingStream(FrameStream):
        def __next__(self):
            entered.set()
            released.wait(10)
            raise StopIteration

        def close(self):
            super().close()
            released.set()

    decoder = ArrayDecoder()
    decoder.stream = BlockingStream([])

    def check():
        if entered.is_set():
            raise KeyerError("decode quota exceeded")

    before = set(enumerate_threads())
    run_stalled_export(path, decoder, check, released.set)
    assert decoder.stream.closed
    assert not [
        thread for thread in enumerate_threads()
        if thread not in before and not thread.daemon
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


def test_stalled_production_decoder_is_killed_and_reaped(tmp_path, monkeypatch):
    module = exporter()
    path = make_source(tmp_path)
    ready = Event()
    stalled = Event()
    processes = []
    commands = []
    original_popen = subprocess.Popen
    original_preflight = module._preflight

    def preflight(*args):
        timing = original_preflight(*args)
        ready.set()
        return timing

    def popen(command, **kwargs):
        if (
            ready.is_set() and str(path) in command
            and kwargs.get("stdout") == subprocess.PIPE
        ):
            commands.append(command)
            command = [sys.executable, "-c", "import time;time.sleep(30)"]
            process = original_popen(command, **kwargs)
            processes.append(process)
            stalled.set()
            return process
        return original_popen(command, **kwargs)

    def check():
        if stalled.is_set():
            raise KeyerError("decode quota exceeded")

    def cleanup():
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def verify():
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed
        assert processes[0].stderr.closed

    monkeypatch.setattr(module, "_preflight", preflight)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    run_stalled_export(path, ImageioVideoDecoder(), check, cleanup, verify)
    assert "-fps_mode" in commands[0]
    assert commands[0][commands[0].index("-fps_mode") + 1] == "passthrough"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]


@pytest.mark.parametrize("origin", [0, -1000, 2**54])
def test_fractional_cfr_timestamp_quantization_is_exact(origin):
    timing = exporter()._SourceTiming(
        VideoMetadata(64, 32, 30000 / 1001, 1, 30, "h264")
    )
    timing.observe(
        "showinfo config in time_base: 1/1000, frame_rate: 30000/1001"
    )
    for index in range(60):
        # Round the rational 1001/30 tick interval without float conversion.
        pts = origin + (index * 1001 + 15) // 30
        timing.observe(f"showinfo n: {index} pts: {pts} pts_time:unused")
    assert timing.count == 60


def test_uncooperative_injected_generator_does_not_hold_quota_failure(tmp_path):
    path = make_source(tmp_path)
    entered = Event()
    released = Event()
    closed = Event()

    def frames():
        try:
            entered.set()
            released.wait(10)
            yield np.zeros((33, 65, 3), dtype=np.uint8)
        finally:
            closed.set()

    decoder = ArrayDecoder()
    decoder.stream = frames()

    def check():
        if entered.is_set():
            raise KeyerError("decode quota exceeded")

    def verify():
        assert not [
            thread for thread in enumerate_threads()
            if thread.name.startswith("packed-") and not thread.daemon
        ]

    run_stalled_export(path, decoder, check, released.set, verify)
    assert closed.wait(2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.mp4"]
