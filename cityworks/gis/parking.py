"""Business logic related to Parking operations.

All database access goes through :mod:`cityworks.gis.operations`.
This module owns business rules: input validation, field-name mapping
from work-order dicts to SDE column names, workflow orchestration, etc.
"""

from typing import Optional, Dict, Any, List
import pandas as pd
import cityworks.client.exceptions as exc
from . import operations as ops


# ---------------------------------------------------------------------------
# Location mapping for CC terminal moves
# ---------------------------------------------------------------------------

LOCATION_MAP = {
    "OC": 1,
    "SSCO": 2,
    "SSCA": 5,
    "CSN": 6,
    "SLS": 18,
    "WS": 19,
}


# ---------------------------------------------------------------------------
# Spaces Out of Service
# ---------------------------------------------------------------------------

def add_space_out_of_service(space_data: List[Dict]) -> int:
    """Mark parking spaces as out of service.

    Inserts a record per space into ``PU_SPACESOUTOFSERVICE``.

    Args:
        space_data: List of dicts with keys: ``space_number``, ``date_out``,
            ``meter_type``, ``created_user``, ``created_date``,
            ``last_edited_user``, ``last_edited_date``, ``note``, ``x``, ``y``,
            ``revenue_collected``, ``removal_method``, ``removed_by``,
            ``reason_removed``, ``ada_relocation``.

    Returns:
        Total number of rows inserted.
    """
    row_count = 0
    with ops.sde_transaction() as conn:
        for space in space_data:
            result = ops.sde_insert(
                table_name="PU_SPACESOUTOFSERVICE",
                data={
                    "Date_Out": space["date_out"],
                    "Space_Type": space["meter_type"],
                    "created_user": space["created_user"],
                    "created_date": space["created_date"],
                    "last_edited_user": space["last_edited_user"],
                    "last_edited_date": space["last_edited_date"],
                    "Note": space["note"],
                    "SpaceNumber": space["space_number"],
                    "RevenueCollected": space["revenue_collected"],
                    "RemovalMethod": space["removal_method"],
                    "RemovedBy": space["removed_by"],
                    "ReasonRemoved": space["reason_removed"],
                    "ADA_Relocation": space["ada_relocation"],
                },
                auto_increment_fields=["OBJECTID", "Event_ID"],
                geometry_point={"x": space["x"], "y": space["y"]},
                geometry_column="Shape",
                conn=conn,
            )
            row_count += result
    return row_count


def return_space_to_service(update_values: Dict) -> int:
    """Mark a parking space as returned to service.

    Updates the open ``PU_SPACESOUTOFSERVICE`` record identified by
    ``event_id`` and ``object_id``.

    Args:
        update_values: Dict with keys: ``date_returned``,
            ``last_edited_date``, ``last_edited_user``, ``event_id``,
            ``object_id``.

    Returns:
        Number of rows updated (should be 1 if successful).
    """
    return ops.sde_update(
        table_name="PU_SPACESOUTOFSERVICE",
        updates={
            "Date_Returned": update_values["date_returned"],
            "last_edited_date": update_values["last_edited_date"],
            "last_edited_user": update_values["last_edited_user"],
        },
        where_clause=(
            "Date_Returned IS NULL "
            "AND Event_ID = :event_id "
            "AND OBJECTID = :object_id"
        ),
        params={
            "event_id": update_values["event_id"],
            "object_id": update_values["object_id"],
        },
    )


def update_space_status(
    table: str,
    space_id: str,
    status: str,
    last_edited_date: Optional[str] = None,
    last_edited_user: Optional[str] = None,
) -> int:
    """Update the status of a parking space in any space layer table.

    Args:
        table: Table name (e.g. ``'PU_METERS_ON_ST'``).
        space_id: Cityworks Asset ID of the space.
        status: New status value (e.g. ``'In service'``, ``'Out of service'``).
        last_edited_date: Audit timestamp.
        last_edited_user: Audit user.

    Returns:
        Number of rows updated.
    """
    cw_field = ops.get_cw_asset_field(table)
    status_field = ops.get_status_field(table)

    return ops.sde_update(
        table_name=table,
        updates={
            status_field: status,
            "last_edited_date": last_edited_date,
            "last_edited_user": last_edited_user,
        },
        where_clause=f"{cw_field} = :cw_asset_id",
        params={"cw_asset_id": space_id},
    )


