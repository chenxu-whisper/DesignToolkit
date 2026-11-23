from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from math import isclose
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from uuid import uuid4

import numpy as np

from designtoolkit.output_names import bounded_derived_name
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

from . import core
from .image_io import _resolved_path
from .models import KeyerError, KeyerParams, ScreenColorModel
from .packed_alpha import pack_alpha_frame
from .source_alpha import split_source_rgba
from .video_export import _remove_directory, _validate_screen_color_model
from .video_io import ImageioVideoDecoder, _validate_input_path
from .video_models import (
    PackedVideoKeyResult,
    ProgressCallback,
    VideoDecoder,
    VideoKeyOptions,
    VideoMetadata,
)

_POLL_SECONDS = 0.1
_STDERR_LIMIT = 64 * 1024
_MAX_PACKED_DIMENSION = 8192


class _OutputMonitor:
    """Check output budgets even while the caller is blocked on a pipe or decode."""

    def __init__(self, callback: Callable[[], object] | None) -> None:
        self.callback = callback
        self.error: Exception | None = None
        self.stop_event = Event()
        self.lock = Lock()
        self.processes: set[subprocess.Popen] = set()
        self.thread = Thread(
            target=self._watch, name="packed-output-guard", daemon=True
        )

    def __enter__(self):
        self.check()
        self.thread.start()
        return self

    def check(self) -> None:
        with self.lock:
            if self.error is None and self.callback is not None:
                try:
                    self.callback()
                except Exception as error:
                    self.error = error
            if self.error is not None:
                raise self.error

    def raise_if_failed(self) -> None:
        with self.lock:
            if self.error is not None:
                raise self.error

    def register(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.processes.add(process)
            if self.error is not None and process.poll() is None:
                process.kill()

    def unregister(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.processes.discard(process)

    def _watch(self) -> None:
        while not self.stop_event.wait(_POLL_SECONDS):
            try:
                self.check()
            except Exception:
                with self.lock:
                    for process in self.processes:
                        if process.poll() is None:
                            try:
                                process.kill()
                            except OSError:
                                pass
                return

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join()


class _FfmpegProcess:
    def __init__(
        self,
        command: list[str],
        monitor: _OutputMonitor,
        *,
        feed: bool = False,
        read: bool = False,
        observe_line: Callable[[str], None] | None = None,
    ) -> None:
        self.monitor = monitor
        self.tail = bytearray()
        self.reader_error: Exception | None = None
        self.observe_line = observe_line
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
            stdout=subprocess.PIPE if read else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.reader = Thread(
            target=self._drain, name="packed-ffmpeg-stderr", daemon=True
        )
        self.reader.start()
        self.monitor.register(self.process)

    def _drain(self) -> None:
        pending = bytearray()
        try:
            while chunk := self.process.stderr.read(4096):
                self.tail.extend(chunk)
                del self.tail[:-_STDERR_LIMIT]
                if self.observe_line is not None:
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        self.observe_line(line.decode("utf-8", errors="replace"))
                    del pending[:-_STDERR_LIMIT]
            if pending and self.observe_line is not None:
                self.observe_line(pending.decode("utf-8", errors="replace"))
        except Exception as error:
            self.reader_error = error
            if self.process.poll() is None:
                self.process.kill()

    def __enter__(self):
        return self

    def wait(self) -> None:
        while self.process.poll() is None:
            self.monitor.raise_if_failed()
            self.monitor.stop_event.wait(_POLL_SECONDS)
        self.reader.join()
        self.monitor.raise_if_failed()
        if self.reader_error is not None:
            raise self.reader_error
        if self.process.returncode != 0:
            raise KeyerError(
                "FFmpeg packed export failed: "
                + self.tail.decode("utf-8", errors="replace")
            )

    def write(self, data: bytes) -> None:
        self.monitor.raise_if_failed()
        try:
            view = memoryview(data)
            while view:
                written = self.process.stdin.write(view)
                if not written:
                    raise BrokenPipeError("encoder pipe closed")
                view = view[written:]
                self.monitor.raise_if_failed()
        except (BrokenPipeError, OSError) as error:
            self.wait()
            raise KeyerError(
                "FFmpeg encoder pipe closed before all frames were fed"
            ) from error

    def finish(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError as error:
                self.wait()
                raise KeyerError("FFmpeg encoder pipe could not be closed") from error
        self.wait()

    def __exit__(self, error_type, primary_error, traceback):
        cleanup_errors: list[Exception] = []
        try:
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError as error:
                    cleanup_errors.append(error)
            try:
                self.process.wait(timeout=5)
            except Exception as error:
                cleanup_errors.append(error)
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except OSError as error:
                    cleanup_errors.append(error)
            self.reader.join(timeout=5)
            if self.process.stdout is not None:
                try:
                    self.process.stdout.close()
                except OSError as error:
                    cleanup_errors.append(error)
            try:
                self.process.stderr.close()
            except OSError as error:
                cleanup_errors.append(error)
        finally:
            self.monitor.unregister(self.process)
        if primary_error is not None:
            for error in cleanup_errors:
                primary_error.add_note(f"FFmpeg cleanup failed: {error}")
        elif cleanup_errors:
            self.monitor.raise_if_failed()
            raise KeyerError(f"FFmpeg cleanup failed: {cleanup_errors[0]}")


class _RawFrameStream:
    """Packed-only passthrough decode, with an explicitly owned child process."""

    def __init__(
        self, executable, path, metadata, monitor, *, source_alpha=False
    ) -> None:
        self.metadata = metadata
        self.channels = 4 if source_alpha else 3
        self.frame_bytes = metadata.width * metadata.height * self.channels
        self.closed = False
        self.ffmpeg = _FfmpegProcess(
            [
                executable, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
                "-fps_mode", "passthrough", "-pix_fmt",
                "rgba" if source_alpha else "rgb24",
                "-f", "rawvideo", "pipe:1",
            ],
            monitor,
            read=True,
        )

    def __iter__(self):
        return self

    def __next__(self):
        data = bytearray()
        while len(data) < self.frame_bytes:
            chunk = self.ffmpeg.process.stdout.read(self.frame_bytes - len(data))
            if not chunk:
                self.ffmpeg.wait()
                if data:
                    raise KeyerError("Decoded video frame has an invalid byte length")
                raise StopIteration
            data.extend(chunk)
        return np.frombuffer(bytes(data), dtype=np.uint8).reshape(
            self.metadata.height, self.metadata.width, self.channels
        )

    def cancel(self):
        process = self.ffmpeg.process
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                if process.poll() is None:
                    raise
        process.wait(timeout=5)

    def close(self):
        if not self.closed:
            self.closed = True
            self.ffmpeg.__exit__(*sys.exc_info())


class _FrameProducer:
    """Bound memory and keep the consumer responsive to output-guard failures."""

    def __init__(self, factory, monitor) -> None:
        self.factory = factory
        self.monitor = monitor
        self.queue = Queue(maxsize=2)
        self.stop = Event()
        self.stream = None
        self.close_lock = Lock()
        self.closed = False
        self.cleanup_error = None
        self.thread = Thread(
            target=self._produce, name="packed-decode-producer", daemon=True
        )

    def _put(self, kind, value=None):
        while not self.stop.is_set():
            try:
                self.queue.put((kind, value), timeout=_POLL_SECONDS)
                return True
            except Full:
                pass
        return False

    def _close(self):
        with self.close_lock:
            if self.stream is not None and not self.closed:
                self.stream.close()
                self.closed = True

    def _produce(self):
        try:
            try:
                metadata, self.stream = self.factory()
                if self._put("metadata", metadata):
                    for frame in self.stream:
                        if not self._put("frame", frame):
                            break
            finally:
                self._close()
            self._put("done")
        except Exception as error:
            self._put("error", error)

    def _receive(self):
        while True:
            self.monitor.raise_if_failed()
            try:
                kind, value = self.queue.get(timeout=_POLL_SECONDS)
            except Empty:
                continue
            self.monitor.raise_if_failed()
            if kind == "error":
                raise value
            return kind, value

    def __enter__(self):
        self.thread.start()
        return self

    def metadata(self):
        kind, value = self._receive()
        if kind != "metadata":
            raise KeyerError("Video stream metadata is missing")
        return value

    def __iter__(self):
        return self

    def __next__(self):
        kind, value = self._receive()
        if kind == "done":
            raise StopIteration
        return value

    def __exit__(self, error_type, primary_error, traceback):
        self.stop.set()
        if self.thread.is_alive():
            if isinstance(self.stream, _RawFrameStream):
                # Killing/reaping the owned child unblocks its pipe reader;
                # the producer remains responsible for closing the handles.
                try:
                    self.stream.cancel()
                except Exception as error:
                    self.cleanup_error = error
            else:
                # Injected close/cancel implementations may themselves block.
                # Never strand a non-daemon thread or delay quota failure.
                def cancel():
                    try:
                        cancel_stream = getattr(self.stream, "cancel", None)
                        if callable(cancel_stream):
                            cancel_stream()
                        else:
                            self._close()
                    except ValueError:
                        # An executing generator must close on its owning thread.
                        pass
                    except Exception as error:
                        self.cleanup_error = error

                closer = Thread(
                    target=cancel, name="packed-decode-cancel", daemon=True
                )
                closer.start()
                closer.join(timeout=0.25)
        self.thread.join(timeout=0.5)
        if self.cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(f"Decoder cleanup failed: {self.cleanup_error}")
            else:
                raise self.cleanup_error


class _SourceTiming:
    def __init__(self, metadata: VideoMetadata) -> None:
        self.rate = Fraction(str(metadata.fps))
        self.time_base: Fraction | None = None
        self.first_pts: int | None = None
        self.phase_min = Fraction(0)
        self.phase_max = Fraction(0)
        self.count = 0
        self.audio_present = False

    def observe(self, line: str) -> None:
        if re.search(r"Stream #0:\d+.*Audio:", line):
            self.audio_present = True
        if "showinfo" not in line:
            return
        config = re.search(
            r"config in time_base:\s*(\d+)/(\d+), frame_rate:\s*(\d+)/(\d+)",
            line,
        )
        if config:
            num, den, rate_num, rate_den = map(int, config.groups())
            if den == 0 or rate_num == 0 or rate_den == 0:
                raise KeyerError("Cannot verify constant frame rate timestamps")
            self.time_base = Fraction(num, den)
            reported_rate = Fraction(rate_num, rate_den)
            if not isclose(float(self.rate), float(reported_rate), rel_tol=0.001):
                raise KeyerError(
                    "Variable frame rate input: convert to constant frame rate"
                )
            self.rate = reported_rate
        match = re.search(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:", line)
        if match:
            index, pts = map(int, match.groups())
            if self.time_base is None or index != self.count:
                raise KeyerError("Cannot verify constant frame rate timestamps")
            if self.first_pts is None:
                self.first_pts = pts
            ticks_per_frame = 1 / (self.rate * self.time_base)
            # Quantized CFR timestamps must share a single rounding phase.
            # Their phase residuals fit strictly inside one tick, never a
            # whole missing slot. Fractions also preserve coarse AVI clocks.
            residual = pts - self.first_pts - self.count * ticks_per_frame
            self.phase_min = min(self.phase_min, residual)
            self.phase_max = max(self.phase_max, residual)
            if ticks_per_frame < 1 or self.phase_max - self.phase_min >= 1:
                raise KeyerError(
                    "Variable frame rate (VFR): convert to constant frame rate"
                )
            self.count += 1


def _preflight(
    executable: str, path: Path, metadata: VideoMetadata, monitor: _OutputMonitor,
    *, source_alpha: bool = False,
) -> _SourceTiming:
    timing = _SourceTiming(metadata)
    command = [
        executable,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-copyts",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "showinfo,alphaextract" if source_alpha else "showinfo",
        "-fps_mode",
        "passthrough",
        "-f",
        "null",
        "-",
    ]
    try:
        with _FfmpegProcess(command, monitor, observe_line=timing.observe) as process:
            process.wait()
    except KeyerError as error:
        if source_alpha and "Requested planes not available" in str(error):
            raise KeyerError("Source video has no native alpha channel") from error
        raise
    if timing.count == 0:
        raise KeyerError("Source contains no frames with verifiable timestamps")
    return timing


def _destination(options: VideoKeyOptions) -> Path:
    path = options.output_path or (
        (options.output_root or options.input_path.parent)
        / bounded_derived_name(
            prefix="",
            stem=options.input_path.stem,
            suffix="_keyed_packed",
            extension=".mp4",
        )
    )
    if path.suffix.lower() != ".mp4":
        raise KeyerError("Output path must have a .mp4 suffix")
    if not path.parent.is_dir():
        raise KeyerError("Output parent directory must exist")
    if _resolved_path(path) == _resolved_path(options.input_path):
        raise KeyerError("Output path must differ from input path")
    for candidate in (path, path.with_suffix(".json")):
        if os.path.lexists(candidate):
            raise KeyerError(f"Output path already exists: {candidate}")
    return path


def _packed_metadata(
    metadata: VideoMetadata, count: int, audio_present: bool
) -> dict[str, object]:
    return {
        "version": 1,
        "format": "packed_alpha_mp4",
        "layout": "alpha_left_color_right",
        "original_width": metadata.width,
        "original_height": metadata.height,
        "stored_width": metadata.width + metadata.width % 2,
        "stored_height": metadata.height + metadata.height % 2,
        "fps": metadata.fps,
        "frame_count": count,
        "duration_seconds": count / metadata.fps,
        "alpha_mode": "straight",
        "alpha_encoding": "grayscale_8bit",
        "audio": {
            "present": audio_present,
            "codec": "aac" if audio_present else None,
            "source_track": 0 if audio_present else None,
            "time_origin_seconds": 0,
        },
    }


def key_video_to_packed_mp4(
    options: VideoKeyOptions,
    decoder: VideoDecoder,
    model: ScreenColorModel | None,
    params: KeyerParams,
    progress_callback: ProgressCallback | None = None,
    *,
    output_check: Callable[[], object] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PackedVideoKeyResult:
    """Stream keyed RGB/Alpha into H.264 MP4 with no-overwrite publication."""
    temporary_dir: Path | None = None
    published: list[tuple[Path, int, int]] = []
    success = False
    try:
        source_alpha = options.alpha_source == "source"
        if not source_alpha:
            if model is None:
                raise KeyerError("Chroma key export requires a ScreenColorModel")
            _validate_screen_color_model(model)
        _validate_input_path(options.input_path)
        destination = _destination(options)
        metadata = decoder.probe(options.input_path)
        stored_width = metadata.width + metadata.width % 2
        stored_height = metadata.height + metadata.height % 2
        if max(stored_width * 2, stored_height) > _MAX_PACKED_DIMENSION:
            raise KeyerError(
                "Packed video dimensions exceed the 8192 pixel encoder limit"
            )
        try:
            executable = FfmpegBackend().get_ffmpeg_exe()
        except Exception as error:
            raise KeyerError("Bundled FFmpeg encoder is unavailable") from error
        with _OutputMonitor(output_check) as monitor:
            if stage_callback is not None:
                stage_callback("validating")
            timing = (
                _preflight(
                    executable, options.input_path, metadata, monitor,
                    source_alpha=True,
                )
                if source_alpha
                else _preflight(executable, options.input_path, metadata, monitor)
            )
            metadata = replace(metadata, fps=float(timing.rate))
            temporary_dir = destination.parent / f".packed.{uuid4().hex}.tmp"
            temporary_dir.mkdir()
            video_path = temporary_dir / "video.mp4"
            mux_path = temporary_dir / "packed.mp4"
            json_path = temporary_dir / "metadata.json"
            def open_stream():
                if source_alpha or type(decoder) is ImageioVideoDecoder:
                    return metadata, _RawFrameStream(
                        executable, options.input_path, metadata, monitor,
                        source_alpha=source_alpha,
                    )
                return decoder.iter_frames(options.input_path)

            count = 0
            with _FrameProducer(open_stream, monitor) as stream:
                stream_metadata = stream.metadata()
                if (stream_metadata.width, stream_metadata.height) != (
                    metadata.width,
                    metadata.height,
                ):
                    raise KeyerError("Video stream dimensions changed")
                command = [
                    executable,
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-n",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-s:v",
                    f"{2 * stored_width}x{stored_height}",
                    "-r",
                    str(timing.rate),
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "18",
                    "-preset",
                    "medium",
                    "-movflags",
                    "+faststart",
                    str(video_path),
                ]
                if stage_callback is not None:
                    stage_callback("keying")
                with _FfmpegProcess(command, monitor, feed=True) as process:
                    for frame in stream:
                        monitor.raise_if_failed()
                        if count >= timing.count:
                            raise KeyerError(
                                "Decoded video frame count does not match preflight"
                            )
                        channels = 4 if source_alpha else 3
                        if frame.shape != (metadata.height, metadata.width, channels):
                            raise KeyerError("Decoded video frame dimensions changed")
                        if source_alpha:
                            rgb, alpha = split_source_rgba(
                                frame, options.source_alpha_mode
                            )
                        else:
                            rgb, alpha = core.key_rgb(frame, model, params)
                        packed = pack_alpha_frame(rgb, alpha)
                        process.write(packed.tobytes())
                        count += 1
                        if progress_callback is not None:
                            progress_callback(
                                count, metadata.estimated_frame_count, destination
                            )
                        del frame, rgb, alpha, packed
                    if count == 0:
                        raise KeyerError("Decoded video contains no frames")
                    if count != timing.count:
                        raise KeyerError(
                            "Decoded video frame count does not match preflight"
                        )
                    if stage_callback is not None:
                        stage_callback("encoding")
                    process.finish()
            monitor.check()
            if timing.audio_present:
                if stage_callback is not None:
                    stage_callback("audio")
                duration = count / metadata.fps
                command = [
                    executable,
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-n",
                    "-i",
                    str(video_path),
                    "-i",
                    str(options.input_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-map_metadata",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-af",
                    f"asetpts=PTS-STARTPTS,apad,atrim=duration={duration:.12f}",
                    "-t",
                    f"{duration:.12f}",
                    "-movflags",
                    "+faststart",
                    str(mux_path),
                ]
                with _FfmpegProcess(command, monitor) as process:
                    process.wait()
                completed_video = mux_path
            else:
                completed_video = video_path
            if not completed_video.is_file() or completed_video.stat().st_size == 0:
                raise KeyerError("FFmpeg produced no MP4 output")
            result_metadata = replace(
                metadata,
                duration_seconds=count / metadata.fps,
                estimated_frame_count=count,
            )
            verified = _preflight(
                executable,
                completed_video,
                replace(result_metadata, width=2 * stored_width, height=stored_height),
                monitor,
            )
            if (
                verified.count != count
                or verified.audio_present != timing.audio_present
            ):
                raise KeyerError(
                    "FFmpeg output frame count or audio track validation failed"
                )
            json_path.write_text(
                json.dumps(
                    {
                        **_packed_metadata(
                            result_metadata, count, timing.audio_present
                        ),
                        "alpha_source": options.alpha_source,
                        "source_alpha_mode": options.source_alpha_mode,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if stage_callback is not None:
                stage_callback("publishing")
            monitor.check()
            for source, target in (
                (completed_video, destination),
                (json_path, destination.with_suffix(".json")),
            ):
                identity = source.stat()
                os.link(source, target)
                published.append((target, identity.st_dev, identity.st_ino))
                monitor.check()
            result = PackedVideoKeyResult(
                destination,
                destination.with_suffix(".json"),
                destination.parent,
                count,
                result_metadata,
            )
        monitor.raise_if_failed()
        _remove_directory(temporary_dir)
        temporary_dir = None
        success = True
        return result
    except FileExistsError as error:
        raise KeyerError("Output path already exists") from error
    except KeyerError:
        raise
    except Exception as error:
        raise KeyerError(f"Packed MP4 export failed: {error}") from error
    finally:
        primary_error = sys.exception()
        if not success:
            for target, device, inode in reversed(published):
                try:
                    identity = target.lstat()
                    if (identity.st_dev, identity.st_ino) == (device, inode):
                        target.unlink()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    if primary_error is not None:
                        primary_error.add_note(
                            f"Published output rollback failed: {error}"
                        )
        if temporary_dir is not None:
            try:
                _remove_directory(temporary_dir)
            except Exception as error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"Temporary output cleanup failed: {error}")
