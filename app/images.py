"""Validate decoded image content; keep the original and make an EXIF-free preview."""
import io
import warnings
from pathlib import PurePosixPath

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_BYTES = 20 * 1024 * 1024
FORMATS = {"JPEG": ("image/jpeg", {".jpg", ".jpeg"}), "PNG": ("image/png", {".png"}),
           "WEBP": ("image/webp", {".webp"}), "HEIF": ("image/heic", {".heic", ".heif"})}


def prepare_image(data: bytes, filename: str):
    filename = PurePosixPath(filename.replace("\\", "/")).name[:180]
    filename = "".join(c for c in filename if c.isprintable() and c not in '";')
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "Elige fotos de hasta 20 MB cada una.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                kind = FORMATS.get(image.format)
                if not kind or PurePosixPath(filename).suffix.lower() not in kind[1]:
                    raise HTTPException(415, "El archivo debe ser una foto JPG, PNG, WebP o HEIC válida.")
                if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "La foto supera el límite de 40 megapíxeles.")
                image.seek(0)
                image.load()
                preview = ImageOps.exif_transpose(image)
                preview.thumbnail((1280, 1280))
                # Composite transparency; never carry EXIF/GPS into the gallery preview.
                rgba = preview.convert("RGBA")
                background = Image.new("RGB", rgba.size, "#fcfaf6")
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                background.save(output, "JPEG", quality=80, optimize=True)
                return filename, kind[0], output.getvalue()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "No pudimos leer esta foto. Elige otro archivo o expórtala como JPG.") from None
