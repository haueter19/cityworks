"""Business logic related to Parking operations"""

from typing import Optional, Dict, Any, List
from datetime import datetime
from sqlalchemy.engine import Engine
from sqlalchemy import text
import pandas as pd
import geopandas as gpd
import re
from cityworks.database import get_engine
from .operations import insert_row, update_fields, update_status, query_records, format_sde_query
import cityworks.client.exceptions as exc


def add_space_out_of_service(space_data: List[Dict]) -> int:
    """
    Mark a parking space as out of service.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        space_id: Unique identifier for the parking space
        reason: Reason for taking space out of service
        start_date: Date when space goes out of service
        end_date: Optional date when space will return to service
        notes: Additional notes
        work_order_id: Related work order ID if applicable
        
    Returns:
        OBJECTID of the new record
        
    Example:
        engine = get_engine('sde')
        
        oid = add_space_out_of_service(
            engine,
            space_id='SP-123',
            reason='Construction',
            start_date=datetime.now(),
            work_order_id=126191
        )
    """
    
    stmt = text("""
        INSERT INTO data_admin8.PU_SPACESOUTOFSERVICE
        (OBJECTID, Event_ID, Date_Out, Space_Type, created_user, created_date, last_edited_user, last_edited_date, Note, SpaceNumber, Shape, RevenueCollected, RemovalMethod, 
                RemovedBy, ReasonRemoved, ADA_Relocation)
        VALUES
        ((SELECT MAX(OBJECTID)+1 FROM data_admin8.PU_SPACESOUTOFSERVICE), (SELECT MAX(Event_ID)+1 FROM data_admin8.PU_SPACESOUTOFSERVICE), :date_out, :meter_type, 
                :created_user, :created_date, :last_edited_user, :last_edited_date, :note, :space_number, 
                geometry::STPointFromText('POINT(' + CAST(:x AS varchar(50)) + ' ' + CAST(:y AS varchar(50)) + ')', 103412), :revenue_collected, 
                :removal_method, :removed_by, :reason_removed, :ada_relocation)
        """)
    
    #max_values = pd.read_sql("SELECT max(OBJECTID) max_object_id, max(Event_ID) max_event_id FROM data_admin8.PU_SPACESOUTOFSERVICE", get_engine('Traffic'))
    #max_object_id = max_values['max_object_id'].iloc[0]
    #max_event_id = max_values['max_event_id'].iloc[0]
    row_count = 0
    with get_engine('Traffic').begin() as conn:
    #    for i, space in space_data
        for space in space_data:
            #max_object_id += 1
            #max_event_id += 1
            
            #space['object_id'] = int(max_object_id)
            #space['event_id'] = int(max_event_id)
            
            result = conn.execute(stmt, space)
            row_count += result.rowcount

    return row_count
    #return insert_row(engine, 'PU_SPACESOUTOFSERVICE', data)


def return_space_to_service(update_values: Dict) -> int:
    """
    Mark a parking space as returned to service.
    
    Updates the most recent out-of-service record for this space.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        space_id: Unique identifier for the parking space
        end_date: Date when space returned to service (defaults to now)
        notes: Additional notes
        
    Returns:
        Number of rows updated (should be 1 if successful)
    """
    # Update PU_SPACESOUTOFSERVICE record by setting the Date_Returned
    stmt = text("""
                UPDATE data_admin8.PU_SPACESOUTOFSERVICE
                SET 
                    Date_Returned = :date_returned,
                    last_edited_date = :last_edited_date,
                    last_edited_user = :last_edited_user
                WHERE 
                    Date_Returned IS NULL
                    AND Event_ID = :event_id
                    AND OBJECTID = :object_id
            """)
    
    with get_engine('Traffic').begin() as conn:
        result = conn.execute(stmt, update_values)
        rows_updated = result.rowcount
    
    return rows_updated


def update_space_status(table: str, space_id: str, status: str, last_edited_date: Optional[str], last_edited_user: Optional[str]) -> int:
    """
    Update the status of a parking space (metered, loading zone, or dis/vet).
    
    Args:
        table: Table name (e.g., 'PU_METERS_ON_ST')
        space_id: Cityworks Asset ID of the space
        status: New status value (e.g. 'In service', 'Out of service')
        last_edited_date: Date when the record was last edited
        last_edited_user: User who last edited the record

    Returns:
        Number of rows updated
    """
    cityworks_field_map = {
        'PU_METERS_ON_ST': 'cwAssetID',
        'PU_LZ_ON_ST': 'CWasset_ID',
        'PU_DISVET_ON_ST': 'CWasset_ID',
        'PU_OFF_ST_SPACES': 'cwAssetID'
    }
    status_map = {
        'PU_METERS_ON_ST': 'Status',
        'PU_LZ_ON_ST': 'Status',
        'PU_DISVET_ON_ST': 'Status',
        'PU_OFF_ST_SPACES': 'Space_Status'
    }

    stmt = text(f"""
            UPDATE data_admin8.{table} 
            SET
                {status_map[table]} = :status,
                last_edited_date = :last_edited_date,
                last_edited_user = :last_edited_user
            WHERE
                {cityworks_field_map[table]} = :cw_asset_id
                
    """)
    with get_engine('Traffic').begin() as conn:
        result = conn.execute(stmt, {'status':status, 'cw_asset_id':space_id})
        rows_updated = result.rowcount
    
    return rows_updated


