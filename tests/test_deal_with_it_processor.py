from src.processors.deal_with_it import DealWithItProcessor
from src.models import ImageModel
from src.app import STATIC_DIR


def test_deal_with_it_processor_with_url(base64_image):
    image = ImageModel(base64=base64_image)
    proc = DealWithItProcessor(image, STATIC_DIR)
    proc.call()
    assert proc.base64_output is not None
    assert proc.base64_output != base64_image
