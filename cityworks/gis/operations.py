"""General-purpose GIS operations using existing ConnectionManager"""

from typing import Dict, Any, List, Optional, Union
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine
import pandas as pd
import geopandas as gpd
import re
from cityworks.database import get_engine


def format_sde_query(table_name) -> str: 
    """ Dynamically find columns from the SDE table 
    Do it this way because SQL Alchemy can't handle the binary geometry column stored in an ArcSDE table.
    This function will query the table for all columns and data types then dynamically build a query that applies the .STAsBinary() function to the geometry column
    OUTPUT: SELECT col1, col2, coln, geometry_column FROM YOUR_TABLE_NAME
    """
    table_info = pd.read_sql(f"""
        SELECT 
            TABLE_SCHEMA,
            TABLE_NAME,
            COLUMN_NAME,
            DATA_TYPE,
            CHARACTER_MAXIMUM_LENGTH,
            NUMERIC_PRECISION,
            NUMERIC_SCALE,
            IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE 
            TABLE_SCHEMA = 'data_admin8'
            AND TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION;
        """, get_engine('Traffic'))
    
    normal_cols = [col for col, col_type in zip(table_info['COLUMN_NAME'], table_info['DATA_TYPE']) if col_type != 'geometry']
    geo_cols = [col+f'.STAsBinary() as geometry' for col, col_type in zip(table_info['COLUMN_NAME'], table_info['DATA_TYPE']) if col_type == 'geometry']
    if len(geo_cols) >= 1 and len(normal_cols) >= 1:
        return "SELECT " + ", ".join(normal_cols) + f", {geo_cols[0]} FROM Traffic.data_admin8.{table_name} WHERE "
    if len(geo_cols) == 0:
        return "SELECT " + ", ".join(normal_cols) + f" FROM Traffic.data_admin8.{table_name} WHERE "
    else:
        return "SELECT " + ", ".join(normal_cols) + f", {geo_cols[0]} FROM Traffic.data_admin8.{table_name} WHERE "


def insert_row(
    engine: Engine,
    table_name: str,
    data: Dict[str, Any],
    schema: str = 'azteca'
) -> int:
    """
    Insert a single row into a table.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        table_name: Name of the table (e.g., 'PU_SPACESOUTOFSERVICE')
        data: Dictionary of column names to values
        schema: Database schema (default: 'azteca')
        
    Returns:
        The IDENTITY value (OBJECTID or similar) of the inserted row
        
    Example:
        from db_manager import ConnectionManager
        cnxn = ConnectionManager()
        engine = cnxn.get_engine('sde')
        
        oid = insert_row(engine, 'PU_SPACESOUTOFSERVICE', {
            'SPACEID': 'SP-123',
            'REASON': 'Construction',
            'STARTDATE': datetime.now()
        })
    """
    # Build column and value lists
    columns = list(data.keys())
    values = list(data.values())
    
    # Create placeholders for parameterized query
    placeholders = ', '.join([f':{col}' for col in columns])
    column_names = ', '.join(columns)
    
    # Build INSERT statement with OUTPUT clause to get inserted ID
    query = text(f"""
        INSERT INTO {schema}.{table_name} ({column_names})
        OUTPUT INSERTED.OBJECTID
        VALUES ({placeholders})
    """)
    
    with engine.begin() as conn:
        result = conn.execute(query, data)
        inserted_id = result.scalar()
    
    return inserted_id


