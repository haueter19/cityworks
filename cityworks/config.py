from dataclasses import dataclass
from typing import Dict, Optional
import os

@dataclass
class CityworksConfig:
    """Configuration for Cityworks API connection"""
    environment: str  # 'dev', 'test', 'prod'
    timeout: int = 30
    rate_limit_delay: float = 5.0
    retry_attempts: int = 3

    _base_urls: Optional[Dict[str, str]] = None

    @property
    def base_url(self) -> str:
        """Get the base URL for the configured environment.

        The production URL changed in the 23.15 upgrade:
            Old (requires Authorization token header): /Cityworks23/services
            New (accepts session cookies):             /cityworks/Services
        """
        urls = self._base_urls or {
            'dev':  os.getenv('CITYWORKS_DEV_URL',  'http://cmmsappdev/cityworks_dev/Services'),
            'test': os.getenv('CITYWORKS_TEST_URL', 'http://cmmsappdev/cityworks_test/Services'),
            'prod': os.getenv('CITYWORKS_PROD_URL', 'https://cmms.cityofmadison.com/cityworks/Services'),
        }

        if self.environment not in urls:
            raise ValueError(
                f"Invalid environment: {self.environment!r}. "
                f"Must be one of {list(urls.keys())}"
            )
        return urls[self.environment]

    def get_endpoint_url(self, service_category: str, service: str, endpoint: str) -> str:
        """Build full endpoint URL."""
        return f"{self.base_url}/{service_category}/{service}/{endpoint}"