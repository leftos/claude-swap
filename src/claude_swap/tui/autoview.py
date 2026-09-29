"""Watch & auto-switch: every account live, with the auto-switch engine running.

The screen the TUI opens on. Every account is a full card (the same live list
as the switch screen, flashing when a measurement advances), and below it the
real :class:`AutoSwitchEngine` runs in a thread worker:

- a DRY-RUN / LIVE badge with the threshold and poll interval;
- one line of ranked switch candidates (``next  3 12% › 1 40%``);
- one line holding the engine's latest decision (poll and sleep ticks don't
  replace it).

The engine starts dry-run unless ``ui.autoLive`` is set. ``l`` goes live
after a confirmation (or back to dry-run at once) and saves the choice to
``ui.autoLive``. ``s`` arms selection for a manual switch that keeps the
screen up; ``t`` then ←/→ adjusts the threshold for this session only. The
engine's state file semantics (shared cooldown, quarantine list, state lock)
make it safe to run alongside an external ``cswap auto``. While this screen
is up the app's snapshot poller runs store-only: the engine is the only
fetcher.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, ListView, Static

from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    pct_label,
)
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    SETTING_SPECS,
    load_settings,
    load_ui_settings,
    parse_model_names,
    set_setting,
)
from claude_swap.tui import data
from claude_swap.tui.dashboard import AccountListScreen
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountCard, AccountItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}
# Routine ticks: they never replace the last-decision line.
_TICK_KINDS = {"poll", "sleep"}


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """One line for an engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


