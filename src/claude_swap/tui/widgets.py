"""Shared render widgets: usage bars, account cards, and the accounts panel.

``bar_cells``/``usage_bar`` are custom renderers rather than Textual's
``ProgressBar`` because the design needs three things the stock widget
doesn't do: a caller-chosen fill color (the pace gradient, see
``window_color``), an optional threshold tick mark (the auto-switch trigger
line), and stale-measurement dimming.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import ListItem, Static

from claude_swap import pace
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.switcher import ERROR_NOTES
from claude_swap.usage_store import STALE_OK_S
from claude_swap.tui import data
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"


def window_color(window: dict, key: str, now: float, palette: Palette) -> str:
    """Pace color for one usage window (``key`` is ``five_hour``, ``seven_day``
    or a scoped window's name); the severity ramp when its reset is unknown."""
    period_s, margin = pace.window_period_and_margin(key)
    delta = pace.pace_delta(window, now=now, period_s=period_s)
    if delta is None:
        return palette.severity(float(window["pct"]))
    return palette.pace_color(delta, margin)


def bar_cells(
    pct: float | None,
    width: int,
    *,
    color: str,
    stale: bool = False,
    threshold: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """Just the bar glyphs: ``color`` fill, track, optional tick."""
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=palette.track)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=palette.sev_warn)
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=palette.track)
    return text


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    color: str,
    stale: bool = False,
    threshold: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """One full bar line: ``5h ━━━━╸────┃──  47%  resets 2h 13m · 20:39``."""
    text = Text()
    text.append(f"{label} ", style=palette.muted)
    text.append(
        bar_cells(
            pct, width, color=color, stale=stale, threshold=threshold, palette=palette
        )
    )
    if pct is None:
        text.append("  usage unknown", style=palette.muted)
    else:
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
    return text


def _reset_parts(window: dict, now: float) -> tuple[str | None, str | None]:
    """Countdown suffix and its clock-extended variant for one window.

    ``("resets 2h 13m", "resets 2h 13m · 20:39")`` — the second form is what
    a row shows when it has the width for it. Equal when no clock is known.
    """
    reset = data.reset_text(window, now)
    if not reset:
        return None, None
    clock = data.reset_clock(window, now)
    return reset, f"{reset} · {clock}" if clock else reset


def _projection_text(
    window: dict, key: str, history: tuple[tuple[float, dict], ...], now: float
) -> str | None:
    """``out in 1h 20m`` / ``lasts to reset`` / ``idle`` at the last 30
    minutes' burn rate, or None without enough recent samples."""
    result = pace.recent_projection(
        history,
        key,
        now=now,
        pct=float(window["pct"]),
        resets_at_ts=parse_reset_ts(window.get("resets_at")),
    )
    if result is None:
        return None
    if result.kind == "out":
        assert result.eta_s is not None
        return f"out in {data.format_duration(result.eta_s)}"
    if result.kind == "lasts":
        return "lasts to reset"
    return "idle"


def _join_suffix(*parts: str | None) -> str:
    return "  ".join(part for part in parts if part)


def _window_row(
    label: str,
    key: str,
    window: dict,
    *,
    history: tuple[tuple[float, dict], ...],
    now: float,
    palette: Palette,
    mark_maxed: bool,
) -> tuple[str, float, str, str, str]:
    pct = float(window["pct"])
    reset, reset_full = _reset_parts(window, now)
    projection = _projection_text(window, key, history, now)
    marker = "(!)" if mark_maxed and pct >= 100 else None
    return (
        label,
        pct,
        _join_suffix(reset, projection, marker),
        _join_suffix(reset_full, projection, marker),
        window_color(window, key, now, palette),
    )


