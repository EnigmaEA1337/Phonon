"""Tests for audio math utilities — dB conversion, pan law, gain validation."""

from __future__ import annotations

import pytest

from phonon_stage.mappings.audio_math import (
    GAIN_MIN_DB,
    db_to_linear,
    linear_to_db,
    pan_to_stereo_gains,
    validate_gain,
)


class TestDbToLinear:
    def test_unity_gain(self) -> None:
        assert db_to_linear(0.0) == pytest.approx(1.0)

    def test_minus_6db(self) -> None:
        assert db_to_linear(-6.0) == pytest.approx(0.5012, abs=0.001)

    def test_plus_6db(self) -> None:
        assert db_to_linear(6.0) == pytest.approx(1.9953, abs=0.001)

    def test_silence(self) -> None:
        assert db_to_linear(-90.0) == 0.0

    def test_below_silence(self) -> None:
        assert db_to_linear(-100.0) == 0.0


class TestLinearToDb:
    def test_unity(self) -> None:
        assert linear_to_db(1.0) == pytest.approx(0.0)

    def test_half(self) -> None:
        assert linear_to_db(0.5) == pytest.approx(-6.02, abs=0.01)

    def test_zero_returns_min(self) -> None:
        assert linear_to_db(0.0) == GAIN_MIN_DB

    def test_roundtrip(self) -> None:
        for db in [-20.0, -6.0, 0.0, 6.0, 12.0]:
            assert linear_to_db(db_to_linear(db)) == pytest.approx(db, abs=0.01)


class TestPanLaw:
    def test_center(self) -> None:
        left, right = pan_to_stereo_gains(0.0)
        assert left == pytest.approx(right, abs=0.001)

    def test_full_left(self) -> None:
        left, right = pan_to_stereo_gains(-1.0)
        assert left == pytest.approx(1.0, abs=0.001)
        assert right == pytest.approx(0.0, abs=0.001)

    def test_full_right(self) -> None:
        left, right = pan_to_stereo_gains(1.0)
        assert left == pytest.approx(0.0, abs=0.001)
        assert right == pytest.approx(1.0, abs=0.001)


class TestValidateGain:
    def test_valid_range(self) -> None:
        assert validate_gain(0.0) == 0.0
        assert validate_gain(-90.0) == -90.0
        assert validate_gain(12.0) == 12.0

    def test_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            validate_gain(13.0)
        with pytest.raises(ValueError, match="out of range"):
            validate_gain(-91.0)

    def test_master_cap(self) -> None:
        assert validate_gain(0.0, is_master=True) == 0.0
        with pytest.raises(ValueError):
            validate_gain(1.0, is_master=True)
