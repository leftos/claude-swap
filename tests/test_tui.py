"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap.autoswitch import (
    ErrorEvent,
    NoSwitchEvent,
    PollEvent,
    SleepEvent,
    SwitchEvent,
)
from claude_swap.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import data as tui_data
from claude_swap.usage_store import UsageEntry


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def make_entry(
    pct5: float | None = 25.0,
    pct7: float | None = 10.0,
    *,
    sentinel: str | None = None,
    age_s: float = 5.0,
    scoped: list[tuple[str, float]] | None = None,
    spend: dict | None = None,
) -> UsageEntry:
    """``pct5``/``pct7`` of None omit that window (e.g. annual plans lack 7d)."""
    if sentinel is not None:
        return UsageEntry(sentinel=sentinel)
    last_good: dict = {}
    if pct5 is not None:
        last_good["five_hour"] = {"pct": pct5, "resets_at": _iso_in(7200)}
    if pct7 is not None:
        last_good["seven_day"] = {"pct": pct7, "resets_at": _iso_in(86400 * 3)}
    if scoped is not None:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(86400 * 2)}
            for name, pct in scoped
        ]
    if spend is not None:
        last_good["spend"] = spend
    return UsageEntry(
        last_good=last_good,
        fetched_at=time.time() - age_s,
        age_s=age_s,
    )


def make_account(
    number: int | str,
    *,
    active: bool = False,
    switchable: bool = True,
    kind: str = "oauth",
    entry: UsageEntry | None = None,
    email: str | None = None,
    alias: str = "",
    disabled: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email or f"user{number}@example.com",
        org_name="",
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=switchable,
        usage=entry if entry is not None else make_entry(),
        alias=alias,
        disabled=disabled,
    )


def make_usage_at(
    fetched_at: float | None,
    pct: float = 25.0,
    *,
    sentinel: str | None = None,
) -> UsageEntry:
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(7200)}},
        fetched_at=fetched_at,
        age_s=(time.time() - fetched_at) if fetched_at is not None else None,
    )


class FakeSwitcher:
    """Structured-surface stand-in for ClaudeAccountSwitcher."""

    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        self._accounts = list(accounts)
        self.backup_dir = backup_dir
        self.active = next(
            (a.number for a in accounts if a.is_active), None
        )
        self.calls: list[tuple] = []
        self.fetch_sets: list[set[str] | None] = []

    # -- surface the TUI consumes ------------------------------------------

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        return AccountsSnapshot(
            active_number=self.active,
            accounts=tuple(self._accounts),
            taken_at=time.time(),
        )

    def current_account_number(self) -> str | None:
        return self.active

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.calls.append(("switch_to", str(identifier)))
        old = self.active
        self.active = str(identifier)
        self._accounts = [
            dataclasses.replace(a, is_active=(a.number == self.active))
            for a in self._accounts
        ]
        return {
            "switched": True,
            "from": {"number": int(old) if old else None, "email": ""},
            "to": {
                "number": int(identifier),
                "email": f"user{identifier}@example.com",
            },
            "reason": "requested",
        }

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict:
        self.calls.append(("switch", strategy))
        return {"switched": False, "from": None, "to": None, "reason": "no-better-target"}

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        self.calls.append(("remove", str(identifier), assume_yes))
        self._accounts = [a for a in self._accounts if a.number != str(identifier)]
        print(f"Removed account {identifier}")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self.calls.append(("set_disabled", str(identifier), disabled))
        self._accounts = [
            dataclasses.replace(a, disabled=disabled)
            if a.number == str(identifier)
            else a
            for a in self._accounts
        ]
        verb = "Disabled" if disabled else "Enabled"
        print(f"{verb} Account-{identifier}")

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        self.calls.append(("add", slot, assume_yes))
        print("Added Account 9: fresh@example.com")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.calls.append(("add_token", token, email, slot, assume_yes))
        print(f"Added Account {slot or 9}")

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None


class BlockingSnapshotSwitcher(FakeSwitcher):
    """Fake switcher with independently gated normal/store snapshot lanes."""

    def __init__(
        self,
        normal_account: AccountSnapshot,
        store_account: AccountSnapshot,
        backup_dir: Path,
    ):
        super().__init__([normal_account], backup_dir)
        self.normal_account = normal_account
        self.store_account = store_account
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.normal_done = threading.Event()
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.store_done = threading.Event()
        self.block_store = False

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if fetch is None:
            self.normal_started.set()
            self.normal_release.wait(timeout=2)
            self.normal_done.set()
            account = self.normal_account
        else:
            self.store_started.set()
            if self.block_store:
                self.store_release.wait(timeout=2)
            self.store_done.set()
            account = self.store_account
        return AccountsSnapshot(
            active_number=account.number,
            accounts=(account,),
            taken_at=time.time(),
        )


def make_app(fake: FakeSwitcher):
    from claude_swap.tui.app import CswapApp

    return CswapApp(fake)


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until its screen stops
    it, so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)
    await pilot.pause()
    await pilot.pause()


async def wait_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


async def menu_select(pilot, action_id: str) -> None:
    """Drive the dashboard menu: highlight the entry by id, press Enter."""
    from textual.widgets import ListView

    from claude_swap.tui.widgets import MenuItem

    menu = pilot.app.screen.query_one("#menu", ListView)
    items = list(menu.query(MenuItem))
    menu.index = next(
        i for i, item in enumerate(items) if item.action_id == action_id
    )
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def to_dashboard(pilot) -> None:
    """The app boots on the live screen; Esc leaves it for the dashboard."""
    from claude_swap.tui.autoview import LiveScreen
    from claude_swap.tui.dashboard import DashboardScreen

    await settle(pilot)
    assert isinstance(pilot.app.screen, LiveScreen)
    await pilot.press("escape")
    await settle(pilot)
    assert isinstance(pilot.app.screen, DashboardScreen)


