#!/usr/bin/env python3
"""One-off generator for scripts/transit/tools/glyph_widths.json.

Downloads Saira as a variable font from Google Fonts and instantiates
Regular (wght=400) and ExtraBold (wght=800) — the two weights the close-zoom
pill-arrows actually render (destination text / line number). Per-character
advance widths are extracted as em fractions. Step 07's close-zoom label
wrapping measures text with these widths instead of assuming a flat average
character width.

Note: the JSON keys `regular` / `bold` / `default_regular` / `default_bold`
are kept for backward compatibility with `close_zoom/constants.py`; `bold`
here refers to Saira ExtraBold (the pill-arrow ref weight).

Kerning is deliberately ignored (~1% error). Coverage: Basic Latin,
Latin-1 Supplement, Latin Extended-A, plus common punctuation used in stop
names (quotes, dashes, ellipsis).

Usage:
    python3 -m pip install --user --break-system-packages fonttools
    python3 scripts/transit/tools/gen_glyph_widths.py

Re-run only if the map's font stack changes.
"""

import io
import json
import statistics
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

OUT = Path(__file__).resolve().parent / "glyph_widths.json"

# Saira ships as a variable font (wght + wdth axes) on Google Fonts. Same
# source we instance for the map's runtime glyph PBFs, so measurements
# match what MapLibre actually renders.
SAIRA_VF_URL = ("https://github.com/google/fonts/raw/main/ofl/saira/"
                "Saira%5Bwdth%2Cwght%5D.ttf")

# JSON keys ↔ weight axis value picked out of the variable font.
INSTANCES = {
    "regular": {"wght": 400, "wdth": 87.5},  # Saira Semi Condensed — destinations
    "bold":    {"wght": 800, "wdth": 100},   # Saira ExtraBold — pill-arrow ref
}

# Basic Latin + Latin-1 Supplement + Latin Extended-A, plus punctuation
# that appears in Swiss stop names and in the wrapper's own output.
CODEPOINTS = (list(range(0x20, 0x7F))
              + list(range(0xA0, 0x180))
              + [0x2018, 0x2019, 0x201C, 0x201D,   # curly quotes
                 0x2013, 0x2014,                   # en/em dash
                 0x2026])                          # ellipsis


def _widths_from_ttf(font: TTFont) -> tuple[dict, float]:
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    upm = font["head"].unitsPerEm
    widths = {}
    for cp in CODEPOINTS:
        gname = cmap.get(cp)
        if gname is None:
            continue
        adv = hmtx[gname][0] / upm
        widths[chr(cp)] = round(adv, 4)
    default = round(statistics.mean(widths.values()), 4)
    return widths, default


def main() -> None:
    print(f"Fetching {SAIRA_VF_URL} ...")
    with urllib.request.urlopen(SAIRA_VF_URL) as resp:
        vf_bytes = resp.read()

    out = {"source": {"variable_font": SAIRA_VF_URL,
                      "instances": INSTANCES}}
    for style, axes in INSTANCES.items():
        # Instance a fresh TTFont per style — instantiateVariableFont mutates
        # its input, so a shared TTFont across weights would double-instance.
        vf = TTFont(io.BytesIO(vf_bytes))
        static = instantiateVariableFont(vf, axes)
        widths, default = _widths_from_ttf(static)
        out[style] = widths
        out[f"default_{style}"] = default
        print(f"  {style} (wght={axes['wght']}): {len(widths)} glyphs, "
              f"default {default} em")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