def usage_rows(
    last_good: dict | None,
    now: float,
    history: tuple[tuple[float, dict], ...],
    *,
    palette: Palette = Palette.DARK,
) -> list[tuple[str, float, str, str, str]]:
    """(label, pct, suffix, suffix_full, color) rows mirroring the CLI's
    ``_format_usage_lines``.

    ``suffix_full`` extends the reset countdown with the absolute clock time
    (``resets 2h 13m · 20:39``) for rows that have room; otherwise it equals
    ``suffix``. Only windows the account actually has produce a row — an
    annual plan without a 7-day window simply has no 7d line. Order matches
    the CLI: spend, 5h, 7d, then per-model scoped windows (e.g. "Fable"),
    the latter marked ``(!)`` at/over their limit. The 5h, 7d and scoped rows
    carry the projection from ``history`` (the store's recent samples) after
    the reset text, and are colored by pace (``window_color``); the spend row
    keeps the severity ramp.
    """
    if not isinstance(last_good, dict):
        return []
    rows: list[tuple[str, float, str, str, str]] = []
    spend = last_good.get("spend")
    if spend:
        amounts = f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"
        reset, reset_full = _reset_parts(spend, now)
        pct = float(spend["pct"])
        rows.append(
            (
                "$$",
                pct,
                _join_suffix(reset, amounts),
                _join_suffix(reset_full, amounts),
                palette.severity(pct),
            )
        )
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key)
        if window:
            rows.append(
                _window_row(
                    label, key, window,
                    history=history, now=now, palette=palette, mark_maxed=False,
                )
            )
    for window in last_good.get("scoped") or []:
        rows.append(
            _window_row(
                window["name"], window["name"], window,
                history=history, now=now, palette=palette, mark_maxed=True,
            )
        )
    return rows


