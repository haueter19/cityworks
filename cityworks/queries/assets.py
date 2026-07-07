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
                    woe.WorkOrderId
                    ,al.SourceActivityStringId ParentWorkOrderId
                    ,wo.Description
                    ,wo.Status
                    ,(select 
                        TOP 1 ch.WorkOrderId
                    from CITYWORKS.azteca.WorkOrder pa 
                    inner join CITYWORKS.azteca.ActivityLink al On (al.SourceActivityStringId=pa.WorkOrderId)
                    inner join CITYWORKS.azteca.WorkOrder ch On (al.DestActivityStringId=ch.WorkOrderId)
                    where 
                        --SourceActivityStringId = '60727'
                        --DestActivityStringId = '60727'
                        al.LinkType = 'Parent'
                        AND pa.DomainId = 3
                        AND pa.WorkOrderId = wo.WorkOrderId
                        AND ch.Description = 'Update GIS'
                    ) As has_update_gis
                    ,wo.InitiateDate
                    ,wo.DateSubmitTo
                    ,wo.ActualFinishDate
                    ,wo.DateWoClosed
                    ,wo.InitiatedBy   
                    ,wo.SubmitTo
                    ,wo.WorkCompletedBy
                    ,wo.WoClosedBy
                    ,wo.WorkOrderId
                    ,EntityUid
                    ,EntityType
                    ,Feature_ID
                    ,Feature_Type
                    ,EntitySid
                FROM CITYWORKS.azteca.WorkOrderEntity woe
                INNER JOIN CITYWORKS.azteca.WorkOrder wo On (woe.WorkOrderId=wo.WorkOrderId)
                LEFT JOIN CITYWORKS.azteca.ActivityLink al On (wo.WorkOrderId=al.DestActivityStringId and al.LinkType != 'Related' AND al.SourceActivityType != 'Inspection')
                WHERE
                    wo.DomainId = 3
                    AND wo.Status != 'Cancel'
                    AND (al.LinkType = 'Parent' OR al.LinkType IS NULL)
                    AND (al.SourceActivityType = 'WorkOrder' OR al.SourceActivityType IS NULL)
                    AND EntityUid = :asset_id
                ORDER BY wo.DateSubmitTo desc, wo.InitiateDate desc, wo.ActualFinishDate desc
                """)
    df = pd.read_sql(query, get_engine(database='CITYWORKS'), params={'asset_id': asset_id})
    return df



def get_entity_id_from_space_name(assets: List[int]) -> pd.DataFrame:
    """
    Docstring for get_entity_id_from_space_name
    
    :param assets: list of space names
    :type assets: List[int]
    :return: Description
    :rtype: DataFrame
    """
    if not isinstance(assets, list):
        assets = [assets]
    assets_str = "','".join([str(a) for a in assets])

    query = text(f"""
                 SELECT SpaceName, cwAssetID, DateRemoved FROM Traffic.data_admin8.PU_METERS_ON_ST WHERE SpaceName in ('{assets_str}')
                    """)
    df = pd.read_sql(query, get_engine(database='Traffic'))
    return df


def get_space_name_from_entity_id(assets: List[int]) -> pd.DataFrame:
    """
    Docstring for get_space_name_from_entity_id
    
    :param assets: list of cwAssetIDs
    :type assets: List[int]
    :return: Description
    :rtype: DataFrame
    """
    if not isinstance(assets, list):
        assets = [assets]
    assets_str = "','".join([str(a) for a in assets])

    query = text(f"""
                 SELECT SpaceName, cwAssetID, DateRemoved FROM Traffic.data_admin8.PU_METERS_ON_ST WHERE cwAssetID in ('{assets_str}')
                    """)
    df = pd.read_sql(query, get_engine(database='Traffic'))
    return df