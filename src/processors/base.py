"""Processor scaffolding: take pixels in, produce a PIL image out."""

import base64
from collections.abc import Callable
from io import BytesIO

import numpy as np
from numpy.typing import NDArray
from PIL import Image


class BaseProcessor:
    #: 'PNG' or 'JPEG'. The task sets it to match the input.
    img_format = 'PNG'
    jpeg_quality = 90

    def __init__(self, image: NDArray[np.uint8],
                 on_progress: Callable[[int, str], None] | None = None) -> None:
        self.image = image
        self.output: Image.Image | None = None
        self._encoded: str | None = None
        self._on_progress = on_progress

    def progress(self, percent: int, step: str) -> None:
        """Announce a checkpoint, if anyone is listening."""
        if self._on_progress is not None:
            self._on_progress(percent, step)

    def call(self) -> None:
        raise NotImplementedError

    @property
    def base64_output(self) -> str | None:
        """The result as a data URI, or None if nothing was produced."""
        if self.output is None:
            return None
        # Memoised on the instance, never in a module-level cache keyed on
        # self: that pins every processor and its image for the process.
        if self._encoded is None:
            self._encoded = self._encode()
        return self._encoded

    def _encode(self) -> str:
        buffer = BytesIO()
        options = {'quality': self.jpeg_quality} if self.img_format == 'JPEG' else {}
        self.output.save(buffer, format=self.img_format, **options)
        payload = base64.b64encode(buffer.getvalue()).decode('utf8')
        return f'data:image/{self.img_format.lower()};base64,{payload}'
