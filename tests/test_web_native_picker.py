from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from designtoolkit.web import native_picker
from designtoolkit.web.app import create_app


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
    expected_paths = (
        [tmp_path / expected_name] if expected_name is not None else []
    )
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