def insert_rows(
    engine: Engine,
    table_name: str,
    data_list: List[Dict[str, Any]],
    schema: str = 'azteca'
) -> List[int]:
    """
    Insert multiple rows efficiently.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        table_name: Name of the table
        data_list: List of dictionaries with column data
        schema: Database schema (default: 'azteca')
        
    Returns:
        List of inserted OBJECTIDs
        
    Example:
        engine = cnxn.get_engine('sde')
        
        data = [
            {'SPACEID': 'SP-123', 'REASON': 'Construction'},
            {'SPACEID': 'SP-124', 'REASON': 'Repair'}
        ]
        oids = insert_rows(engine, 'PU_SPACESOUTOFSERVICE', data)
    """
    if not data_list:
        return []
    
    # Convert to DataFrame for efficient bulk insert
    df = pd.DataFrame(data_list)
    
    # Use pandas to_sql with method='multi' for bulk insert
    # Note: This doesn't return IDs easily, so we'll use individual inserts
    # if you need the IDs, or use bulk insert if you don't need them
    
    inserted_ids = []
    for data in data_list:
        oid = insert_row(engine, table_name, data, schema)
        inserted_ids.append(oid)
    
    return inserted_ids


def insert_rows_bulk(
    engine: Engine,
    table_name: str,
    data_list: List[Dict[str, Any]],
    schema: str = 'azteca'
) -> int:
    """
    Bulk insert multiple rows (faster, but doesn't return IDs).
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        table_name: Name of the table
        data_list: List of dictionaries with column data
        schema: Database schema (default: 'azteca')
        
    Returns:
        Number of rows inserted
    """
    if not data_list:
        return 0
    
    df = pd.DataFrame(data_list)
    
    with engine.begin() as conn:
        df.to_sql(
            table_name,
            conn,
            schema=schema,
            if_exists='append',
            index=False,
            method='multi'
        )
    
    return len(data_list)


def update_status(
    engine: Engine,
    table_name: str,
    record_id: Any,
    status: str,
    id_field: str = 'OBJECTID',
    status_field: str = 'STATUS',
    schema: str = 'azteca'
) -> int:
    """
    Update the status field of a record.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        table_name: Name of the table
        record_id: Value to match in the ID field
        status: New status value
        id_field: Name of the ID field to match on
        status_field: Name of the status field to update
        schema: Database schema (default: 'azteca')
        
    Returns:
        Number of rows updated (should be 1 if successful, 0 if not found)
        
    Example:
        engine = cnxn.get_engine('sde')
        
        rows_updated = update_status(
            engine, 
            'PU_METERS_ON_ST', 
            'MTR-2395',
            'INACTIVE',
            id_field='METERID',
            status_field='METERSTATUS'
        )
    """
    # Build parameterized UPDATE query
    if isinstance(record_id, str):
        where_clause = f"{id_field} = :id_value"
    else:
        where_clause = f"{id_field} = :id_value"
    
    query = text(f"""
        UPDATE {schema}.{table_name}
        SET {status_field} = :status_value
        WHERE {where_clause}
    """)
    
    with engine.begin() as conn:
        result = conn.execute(query, {
            'id_value': record_id,
            'status_value': status
        })
        rows_updated = result.rowcount
    
    return rows_updated


def update_fields(
    engine: Engine,
    table_name: str,
    record_id: Any,
    updates: Dict[str, Any],
    id_field: str = 'OBJECTID',
    schema: str = 'azteca'
) -> int:
    """
    Update multiple fields of a record.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        table_name: Name of the table
        record_id: Value to match in the ID field
        updates: Dictionary of field names to new values
        id_field: Name of the ID field to match on
        schema: Database schema (default: 'azteca')
        
    Returns:
        Number of rows updated
        
    Example:
        engine = cnxn.get_engine('sde')
        
        rows_updated = update_fields(
            engine,
            'PU_SPACESOUTOFSERVICE',
            'SP-123',
            {
                'ENDDATE': datetime.now(),
                'NOTES': 'Reopened after construction'
            },
            id_field='SPACEID'
        )
    """
    if not updates:
        return 0
    
    # Build SET clause
    set_clauses = ', '.join([f"{field} = :{field}" for field in updates.keys()])
    
    # Build WHERE clause
    query = text(f"""
        UPDATE {schema}.{table_name}
        SET {set_clauses}
        WHERE {id_field} = :id_value
    """)
    
    # Combine parameters
    params = {**updates, 'id_value': record_id}
    
    with engine.begin() as conn:
        result = conn.execute(query, params)
        rows_updated = result.rowcount
    
    return rows_updated


