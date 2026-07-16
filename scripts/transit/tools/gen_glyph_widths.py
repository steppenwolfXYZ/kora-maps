#!/usr/bin/env python3
"""One-off generator for scripts/transit/tools/glyph_widths.json.

Downloads the Noto Sans Regular and Bold TTFs (the same typeface the
OpenFreeMap glyph server serves) and extracts per-character advance widths
as em fractions. Step 07's close-zoom label wrapping measures text with
these widths instead of assuming a flat average character width.

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

OUT = Path(__file__).resolve().parent / "glyph_widths.json"

URLS = {
    "regular": ("https://raw.githubusercontent.com/notofonts/"
                "notofonts.github.io/main/fonts/NotoSans/hinted/ttf/"
                "NotoSans-Regular.ttf"),
    "bold":    ("https://raw.githubusercontent.com/notofonts/"
                "notofonts.github.io/main/fonts/NotoSans/hinted/ttf/"
                "NotoSans-Bold.ttf"),
}

# Basic Latin + Latin-1 Supplement + Latin Extended-A, plus punctuation
# that appears in Swiss stop names and in the wrapper's own output.
CODEPOINTS = (list(range(0x20, 0x7F))
              + list(range(0xA0, 0x180))
              + [0x2018, 0x2019, 0x201C, 0x201D,   # curly quotes
                 0x2013, 0x2014,                   # en/em dash
                 0x2026])                          # ellipsis


def extract(url: str) -> tuple[dict, float]:
    print(f"Fetching {url} ...")
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    font = TTFont(io.BytesIO(data))
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
    out = {"source": URLS}
    for style, url in URLS.items():
        widths, default = extract(url)
        out[style] = widths
        out[f"default_{style}"] = default
        print(f"  {style}: {len(widths)} glyphs, default {default} em")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
