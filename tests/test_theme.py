"""Tests for the Palette value object and the light/dark themes."""
from __future__ import annotations

from claude_swap.tui import theme
from claude_swap.tui.theme import CSWAP_DARK, CSWAP_LIGHT, Palette


def test_dark_palette_matches_constants():
    p = Palette.DARK
    assert (p.accent, p.foreground, p.muted, p.sev_ok, p.sev_warn, p.sev_crit, p.track) == (
        theme.ACCENT,
        theme.FOREGROUND,
        theme.MUTED,
        theme.SEV_OK,
        theme.SEV_WARN,
        theme.SEV_CRIT,
        theme.TRACK,
    )


def test_from_theme_reads_theme_object_including_track():
    p = Palette.from_theme(CSWAP_LIGHT)
    assert p.accent == theme.ACCENT_LIGHT
    assert p.sev_crit == theme.SEV_CRIT_LIGHT
    assert p.track == theme.TRACK_LIGHT  # from Theme.variables["track"], not app cache


def test_severity_ramp_and_none():
    p = Palette.DARK
    assert p.severity(None) == p.muted
    assert p.severity(95.0) == p.sev_crit
    assert p.severity(75.0) == p.sev_warn
    assert p.severity(10.0) == p.sev_ok


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))


def test_pace_color_endpoints_in_both_themes():
    dark = Palette.DARK
    light = Palette.from_theme(CSWAP_LIGHT)
    assert Palette.from_theme(CSWAP_DARK) == dark
    expected = {
        dark: (theme.PACE_ON, theme.PACE_BEHIND, theme.PACE_AHEAD, theme.PACE_FAR),
        light: (theme.SEV_OK_LIGHT, "#1f5fa8", theme.SEV_WARN_LIGHT, theme.SEV_CRIT_LIGHT),
    }
    assert expected[dark] == ("#3fb96a", "#58a6ff", "#e8a531", "#ef5c5c")
    for p, (on, behind, ahead, far) in expected.items():
        for margin in (5.0, 10.0):
            assert p.pace_color(0.0, margin) == on
            assert p.pace_color(-2 * margin, margin) == behind
            assert p.pace_color(-5 * margin, margin) == behind  # clamped past 2×
            assert p.pace_color(margin, margin) == ahead
            assert p.pace_color(2 * margin, margin) == far
            assert p.pace_color(9 * margin, margin) == far


def test_pace_color_midpoints():
    p = Palette.DARK
    # +margin/2 is halfway from green to yellow; −margin is halfway to blue;
    # +1.5×margin is halfway from yellow to red.
    cases = (
        (5.0, (63, 185, 106), (232, 165, 49)),
        (-10.0, (63, 185, 106), (88, 166, 255)),
        (15.0, (232, 165, 49), (239, 92, 92)),
    )
    for delta, start, end in cases:
        got = _rgb(p.pace_color(delta, 10.0))
        mid = tuple((a + b) // 2 for a, b in zip(start, end))
        assert all(abs(g - m) <= 1 for g, m in zip(got, mid)), (delta, got, mid)


def test_both_themes_expose_track_variable():
    assert CSWAP_DARK.variables["track"] == theme.TRACK
    assert CSWAP_LIGHT.variables["track"] == theme.TRACK_LIGHT
    assert CSWAP_LIGHT.dark is False


def _contrast(hex_a: str, hex_b: str) -> float:
    def lum(h: str) -> float:
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    la, lb = sorted((lum(hex_a), lum(hex_b)))
    return (lb + 0.05) / (la + 0.05)


def test_light_text_meets_AA_on_all_backgrounds():
    # Accent and severity colors render as PERCENTAGE TEXT on highlighted
    # ($surface) and flash ($panel) rows, not just the base background — so
    # every text color must clear the 4.5:1 text bar against all three.
    from claude_swap.tui import theme
    text_colors = (
        theme.FOREGROUND_LIGHT,
        theme.MUTED_LIGHT,
        theme.ACCENT_LIGHT,
        theme.SEV_OK_LIGHT,
        theme.SEV_WARN_LIGHT,
        theme.SEV_CRIT_LIGHT,
    )
    backgrounds = (theme.BACKGROUND_LIGHT, theme.SURFACE_LIGHT, theme.PANEL_LIGHT)
    for color in text_colors:
        for bg in backgrounds:
            assert _contrast(color, bg) >= 4.5, f"{color} on {bg}"
