"""Regenerate the self-hosted web fonts under landing/static/landing/.

We self-host Montserrat + Space Grotesk instead of linking fonts.googleapis.com
so that (a) no render-blocking third-party request sits in front of first paint
and (b) no visitor IP is handed to Google, which keeps us consistent with the
cookie banner. The files are byte-identical to what fonts.gstatic.com serves.

Run from the project root:

    venv/Scripts/python.exe tools/fetch_fonts.py     # Windows
    venv/bin/python tools/fetch_fonts.py             # POSIX

It rewrites landing/static/landing/css/fonts.css and downloads any missing
.woff2 into landing/static/landing/fonts/. Commit both.
"""
import pathlib
import re
import urllib.request

# `wght@400..700` asks the css2 API for VARIABLE fonts: one file per family per
# subset covering every weight, instead of one static file per weight (which was
# 585 KB across 16 files vs 146 KB across 4).
CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Montserrat:wght@400..700"
    "&family=Space+Grotesk:wght@400..700"
    "&display=swap"
)
# Google picks the font format from the User-Agent. It must be a FULL modern
# browser UA — an abbreviated one (no "AppleWebKit ... Safari") silently gets you
# legacy .ttf instead of .woff2, which is ~3x the bytes.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}
# The site is Spanish (PR) and English — Cyrillic/Greek/Vietnamese are dead weight.
SUBSETS = {"latin", "latin-ext"}

ROOT = pathlib.Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "landing/static/landing/fonts"
CSS_OUT = ROOT / "landing/static/landing/css/fonts.css"

HEADER = """/* Self-hosted Google Fonts: Montserrat + Space Grotesk variable (wght 400-700),
   latin & latin-ext subsets only — the same files fonts.gstatic.com served.
   Self-hosted so the render-blocking third-party requests are gone and no
   visitor IP reaches Google (consistent with our cookie banner).
   DO NOT EDIT BY HAND — regenerate with: python tools/fetch_fonts.py
   See docs/lanzamiento/04-fuentes.md. */

"""


def get(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read()


def main() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    css = get(CSS_URL).decode("utf-8")

    if "woff2" not in css:
        raise SystemExit(
            "Google served a legacy format (no woff2) — the User-Agent above is "
            "probably no longer accepted as a modern browser."
        )

    blocks = re.findall(r"/\* ([\w-]+) \*/\s*(@font-face \{.*?\})", css, re.S)
    if not blocks:
        raise SystemExit("Google returned no @font-face blocks — did the css2 API change?")

    out, total, kept = [], 0, set()
    for subset, block in blocks:
        if subset not in SUBSETS:
            continue
        family = re.search(r"font-family: '([^']+)'", block).group(1)
        url = re.search(r"url\((https://[^)]+)\)", block).group(1)
        name = f"{family.lower().replace(' ', '-')}-{subset}-var.woff2"
        dest = FONTS_DIR / name
        if not dest.exists():
            dest.write_bytes(get(url))
            print(f"  downloaded {name}")
        kept.add(name)
        total += dest.stat().st_size
        # Relative URL: WhiteNoise's ManifestStaticFilesStorage rewrites it to the
        # content-hashed filename at collectstatic time.
        out.append(block.replace(url, f"../fonts/{name}"))

    if not out:
        raise SystemExit(f"No {'/'.join(sorted(SUBSETS))} subsets found in the css2 response.")

    for stale in FONTS_DIR.glob("*.woff2"):
        if stale.name not in kept:
            stale.unlink()
            print(f"  removed stale {stale.name}")

    CSS_OUT.write_text(HEADER + "\n\n".join(out) + "\n", encoding="utf-8")
    print(f"{CSS_OUT.relative_to(ROOT).as_posix()}: {len(out)} @font-face blocks, {total / 1024:.0f} KB of woff2")


if __name__ == "__main__":
    main()
