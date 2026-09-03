"""Processor scaffolding: take pixels in, produce a PIL image out."""

import base64
from io import BytesIO

import numpy as np
from numpy.typing import NDArray
from PIL import Image


class BaseProcessor:
    img_format = 'PNG'

    def __init__(self, image: NDArray[np.uint8]) -> None:
        self.image = image
        self.output: Image.Image | None = None
        self._encoded: str | None = None

    def call(self) -> None:
        raise NotImplementedError

    @property
    def base64_output(self) -> str | None:
        """The result as a data URI, or None if nothing was produced."""
        if self.output is None:
            return None
        # Encoded lazily and memoised on the instance. This used to be a
        # functools.cache on the method, which keyed on `self` in a global
        # dict and so pinned every processor -- and its decoded image -- in
        # memory for the lifetime of the process.
        if self._encoded is None:
            self._encoded = self._encode()
        return self._encoded

    def _encode(self) -> str:
        buffer = BytesIO()
        self.output.save(buffer, format=self.img_format)
        payload = base64.b64encode(buffer.getvalue()).decode('utf8')
        return f'data:image/{self.img_format.lower()};base64,{payload}'