def account_card_text(
    acc: AccountSnapshot,
    width: int,
    *,
    threshold: float | None = None,
    now: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """The full account card: header line + per-window bar rows."""
    now = now if now is not None else time.time()

    text = Text()
    text.append(f"{acc.number:>2}  ", style=f"bold {palette.foreground}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {palette.accent}")
        text.append(f" ({acc.email})", style=palette.foreground)
    else:
        text.append(acc.email, style=palette.foreground)
    text.append(f"  [{acc.display_tag}]", style=palette.muted)
    if acc.is_active:
        text.append("   ● active", style=f"bold {palette.accent}")
    if acc.disabled:
        text.append("   (disabled)", style=palette.muted)
    age = data.format_age(acc.usage.age_s)
    if age:
        text.append(f"   {age}", style=palette.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        text.append("\n    ")
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        marker = "·" if sentinel == USAGE_API_KEY else "⚠"
        text.append(f"{marker} {data.sentinel_label(sentinel)}", style=style)
        # Same supplementary line `cswap list` prints: the last good
        # measurement behind the sentinel (API-key accounts have no quota to
        # have "seen").
        if sentinel != USAGE_API_KEY:
            last_seen = data.last_seen_note(acc.usage)
            if last_seen is not None:
                text.append("\n    ")
                text.append(f"└ {last_seen}", style=palette.muted)
        return text

    rows = usage_rows(acc.usage.last_good, now, acc.usage.history, palette=palette)
    if not rows:
        text.append("\n    ")
        text.append("usage unavailable", style=palette.muted)
        if acc.usage.last_error:
            # Same wording as the CLI detail line: error KINDS with a
            # friendly note render it, so both surfaces describe the state
            # identically.
            note = ERROR_NOTES.get(acc.usage.last_error, acc.usage.last_error)
            text.append(f" · {note}", style=palette.muted)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    label_width = max(len(row[0]) for row in rows)
    bar_width = max(12, min(30, width - 42 - label_width))
    # everything on a row except the suffix: indent, label, bar, " NNN%", gap
    row_overhead = 4 + label_width + 1 + bar_width + 5 + 2
    for label, pct, suffix, suffix_full, color in rows:
        # per-row: show the absolute clock only where it fits, so a long
        # spend row degrading doesn't cost the 5h/7d rows their clocks
        if suffix_full != suffix and row_overhead + len(suffix_full) <= width:
            suffix = suffix_full
        text.append("\n    ")
        text.append(
            usage_bar(
                f"{label:<{label_width}}",
                pct,
                suffix or None,
                bar_width,
                color=color,
                stale=stale,
                # the auto-switch threshold is about usage windows, not spend
                threshold=None if label == "$$" else threshold,
                palette=palette,
            )
        )
    return text


def mini_account_text(
    acc: AccountSnapshot, now: float, *, palette: Palette = Palette.DARK
) -> Text:
    """One minimized line for an inactive account.

    ``2  work@acme.dev [personal]   5h 92% · 7d 63%`` — pcts only, pace
    colored; a window at/over 100% brings its reset countdown along, and a
    maxed per-model window shows as ``Fable (!)``. Sentinel states show
    their label instead.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{acc.number:>2}  ", style=f"bold {palette.muted}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {palette.accent}")
        text.append(f" ({acc.email})", style=palette.foreground)
    else:
        text.append(acc.email, style=palette.foreground)
    text.append(f"  [{acc.display_tag}]", style=palette.muted)
    if acc.disabled:
        text.append("  (disabled)", style=palette.muted)
    text.append("   ")

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        text.append(data.sentinel_label(sentinel), style=style)
        return text

    last_good = acc.usage.last_good
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    parts = 0
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key) if isinstance(last_good, dict) else None
        if not window:
            continue
        pct = float(window["pct"])
        if parts:
            text.append(" · ", style=palette.track)
        color = window_color(window, key, now, palette)
        text.append(f"{label} ", style=palette.muted)
        text.append(f"{pct:.0f}%", style=f"{color} dim" if stale else color)
        if pct >= 100:
            reset = data.reset_text(window, now)
            if reset:
                text.append(f" ({reset})", style=palette.muted)
        parts += 1
    maxed = [
        w["name"]
        for w in (last_good.get("scoped") or [] if isinstance(last_good, dict) else [])
        if float(w["pct"]) >= 100
    ]
    for name in maxed:
        if parts:
            text.append(" · ", style=palette.track)
        text.append(f"{name} (!)", style=palette.sev_crit)
        parts += 1
    if not parts:
        text.append("usage unknown", style=palette.muted)
    return text


class AccountsPanel(Static):
    """Static account overview: the active account full-size, others as
    one-line minis (in slot order, expanded in place). The dashboard's
    always-visible monitor."""

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _snap: self.refresh(layout=True))
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        snap = app.snapshot
        if snap is None:
            return Text("loading…", style=palette.muted)
        if not snap.accounts:
            return Text(
                "No managed accounts yet.\n"
                "Use the menu below: Add account — from your current "
                "Claude Code login, or from a setup-token / API key.",
                style=palette.muted,
            )
        now = time.time()
        width = (self.size.width or 80) - 2
        blocks: list[Text] = []
        for acc in snap.accounts:
            if acc.is_active:
                blocks.append(
                    account_card_text(
                        acc, width, threshold=app.threshold_pct, now=now,
                        palette=palette,
                    )
                )
            else:
                blocks.append(mini_account_text(acc, now, palette=palette))
        if not blocks:
            return Text("no active managed login", style=palette.muted)
        text = Text()
        previous_multiline = False
        for i, block in enumerate(blocks):
            multiline = "\n" in block.plain
            if i:
                # breathe around the expanded active card
                text.append("\n\n" if (multiline or previous_multiline) else "\n")
            text.append(block)
            previous_multiline = multiline
        return text


class AccountCard(Static):
    """One account rendered full-size (the account list screens' rows).

    Its bars carry the auto-switch threshold tick at ``app.threshold_pct``,
    read on every render; a screen that moves the threshold refreshes it.
    """

    def __init__(self, acc: AccountSnapshot) -> None:
        super().__init__()
        self._acc = acc

    def set_account(self, acc: AccountSnapshot) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]  # always mounted in CswapApp
        return account_card_text(
            self._acc, self.size.width or 80, threshold=app.threshold_pct,
            palette=Palette.from_theme(app.current_theme),
        )


class AccountItem(ListItem):
    """ListView row wrapping an :class:`AccountCard`; remembers its slot."""

    def __init__(self, acc: AccountSnapshot) -> None:
        super().__init__(AccountCard(acc))
        self.number = acc.number
        self.email = acc.email

    def set_account(self, acc: AccountSnapshot) -> None:
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(acc)


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(self, label: str, action_id: str, *, muted: bool = False) -> None:
        item = Static(label, markup=False)
        if muted:
            item.add_class("menu-item-muted")
        super().__init__(item)
        self.action_id = action_id
