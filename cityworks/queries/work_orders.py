import json
import pandas as pd
from sqlalchemy import text
from cityworks.database import get_engine


def get_work_order_details(work_order_sid: int) -> pd.DataFrame:

    query = text("""
            WITH wo AS (
                -- Get details about single work order
                SELECT 
                    WorkOrderSid, WorkOrderId, Status, Description, Supervisor, SupervisorSid, ProjStartDate, ProjFinishDate, ActualStartDate, ActualFinishDate, RequestedBy, RequestedBySid,
                    SubmitTo, SubmitToOpenBy, SubmitToOpenBySid, Priority, WoAddress, Location, WoxCoordinate, WoYCoordinate, WoClosedBy, ExpenseType, WoCategory, WoTemplateId, InitiateDate,
                    InitiatedBy, InitiatedBySid, DateWoClosed
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
                    (SELECT cf.CustFieldName, cf.CustFieldValue
                    FROM CMMS.azteca.ReqCustField cf
                    WHERE r.REQUESTID = cf.REQUESTID
                    FOR JSON PATH) as cf
                FROM CMMS.azteca.Request r
                WHERE
                    r.RequestId = (SELECT TOP 1
                                    SOURCEACTIVITYID AS ServiceRequestID
                                FROM ActivityHierarchy
                                WHERE SOURCEACTIVITYTYPE = 'ServiceRequest'
                                ORDER BY Level DESC)
                FOR JSON PATH, WITHOUT_ARRAY_WRAPPER) as json_data
            """)
    df = pd.read_sql(query, get_engine(database='cmms', server='pubworksdbprd'), params={'work_order_sid': work_order_sid})
    return df


