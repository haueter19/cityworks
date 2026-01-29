"""
Backward compatibility layer for old cityworksrequest module.
Deprecated - use cityworks.CityworksSession instead.
"""
import warnings
from .client.session import CityworksSession
from .config import CityworksConfig
import getpass
import json
import os
from pathlib import Path
import requests
import time
import base64
from io import BytesIO
from pdf2image import convert_from_bytes

class CityworksAuthenticationError(Exception):
    def __init__(self) -> None:
        pass

    def __str__(self) -> str:
        return f'You are not authenticated. You must authenticate before sending a request.'

class CityworksResponse:
    '''
    A class to represent a response object from Cityworks

    ---
    Attributes
    ----------
    requestResponse (requests.Response): The originally provided response object from requests module
    value (dict): The return value from the Cityworks API method. This will be null when an error occurs.
    status (int): The return status of the method. Corresponds to error codes listed in response messages: /apidocs/#/return-messages
    errorMessages (list): An array of messages that contain error details/codes.
    warningMessages (list): An array of messages that contain warning details/codes.
    sucessMessages (list): An array of messages that contain warning details/codes.

    ---
    Methods
    -------

    '''

    def __init__(self, r) -> None:
        '''
        Construct the response object.

        params
        ------
        r (requests.Response): A response object as it was returned from requests module
        '''
        self.requestUrl = r.url
        self.requestResponse = r
        try:
            response = r.json() #convert response json to dict
            self.value = response['Value']
            self.status = response['Status']
            self.errorMessages = response['ErrorMessages']
            self.warningMessages = response['WarningMessages']
            self.successMessages = response['SuccessMessages']
        except json.JSONDecodeError as e:
            errorMessage = f'Error in json decode of {self.requestUrl}\n{e}'
            self.errorMessages = errorMessage
            print(errorMessage)

    def __str__(self) -> str:
        '''
        Print method override
        '''
        return json.dumps(self.requestResponse.json(), indent=4) #pretty print the response
        
