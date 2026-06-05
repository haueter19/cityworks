import json
import pandas as pd
from sqlalchemy import text
from typing import Optional
from cityworks.database import get_engine


def get_service_request_meter_oos(custom_field_id: Optional[int] = 1768, custom_field_value: Optional[str] = 'Y'):
    # Find all SRs where meters go OOS
    query = text("""
                    SELECT 
                        sr.RequestID, sr.Description, sr.ProblemCode, sr.Status, sr.Details, sr.ProbAddress, 
                        sr.InitiatedBySid, sr.InitiatedBy, sr.DateTimeInit, sr.SubmitToSid, sr.SubmitTo, sr.DateSubmitTo, sr.ClosedBySid, sr.ClosedBy, sr.DateTimeClosed,
                        cf.CustFieldId, cf.CustFieldName, cf.CustFieldValue
                    FROM azteca.Request sr
                    INNER JOIN azteca.ReqCustField cf On (sr.RequestId=cf.RequestId)
                    WHERE 
                        DomainID = 3 -- Parking domain ID
                        AND cf.CUSTFIELDID = :custom_field_id -- same as: CustFieldName = 'Using Metered Parking Space?'
                        AND cf.CUSTFIELDVALUE = :custom_field_value
                """)
    
    df = pd.read_sql(query, get_engine(database='CITYWORKS', server='pubworksdbprd'), params={'custom_field_id':custom_field_id, 'custom_field_value': custom_field_value})
    return df