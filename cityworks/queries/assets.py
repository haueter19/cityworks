import json
import pandas as pd
from sqlalchemy import text
from cityworks.database import get_engine
from typing import List


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



def get_entity_id_from_space_name(assets: List[int]) -> pd.DataFrame:
    """
    Docstring for get_entity_id_from_space_name
    
    :param assets: list of space names
    :type assets: List[int]
    :return: Description
    :rtype: DataFrame
    """
    assets_str = "','".join([str(a) for a in assets])

    query = text(f"""
                 SELECT SpaceName, cwAssetID FROM Traffic.data_admin8.PU_METERS_ON_ST WHERE SpaceName in ('{assets_str}')
                    """)
    df = pd.read_sql(query, get_engine(database='Traffic'))
    return df