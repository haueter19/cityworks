import json
import pandas as pd
from sqlalchemy import text
from typing import Optional, List
from cityworks.database import get_engine


def get_work_order_details_json(work_order_sid: int) -> pd.DataFrame:
    """
    Query based on a single work order. Finds components related to that work order such as assets, instructions, comments, custom fields, parent work order and custom fields, and service request and custom fields.
    Returns JSON designed to be used for the Parking Division API interface. 
    """
    query = text("""
            WITH wo AS (
                -- Get details about single work order
                SELECT 
                    WorkOrderSid, WorkOrderId, Status, Description, Supervisor, SupervisorSid, ProjStartDate, ProjFinishDate, ActualStartDate, ActualFinishDate, RequestedBy, RequestedBySid,
                    SubmitToSid, SubmitTo, SubmitToOpenBy, SubmitToOpenBySid, Priority, WoAddress, Location, WoxCoordinate, WoYCoordinate, WoClosedBy, ExpenseType, WoCategory, WoTemplateId, 
                    InitiateDate, InitiatedBy, InitiatedBySid, DateWoClosed
                FROM CMMS.azteca.WorkOrder
                WHERE DomainId = 3 AND WorkOrderSid = :work_order_sid
            ),
            parent_work_order AS (
                SELECT
                    p.WorkOrderSid, p.WorkOrderId, p.Description, p.Status,
                    p.ProjStartDate, p.ProjFinishDate, p.ActualStartDate, p.ActualFinishDate,
                    p.SubmitTo, p.DateSubmitTo, p.DateSubmitToOpen,
                    p.WoClosedBy, p.DateWoClosed, p.ExpenseType,
                    p.Supervisor, p.SupervisorSid, p.WoAddress, p.Location,
                    p.WoTemplateId,
                    al.ActivityLinkId
                FROM CMMS.azteca.ActivityLink al
                INNER JOIN CMMS.azteca.WorkOrder p ON (al.SourceActivityId = p.WorkOrderSid)
                WHERE
                    p.DomainId = 3
                    AND al.DestActivityId = :work_order_sid
                    AND al.DestActivityType IN ('WorkOrder', 'ServiceRequest')
                    AND al.LinkType = 'Parent'
            ),
            parent_instructions AS (
                -- Get parent work order's instructions
                SELECT
                    i.WorkOrderSid, i.WorkOrderId, i.SeqId, i.Instructions
                FROM CMMS.azteca.WoInstruction i
                INNER JOIN parent_work_order p ON (i.WorkOrderSid = p.WorkOrderSid)
            ),
            assets AS (
                -- Get all entities associated with single work order
                SELECT
                    WorkOrderSid, WorkOrderId, EntityUid, EntityType, EntitySid, X, Y
                FROM CMMS.azteca.WORKORDERENTITY
                WHERE WorkOrderSid = :work_order_sid
            ),
            comments AS (
                -- Get all Comments
                SELECT 
                    CommentId, WorkOrderSid, WorkOrderId, AuthorSid, Comments, 
                    DateCreated, LastModified, LastModifiedBySid
                FROM CMMS.azteca.WorkOrderComment
                WHERE WorkOrderSid = :work_order_sid
            ),
            instructions AS (
                -- Get all instructions
                SELECT
                    WorkOrderSid, WorkOrderId, SeqId, Instructions
                FROM CMMS.azteca.WoInstruction
                WHERE WorkOrderSid = :work_order_sid 
            ),
            custom_fields AS (
                -- Get custom fields
                SELECT WorkOrderSid, WorkOrderId, CustFieldId, CustFieldName, CustFieldValue
                FROM CMMS.azteca.WoCustField
                WHERE WorkOrderSid = :work_order_sid
            ),
            parent_custom_fields AS (
                --
                SELECT 
                    CustFieldId,
                    CustFieldName,
                    CustFieldValue
                FROM parent_work_order pwo
                INNER JOIN CMMS.azteca.WoCustField cf On (pwo.WorkOrderSid=cf.WorkOrderSid)
                WHERE cf.WorkOrderSid = pwo.WorkOrderSid
            ), ActivityHierarchy AS (
                SELECT 
                    al.SOURCEACTIVITYID,
                    al.SOURCEACTIVITYTYPE,
                    al.DESTACTIVITYID,
                    0 AS Level
                FROM CMMS.azteca.ACTIVITYLINK al
                WHERE al.DESTACTIVITYID = :work_order_sid
                
                UNION ALL
                
                SELECT 
                    al.SOURCEACTIVITYID,
                    al.SOURCEACTIVITYTYPE,
                    al.DESTACTIVITYID,
                    ah.Level + 1
                FROM CMMS.azteca.ACTIVITYLINK al
                INNER JOIN ActivityHierarchy ah 
                    ON al.DESTACTIVITYID = ah.SOURCEACTIVITYID
                WHERE ah.SOURCEACTIVITYTYPE != 'ServiceRequest'
                    AND ah.Level < 10
            )
            -- Combine all results with identifiers
            SELECT 
                'work_order' as result_type,
                (SELECT * FROM wo FOR JSON PATH, WITHOUT_ARRAY_WRAPPER) as json_data
            UNION ALL
            SELECT 
                'parent_work_order' as result_type,
                (SELECT * FROM parent_work_order FOR JSON PATH, WITHOUT_ARRAY_WRAPPER) as json_data
            UNION ALL
            SELECT 
                'assets' as result_type,
                (SELECT * FROM assets FOR JSON PATH) as json_data
            UNION ALL
            SELECT 
                'comments' as result_type,
                (SELECT * FROM comments FOR JSON PATH) as json_data
            UNION ALL
            SELECT 
                'instructions' as result_type,
                (SELECT Instructions FROM instructions ORDER BY SeqId FOR JSON PATH) as json_data
            UNION ALL
            SELECT 
                'parent_instructions' as result_type,
                (SELECT Instructions FROM parent_instructions ORDER BY SeqId FOR JSON PATH) as json_data
            UNION ALL
            SELECT 
                'custom_fields' as result_type,
                (SELECT * FROM custom_fields FOR JSON PATH) as json_data
            UNION ALL
            SELECT 
                'parent_custom_fields' as result_type,
                (SELECT * FROM parent_custom_fields FOR JSON PATH) as json_data
            UNION ALL
            SELECT
                'service_request' as result_type,
                (SELECT
                    r.RequestId, 
                    r.Description, 
                    r.Status, 
                    r.InitiatedBy, 
                    r.DateTimeInit,
                    r.Details,
                    src.Comments,
                    (SELECT cf.CustFieldName, cf.CustFieldValue
                    FROM CMMS.azteca.ReqCustField cf
                    WHERE r.REQUESTID = cf.REQUESTID
                    FOR JSON PATH) as cf
                FROM CMMS.azteca.Request r
                LEFT JOIN CMMS.azteca.ServiceRequestComment src On (r.RequestId=src.RequestId)
                WHERE
                    r.RequestId = (SELECT TOP 1
                                    SOURCEACTIVITYID AS ServiceRequestID
                                FROM ActivityHierarchy
                                WHERE SOURCEACTIVITYTYPE = 'ServiceRequest'
                                ORDER BY Level DESC)
                FOR JSON PATH, WITHOUT_ARRAY_WRAPPER) as json_data
            """)
    # Execute the query, return a dataframe
    df = pd.read_sql(query, get_engine(database='cmms', server='pubworksdbprd'), params={'work_order_sid': work_order_sid})

    if df.shape[0] == 0:
        return 
    
    # Initialize response structure
    response = {
        "work_order": {},
        "parent_work_order": {},
        "assets": [],
        "comments": [],
        "instructions": [],
        "parent_instructions": [],
        "custom_fields": {},
        "parent_custom_fields": {},
        "service_request": {}
    }

    for i, row in df.iterrows():
        result_type = row.result_type
        json_data = row.json_data
        
        if json_data:
            parsed_data = json.loads(json_data)
            
            if result_type == 'work_order':
                response['work_order'] = parsed_data
            elif result_type == 'parent_work_order':
                response['parent_work_order'] = parsed_data if isinstance(parsed_data, dict) else {}
            elif result_type == 'assets':
                response['assets'] = parsed_data if isinstance(parsed_data, list) else []
            elif result_type == 'comments':
                response['comments'] = parsed_data if isinstance(parsed_data, list) else []
                response['comments'] = "\n".join([i['Comments'] for i in parsed_data]) if isinstance(parsed_data, list) else []
            elif result_type == 'instructions':
                response['instructions'] = parsed_data if isinstance(parsed_data, list) else []
            elif result_type == 'parent_instructions':
                    response['parent_instructions'] = "','".join([i['Instructions'] for i in parsed_data]) if isinstance(parsed_data, list) else ''
            elif result_type == 'custom_fields':
                # Convert array to flattened dict
                if isinstance(parsed_data, list):
                    response['custom_fields'] = {
                        item['CustFieldName']: item['CustFieldValue'] 
                        for item in parsed_data
                    }
            elif result_type == 'parent_custom_fields':
                # Convert array to flattened dict
                if isinstance(parsed_data, list):
                    response['parent_custom_fields'] = {
                        item['CustFieldName']: item['CustFieldValue'] 
                        for item in parsed_data
                    }
            elif result_type == 'service_request':
                if isinstance(parsed_data, dict):
                    if 'cf' in parsed_data.keys():
                        cf = parsed_data.pop('cf')
                        response['service_request'] = parsed_data
                    
                        # Convert array to flattened dict
                        response['service_request'].update({
                            item['CustFieldName']: item['CustFieldValue'] 
                            for item in cf
                        })
                    else:
                        response['service_request'] = parsed_data
                        
    return response



