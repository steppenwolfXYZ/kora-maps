#!/usr/bin/env python3
"""Build every glyph PBF the map needs, from source.

Called unconditionally from rebuild_transit.sh when no `--start` was passed
(a full rebuild-from-scratch); skipped when the user starts at a mid-pipeline
step. Populates `static/map-assets/fonts/` with:

- Saira Regular / Bold / Italic / SemiBold / ExtraBold / SemiCondensed —
  six weight/width combos instanced from the Saira variable font on Google
  Fonts, converted to fontnik PBFs.
- Noto Sans Regular — OpenFreeMap's pre-composited 23-font stack, downloaded
  per range with the internal stack name rewritten to "Noto Sans Regular".
  Provides U+25CF for the color-dot indicator layer, which Saira lacks.

Regenerates `scripts/transit/tools/glyph_widths.json` against the freshly
instanced Saira metrics.

Node dependencies: `fontnik` and `@mapbox/glyph-pbf-composite`, declared in
the project's `package.json`. Run `npm install` from the project root once
after cloning if you haven't already.
"""

import concurrent.futures
import io
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "static" / "map-assets" / "fonts"
BUILD_GLYPHS = ROOT / "node_modules" / ".bin" / "build-glyphs"

SAIRA_ROMAN_URL = ("https://github.com/google/fonts/raw/main/ofl/saira/"
                   "Saira%5Bwdth%2Cwght%5D.ttf")
SAIRA_ITALIC_URL = ("https://github.com/google/fonts/raw/main/ofl/saira/"
                    "Saira-Italic%5Bwdth%2Cwght%5D.ttf")

# The six static instances the style asks for by name.
# (folder_name, source_key, {wght, wdth})
INSTANCES = [
    ("Saira Regular",       "roman",  {"wght": 400, "wdth": 100}),
    ("Saira Bold",          "roman",  {"wght": 700, "wdth": 100}),
    ("Saira SemiBold",      "roman",  {"wght": 600, "wdth": 100}),
    ("Saira ExtraBold",     "roman",  {"wght": 800, "wdth": 100}),
    ("Saira SemiCondensed", "roman",  {"wght": 400, "wdth": 87.5}),
    ("Saira Italic",        "italic", {"wght": 400, "wdth": 100}),
]

# OpenFreeMap serves 256 range PBFs. Their "Noto Sans Regular" stack is a
# server-side composite of 23 Noto family fonts including CJK — hence its
# U+25CF glyph, which the raw NotoSans-Regular.ttf does not carry.
NOTO_BASE_URL = "https://tiles.openfreemap.org/fonts/Noto%20Sans%20Regular"
NOTO_RANGES = 256
NOTO_DOWNLOAD_WORKERS = 8


def _fetch(url: str) -> bytes:
    # OpenFreeMap's CDN rejects the default "Python-urllib/x.y" user
    # agent with 403; any browser-ish UA passes. Sent for all hosts —
    # GitHub does not care either way.
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (kora-maps glyph build)"}
    )
    with urllib.request.urlopen(req) as r:
        return r.read()


def _check_deps() -> None:
    if not BUILD_GLYPHS.exists():
        sys.exit(
            "build-glyphs not found at node_modules/.bin/build-glyphs — "
            "run `npm install` from the project root first "
            "(fontnik and @mapbox/glyph-pbf-composite are devDependencies)."
        )


def _rmtree(p: Path) -> None:
    if not p.exists():
        return
    for child in p.iterdir():
        child.unlink() if child.is_file() else _rmtree(child)
    p.rmdir()


def _build_saira() -> None:
    print("── Saira: downloading variable fonts …")
    sources = {"roman":  _fetch(SAIRA_ROMAN_URL),
               "italic": _fetch(SAIRA_ITALIC_URL)}

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for name, src_key, axes in INSTANCES:
            vf = TTFont(io.BytesIO(sources[src_key]))
            static = instantiateVariableFont(vf, axes)
            ttf_path = td_path / f"{name}.ttf"
            static.save(str(ttf_path))

            out_dir = FONTS_DIR / name
            _rmtree(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            subprocess.run(
                [str(BUILD_GLYPHS), str(ttf_path), str(out_dir)],
                check=True, capture_output=True,
            )
            n_pbfs = sum(1 for _ in out_dir.iterdir())
            print(f"  {name}: {n_pbfs} PBFs")


def _download_noto_range(idx: int) -> tuple[int, int, bytes]:
    lo, hi = idx * 256, idx * 256 + 255
    return lo, hi, _fetch(f"{NOTO_BASE_URL}/{lo}-{hi}.pbf")


def _build_noto() -> None:
    print(f"── Noto Sans Regular: fetching OpenFreeMap composite "
          f"({NOTO_RANGES} PBFs, {NOTO_DOWNLOAD_WORKERS} workers) …")
    out_dir = FONTS_DIR / "Noto Sans Regular"
    _rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=NOTO_DOWNLOAD_WORKERS) as ex:
        futures = [ex.submit(_download_noto_range, i)
                   for i in range(NOTO_RANGES)]
        for fut in concurrent.futures.as_completed(futures):
            lo, hi, data = fut.result()
            (out_dir / f"{lo}-{hi}.pbf").write_bytes(data)

    # Rewrite each PBF's internal stack name to "Noto Sans Regular" so
    # MapLibre matches when text-font asks for that exact fontstack.
    print("  rewriting stack names …")
    rename_js = r"""
        const fs   = require('fs');
        const path = require('path');
        const c    = require('@mapbox/glyph-pbf-composite');
        const dir  = process.argv[1];
        for (const f of fs.readdirSync(dir).sort()) {
            if (!f.endsWith('.pbf')) continue;
            const p = path.join(dir, f);
            const dec = c.decode(fs.readFileSync(p));
            for (const st of dec.stacks) st.name = 'Noto Sans Regular';
            fs.writeFileSync(p, c.encode(dec));
        }
    """
    subprocess.run(
        ["node", "-e", rename_js, str(out_dir)],
        check=True, capture_output=True, cwd=str(ROOT),
    )
    print(f"  Noto Sans Regular: {NOTO_RANGES} PBFs")


def _regen_widths() -> None:
    print("── Regenerating glyph_widths.json against Saira metrics …")
    subprocess.run(
        [sys.executable, "scripts/transit/tools/gen_glyph_widths.py"],
        check=True, cwd=str(ROOT),
    )


def main() -> None:
    _check_deps()
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    _build_saira()
    _build_noto()
    _regen_widths()
    print(f"\nDone. Fonts in {FONTS_DIR}")


if __name__ == "__main__":
    main()