class CityworksRequest:
    '''
    A class to easily query Cityworks API endpoints. Relies on requests module. A query will return a CityworksReponse object. 

    ---
    Class Attributes
    ----------
    baseUrls(dict): Current Cityworks environments. Full urls are built from this base
    authEndpoint(str): Service responsible for authentication. Authenticate uses this endpoint.

    ---
    Instance Attributes
    ----------
    requestEnv(str): Environment is either dev, test, or prod. This will determine which environment to query
    serviceCategory(str): Corresponds to the category listed in Cityworks API docs. Will be Ams, General, Gis, or Pll
    service(str): Corresponds to the service to query. These are listed under the categories in the CW api docs
    endpoint(str): The method to query
    fullUrl(str): the full url that will be queried
    token(str): The token used to authenticate the request

    ---
    Methods
    -------
    authenticate(username, password): Retrieves a token from the environment's authentication service. Not necessary if token is provided to constructor. Will overwrite init token.
    validateToken(token): Asks Cityworks API whether optionally provided token or instance's token is valid. Potentially buggy.
    requestGet(params): Makes a get request to the service
    requestPost(params): Makes a post request to the service

    '''

    baseUrls = {
        'dev': 'http://cmmsappdev/cityworks_dev/services' ,
        'test': 'http://cmmsappdev/cityworks_test/services',
        'prod': 'https://cmms.cityofmadison.com/Cityworks/services'
    }
    authEndpoint = '/General/Authentication/'
    authAuthenticate = f'{authEndpoint}Authenticate'
    authValidate =  f'{authEndpoint}Validate'

    def __init__(self, requestEnv: str, serviceCategory: str=None, service: str=None, endpoint: str=None, token: str=None) -> None:
        '''
        Construct the  request object

        params
        ------
        requestEnv(str): Environment is either dev, test, or prod. This will determine which environment to query
        serviceCategory(str): Corresponds to the category listed in Cityworks API docs. Will be Ams, General, Gis, or Pll
        service(str): Corresponds to the service to query. These are listed under the categories in the CW api docs
        endpoint(str): The method to query
        token(str): Optional. Include if you already have a token and do not wish to authenticate.
        timeDelay(int): Seconds to wait between API calls
        _previousRequestTimestamp(float): time the last request was made to the API. Delay is calculated from timestamp.
        '''
        self.requestEnv = requestEnv
        self.serviceCategory = serviceCategory
        self.service = service
        self.endpoint = endpoint
        self.token = token
        self.timeDelay = 5
        self._previousRequestTimestamp = time.time()

    @property
    def fullUrl(self) -> str:
        '''
        getter for full url of the current service

        params
        ------
        None

        returns
        -------
        (str): the full url of the request object
        '''
        #make sure the url is up to date before returning
        self.fullUrl = (self.serviceCategory, self.service, self.endpoint)
        return self._fullUrl

    @fullUrl.setter
    def fullUrl(self, value: tuple[str, str, str]) -> None:
        '''
        setter for full url of the service to query

        params
        ------
        value(tuple[str,str,str]): tuple of three strings, each corresponding to portion of url. Should be passed as serviceCategory, service, and endpoint.

        returns
        -------
        None
        '''
        #unpack tuple strings into instance variables corresponding to each portion
        #at this time, setter only accepts one value, thus the reason for accepting a tuple vs strings
        self.serviceCategory, self.service, self.endpoint = value

        #set private url variable
        self._fullUrl = f'{self.baseUrls[self.requestEnv]}/{self.serviceCategory}/{self.service}/{self.endpoint}'

    def promptCredentials(self) -> bool:
        '''
        Prompt user for credentials

        params
        ------
        None

        returns
        -------
        (bool): Whether entered credentials sucessfully authenticated
        '''
        # ask user for credentials
        u = f'CITY/{getpass.getuser()}'
        p = getpass.getpass()
        
        # pass to authenticate
        token = self.authenticate(u, p)
        # success?
        return self.token == token

    def authenticate(self, username: str, password: str) -> str:
        '''
        Authenticate this instance. Sets the token of instance to returned value from Cityworks API request. Each subsequent request will send authenticated token with the request.
        If other requests succeed but don't return data, authenticate to ensure token is up to date.
        Uses https://cmms.cityofmadison.com/Cityworks/apidocs/#/service-info/General/Authentication

        params
        ------
        username(str): Cityworks username, if using AD authentication, ensure username is prepended with domain (i.e. CITY/{username})
        password(str): Cityworks password

        returns
        -------
        (str): Cityworks service token or error message if authentication failed
        '''
        #convert arguments to dictionary with keys corresponding to Cityworks API specification
        params = {"LoginName": username, "Password": password}

        #print('url:')
        #print(self.baseUrls[self.requestEnv]+self.authAuthenticate)
        #print('data:')
        #print(self._formatParams(params))
        #send request
        r = requests.post(self.baseUrls[self.requestEnv]+self.authAuthenticate, data=self._formatParams(params))

        #convert requests return to a CityworksResponse object
        response = CityworksResponse(r)

        #If no value is returned, authentication was not successful
        if response.value is None:
            return response.errorMessages

        #if authentication was successful, the response will include the token value. Set instance variable equal to the token and return it to user
        self.token = response.value['Token']
        return self.token

    def validateToken(self, token: str=None) -> bool:
        '''
        Validate the provided token, or if no token is provided, validate the instance's set token. Won't check whether instance token exists before attempting to validate.
        Not sure of the accuracy of Cityworks API service. Have successfully validated tokens from dev in prod, but subsequent requests have failed that require authentication.
        Possible that Cityworks API does not work how I assume. Take results with a grain of salt and if other requests are failing, try to authenticate to reset the token.

        params
        ------
        token(str): Optional token from previous authentication with the service

        returns
        -------
        (boolean): whether token is valid
        '''
        #check whether token was provided, if it wasn't, check the instance's token
        if token is None:
            token = self.token

        #format arguments and send request to API
        params = {'Token': token}
        r = requests.post(self.baseUrls[self.requestEnv]+self.authValidate, data=self._formatParams(params))

        #convert response
        response = CityworksResponse(r)

        #if the API returns True or False
        return response.value

    def requestGet(self, params: dict) -> CityworksResponse:
        '''
        Send a get request to the API

        params
        ------
        params(dict): dictionary of request parameters as defined in Cityworks API documentation. The key must be named exactly as in service documentation.

        returns
        -------
        (CityworksResponse): Response object containing return from request
        '''
        self._isAuthenticated() # raises CityworksAuthenticationError if no token is set
        pstr = self._formatParams(params) # format parameters for request
        self._delayRequest() # enforce delay between requests
        r = requests.get(self.fullUrl, params=pstr) # send request to API
        self._previousRequestTimestamp = time.time() # update the timestamp of the last request
        return CityworksResponse(r)

    def requestPost(self, params: dict) -> CityworksResponse:
        '''
        Send a post request to the API

        params
        ------
        params(dict): dictionary of request parameters as defined in Cityworks API documentation. The key must be named exactly as in service documentation.

        returns
        -------
        (CityworksResponse): Response object containing return from request
        '''
        self._isAuthenticated()
        payload = self._formatParams(params)
        self._delayRequest()
        r = requests.post(self.fullUrl, data=payload)
        self._previousRequestTimestamp = time.time()
        return CityworksResponse(r)

    def _formatParams(self, params) -> dict:
        '''
        Formats dictionary as string for usage in request

        params
        ------
        params(dict): dictionary of request parameters

        returns
        -------
        (dict): dictionary with parameters formatted as string and assigned to data key, token is assigned to token key
        '''
        return {
            'data': {json.dumps(params)}, #use json dumps to convert python dict to json string
            'Token': self.token
        }

    def _isAuthenticated(self) -> None:
        '''
        Check if instance has a token assigned. Throw error if none is assigned.
        Note this doesn't validate the token, just checks if one exists. It is possible to provide a token that is not valid and still pass this authentication check.
        
        Could update to check for a validated token or store additional variable that checks whether a token has been validated or simply provided.

        params
        ------
        None

        returns
        -------
        None

        Raises
        ------
        CityworksAuthenticationError
        '''
        if self.token is None:
            raise CityworksAuthenticationError()

    def _delayRequest(self) -> None:
        '''
        Enforce delay between calls so that the API does not become overloaded.
        Research did not yield an answer as to what would be considered a reasonable delay.

        params
        ------
        None

        returns
        -------
        None
        '''
        timepassed = time.time() - self._previousRequestTimestamp
        if timepassed < 5:
            time.sleep(self.timeDelay - timepassed)
    
    def download_attachment(self, attachment_id, download=False, save_path=None, filename=None):
        """
        Download a work order's specific attachment by attachment ID

        params
        ------
        attachment_id(int): ID for the attachment. Can be found via query: SELECT ATTACHEDBYSID FROM azteca.WORKORDERIMG WHERE WORKORDERID = '{work_order_id}' or WORKORDERSID = {work_order_id}
        
        returns
        -------
        None
        """
        
        self._isAuthenticated() # raises CityworksAuthenticationError if no token is set
        params = {'AttachmentID':attachment_id}
        pstr = self._formatParams(params) # format parameters for request
        self._delayRequest() # enforce delay between requests
        r = requests.get(self.fullUrl, params=pstr) # send request to API
        if r.ok:
            # Update the timestamp of the last request
            self._previousRequestTimestamp = time.time()

            # Default save_path to user's download folder
            if save_path == None:
                save_path = str(Path.home() / "Downloads")

            # Try to get filename from Content-Disposition header if not provided
            if filename == None:
                content_disposition = r.headers.get('Content-Disposition', '')
                if 'filename=' in content_disposition:
                    filename = content_disposition.split('filename=')[1].strip('"')
                else:
                    # Fallback filename - try to get extension from Content-Type
                    content_type = r.headers.get('Content-Type', '')
                    ext = ''
                    if 'pdf' in content_type:
                        ext = '.pdf'
                    elif 'image' in content_type:
                        ext = '.jpg'
                    filename = f"attachment_{attachment_id}{ext}"
            
            
            if download:
                full_path = os.path.join(save_path, filename)
                
                # Combine path and filename
                print(save_path, filename)

                # Begin download
                with open(full_path, 'wb') as f:
                    f.write(r.content)
                
                return f"Attachment saved to {full_path}"
            else:
                if isinstance(r.content, bytes):
                    base64_images = self._pdf_to_base64_images(r.content)
                    return base64_images
                else:
                    raise Exception(f"Download failed")
            
        else:
            raise Exception(f"Download failed: {r.status_code}")
        


    def _pdf_to_base64_images(self, pdf_content):
        """
        Convert PDF bytes to a list of base64-encoded images
        
        params
        ------
        pdf_content (bytes): The PDF file content
        
        returns
        -------
        list: List of base64-encoded image strings (one per page)
        """
        # Convert PDF to images (one per page)
        images = convert_from_bytes(pdf_content)
        
        base64_images = []
        for image in images:
            # Convert PIL Image to bytes
            buffered = BytesIO()
            image.save(buffered, format="PNG")
            img_bytes = buffered.getvalue()
            
            # Convert to base64
            img_base64 = base64.b64encode(img_bytes).decode('utf-8')
            base64_images.append(img_base64)
        
        return base64_images
