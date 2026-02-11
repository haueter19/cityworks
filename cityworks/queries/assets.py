import json
import pandas as pd
from sqlalchemy import text
from cityworks.database import get_engine


def get_asset_history(asset_id: str) -> pd.DataFrame:

    query = text("""
                SELECT 
                    woe.WorkOrderSid
                    ,wo.InitiateDate
                    ,wo.DESCRIPTION
                    ,wo.ACTUALFINISHDATE
                    ,wo.DATEWOCLOSED
                    ,[ENTITYUID]
                    ,[ENTITYTYPE]
                    ,[FEATURE_ID]
                    ,[FEATURE_TYPE]
                    ,[ENTITYSID]
                    ,[WORKCOMPLETED]
                    ,woe.[WORKORDERSID]
                FROM [CMMS].[azteca].[WORKORDERENTITY] woe
                INNER JOIN azteca.WorkOrder wo On (woe.WorkOrderSid=wo.WorkOrderSid)
                WHERE
                    ENTITYUID = :asset_id
                ORDER BY wo.INITIATEDATE desc, wo.ACTUALFINISHDATE desc
                """)
    df = pd.read_sql(query, get_engine(database='CMMS'), params={'asset_id': asset_id})
    return df