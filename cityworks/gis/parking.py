"""Business logic related to Parking operations.

All database access goes through :mod:`cityworks.gis.operations`.
This module owns business rules: input validation, field-name mapping
from work-order dicts to SDE column names, workflow orchestration, etc.
"""

from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import pandas as pd
import cityworks.client.exceptions as exc
from . import operations as ops


# ---------------------------------------------------------------------------
# Asset-type sets
# ---------------------------------------------------------------------------

SPACE_LAYERS = {"PU_METERS_ON_ST", "PU_LZ_ON_ST", "PU_DISVET_ON_ST", "PU_OFF_ST_SPACES"}
CC_TERMINAL_LAYERS = {"PU_REVENUE_CREDITCARDTERMINALS"}

# ---------------------------------------------------------------------------
# Workflow registry
# ---------------------------------------------------------------------------

# Each workflow is a list of (action_name, set_of_applicable_entity_types).
# An asset is only processed for actions whose entity-type set it belongs to.
WORKFLOWS = {
    "space_out_of_service": [
        ("space_out_of_service", SPACE_LAYERS),
    ],
    "return_to_service": [
        ("return_space_to_service", SPACE_LAYERS),
    ],
    "space_out_of_service_and_return": [
        ("space_out_of_service", SPACE_LAYERS),
        ("return_space_to_service", SPACE_LAYERS),
    ],
    "cc_terminal_add": [
        ("add_asset", CC_TERMINAL_LAYERS),
    ],
    "cc_terminal_remove": [
        ("remove_asset", CC_TERMINAL_LAYERS),
    ],
    "cc_terminal_move": [
        ("update_location", CC_TERMINAL_LAYERS),
    ],
}

# Map parent work-order template IDs → workflow name
TEMPLATE_WORKFLOWS = {
    # Hood/Sign Space → out of service
    214: "space_out_of_service",
    682: "space_out_of_service",
    1160: "space_out_of_service",
    1161: "space_out_of_service",
    1598: "space_out_of_service", # Hood Install
    1596: "space_out_of_service", # Sign Install

    # Hood/Sign Removal → return to service
    99: "return_space_to_service",
    100: "return_space_to_service",
    101: "return_space_to_service",
    1162: "return_space_to_service",
    1599: "return_space_to_service", # Hood Removal
    1597: "return_space_to_service", # Sign Removal

    # Credit Card Terminals
    1586: "cc_terminal_move",
    1587: "cc_terminal_add",
    1588: "cc_terminal_remove",

    # Temporary No Parking Sign
    85: "space_out_of_service_and_return",

}

# Map parent work-order description → workflow name
DESCRIPTION_WORKFLOWS = {
    "Hood/Sign Space": "space_out_of_service",
    "Hood Install": "space_out_of_service",
    "Hood/Sign Removal": "return_space_to_service",
    "Hood Removal": "return_space_to_service",
    "No Parking Sign Check": "space_out_of_service_and_return",
}

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
# Workflow dispatch
# ---------------------------------------------------------------------------

def get_workflow(
    template_id: Optional[int] = None,
    description: Optional[str] = None,
) -> List[Tuple[str, set]]:
    """Return the workflow steps for a parent work order.

    Look up by template ID (preferred) or description string.
    Returns an empty list if no workflow is registered.

    Args:
        template_id: Parent work order's ``WoTemplateId``.
        description: Parent work order's ``Description``.

    Returns:
        List of ``(action_name, applicable_entity_types)`` tuples.

    Example::

        steps = get_workflow(template_id=214)
        # [("out_of_service", {"PU_METERS_ON_ST", ...})]
    """
    name = None
    if template_id is not None:
        name = TEMPLATE_WORKFLOWS.get(int(template_id))
    if name is None and description is not None:
        name = DESCRIPTION_WORKFLOWS.get(description)
    return list(WORKFLOWS.get(name, [])) if name else []


