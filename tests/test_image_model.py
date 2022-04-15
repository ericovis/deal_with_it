import pytest

from src.models import Image
from pydantic import ValidationError


def test_a_param_must_be_passed_to_the_image():
    with pytest.raises(ValidationError):
        Image()

def test_a_base64_image_should_have_a_valid_header(base64_image):
    with pytest.raises(ValidationError):
        Image(base64=f'randomHEADER,{base64_image}')

def test_a_url_must_be_exist():
    with pytest.raises(ValidationError):
        Image(url='https://example.com/image-not-found.png')

def test_a_url_must_be_an_image():
    with pytest.raises(ValidationError):
        Image(url='https://emagalha.es')

def test_valid_url_produces_image_data(): 
    image = Image(url='https://emagalha.es/images/me.jpg')
    assert image.data is not None

def test_valid_base64_produces_image_data(base64_image):
    image = Image(base64=base64_image)    
    assert image.data is not None
