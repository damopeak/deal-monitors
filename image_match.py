import io
import logging

import requests

logger = logging.getLogger(__name__)

HEADERS = {'User-Agent': 'Mozilla/5.0'}
IMAGE_MATCH_THRESHOLD = 0.75  # phash similarity; above this = likely same product


def compare_images(url1: str, url2: str, timeout: int = 6) -> float:
    """Download two images and return perceptual hash similarity (0.0–1.0). Returns 0.0 on failure."""
    if not url1 or not url2:
        return 0.0
    try:
        import imagehash
        from PIL import Image

        def fetch_hash(url: str):
            r = requests.get(url, timeout=timeout, headers=HEADERS)
            r.raise_for_status()
            return imagehash.phash(Image.open(io.BytesIO(r.content)).convert('RGB'))

        diff = fetch_hash(url1) - fetch_hash(url2)  # hamming distance 0–64
        return max(0.0, 1.0 - diff / 64.0)
    except Exception as e:
        logger.debug(f"[image_match] {e}")
        return 0.0
