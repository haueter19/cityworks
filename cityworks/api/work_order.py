from typing import Dict, Any, List, Optional
from ..client.session import CityworksSession
from ..client.response import CityworksResponse
import pandas as pd
from cityworks.database import get_engine

class WorkOrderAPI:
    """High-level API for work order operations"""
    
    def __init__(self, session: CityworksSession):
        self.session = session
    

    def get_by_sid(self, work_order_sid: int) -> Dict[str, Any]:
        """Get work order by SID"""
        response = self.session.get('Ams', 'WorkOrder', 'BySid', 
                                    {'WorkOrderSid': work_order_sid})
        response.raise_for_status()
        return response.value
    

    def get_instructions(self, work_order_sid: int) -> Dict[str, str]:
        """Get instructions for multiple work orders"""
        response = self.session.get('Ams', 'WorkOrder', 'InstructionsByWorkOrderSids',
                                    {'WorkOrderSids': [work_order_sid]})
        if response.value.get(str(work_order_sid)) is not None: # only return if instructions exist
            return response.value.get(str(work_order_sid))
        else:
            response.raise_for_status()
            return response.value
    

    def get_comments(self, work_order_sid: int) -> List[str]:
        """
        Get a list of comments associated with the work order
        
        :param work_order_sid: WorkOrderSid
        :type work_order_sid: int
        :return: Description
        :rtype: List[str]
        """
        # Can't find how to use API for this
        comments = pd.read_sql(f"SELECT comments FROM CMMS.azteca.WorkOrderComment WHERE WorkOrderSid = {work_order_sid} ORDER BY DATECREATED", get_engine('cmms'))
        return comments['comments'].tolist()
    

    def add_comments(self, work_order_sid: int, comments: str) -> Dict[str, Any]:
        response = self.session.get('Ams', 'WorkOrder', 'AddComments',
                                    {'WorkOrderSid': work_order_sid, 'WorkOrderId':work_order_sid,'Comments':comments})
        response.raise_for_status()
        return response.value
    

    def get_entities(self, work_order_sid: int) -> List[Dict[str, Any]]:
        """Get assets/entities associated with a work order"""
        response = self.session.get('Ams', 'WorkOrder', 'Entities', {'WorkOrderSids': [work_order_sid]})
        response.raise_for_status()
        return response.value
    

    def add_entities(self, work_order_sid: int, entity_type: str, entity_uids: List[str]) -> Dict[str, Any]:
        """Add assets/entities to a work order"""
        params = {
            'WorkOrderSid': work_order_sid,
            'WorkOrderId': work_order_sid,
            'EntityType': entity_type,
            'EntityUids': entity_uids
        }
        response = self.session.get('Ams', 'WorkOrder', 'AddEntities', params)
        response.raise_for_status()
        return response.value
    
    

    def get_custom_fields(self, work_order_sid: int) -> Dict[str, Any]:
        """Get custom fields associated with a work order. Return as dictionary where the CustFieldName is the key and CustFieldValue is the value."""
        response = self.session.get('Ams', 'WorkOrder', 'CustomFieldsByWorkOrderSids', {'WorkOrderSids': [work_order_sid]})
        response.raise_for_status()
        return {item['CustFieldName']:item['CustFieldValue'] for item in response.value[str(work_order_sid)]}


    def create_from_parent(self, parent_wo_id: int, template_id: str = '217', 
                          **kwargs) -> Dict[str, Any]:
        """
        Create child work order from parent.
        
        Args:
            parent_wo_id: Parent work order ID
            template_id: Work order template ID
            **kwargs: Additional parameters for CreateFromParent
        """
        params = {
            'WorkOrderId': parent_wo_id,
            'WorkOrderSid': parent_wo_id,
            'WOTemplateId': template_id,
            **kwargs
        }
        
        response = self.session.get('Ams', 'WorkOrder', 'CreateFromParent', params)        
        response.raise_for_status()
        return response.value[0] if response.value else {}
    

    def create_from_service_request(self, service_request_id: int, entity_type: str, template_id: str, **kwargs) -> Dict[str, Any]:
        """
        Create work order from service request.
        
        Args:
            service_request_id: Service request ID
            entity_type: Entity type (e.g., 'Asset', 'Location')
            template_id: Work order template ID
            **kwargs: Additional parameters for CreateFromServiceRequest
        """
        params = {
            'RequestId': service_request_id,
            'EntityType': entity_type,
            'WOTemplateId': template_id,
            **kwargs
        }
        
        response = self.session.get('Ams', 'WorkOrder', 'CreateFromServiceRequest', params)
        response.raise_for_status()
        return response.value[0] if response.value else {}

    
    
    def update_work_order(self, work_order_sid: int, **kwargs) -> Dict[str, Any]:
        """Update work order"""
        params = {
            'WorkOrderId': work_order_sid,
            'WorkOrderSid': work_order_sid,
            **kwargs
        }
        
        response = self.session.get('Ams', 'WorkOrder', 'Update', params)
        response.raise_for_status()
        return response.value
    

    def close_work_order(self, work_order_id: str) -> Dict[str, Any]:
        """Close work orders"""
        response = self.session.get('Ams', 'WorkOrder', 'Close',
                                    {'WorkOrderIds': [work_order_id]})
        response.raise_for_status()
        return response.value
    

    def search(self, **kwargs) -> List[str]:
        """"""
        params = {
            **kwargs
        }
        response = self.session.get('Ams', 'WorkOrder', 'Search', params)
        response.raise_for_status()
        return response.value