"""Shared CDP box-model quad math for observation bounds and click targeting."""

from __future__ import annotations

from collections.abc import Sequence


def quad_bounds(quad: Sequence[float]) -> tuple[float, float, float, float]:
    """Axis-aligned bounds (left, top, right, bottom) of a CDP content quad."""
    xs = quad[0::2]
    ys = quad[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def quad_center(quad: Sequence[float]) -> tuple[float, float]:
    left, top, right, bottom = quad_bounds(quad)
    return ((left + right) / 2, (top + bottom) / 2)
