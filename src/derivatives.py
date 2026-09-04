"""Turning one finished picture into the handful of files a browser wants.

Worker-only: this is the module that holds Pillow, so :mod:`src.blobs` does
not have to. The web tier imports blobs and never this.

Sizes come from what the CSS actually renders, not from round numbers. See
``docs/LOAD.md`` for what each one weighs.
"""

import logging
from io import BytesIO

from PIL import Image

from src import blobs

logger = logging.getLogger(__name__)

#: `.thumb` in a card head is 44x44 CSS px. 160 covers a 3x display.
THUMB = 160
#: One size for the card *and* the full-screen view, on purpose.
#:
#: `_result_view` deliberately reuses the same <img> for both -- the lightbox
#: is a CSS `:has(:checked)` state over the very same element -- so there is no
#: srcset/sizes pair that can describe it: `sizes` cannot see a sibling
#: checkbox. 1600 px is the compromise that falls out of that: crisp in the
#: card (max-height 460), slightly soft filling a 1440p screen, and a
#: fortieth of the full-resolution file. Download is the escape hatch.
VIEW = 1600

#: Good enough that nobody reaches for the original, small enough to send.
VIEW_QUALITY = 80
THUMB_QUALITY = 75
#: A file someone chose to download. Barely lossy.
FULL_QUALITY = 92
#: What a scraper puts in a chat preview when a share link is pasted. JPEG,
#: not WebP: link unfurlers are years behind browsers. 1200px is what the
#: large-summary card wants, and it keeps a preview off the 3-8 MB original.
CARD = 1200
CARD_QUALITY = 85


def _fit(image: Image.Image, longest: int) -> Image.Image:
    """The image no larger than ``longest`` on its long side. Never upscales:
    a small picture is already the right size for anything below it."""
    width, height = image.size
    if max(width, height) <= longest:
        return image
    ratio = longest / max(width, height)
    return image.resize((max(1, round(width * ratio)), max(1, round(height * ratio))),
                        Image.LANCZOS)


def _write(job_id: str, name: str, image: Image.Image, img_format: str, **options) -> str:
    buffer = BytesIO()
    image.save(buffer, format=img_format, **options)
    return blobs.put(job_id, name, buffer.getvalue())


def write_result(job_id: str, image: Image.Image,
                 img_format: str) -> tuple[dict[str, str], dict[str, str]]:
    """Every file a finished job serves, and the formats it offers.

    Written here rather than converted on request: the web tier turning a
    36 MP picture into a PNG on demand would be an unauthenticated way to
    spend the two cores detection needs. The worker already holds the pixels.
    """
    native = 'jpg' if img_format == 'JPEG' else 'png'
    images = {
        'full': _write(job_id, f'result.{native}', image, img_format,
                       **({'quality': FULL_QUALITY} if native == 'jpg' else {})),
        'view': _write(job_id, 'view.webp', _fit(image, VIEW), 'WEBP',
                       quality=VIEW_QUALITY, method=4),
        'thumb': _write(job_id, 'thumb.webp', _fit(image, THUMB), 'WEBP',
                        quality=THUMB_QUALITY, method=4),
        'card': _write(job_id, 'card.jpg', _fit(image.convert('RGB'), CARD), 'JPEG',
                       quality=CARD_QUALITY),
    }
    downloads = {
        native: images['full'],
        'webp': _write(job_id, 'result.webp', image, 'WEBP', quality=FULL_QUALITY, method=4),
    }
    if native == 'png':
        # A photo that arrived as a PNG is worth offering as a JPEG. The
        # reverse is not: a lossless wrapper around lossy data is bloat.
        downloads['jpg'] = _write(job_id, 'result.jpg', image.convert('RGB'), 'JPEG',
                                  quality=FULL_QUALITY)
    return images, downloads


def write_before(job_id: str, image: Image.Image) -> dict[str, str]:
    """The submitted picture, at viewing size, for the Before half."""
    return {'before': _write(job_id, 'before.webp', _fit(image, VIEW), 'WEBP',
                             quality=VIEW_QUALITY, method=4)}
