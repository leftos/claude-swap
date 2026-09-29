"""Weekly usage pace (issue #125).

A weekly window is "ahead of pace" when the account has used more of it than
the fraction of the weekly reset cycle that has elapsed so far — e.g. 40% used
at the 20%-through-the-week mark. Applies only to weekly windows (``seven_day``
and every ``scoped`` per-model window); the 5h window is excluded because it
resets too fast for pace to mean anything (it starts "ahead of pace" almost by
definition early in the window, and the store's poll floor of ~3 minutes,
drifting to ~10 minutes when idle, is a large fraction of a 5h window but
negligible against a week — see issue #125 discussion).

``compute_pace`` is a pure function: it consumes an already-fetched window
dict and the snapshot's ``fetched_at``, never fetches anything itself, and has
no influence on poll cadence (that stays entirely in ``poll_policy``). Elapsed
time is measured against ``fetched_at`` rather than wall-clock ``now()``, so a
snapshot served stale (last-good data re-served after a failed refetch) is
evaluated against the clock it was actually measured at.

The TUI's display helpers (``pace_delta``, ``recent_projection``) are pure as
well and cover the 5h window too: there the pace only colours a figure, and
the projection extrapolates the store's recent samples, not the whole cycle.
Neither feeds the auto-switch engine or the poll planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Weekly windows reset on a fixed 7-day cadence (matches menubar._WEEKLY_PERIOD_S).
WEEKLY_PERIOD_S = 7 * 86400.0
PERIOD_5H_S = 5 * 3600.0

# Percentage points off pace at which the TUI's pace colour reaches the
# "ahead" stop (and the far stop at twice this). A short window moves more per
# prompt, so it gets the wider margin.
PACE_MARGIN_5H = 10.0
PACE_MARGIN_WEEKLY = 5.0

# Suppress the marker for this long after a weekly reset. Right after reset,
# elapsed is tiny so `expected_pct` is near zero and almost any usage reads as
# "far ahead" — a false positive, not a genuine pace warning. A plain
# `elapsed == 0` guard isn't enough since a snapshot fetched shortly after
# reset already has nonzero (if small) elapsed time.
SUPPRESS_AFTER_RESET_S = 24 * 3600.0

# Minimum (actual - expected) percentage-point gap before showing a marker.
# Below this, "ahead of pace" is within normal usage variance and would just
# add noise to already-dense usage rows. A flat gap also means the marker
# cannot fire once expected passes (100 - threshold) — the last ~day of the
# week — since pct tops out at 100. Deliberate: by then the percentage itself
# tells the story, and a maxed window is the switcher's job anyway.
AHEAD_THRESHOLD_PCT = 15.0


@dataclass(frozen=True)
class PaceResult:
    """One weekly window's pace at the moment its snapshot was fetched."""

    expected_pct: float  # % of the week's budget "on schedule" usage would be at
    actual_pct: float  # the window's actual pct
    elapsed_s: float  # time since this window's current cycle started
    period_s: float  # the window's full cycle length (e.g. 7 days)
    ahead: bool  # actual_pct - expected_pct >= the "meaningfully ahead" threshold


def _resets_at_ts(resets_at: object) -> float | None:
    """POSIX timestamp of a ``resets_at`` ISO string, or None if missing/unparseable."""
    if not isinstance(resets_at, str):
        return None
    try:
        return datetime.fromisoformat(resets_at).timestamp()
    except ValueError:
        return None


def window_elapsed(window: dict | None, *, now: float, period_s: float) -> float | None:
    """Seconds since the window's current cycle started, as of ``now``.

    ``resets_at`` is the *next* reset; the cycle start is found by rolling it
    by whole ``period_s`` increments until it lands at or before ``now``, so a
    ``resets_at`` any number of cycles stale still resolves. None when the
    window's ``pct`` or ``resets_at`` is missing or unusable.
    """
    if not isinstance(window, dict):
        return None
    if not isinstance(window.get("pct"), (int, float)):
        return None
    next_reset = _resets_at_ts(window.get("resets_at"))
    if next_reset is None:
        return None
    # (next_reset - now) mod period_s == time remaining until the next reset,
    # folded into [0, period_s). period_s minus that is elapsed time since the
    # current window started — works regardless of how many whole cycles
    # next_reset is ahead of or behind now.
    remaining = (next_reset - now) % period_s
    return 0.0 if remaining == 0 else period_s - remaining


def pace_delta(window: dict | None, *, now: float, period_s: float) -> float | None:
    """Points used above (+) or below (−) the share of the cycle already elapsed.

    Zero is exactly on pace: the rate that ends the cycle at 100%. No
    after-reset suppression — this only colours a figure. None when the
    window's pct or reset time is unusable.
    """
    elapsed = window_elapsed(window, now=now, period_s=period_s)
    if elapsed is None:
        return None
    assert isinstance(window, dict)
    return float(window["pct"]) - elapsed / period_s * 100.0


def window_period_and_margin(key: str) -> tuple[float, float]:
    """``(cycle length, pace margin)`` for a window key: the 5h window, or a
    weekly one (``seven_day`` and every scoped per-model window)."""
    if key == "five_hour":
        return PERIOD_5H_S, PACE_MARGIN_5H
    return WEEKLY_PERIOD_S, PACE_MARGIN_WEEKLY


@dataclass(frozen=True)
class Projection:
    """When a window runs out at its recent burn rate.

    ``kind`` is ``"out"`` (runs out ``eta_s`` seconds from now, before the
    reset), ``"lasts"`` (the reset comes first) or ``"idle"`` (no usage
    growth over the lookback); ``eta_s`` is set only for ``"out"``.
    """

    kind: str
    eta_s: float | None


