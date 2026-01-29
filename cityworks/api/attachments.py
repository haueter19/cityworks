from typing import Optional
from pathlib import Path
import os
import requests
import time
from ..client.session import CityworksSession
from ..client.exceptions import CityworksAPIError
from ..utils.pdf import pdf_to_base64_images  # Moved to utils!

class AttachmentAPI:
    """API for work order attachment operations"""
    
    def __init__(self, session: CityworksSession):
        self.session = session
    
    def download(self, attachment_id: int, save_path: Optional[str] = None,
                filename: Optional[str] = None, as_base64: bool = False):
        """
        Download work order attachment.
        
        Args:
            attachment_id: Attachment ID
            save_path: Directory to save file (defaults to Downloads)
            filename: Filename (auto-detected if not provided)
            as_base64: Return as base64 images instead of saving
            
        Returns:
            File path if saved, list of base64 images if as_base64=True
        """
        self.session._ensure_authenticated()
        self.session._apply_rate_limit()
        
        url = self.session.config.get_endpoint_url('Ams', 'Attachment', 'Download')
        params = self.session._format_params({'AttachmentID': attachment_id})
        
        response = self.session._session.get(url, params=params, timeout=self.session.config.timeout)
        self.session._last_request_time = time.time()
        
        if not response.ok:
            raise CityworksAPIError(f"Download failed: {response.status_code}")
        
        # Get filename from headers or generate one
        if filename is None:
            filename = self._extract_filename(response, attachment_id)
        
        if as_base64:
            # Convert PDF to base64 images
            if b'%PDF' in response.content[:10]:  # Check if it's a PDF
                return pdf_to_base64_images(response.content)
            else:
                raise ValueError("as_base64 only supported for PDF files")
        else:
            # Save to disk
            save_path = save_path or str(Path.home() / "Downloads")
            full_path = os.path.join(save_path, filename)
            
            with open(full_path, 'wb') as f:
                f.write(response.content)
            
            return full_path
    
    def _extract_filename(self, response: requests.Response, attachment_id: int) -> str:
        """Extract filename from response headers or generate default"""
        content_disposition = response.headers.get('Content-Disposition', '')
        
        if 'filename=' in content_disposition:
            return content_disposition.split('filename=')[1].strip('"')
        
        # Generate filename based on content type
        content_type = response.headers.get('Content-Type', '')
        ext = '.bin'
        if 'pdf' in content_type:
            ext = '.pdf'
        elif 'image' in content_type:
            ext = '.jpg'
        
        return f"attachment_{attachment_id}{ext}"