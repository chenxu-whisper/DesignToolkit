from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from designtoolkit.web import native_picker
from designtoolkit.web.app import create_app
from designtoolkit.web.schemas import LocalFilePickRequest

CHROMA_ALLOWED_SUFFIXES = [
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".png",
    ".jpg",
    ".jpeg",
]


def test_image_picker_filters_to_supported_extensions_case_insensitively(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.PNG"
    video_path = tmp_path / "clip.mp4"
    options: dict[str, object] = {}

    class FakeRoot:
        def destroy(self) -> None:
            pass

    class FakeFileDialog:
        def askopenfilenames(self, **kwargs: object) -> tuple[str, str]:
            options.update(kwargs)
            return str(image_path), str(video_path)

    monkeypatch.setattr(
        native_picker,
        "_create_tk_dialog_context",
        lambda: (FakeRoot(), FakeFileDialog()),
    )

    paths = native_picker.TkinterLocalPathPicker().pick_files(
        title="Select image",
        multiple=True,
        file_kind="image",
    )

    assert options["filetypes"] == [("Image files", "*.png *.jpg *.jpeg")]
    assert paths == [image_path]


def test_constrained_media_picker_uses_allowlist_for_filters_and_returned_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "clip.MP4"
    image_path = tmp_path / "screen.JPEG"
    unsupported_paths = [
        tmp_path / "animation.gif",
        tmp_path / "bitmap.BMP",
        tmp_path / "photo.webp",
        tmp_path / "typed.txt",
    ]
    options: dict[str, object] = {}

    class FakeRoot:
        def destroy(self) -> None:
            pass

    class FakeFileDialog:
        def askopenfilenames(self, **kwargs: object) -> tuple[str, ...]:
            options.update(kwargs)
            return tuple(
                str(path) for path in [video_path, image_path, *unsupported_paths]
            )

    monkeypatch.setattr(
        native_picker,
        "_create_tk_dialog_context",
        lambda: (FakeRoot(), FakeFileDialog()),
    )

    paths = native_picker.TkinterLocalPathPicker().pick_files(
        title="Select supported media",
        multiple=True,
        file_kind="media",
        allowed_suffixes=CHROMA_ALLOWED_SUFFIXES,
    )

    assert options["filetypes"] == [
        (
            "Media files",
            "*.mp4 *.webm *.mov *.mkv *.avi *.m4v *.png *.jpg *.jpeg",
        ),
        ("Video files", "*.mp4 *.webm *.mov *.mkv *.avi *.m4v"),
        ("Image files", "*.png *.jpg *.jpeg"),
    ]
    assert paths == [video_path, image_path]


def test_unconstrained_media_picker_keeps_broad_filters_and_returned_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    webp_path = tmp_path / "photo.webp"
    typed_path = tmp_path / "typed.txt"
    options: dict[str, object] = {}

    class FakeRoot:
        def destroy(self) -> None:
            pass

    class FakeFileDialog:
        def askopenfilenames(self, **kwargs: object) -> tuple[str, str]:
            options.update(kwargs)
            return str(webp_path), str(typed_path)

    monkeypatch.setattr(
        native_picker,
        "_create_tk_dialog_context",
        lambda: (FakeRoot(), FakeFileDialog()),
    )

    paths = native_picker.TkinterLocalPathPicker().pick_files(
        title="Select media",
        multiple=True,
        file_kind="media",
    )

    assert options["filetypes"] == [
        (
            "Media files",
            "*.mp4 *.webm *.mov *.mkv *.avi *.m4v "
            "*.png *.jpg *.jpeg *.webp *.bmp *.gif",
        ),
        ("Video files", "*.mp4 *.webm *.mov *.mkv *.avi *.m4v"),
        ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
        ("All files", "*.*"),
    ]
    assert paths == [webp_path, typed_path]


@pytest.mark.parametrize(
    ("selected_name", "expected_name"),
    [
        ("screen_keyed.jpg", None),
        ("screen_keyed", "screen_keyed.png"),
        ("", None),
        ("screen_keyed.PNG", "screen_keyed.PNG"),
    ],
    ids=["jpg", "suffixless", "cancel", "uppercase-png"],
)
def test_image_save_picker_normalizes_selected_path_to_png(
    tmp_path: Path,
    monkeypatch,
    selected_name: str,
    expected_name: str | None,
) -> None:
    selected_path = tmp_path / selected_name
    options: dict[str, object] = {}

    class FakeRoot:
        def destroy(self) -> None:
            pass

    class FakeFileDialog:
        def asksaveasfilename(self, **kwargs: object) -> str:
            options.update(kwargs)
            return str(selected_path) if selected_name else ""

    monkeypatch.setattr(
        native_picker,
        "_create_tk_dialog_context",
        lambda: (FakeRoot(), FakeFileDialog()),
    )

    paths = native_picker.TkinterLocalPathPicker().pick_save_file(
        title="Save keyed image",
        file_kind="image",
    )

    assert options["filetypes"] == [("PNG files", "*.png")]
    assert options["defaultextension"] == ".png"
    expected_paths = [tmp_path / expected_name] if expected_name is not None else []
    assert paths == expected_paths


def test_image_save_picker_remembers_directory_only_for_accepted_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initial_directory = tmp_path / "initial"
    rejected_path = tmp_path / "rejected" / "screen_keyed.jpg"
    accepted_path = tmp_path / "accepted" / "screen_keyed"
    dialog_options: list[dict[str, object]] = []
    selected_paths = iter([str(rejected_path), str(accepted_path), ""])

    class FakeRoot:
        def destroy(self) -> None:
            pass

    class FakeFileDialog:
        def asksaveasfilename(self, **kwargs: object) -> str:
            dialog_options.append(kwargs)
            return next(selected_paths)

    monkeypatch.setattr(
        native_picker,
        "_create_tk_dialog_context",
        lambda: (FakeRoot(), FakeFileDialog()),
    )
    picker = native_picker.TkinterLocalPathPicker()
    picker._last_directory = initial_directory

    assert picker.pick_save_file(title="Save keyed image", file_kind="image") == []
    assert picker.pick_save_file(title="Save keyed image", file_kind="image") == [
        accepted_path.with_suffix(".png")
    ]
    assert picker.pick_save_file(title="Save keyed image", file_kind="image") == []

    assert dialog_options[0]["initialdir"] == str(initial_directory)
    assert dialog_options[1]["initialdir"] == str(initial_directory)
    assert dialog_options[2]["initialdir"] == str(accepted_path.parent)


def test_pick_files_endpoint_accepts_image_kind_and_returns_selected_image(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.jpeg"

    class ImagePicker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool, str]] = []

        def pick_files(
            self,
            *,
            title: str,
            multiple: bool,
            file_kind: str,
        ) -> list[Path]:
            self.calls.append((title, multiple, file_kind))
            return [image_path]

    picker = ImagePicker()
    client = TestClient(create_app(local_path_picker=picker))

    response = client.post(
        "/api/local-paths/pick-files",
        json={
            "title": "Select image",
            "multiple": False,
            "file_kind": "image",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"paths": [str(image_path)], "cancelled": False}
    assert picker.calls == [("Select image", False, "image")]


def test_pick_files_endpoint_propagates_optional_allowed_suffixes(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.jpeg"

    class ConstrainedPicker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool, str, list[str] | None]] = []

        def pick_files(
            self,
            *,
            title: str,
            multiple: bool,
            file_kind: str,
            allowed_suffixes: list[str] | None = None,
        ) -> list[Path]:
            self.calls.append((title, multiple, file_kind, allowed_suffixes))
            return [image_path]

    picker = ConstrainedPicker()
    client = TestClient(create_app(local_path_picker=picker))

    response = client.post(
        "/api/local-paths/pick-files",
        json={
            "title": "Select supported media",
            "multiple": False,
            "file_kind": "media",
            "allowed_suffixes": CHROMA_ALLOWED_SUFFIXES,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"paths": [str(image_path)], "cancelled": False}
    assert picker.calls == [
        (
            "Select supported media",
            False,
            "media",
            CHROMA_ALLOWED_SUFFIXES,
        )
    ]


def test_local_file_pick_request_normalizes_and_deduplicates_allowed_suffixes() -> None:
    request = LocalFilePickRequest(
        allowed_suffixes=[".MP4", ".mp4", ".JPEG", ".jpeg", ".PNG"],
    )

    assert request.allowed_suffixes == [".mp4", ".jpeg", ".png"]


@pytest.mark.parametrize(
    "allowed_suffixes",
    [
        [],
        ["mp4"],
        ["*.mp4"],
        [".mp4 "],
        [".exe"],
    ],
    ids=["empty", "missing-dot", "wildcard", "padded", "unsupported"],
)
def test_pick_files_endpoint_rejects_invalid_allowed_suffixes_before_picker(
    allowed_suffixes: list[str],
) -> None:
    class UncalledPicker:
        def __init__(self) -> None:
            self.call_count = 0

        def pick_files(
            self,
            *,
            title: str,
            multiple: bool,
            file_kind: str,
            allowed_suffixes: list[str] | None = None,
        ) -> list[Path]:
            self.call_count += 1
            return []

    picker = UncalledPicker()
    client = TestClient(create_app(local_path_picker=picker))

    response = client.post(
        "/api/local-paths/pick-files",
        json={"allowed_suffixes": allowed_suffixes},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert picker.call_count == 0


@pytest.mark.parametrize(
    ("file_kind", "allowed_suffixes"),
    [
        ("image", [".mp4"]),
        ("video", [".png"]),
        ("image", [".webp"]),
        ("media", [".webp"]),
    ],
    ids=[
        "video-suffix-for-image",
        "image-suffix-for-video",
        "broad-media-suffix-for-image",
        "broad-media-suffix-for-media",
    ],
)
def test_pick_files_endpoint_rejects_cross_kind_allowed_suffixes_before_picker(
    file_kind: str,
    allowed_suffixes: list[str],
) -> None:
    class UncalledPicker:
        def __init__(self) -> None:
            self.call_count = 0

        def pick_files(
            self,
            *,
            title: str,
            multiple: bool,
            file_kind: str,
            allowed_suffixes: list[str] | None = None,
        ) -> list[Path]:
            self.call_count += 1
            return []

    picker = UncalledPicker()
    client = TestClient(create_app(local_path_picker=picker))

    response = client.post(
        "/api/local-paths/pick-files",
        json={
            "file_kind": file_kind,
            "allowed_suffixes": allowed_suffixes,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert picker.call_count == 0


def test_any_file_picker_accepts_known_broad_media_suffix() -> None:
    request = LocalFilePickRequest(
        file_kind="any",
        allowed_suffixes=[".WEBP"],
    )

    assert request.allowed_suffixes == [".webp"]


def test_constrained_pick_files_supports_legacy_picker_and_filters_paths(
    tmp_path: Path,
) -> None:
    allowed_path = tmp_path / "screen.PNG"
    unsupported_path = tmp_path / "clip.mp4"

    class LegacyPicker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool, str]] = []

        def pick_files(
            self,
            *,
            title: str,
            multiple: bool,
            file_kind: str,
        ) -> list[Path]:
            self.calls.append((title, multiple, file_kind))
            return [allowed_path, unsupported_path]

    picker = LegacyPicker()
    client = TestClient(create_app(local_path_picker=picker))

    response = client.post(
        "/api/local-paths/pick-files",
        json={
            "title": "Select PNG",
            "multiple": True,
            "file_kind": "image",
            "allowed_suffixes": [".PNG", ".png"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "paths": [str(allowed_path)],
        "cancelled": False,
    }
    assert picker.calls == [("Select PNG", True, "image")]


def test_constrained_pick_files_does_not_swallow_internal_type_error() -> None:
    class FailingPicker:
        def pick_files(
            self,
            *,
            title: str,
            multiple: bool,
            file_kind: str,
            allowed_suffixes: list[str] | None = None,
        ) -> list[Path]:
            raise TypeError("picker failed internally")

    client = TestClient(create_app(local_path_picker=FailingPicker()))

    with pytest.raises(TypeError, match="picker failed internally"):
        client.post(
            "/api/local-paths/pick-files",
            json={
                "file_kind": "image",
                "allowed_suffixes": [".png"],
            },
        )


def test_pick_save_file_endpoint_accepts_image_kind_and_returns_selected_path(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen_keyed.png"

    class ImageSavePicker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def pick_save_file(self, *, title: str, file_kind: str) -> list[Path]:
            self.calls.append((title, file_kind))
            return [image_path]

    picker = ImageSavePicker()
    client = TestClient(create_app(local_path_picker=picker))

    response = client.post(
        "/api/local-paths/pick-save-file",
        json={
            "title": "Save keyed image",
            "file_kind": "image",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"paths": [str(image_path)], "cancelled": False}
    assert picker.calls == [("Save keyed image", "image")]