# ---------------------------------------------------------------------------
# Data service units (sync)
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_duration(self):
        assert tui_data.format_duration(42) == "42s"
        assert tui_data.format_duration(180) == "3m"
        assert tui_data.format_duration(7980) == "2h 13m"
        assert tui_data.format_duration(3600 * 26) == "1d 2h"

    def test_format_age_fresh_is_silent(self):
        # Ages inside the serve TTL are the polling cadence at work, not
        # staleness worth flagging.
        assert tui_data.format_age(3.0) is None
        assert tui_data.format_age(120) is None
        assert tui_data.format_age(None) is None
        assert tui_data.format_age(400) == "· 6m ago"

    def test_sentinel_labels_match_cswap_list(self):
        # The TUI must describe sentinel states with the exact wording `cswap
        # list` prints — owned-and-expired means Claude Code refreshes the
        # active account, not that the user must re-login.
        assert (
            tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            == "token expired — refresh deferred this pass; retries automatically"
        )
        from claude_swap.switcher import SENTINEL_NOTES

        for sentinel, note in SENTINEL_NOTES.items():
            assert tui_data.sentinel_label(sentinel) == note
        assert tui_data.sentinel_label("unknown state") == "unknown state"

    def test_sentinel_card_shows_last_seen_like_cswap_list(self):
        # A sentinel is a live overlay — the entry can still carry the last
        # good measurement, and `cswap list` prints it as a "last seen" line.
        # The card must too (except for API-key accounts, which have no quota).
        from claude_swap.tui.widgets import account_card_text

        entry = UsageEntry(
            sentinel=USAGE_TOKEN_EXPIRED,
            last_good={"five_hour": {"pct": 53.0}},
            fetched_at=time.time() - 720,
            age_s=720.0,
        )
        card = account_card_text(make_account(1, active=True, entry=entry), 80).plain
        assert "token expired — refresh deferred this pass; retries automatically" in card
        assert "last seen 53% used" in card

        no_history = account_card_text(
            make_account(1, entry=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)), 80
        ).plain
        assert "last seen" not in no_history

        api_key = account_card_text(
            make_account(
                1,
                kind="api_key",
                entry=dataclasses.replace(entry, sentinel=USAGE_API_KEY),
            ),
            80,
        ).plain
        assert "last seen" not in api_key

    def test_account_card_uses_light_palette_when_passed(self):
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette
        from claude_swap.tui.widgets import account_card_text

        acc = make_account(1, active=True, entry=make_entry(pct5=95.0))
        text = account_card_text(acc, 100, palette=Palette.from_theme(CSWAP_LIGHT))
        styles = {str(span.style) for span in text.spans}
        assert any(ACCENT_LIGHT in s for s in styles)  # active marker uses light accent

    def test_window_helpers(self):
        entry = make_entry(pct5=47.0)
        assert tui_data.window_pct(entry.last_good, "five_hour") == 47.0
        assert tui_data.window_pct(None, "five_hour") is None
        text = tui_data.window_reset_text(entry.last_good, "five_hour", time.time())
        assert text is not None and text.startswith("resets ")
        assert tui_data.window_reset_text(None, "five_hour", time.time()) is None

    def test_reset_clock(self):
        # Same-day reset → bare HH:MM; a reset days out carries its date.
        now = time.time()
        entry = make_entry()  # 5h resets in 2h, 7d in 3d
        clock5 = tui_data.reset_clock(entry.last_good["five_hour"], now)
        assert clock5 is not None and clock5.count(":") == 1
        clock7 = tui_data.reset_clock(entry.last_good["seven_day"], now)
        import calendar

        months = list(calendar.month_abbr)[1:]
        assert clock7 is not None and any(m in clock7 for m in months)

    def test_reset_clock_unknown_or_elapsed_is_none(self):
        now = time.time()
        assert tui_data.reset_clock(None, now) is None
        assert tui_data.reset_clock({"pct": 5.0}, now) is None
        assert tui_data.reset_clock({"resets_at": "garbage"}, now) is None
        # elapsed reset: the row says "resets now" — no clock to show
        elapsed = {"resets_at": _iso_in(-60)}
        assert tui_data.reset_clock(elapsed, now) is None
        assert tui_data.reset_text(elapsed, now) == "resets now"