def get_spaces_from_work_order(work_order: Dict[str, Any]) -> pd.DataFrame:
    """Uses the entities from a work order to find the space record in the SDE"""
    # Must have a key called `assets` which is a list of dicts with keys `EntityType` and `EntityUid`
    if 'assets' not in work_order.keys():
        return pd.DataFrame()
    if 'assets' in work_order.keys() and not isinstance(work_order['assets'], list):
        raise ValueError("Expected 'assets' to be a list of dictionaries")
    if not all('EntityType', 'EntityUid') in work_order['assets'][0].keys():
        raise ValueError("Expected each asset to have 'EntityType' and 'EntityUid' keys")
    
    # Establish dictionary to hold the entity type as the key and a list of assets by entityuid which is the CW asset ID    
    entity_type_dict = {}

    for asset in work_order['assets']:
        # If the entity type is not already a key in the dictionary, add it with an empty list
        if asset['EntityType'] not in entity_type_dict.keys():
            entity_type_dict[asset['EntityType']] = []
        
        # Append the entity uid to the list for that entity type
        entity_type_dict[asset['EntityType']].append(asset['EntityUid'])

    # Establish list to store queries. Because a work order may have a mixture of space types/entity types, we need to create a query for each table associated with each entity type
    query_list = []

    # Iterate entity_type_dict to build a query for each entity type and its associated list of entityuids
    for entity_type, entity_list in entity_type_dict.items():
        # Build the base query for the entity type
        base = format_sde_query(entity_type)

        # Build the where clause based on the entity type and list of entity uids
        cw_asset_id_list = "','".join(entity_list)
        if entity_type in ['PU_DISVET_ON_ST', 'PU_LZ_ON_ST']: # These tables use CWasset_ID instead of cwAssetID
            where_clause = f"CWasset_ID IN ('{cw_asset_id_list}')"
        else:
            where_clause = f"cwAssetID IN ('{cw_asset_id_list}')"
        query_list.append(base + where_clause)

    
    # Initialize empty dataframe to hold all spaces
    spaces = pd.DataFrame()

    for qry in query_list:
        try:
            temp = gpd.read_postgis(qry, get_engine('Traffic'), geom_col='geometry', crs='EPSG:8193')
            if 'PU_DISVET_ON_ST' in qry:
                temp['Meter_Type'] = 'ADA'
            match = re.search(r'FROM\s+([^\s]+)', qry, re.IGNORECASE)
            if match:
                table_name = match.group(1)
                temp['table_name'] = table_name
        except:
            temp = pd.read_sql(qry, get_engine('Traffic'))
            
        temp.rename(columns={'CWasset_ID':'cwAssetID', 'Space_Status':'Status', 'LZ_Type':'Space_Type', 'Meter_Type':'Space_Type'}, inplace=True)
        if 'Block_Nbr' in temp.columns and 'St_Pre_Dir' in temp.columns and 'St_Name' in temp.columns and 'St_Name_Suf' in temp.columns:
            temp.fillna({'St_Pre_Dir':'' , 'St_Name_Suf':'', 'St_Name':'', 'Block_Nbr':0}, inplace=True)
            temp['Address'] = (temp['Block_Nbr'].astype(str) + ' '+ temp['St_Pre_Dir']+ ' ' + temp['St_Name'] + ' ' + temp['St_Name_Suf']).str.replace('  ',' ')
        else:
            if 'Facility_Name_Full' in temp.columns:
                temp['Address'] = temp['Facility_Name_Full']
            else:
                temp['Address'] = 'No address found'
        # column in temp to explain which table it's from
        # only needed if there's more than one
        spaces = pd.concat([spaces, temp])
    
    return spaces


