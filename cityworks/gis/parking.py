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
from . import revenue_ops


# ---------------------------------------------------------------------------
# Asset-type sets
# ---------------------------------------------------------------------------

SPACE_LAYERS = {"PU_METERS_ON_ST", "PU_LZ_ON_ST", "PU_DISVET_ON_ST", "PU_OFF_ST_SPACES"}
CC_TERMINAL_LAYERS = {"PU_REVENUE_CREDITCARDTERMINALS"}

# ---------------------------------------------------------------------------
# Sign date custom-field names
# ---------------------------------------------------------------------------
# For "No Parking Sign Check" workflows, the sign posting dates come from
# either the parent work order's custom fields or the service request's
# custom fields.  We check parent_custom_fields first, then fall back to
# service_request.
SIGN_DATE_FIELDS = {
    "parent_custom_fields": {
        "start": "ProjStartDate", # used to be from custom field "Posting Begin Date",
        "end": "ProjFinishDate", # used to be from custom field "Posting End Date",
    },
    "service_request": {
        "start": "Start Date",
        "end": "End Date",
    },
}

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

# Map parent work-order template IDs → workflow name.
# Values MUST match keys in WORKFLOWS above.
TEMPLATE_WORKFLOWS = {
    # Hood/Sign Space → out of service
    214: "space_out_of_service",
    682: "space_out_of_service",
    1160: "space_out_of_service",
    1161: "space_out_of_service",
    1598: "space_out_of_service",  # Hood Install
    1596: "space_out_of_service",  # Sign Install

    # Hood/Sign Removal → return to service
    99: "return_to_service",
    100: "return_to_service",
    101: "return_to_service",
    1162: "return_to_service",
    1599: "return_to_service",  # Hood Removal
    1597: "return_to_service",  # Sign Removal

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
    "Sign Install": "space_out_of_service",
    "Hood/Sign Removal": "return_to_service",
    "Hood Removal": "return_to_service",
    "Sign Removal": "return_to_service",
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

cityworks_to_revenue_id_mapping = {
    985: 5, 
    1629: 2
} # Add others as needed

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
        # [("space_out_of_service", {"PU_METERS_ON_ST", ...})]
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
    """Return the workflow name (e.g. ``'space_out_of_service'``) or None."""
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
        List of action names (e.g. ``['space_out_of_service']``).
        Empty list if the entity type doesn't participate in the workflow.
    """
    return [
        action
        for action, asset_types in get_workflow(template_id, description)
        if entity_type in asset_types
    ]


# ---------------------------------------------------------------------------
# Work-order enrichment & validation pipeline
# ---------------------------------------------------------------------------

def prepare_work_order(response: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a raw work-order response with workflow, SDE data, and validation.

    This is the single entry-point for turning the output of
    :func:`~cityworks.queries.work_orders.get_work_order_details_json` into a
    fully-validated payload ready for the front end.

    Pipeline steps:

    1. Resolve the workflow name from the parent work order.
    2. Enrich space-layer assets with SDE data (Status, SpaceName, etc.).
    3. Compute per-asset actions from the workflow registry.
    4. Extract sign dates (for sign workflows).
    5. Validate assets against PU_SPACESOUTOFSERVICE records.

    Args:
        response: Dict returned by ``get_work_order_details_json()``.
            Must contain ``'work_order'``, ``'parent_work_order'``, and
            ``'assets'`` keys.

    Returns:
        The same *response* dict, mutated in place, with:

        * ``response['workflow_name']`` -- resolved workflow name or *None*
        * ``response['sign_start_date']`` / ``response['sign_end_date']``
          -- sign posting dates (sign workflows only, else *None*)
        * Each asset gains ``actions``, ``validation_status``,
          ``validation_message``, ``object_id``, ``event_id``,
          ``recent_space_out_of_service``, plus SDE fields when applicable.
    """
    parent_wo = response.get("parent_work_order", {}) or {}
    work_wo = response.get("work_order", {}) or {}

    # Use the parent template/description when present; fall back to the
    # work order itself for standalone templates (e.g. CC terminal 1586/1587/1588)
    # that have no parent work order.
    template_id = parent_wo.get("WoTemplateId") or work_wo.get("WoTemplateId")
    description = parent_wo.get("Description") or work_wo.get("Description")
    parent_actual_finish_date = parent_wo.get("ActualFinishDate")

    tid = int(template_id) if template_id else None

    # 1. Resolve workflow
    workflow_name = get_workflow_name(template_id=tid, description=description)
    response["workflow_name"] = workflow_name

    # 2. Enrich space assets with SDE data
    has_spaces = any(
        a["EntityType"] in SPACE_LAYERS for a in response["assets"]
    )
    if has_spaces:
        spaces = get_spaces_from_work_order(response)
        spaces.fillna(
            {"Status": "", "SpaceName": "", "Space_Type": ""}, inplace=True
        )
        if spaces.shape[0] > 0:
            for asset in response["assets"]:
                match = spaces[spaces["cwAssetID"] == asset["EntityUid"]]
                if match.shape[0] > 0:
                    asset.update(
                        match[["Status", "SpaceName", "Space_Type", "Address"]]
                        .to_dict(orient="records")[0]
                    )

    # 3. Compute per-asset actions
    for asset in response["assets"]:
        asset["actions"] = get_asset_actions(
            entity_type=asset["EntityType"],
            template_id=tid,
            description=description,
        )

    # 4. Extract sign dates for sign workflows
    sign_start_date = None
    sign_end_date = None
    if workflow_name == "space_out_of_service_and_return":
        sign_start_date, sign_end_date = _extract_sign_dates(response)

    response["sign_start_date"] = sign_start_date
    response["sign_end_date"] = sign_end_date

    # 5. Validate against OOS records
    response["assets"] = validate_space_assets(
        assets=response["assets"],
        parent_actual_finish_date=parent_actual_finish_date,
        workflow_name=workflow_name,
        sign_start_date=sign_start_date,
        sign_end_date=sign_end_date,
    )

    return response


def _extract_sign_dates(
    response: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Extract sign start/end dates from parent custom fields or service request.

    Tries ``parent_custom_fields`` first (Posting Begin Date / Posting End Date),
    then falls back to ``service_request`` (Start Date / End Date).

    Returns:
        Tuple of ``(start_date_str, end_date_str)``.  Either or both may be *None*.
    """
    # Primary: parent custom fields (from the No Parking Sign Check work order)
    pcf = response.get("parent_custom_fields", {}) or {}
    fields = SIGN_DATE_FIELDS["parent_custom_fields"]
    start = pcf.get(fields["start"])
    end = pcf.get(fields["end"])
    if start:
        return start, end

    # Fallback: service request custom fields
    sr = response.get("service_request", {}) or {}
    fields = SIGN_DATE_FIELDS["service_request"]
    start = sr.get(fields["start"])
    end = sr.get(fields["end"])
    return start, end


# ---------------------------------------------------------------------------
# Asset validation (internal to prepare_work_order pipeline)
# ---------------------------------------------------------------------------

def validate_space_assets(
    assets: List[Dict[str, Any]],
    parent_actual_finish_date: Optional[str],
    workflow_name: Optional[str],
    sign_start_date: Optional[str] = None,
    sign_end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Validate work-order assets against PU_SPACESOUTOFSERVICE records.

    For ``space_out_of_service`` workflows, checks whether an OOS record
    already exists for the parent finish date.

    For ``return_to_service`` workflows, finds an open OOS record
    (Date_Returned IS NULL) to close.

    For ``space_out_of_service_and_return`` (temporary sign) workflows,
    checks whether a completed OOS record already exists matching the sign
    start/end dates.

    Args:
        assets: Asset dicts (must already have ``actions`` key).
        parent_actual_finish_date: ISO ``ActualFinishDate`` of the parent, or *None*.
        workflow_name: Resolved workflow name, or *None*.
        sign_start_date: Sign start datetime string, or *None*.
        sign_end_date: Sign end datetime string, or *None*.

    Returns:
        The same list (mutated, sorted by SpaceName), with each asset gaining
        ``validation_status``, ``validation_message``, ``object_id``,
        ``event_id``, ``recent_space_out_of_service``, and optionally
        ``sign_start_date`` / ``sign_end_date``.
    """
    finish_dt = _parse_datetime(parent_actual_finish_date)
    sign_start_dt = _parse_datetime(sign_start_date)
    sign_end_dt = _parse_datetime(sign_end_date)

    # Collect space names for bulk OOS lookup
    space_names: List[str] = [
        a["SpaceName"]
        for a in assets
        if a["EntityType"] in SPACE_LAYERS and a.get("SpaceName")
    ]

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
                    existing_records.setdefault(space_num, []).append(row.to_dict())
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

        # Determine reference date for recent_space_out_of_service.
        # Sign workflows use sign_start_date; hoods use parent finish.
        ref_dt = sign_start_dt if sign_start_dt is not None else finish_dt

        # Compute recent_space_out_of_service
        if ref_dt is not None and space_name:
            max_oos = max_date_out_by_space.get(space_name)
            if max_oos is not None:
                asset["recent_space_out_of_service"] = (
                    "true" if _ensure_tz(ref_dt) > _ensure_tz(max_oos) else "false"
                )
            else:
                asset["recent_space_out_of_service"] = "true"

        # Dispatch validation by workflow
        records = existing_records.get(space_name, [])
        _validate_asset(asset, records, workflow_name, finish_dt, sign_start_dt, sign_end_dt)

    # Sort by SpaceName
    assets.sort(key=lambda a: a.get("SpaceName") or "")
    return assets


def _validate_asset(
    asset: Dict[str, Any],
    records: List[dict],
    workflow_name: Optional[str],
    finish_dt: Optional[datetime],
    sign_start_dt: Optional[datetime],
    sign_end_dt: Optional[datetime],
) -> None:
    """Dispatch validation for a single asset based on workflow type."""
    if workflow_name == "space_out_of_service":
        _check_oos_record_exists(asset, records, finish_dt)

    elif workflow_name == "return_to_service":
        _find_open_record_to_close(asset, records, finish_dt)

    elif workflow_name == "space_out_of_service_and_return":
        _check_sign_record_exists(asset, records, sign_start_dt, sign_end_dt)


def _check_oos_record_exists(
    asset: Dict[str, Any],
    records: List[dict],
    finish_dt: Optional[datetime],
) -> None:
    """Mark as already_processed if an OOS record exists on the finish date."""
    if finish_dt is None:
        return

    target_date = finish_dt.date()
    for record in records:
        if _to_date(record.get("Date_Out")) == target_date:
            asset["validation_status"] = "already_processed"
            asset["validation_message"] = f"OOS record already exists for {target_date}"
            return


def _find_open_record_to_close(
    asset: Dict[str, Any],
    records: List[dict],
    finish_dt: Optional[datetime],
) -> None:
    """Find an open OOS record (Date_Returned IS NULL) to close.

    Sets ``object_id`` and ``event_id`` on the asset if found.
    Marks as ``already_processed`` if no open record exists.
    """
    for record in records:
        date_returned = record.get("Date_Returned")
        # Skip closed records (non-null, non-NaN Date_Returned)
        if date_returned is not None and date_returned == date_returned:
            continue

        # Verify Date_Out is before finish date
        date_out = record.get("Date_Out")
        if finish_dt is not None and date_out is not None:
            if _ensure_tz(date_out) >= _ensure_tz(finish_dt):
                continue

        asset["object_id"] = record.get("OBJECTID")
        asset["event_id"] = record.get("Event_ID")
        asset["validation_message"] = (
            f"Open OOS record found (OBJECTID={record.get('OBJECTID')}, "
            f"Event_ID={record.get('Event_ID')})"
        )
        return

    # No open record found
    asset["validation_status"] = "already_processed"
    asset["validation_message"] = "No open OOS record found for this space"


def _check_sign_record_exists(
    asset: Dict[str, Any],
    records: List[dict],
    sign_start_dt: Optional[datetime],
    sign_end_dt: Optional[datetime],
) -> None:
    """Check whether a sign OOS record already exists with matching dates.

    If found: ``already_processed``.
    If not found: ``ready``, and stores sign dates on the asset for processing.
    """
    if sign_start_dt is None:
        asset["validation_status"] = "error"
        asset["validation_message"] = "Missing sign posting dates"
        return

    start_date = sign_start_dt.date()
    end_date = sign_end_dt.date() if sign_end_dt else None

    for record in records:
        if _to_date(record.get("Date_Out")) != start_date:
            continue

        if end_date is not None:
            date_returned = record.get("Date_Returned")
            # Skip if Date_Returned is null/NaN
            if date_returned is None or date_returned != date_returned:
                continue
            if _to_date(date_returned) != end_date:
                continue

        # Match found
        asset["validation_status"] = "already_processed"
        asset["validation_message"] = (
            f"OOS record already exists (Date_Out={start_date}"
            + (f", Date_Returned={end_date}" if end_date else "")
            + ")"
        )
        return

    # No match — store sign dates on asset for processing endpoint
    asset["sign_start_date"] = sign_start_dt.isoformat()
    if sign_end_dt:
        asset["sign_end_date"] = sign_end_dt.isoformat()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_datetime(value) -> Optional[datetime]:
    """Parse an ISO-format or M/D/YYYY h:MM:SS AM/PM date/datetime string, returning *None* on failure."""
    if not value:
        return None
    try:
        if isinstance(value, str):
            # Try ISO format first
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                # Fall back to M/D/YYYY h:MM:SS AM/PM format
                return datetime.strptime(value, '%m/%d/%Y %I:%M:%S %p')
        if hasattr(value, "tzinfo"):
            return value  # already a datetime
    except (ValueError, AttributeError):
        pass
    return None


def _to_date(value):
    """Coerce a date-like value (str, datetime, Timestamp) to a ``date``, or *None*."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        if hasattr(value, "date"):
            return value.date()
    except Exception:
        pass
    return None


def _ensure_tz(dt):
    """Ensure a datetime-like value is timezone-aware (UTC fallback)."""
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Spaces Out of Service
# ---------------------------------------------------------------------------

def add_space_out_of_service(space_data: List[Dict]) -> int:
    """Mark parking spaces as out of service.

    Inserts a record per space into ``PU_SPACESOUTOFSERVICE``.
    Supports an optional ``date_returned`` for sign workflows that create
    complete OOS records in a single insert.

    Args:
        space_data: List of dicts with keys: ``space_number``, ``date_out``,
            ``meter_type``, ``created_user``, ``created_date``,
            ``last_edited_user``, ``last_edited_date``, ``note``, ``x``, ``y``,
            ``revenue_collected``, ``removal_method``, ``removed_by``,
            ``reason_removed``, ``ada_relocation``.
            Optional: ``date_returned`` (for sign workflows).

    Returns:
        Total number of rows inserted.
    """
    row_count = 0
    with ops.sde_transaction() as conn:
        for space in space_data:
            data = {
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
            }
            if space.get("date_returned"):
                data["Date_Returned"] = space["date_returned"]

            result = ops.sde_insert(
                table_name="PU_SPACESOUTOFSERVICE",
                data=data,
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


def cc_terminal_update_revenue(work_order: Dict[str, Any]) -> Dict:
    """Handle a CC terminal location change in the revenue database.

    Executes as a single transaction:
    1. Look up device_id from app.dim_device
    2. Look up location_id from app.dim_facility / app.dim_location
    3. Query the active assignment from app.fact_device_assignment
    4. Close it (set end_date, end_by_id, workorder_remove_id, notes)
    5. Insert a new active assignment at the destination location

    Args:
        work_order: Dict with ``assets[0]['EntityUid']``,
            ``custom_fields['Destination Location']``,
            ``custom_fields['Date of Change']``,
            ``employee_id`` (int), ``WorkOrderSid``.

    Returns:
        ``{'updated': int, 'created': int}`` with row counts.
    """
    entity_uid = work_order["assets"][0].get("EntityUid")
    if not entity_uid:
        raise exc.MissingRequiredValueError("EntityUid is required")
    if not work_order["custom_fields"].get("Destination Location"):
        raise exc.MissingRequiredValueError("Destination Location is required")
    if not work_order["custom_fields"].get("Date of Change"):
        raise exc.MissingRequiredValueError("Date of Change is required")
    if not work_order['work_order'].get("SubmitToSid"):
        raise exc.MissingRequiredValueError("employee_id is required")

    dest = work_order["custom_fields"]["Destination Location"]
    date_of_change = work_order["custom_fields"]["Date of Change"]
    date_of_change = datetime.strptime(date_of_change, '%m/%d/%Y %I:%M:%S %p').strftime('%Y-%m-%d %H:%M:%S')
    employee_id = work_order['work_order']['SubmitToSid']
    # Translate Cityworks ID to Revenue ID
    employee_id = cityworks_to_revenue_id_mapping.get(employee_id, employee_id) 
    wo_sid = work_order['work_order'].get("WorkOrderSid")

    with revenue_ops.revenue_transaction() as conn:
        # 1. Look up device_id
        device_id = revenue_ops.get_device_id(entity_uid, conn=conn)
        if device_id is None:
            raise exc.MissingRequiredValueError(f"No revenue device found for UID: {entity_uid}")

        # 2. Look up location_id
        location_id = revenue_ops.get_location_id(dest, conn=conn)
        if location_id is None:
            raise exc.MissingRequiredValueError(f"No revenue location found for: {dest}")

        # 3. Query active assignment
        active = revenue_ops.get_active_device_assignment(device_id, conn=conn)
        if active.empty:
            raise exc.MissingRequiredValueError("No active revenue assignment found")

        assignment_id = int(active.iloc[0]["assignment_id"])

        # 4. Close active assignment
        update_count = revenue_ops.close_device_assignment(
            assignment_id=assignment_id,
            end_date=date_of_change,
            end_by_id=employee_id,
            wo_remove_id=wo_sid,
            notes=dest,
            conn=conn,
        )

        # 5. Insert new active assignment
        insert_count = revenue_ops.insert_device_assignment(
            device_id=device_id,
            location_id=location_id,
            assign_date=date_of_change,
            assign_by_id=employee_id,
            wo_assign_id=wo_sid,
            notes=dest,
            conn=conn,
        )

    return {"updated": update_count, "created": insert_count}


def cc_terminal_move(work_order: Dict[str, Any]) -> Dict:
    """Orchestrate a CC terminal location move across GIS and revenue databases.

    Runs the GIS and revenue updates as independent transactions and reports
    the outcome of each.  A failure in one does not prevent the other from
    running, but both results are returned so the caller can surface any
    partial failures.

    Args:
        work_order: Dict with all fields required by
            :func:`cc_terminal_update_gis` and
            :func:`cc_terminal_update_revenue`.

    Returns:
        Dict with ``'gis'`` and ``'revenue'`` keys (each a row-count dict),
        plus an ``'errors'`` key containing any exception messages keyed by
        database name.

        Example (both succeeded)::

            {"gis": {"updated": 1, "created": 1}, "revenue": {"updated": 1, "created": 1}}

        Example (revenue failed)::

            {
                "gis": {"updated": 1, "created": 1},
                "errors": {"revenue": "No active revenue assignment found"},
            }
    """
    result: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    try:
        result["gis"] = cc_terminal_update_gis(work_order)
    except Exception as e:
        errors["gis"] = str(e)

    try:
        result["revenue"] = cc_terminal_update_revenue(work_order)
    except Exception as e:
        errors["revenue"] = str(e)

    if errors:
        result["errors"] = errors

    return result


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
    """Handle a CC terminal location change in the GIS database.

    Executes as a single transaction:
    1. Query the active assignment from PU_CC_TERMINAL_HISTORY
    2. Close it (set DateRemoved, RemovedBy, WorkOrderRemoveId, IsActive=0)
    3. Insert a new active assignment at the destination location

    Args:
        work_order: Dict with ``assets[0]['EntityUid']``,
            ``custom_fields['Destination Location']``,
            ``custom_fields['Date of Change']``,
            ``employee_id`` (int), ``WorkOrderSid``.

    Returns:
        ``{'updated': int, 'created': int}`` with row counts.
    """
    if not work_order["assets"][0].get("EntityUid"):
        raise exc.MissingRequiredValueError("EntityUid is required")
    if not work_order["custom_fields"].get("Destination Location"):
        raise exc.MissingRequiredValueError("Destination Location is required")
    if not work_order["custom_fields"].get("Date of Change"):
        raise exc.MissingRequiredValueError("Date of Change is required")
    if not work_order['work_order'].get("SubmitToSid"):
        raise exc.MissingRequiredValueError("employee_id is required")

    dest = work_order["custom_fields"]["Destination Location"]
    date_of_change = work_order["custom_fields"]["Date of Change"]
    date_of_change = datetime.strptime(date_of_change, '%m/%d/%Y %I:%M:%S %p').strftime('%Y-%m-%d %H:%M:%S')
    employee_id = work_order['work_order']["SubmitToSid"]
    employee_id = cityworks_to_revenue_id_mapping.get(employee_id, employee_id)
    location_id = LOCATION_MAP.get(dest, -1)

    with ops.sde_transaction() as conn:
        # 1. Query active assignment
        active = ops.get_active_terminal_assignment(
            uid=work_order["assets"][0]["EntityUid"],
            conn=conn,
        )
        if active.empty:
            raise exc.MissingRequiredValueError("No active GIS terminal assignment found")

        row = active.iloc[0]

        # 2. Close active assignment
        update_count = ops.sde_update(
            table_name="PU_CC_TERMINAL_HISTORY",
            updates={
                "DateRemoved": date_of_change,
                "RemovedBy": employee_id,
                "WorkOrderRemoveId": work_order['work_order']["WorkOrderSid"],
                "Notes": dest,
                "IsActive": 0,
            },
            where_clause="OBJECTID = :object_id AND TerminalID = :terminal_id",
            params={
                "object_id": int(row["OBJECTID"]),
                "terminal_id": row["TerminalID"],
            },
            conn=conn,
        )

        # 3. Insert new active assignment
        insert_count = ops.sde_insert(
            table_name="PU_CC_TERMINAL_HISTORY",
            data={
                "DeviceID": row["DeviceID"],
                "TerminalID": row["TerminalID"],
                "LocationID": location_id,
                "DateAssigned": date_of_change,
                "AssignedBy": employee_id,
                "WorkOrderAssignID": work_order['work_order']["WorkOrderSid"],
                "Notes": dest,
                "IsActive": 1,
            },
            auto_increment_fields=["OBJECTID"],
            conn=conn,
        )

    return {"updated": update_count, "created": insert_count}
