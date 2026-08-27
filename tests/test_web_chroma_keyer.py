from io import BytesIO
from os import utime
from pathlib import Path
from uuid import UUID

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image
from pytest import mark, raises

from designtoolkit.chroma_keyer.despill import despill
from designtoolkit.chroma_keyer.image_io import make_preview_png, read_rgb_image
from designtoolkit.chroma_keyer.matte import generate_alpha, refine_alpha
from designtoolkit.chroma_keyer.models import KeyerParams
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples
from designtoolkit.web.app import create_app
from designtoolkit.web.chroma_previews import ChromaPreviewStore
from designtoolkit.web.schemas import ChromaKeyerTaskRequest


def _write_input_image(path: Path) -> np.ndarray:
    pixels = np.array(
        [
            [[42, 180, 72], [220, 40, 40], [30, 50, 200], [10, 20, 30]],
            [[20, 30, 40], [50, 60, 70], [80, 90, 100], [110, 120, 130]],
        ],
        dtype=np.uint8,
    )
    Image.fromarray(pixels, mode="RGB").save(path)
    return pixels


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(local_path_roots=[tmp_path]))


def _decode_rgb_png(png_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(png_bytes)) as image:
        return np.asarray(image.convert("RGB")).copy()


def test_chroma_keyer_task_request_accepts_the_default_export_contract() -> None:
    request = ChromaKeyerTaskRequest(
        input_path="input.png",
        samples=[[42, 180, 72]],
    )

    assert request.model_dump() == {
        "input_path": "input.png",
        "samples": [[42, 180, 72]],
        "tolerance": 0.25,
        "softness": 0.15,
        "clip_black": 0.0,
        "clip_white": 1.0,
        "shrink_grow": 0,
        "feather": 0,
        "despill_amount": 0.5,
        "output_path": None,
    }


@mark.parametrize(
    "changes",
    [
        {"samples": []},
        {"samples": [[42, 180]]},
        {"samples": [[-1, 180, 72]]},
        {"samples": [[42, 180, 256]]},
        {"samples": [[True, 180, 72]]},
        {"clip_black": 0.5, "clip_white": 0.5},
        {"output_path": "   "},
    ],
)
def test_chroma_keyer_task_request_rejects_invalid_export_contract(
    changes: dict[str, object],
) -> None:
    with raises(ValueError):
        ChromaKeyerTaskRequest.model_validate(
            {
                "input_path": "input.png",
                "samples": [[42, 180, 72]],
                **changes,
            }
        )


