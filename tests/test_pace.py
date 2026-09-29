"""Unit tests for the weekly usage pace helper (issue #125)."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import pace

NOW = 1_700_000_000.0  # a realistic epoch; keeps NOW-minus-several-weeks positive
                        # (datetime.fromtimestamp rejects negative timestamps on Windows)
DAY = 86400.0
WEEK = pace.WEEKLY_PERIOD_S


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _window(pct: float, resets_at_ts: float | None) -> dict:
    window: dict = {"pct": pct}
    if resets_at_ts is not None:
        window["resets_at"] = _iso(resets_at_ts)
    return window


class TestComputePaceElapsed:
    def test_one_day_into_the_week(self):
        # Reset is 6 days away -> 1 day has elapsed since the window started.
        window = _window(20.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.elapsed_s == DAY
        assert result.expected_pct == (DAY / WEEK) * 100.0

    def test_right_at_reset_boundary_is_suppressed(self):
        # resets_at exactly one period ahead of fetched_at -> elapsed == 0.
        window = _window(5.0, NOW + WEEK)
        assert pace.compute_pace(window, fetched_at=NOW) is None

    def test_stale_resets_at_multiple_cycles_in_the_past_still_resolves(self):
        # resets_at is 2 full weeks + 1 day behind fetched_at (never rolled
        # forward by a caller) -> current window still started 1 day ago.
        window = _window(20.0, NOW - 2 * WEEK - DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.elapsed_s == DAY

    def test_missing_fields_return_none(self):
        assert pace.compute_pace(None, fetched_at=NOW) is None
        assert pace.compute_pace({"pct": 10.0}, fetched_at=NOW) is None  # no resets_at
        assert pace.compute_pace({"resets_at": _iso(NOW + DAY)}, fetched_at=NOW) is None  # no pct
        assert pace.compute_pace(_window(10.0, NOW + DAY), fetched_at=None) is None
        assert pace.compute_pace({"pct": 10.0, "resets_at": "not-a-date"}, fetched_at=NOW) is None


class TestSuppressionWindow:
    def test_just_inside_suppression_window_is_none(self):
        elapsed = pace.SUPPRESS_AFTER_RESET_S - 1.0
        window = _window(50.0, NOW + WEEK - elapsed)
        assert pace.compute_pace(window, fetched_at=NOW) is None

    def test_just_outside_suppression_window_is_not_none(self):
        elapsed = pace.SUPPRESS_AFTER_RESET_S
        window = _window(50.0, NOW + WEEK - elapsed)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.elapsed_s == elapsed


class TestAheadThreshold:
    def test_meaningfully_ahead_flags_true(self):
        # 1 day elapsed (~14.3% expected); 50% actual is far ahead.
        window = _window(50.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is True

    def test_within_threshold_flags_false_but_still_returns(self):
        # 1 day elapsed (~14.3% expected); 20% actual is close, not "ahead".
        window = _window(20.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is False

    def test_behind_pace_flags_false(self):
        window = _window(5.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is False


class TestProjectedExhaustionTs:
    def test_linear_projection(self):
        # 1 day elapsed, 50% used -> burn rate implies 100% at day 2.
        window = _window(50.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        eta = pace.projected_exhaustion_ts(result, fetched_at=NOW)
        assert eta is not None
        assert eta == NOW + DAY  # one more day at the same rate exhausts it

    def test_already_at_or_over_100_returns_fetched_at(self):
        window = _window(120.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.projected_exhaustion_ts(result, fetched_at=NOW) == NOW

    def test_zero_usage_has_no_projection(self):
        window = _window(0.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.projected_exhaustion_ts(result, fetched_at=NOW) is None


class TestWillLastToReset:
    def test_sustainable_rate_will_last(self):
        # 1 day elapsed, 10% used -> extrapolated to a full week is 70%, fine.
        window = _window(10.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is True

    def test_unsustainable_rate_will_not_last(self):
        # 1 day elapsed, 50% used -> extrapolated to a full week is 350%.
        window = _window(50.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is False

    def test_zero_usage_will_last(self):
        window = _window(0.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is True

    def test_comfortably_sustainable_rate_will_last(self):
        # 1 day elapsed, actual slightly under the ~14.3% expected -> stays
        # comfortably under 100% extrapolated across the full week.
        window = _window(12.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is True


class TestAheadVsWillLastRelationship:
    """Pin that will_last_to_reset is the marker's signal with no threshold."""

    def test_will_last_flips_exactly_at_expected_pct(self):
        # Linear projection crosses 100% exactly when actual exceeds expected
        # (projected total = 100 × actual/expected), threshold not involved.
        for days in (1.0, 2.5, 4.0, 6.0):
            resets_at = NOW + (7.0 - days) * DAY
            expected = (days * DAY / WEEK) * 100.0
            over = pace.compute_pace(_window(expected + 0.5, resets_at), fetched_at=NOW)
            under = pace.compute_pace(_window(expected - 0.5, resets_at), fetched_at=NOW)
            assert over is not None and under is not None
            assert pace.will_last_to_reset(over) is False
            assert pace.will_last_to_reset(under) is True

    def test_slightly_ahead_reads_wont_last_with_no_marker(self):
        # The deliberate in-between band: over expected by less than
        # AHEAD_THRESHOLD_PCT keeps the UI silent while the JSON projection
        # already says the window won't last — scripts get the sensitive
        # signal, the UI gets the noise-gated one.
        window = _window(25.0, NOW + 6 * DAY)  # expected ~14.3, delta ~10.7
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is False
        assert pace.will_last_to_reset(result) is False


