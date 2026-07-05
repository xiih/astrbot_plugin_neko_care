from pathlib import Path

from PIL import Image


OUTPUT_IMAGE_SCALE = 0.85


def scaled_output_image(image: Image.Image, scale: float = OUTPUT_IMAGE_SCALE) -> Image.Image:
    try:
        scale = float(scale)
    except Exception:
        scale = 1.0
    if scale <= 0 or abs(scale - 1.0) < 0.001:
        return image
    width, height = image.size
    target = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    if target == image.size:
        return image
    return image.resize(target, Image.LANCZOS)


def save_scaled_image(image: Image.Image, out: Path, fmt: str = "PNG", scale: float = OUTPUT_IMAGE_SCALE, **save_kwargs) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    scaled_output_image(image, scale).save(out, fmt, **save_kwargs)
