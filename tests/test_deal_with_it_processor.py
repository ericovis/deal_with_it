from src.processors.deal_with_it import DealWithItProcessor
from src.models import ImageModel
from src.app import STATIC_DIR


def test_deal_with_it_processor_with_url(image_url):
    image = ImageModel(url=image_url)
    proc = DealWithItProcessor(image, STATIC_DIR)
    proc.call()