def get_work_orders(template_description: str = 'Update GIS', statuses: List[str] = ['OPEN', 'HOLD']) -> pd.DataFrame:
    """
    Returns information from work orders with a certain template description type (default is 'Update GIS') and status (default is ['Open', 'Hold']). 

    The return information includes some data from the parent work order, if it exists. 
    """
    statuses_str = "', '".join(statuses)
    query = text(f"""
        SELECT 
            wo.WorkOrderId, wo.WorkOrderSid, wo.Description, wo.Status, wo.Supervisor, wo.RequestedBy, wo.InitiatedBy, wo.InitiateDate, wo.SubmitTo, wo.DateSubmitTo, wo.DateSubmitToOpen, wo.WorkCompletedBy, 
            wo.Location, wo.WoAddress, wo.ProjStartDate, wo.ProjFinishDate, wo.ActualStartDate, wo.ActualFinishDate, wo.Cancel, wo.WoXCoordinate x_coord, wo.WOYCOORDINATE y_coord, 
            wo.SupervisorSid, wo.RequestedBySid, wo.InitiatedBySid, wo.SubmitToSid, wo.SubmitToOpenBySid, wo.WorkCompletedBySid, wo.WoTemplateId, 
            COALESCE(pa.DESCRIPTION, 'TE Signing') ParentTemplateDescription --, al.*
        FROM CMMS.azteca.WorkOrder wo
        left join CMMS.azteca.ActivityLink al On (wo.WorkorderId=al.DESTACTIVITYID)
        left join CMMS.azteca.WorkOrder pa On (al.SOURCEACTIVITYID=pa.WorkOrderId)
        WHERE
            wo.DomainID = 3
            AND wo.Description = :template_description
            --AND wo.WOTEMPLATEID = '217'
            AND wo.Status IN ('{statuses_str}')
            AND (al.LINKTYPE = 'Parent' or al.LINKTYPE IS NULL)
            AND (al.SOURCEACTIVITYTYPE IN ('WorkOrder', 'ServiceRequest') Or al.SOURCEACTIVITYTYPE IS NULL)
        ORDER BY 1 DESC
    """)
    df = pd.read_sql(query, get_engine(database='cmms', server='pubworksdbprd'), params={'template_description': template_description})
    return df



