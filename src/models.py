import re
import base64
import requests
import image_to_numpy

from io import BytesIO
from typing import Any
from pydantic import (
    BaseModel,
    validator,
    PrivateAttr,
    root_validator
)


class Image(BaseModel):
    url: str | None = None
    base64: str | None = None
    _data: Any = PrivateAttr(None)  

    def __init__(self, **data: Any):
        super().__init__(**data)
        self._load_image_data()
    
    @property
    def data(self):
        return self._data

    @root_validator(pre=True)
    def a_parameter_must_be_passed(cls, values):
        if True not in map(lambda x: bool(x), values):
            raise ValueError('An url or a base64 string must be passed')
        return values
            
    @validator('base64')
    def base64_must_have_a_header(cls, v):    
        pattern = re.compile('^data:image/.+;base64,')            
        if not pattern.match(v):
            raise ValueError('base64 image must have a format header')        
        return v

    @validator('url')
    def url_must_be_an_existing_image(cls, v):
        req = requests.head(v)
        if req.status_code != 200:
            raise ValueError(f'The URL is not acessible. HTTP status: {req.status_code}')
        if not req.headers['content-type'].startswith('image/'):
            raise ValueError('The URL is not an image')
        return v

    def _load_image_data(self):
        if self.base64 is not None:
            base64_data = re.sub('^data:image/.+;base64,', '', self.base64)
            byte_data = base64.b64decode(base64_data)
            image_data = BytesIO(byte_data) 
            self._data = image_to_numpy.load_image_file(image_data)
        else:
            req = requests.get(self.url)
            self._data = image_to_numpy.load_image_file(BytesIO(req.content))
