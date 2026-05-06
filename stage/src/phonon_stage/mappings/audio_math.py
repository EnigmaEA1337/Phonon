"""Audio math utilities — dB/linear conversion, pan law, gain safety."""

from __future__ import annotations

import math

# Gain bounds (SECURITY.md §3.5 + CLAUDE.md standalone)
GAIN_MIN_DB = -90.0
GAIN_MAX_DB = 12.0
MASTER_GAIN_MAX_DB = 0.0  # Master strip capped at unity

# Silence threshold: below this dB, treat as -inf (mute)
SILENCE_THRESHOLD_DB = -89.0


def db_to_linear(db: float) -> float:
    """Convert decibels to linear amplitude.

    Args:
        db: Gain in decibels. Values below SILENCE_THRESHOLD_DB return 0.0.

    Returns:
        Linear amplitude (0.0 = silence, 1.0 = unity gain).
    """
    if db <= SILENCE_THRESHOLD_DB:
        return 0.0
    return float(10.0 ** (db / 20.0))


def linear_to_db(linear: float) -> float:
    """Convert linear amplitude to decibels.

    Args:
        linear: Amplitude (must be > 0). 0.0 returns GAIN_MIN_DB.

    Returns:
        Gain in decibels.
    """
    if linear <= 0.0:
        return GAIN_MIN_DB
    return 20.0 * math.log10(linear)


def pan_to_stereo_gains(pan: float) -> tuple[float, float]:
    """Convert pan position to stereo gain multipliers (constant-power pan law).

    Args:
        pan: Pan position, -1.0 (full left) to +1.0 (full right). 0.0 = center.

    Returns:
        Tuple of (left_gain, right_gain), each in [0.0, 1.0].
    """
    # Constant-power pan: equal power at center (each channel at -3 dB)
    angle = (pan + 1.0) * math.pi / 4.0  # 0 to pi/2
    left = math.cos(angle)
    right = math.sin(angle)
    return (left, right)


def validate_gain(db: float, *, is_master: bool = False) -> float:
    """Validate and clamp gain to allowed range.

    Args:
        db: Requested gain in dB.
        is_master: If True, cap at MASTER_GAIN_MAX_DB (0 dB).

    Returns:
        Validated gain value.

    Raises:
        ValueError: If gain is outside allowed range.
    """
    max_db = MASTER_GAIN_MAX_DB if is_master else GAIN_MAX_DB
    if db < GAIN_MIN_DB or db > max_db:
        msg = f"Gain {db} dB out of range [{GAIN_MIN_DB}, {max_db}]"
        raise ValueError(msg)
    return db