def cc_terminal_add_asset(work_order: Dict[str, Any]) -> int:
    """"""
    # work_order dict should have Model, Serial Number, Terminal ID, and Brand, but MUST have Terminal ID
    if 'Terminal ID' not in work_order['custom_fields']:
        print('need to raise exception, must have this')
    
    # Get max OBJECTID from SDE
    df = pd.read_sql("SELECT max(OBJECTID) max_object_id FROM Traffic.data_admin8.PU_REVENUE_CREDITCARDTERMINALS", get_engine('Traffic'))
    if df.shape[0] > 0:
        object_id = df['max_object_id'].iloc[0] + 1
    else:
        print('need to raise issue')

    device_id = f"rvtrm-{object_id}" # Will always be 'rvtrm-' + OBJECTID
    cw_asset_id = f"cw-rvtrm-{object_id}" # Will always be 'cw-' + DeviceID
    uid = work_order['custom_fields']['Terminal ID'][-6:] # Should be last 6 digits
    # Should start with the same string. If it doesn't, add it
    if not work_order['custom_fields']['Terminal ID'].startswith('0010050008016090'):
        terminal_id = f'0010050008016090{uid}'
    else:
        terminal_id = work_order['custom_fields']['Terminal ID']
    
    with get_engine('Traffic').begin() as conn:
        result = conn.execute(text("""
                     INSERT INTO Traffic.data_admin8.PU_REVENUE_CREDITCARDTERMINALS
                        (OBJECTID, DeviceID, Model, SerialNumber, cwAssetID, Brand, TerminalID, UID)
                     VALUES
                        (:object_id, :device_id, :model, :serial_number, :cw_asset_id, :brand, :terminal_id, :uid)
                     """), {
                         'object_id':object_id,
                         'device_id':device_id,
                         'model':work_order['custom_fields']['Model'],
                         'serial_number':work_order['custom_fields']['Serial Number'],
                         'cw_asset_id':cw_asset_id,
                         'brand':work_order['custom_fields']['Brand'],
                         'terminal_id':terminal_id,
                         'uid':uid
                     })
        rows_updated = result.rowcount
    return rows_updated
    

def cc_terminal_remove_asset(work_order: Dict[str, Any]) -> int:
    """
    Remove Portable CC Terminal from service
    
    :param work_order: Description
    :type work_order: Dict[str, Any]
    :return: Description
    :rtype: int
    """
    if not work_order.get('ActualFinishDate'):
        raise exc.MissingRequiredValueError
    
    with get_engine('Traffic').begin() as conn:
        result = conn.execute(text("""
            UPDATE data_admin8.PU_REVENUE_CREDITCARDTERMINALS 
            SET DateRemoved = :actual_finish_date
            WHERE
                UID = :entity_uid
            """), {'actual_finish_date':work_order['ActualFinishDate'], 'entity_uid':work_order['assets'][0]['EntityUid']})
        rows_updated = result.rowcount
    return rows_updated
    

def cc_terminal_update_gis(work_order: Dict[str, Any]) -> Dict:
    """
    Handle changes that result from Update GIS work order with Portable CC Terminal asset. Occurs when one of these devices changes location.
    Must update SDE and revenue. 
    """
    # Must have work_order asset with EntityUid
    if not work_order['assets'][0].get('EntityUid'):
        raise exc.MissingRequiredValueError
    
    if not work_order['custom_fields'].get('Destination Location'):
        raise exc.MissingRequiredValueError
    else:
        if work_order['custom_fields']['Destination Location']=='OC':
            location_id = 1
        elif work_order['custom_fields']['Destination Location']=='SSCO':
            location_Id = 2
        elif work_order['custom_fields']['Destination Location']=='SSCA':
            location_Id = 5
        elif work_order['custom_fields']['Destination Location']=='CSN':
            location_Id = 6
        elif work_order['custom_fields']['Destination Location']=='SLS':
            location_Id = 18
        elif work_order['custom_fields']['Destination Location']=='WS':
            location_Id = 19
        else:
            location_id = -1
    
    engine = get_engine('Traffic')
    
    # Get active record for info. Will need to close this record.
    qry_active_assignment = text("""
        SELECT
	        ct.DeviceID base_device_id, ct.cwAssetID, ct.UID, ch.*
        FROM data_admin8.PU_REVENUE_CREDITCARDTERMINALS ct
        INNER JOIN data_admin8.PU_CC_TERMINAL_HISTORY ch On (ct.DeviceID=ch.DeviceID)
        WHERE
	        ct.UID = :uid
	        AND ch.DateRemoved IS NULL
        ORDER BY ct.UID, ch.DateAssigned, ch.DateRemoved
    """)

    close_active_assignment = text("""
        UPDATE data_admin8.PU_CC_TERMINAL_HISTORY
        SET 
            DateRemoved = :actual_finish_date,
            RemovedBy = :removed_by,
            WorkOrderRemoveId = :work_order_id,
            Notes = :notes,
            IsActive = 0
        WHERE
            OBJECTID = :object_id
            AND TerminalID = :terminal_id
        """)
    open_active_assignment = text("""
        INSERT INTO data_admin8.PU_CC_TERMINAL_HISTORY
        (OBJECTID, DeviceID, TerminalID, LocationID, DateAssigned, AssignedBy, WorkOrderAssignID, Notes, IsActive)
        VALUES
        ((SELECT MAX(OBJECTID)+1 FROM data_admin8.PU_CC_TERMINAL_HISTORY), :device_id, :terminal_id, :location_id, :date_assigned, :assigned_by, :work_order_assign_id, :notes, 1)
        """)
    with engine.begin() as conn:
        # Query for details from the current active assignment
        active_assignment = conn.execute(qry_active_assignment, {'uid':work_order['assets'][0]['EntityUid']}).fetchone()
        # Close active assignment with UPDATE query
        update_result = conn.execute(close_active_assignment, {
                                                                'actual_finish_date':work_order['ActualFinishDate'], # ActualFinishDate is the datetime of the move
                                                                'removed_by':work_order['InitiatedBy'], # Assume that the initiator is the remover
                                                                'work_order_id':work_order['WorkOrderSid'], # Work Order Sid
                                                                'object_id':active_assignment[3], # OBJECTID from active assignment
                                                                'terminal_id':active_assignment[5], # Terminal ID from active assignment
                                                                'notes':work_order['instructions']
                                                                })
        # Insert new record which becomes the active assignment
        insert_result = conn.execute(open_active_assignment, {
            'device_id':active_assignment[0], # Device ID like 'rvtrm-#'
            'terminal_id':active_assignment[5],
            'location_id':location_id,
            'date_assigned':work_order['ActualFinishDate'],
            'assigned_by':work_order['InitiatedBy'],
            'work_order_assign_id':work_order['WorkOrderSid'],
            'notes':work_order['instructions']
        })
    return {'updated':update_result, 'created':insert_result}




