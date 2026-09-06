"""The GIF: glasses falling onto every face, then DEAL WITH IT.

Worker-only for the same reason :mod:`src.derivatives` is -- it holds Pillow
and rasterises artwork -- and, like it, it is handed pixels rather than going
looking for them. Nothing here knows how a face was found. It gets the three
landmarks the glasses are placed from and the processor's own ``place``, so
the frame the fall lands on is the picture the job already produced, drawn by
the same rule at a smaller size rather than approximated.

The choreography is here; the size the GIF is drawn at is in
:mod:`src.derivatives`, beside the other derivative sizes.
"""

from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path

from PIL import Image

from src import svg

TEXT_PATH = Path(__file__).resolve().parent / 'static' / 'img' / 'dealwithit.svg'

Point = tuple[float, float]
#: ``DealWithItProcessor.place``: three landmarks in, a sprite and the
#: top-left it belongs at out.
Placer = Callable[[Point, Point, Point], tuple[Image.Image, tuple[int, int]]]

#: The photo alone before anything moves, so a loop reads as a joke with a
#: setup rather than a flicker.
OPENING_MS = 400
#: Steps in the fall, and how long each is on screen. Twelve at 60 ms is
#: 0.72 s, which is about how long a real object takes to fall a metre.
FALL_STEPS = 12
STEP_MS = 60
#: How far above the top edge the glasses start, so nothing is half-visible
#: on the first frame.
FALL_MARGIN = 8
#: Steps between the first face's fall and the last one's, however many
#: faces there are: the Solvay conference has twenty-nine, and one stagger
#: per face would run for a minute.
SPREAD_STEPS = 5
#: The beat between the last pair landing and the caption.
LANDED_MS = 420
#: The caption snapping in: a scale of its final size, and a duration. The
#: overshoot is what makes it read as a stamp rather than a fade.
CAPTION_POP = ((0.72, STEP_MS), (1.08, STEP_MS), (1.0, 1800))
#: Caption width as a fraction of the frame's, and the gap under it as a
#: fraction of the frame's height.
CAPTION_WIDTH = 0.72
CAPTION_BOTTOM = 0.06

#: A GIF has one palette. Dithering is deliberately off: Floyd-Steinberg
#: diffuses its error across the whole scanline, so a moving sprite would
#: change pixels far away from itself -- visible as a shimmer over the
#: photograph, and expensive, because what a GIF stores per frame is the
#: rectangle that changed.
COLOURS = 256


def _caption(frame: Image.Image, factor: float) -> None:
    """Stamp DEAL WITH IT across the bottom of ``frame``, in place.

    Scaled about the caption's own centre rather than its baseline, so the
    pop grows in both directions and does not appear to climb.
    """
    full = max(1, round(frame.width * CAPTION_WIDTH))
    art = svg.render(svg.source(TEXT_PATH), max(1, round(full * factor)))
    settled = svg.render(svg.source(TEXT_PATH), full)
    middle = frame.height - round(frame.height * CAPTION_BOTTOM) - settled.height / 2
    frame.paste(art, ((frame.width - art.width) // 2, round(middle - art.height / 2)), art)


def _steps(base: Image.Image, sprites: list[tuple[Image.Image, int, int]],
           ) -> list[tuple[Image.Image, int]]:
    """Every frame of the loop with how long it is held.

    Frames, not seconds: the still ones are one frame with a long duration,
    which is why a three-second loop is eighteen frames and not sixty.
    """
    steps = [(base, OPENING_MS)]
    spread = SPREAD_STEPS if len(sprites) > 1 else 0
    for step in range(1, FALL_STEPS + spread + 1):
        frame = base.copy()
        for index, (glasses, x, y) in enumerate(sprites):
            start = round(spread * index / max(len(sprites) - 1, 1))
            fallen = min(max((step - start) / FALL_STEPS, 0.0), 1.0)
            # Squared, not linear: things fall under gravity, and a constant
            # speed reads as a lift descending.
            above = y + glasses.height + FALL_MARGIN
            frame.paste(glasses, (x, y - round((1 - fallen ** 2) * above)), glasses)
        steps.append((frame, STEP_MS))
    steps[-1] = (steps[-1][0], LANDED_MS)

    landed = steps[-1][0]
    for factor, duration in CAPTION_POP:
        frame = landed.copy()
        _caption(frame, factor)
        steps.append((frame, duration))
    return steps


def render(base: Image.Image, faces: Sequence[tuple[Point, Point, Point]],
           place: Placer, scale: float) -> bytes:
    """The animation for one finished job, as GIF bytes.

    ``base`` is the submitted picture already sized for the GIF and ``scale``
    is what it was sized by, which is also what the landmarks are multiplied
    by before ``place`` draws on that smaller canvas.
    """
    photo = base.convert('RGB')
    sprites = []
    for landmarks in faces:
        glasses, (x, y) = place(*[(px * scale, py * scale) for px, py in landmarks])
        sprites.append((glasses, x, y))
    # Left to right, so a staggered fall crosses the picture instead of
    # arriving in whatever order the detector happened to report.
    sprites.sort(key=lambda sprite: sprite[1])

    steps = _steps(photo, sprites)
    # The palette comes from the last frame because it is the one that has to
    # look right: the whole photograph, the glasses and the caption, which is
    # every colour the loop ever shows bar the pixels the glasses cover at
    # rest.
    palette = steps[-1][0].quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT)
    frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE)
              for frame, _ in steps]

    buffer = BytesIO()
    # `disposal=1` leaves each frame in place, which is what lets Pillow
    # store the later ones as the rectangle that changed rather than as a
    # whole picture apiece.
    frames[0].save(buffer, format='GIF', save_all=True, append_images=frames[1:],
                   duration=[duration for _, duration in steps], loop=0,
                   disposal=1, optimize=True)
    return buffer.getvalue()