# ---------------------------------------------------------------------------
# Work-order → SDE space lookup
# ---------------------------------------------------------------------------

def get_spaces_from_work_order(work_order: Dict[str, Any]) -> pd.DataFrame:
    """Find space records in the SDE from a work order's entities.

    Args:
        work_order: Dict containing an ``'assets'`` key with a list of
            ``{'EntityType': str, 'EntityUid': str}`` dicts.

    Returns:
        DataFrame with unified columns across all space types.
    """
    if "assets" not in work_order:
        return pd.DataFrame()
    if not isinstance(work_order["assets"], list):
        raise ValueError("Expected 'assets' to be a list of dictionaries")
    if not all(k in work_order["assets"][0] for k in ("EntityType", "EntityUid")):
        raise ValueError("Expected each asset to have 'EntityType' and 'EntityUid' keys")

    # Group entity UIDs by entity type
    entity_type_dict: Dict[str, List[str]] = {}
    for asset in work_order["assets"]:
        entity_type_dict.setdefault(asset["EntityType"], []).append(asset["EntityUid"])

    frames: List[pd.DataFrame] = []

    for entity_type, entity_uids in entity_type_dict.items():
        cw_field = ops.get_cw_asset_field(entity_type)
        uid_list = "','".join(entity_uids)
        where = f"{cw_field} IN ('{uid_list}')"

        try:
            temp = ops.sde_query(entity_type, where, as_geodataframe=True)
        except Exception:
            temp = ops.sde_query(entity_type, where, as_geodataframe=False)

        # Annotate source table
        temp["table_name"] = ops._fqn(entity_type)

        # Type-specific enrichment
        if entity_type == "PU_DISVET_ON_ST":
            temp["Meter_Type"] = "ADA"

        # Normalize column names across tables
        temp.rename(
            columns={
                "CWasset_ID": "cwAssetID",
                "Space_Status": "Status",
                "LZ_Type": "Space_Type",
                "Meter_Type": "Space_Type",
            },
            inplace=True,
        )

        # Build address
        addr_cols = ("Block_Nbr", "St_Pre_Dir", "St_Name", "St_Name_Suf")
        if all(c in temp.columns for c in addr_cols):
            temp.fillna(
                {"St_Pre_Dir": "", "St_Name_Suf": "", "St_Name": "", "Block_Nbr": 0},
                inplace=True,
            )
            temp["Address"] = (
                temp["Block_Nbr"].astype(str) + " "
                + temp["St_Pre_Dir"] + " "
                + temp["St_Name"] + " "
                + temp["St_Name_Suf"]
            ).str.replace("  ", " ")
        elif "Facility_Name_Full" in temp.columns:
            temp["Address"] = temp["Facility_Name_Full"]
        else:
            temp["Address"] = "No address found"

        frames.append(temp)

    return pd.concat(frames) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# CC Terminal workflows
# ---------------------------------------------------------------------------

def cc_terminal_add_asset(work_order: Dict[str, Any]) -> int:
    """Add a new portable CC terminal asset to the SDE.

    Business rules:
    - DeviceID = ``'rvtrm-{OBJECTID}'``
    - cwAssetID = ``'cw-rvtrm-{OBJECTID}'``
    - UID = last 6 digits of Terminal ID
    - Terminal ID must start with ``'0010050008016090'``

    Args:
        work_order: Dict with ``custom_fields`` containing at minimum
            ``'Terminal ID'``, plus optionally ``'Model'``, ``'Serial Number'``,
            ``'Brand'``.

    Returns:
        Number of rows inserted.
    """
    if "Terminal ID" not in work_order["custom_fields"]:
        raise exc.MissingRequiredValueError("Terminal ID is required")

    raw_terminal_id = work_order["custom_fields"]["Terminal ID"]
    uid = raw_terminal_id[-6:]
    prefix = "0010050008016090"
    terminal_id = raw_terminal_id if raw_terminal_id.startswith(prefix) else f"{prefix}{uid}"

    with ops.sde_transaction() as conn:
        object_id = ops.get_next_id("PU_REVENUE_CREDITCARDTERMINALS", "OBJECTID", conn=conn)
        device_id = f"rvtrm-{object_id}"
        cw_asset_id = f"cw-rvtrm-{object_id}"

        return ops.sde_insert(
            table_name="PU_REVENUE_CREDITCARDTERMINALS",
            data={
                "OBJECTID": object_id,
                "DeviceID": device_id,
                "Model": work_order["custom_fields"].get("Model"),
                "SerialNumber": work_order["custom_fields"].get("Serial Number"),
                "cwAssetID": cw_asset_id,
                "Brand": work_order["custom_fields"].get("Brand"),
                "TerminalID": terminal_id,
                "UID": uid,
            },
            conn=conn,
        )