class TestPaceDelta:
    def test_window_elapsed_matches_compute_pace(self):
        for resets_at in (NOW + 6 * DAY, NOW - 2 * WEEK - DAY, NOW + 2.5 * DAY):
            window = _window(40.0, resets_at)
            result = pace.compute_pace(window, fetched_at=NOW)
            assert result is not None
            assert pace.window_elapsed(window, now=NOW, period_s=WEEK) == result.elapsed_s
        assert pace.window_elapsed(None, now=NOW, period_s=WEEK) is None
        assert pace.window_elapsed({"pct": 10.0}, now=NOW, period_s=WEEK) is None
        assert pace.window_elapsed({"resets_at": _iso(NOW + DAY)}, now=NOW, period_s=WEEK) is None

    def test_pace_delta_on_pace_is_zero(self):
        period, margin = pace.window_period_and_margin("five_hour")
        assert (period, margin) == (pace.PERIOD_5H_S, pace.PACE_MARGIN_5H)
        halfway = _window(50.0, NOW + period / 2)
        assert pace.pace_delta(halfway, now=NOW, period_s=period) == 0.0
        ahead = _window(70.0, NOW + period / 2)
        assert pace.pace_delta(ahead, now=NOW, period_s=period) == 20.0
        # weekly windows (seven_day and scoped names) share the weekly period
        assert pace.window_period_and_margin("seven_day") == (WEEK, pace.PACE_MARGIN_WEEKLY)
        assert pace.window_period_and_margin("Fable") == (WEEK, pace.PACE_MARGIN_WEEKLY)
        behind = _window(10.0, NOW + 3 * DAY)  # 4 of 7 days gone, ~57% expected
        delta = pace.pace_delta(behind, now=NOW, period_s=WEEK)
        assert delta is not None and abs(delta - (10.0 - 4 / 7 * 100.0)) < 1e-9
        assert pace.pace_delta({"pct": 10.0}, now=NOW, period_s=WEEK) is None


def _samples(*points: tuple[float, float], key: str = "five_hour") -> tuple:
    """``(seconds relative to NOW, pct)`` points as store history samples."""
    return tuple((NOW + offset, {key: pct}) for offset, pct in points)


def _project(samples: tuple, *, now: float = NOW, pct: float = 50.0, resets_at_ts=None):
    return pace.recent_projection(
        samples, "five_hour", now=now, pct=pct, resets_at_ts=resets_at_ts
    )


class TestRecentProjection:
    def test_projection_needs_two_samples(self):
        assert _project(()) is None
        assert _project(_samples((0, 40.0))) is None
        # a sample carrying only another window does not count
        other = _samples((-1200, 5.0), key="seven_day")
        assert _project(other + _samples((0, 40.0))) is None

    def test_projection_needs_min_span(self):
        assert _project(_samples((-599, 30.0), (0, 40.0))) is None
        assert _project(_samples((-600, 30.0), (0, 40.0))) is not None

    def test_projection_idle_when_flat(self):
        assert _project(_samples((-1200, 40.0), (0, 40.0))) == pace.Projection("idle", None)

    def test_projection_idle_when_falling_after_reset_discard(self):
        # 80 → 90, then the window reset to 5 and stayed there: what is left
        # after dropping the pre-reset samples is flat.
        samples = _samples((-1500, 80.0), (-1200, 90.0), (-900, 5.0), (0, 5.0))
        assert _project(samples, pct=5.0) == pace.Projection("idle", None)

    def test_projection_discards_samples_before_reset(self):
        # Only 5 → 20 over the last 900 s counts: 15 points per 900 s leaves
        # 80 points = 4800 s. Keeping the pre-reset 80/90 would read as falling.
        samples = _samples((-1500, 80.0), (-1200, 90.0), (-900, 5.0), (0, 20.0))
        assert _project(samples, pct=20.0) == pace.Projection("out", 4800.0)

    def test_projection_ignores_samples_older_than_lookback(self):
        # Inside the 30-minute lookback: 40 → 50 over 1200 s → 50 points in 6000 s.
        samples = _samples((-3000, 0.0), (-1200, 40.0), (0, 50.0))
        assert _project(samples) == pace.Projection("out", 6000.0)

    def test_projection_out_eta(self):
        # 20 points per 1200 s; 40 points to go → 2400 s, reset 4h away.
        samples = _samples((-1200, 40.0), (0, 60.0))
        result = _project(samples, pct=60.0, resets_at_ts=NOW + 4 * 3600)
        assert result == pace.Projection("out", 2400.0)

    def test_projection_lasts_when_eta_after_reset(self):
        samples = _samples((-1200, 40.0), (0, 60.0))
        result = _project(samples, pct=60.0, resets_at_ts=NOW + 1800)
        assert result == pace.Projection("lasts", None)

    def test_projection_eta_counts_time_since_last_sample(self):
        samples = _samples((-1200, 40.0), (0, 60.0))
        assert _project(samples, now=NOW + 600, pct=60.0) == pace.Projection("out", 1800.0)
        assert _project(samples, now=NOW + 5000, pct=60.0) == pace.Projection("out", 0.0)

    def test_projection_none_when_maxed(self):
        samples = _samples((-1200, 40.0), (0, 100.0))
        assert _project(samples, pct=100.0) is None
