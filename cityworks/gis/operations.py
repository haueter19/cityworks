"""SDE database operations for GIS tables in Traffic.data_admin8.

This module is the single point of contact for all SQL generation and execution
against the ArcSDE. Callers (parking.py, etc.) should never build raw SQL —
they prepare data dicts and call functions here.
"""

from typing import Dict, Any, List, Optional, Union
from contextlib import contextmanager
import pandas as pd
import geopandas as gpd
from sqlalchemy import text
from cityworks.database import get_engine


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB = "Traffic"
SCHEMA = "data_admin8"
SRID = 103412
CRS = "EPSG:8193"

# Column name for Cityworks Asset ID varies by table
CITYWORKS_FIELD_MAP = {
    "PU_METERS_ON_ST": "cwAssetID",
    "PU_LZ_ON_ST": "CWasset_ID",
    "PU_DISVET_ON_ST": "CWasset_ID",
    "PU_OFF_ST_SPACES": "cwAssetID",
    "PU_REVENUE_CREDITCARDTERMINALS": "cwAssetID",
}

# Column name for status varies by table
STATUS_FIELD_MAP = {
    "PU_METERS_ON_ST": "Status",
    "PU_LZ_ON_ST": "Status",
    "PU_DISVET_ON_ST": "Status",
    "PU_OFF_ST_SPACES": "Space_Status",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_engine():
    """Return the Traffic SDE engine."""
    return get_engine(DB)


def _fqn(table_name: str) -> str:
    """Return fully-qualified table name: Traffic.data_admin8.<table>."""
    return f"{DB}.{SCHEMA}.{table_name}"


# ---------------------------------------------------------------------------
# Field-map accessors
# ---------------------------------------------------------------------------

def get_cw_asset_field(table_name: str) -> str:
    """Return the Cityworks asset ID column name for *table_name*.

    Raises ValueError if the table is not in the field map.
    """
    if table_name not in CITYWORKS_FIELD_MAP:
        raise ValueError(f"Unknown table for CW asset field: {table_name}")
    return CITYWORKS_FIELD_MAP[table_name]


def get_status_field(table_name: str) -> str:
    """Return the status column name for *table_name*.

    Raises ValueError if the table is not in the field map.
    """
    if table_name not in STATUS_FIELD_MAP:
        raise ValueError(f"Unknown table for status field: {table_name}")
    return STATUS_FIELD_MAP[table_name]


# ---------------------------------------------------------------------------
# Transaction context manager
# ---------------------------------------------------------------------------

@contextmanager
def sde_transaction():
    """Yield a connection inside a transaction.

    Usage::

        with sde_transaction() as conn:
            sde_update(..., conn=conn)
            sde_insert(..., conn=conn)
            # commits on clean exit, rolls back on exception
    """
    with _get_engine().begin() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def format_sde_query(table_name: str) -> str:
    """Build a SELECT that applies .STAsBinary() to geometry columns.

    Queries INFORMATION_SCHEMA to discover columns, then returns a string
    ending with ``WHERE `` ready for the caller to append conditions.
    """
    table_info = pd.read_sql(f"""
        SELECT
            COLUMN_NAME,
            DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE
            TABLE_SCHEMA = '{SCHEMA}'
            AND TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION;
        """, _get_engine())

    normal_cols = [
        col for col, dtype in zip(table_info["COLUMN_NAME"], table_info["DATA_TYPE"])
        if dtype != "geometry"
    ]
    geo_cols = [
        f"{col}.STAsBinary() as geometry"
        for col, dtype in zip(table_info["COLUMN_NAME"], table_info["DATA_TYPE"])
        if dtype == "geometry"
    ]

    fqn = _fqn(table_name)
    col_list = ", ".join(normal_cols)
    if geo_cols:
        col_list += f", {geo_cols[0]}"

    return f"SELECT {col_list} FROM {fqn} WHERE "


def sde_query(
    table_name: str,
    where_clause: str,
    params: Optional[Dict[str, Any]] = None,
    as_geodataframe: bool = True,
    conn=None,
) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
    """Query an SDE table with automatic geometry handling.

    Args:
        table_name: Table name without schema (e.g. ``'PU_METERS_ON_ST'``).
        where_clause: SQL WHERE conditions **without** the ``WHERE`` keyword.
            Use ``:param_name`` for parameterised values.
        params: Parameter dict for the where clause.
        as_geodataframe: If True and the table has a geometry column, return
            a GeoDataFrame.  Falls back to a plain DataFrame on failure.
        conn: Optional connection for transactional reads.

    Returns:
        GeoDataFrame or DataFrame.
    """
    base = format_sde_query(table_name)
    full_query = base + where_clause
    engine_or_conn = conn if conn is not None else _get_engine()

    if as_geodataframe:
        try:
            return gpd.read_postgis(
                full_query, engine_or_conn,
                geom_col="geometry", crs=CRS, params=params if params else None,
            )
        except Exception:
            pass  # fall through to plain DataFrame

    return pd.read_sql(full_query, engine_or_conn, params=params if params else None)


def get_spaces_out_of_service(spaces: List[str]) -> gpd.GeoDataFrame:
    """Query PU_SPACESOUTOFSERVICE for historical records of the given spaces."""
    space_list = "','".join(spaces)
    where_clause = f"SpaceNumber IN ('{space_list}')"

    return gpd.read_postgis(f"""
        SELECT
            OBJECTID, Event_ID, SpaceNumber, Date_Out, Date_Returned, Space_Type,
            Note, RevenueCollected, RemovalMethod, RemovedBy, ReasonRemoved, ADA_Relocation,
            created_user, created_date, last_edited_user, last_edited_date,
            SHAPE.STAsBinary() as geometry
        FROM {_fqn('PU_SPACESOUTOFSERVICE')}
        WHERE
            {where_clause}
        ORDER BY SpaceNumber, Date_Out, Date_Returned
        """, _get_engine(), geom_col="geometry", crs=CRS)


def get_active_terminal_assignment(uid: str, conn=None) -> pd.DataFrame:
    """Query the active CC terminal assignment by UID.

    Joins PU_REVENUE_CREDITCARDTERMINALS with PU_CC_TERMINAL_HISTORY.

    Args:
        uid: Terminal UID value.
        conn: Optional connection for transactional use.

    Returns:
        DataFrame with the active assignment row(s).
    """
    sql = text(f"""
        SELECT
            ct.DeviceID base_device_id, ct.cwAssetID, ct.UID, ch.*
        FROM {_fqn('PU_REVENUE_CREDITCARDTERMINALS')} ct
        INNER JOIN {_fqn('PU_CC_TERMINAL_HISTORY')} ch
            ON ct.DeviceID = ch.DeviceID
        WHERE
            ct.UID = :uid
            AND ch.DateRemoved IS NULL
        ORDER BY ct.UID, ch.DateAssigned, ch.DateRemoved
    """)

    if conn is not None:
        result = conn.execute(sql, {"uid": uid})
        return pd.DataFrame([dict(r._mapping) for r in result])
    return pd.read_sql(sql, _get_engine(), params={"uid": uid})


# ---------------------------------------------------------------------------
# Auto-increment helper
# ---------------------------------------------------------------------------

def get_next_id(table_name: str, field: str = "OBJECTID", conn=None) -> int:
    """Return ``MAX(field) + 1`` from *table_name*.

    Useful when the ID is needed *before* the insert (e.g. to derive
    DeviceID from OBJECTID).

    Args:
        table_name: SDE table name (without schema).
        field: Column to increment.
        conn: Optional connection for transactional use.
    """
    sql = text(f"SELECT MAX({field}) + 1 AS next_id FROM {_fqn(table_name)}")

    if conn is not None:
        row = conn.execute(sql).fetchone()
        return int(row[0])
    result = pd.read_sql(sql, _get_engine())
    return int(result["next_id"].iloc[0])


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def sde_insert(
    table_name: str,
    data: Dict[str, Any],
    auto_increment_fields: Optional[List[str]] = None,
    geometry_point: Optional[Dict[str, float]] = None,
    geometry_column: str = "Shape",
    conn=None,
) -> int:
    """Insert a row into an SDE table.

    Handles OBJECTID-style auto-increment and geometry point creation.

    Args:
        table_name: SDE table name (without schema).
        data: Column-name → value pairs.  Keys that overlap with
            *auto_increment_fields* are treated as explicit values (no
            subquery generated for them).
        auto_increment_fields: Columns that should be filled with
            ``(SELECT MAX(col)+1 …)`` if they are **not** already in *data*.
        geometry_point: ``{'x': float, 'y': float}`` for a point geometry.
        geometry_column: Name of the geometry column (default ``'Shape'``).
        conn: Optional connection for transactional use.

    Returns:
        Number of rows inserted (1 on success).
    """
    auto_increment_fields = auto_increment_fields or []
    fqn = _fqn(table_name)

    columns: List[str] = []
    value_exprs: List[str] = []
    params: Dict[str, Any] = {}

    # Auto-increment fields NOT already provided in data
    for field in auto_increment_fields:
        if field not in data:
            columns.append(field)
            value_exprs.append(f"(SELECT MAX({field})+1 FROM {fqn})")

    # Regular data columns
    for col, val in data.items():
        columns.append(col)
        value_exprs.append(f":{col}")
        params[col] = val

    # Geometry point
    if geometry_point is not None:
        columns.append(geometry_column)
        value_exprs.append(
            f"geometry::STPointFromText("
            f"'POINT(' + CAST(:_geo_x AS varchar(50)) + ' ' "
            f"+ CAST(:_geo_y AS varchar(50)) + ')', {SRID})"
        )
        params["_geo_x"] = geometry_point["x"]
        params["_geo_y"] = geometry_point["y"]

    col_str = ", ".join(columns)
    val_str = ", ".join(value_exprs)
    sql = text(f"INSERT INTO {fqn} ({col_str}) VALUES ({val_str})")

    if conn is not None:
        return conn.execute(sql, params).rowcount
    with _get_engine().begin() as auto_conn:
        return auto_conn.execute(sql, params).rowcount


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def sde_update(
    table_name: str,
    updates: Dict[str, Any],
    where_clause: str,
    params: Optional[Dict[str, Any]] = None,
    conn=None,
) -> int:
    """Update rows in an SDE table.

    Args:
        table_name: SDE table name (without schema).
        updates: Column-name → new-value pairs for the SET clause.
        where_clause: SQL conditions **without** ``WHERE``.
            Use ``:param_name`` for parameterised values.
        params: Additional parameters for the where clause.
        conn: Optional connection for transactional use.

    Returns:
        Number of rows updated.
    """
    params = dict(params) if params else {}
    fqn = _fqn(table_name)

    set_parts: List[str] = []
    for col, val in updates.items():
        param_name = f"_upd_{col}"
        set_parts.append(f"{col} = :{param_name}")
        params[param_name] = val

    set_str = ", ".join(set_parts)
    sql = text(f"UPDATE {fqn} SET {set_str} WHERE {where_clause}")

    if conn is not None:
        return conn.execute(sql, params).rowcount
    with _get_engine().begin() as auto_conn:
        return auto_conn.execute(sql, params).rowcount