def process_work_order_gis_updates(
    engine: Engine,
    work_order_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Process all GIS updates for a work order.
    
    This orchestrates multiple operations based on the work order type.
    
    Args:
        engine: SQLAlchemy engine from ConnectionManager
        work_order_data: Work order details including entity type and changes
        
    Returns:
        Dictionary with results of each operation
        
    Example:
        from db_manager import ConnectionManager
        cnxn = ConnectionManager()
        engine = get_engine('sde')
        
        results = process_work_order_gis_updates(engine, {
            'work_order_id': 126191,
            'entity_type': 'PU_METERS_ON_ST',
            'entity_id': 'MTR-2395',
            'operation': 'UPDATE',
            'updates': {'METERSTATUS': 'ACTIVE'}
        })
    """
    results = {
        'work_order_id': work_order_data.get('work_order_id'),
        'success': False,
        'operations': []
    }
    
    try:
        entity_type = work_order_data['entity_type']
        entity_id = work_order_data['entity_id']
        operation = work_order_data.get('operation', 'UPDATE')
        
        if operation == 'UPDATE':
            rows_updated = update_fields(
                engine,
                entity_type,
                entity_id,
                work_order_data['updates']
            )
            results['operations'].append({
                'type': 'UPDATE',
                'table': entity_type,
                'id': entity_id,
                'rows_affected': rows_updated,
                'success': rows_updated > 0
            })
        
        elif operation == 'UPDATE_STATUS':
            rows_updated = update_meter_status(
                engine,
                meter_id=entity_id,
                status=work_order_data['status'],
                notes=work_order_data.get('notes')
            )
            results['operations'].append({
                'type': 'UPDATE_STATUS',
                'table': entity_type,
                'id': entity_id,
                'rows_affected': rows_updated,
                'success': rows_updated > 0
            })
        
        elif operation == 'OUT_OF_SERVICE':
            oid = add_space_out_of_service(
                engine,
                space_id=entity_id,
                reason=work_order_data.get('reason', 'Work Order'),
                start_date=work_order_data.get('start_date', datetime.now()),
                work_order_id=work_order_data.get('work_order_id')
            )
            results['operations'].append({
                'type': 'OUT_OF_SERVICE',
                'table': 'PU_SPACESOUTOFSERVICE',
                'oid': oid,
                'success': oid is not None
            })
        
        elif operation == 'RETURN_TO_SERVICE':
            rows_updated = return_space_to_service(
                engine,
                space_id=entity_id,
                end_date=work_order_data.get('end_date'),
                notes=work_order_data.get('notes')
            )
            results['operations'].append({
                'type': 'RETURN_TO_SERVICE',
                'table': 'PU_SPACESOUTOFSERVICE',
                'rows_affected': rows_updated,
                'success': rows_updated > 0
            })
        
        results['success'] = all(
            op.get('success', False) for op in results['operations']
        )
        
    except Exception as e:
        results['error'] = str(e)
        results['success'] = False
    
    return results