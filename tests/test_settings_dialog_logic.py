from __future__ import annotations

import pytest

from pdf_ocrer.settings_dialog import (
    MODEL_SIZE_CUSTOM_LABEL,
    MODEL_SIZE_MEDIUM_LABEL,
    MODEL_SIZE_SMALL_LABEL,
    MODEL_SIZE_TINY_LABEL,
    model_pair_for_label,
    model_size_dropdown_values,
    model_size_label_for_pair,
)


@pytest.mark.parametrize(
    ("det_model_name", "rec_model_name", "expected_label"),
    [
        ("PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec", MODEL_SIZE_TINY_LABEL),
        ("PP-OCRv6_small_det", "PP-OCRv6_small_rec", MODEL_SIZE_SMALL_LABEL),
        (None, None, MODEL_SIZE_MEDIUM_LABEL),
    ],
)
def test_model_size_label_for_known_pairs(det_model_name, rec_model_name, expected_label):
    assert model_size_label_for_pair(det_model_name, rec_model_name) == expected_label


@pytest.mark.parametrize(
    ("det_model_name", "rec_model_name"),
    [
        ("PP-OCRv6_tiny_det", "PP-OCRv6_small_rec"),
        ("PP-OCRv6_small_det", None),
        (None, "PP-OCRv6_small_rec"),
        ("custom-det", "custom-rec"),
        ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
    ],
)
def test_model_size_label_for_unknown_or_mismatched_pairs_returns_custom(
    det_model_name,
    rec_model_name,
):
    assert model_size_label_for_pair(det_model_name, rec_model_name) == MODEL_SIZE_CUSTOM_LABEL


@pytest.mark.parametrize(
    ("label", "expected_pair"),
    [
        (MODEL_SIZE_TINY_LABEL, ("PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec")),
        (MODEL_SIZE_SMALL_LABEL, ("PP-OCRv6_small_det", "PP-OCRv6_small_rec")),
        (MODEL_SIZE_MEDIUM_LABEL, (None, None)),
    ],
)
def test_model_pair_for_known_labels(label, expected_pair):
    assert model_pair_for_label(label, current=("keep-det", "keep-rec")) == expected_pair


def test_model_pair_for_custom_label_returns_current_pair_unchanged():
    current = ("custom-det", None)

    assert model_pair_for_label(MODEL_SIZE_CUSTOM_LABEL, current=current) is current


def test_model_pair_for_unknown_label_raises():
    with pytest.raises(ValueError, match="未知模型大小"):
        model_pair_for_label("other", current=(None, None))


@pytest.mark.parametrize(
    ("det_model_name", "rec_model_name"),
    [
        ("PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec"),
        ("PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
        (None, None),
    ],
)
def test_model_size_dropdown_values_for_known_pairs(det_model_name, rec_model_name):
    assert model_size_dropdown_values(det_model_name, rec_model_name) == [
        MODEL_SIZE_TINY_LABEL,
        MODEL_SIZE_SMALL_LABEL,
        MODEL_SIZE_MEDIUM_LABEL,
    ]


def test_model_size_dropdown_values_for_unknown_pair_includes_custom_label():
    assert model_size_dropdown_values("custom-det", "custom-rec") == [
        MODEL_SIZE_TINY_LABEL,
        MODEL_SIZE_SMALL_LABEL,
        MODEL_SIZE_MEDIUM_LABEL,
        MODEL_SIZE_CUSTOM_LABEL,
    ]
