"""Test that the package is properly installed"""

def test_import_main_package():
    """Test that main package imports successfully"""
    import cityworks
    assert hasattr(cityworks, '__version__')

def test_import_session():
    """Test that session module imports"""
    from cityworks import CityworksSession, CityworksConfig
    assert CityworksSession is not None
    assert CityworksConfig is not None

def test_import_api():
    """Test that API modules import"""
    from cityworks.api import WorkOrderAPI
    assert WorkOrderAPI is not None

def test_import_exceptions():
    """Test that exceptions import"""
    from cityworks import CityworksAuthenticationError, CityworksAPIError
    assert CityworksAuthenticationError is not None
    assert CityworksAPIError is not None