import qrcode
from qrcode.image.svg import SvgPathImage


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")
