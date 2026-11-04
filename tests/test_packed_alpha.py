import importlib

import numpy as np
import pytest

from designtoolkit.chroma_keyer.models import KeyerError


def pack(rgb, alpha):
    module = importlib.import_module("designtoolkit.chroma_keyer.packed_alpha")
    return module.pack_alpha_frame(rgb, alpha)


def test_pack_alpha_frame_preserves_rgb_and_pads_each_half():
    rgb = np.arange(45, dtype=np.uint8).reshape(3, 5, 3)
    alpha = np.tile([0, 0.5, 1, 0.5, 0], (3, 1))
    packed = pack(rgb, alpha)
    assert packed.shape == (4, 12, 3)
    assert packed.dtype == np.uint8
    assert packed.flags.c_contiguous
    np.testing.assert_array_equal(packed[:3, :5], rgb)
    np.testing.assert_array_equal(
        packed[:3, 6:11],
        np.repeat(np.tile([0, 128, 255, 128, 0], (3, 1))[..., None], 3, 2),
    )
    assert not packed[3].any()
    assert not packed[:, 5].any()
    assert not packed[:, 11].any()


def test_pack_alpha_frame_accepts_core_byte_alpha_and_strided_rgb():
    rgb = np.full((2, 4, 3), 213, dtype=np.uint8)[:, ::2]
    alpha = np.array([[0, 128], [255, 16]], dtype=np.uint8)
    packed = pack(rgb, alpha)
    np.testing.assert_array_equal(packed[:, :2], rgb)
    np.testing.assert_array_equal(packed[:, 2:, 0], alpha)
    assert packed.flags.c_contiguous


@pytest.mark.parametrize(
    "rgb,alpha",
    [
        (None, np.zeros((2, 2))),
        (np.zeros((2, 2, 3)), np.zeros((2, 2))),
        (np.zeros((2, 2, 4), dtype=np.uint8), np.zeros((2, 2))),
        (np.zeros((0, 2, 3), dtype=np.uint8), np.zeros((0, 2))),
        (np.zeros((2, 2, 3), dtype=np.uint8), None),
        (np.zeros((2, 2, 3), dtype=np.uint8), np.zeros((1, 2))),
        (np.zeros((2, 2, 3), dtype=np.uint8), np.zeros((2, 2), dtype=np.int32)),
        (np.zeros((2, 2, 3), dtype=np.uint8), np.full((2, 2), np.nan)),
        (np.zeros((2, 2, 3), dtype=np.uint8), np.full((2, 2), np.inf)),
        (np.zeros((2, 2, 3), dtype=np.uint8), np.full((2, 2), -0.1)),
        (np.zeros((2, 2, 3), dtype=np.uint8), np.full((2, 2), 1.1)),
    ],
)
def test_pack_alpha_frame_rejects_invalid_inputs(rgb, alpha):
    with pytest.raises(KeyerError, match="rgb|alpha"):
        pack(rgb, alpha)
