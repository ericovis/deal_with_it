import base64
import pytest

from src.models import ImageModel
from pydantic import ValidationError


def test_a_param_must_be_passed_to_the_image():
    with pytest.raises(ValidationError):
        ImageModel()

def test_only_a_single_patem_must_be_passed_to_the_image(image_url, base64_image):
    with pytest.raises(ValidationError):
        ImageModel(url=image_url, base64=base64_image)

def test_a_base64_image_should_have_a_valid_header(base64_image):
    with pytest.raises(ValidationError):
        ImageModel(base64=f'randomHEADER,{base64_image}')


def test_a_url_must_be_valid():
    with pytest.raises(ValidationError):
        ImageModel(url='string')


def test_a_url_must_exist():
    with pytest.raises(ValidationError):
        ImageModel(url='https://emagalha.es/404.png')


def test_a_url_must_be_an_image():
    with pytest.raises(ValidationError):
        ImageModel(url='https://emagalha.es')


def test_valid_url_produces_image_data(image_url): 
    image = ImageModel(url=image_url)
    assert image.data is not None


def test_valid_base64_produces_image_data(base64_image):
    image = ImageModel(base64=base64_image)    
    assert image.data is not None
