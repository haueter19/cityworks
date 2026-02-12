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
# Service-request custom-field names for sign date extraction
# ---------------------------------------------------------------------------
# These are the CustFieldName values from CMMS.azteca.ReqCustField that hold
# the sign start and end datetimes for "No Parking Sign Check" workflows.
# Adjust if the actual field names in Cityworks differ.
SIGN_DATE_FIELDS = {
    "start": "Sign Start Date",
    "end": "Sign End Date",
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
    4. Validate assets against PU_SPACESOUTOFSERVICE records.

    Args:
        response: Dict returned by ``get_work_order_details_json()``.
            Must contain ``'work_order'``, ``'parent_work_order'``, and
            ``'assets'`` keys.

    Returns:
        The same *response* dict, mutated in place, with:

        * ``response['workflow_name']`` – resolved workflow name or *None*
        * Each asset in ``response['assets']`` gains ``actions``,
          ``validation_status``, ``validation_message``, ``object_id``,
          ``event_id``, ``recent_space_out_of_service``, plus SDE fields
          (``SpaceName``, ``Status``, ``Space_Type``, ``Address``) when
          applicable.
    """
    parent_wo = response.get("parent_work_order", {}) or {}
    template_id = parent_wo.get("WoTemplateId")
    description = parent_wo.get("Description")
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

    # 4. Extract sign dates from service request for sign workflows
    sign_start_date = None
    sign_end_date = None
    if workflow_name == "space_out_of_service_and_return":
        sr = response.get("service_request", {}) or {}
        sign_start_date = sr.get(SIGN_DATE_FIELDS["start"])
        sign_end_date = sr.get(SIGN_DATE_FIELDS["end"])

    # 5. Validate against OOS records
    response["assets"] = validate_space_assets(
        assets=response["assets"],
        parent_actual_finish_date=parent_actual_finish_date,
        workflow_name=workflow_name,
        sign_start_date=sign_start_date,
        sign_end_date=sign_end_date,
    )

    return response


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

    Enriches each asset dict with validation status, OOS record references,
    and a flag indicating whether the parent finish date is more recent than
    the latest OOS Date_Out for that space.

    For ``space_out_of_service_and_return`` workflows (temporary sign), the
    sign dates from the service request are used instead of the parent finish
    date.  An OOS record is checked for matching ``Date_Out`` and
    ``Date_Returned`` on the same dates as the sign start/end.

    Args:
        assets: Asset dicts from the work-order response.  Each must have at
            least ``EntityType``, ``EntityUid``, ``EntitySid``.  Space-layer
            assets should also have ``SpaceName``, ``Space_Type``, ``Address``,
            ``Status``, ``X``, ``Y``.  Every asset must already carry an
            ``actions`` key (list of action name strings).
        parent_actual_finish_date: ISO-format ``ActualFinishDate`` of the
            parent work order, or *None*.
        workflow_name: Resolved workflow name, or *None*.
        sign_start_date: ISO-format sign start datetime from the service
            request, or *None*.  Used for ``space_out_of_service_and_return``.
        sign_end_date: ISO-format sign end datetime from the service
            request, or *None*.  Used for ``space_out_of_service_and_return``.

    Returns:
        The same list (mutated in place and sorted by ``SpaceName``), with
        each asset gaining:

        * ``validation_status`` – ``'ready'``, ``'already_processed'``,
          ``'manual_review'``, or ``'error'``
        * ``validation_message`` – human-readable note (or *None*)
        * ``object_id`` – OOS ``OBJECTID`` for return-to-service (or *None*)
        * ``event_id`` – OOS ``Event_ID`` for return-to-service (or *None*)
        * ``recent_space_out_of_service`` – ``'true'`` / ``'false'``
        * ``sign_start_date`` / ``sign_end_date`` – (sign workflows only)
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

    # Parse sign dates once
    sign_start_dt = _parse_datetime(sign_start_date)
    sign_end_dt = _parse_datetime(sign_end_date)

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

        # Determine the reference date for recent_space_out_of_service.
        # For sign workflows, use sign_start_date; for hoods, use parent finish.
        ref_dt = sign_start_dt if sign_start_dt is not None else finish_dt

        # Compute recent_space_out_of_service
        if ref_dt is not None and space_name:
            max_oos = max_date_out_by_space.get(space_name)
            if max_oos is not None:
                # Ensure timezone-aware comparison
                if max_oos.tzinfo is None:
                    max_oos = max_oos.replace(tzinfo=timezone.utc)
                cmp_ref = ref_dt if ref_dt.tzinfo else ref_dt.replace(
                    tzinfo=timezone.utc
                )
                asset["recent_space_out_of_service"] = (
                    "true" if cmp_ref > max_oos else "false"
                )
            else:
                # No OOS records at all → this is the most recent
                asset["recent_space_out_of_service"] = "true"

        # Per-action validation
        has_oos = "space_out_of_service" in actions
        has_rts = "return_space_to_service" in actions

        if has_oos and has_rts and sign_start_dt is not None:
            # Combined sign workflow — validate as a single atomic operation
            _validate_sign_oos_and_return(
                asset, space_name, existing_records,
                sign_start_dt, sign_end_dt,
            )
        else:
            # Independent validation per action
            if has_oos:
                _validate_out_of_service(asset, space_name, existing_records, finish_dt)
            if has_rts:
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
        record_date = _to_date(record.get("Date_Out"))
        if record_date is not None and record_date == finish_date_only:
            asset["validation_status"] = "already_processed"
            asset["validation_message"] = (
                f"Record already exists for {finish_date_only}"
            )
            return


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


def _validate_sign_oos_and_return(
    asset: Dict[str, Any],
    space_name: Optional[str],
    existing_records: Dict[str, List[dict]],
    sign_start_dt: datetime,
    sign_end_dt: Optional[datetime],
) -> None:
    """Validate a combined sign out-of-service-and-return against OOS records.

    For temporary No Parking Sign workflows the OOS record is created in a
    single operation with both ``Date_Out`` (sign start) and ``Date_Returned``
    (sign end).  Validation checks whether such a record already exists.

    If the record is found: ``already_processed``.
    If not found: ``ready``, and the sign dates are stored on the asset so
    the processing endpoint can use them.
    """
    if not space_name:
        return

    records = existing_records.get(space_name, [])
    start_date_only = sign_start_dt.date()
    end_date_only = sign_end_dt.date() if sign_end_dt else None

    for record in records:
        date_out = record.get("Date_Out")
        if date_out is None:
            continue

        # Compare Date_Out against sign start date
        record_date_out = _to_date(date_out)
        if record_date_out is None or record_date_out != start_date_only:
            continue

        # Date_Out matches the sign start.  Now check Date_Returned.
        date_returned = record.get("Date_Returned")

        if end_date_only is not None:
            # We expect a matching Date_Returned
            if date_returned is None or date_returned != date_returned:
                # NULL or NaN — not a completed sign record
                continue
            record_date_returned = _to_date(date_returned)
            if record_date_returned is None or record_date_returned != end_date_only:
                continue

            # Both dates match
            asset["validation_status"] = "already_processed"
            asset["validation_message"] = (
                f"OOS record already exists with Date_Out={start_date_only} "
                f"and Date_Returned={end_date_only}"
            )
            return
        else:
            # No sign end date — a Date_Out match alone is sufficient
            asset["validation_status"] = "already_processed"
            asset["validation_message"] = (
                f"OOS record already exists with Date_Out={start_date_only}"
            )
            return

    # No matching record found — ready to create
    asset["sign_start_date"] = sign_start_dt.isoformat()
    if sign_end_dt:
        asset["sign_end_date"] = sign_end_dt.isoformat()


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-format date/datetime string, returning *None* on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _to_date(value):
    """Coerce a date-like value (str, datetime, Timestamp) to a date, or None."""
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
