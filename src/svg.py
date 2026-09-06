"""Vector artwork into pixels, at whatever size the caller needs.

Worker-only, like :mod:`src.derivatives`: resvg lives in the worker extra and
the web tier never draws anything. Both pieces of artwork -- the glasses and
the DEAL WITH IT lettering -- come through here, so there is one cache rather
than one per caller.
"""

import functools
from io import BytesIO
from pathlib import Path

import resvg_py
from PIL import Image


@functools.cache
def source(path: Path) -> str:
    """The SVG text, read once per process rather than once per face."""
    return path.read_text()


@functools.lru_cache(maxsize=32)
def render(text: str, width: int) -> Image.Image:
    """The artwork rasterised at ``width`` pixels across.

    Keyed on the SVG text and the width, never on the object that asked:
    callers only warp, rotate, resize or paste the result, all of which
    return a new image, so one raster is safe to hand out repeatedly.
    """
    png = bytes(resvg_py.svg_to_bytes(svg_string=text, width=width))
    return Image.open(BytesIO(png)).convert('RGBA')