def recent_projection(
    samples: tuple[tuple[float, dict], ...],
    key: str,
    *,
    now: float,
    pct: float,
    resets_at_ts: float | None,
    lookback_s: float = 1800.0,
    min_span_s: float = 600.0,
) -> Projection | None:
    """Extrapolate one window's recent samples to the moment it hits 100%.

    Uses only samples since the window's last reset (a drop between two
    consecutive samples) and within ``lookback_s`` of the newest one. None
    when the window is already maxed, or there are fewer than two usable
    samples spanning at least ``min_span_s``.
    """
    if pct >= 100:
        return None
    points = sorted(
        (float(t), float(sample[key]))
        for t, sample in samples
        if isinstance(sample.get(key), (int, float))
    )
    since_reset: list[tuple[float, float]] = []  # newest first
    for t, value in reversed(points):
        if since_reset and value > since_reset[-1][1]:
            break  # the window reset between this sample and the next-newer one
        since_reset.append((t, value))
    if not since_reset:
        return None
    newest_t, newest_pct = since_reset[0]
    recent = [(t, value) for t, value in since_reset if t >= newest_t - lookback_s]
    if len(recent) < 2:
        return None
    oldest_t, oldest_pct = recent[-1]
    span = newest_t - oldest_t
    if span < min_span_s:
        return None
    rate = (newest_pct - oldest_pct) / span
    if rate <= 0:
        return Projection("idle", None)
    eta_now = max(0.0, (100.0 - newest_pct) / rate - (now - newest_t))
    if resets_at_ts is not None and now + eta_now >= resets_at_ts:
        return Projection("lasts", None)
    return Projection("out", eta_now)


def compute_pace(
    window: dict | None,
    *,
    fetched_at: float | None,
    period_s: float = WEEKLY_PERIOD_S,
    suppress_after_reset_s: float = SUPPRESS_AFTER_RESET_S,
    ahead_threshold_pct: float = AHEAD_THRESHOLD_PCT,
) -> PaceResult | None:
    """Pace for one weekly usage window, or None when pace isn't computable/meaningful.

    ``window`` is a raw window dict (``{"pct": ..., "resets_at": ...}``) as
    stored in ``UsageEntry.last_good`` — the *next* reset, not the current
    window's start, since that's the only timestamp the usage API provides.
    The current window's start is derived by rolling ``resets_at`` by whole
    ``period_s`` increments until it lands at or before ``fetched_at``; this
    is correct however many cycles old ``resets_at`` is, including a
    not-yet-rolled-forward stale value.

    Returns None when: the window is missing/unparseable, ``pct``/``resets_at``
    aren't usable, or elapsed time since the window's start is inside
    ``suppress_after_reset_s``.
    """
    if not isinstance(window, dict) or fetched_at is None:
        return None
    elapsed = window_elapsed(window, now=fetched_at, period_s=period_s)
    if elapsed is None:
        return None
    if elapsed < suppress_after_reset_s:
        return None

    pct = float(window["pct"])
    expected_pct = min(100.0, (elapsed / period_s) * 100.0)
    ahead = (pct - expected_pct) >= ahead_threshold_pct
    return PaceResult(
        expected_pct=expected_pct,
        actual_pct=pct,
        elapsed_s=elapsed,
        period_s=period_s,
        ahead=ahead,
    )


def projected_exhaustion_ts(pace: PaceResult, *, fetched_at: float) -> float | None:
    """Linear-projection ETA (POSIX timestamp) for when usage would hit 100%.

    JSON-only per issue #125: the projection assumes a constant burn rate,
    which has wide error bars against real, bursty usage, and would look
    falsely precise if surfaced in the UI. Returns None when there's no
    measurable rate (no elapsed time, or usage isn't climbing).
    """
    if pace.elapsed_s <= 0 or pace.actual_pct <= 0:
        return None
    rate_pct_per_s = pace.actual_pct / pace.elapsed_s
    if rate_pct_per_s <= 0:
        return None
    remaining_pct = 100.0 - pace.actual_pct
    if remaining_pct <= 0:
        return fetched_at
    return fetched_at + remaining_pct / rate_pct_per_s


def will_last_to_reset(pace: PaceResult) -> bool | None:
    """Whether, at the current burn rate, usage stays under 100% through reset.

    JSON-only (like ``projected_exhaustion_ts``): a boolean answer to "should I
    worry" is safe to expose even though it's built from the same linear-rate
    assumption, but it's still a projection with the same wide error bars
    against bursty real usage, so it stays out of every human-facing surface.
    Returns None when there's no measurable rate to extrapolate from.

    Relationship to the ``(ahead)`` marker: at a constant rate the projected
    total is ``100 × actual/expected``, so this is False exactly when the
    window is over expected *at all* — the marker's signal with no threshold.
    A window can therefore report ``willLastToReset: false`` while showing no
    marker: the marker additionally requires being ``AHEAD_THRESHOLD_PCT``
    over expected. Deliberate — scripts get the sensitive signal, the UI gets
    the noise-gated one.
    """
    if pace.actual_pct <= 0:
        return True  # no usage yet — nothing to run out of before reset
    if pace.elapsed_s <= 0:
        return None
    rate_pct_per_s = pace.actual_pct / pace.elapsed_s
    if rate_pct_per_s <= 0:
        return None
    projected_total_pct = pace.actual_pct + rate_pct_per_s * (pace.period_s - pace.elapsed_s)
    return projected_total_pct <= 100.0
