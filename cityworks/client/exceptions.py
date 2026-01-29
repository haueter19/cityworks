class CityworksError(Exception):
    """Base exception for Cityworks-related errors"""
    pass

class CityworksAuthenticationError(CityworksError):
    """Raised when authentication fails or token is missing"""
    
    def __init__(self, message: str = "Authentication required. Please authenticate before making requests."):
        self.message = message
        super().__init__(self.message)

class CityworksAPIError(CityworksError):
    """Raised when API returns an error response"""
    pass

class CityworksRateLimitError(CityworksError):
    """Raised when rate limit is exceeded"""
    pass

class CityworksConfigError(CityworksError):
    """Raised when configuration is invalid"""
    pass

class MissingRequiredValueError(CityworksError):
    """Raised when missing a required value"""
    def __init__(self, message: str = "Missing required value"):
        self.message = message
        super().__init__(self.message)