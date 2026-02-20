"""Revenue database operations for PUReporting.app tables.

This module is the single point of contact for all SQL against PUReporting.
Callers (parking.py) prepare data dicts and call functions here —
they never build raw SQL themselves.
"""

from typing import Optional
from contextlib import contextmanager
import pandas as pd
from sqlalchemy import text

from cityworks.database import get_engine


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB = "PUReporting"
SCHEMA = "app"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_engine():
    """Return the PUReporting engine."""
    return get_engine(DB)


def _fqn(table_name: str) -> str:
    """Return schema-qualified table name: app.<table>."""
    return f"{SCHEMA}.{table_name}"


# ---------------------------------------------------------------------------
# Transaction context manager
# ---------------------------------------------------------------------------

@contextmanager
def revenue_transaction():
    """Yield a connection inside a transaction against PUReporting.

    Usage::

        with revenue_transaction() as conn:
            close_device_assignment(..., conn=conn)
            insert_device_assignment(..., conn=conn)
            # commits on clean exit, rolls back on exception
    """
    with _get_engine().begin() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_device_id(entity_uid: str, conn=None) -> Optional[int]:
    """Return the revenue device_id for a terminal by its UID.

    Args:
        entity_uid: The terminal's UID (matches EntityUid on the work order asset).
        conn: Optional connection for transactional use.

    Returns:
        Integer device_id, or *None* if not found.
    """
    sql = text(
        f"SELECT device_id FROM {_fqn('dim_device')} WHERE device_terminal_id = :uid"
    )
    if conn is not None:
        row = conn.execute(sql, {"uid": entity_uid}).fetchone()
        return int(row[0]) if row else None
    df = pd.read_sql(sql, _get_engine(), params={"uid": entity_uid})
    return int(df["device_id"].iloc[0]) if len(df) > 0 else None


def get_location_id(destination: str, conn=None) -> Optional[int]:
    """Return the revenue location_id for a facility nickname.

    Joins dim_facility and dim_location to find the location that corresponds
    to the parking garage identified by *destination*.

    Args:
        destination: Facility nickname (e.g. ``'OC'``, ``'SSCO'``).
        conn: Optional connection for transactional use.

    Returns:
        Integer location_id, or *None* if not found.
    """
    sql = text(f"""
        SELECT l.location_id
        FROM {_fqn('dim_facility')} f
        INNER JOIN {_fqn('dim_location')} l ON f.facility_id = l.facility_id
        WHERE f.facility_type = 'garage'
          AND f.facility_nickname = :destination
    """)
    if conn is not None:
        row = conn.execute(sql, {"destination": destination}).fetchone()
        return int(row[0]) if row else None
    df = pd.read_sql(sql, _get_engine(), params={"destination": destination})
    return int(df["location_id"].iloc[0]) if len(df) > 0 else None


def get_active_device_assignment(device_id: int, conn=None) -> pd.DataFrame:
    """Return the active assignment row(s) for a device (end_date IS NULL).

    Args:
        device_id: Revenue device_id from dim_device.
        conn: Optional connection for transactional use.

    Returns:
        DataFrame with the active assignment row(s).
    """
    sql = text(f"""
        SELECT *
        FROM {_fqn('fact_device_assignment')}
        WHERE device_id = :device_id
          AND end_date IS NULL
    """)
    if conn is not None:
        result = conn.execute(sql, {"device_id": device_id})
        return pd.DataFrame([dict(r._mapping) for r in result])
    return pd.read_sql(sql, _get_engine(), params={"device_id": device_id})


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def close_device_assignment(
    assignment_id: int,
    end_date: str,
    end_by_id: int,
    wo_remove_id,
    notes: str,
    conn,
) -> int:
    """Close an active fact_device_assignment record.

    Args:
        assignment_id: Primary key of the record to close.
        end_date: Date the assignment ended (ISO string or datetime).
        end_by_id: Employee ID of the person closing the assignment.
        wo_remove_id: Work order identifier requesting the change.
        notes: Free-text notes (typically the destination location).
        conn: Connection (required — must be inside a transaction).

    Returns:
        Number of rows updated (should be 1).
    """
    sql = text(f"""
        UPDATE {_fqn('fact_device_assignment')}
        SET end_date          = :end_date,
            end_by_id         = :end_by_id,
            workorder_remove_id = :wo_remove_id,
            notes             = :notes
        WHERE assignment_id = :assignment_id
    """)
    return conn.execute(sql, {
        "end_date": end_date,
        "end_by_id": end_by_id,
        "wo_remove_id": wo_remove_id,
        "notes": notes,
        "assignment_id": assignment_id,
    }).rowcount


def insert_device_assignment(
    device_id: int,
    location_id: int,
    assign_date: str,
    assign_by_id: int,
    wo_assign_id,
    notes: str,
    conn,
) -> int:
    """Insert a new active fact_device_assignment record.

    The assignment_id primary key is generated by the database.

    Args:
        device_id: Revenue device_id for the terminal.
        location_id: Revenue location_id for the destination.
        assign_date: Date of the new assignment (ISO string or datetime).
        assign_by_id: Employee ID of the person creating the assignment.
        wo_assign_id: Work order identifier authorising the assignment.
        notes: Free-text notes (typically the destination location).
        conn: Connection (required — must be inside a transaction).

    Returns:
        Number of rows inserted (should be 1).
    """
    sql = text(f"""
        INSERT INTO {_fqn('fact_device_assignment')}
            (device_id, location_id, assign_date, assign_by_id, workorder_assign_id, notes)
        VALUES
            (:device_id, :location_id, :assign_date, :assign_by_id, :wo_assign_id, :notes)
    """)
    return conn.execute(sql, {
        "device_id": device_id,
        "location_id": location_id,
        "assign_date": assign_date,
        "assign_by_id": assign_by_id,
        "wo_assign_id": wo_assign_id,
        "notes": notes,
    }).rowcount