class LiveScreen(AccountListScreen):
    """Every account as a full card, with the auto-switch engine below."""

    _WATCH_TITLE = "watching all accounts"
    _SELECT_TITLE = "switch to which account? · enter confirm · esc cancel"
    # Nothing focused while just watching: a focused list would turn the
    # scroll keys into cursor moves. Arming selection focuses it.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        # priority: outranks the focused ListView's own enter binding; the
        # action ends threshold adjust, or confirms an armed selection.
        Binding("enter", "enter", "Confirm", priority=True),
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("f", "wake_engine", "Check now", show=False),
        Binding("escape,q", "back", "Back"),
        Binding("down,j", "nav_down", show=False),
        Binding("up,k", "nav_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        self._selecting = False
        # Session-only threshold adjustment (t, then arrows). Never written
        # to settings.json. ``_configured_threshold`` is the mount-time file
        # value the screen reverts to on exit; ``_entry_threshold`` is the
        # value when adjust mode was entered (wake/announce only on a net
        # change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="accounts")
        with Horizontal(id="auto-bar"):
            yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
            yield Static("", id="auto-summary")
        yield Static("", id="candidates")
        yield Static("", id="last-decision")
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = self._settings.threshold
        self._update_summary()
        self._update_title()
        self.watch(self.app, "refresh_status", self._on_refresh_status)
        self.watch(self.app, "theme", self._on_theme_change)
        super().on_mount()
        self._start_engine(dry_run=not self._auto_live_setting())

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher.clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
            self._repaint_cards()
        self.app.set_store_only(False)

    def _auto_live_setting(self) -> bool:
        try:
            return load_ui_settings(self.app.switcher.backup_dir).auto_live
        except Exception as exc:
            self.app.notify(
                f"Could not read auto-switch mode: {exc}; starting dry-run",
                severity="warning",
            )
            return False

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshot
        if snap is not None:
            self._update_candidates(snap)

    async def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        await super()._on_snapshot(snap)
        if snap is not None:
            self._update_candidates(snap)

    def action_back(self) -> None:
        if self._adjusting:
            self._end_adjust()
        elif self._selecting:
            self._set_selecting(False)
        else:
            self.app.pop_screen()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "threshold_step" and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        if action == "enter" and not (self._adjusting or self._selecting):
            return False  # nothing to confirm while just watching
        return True

    def action_enter(self) -> None:
        if self._adjusting:
            self._end_adjust()
        elif self._selecting:
            self.query_one("#accounts", ListView).action_select_cursor()

    # -- title and selection ------------------------------------------------

    def _title_text(self) -> str:
        if self._selecting:
            return self._SELECT_TITLE
        status = self.app.refresh_status
        return f"{self._WATCH_TITLE} · {status}" if status else self._WATCH_TITLE

    def _update_title(self) -> None:
        self.query_one("#list-title", Static).update(self._title_text())

    def _on_refresh_status(self, _status: str) -> None:
        if not self._selecting:
            self._update_title()

    def _index_after_build(
        self, snap: AccountsSnapshot, first_build: bool, previous: int | None
    ) -> int | None:
        if not self._selecting:
            return None  # monitor mode: no cursor at all
        return super()._index_after_build(snap, first_build, previous)

    def _set_selecting(self, on: bool) -> None:
        self._selecting = on
        listview = self.query_one("#accounts", ListView)
        if on:
            snap = self.app.snapshot
            if snap is not None and snap.accounts:
                listview.index = self._active_index(snap)
            listview.focus()
        else:
            listview.index = None
            self.set_focus(None)
        self._update_title()
        self.refresh_bindings()

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._selecting)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._selecting:
            return  # e.g. a stray click while just watching
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.number)
            self._set_selecting(False)  # stay here, keep watching

    def action_nav_down(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_down()
        else:
            listview.scroll_down(animate=False)

    def action_nav_up(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_up()
        else:
            listview.scroll_up(animate=False)

    # -- threshold adjust mode ------------------------------------------------

    def action_adjust_threshold(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting = True
        self._entry_threshold = self._settings.threshold
        self._update_summary()
        self.refresh_bindings()

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        self._set_threshold(value)

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._update_summary()
        self.refresh_bindings()
        if self._settings.threshold == self._entry_threshold:
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        self._show_note(
            f"— threshold set to {pct_label(self._settings.threshold)}% "
            "for this session —"
        )

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self._repaint_cards()
        self._update_summary()

    def _repaint_cards(self) -> None:
        """Redraw every card so its threshold tick follows ``app.threshold_pct``."""
        for card in self.query(AccountCard):
            card.refresh()

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
            # App start, not engine start: a restart via `l` keeps counting
            # the accounts already polled since the app came up.
            warm_since=self.app.started_at,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        mode = "DRY-RUN (watching only)" if dry_run else "LIVE (will switch accounts)"
        self._show_note(f"— engine started: {mode} —")

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        if event.kind not in _TICK_KINDS:
            palette = Palette.from_theme(self.app.current_theme)
            self._last_decision().update(event_text(event, palette=palette))
        if event.kind == "switch":
            self.app.request_refresh()

    def _show_note(self, note: str) -> None:
        """A screen-side note (engine start, threshold change) in the
        last-decision line."""
        palette = Palette.from_theme(self.app.current_theme)
        self._last_decision().update(Text(note, style=palette.muted))

    def _last_decision(self) -> Static:
        return self.query_one("#last-decision", Static)

    def action_wake_engine(self) -> None:
        # The app poller is store-only here, so a full refresh would fetch
        # nothing: the engine's next tick is the fetch.
        if self._engine is not None:
            self._engine.wake()
            self.app.notify("Checking usage now…", timeout=2)

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)
            self._save_auto_live(False)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)
            self._save_auto_live(True)

    def _save_auto_live(self, live: bool) -> None:
        try:
            set_setting(
                self.app.switcher.backup_dir,
                "ui.autoLive",
                "true" if live else "false",
            )
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.app.notify(
                f"Could not save auto-switch mode: {exc}", severity="warning"
            )

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _update_candidates(self, snap: AccountsSnapshot) -> None:
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets on one line, ranked by remaining headroom (best first)."""
        # Same window set as the engine (autoswitch.model included), so the
        # displayed ranking can never disagree with the account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        models = parse_model_names(self._settings.model) if self._settings else ()
        ranked: list[tuple[float, str]] = []  # (sort key: pct used, number)
        entries: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(acc.usage.last_good, models)
            entry = Text()
            entry.append(f"{acc.number} ", style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(data.sentinel_label(acc.usage.sentinel), style=palette.muted)
                ranked.append((998.0, acc.number))
            elif pct is None:
                entry.append("unknown", style=palette.muted)
                ranked.append((999.0, acc.number))
            else:
                entry.append(f"{pct:.0f}%", style=palette.severity(pct))
                ranked.append((pct, acc.number))
            entries[acc.number] = entry

        text = Text()
        text.append("next  ", style=palette.muted)
        if not ranked:
            text.append("no other switchable accounts", style=palette.muted)
            return text
        for i, (_pct, number) in enumerate(sorted(ranked)):
            if i:
                text.append(" › ", style=palette.muted)
            text.append(entries[number])
        return text