def cc_terminal_remove_asset(work_order: Dict[str, Any]) -> int:
    """Remove a portable CC terminal from service.

    Sets ``DateRemoved`` on the terminal record.

    Args:
        work_order: Dict with ``ActualFinishDate`` and ``assets[0]['EntityUid']``.

    Returns:
        Number of rows updated.
    """
    if not work_order.get("ActualFinishDate"):
        raise exc.MissingRequiredValueError("ActualFinishDate is required")

    return ops.sde_update(
        table_name="PU_REVENUE_CREDITCARDTERMINALS",
        updates={"DateRemoved": work_order["ActualFinishDate"]},
        where_clause="UID = :entity_uid",
        params={"entity_uid": work_order["assets"][0]["EntityUid"]},
    )


def cc_terminal_update_gis(work_order: Dict[str, Any]) -> Dict:
    """Handle a CC terminal location change.

    Executes as a single transaction:
    1. Query the active assignment
    2. Close it (set DateRemoved, IsActive=0)
    3. Insert a new active assignment at the destination location

    Args:
        work_order: Dict with ``assets[0]['EntityUid']``,
            ``custom_fields['Destination Location']``, ``ActualFinishDate``,
            ``InitiatedBy``, ``WorkOrderSid``, ``instructions``.

    Returns:
        ``{'updated': int, 'created': int}`` with row counts.
    """
    if not work_order["assets"][0].get("EntityUid"):
        raise exc.MissingRequiredValueError("EntityUid is required")
    if not work_order["custom_fields"].get("Destination Location"):
        raise exc.MissingRequiredValueError("Destination Location is required")

    dest = work_order["custom_fields"]["Destination Location"]
    location_id = LOCATION_MAP.get(dest, -1)

    with ops.sde_transaction() as conn:
        # 1. Query active assignment
        active = ops.get_active_terminal_assignment(
            uid=work_order["assets"][0]["EntityUid"],
            conn=conn,
        )
        if active.empty:
            raise exc.MissingRequiredValueError("No active terminal assignment found")

        row = active.iloc[0]

        # 2. Close active assignment
        update_count = ops.sde_update(
            table_name="PU_CC_TERMINAL_HISTORY",
            updates={
                "DateRemoved": work_order["ActualFinishDate"],
                "RemovedBy": work_order["InitiatedBy"],
                "WorkOrderRemoveId": work_order["WorkOrderSid"],
                "Notes": work_order["instructions"],
                "IsActive": 0,
            },
            where_clause="OBJECTID = :object_id AND TerminalID = :terminal_id",
            params={
                "object_id": row["OBJECTID"],
                "terminal_id": row["TerminalID"],
            },
            conn=conn,
        )

        # 3. Insert new active assignment
        insert_count = ops.sde_insert(
            table_name="PU_CC_TERMINAL_HISTORY",
            data={
                "DeviceID": row["base_device_id"],
                "TerminalID": row["TerminalID"],
                "LocationID": location_id,
                "DateAssigned": work_order["ActualFinishDate"],
                "AssignedBy": work_order["InitiatedBy"],
                "WorkOrderAssignID": work_order["WorkOrderSid"],
                "Notes": work_order["instructions"],
                "IsActive": 1,
            },
            auto_increment_fields=["OBJECTID"],
            conn=conn,
        )

    return {"updated": update_count, "created": insert_count}
