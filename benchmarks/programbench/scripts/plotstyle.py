"""The Open Engine figure style shared by the plotting scripts: palette and fonts.

Fraunces and Spline Sans are fetched once from Google Fonts into ~/.cache/zsbench/fonts (the default
fonts are used offline).
"""

import re
import sys
import urllib.request
from pathlib import Path

from matplotlib import font_manager

# docs/brand/README.md: cream canvas, ink text, rust as the only accent (one rule, never a fill).
CANVAS, INK, INK_2, MUTED, HAIRLINE, RUST = "#FAF7F1", "#171411", "#4A433A", "#847A6C", "#E4DED3", "#C2240C"
FONTS = {"Fraunces": "Fraunces:wght@600", "SplineSans": "Spline+Sans:wght@400;500"}
FONT_CACHE = Path.home() / ".cache" / "zsbench" / "fonts"


def load_fonts() -> None:
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    for name, spec in FONTS.items():
        try:
            if not any(FONT_CACHE.glob(f"{name}-*.ttf")):
                css = urllib.request.urlopen(f"https://fonts.googleapis.com/css2?family={spec}", timeout=30).read().decode()
                for weight, url in re.findall(r"font-weight:\s*(\d+);\s*src:\s*url\((https://[^)]+\.ttf)\)", css):
                    urllib.request.urlretrieve(url, FONT_CACHE / f"{name}-{weight}.ttf")
        except OSError as error:
            print(f"warning: could not fetch {name} ({error}); using the default font", file=sys.stderr)
    for font in FONT_CACHE.glob("*.ttf"):
        font_manager.fontManager.addfont(str(font))
