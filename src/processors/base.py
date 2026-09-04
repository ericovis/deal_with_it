"""Processor scaffolding: take pixels in, produce a PIL image out."""

from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray
from PIL import Image


class BaseProcessor:
    #: 'PNG' or 'JPEG'. The task sets it to match the input, and
    #: src.derivatives writes the files out in it.
    img_format = 'PNG'

    def __init__(self, image: NDArray[np.uint8],
                 on_progress: Callable[[int, str], None] | None = None) -> None:
        self.image = image
        self.output: Image.Image | None = None
        self._on_progress = on_progress

    def progress(self, percent: int, step: str) -> None:
        """Announce a checkpoint, if anyone is listening."""
        if self._on_progress is not None:
            self._on_progress(percent, step)

    def call(self) -> None:
        raise NotImplementedError
