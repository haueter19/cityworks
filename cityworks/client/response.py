from typing import Any, Dict, List, Optional
import json
import requests

class CityworksResponse:
    """Wrapper for Cityworks API responses with standardized error handling"""
    
    def __init__(self, response: requests.Response) -> None:
        self.raw_response = response
        self.url = response.url
        self.status_code = response.status_code
        
        # Parse JSON response
        try:
            data = response.json()
            self.value: Optional[Any] = data.get('Value')
            self.status: int = data.get('Status', 0)
            self.error_messages: List[str] = data.get('ErrorMessages', [])
            self.warning_messages: List[str] = data.get('WarningMessages', [])
            self.success_messages: List[str] = data.get('SuccessMessages', [])
        except (json.JSONDecodeError, KeyError) as e:
            self.value = None
            self.status = 0
            self.error_messages = [f'Failed to parse response from {self.url}: {str(e)}']
            self.warning_messages = []
            self.success_messages = []
    
    @property
    def is_success(self) -> bool:
        """Check if the response indicates success"""
        return self.status == 0 and self.value is not None
    
    @property
    def has_errors(self) -> bool:
        """Check if response has errors"""
        return bool(self.error_messages) or not self.is_success
    
    def raise_for_status(self) -> None:
        """Raise exception if response has errors"""
        if self.has_errors:
            from .exceptions import CityworksAPIError
            error_msg = '; '.join(self.error_messages) if self.error_messages else 'Unknown error'
            raise CityworksAPIError(f"API request failed: {error_msg}")
    
    def __str__(self) -> str:
        return json.dumps(self.raw_response.json(), indent=2)
    
    def __repr__(self) -> str:
        return f"<CityworksResponse status={self.status} has_value={self.value is not None}>"