class TestSnapshotSource:
    def _source(self, tmp_path: Path, accounts=None):
        fake = FakeSwitcher(
            accounts
            or [make_account(1, active=True), make_account(2)],
            tmp_path,
        )
        return fake, tui_data.SnapshotSource(fake)

    def test_every_pass_is_store_governed(self, tmp_path):
        # Pacing lives in the usage store (poll plans + freshness + atomic
        # reservation), so every take is the same on-demand pass `cswap list`
        # runs — including the user's explicit refresh, which cannot bypass
        # the store's per-account cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]

    def test_expired_sentinel_retained_until_fetched_at_advances(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fresh_same_stamp = make_account(1, active=True, entry=make_usage_at(100.0))
        fresh_new_stamp = make_account(1, active=True, entry=make_usage_at(101.0))
        fake, source = self._source(tmp_path, [expired])

        assert source.take().accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_same_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_new_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_expired_sentinel_clears_on_superseding_sentinel(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        api_key = make_account(
            1,
            active=True,
            kind="api_key",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [api_key]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_API_KEY

    def test_expired_sentinel_clears_on_identity_replacement(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            email="old@example.com",
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        replacement = make_account(
            1,
            active=True,
            email="new@example.com",
            entry=make_usage_at(100.0),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [replacement]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_late_worker_fetched_at_regression_is_rejected(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        snap = source.take(store_only=True)
        usage = snap.accounts[0].usage
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0

    def test_late_expired_sentinel_cannot_replace_newer_usage(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, pct=10.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        usage = source.take(store_only=True).accounts[0].usage
        assert usage.sentinel is None
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0


class TestUsageRows:
    """The card's rows must mirror the CLI's _format_usage_lines semantics."""

    def test_absent_window_produces_no_row(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0, pct7=None)  # annual plan: no 7d window
        labels = [label for label, *_ in usage_rows(entry.last_good, time.time(), ())]
        assert labels == ["5h"]

    def test_scoped_models_and_over_limit_marker(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(scoped=[("Fable", 100.0), ("Opus", 12.0)])
        rows = usage_rows(entry.last_good, time.time(), ())
        labels = [label for label, *_ in rows]
        assert labels == ["5h", "7d", "Fable", "Opus"]
        fable = next(row for row in rows if row[0] == "Fable")
        assert "(!)" in fable[2]
        # the marker stays terminal in the clock-extended variant too
        assert fable[3].endswith("(!)") and " · " in fable[3]

    def test_spend_row_first_with_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(spend={"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"})
        rows = usage_rows(entry.last_good, time.time(), ())
        assert rows[0][0] == "$$"
        assert "$12.50 / $50.00" in rows[0][2]

    def test_suffix_full_extends_countdown_with_clock(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0)
        row5 = usage_rows(entry.last_good, time.time(), ())[0]
        assert row5[2].startswith("resets ")
        assert row5[3].startswith(row5[2] + " · ")

    def test_spend_clock_sits_with_reset_not_after_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        spend = usage_rows(entry.last_good, time.time(), ())[0]
        assert spend[0] == "$$"
        assert " · " in spend[3]
        assert spend[3].index(" · ") < spend[3].index("$12.50")

    def test_no_data_no_rows(self):
        from claude_swap.tui.widgets import usage_rows

        assert usage_rows(None, time.time(), ()) == []
        assert usage_rows({}, time.time(), ()) == []

    def test_far_ahead_windows_carry_no_text_marker(self):
        # 1 day into the week at 50% is far ahead of pace: the color says so,
        # the suffix carries no "(ahead of pace)" text.
        from claude_swap.tui.theme import Palette
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {
            "seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)},
            "scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso_in(86400 * 6)}],
        }
        for row in usage_rows(last_good, now, ()):
            assert "pace" not in row[2] and "pace" not in row[3]
            assert row[4] == Palette.DARK.pace_far

    def test_maxed_scoped_keeps_marker_without_projection(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso_in(86400 * 6)}]}
        history = ((now - 1200, {"Fable": 80.0}), (now, {"Fable": 100.0}))
        row = usage_rows(last_good, now, history)[0]
        assert row[2].endswith("  (!)")
        assert "out in" not in row[2] and "lasts" not in row[2] and "idle" not in row[2]

    @staticmethod
    def _rising(now: float, key: str) -> tuple:
        # 20 points in the last 20 minutes: 40 points to go at 60% = 40 minutes
        return ((now - 1200, {key: 40.0}), (now, {key: 60.0}))

    def test_usage_rows_shows_out_in_projection(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"five_hour": {"pct": 60.0, "resets_at": _iso_in(3600 * 4)}}
        row = usage_rows(last_good, now, self._rising(now, "five_hour"))[0]
        assert row[2].startswith("resets ")
        assert row[2].endswith("  out in 40m")
        assert " · " in row[3] and row[3].endswith("  out in 40m")
        scoped = {"scoped": [{"name": "Fable", "pct": 60.0, "resets_at": _iso_in(86400 * 6)}]}
        fable = usage_rows(scoped, now, self._rising(now, "Fable"))[0]
        assert fable[2].endswith("  out in 40m")

    def test_usage_rows_shows_lasts_to_reset(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        # 40 minutes to 100%, but the window resets in 30
        last_good = {"five_hour": {"pct": 60.0, "resets_at": _iso_in(1800)}}
        row = usage_rows(last_good, now, self._rising(now, "five_hour"))[0]
        assert row[2].startswith("resets ") and row[2].endswith("  lasts to reset")
        assert row[3].endswith("  lasts to reset")

    def test_usage_rows_shows_idle(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 10.0, "resets_at": _iso_in(86400 * 3)}}
        history = ((now - 1500, {"seven_day": 10.0}), (now, {"seven_day": 10.0}))
        row = usage_rows(last_good, now, history)[0]
        assert row[2].startswith("resets ") and row[2].endswith("  idle")

    def test_usage_rows_no_projection_without_history(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        entry = make_entry(pct5=60.0, scoped=[("Fable", 20.0)])
        rows = usage_rows(entry.last_good, now, ())
        assert [row[0] for row in rows] == ["5h", "7d", "Fable"]
        for label, _pct, suffix, suffix_full, _color in rows:
            assert suffix.startswith("resets ") and "  " not in suffix, label
            assert "  " not in suffix_full, label

    @staticmethod
    def _styles_of(text, needle: str) -> set[str]:
        return {
            str(span.style)
            for span in text.spans
            if needle in text.plain[span.start : span.end]
        }

    def test_card_bar_uses_pace_color(self):
        from claude_swap.tui.theme import Palette
        from claude_swap.tui.widgets import account_card_text

        def card_for(pct: float, resets_in: float):
            entry = UsageEntry(
                last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(resets_in)}},
                fetched_at=time.time(),
                age_s=0.0,
            )
            return account_card_text(make_account(1, active=True, entry=entry), 100)

        # halfway through the 5h window at 50%: on pace
        on_pace = card_for(50.0, 2.5 * 3600)
        assert self._styles_of(on_pace, "━") == {Palette.DARK.pace_on}
        assert self._styles_of(on_pace, " 50%") == {Palette.DARK.pace_on}
        # 1h into the window at 90%: 70 points ahead, past twice the margin
        far = card_for(90.0, 4 * 3600)
        assert self._styles_of(far, "━") == {Palette.DARK.pace_far}
        assert self._styles_of(far, " 90%") == {Palette.DARK.pace_far}

    def test_spend_row_keeps_severity_color(self):
        from claude_swap.tui.theme import Palette
        from claude_swap.tui.widgets import account_card_text

        spend = {"used": 47.5, "limit": 50.0, "pct": 95.0, "currency": "USD"}
        entry = UsageEntry(last_good={"spend": spend}, fetched_at=time.time(), age_s=0.0)
        card = account_card_text(
            make_account(1, active=True, entry=entry), 100, threshold=90.0
        )
        assert self._styles_of(card, " 95%") == {Palette.DARK.sev_crit}
        assert self._styles_of(card, "━") == {Palette.DARK.sev_crit}
        # the auto-switch threshold tick belongs to usage windows, not spend
        assert "┃" not in card.plain

    def test_card_shows_clock_only_where_it_fits(self):
        # Per-row degradation: the wide card shows every clock, a mid width
        # keeps 5h/7d clocks while the longer spend row falls back to its
        # countdown, and a narrow card is exactly the old countdown-only look.
        from claude_swap.tui.widgets import account_card_text

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        acc = make_account(1, active=True, entry=entry)

        wide = account_card_text(acc, 100).plain
        assert wide.count(" · ") == 3

        mid_lines = account_card_text(acc, 78).plain.splitlines()
        spend_line = next(line for line in mid_lines if "$12.50" in line)
        assert " · " not in spend_line
        for line in mid_lines:
            if "resets" in line and "$12.50" not in line:
                assert " · " in line

        narrow = account_card_text(acc, 40).plain
        assert " · " not in narrow


class TestMiniAccountText:
    def test_far_ahead_week_is_colored_not_marked(self):
        from claude_swap.tui.theme import Palette
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=now,
            age_s=0.0,
        )
        text = mini_account_text(make_account(1, entry=entry), now)
        assert "ahead" not in text.plain
        styles = {
            str(span.style) for span in text.spans if text.plain[span.start : span.end] == "50%"
        }
        assert styles == {Palette.DARK.pace_far}

    def test_window_without_reset_keeps_severity_color(self):
        from claude_swap.tui.theme import Palette
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"five_hour": {"pct": 92.0}}, fetched_at=now, age_s=0.0
        )
        text = mini_account_text(make_account(1, entry=entry), now)
        styles = {
            str(span.style) for span in text.spans if text.plain[span.start : span.end] == "92%"
        }
        assert styles == {Palette.DARK.sev_crit}


class TestRunAction:
    def test_captures_output_and_payload(self):
        def fn():
            print("hello")
            return {"switched": True}

        result = tui_data.run_action(fn)
        assert result.ok and result.payload == {"switched": True}
        assert "hello" in result.output

    def test_switch_error_is_captured_not_raised(self):
        from claude_swap.exceptions import ClaudeSwitchError

        def fn():
            raise ClaudeSwitchError("boom")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "boom" in result.output

    def test_unexpected_input_becomes_eoferror(self):
        def fn():
            input("should not block")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "interactive input" in result.output

    def test_first_line_strips_ansi(self):
        def fn():
            print("\x1b[1mBold headline\x1b[0m")

        assert tui_data.run_action(fn).first_line == "Bold headline"


# ---------------------------------------------------------------------------
# Pilot tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDashboard:
    async def test_panel_shows_active_full_and_others_mini(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(47.0, 63.0)),
                make_account(2, entry=make_entry(92.0, 71.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "user1@example.com" in panel and "● active" in panel
            assert "resets" in panel  # the active card is the full one
            assert "user2@example.com" in panel and "92%" in panel
            # the mini line has no bars — bar glyphs only in the active card
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "━" not in mini_part

    async def test_disabled_marker_on_active_card_and_mini(self, tmp_path):
        # A disabled account is still shown; it's just annotated so the user
        # can see it's held out of auto-rotation — on the full card when it's
        # the active login, and on the one-line form otherwise.
        fake = FakeSwitcher(
            [
                make_account(1, active=True, disabled=True),
                make_account(2, disabled=True),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "● active" in panel  # still the active card
            # both the active card and the mini row carry the marker
            assert panel.count("(disabled)") == 2

    async def test_active_card_skips_absent_window_and_shows_scoped(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(
                    1,
                    active=True,
                    entry=make_entry(pct5=47.0, pct7=None, scoped=[("Fable", 62.0)]),
                )
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "5h" in panel
            assert "7d" not in panel  # annual plan: no invented row
            assert "usage unknown" not in panel
            assert "Fable" in panel and "62%" in panel

    async def test_mini_line_skips_absent_window(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, entry=make_entry(pct5=92.0, pct7=None)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "5h 92%" in mini_part
            assert "7d" not in mini_part

    async def test_menu_is_default_navigation_and_nests(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == [
                "switch",
                "live",
                "add-menu",
                "disable-menu",
                "remove-menu",
                "theme-menu",
                "quit",
            ]
            # nest into Add (index 2), then back out with escape
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == ["add-login", "add-token", "back"]
            await pilot.press("escape")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_remove_menu_shows_alias_before_email(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, alias="dev"),
                make_account(2, email="plain@example.com"),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            from textual.widgets import Static

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("dev (user1@example.com)" in label for label in labels)
            assert any("plain@example.com" in label for label in labels)
            assert not any("(plain@example.com)" in label for label in labels)

    async def test_remove_menu_label_renders_bracket_tag_literally(self, tmp_path):
        # The remove menu labels each account with `[{display_tag}]`, and an
        # org name of "red" makes that literally "[red]" — a valid Rich
        # color markup tag. MenuItem must render it as text, not consume it
        # as styling (which would silently drop the tag from the label).
        fake = FakeSwitcher(
            [dataclasses.replace(make_account(1, active=True), org_name="red")],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("[red]" in label for label in labels)

    async def test_back_menu_entry_pops_submenu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "back")
            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_vim_keys_move_menu_cursor(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            assert menu.index == 0
            await pilot.press("j")
            assert menu.index == 1
            await pilot.press("k")
            assert menu.index == 0

    async def test_s_opens_switch_screen_and_enter_switches(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, SwitchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            items = list(listview.query(AccountItem))
            assert [item.number for item in items] == ["1", "2"]
            assert listview.index == 0  # starts on the active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, DashboardScreen)  # popped back
            assert app.snapshot.active_number == "2"

    async def test_switch_screen_escape_backs_out(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await pilot.press("enter")  # menu: Switch account…
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen

            assert isinstance(app.screen, SwitchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_remove_via_menu_confirms_then_removes(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_remove_via_menu_cancel_is_safe(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:1")
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)

    async def test_disable_via_menu_toggles_without_confirm(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "disable-menu")
            await menu_select(pilot, "disable:2")  # no modal — direct action
            await settle(pilot)
            assert ("set_disabled", "2", True) in fake.calls
            # the submenu pops back to root after the toggle
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_disable_menu_row_reflects_state_and_re_enables(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2, disabled=True)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "disable-menu")
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            # the already-disabled account offers to enable; the active one to disable
            assert any("(disabled)" in label and "enable" in label for label in labels)
            assert any("disable" in label and "(disabled)" not in label for label in labels)
            # selecting the disabled account flips it back on
            await menu_select(pilot, "disable:2")
            await settle(pilot)
            assert ("set_disabled", "2", False) in fake.calls

    async def test_modal_arrow_keys_choose_button(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")  # → confirm modal
            # focus starts on the confirm button; → moves to Cancel, enter presses it
            await pilot.press("right", "enter")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)
            # reopen (menu index still on account 2), ← back to confirm, press it
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("right", "left", "enter")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_full_refresh_binding(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert fake.fetch_sets[-1] is None  # full on-demand pass

    async def test_add_token_via_menu_passes_assume_yes(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "5"
            await pilot.click("#add")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat01-test", None, 5, True) in fake.calls

    async def test_add_token_occupied_slot_asks_first(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await to_dashboard(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "2"
            await pilot.click("#add")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)  # overwrite confirm
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "add_token" for call in fake.calls)

    async def test_empty_state_hint_in_panel(self, tmp_path):
        fake = FakeSwitcher([], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "No managed accounts yet" in panel

    async def test_palette_is_disabled(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        assert CswapApp.ENABLE_COMMAND_PALETTE is False


def fake_calls(app) -> list[tuple]:
    return app.switcher.calls


class _FakeEngine:
    """Stands in for AutoSwitchEngine: records construction, blocks until stop."""

    instances: list["_FakeEngine"] = []
    # Emit one decision as the loop starts, as the real engine's first tick does.
    emit_on_start = True

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **kwargs):
        self.settings = settings
        self.on_event = on_event
        self.dry_run = dry_run
        self.stopped = False
        self.applied_thresholds: list[float] = []
        self.wakes = 0
        self._stop = threading.Event()
        _FakeEngine.instances.append(self)

    def run_loop(self) -> int:
        if _FakeEngine.emit_on_start:
            self.on_event(NoSwitchEvent(reason="cooldown"))
        self._stop.wait(30)
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        self.settings = dataclasses.replace(self.settings, threshold=threshold)
        self.applied_thresholds.append(threshold)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    """Every app boots onto the live screen, so no test runs the real engine."""
    _FakeEngine.instances = []
    _FakeEngine.emit_on_start = True
    monkeypatch.setattr(
        "claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


@pytest.fixture
def no_live_screen(monkeypatch):
    """Boot onto an empty screen instead of the live one, so poll-lane tests
    see the app's poller without the live screen's store-only switch."""
    from textual.screen import Screen

    class _InertScreen(Screen):
        pass

    monkeypatch.setattr("claude_swap.tui.app.LiveScreen", _InertScreen)


async def wait_for(pilot, condition, timeout: float = 2.0) -> None:
    """Pause the pilot until ``condition()`` holds (thread-delivered updates)."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await pilot.pause(0.02)


def rendered(app, selector: str) -> str:
    from textual.widgets import Static

    return app.screen.query_one(selector, Static).render().plain


def write_settings(tmp_path: Path, payload: dict) -> None:
    (tmp_path / "settings.json").write_text(json.dumps(payload))


def read_settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "settings.json").read_text())


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_live_screen")
class TestPollLanes:
    async def test_blocked_normal_allows_store_only_repaint_without_stale_overpaint(
        self, tmp_path
    ):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0
            assert fake.fetch_sets == [None, set()]

    async def test_late_normal_can_advance_usage_after_store_repaint(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        store = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 10.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

    async def test_repeated_ticks_keep_store_lane_single_flight(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        fake.block_store = True
        app = make_app(fake)

        async with app.run_test(size=(100, 40)):
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_started)
            app._tick()
            app._tick()
            assert fake.fetch_sets == [None, set()]
            fake.store_release.set()
            fake.normal_release.set()
            await wait_event(fake.store_done)
            await wait_event(fake.normal_done)

    async def test_store_only_mode_launches_only_store_lane(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            fake.fetch_sets.clear()
            app.set_store_only(True)
            await settle(pilot)
            assert fake.fetch_sets == [set()]


@pytest.mark.asyncio
class TestLiveScreen:
    def _fake(self, tmp_path, accounts=None):
        return FakeSwitcher(
            accounts or [make_account(1, active=True), make_account(2)], tmp_path
        )

    # -- boot, navigation, lifecycle -----------------------------------------

    async def test_boot_opens_live_screen_over_dashboard(self, tmp_path, fake_engine):
        from claude_swap.tui.autoview import LiveScreen
        from claude_swap.tui.dashboard import DashboardScreen

        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert isinstance(app.screen, LiveScreen)
            assert app._store_only is True  # the engine is the only fetcher
            assert len(fake_engine.instances) == 1
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            assert fake_engine.instances[0].stopped is True
            assert app._store_only is False
            assert fake._poll_inputs_override is None

    async def test_menu_has_single_watch_auto_entry(self, tmp_path):
        from textual.widgets import ListView, Static

        from claude_swap.tui.autoview import LiveScreen
        from claude_swap.tui.widgets import MenuItem

        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await to_dashboard(pilot)
            menu = app.screen.query_one("#menu", ListView)
            items = list(menu.query(MenuItem))
            ids = [item.action_id for item in items]
            assert ids.count("live") == 1
            assert "watch" not in ids and "auto" not in ids
            assert ids.index("live") == 1  # right under "Switch account…"
            label = items[1].query_one(Static).render().plain
            assert label == "Watch & auto-switch"
            await menu_select(pilot, "live")
            assert isinstance(app.screen, LiveScreen)

    async def test_w_and_g_open_live_screen(self, tmp_path, fake_engine):
        from claude_swap.tui.autoview import LiveScreen
        from claude_swap.tui.dashboard import DashboardScreen

        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await to_dashboard(pilot)
            await pilot.press("w")
            await settle(pilot)
            assert isinstance(app.screen, LiveScreen)
            depth = len(app.screen_stack)
            app.action_open_live()  # already there: no second copy stacked
            await pilot.pause()
            assert len(app.screen_stack) == depth
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            await pilot.press("g")
            await settle(pilot)
            assert isinstance(app.screen, LiveScreen)
            assert app._store_only is True
            assert len(fake_engine.instances) == 3  # boot, w, g

    # -- the account list ------------------------------------------------------

    async def test_all_accounts_render_as_full_cards(self, tmp_path):
        from textual.widgets import ListView

        from claude_swap.tui.widgets import AccountCard, AccountItem

        fake = self._fake(
            tmp_path,
            [make_account(1, active=True), make_account(2), make_account(3)],
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 60)) as pilot:
            await settle(pilot)
            listview = app.screen.query_one("#accounts", ListView)
            items = list(listview.query(AccountItem))
            assert [item.number for item in items] == ["1", "2", "3"]
            for item in items:
                card = item.query_one(AccountCard).render().plain
                assert f"user{item.number}@example.com" in card
                assert "5h" in card and "7d" in card  # full card, not a mini
            assert listview.index is None  # monitor mode: no cursor

    async def test_scroll_keys_leave_no_cursor_and_enter_is_inert(self, tmp_path):
        from textual.widgets import ListView

        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            listview = app.screen.query_one("#accounts", ListView)
            assert (listview.index, app.focused) == (None, None)
            await pilot.press("down", "j", "up", "k")
            await pilot.pause()
            assert listview.index is None  # scrolling, never a cursor
            await pilot.press("enter")  # nothing armed: inert
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        from textual.widgets import ListView

        from claude_swap.tui.autoview import LiveScreen

        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index == 0  # cursor armed, on the active account
            assert rendered(app, "#list-title") == LiveScreen._SELECT_TITLE
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, LiveScreen)  # stayed watching
            assert app.screen.query_one("#accounts", ListView).index is None
            assert app.snapshot.active_number == "2"
            assert rendered(app, "#list-title").startswith("watching all accounts")

    async def test_escape_disarms_then_leaves(self, tmp_path):
        from textual.widgets import ListView

        from claude_swap.tui.autoview import LiveScreen
        from claude_swap.tui.dashboard import DashboardScreen

        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")  # disarm selection only
            await pilot.pause()
            assert isinstance(app.screen, LiveScreen)
            assert app.screen.query_one("#accounts", ListView).index is None
            await pilot.press("escape")  # now leave
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_title_shows_snapshot_age_and_long_refresh(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            # Fresh snapshots stay quiet; the age note is a staleness alarm.
            assert rendered(app, "#list-title") == "watching all accounts"
            app.snapshot = dataclasses.replace(
                app.snapshot, taken_at=time.time() - app.SNAPSHOT_AGE_NOTE_S - 1.0
            )
            app._update_refresh_status()
            await pilot.pause()
            assert "snapshot 1m ago" in rendered(app, "#list-title")
            app._normal_refreshing = True
            app._normal_started_at = time.time() - app.POLL_INTERVAL_S - 1.0
            app._update_refresh_status()
            await pilot.pause()
            assert "refreshing" in rendered(app, "#list-title")

    # -- engine mode -------------------------------------------------------------

    async def test_auto_live_false_starts_dry_run(self, tmp_path, fake_engine):
        from claude_swap.tui.autoview import LiveScreen

        write_settings(tmp_path, {"ui": {"autoLive": False}})
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert isinstance(app.screen, LiveScreen)
            assert [e.dry_run for e in fake_engine.instances] == [True]
            assert rendered(app, "#mode-badge").strip() == "DRY-RUN"

    async def test_missing_setting_starts_dry_run(self, tmp_path, fake_engine):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert [e.dry_run for e in fake_engine.instances] == [True]

    async def test_auto_live_true_starts_live_without_modal(
        self, tmp_path, fake_engine
    ):
        from claude_swap.tui.autoview import LiveScreen

        write_settings(tmp_path, {"ui": {"autoLive": True}})
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert isinstance(app.screen, LiveScreen)  # no confirm modal on boot
            assert [e.dry_run for e in fake_engine.instances] == [False]
            badge = app.screen.query_one("#mode-badge")
            assert rendered(app, "#mode-badge").strip() == "LIVE"
            assert badge.has_class("live")

    async def test_engine_start_note_in_last_decision(self, tmp_path, fake_engine):
        fake_engine.emit_on_start = False
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert rendered(app, "#last-decision") == (
                "— engine started: DRY-RUN (watching only) —"
            )

    async def test_go_live_confirm_persists_true(self, tmp_path, fake_engine):
        from claude_swap.tui.modals import ConfirmModal

        fake_engine.emit_on_start = False
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")  # cancelled: still dry-run, nothing saved
            await settle(pilot)
            assert len(fake_engine.instances) == 1
            assert not (tmp_path / "settings.json").exists()
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            assert fake_engine.instances[1].dry_run is False
            assert read_settings(tmp_path)["ui"] == {"autoLive": True}
            assert rendered(app, "#mode-badge").strip() == "LIVE"
            assert rendered(app, "#last-decision") == (
                "— engine started: LIVE (will switch accounts) —"
            )

    async def test_toggle_back_to_dry_run_persists_false(self, tmp_path, fake_engine):
        from claude_swap.tui.autoview import LiveScreen

        write_settings(tmp_path, {"ui": {"autoLive": True}})
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")  # live → dry-run needs no confirmation
            await settle(pilot)
            assert isinstance(app.screen, LiveScreen)
            assert [e.dry_run for e in fake_engine.instances] == [False, True]
            assert fake_engine.instances[0].stopped is True
            assert read_settings(tmp_path)["ui"] == {"autoLive": False}
            assert rendered(app, "#mode-badge").strip() == "DRY-RUN"

    async def test_f_wakes_engine(self, tmp_path, fake_engine):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            before = len(fake.fetch_sets)
            await pilot.press("f")
            await settle(pilot)
            assert fake_engine.instances[0].wakes == 1
            # the app poller stays store-only: no full fetch of its own
            assert all(fetch == set() for fetch in fake.fetch_sets[before:])

    # -- last decision -----------------------------------------------------------

    async def test_engine_event_reaches_last_decision(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            # the fake engine's first decision arrives via call_from_thread
            await wait_for(
                pilot, lambda: "no switch: cooldown" in rendered(app, "#last-decision")
            )

    async def test_last_decision_shows_latest_non_quiet_event(
        self, tmp_path, fake_engine
    ):
        fake_engine.emit_on_start = False
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            switch = SwitchEvent(
                trigger="proactive",
                from_ref={"number": 1, "email": "user1@example.com"},
                to_ref={"number": 2, "email": "user2@example.com"},
            )
            screen._on_engine_event(switch)
            await pilot.pause()
            assert switch.human() in rendered(app, "#last-decision")
            error = ErrorEvent(message="usage endpoint down")
            screen._on_engine_event(error)
            await pilot.pause()
            line = rendered(app, "#last-decision")
            assert error.human() in line
            assert switch.human() not in line  # one line: the latest only

    async def test_last_decision_ignores_poll_and_sleep(self, tmp_path, fake_engine):
        fake_engine.emit_on_start = False
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            screen._on_engine_event(NoSwitchEvent(reason="cooldown"))
            await pilot.pause()
            assert "no switch: cooldown" in rendered(app, "#last-decision")
            screen._on_engine_event(
                PollEvent(active=None, headroom={"2": 60.0}, threshold=90.0)
            )
            screen._on_engine_event(SleepEvent(seconds=120.0, until="12:00"))
            await pilot.pause()
            assert "no switch: cooldown" in rendered(app, "#last-decision")

    # -- threshold adjust ----------------------------------------------------------

    async def test_threshold_adjust_is_session_only(self, tmp_path, fake_engine):
        fake_engine.emit_on_start = False
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            assert app.threshold_pct == 90.0  # mount syncs to the file value
            await pilot.press("right")  # inert outside adjust mode
            await pilot.pause()
            assert screen._settings.threshold == 90.0
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 93.0
            assert app.threshold_pct == 93.0
            engine = fake_engine.instances[0]
            assert engine.applied_thresholds == [91.0, 92.0, 93.0]
            assert "threshold 93% (session)" in rendered(app, "#auto-summary")
            await pilot.press("enter")
            await pilot.pause()
            assert engine.wakes == 1  # one forced tick on leaving the mode
            assert rendered(app, "#last-decision") == (
                "— threshold set to 93% for this session —"
            )
            # the override lives in memory only — nothing was persisted
            assert not (tmp_path / "settings.json").exists()
            # a dry↔live restart rebuilds the engine from the adjusted copy
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert fake_engine.instances[1].settings.threshold == 93.0
            assert "autoswitch" not in read_settings(tmp_path)
            await pilot.press("escape")
            await settle(pilot)
            # leaving the screen reverts the tick and unpins poll planning
            assert app.threshold_pct == 90.0
            assert fake._poll_inputs_override is None

    async def test_live_cards_show_threshold_tick_and_follow_adjust(
        self, tmp_path, fake_engine
    ):
        from claude_swap.tui.widgets import AccountCard

        fake_engine.emit_on_start = False
        fake = self._fake(
            tmp_path,
            [make_account(1, active=True), make_account(2), make_account(3)],
        )
        app = make_app(fake)

        def assert_ticks_at(threshold: float) -> None:
            # Read the cards' painted lines (not a fresh render()), so a card
            # that was never repainted still shows its old tick.
            cards = list(app.screen.query(AccountCard))
            assert len(cards) == 3
            for card in cards:
                bar_width = max(12, min(30, card.size.width - 42 - 2))
                expected = min(bar_width - 1, round(threshold / 100 * bar_width))
                bar_lines = [
                    line
                    for line in (card.render_line(y).text for y in range(card.size.height))
                    if line.strip().startswith(("5h", "7d"))
                ]
                assert len(bar_lines) == 2  # 5h and 7d, every card
                for line in bar_lines:
                    label = line.strip()[:2]
                    bar_start = line.index(label) + len(label) + 1
                    assert line.count("┃") == 1
                    assert line.index("┃") - bar_start == expected

        async with app.run_test(size=(100, 60)) as pilot:
            await settle(pilot)
            assert app.threshold_pct == 90.0
            assert_ticks_at(90.0)
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert app.threshold_pct == 93.0
            assert_ticks_at(93.0)

    async def test_threshold_adjust_escape_exits_mode_not_screen(
        self, tmp_path, fake_engine
    ):
        from claude_swap.tui.autoview import LiveScreen
        from claude_swap.tui.dashboard import DashboardScreen

        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, LiveScreen)
            # no net change → no forced tick
            assert fake_engine.instances[0].wakes == 0
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)

    async def test_enter_while_adjusting_ends_adjust_without_switching(
        self, tmp_path, fake_engine
    ):
        from textual.widgets import ListView

        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            await pilot.press("s", "down")  # selection armed on account 2
            await pilot.press("t", "right")
            await pilot.pause()
            await pilot.press("enter")
            await settle(pilot)
            assert screen._adjusting is False
            assert fake_engine.instances[0].wakes == 1
            assert not any(call[0] == "switch_to" for call in fake.calls)
            assert screen.query_one("#accounts", ListView).index == 1  # still armed
            await pilot.press("enter")  # now it confirms the selection
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls

    async def test_threshold_clamps_and_keeps_meaningful_decimals(
        self, tmp_path, fake_engine
    ):
        write_settings(
            tmp_path, {"schemaVersion": 1, "autoswitch": {"threshold": 99.0}}
        )
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            await pilot.press("t", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 99.9  # spec's upper bound
            # never a lying "100%"
            assert "threshold 99.9% (session)" in rendered(app, "#auto-summary")
            screen.action_threshold_step(-60.0)
            await pilot.pause()
            assert screen._settings.threshold == 50.0  # spec's lower bound

    # -- candidates ------------------------------------------------------------------

    async def test_candidates_ranked_by_headroom(self, tmp_path):
        fake = self._fake(
            tmp_path,
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(80.0, 10.0)),
                make_account(3, entry=make_entry(15.0, 5.0)),
            ],
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert rendered(app, "#candidates") == "next  3 15% › 2 80%"

    async def test_candidates_ranking_honors_configured_model(self, tmp_path):
        """The ranking must use the same window set as the engine: with
        autoswitch.model set, a Fable-bound account ranks by its Fable pct,
        not its roomy 5h."""
        write_settings(tmp_path, {"schemaVersion": 1, "autoswitch": {"model": "Fable"}})
        fake = self._fake(
            tmp_path,
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(
                    2, entry=make_entry(10.0, 5.0, scoped=[("Fable", 95.0)])
                ),
                make_account(
                    3, entry=make_entry(50.0, 5.0, scoped=[("Fable", 20.0)])
                ),
            ],
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            # On 5h alone #2 (10% used) would rank first; Fable 95% binds it
            # below #3 (50% binding).
            assert rendered(app, "#candidates") == "next  3 50% › 2 95%"

    async def test_candidates_sentinel_unknown_and_api_key(self, tmp_path):
        fake = self._fake(
            tmp_path,
            [
                make_account(1, active=True),
                make_account(2, entry=make_entry(sentinel=USAGE_TOKEN_EXPIRED)),
                make_account(3, entry=make_entry(None, None)),
                make_account(
                    4, kind="api_key", entry=make_entry(sentinel=USAGE_API_KEY)
                ),
                make_account(5, switchable=False, entry=make_entry(5.0, 5.0)),
                make_account(6, entry=make_entry(30.0, 10.0)),
            ],
        )
        app = make_app(fake)
        async with app.run_test(size=(120, 60)) as pilot:
            await settle(pilot)
            expired = tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            api_key = tui_data.sentinel_label(USAGE_API_KEY)
            # measured first, then sentinels, then unknown; unswitchable #5
            # is never offered
            assert rendered(app, "#candidates") == (
                f"next  6 30% › 2 {expired} › 4 {api_key} › 3 unknown"
            )

    async def test_candidates_empty(self, tmp_path):
        app = make_app(self._fake(tmp_path, [make_account(1, active=True)]))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert rendered(app, "#candidates") == "next  no other switchable accounts"

    async def test_theme_change_repaints_candidates(self, tmp_path):
        from textual.widgets import Static

        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)

            def styles() -> set[str]:
                content = app.screen.query_one("#candidates", Static).render()
                return {str(span.style) for span in content.spans}

            before = styles()
            app.theme = "cswap-light" if app.theme == "cswap-dark" else "cswap-dark"
            await pilot.pause()
            assert styles() != before


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_swap.tui.autoview import event_text

        assert event.human() in event_text(event).plain

    def test_event_text_uses_light_accent_for_switch(self):
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(CSWAP_LIGHT))
        assert any(ACCENT_LIGHT in str(s.style) for s in text.spans)


# ---------------------------------------------------------------------------
# accounts_snapshot on the real switcher
# ---------------------------------------------------------------------------


class TestAccountsSnapshot:
    def test_one_pass_snapshot(self, temp_home, mock_claude_config):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "test@example.com", "uuid": "test-uuid-1234"},
            "2": {"email": "other@example.com", "uuid": "uuid-2"},
        }
        switcher._write_json(switcher.sequence_file, data)

        snap = switcher.accounts_snapshot(fetch=set())  # store-only: no network
        assert snap.active_number == "1"
        assert [acc.number for acc in snap.accounts] == ["1", "2"]
        active = snap.accounts[0]
        assert active.is_active and active.email == "test@example.com"
        assert all(acc.kind == "oauth" for acc in snap.accounts)
        # No stored credential backups: nothing is switchable, and usage is
        # sentinel'd rather than fetched.
        assert all(not acc.switchable for acc in snap.accounts)
        assert all(acc.usage.sentinel is not None for acc in snap.accounts)
        assert isinstance(snap.taken_at, float)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_bare_tty_launches_tui(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home):
        import claude_swap.cli as cli

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2  # argparse usage error

    @pytest.mark.parametrize("command", ["watch", "tui"])
    def test_watch_and_tui_open_the_same_tui(self, monkeypatch, temp_home, command):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap", command])
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched


# ---------------------------------------------------------------------------
# Theme wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestThemeWiring:
    async def test_mount_selects_light_theme_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-dark"           # → back to dark

    async def test_theme_menu_marks_current_and_applies(self, tmp_path):
        from textual.widgets import ListView, Static

        from claude_swap.tui.widgets import MenuItem

        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await to_dashboard(pilot)
            assert app._theme_name == "auto"  # default
            await menu_select(pilot, "theme-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [it.query_one(Static).render().plain for it in menu.query(MenuItem)]
            assert any("dark" in lbl for lbl in labels)
            assert any("light" in lbl for lbl in labels)
            current = next(lbl for lbl in labels if "auto" in lbl)
            assert "●" in current  # the current theme is marked
            await menu_select(pilot, "theme:light")
            assert app._theme_name == "light"
            assert app.theme == "cswap-light"

