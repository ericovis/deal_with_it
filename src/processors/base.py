import base64
import functools

from src.models import ImageModel
from io import BytesIO

class BaseProcessor(object):
    img_format = 'PNG'

    def __init__(self, image: ImageModel) -> None:
        self.image = image
        self.output = None

    def call(self) -> None:  
        pass

    @property
    def base64_output(self) -> str:            
        if self.output is None:
            return None
        
        return self._base64_output()

    @functools.cache
    def _base64_output(self) -> str:
        arr = BytesIO()
        self.output.save(arr, format=self.img_format)
        arr = arr.getvalue()
        img = base64.b64encode(arr).decode('utf8').replace("'", '')
        return f'data:image/{self.img_format.lower()};base64,{img}'
