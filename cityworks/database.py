"""Simple database connection utilities for cityworks package."""
from typing import Optional
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus


def build_connection_string(
    database: str,
    server: Optional[str] = None,
    driver: str = "ODBC Driver 17 for SQL Server",
    username: Optional[str] = None,
    password: Optional[str] = None,
    trusted_connection: bool = True
) -> str:
    """
    Build a SQL Server connection string.
    
    Args:
        server: Database server hostname
        database: Database name
        driver: ODBC driver name
        username: Username (for SQL auth)
        password: Password (for SQL auth)
        trusted_connection: Use Windows authentication (default: True)
    
    Returns:
        SQLAlchemy connection string
    
    Examples:
        # Windows authentication
        conn_str = build_connection_string('localhost', 'Traffic')
        
        # SQL Server authentication
        conn_str = build_connection_string(
            'localhost', 'Traffic',
            username='user', password='pass',
            trusted_connection=False
        )
    """
    server_from_database = {
        'Traffic':'arcsde',
        'CMMS':'cmms',
        'PUReporting':'pubworksdbprd'
    }
    if not server:
        server = server_from_database.get(database, None)
        
    params = {
        "DRIVER": driver,
        "SERVER": server,
        "DATABASE": database,
    }
    
    if trusted_connection:
        params["Trusted_Connection"] = "yes"
    else:
        if not username or not password:
            raise ValueError("Username and password required when trusted_connection=False")
        params["UID"] = username
        params["PWD"] = password
    
    conn_str = ";".join(f"{k}={v}" for k, v in params.items())
    return f"mssql+pyodbc:///?odbc_connect={quote_plus(conn_str)}"


def get_engine(
    database: str,
    server: Optional[str] = None,
    driver: str = "ODBC Driver 17 for SQL Server",
    username: Optional[str] = None,
    password: Optional[str] = None,
    trusted_connection: bool = True,
    **engine_kwargs
) -> Engine:
    """
    Create a SQLAlchemy engine.
    
    Args:
        server: Database server hostname
        database: Database name
        driver: ODBC driver name
        username: Username (for SQL auth)
        password: Password (for SQL auth)
        trusted_connection: Use Windows authentication (default: True)
        **engine_kwargs: Additional arguments passed to create_engine()
    
    Returns:
        SQLAlchemy Engine
    
    Examples:
        # Simple usage
        engine = get_engine('localhost', 'Traffic')
        
        # With connection pooling
        engine = get_engine('localhost', 'Traffic', pool_size=10, max_overflow=20)
    """
    conn_str = build_connection_string(
        server=server,
        database=database,
        driver=driver,
        username=username,
        password=password,
        trusted_connection=trusted_connection
    )
    
    return create_engine(conn_str, **engine_kwargs)