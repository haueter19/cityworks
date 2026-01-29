from typing import List
import base64
from io import BytesIO
from pdf2image import convert_from_bytes

def pdf_to_base64_images(pdf_content: bytes) -> List[str]:
    """
    Convert PDF bytes to list of base64-encoded images.
    
    Args:
        pdf_content: PDF file content as bytes
        
    Returns:
        List of base64-encoded PNG images (one per page)
    """
    images = convert_from_bytes(pdf_content)
    
    base64_images = []
    for image in images:
        buffered = BytesIO()
        image.save(buffered, format="PNG")
        img_bytes = buffered.getvalue()
        img_base64 = base64.b64encode(img_bytes).decode('utf-8')
        base64_images.append(img_base64)
    
    return base64_images