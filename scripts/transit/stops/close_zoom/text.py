"""Pill-arrow label text: width measuring from baked glyph advances,
destination shortening, ref-font shrinking, and label wrapping."""
import re

from stops.close_zoom.constants import *  # noqa: F401,F403


# Break characters where a line wrap may fall AFTER the character (so a
# dashed compound like "Zug-Bahnhof" can split as "Zug-" / "Bahnhof").
# Covers ASCII hyphen-minus, en dash, em dash.
_DASH_RE = re.compile(r"(?<=[-–—])")

def strip_city_prefix(name: str, city: str) -> str:
    """If `name` starts with `city` followed by ',' or ' ', strip the
    prefix and the separator. Case-insensitive. The separator requirement
    keeps "Berneck" intact when city is "Bern"."""
    if not name or not city:
        return name
    n = name.strip()
    low_n, low_c = n.lower(), city.lower()
    if low_n.startswith(low_c + ",") or low_n.startswith(low_c + " "):
        return n[len(city) + 1:].strip()
    return n


def _shorten_destination(dest: str, current_stop_name: str) -> str:
    """Destination shortening for pill-arrow labels.

    1. If the destination begins with the current stop's city — comma- or
       space-separated ("Bern, …" or "Bern …" on a pill-arrow in Bern) — strip the
       city prefix. The city is the part of the current stop's name before
       its first comma. The separator requirement keeps "Berneck" intact.
    2. If (afterwards) a comma remains, keep only the part before it
       ("Wabern, Tram-Endstation" → "Wabern").
    """
    if not dest:
        return dest
    city = (current_stop_name or "").split(",")[0].strip()
    d = strip_city_prefix(dest, city)
    if "," in d:
        d = d.split(",")[0].strip()
    return d or dest


def _text_width_em(s: str) -> float:
    """Width of `s` in ems, from the baked Noto Sans advance widths
    (kerning ignored). Falls back to a flat average per character when
    glyph_widths.json is absent."""
    return sum(GLYPH_WIDTHS.get(ch, GLYPH_WIDTH_DEFAULT) for ch in s)


def _text_width_em_bold(s: str) -> float:
    """Bold-weight counterpart of `_text_width_em` — used to size the
    close-zoom pill-arrow line number, which renders in Noto Sans Bold."""
    return sum(GLYPH_WIDTHS_BOLD.get(ch, GLYPH_WIDTH_DEFAULT_BOLD) for ch in s)


def _shrink_ref_font_m(ref_text: str, nominal_font_m: float,
                       band_config: dict) -> float:
    """Return the pill-arrow line-number glyph height in metres, shrunk
    from `nominal_font_m` only as far as needed so the ref text fits its
    container: the disc for duo-tone bands (B–E), the whole pill-arrow for the
    solid band A. Short numbers keep the nominal size."""
    if not ref_text or nominal_font_m <= 0:
        return nominal_font_m
    w_em = _text_width_em_bold(ref_text)
    if w_em <= 0:
        return nominal_font_m
    border_half = CLOSE_ZOOM_BORDER_M / 2.0
    solid = band_config["font_dest_m"] is None
    if solid:
        # Solid pill-arrow (band A): text sits horizontally along the pill-arrow's
        # long axis inside an inner rectangle body_len × W (minus border on
        # each side). Width-bound by the length axis, height-bound by the
        # transverse axis. Slightly conservative vs the true stadium shape
        # (the caps give a hair of extra room past the body length) but
        # dead simple.
        inner_len = band_config["length_m"] - 2.0 * border_half
        inner_wid = band_config["width_m"] - 2.0 * border_half
        return min(nominal_font_m, inner_len / w_em, inner_wid)
    # Duo-tone bands (B–E): the ref sits inside the disc. Its bounding box
    # of size (w_em * h) × h must inscribe in a circle of inner radius
    # R_inner = R - border_half. Corners on the circle give
    # (w_em·h)² + h² = (2·R_inner)² → h = 2·R_inner / sqrt(w_em² + 1).
    R_inner = band_config["width_m"] / 2.0 - border_half
    max_font = 2.0 * R_inner / sqrt(w_em * w_em + 1.0)
    return min(nominal_font_m, max_font)


def _wrap_label(text: str, max_w_em: float, max_lines: int) -> str:
    """Greedy-wrap `text` into at most `max_lines` lines of at most
    `max_w_em` measured ems, with baked "\\n" breaks (MapLibre honours
    them). Break points are whitespace AND dashes (`-` / `–` / `—`) — the
    dash stays on the preceding piece so a broken "Zug-Bahnhof" reads
    "Zug-" / "Bahnhof". Words still wider than a line are shortened with a
    single abbreviation dot (no hyphen splitting mid-word — without
    linguistic hyphenation the break positions would be nonsense).

    The line breaks are computed FIRST. If the text needs more than
    max_lines, the last kept line is rejoined with the dropped lines and
    letter-filled to the line width before the trailing ellipsis lands, so
    the ellipsis sits where the characters actually run out — not at the
    word boundary that bumped the next word to a discarded line. On a
    one-line band the latter would strand the ellipsis right after the
    first word ("La Roche FR" → "La…" instead of "La Roch…")."""
    # Tokenise into (piece, join_sep) where join_sep is the separator
    # inserted BEFORE this piece when adjacent tokens land on the same line.
    # Whitespace boundary → " "; dash boundary → "" (dash stays on prev).
    tokens: list[tuple[str, str]] = []
    for w in text.split():
        for i, part in enumerate(p for p in _DASH_RE.split(w) if p):
            sep = "" if (i > 0 or not tokens) else " "
            tokens.append((part, sep))

    lines = []
    cur = ""
    for piece, sep in tokens:
        cand = (cur + sep + piece) if cur else piece
        if _text_width_em(cand) <= max_w_em:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if _text_width_em(piece) <= max_w_em:
            cur = piece
        else:
            # Single token wider than a line → abbreviate with a dot.
            cut = len(piece)
            while cut > 1 and _text_width_em(piece[:cut] + ".") > max_w_em:
                cut -= 1
            lines.append(piece[:cut] + ".")
    if cur:
        lines.append(cur)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    # Rejoin the last kept line with every dropped line and letter-fill the
    # combined string, so the ellipsis uses the full line width instead of
    # stranding right after the word boundary that bumped the next word.
    last = " ".join(lines[max_lines - 1:])
    while last and _text_width_em(last + "…") > max_w_em:
        last = last[:-1].rstrip()
    last = last.rstrip(" .")
    kept[-1] = (last + "…") if last else "…"
    return "\n".join(kept)

