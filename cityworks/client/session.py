from typing import Optional, Dict, Any
import time
import getpass
import json
import requests
from .response import CityworksResponse
from .exceptions import CityworksAuthenticationError, CityworksAPIError
from ..config import CityworksConfig

class CityworksSession:
    """
    Manages authenticated sessions with Cityworks API.
    
    Handles:
    - Authentication and token management
    - Rate limiting
    - Request formatting
    - Session reuse
    """
    
    AUTH_ENDPOINT = '/General/Authentication'
    
    def __init__(self, config: CityworksConfig, token: Optional[str] = None):
        self.config = config
        self._token = token
        self._last_request_time = 0.0
        self._session = requests.Session()  # Reuse connections
        
        # Set default headers
        self._session.headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json'
        })
    
    @property
    def token(self) -> Optional[str]:
        """Get current authentication token"""
        return self._token
    
    @property
    def is_authenticated(self) -> bool:
        """Check if session has a token"""
        return self._token is not None
    
    def authenticate(self, username: str, password: str) -> str:
        """
        Authenticate with Cityworks and store token.
        
        Args:
            username: Cityworks username (use 'CITY/{username}' for AD auth)
            password: Cityworks password
            
        Returns:
            Authentication token
            
        Raises:
            CityworksAuthenticationError: If authentication fails
        """
        url = self.config.base_url + f"{self.AUTH_ENDPOINT}/Authenticate"
        params = {"LoginName": username, "Password": password}
        print(params)
        print(self._format_params(params, include_token=False))
        
        try:
            response = self._session.post(url, data=self._format_params(params, include_token=False))
            response.raise_for_status()
            
            cw_response = CityworksResponse(response)
            
            if not cw_response.is_success or not cw_response.value:
                error_msg = '; '.join(cw_response.error_messages) if cw_response.error_messages else 'Authentication failed'
                raise CityworksAuthenticationError(error_msg)
            
            self._token = cw_response.value['Token']
            return self._token
            
        except requests.RequestException as e:
            raise CityworksAuthenticationError(f"Authentication request failed: {str(e)}")
    
    def prompt_credentials(self, username: Optional[str] = None) -> str:
        """
        Prompt user for credentials and authenticate.
        
        Args:
            username: Optional username (will prompt if not provided)
            
        Returns:
            Authentication token
        """
        if username is None:
            username = getpass.getuser()
            #username = input("Username (CITY/{user}): ").strip()
            #if not username.startswith('CITY/'):
            #    username = f'CITY/{username}'
        
        password = getpass.getpass("Password: ")
        return self.authenticate(username, password)
    
    def validate_token(self, token: Optional[str] = None) -> bool:
        """
        Validate a token with Cityworks API.
        
        Args:
            token: Token to validate (uses session token if not provided)
            
        Returns:
            True if token is valid, False otherwise
        """
        token_to_validate = token or self._token
        if not token_to_validate:
            return False
        
        url = self.config.base_url + f"{self.AUTH_ENDPOINT}/Validate"
        params = {'Token': token_to_validate}
        
        try:
            response = self._session.post(url, data=self._format_params(params, include_token=False))
            cw_response = CityworksResponse(response)
            return cw_response.value if cw_response.is_success else False
        except:
            return False
    
    def get(self, service_category: str, service: str, endpoint: str, 
            params: Optional[Dict[str, Any]] = None) -> CityworksResponse:
        """
        Make a GET request to Cityworks API.
        
        Args:
            service_category: API category (Ams, General, Gis, Pll)
            service: Service name
            endpoint: Endpoint method
            params: Request parameters
            
        Returns:
            CityworksResponse object
        """
        #self._ensure_authenticated()
        self._apply_rate_limit()
        
        url = self.config.get_endpoint_url(service_category, service, endpoint)
        formatted_params = self._format_params(params or {})
        
        response = self._session.get(url, params=formatted_params, timeout=self.config.timeout)
        self._last_request_time = time.time()
        
        return CityworksResponse(response)
    
    def post(self, service_category: str, service: str, endpoint: str,
             params: Optional[Dict[str, Any]] = None) -> CityworksResponse:
        """
        Make a POST request to Cityworks API.
        
        Args:
            service_category: API category (Ams, General, Gis, Pll)
            service: Service name
            endpoint: Endpoint method
            params: Request parameters
            
        Returns:
            CityworksResponse object
        """
        self._ensure_authenticated()
        self._apply_rate_limit()
        
        url = self.config.get_endpoint_url(service_category, service, endpoint)
        formatted_params = self._format_params(params or {})
        
        response = self._session.post(url, data=formatted_params, timeout=self.config.timeout)
        self._last_request_time = time.time()
        
        return CityworksResponse(response)
    
    def _format_params(self, params: Dict[str, Any], include_token: bool = True) -> Dict[str, Any]:
        """Format parameters for Cityworks API request"""
        formatted = {'data': json.dumps(params)}
        if include_token and self._token:
            formatted['Token'] = self._token
        return formatted
    
    def _ensure_authenticated(self) -> None:
        """Ensure session is authenticated before making request"""
        if not self.is_authenticated:
            raise CityworksAuthenticationError()
    
    def _apply_rate_limit(self) -> None:
        """Enforce rate limiting between requests"""
        if self._last_request_time > 0:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.config.rate_limit_delay:
                time.sleep(self.config.rate_limit_delay - elapsed)
    
    def __enter__(self):
        """Context manager support"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close session on exit"""
        self._session.close()