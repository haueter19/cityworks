from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit
import json
import requests

# Headers that carry credentials; masked so a stored request is safe to log
SENSITIVE_HEADERS = {'authorization', 'cookie'}

class CityworksResponse:
    """Wrapper for Cityworks API responses with standardized error handling"""

    def __init__(self, response: requests.Response) -> None:
        self.raw_response = response
        self.url = response.url
        self.status_code = response.status_code
        self.request: Dict[str, Any] = self._build_request_info(response)

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

    @staticmethod
    def _build_request_info(response: requests.Response) -> Dict[str, Any]:
        """Describe the request that produced this response.

        If redirects occurred, the original request (history[0]) is used.
        The 'data' payload is decoded from the query string (GET) or body (POST).
        """
        prepared = response.history[0].request if response.history else response.request
        if prepared is None:
            return {}

        split = urlsplit(prepared.url or '')
        body = prepared.body.decode() if isinstance(prepared.body, bytes) else prepared.body
        form = parse_qs(body or '') or parse_qs(split.query)
        data: Any = None
        if 'data' in form:
            try:
                data = json.loads(form['data'][0])
            except json.JSONDecodeError:
                data = form['data'][0]

        headers = {k: ('***' if k.lower() in SENSITIVE_HEADERS else v)
                   for k, v in prepared.headers.items()}

        return {
            'method': prepared.method,
            'url': prepared.url,
            'endpoint': f"{split.scheme}://{split.netloc}{split.path}",
            'data': data,
            'headers': headers,
            'body': body,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return the response contents as a plain dictionary"""
        return {
            'value': self.value,
            'status': self.status,
            'status_code': self.status_code,
            'url': self.url,
            'error_messages': self.error_messages,
            'warning_messages': self.warning_messages,
            'success_messages': self.success_messages,
            'request': self.request,
        }

    def __getitem__(self, key: str) -> Any:
        """Allow dict-style access, e.g. response['value'] or response['request']"""
        return self.to_dict()[key]

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