def test_inspect_returns_dimensions_and_a_retrievable_preview(tmp_path: Path) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)

    response = client.post(
        "/api/chroma-keyer/images/inspect",
        json={"input_path": str(input_path)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "input_path": str(input_path),
        "width": 4,
        "height": 2,
        "preview_width": 4,
        "preview_height": 2,
        "preview_url": body["preview_url"],
    }
    assert body["preview_url"].startswith("/api/chroma-keyer/previews/")

    preview = client.get(body["preview_url"])

    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_inspect_succeeds_when_eviction_interleaves_after_cache_insertion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    original_get_or_put_entry = ChromaPreviewStore.get_or_put_entry
    calls = 0

    def evict_after_cache_insertion(store, cache_key, create_png_bytes):
        nonlocal calls
        calls += 1
        preview_entry = original_get_or_put_entry(store, cache_key, create_png_bytes)
        for index in range(32):
            store.put(f"eviction-{index}".encode())
        return preview_entry

    monkeypatch.setattr(
        ChromaPreviewStore,
        "get_or_put_entry",
        evict_after_cache_insertion,
    )
    client = TestClient(
        create_app(local_path_roots=[tmp_path]),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/chroma-keyer/images/inspect",
        json={"input_path": str(input_path)},
    )

    assert response.status_code == 200
    assert calls == 1


def test_inspect_preview_reuses_file_fingerprint_and_changes_after_source_update(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    pixels = _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {"input_path": str(input_path)}

    first_response = client.post("/api/chroma-keyer/images/inspect", json=payload)
    duplicate_response = client.post(
        "/api/chroma-keyer/images/inspect",
        json=payload,
    )

    first_url = first_response.json()["preview_url"]
    assert first_response.status_code == 200
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["preview_url"] == first_url
    UUID(first_url.rsplit("/", maxsplit=1)[1])
    assert str(input_path) not in first_url

    source_stat = input_path.stat()
    pixels[0, 0] = [1, 2, 3]
    Image.fromarray(pixels, mode="RGB").save(input_path)
    utime(
        input_path,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000_000),
    )

    updated_response = client.post("/api/chroma-keyer/images/inspect", json=payload)

    assert updated_response.status_code == 200
    assert updated_response.json()["preview_url"] != first_url


def test_sample_maps_preview_coordinates_to_source_pixel_with_uppercase_hex(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            "x": 0.9,
            "y": 0.1,
            "preview_width": 2,
            "preview_height": 1,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "rgb": [220, 40, 40],
        "hex": "#DC2828",
        "source_x": 1,
        "source_y": 0,
    }


def test_final_and_alpha_previews_match_the_public_keying_pipeline(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {
            "tolerance": 0.25,
            "softness": 0.15,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 0.5,
        },
    }

    final_response = client.post("/api/chroma-keyer/previews", json=payload)
    alpha_response = client.post(
        "/api/chroma-keyer/previews",
        json={**payload, "view_mode": "alpha"},
    )

    assert final_response.status_code == 200
    assert alpha_response.status_code == 200
    final_png = client.get(final_response.json()["preview_url"])
    alpha_png = client.get(alpha_response.json()["preview_url"])
    assert final_png.headers["content-type"] == "image/png"
    assert alpha_png.headers["content-type"] == "image/png"
    assert final_png.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert alpha_png.content.startswith(b"\x89PNG\r\n\x1a\n")

    source_rgb = read_rgb_image(input_path)
    model = screen_model_from_samples(payload["samples"])
    params = KeyerParams(**payload["params"])
    refined_alpha = refine_alpha(generate_alpha(source_rgb, model, params), params)
    expected_final = _decode_rgb_png(
        make_preview_png(
            despill(source_rgb, refined_alpha, model, params.despill_amount)
        )
    )
    expected_alpha = _decode_rgb_png(
        make_preview_png(np.repeat(refined_alpha[..., np.newaxis], 3, axis=2))
    )

    assert np.array_equal(_decode_rgb_png(final_png.content), expected_final)
    assert np.array_equal(_decode_rgb_png(alpha_png.content), expected_alpha)


def test_generated_preview_reuses_request_state_and_varies_by_view_and_params(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
        "params": {
            "tolerance": 0.25,
            "softness": 0.15,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 0.5,
        },
    }

    first_response = client.post("/api/chroma-keyer/previews", json=payload)
    duplicate_response = client.post("/api/chroma-keyer/previews", json=payload)
    alpha_response = client.post(
        "/api/chroma-keyer/previews",
        json={**payload, "view_mode": "alpha"},
    )
    tolerance_response = client.post(
        "/api/chroma-keyer/previews",
        json={
            **payload,
            "params": {**payload["params"], "tolerance": 0.3},
        },
    )

    first_url = first_response.json()["preview_url"]
    assert first_response.status_code == 200
    assert duplicate_response.status_code == 200
    assert alpha_response.status_code == 200
    assert tolerance_response.status_code == 200
    assert duplicate_response.json()["preview_url"] == first_url
    assert alpha_response.json()["preview_url"] != first_url
    assert tolerance_response.json()["preview_url"] != first_url
    for response in (
        first_response,
        duplicate_response,
        alpha_response,
        tolerance_response,
    ):
        preview_url = response.json()["preview_url"]
        UUID(preview_url.rsplit("/", maxsplit=1)[1])
        assert str(input_path) not in preview_url


def test_singular_and_plural_preview_routes_share_the_request_cache(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    client = _client(tmp_path)
    payload = {
        "input_path": str(input_path),
        "samples": [[42, 180, 72]],
    }

    plural_response = client.post("/api/chroma-keyer/previews", json=payload)
    singular_response = client.post("/api/chroma-keyer/preview", json=payload)

    assert plural_response.status_code == 200
    assert singular_response.status_code == 200
    assert (
        singular_response.json()["preview_url"]
        == plural_response.json()["preview_url"]
    )


@mark.parametrize("url", ["/api/chroma-keyer/preview", "/api/chroma-keyer/previews"])
@mark.parametrize("samples", [None, []])
def test_preview_rejects_missing_or_empty_samples_for_a_valid_image(
    tmp_path: Path,
    url: str,
    samples: list[list[int]] | None,
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)
    payload: dict[str, object] = {
        "input_path": str(input_path),
        "params": {
            "tolerance": 0.25,
            "softness": 0.15,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 0.5,
        },
    }
    if samples is not None:
        payload["samples"] = samples

    response = _client(tmp_path).post(
        url,
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_samples"


def test_unknown_preview_returns_structured_not_found(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/chroma-keyer/previews/not-a-preview")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "preview_not_found"


@mark.parametrize(
    "payload",
    [
        {
            "x": 2,
            "y": 0,
            "preview_width": 2,
            "preview_height": 1,
        },
        {
            "x": 0,
            "y": 0,
            "preview_width": 0,
            "preview_height": 1,
        },
    ],
)
def test_invalid_sample_coordinates_return_structured_bad_request(
    tmp_path: Path,
    payload: dict[str, int],
) -> None:
    input_path = tmp_path / "input.png"
    _write_input_image(input_path)

    response = _client(tmp_path).post(
        "/api/chroma-keyer/images/sample",
        json={
            "input_path": str(input_path),
            **payload,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_sample_coordinates"


@mark.parametrize(
    ("url", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "x": 0,
                "y": 0,
                "preview_width": 1,
                "preview_height": 1,
            },
        ),
        (
            "/api/chroma-keyer/previews",
            {
                "samples": [[42, 180, 72]],
            },
        ),
    ],
)
def test_chroma_decoder_failures_return_invalid_image(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
) -> None:
    input_path = tmp_path / "corrupt.png"
    input_path.write_bytes(b"not an image")

    response = _client(tmp_path).post(
        url,
        json={"input_path": str(input_path), **payload},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image"


@mark.parametrize(
    ("url", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "x": 0,
                "y": 0,
                "preview_width": 1,
                "preview_height": 1,
            },
        ),
        (
            "/api/chroma-keyer/preview",
            {
                "samples": [[42, 180, 72]],
            },
        ),
        (
            "/api/chroma-keyer/previews",
            {
                "samples": [[42, 180, 72]],
            },
        ),
    ],
)
def test_missing_chroma_source_returns_invalid_request(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
) -> None:
    response = _client(tmp_path).post(
        url,
        json={
            "input_path": str(tmp_path / "missing.png"),
            **payload,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@mark.parametrize(
    ("url", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {"input_path": "   "}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "input_path": "missing.png",
                "x": 2,
                "y": 0,
                "preview_width": 2,
                "preview_height": 1,
            },
        ),
        (
            "/api/chroma-keyer/previews",
            {
                "input_path": "missing.png",
                "samples": [[42, 180, 72]],
            },
        ),
    ],
)
def test_invalid_chroma_requests_return_structured_bad_request(
    tmp_path: Path,
    url: str,
    payload: dict[str, object],
) -> None:
    response = _client(tmp_path).post(url, json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_preview_store_evicts_oldest_entry_at_capacity() -> None:
    store = ChromaPreviewStore(max_entries=2)

    first_id = store.put(b"first")
    second_id = store.put(b"second")
    third_id = store.put(b"third")

    UUID(first_id)
    assert store.get(first_id) is None
    assert store.get(second_id) == b"second"
    assert store.get(third_id) == b"third"


def test_preview_store_evicts_reverse_cache_key_mapping() -> None:
    store = ChromaPreviewStore(max_entries=2)

    first_id = store.put(b"first", cache_key="first-request")
    store.put(b"second", cache_key="second-request")
    store.put(b"third", cache_key="third-request")
    replacement_id = store.put(b"replacement", cache_key="first-request")

    assert store.get(first_id) is None
    assert replacement_id != first_id
    assert store.get(replacement_id) == b"replacement"


def test_preview_store_returns_immutable_entry_across_eviction() -> None:
    store = ChromaPreviewStore(max_entries=1)

    def create_unexpected_preview() -> bytes:
        raise AssertionError("cache hit must not create a preview")

    inserted_entry = store.get_or_put_entry("request", lambda: b"first")
    cached_entry = store.get_or_put_entry("request", create_unexpected_preview)
    store.put(b"second")

    assert cached_entry == inserted_entry
    assert store.get(inserted_entry.preview_id) is None
    assert inserted_entry.png_bytes == b"first"


@mark.parametrize("max_entries", [0, -1, True, 1.5])
def test_preview_store_rejects_invalid_capacity(max_entries: object) -> None:
    with raises(ValueError):
        ChromaPreviewStore(max_entries=max_entries)  # type: ignore[arg-type]


@mark.parametrize("png_bytes", [b"", "not-bytes", bytearray(b"bytes")])
def test_preview_store_rejects_empty_or_non_bytes_values(png_bytes: object) -> None:
    with raises(ValueError):
        ChromaPreviewStore().put(png_bytes)  # type: ignore[arg-type]
