"""
Cityworks API Client and Automation Tools

Simple usage:
    from cityworks import CityworksSession, CityworksConfig
    
    config = CityworksConfig(environment='dev')
    with CityworksSession(config) as session:
        session.prompt_credentials()
        # ... make API calls
"""

from .config import CityworksConfig
from .client.session import CityworksSession
from .client.response import CityworksResponse
from .client.exceptions import (
    CityworksError,
    CityworksAuthenticationError,
    CityworksAPIError,
    CityworksRateLimitError,
    CityworksConfigError
)
from .api.work_order import WorkOrderAPI
from .api.attachments import AttachmentAPI

__version__ = "2.0.0"
__all__ = [
    'CityworksConfig',
    'CityworksSession',
    'CityworksResponse',
    'CityworksError',
    'CityworksAuthenticationError',
    'CityworksAPIError',
    'WorkOrderAPI',
    'AttachmentAPI',
]