def get_max_value(table_name: str, field: str, db: str = 'Traffic', schema: str = 'data_admin8') -> Dict[str, Any]:
    """Returns max value for user defined table and field"""
    max_value = pd.read_sql(f"SELECT max({field}) max_id FROM {db}.{schema}.{table_name}")
    return {field:max_value}



def query_records(
    engine: Engine,
    table_name: str,
    where_clause: str = "1=1",
    fields: Optional[List[str]] = None,
    schema: str = 'azteca',
    params: Optional[Dict[str, Any]] = None
) -> pd.DataFrame:
    """
    Query multiple records from a table, returning a DataFrame.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        table_name: Name of the table
        where_clause: SQL where clause (can use :param_name for parameters)
        fields: List of field names to retrieve (None = all fields)
        schema: Database schema (default: 'azteca')
        params: Dictionary of parameters for the where clause
        
    Returns:
        DataFrame with query results
        
    Example:
        engine = cnxn.get_engine('sde')
        
        # Query with parameters
        df = query_records(
            engine,
            'PU_SPACESOUTOFSERVICE',
            where_clause="ENDDATE IS NULL AND STARTDATE < :cutoff_date",
            params={'cutoff_date': '2024-01-01'}
        )
    """
    field_list = ', '.join(fields) if fields else '*'
    
    query = f"""
        SELECT {field_list}
        FROM {schema}.{table_name}
        WHERE {where_clause}
    """
    
    df = pd.read_sql(query, engine, params=params or {})
    return df


def execute_query(
    engine: Engine,
    query: str,
    params: Optional[Dict[str, Any]] = None,
    return_df: bool = True
) -> Union[pd.DataFrame, List[Dict[str, Any]]]:
    """
    Execute a custom SQL query.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        query: SQL query string (can use :param_name for parameters)
        params: Dictionary of parameters for the query
        return_df: If True, return DataFrame; if False, return list of dicts
        
    Returns:
        Query results as DataFrame or list of dictionaries
        
    Example:
        engine = cnxn.get_engine('sde')
        
        df = execute_query(
            engine,
            '''
            SELECT s.SPACEID, s.REASON, w.WORKORDERID
            FROM azteca.PU_SPACESOUTOFSERVICE s
            LEFT JOIN azteca.WORKORDER w ON s.WORKORDERID = w.WORKORDERSID
            WHERE s.ENDDATE IS NULL
            ''',
            return_df=True
        )
    """
    if return_df:
        return pd.read_sql(query, engine, params=params or {})
    else:
        with engine.connect() as conn:
            result = conn.execute(text(query), params or {})
            return [dict(row._mapping) for row in result]
        


def get_spaces_out_of_service(spaces: List[str]) -> pd.DataFrame:
    """Query the PU_SPACESOUTOFSERVICE table for historical records of spaces out of service"""
    
    #base = format_sde_query('PU_SPACESOUTOFSERVICE')
    space_list = "','".join(spaces)
    where_clause = f"SpaceNumber IN ('{space_list}')"
    
    return gpd.read_postgis(f"""
        SELECT 
            OBJECTID, Event_ID, SpaceNumber, Date_Out, Date_Returned, Space_Type, 
            Note, RevenueCollected, RemovalMethod, RemovedBy, ReasonRemoved, ADA_Relocation,
            created_user, created_date, last_edited_user, last_edited_date, SHAPE.STAsBinary() as geometry
        FROM Traffic.data_admin8.PU_SPACESOUTOFSERVICE
        WHERE 
            {where_clause}
        ORDER BY SpaceNumber, Date_Out, Date_Returned
        """, get_engine('Traffic'), geom_col='geometry', crs="EPSG:8193")