def get_activity_tree(work_order_sid: int, direction: Optional[str] = 'origin', level_limit: Optional[int] = 25) -> pd.DataFrame:
    """
    Recursive loop starting with any work order ID. User determines the direction, default is toward the origin or oldest parent. 

    Returns:
        Dataframe of activity links in the chain
    """
    if direction == 'origin':
        destination_id = 'DestActivityId'
        source_id = 'SourceActivityId'
        destination_type = 'DestActivityType'
    else:
        destination_id = 'SourceActivityId'
        source_id = 'DestActivityId'
        destination_type = 'SourceActivityType'

    query = text(f"""
                -- Recursive loop from an Update GIS work order to the parent SR
                WITH ActivityHierarchy AS (
                    -- Base case: start with your target activity
                    SELECT 
                        al.DestActivityId,
                        al.DestActivityType,
                        al.SourceActivityId,
                        al.SourceActivityType,
                        al.LinkType,        
                        0 AS Level
                    FROM azteca.ActivityLink al
                    WHERE 
                        al.{destination_id} = :work_order_sid
                        and al.DestActivityType != 'Inspection'
                        and al.SourceActivityType != 'Inspection'
                    
                    UNION ALL
                    
                    -- Recursive case: keep going up the chain
                    SELECT 
                        al.DestActivityId,
                        al.DestActivityType,
                        al.SourceActivityId,
                        al.SourceActivityType,
                        al.LinkType,
                        ah.Level + 1
                    FROM azteca.ActivityLink al
                    INNER JOIN ActivityHierarchy ah ON (al.{destination_id} = ah.{source_id})
                    WHERE 
                        ah.SourceActivityType != 'ServiceRequest'  -- Stop when we hit a ServiceRequest
                        AND al.{destination_type} != 'Inspection'
                        AND ah.Level < :level_limit  -- Safety limit to prevent infinite loops
                )
                SELECT 
                    DestActivityId,
                    DestActivityType,
                    SourceActivityId,
                    SourceActivityType,
                    LinkType,
                    Level
                FROM ActivityHierarchy
                ORDER BY Level, {destination_id};
                 """)
    df = pd.read_sql(query, get_engine(database='cmms', server='pubworksdbprd'), params={'work_order_sid': work_order_sid, 'level_limit':level_limit})
    return df



