"""The "cswap-dark" Textual theme and shared color constants.

A subtle modern dark theme: neutral charcoal backgrounds in the VS Code
register, one warm terracotta accent (the same xterm-173 tone printer.py has
always used for the CLI — a deliberate nod to Claude Code's orange, used
sparingly), and desaturated severity colors so usage bars read calmly on a
dark background. Deliberately *not* a wholesale copy of any other tool's
palette.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.theme import Theme

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#d7875f"  # warm terracotta (xterm 173)
FOREGROUND = "#e8e4de"  # soft, slightly warm off-white
MUTED = "#8a8a8a"  # secondary text
BACKGROUND = "#141414"
SURFACE = "#1e1e1e"
PANEL = "#262626"

# Usage severity ramp (desaturated for dark backgrounds).
SEV_OK = "#87af87"  # calm green: plenty of headroom
SEV_WARN = "#d7af5f"  # amber: climbing (>= 70%)
SEV_CRIT = "#d75f5f"  # soft red: near the limit (>= 90%)
TRACK = "#3a3a3a"  # unfilled bar track

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0

# Pace gradient stops (dark): on pace is green, behind fades to blue, ahead
# runs through yellow (at the margin) to red (at twice it). The same stops as
# the Claude Code statusline, so the two read alike side by side.
PACE_ON = "#3fb96a"
PACE_BEHIND = "#58a6ff"
PACE_AHEAD = "#e8a531"
PACE_FAR = "#ef5c5c"


def _blend(start: str, end: str, t: float) -> str:
    """``#rrggbb`` a fraction ``t`` (0..1) of the way from ``start`` to ``end``."""
    a = [int(start[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(end[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str
    # Pace gradient stops (see ``pace_color``).
    pace_on: str
    pace_behind: str
    pace_ahead: str
    pace_far: str

    DARK: ClassVar["Palette"]

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    def pace_color(self, delta: float, margin: float) -> str:
        """Color for a window ``delta`` points off pace (+ ahead, − behind).

        On pace is ``pace_on``; behind fades to ``pace_behind`` at twice the
        margin; ahead reaches ``pace_ahead`` at the margin and ``pace_far`` at
        twice it. Beyond twice the margin either way the end stop holds.
        """
        span = margin * 2
        if delta < 0:
            return _blend(self.pace_on, self.pace_behind, min(1.0, -delta / span))
        t = min(1.0, delta / span)
        if t < 0.5:
            return _blend(self.pace_on, self.pace_ahead, t * 2)
        return _blend(self.pace_ahead, self.pace_far, (t - 0.5) * 2)

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        stops = (
            (PACE_ON, PACE_BEHIND, PACE_AHEAD, PACE_FAR)
            if theme.dark
            else (PACE_ON_LIGHT, PACE_BEHIND_LIGHT, PACE_AHEAD_LIGHT, PACE_FAR_LIGHT)
        )
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
            pace_on=stops[0],
            pace_behind=stops[1],
            pace_ahead=stops[2],
            pace_far=stops[3],
        )


CSWAP_DARK = Theme(
    name="cswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": PANEL,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
        "track": TRACK,
    },
)

# Light companion palette (same intent, tuned for a warm near-white base).
ACCENT_LIGHT = "#954c2a"  # burnt sienna — deepened for AA on panel (worst-case row bg)
FOREGROUND_LIGHT = "#2b2723"
MUTED_LIGHT = "#635d55"
BACKGROUND_LIGHT = "#faf7f2"
SURFACE_LIGHT = "#efeae1"
PANEL_LIGHT = "#e2dbcf"  # most-elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#3d6b3d"  # forest green — deepened for AA on panel
SEV_WARN_LIGHT = "#795911"  # deep ochre — deepened for AA on panel
SEV_CRIT_LIGHT = "#ad3128"  # brick red — deepened for AA on panel
TRACK_LIGHT = "#cec7ba"
# Pace stops deepened for the light panel: the severity tones, plus a blue.
PACE_ON_LIGHT = SEV_OK_LIGHT
PACE_BEHIND_LIGHT = "#1f5fa8"
PACE_AHEAD_LIGHT = SEV_WARN_LIGHT
PACE_FAR_LIGHT = SEV_CRIT_LIGHT

CSWAP_LIGHT = Theme(
    name="cswap-light",
    primary=ACCENT_LIGHT,
    secondary=MUTED_LIGHT,
    accent=ACCENT_LIGHT,
    foreground=FOREGROUND_LIGHT,
    background=BACKGROUND_LIGHT,
    surface=SURFACE_LIGHT,
    panel=PANEL_LIGHT,
    success=SEV_OK_LIGHT,
    warning=SEV_WARN_LIGHT,
    error=SEV_CRIT_LIGHT,
    dark=False,
    variables={
        "footer-key-foreground": ACCENT_LIGHT,
        "block-cursor-background": PANEL_LIGHT,
        "block-cursor-foreground": FOREGROUND_LIGHT,
        "block-cursor-text-style": "none",
        "track": TRACK_LIGHT,
    },
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
    pace_on=PACE_ON, pace_behind=PACE_BEHIND, pace_ahead=PACE_AHEAD, pace_far=PACE_FAR,
)
