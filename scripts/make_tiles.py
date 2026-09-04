"""Regenerate the display derivatives of the committed pictures.

    uv run python scripts/make_tiles.py

Run by hand, output committed. The originals stay exactly as they are: a
sample is *submitted* at full resolution, because the face counts the tiles
claim depend on the width (see ``src/static/img/CREDITS.md`` and
``SAMPLE_FACES`` in ``tests/test_processor.py``). These files are for looking
at, nothing else.

The sample grid showed 16 full-size JPEGs -- 4.0 MB -- inside ~92 px squares.
"""

import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / 'src' / 'static' / 'img'

#: `.sample img` is `aspect-ratio: 1; object-fit: cover` at 88-100 px, and
#: 96 px in the phone strip. A square centre crop is precisely what the
#: browser was already showing, so the page does not change; 200 px covers
#: a 2x display.
TILE = 200
#: `.figures img` is ~372 CSS px wide on the docs page, single-column at
#: narrow widths. 760 px covers 2x.
FIGURE = 760

FIGURES = (
    'me.jpg', 'deal_with_me.png',
    'multiple_people.jpg', 'deal_with_multiple_people.png',
)


def tiles() -> int:
    out = IMG / 'tiles'
    out.mkdir(exist_ok=True)
    written = 0
    for source in sorted(IMG.glob('*.jpg')):
        image = ImageOps.exif_transpose(Image.open(source)).convert('RGB')
        square = ImageOps.fit(image, (TILE, TILE), method=Image.LANCZOS)
        square.save(out / f'{source.stem}.webp', format='WEBP', quality=80, method=6)
        written += 1
    return written


def figures() -> int:
    out = IMG / 'figures'
    out.mkdir(exist_ok=True)
    for name in FIGURES:
        source = IMG / name
        image = ImageOps.exif_transpose(Image.open(source)).convert('RGB')
        width, height = image.size
        if width > FIGURE:
            image = image.resize(
                (FIGURE, round(height * FIGURE / width)), Image.LANCZOS)
        image.save(out / f'{source.stem}.webp', format='WEBP', quality=82, method=6)
    return len(FIGURES)


def main() -> int:
    before = sum(p.stat().st_size for p in IMG.glob('*.jpg'))
    count = tiles()
    after = sum(p.stat().st_size for p in (IMG / 'tiles').glob('*.webp'))
    print(f'{count} tiles at {TILE}px: {before / 1024:.0f} KB -> {after / 1024:.0f} KB')

    figures()
    for name in FIGURES:
        original = (IMG / name).stat().st_size
        derived = (IMG / 'figures' / f'{Path(name).stem}.webp').stat().st_size
        print(f'  figure {name:32s} {original / 1024:6.0f} KB -> {derived / 1024:5.0f} KB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