def get_work_orders_missing_update_gis(template_description: str = 'Hood/Sign Space', date_from: str = '2023-01-01') -> pd.DataFrame:
    """
    Query to find any work orders of a specific type that do not have a child Update GIS work order type. This would be used with any work orders that would be expected to have an Update GIS child.
    """
    query = text("""
                    SELECT 
                        wo.WorkOrderId, wo.DESCRIPTION AS wo_desc, wo.InitiateDate, wo.INITIATEDBY, wo.REQUESTEDBY
                    FROM azteca.WorkOrder wo
                    INNER JOIN azteca.WOTEMPLATE t ON (wo.WOTEMPLATEID = t.WOTEMPLATEID)
                    WHERE
                        wo.domainId = 3
                        AND wo.INITIATEDATE >= :date_from
                        AND t.DESCRIPTION = :template_description
                        AND NOT EXISTS (
                            SELECT 1
                            FROM azteca.ActivityLink al
                            INNER JOIN azteca.WorkOrder ch ON (al.DESTACTIVITYID = ch.WorkOrderId)
                            INNER JOIN azteca.WOTEMPLATE ch_template ON (ch.WOTEMPLATEID = ch_template.WOTEMPLATEID)
                            WHERE al.SOURCEACTIVITYID = wo.WorkOrderId
                                AND al.LINKTYPE = 'Parent'
                                AND al.SOURCEACTIVITYTYPE = 'WorkOrder'
                                AND ch_template.DESCRIPTION = 'Update GIS'
                        )
                    order by wo.InitiateDate desc
                 """)
    df = pd.read_sql(query, get_engine(database='cmms', server='pubworksdbprd'), params={'template_description':template_description, 'date_from': date_from})
    return df