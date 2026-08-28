from pathlib import Path

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
