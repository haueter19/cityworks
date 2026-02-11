import json
import pandas as pd
from sqlalchemy import text
from cityworks.database import get_engine


def get_asset_history(asset_id: str) -> pd.DataFrame:
    """
    Returns the history of work orders that have been attached to an asset.
    
    Requires the asset ID which is the Cityworks ID from the SDE or the Entity Uid from Cityworks
    """
    query = text("""
                SELECT 
                    woe.WorkOrderSid
                    ,wo.InitiateDate
                    ,wo.Description
                    ,wo.ActualFinishDate
                    ,wo.DateWoClosed
                    ,EntityUid
                    ,EntityType
                    ,Feature_ID
                    ,Feature_Type
                    ,EntitySid
                    ,WorkCompleted
                    ,woe.WorkOrderSid
                FROM CMMS.azteca.WorkOrderEntity woe
                INNER JOIN CMMS.azteca.WorkOrder wo On (woe.WorkOrderSid=wo.WorkOrderSid)
                WHERE
                    EntityUid = :asset_id
                ORDER BY wo.InitiateDate desc, wo.ActualFinishDate desc
                """)
    df = pd.read_sql(query, get_engine(database='CMMS'), params={'asset_id': asset_id})
    return df