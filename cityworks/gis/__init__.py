"""GIS operations and utilities."""
from .operations import (
    format_sde_query,
    get_spaces_out_of_service,
    sde_query,
    sde_insert,
    sde_update,
    sde_transaction,
    get_next_id,
    get_cw_asset_field,
    get_status_field,
    get_active_terminal_assignment,
    CITYWORKS_FIELD_MAP,
    STATUS_FIELD_MAP,
)
from .parking import (
    # workflow registry
    WORKFLOWS,
    TEMPLATE_WORKFLOWS,
    DESCRIPTION_WORKFLOWS,
    SPACE_LAYERS,
    CC_TERMINAL_LAYERS,
    get_workflow,
    get_workflow_name,
    get_asset_actions,
    # business logic
    add_space_out_of_service,
    return_space_to_service,
    update_space_status,
    get_spaces_from_work_order,
    cc_terminal_add_asset,
    cc_terminal_remove_asset,
    cc_terminal_update_gis,
)

__all__ = [
    # operations — constants
    "CITYWORKS_FIELD_MAP",
    "STATUS_FIELD_MAP",
    # operations — query / CRUD
    "format_sde_query",
    "get_spaces_out_of_service",
    "sde_query",
    "sde_insert",
    "sde_update",
    "sde_transaction",
    "get_next_id",
    "get_cw_asset_field",
    "get_status_field",
    "get_active_terminal_assignment",
    # parking — workflow registry
    "WORKFLOWS",
    "TEMPLATE_WORKFLOWS",
    "DESCRIPTION_WORKFLOWS",
    "SPACE_LAYERS",
    "CC_TERMINAL_LAYERS",
    "get_workflow",
    "get_workflow_name",
    "get_asset_actions",
    # parking — business logic
    "add_space_out_of_service",
    "return_space_to_service",
    "update_space_status",
    "get_spaces_from_work_order",
    "cc_terminal_add_asset",
    "cc_terminal_remove_asset",
    "cc_terminal_update_gis",
]