def get_workflow_name(
    template_id: Optional[int] = None,
    description: Optional[str] = None,
) -> Optional[str]:
    """Return the workflow name (e.g. ``'out_of_service'``) or None."""
    if template_id is not None:
        name = TEMPLATE_WORKFLOWS.get(int(template_id))
        if name:
            return name
    if description is not None:
        return DESCRIPTION_WORKFLOWS.get(description)
    return None


def get_asset_actions(
    entity_type: str,
    template_id: Optional[int] = None,
    description: Optional[str] = None,
) -> List[str]:
    """Return which actions apply to a specific asset within a workflow.

    Args:
        entity_type: The asset's ``EntityType`` (e.g. ``'PU_METERS_ON_ST'``).
        template_id: Parent work order's ``WoTemplateId``.
        description: Parent work order's ``Description``.

    Returns:
        List of action names (e.g. ``['out_of_service']``).
        Empty list if the entity type doesn't participate in the workflow.
    """
    return [
        action
        for action, asset_types in get_workflow(template_id, description)
        if entity_type in asset_types
    ]


# ---------------------------------------------------------------------------
# Asset validation
# ---------------------------------------------------------------------------

def validate_space_assets(
    assets: List[Dict[str, Any]],
    parent_actual_finish_date: Optional[str],
    workflow_name: Optional[str],
) -> List[Dict[str, Any]]:
    """Validate work-order assets against PU_SPACESOUTOFSERVICE records.

    Enriches each asset dict with validation status, OOS record references,
    and a flag indicating whether the parent finish date is more recent than
    the latest OOS Date_Out for that space.

    Args:
        assets: Asset dicts from the work-order response.  Each must have at
            least ``EntityType``, ``EntityUid``, ``EntitySid``.  Space-layer
            assets should also have ``SpaceName``, ``Space_Type``, ``Address``,
            ``Status``, ``X``, ``Y``.  Every asset must already carry an
            ``actions`` key (list of action name strings).
        parent_actual_finish_date: ISO-format ``ActualFinishDate`` of the
            parent work order, or *None*.
        workflow_name: Resolved workflow name, or *None*.

    Returns:
        The same list (mutated in place and sorted by ``SpaceName``), with
        each asset gaining:

        * ``validation_status`` – ``'ready'``, ``'already_processed'``,
          ``'manual_review'``, or ``'error'``
        * ``validation_message`` – human-readable note (or *None*)
        * ``object_id`` – OOS ``OBJECTID`` for return-to-service (or *None*)
        * ``event_id`` – OOS ``Event_ID`` for return-to-service (or *None*)
        * ``recent_space_out_of_service`` – ``'true'`` / ``'false'``
    """
    # Parse the parent finish date once
    finish_dt = None
    if parent_actual_finish_date:
        try:
            finish_dt = datetime.fromisoformat(
                parent_actual_finish_date.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    # Identify space assets and collect their space names for bulk lookup
    space_names: List[str] = []
    for asset in assets:
        if asset["EntityType"] in SPACE_LAYERS and asset.get("SpaceName"):
            space_names.append(asset["SpaceName"])

    # Bulk-fetch OOS records
    existing_records: Dict[str, List[dict]] = {}
    max_date_out_by_space: Dict[str, datetime] = {}

    if space_names:
        try:
            oos_data = ops.get_spaces_out_of_service(space_names)
            if oos_data is not None and len(oos_data) > 0:
                max_date_out_by_space = (
                    oos_data.groupby("SpaceNumber")["Date_Out"].max().to_dict()
                )
                for _, row in oos_data.iterrows():
                    space_num = row.get("SpaceNumber")
                    if space_num not in existing_records:
                        existing_records[space_num] = []
                    existing_records[space_num].append(row.to_dict())
        except Exception as e:
            print(f"Error fetching out-of-service records: {e}")

    # Validate each asset
    for asset in assets:
        entity_type = asset.get("EntityType")
        space_name = asset.get("SpaceName")
        actions = asset.get("actions", [])

        # Defaults
        asset["validation_status"] = "ready"
        asset["validation_message"] = None
        asset["object_id"] = None
        asset["event_id"] = None
        asset["recent_space_out_of_service"] = "false"

        # Non-space assets or assets with no actions → manual review
        if entity_type not in SPACE_LAYERS or not actions:
            asset["validation_status"] = "manual_review"
            asset["validation_message"] = (
                f"Non-space asset type ({entity_type}) requires manual review"
                if entity_type not in SPACE_LAYERS
                else "No automated actions for this asset"
            )
            continue

        # Compute recent_space_out_of_service
        if finish_dt is not None and space_name:
            max_oos = max_date_out_by_space.get(space_name)
            if max_oos is not None:
                # Ensure timezone-aware comparison
                if max_oos.tzinfo is None:
                    max_oos = max_oos.replace(tzinfo=timezone.utc)
                cmp_finish = finish_dt if finish_dt.tzinfo else finish_dt.replace(
                    tzinfo=timezone.utc
                )
                asset["recent_space_out_of_service"] = (
                    "true" if cmp_finish > max_oos else "false"
                )
            else:
                # No OOS records at all → parent is most recent
                asset["recent_space_out_of_service"] = "true"

        # Per-action validation
        if "space_out_of_service" in actions:
            _validate_out_of_service(asset, space_name, existing_records, finish_dt)

        if "return_space_to_service" in actions:
            _validate_return_to_service(asset, space_name, existing_records, finish_dt)

    # Sort by SpaceName
    assets.sort(key=lambda a: a.get("SpaceName") or "")

    return assets


def _validate_out_of_service(
    asset: Dict[str, Any],
    space_name: Optional[str],
    existing_records: Dict[str, List[dict]],
    finish_dt: Optional[datetime],
) -> None:
    """Check if an OOS record already exists for the parent finish date."""
    if not space_name or finish_dt is None:
        return

    records = existing_records.get(space_name, [])
    finish_date_only = finish_dt.date()

    for record in records:
        date_out = record.get("Date_Out")
        if date_out is None:
            continue
        try:
            if isinstance(date_out, str):
                record_date = datetime.fromisoformat(
                    date_out.replace("Z", "+00:00")
                ).date()
            elif hasattr(date_out, "date"):
                record_date = date_out.date()
            else:
                continue

            if record_date == finish_date_only:
                asset["validation_status"] = "already_processed"
                asset["validation_message"] = (
                    f"Record already exists for {finish_date_only}"
                )
                return
        except Exception:
            continue


def _validate_return_to_service(
    asset: Dict[str, Any],
    space_name: Optional[str],
    existing_records: Dict[str, List[dict]],
    finish_dt: Optional[datetime],
) -> None:
    """Find an open OOS record (Date_Returned IS NULL) to close."""
    if not space_name:
        return

    records = existing_records.get(space_name, [])
    has_open_record = False

    for record in records:
        date_returned = record.get("Date_Returned")
        # Check for NULL/NaN Date_Returned
        if date_returned is not None and date_returned == date_returned:
            continue

        # Found an open record — verify its Date_Out is before the parent finish
        date_out = record.get("Date_Out")
        if finish_dt is not None and date_out is not None:
            if hasattr(date_out, "tzinfo") and date_out.tzinfo is None:
                date_out = date_out.replace(tzinfo=timezone.utc)
            cmp_finish = finish_dt if finish_dt.tzinfo else finish_dt.replace(
                tzinfo=timezone.utc
            )
            if date_out >= cmp_finish:
                continue

        has_open_record = True
        asset["object_id"] = record.get("OBJECTID")
        asset["event_id"] = record.get("Event_ID")
        asset["validation_message"] = (
            f"Found OOS record (OBJECTID={record.get('OBJECTID')}, "
            f"Event_ID={record.get('Event_ID')}) with no close date"
        )
        break

    if not has_open_record:
        asset["validation_status"] = "already_processed"
        asset["validation_message"] = (
            "No open out-of-service record found for this space"
        )


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
