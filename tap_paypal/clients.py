import json
import re
from datetime import datetime
import urllib.parse
import pytz
import dateutil
from dateutil.relativedelta import relativedelta
from requests.exceptions import HTTPError
from oauthlib.oauth2 import BackendApplicationClient, TokenExpiredError
from requests_oauthlib import OAuth2Session
import singer
import requests
import backoff

LOGGER = singer.get_logger()
BASE_URL = 'https://api.paypal.com'
ENDPOINTS = {
    'transactions': 'v1/reporting/transactions',
    'invoices': 'v1/invoicing/invoices',
    'token': 'v1/oauth2/token'}

def strip_query_string(url):
    '''Remove the query string from a URL and return it as a dictionary of params.'''
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    parsed = parsed._replace(query='')
    url = parsed.geturl()
    return url, params

class PayPalClient:
    '''Authenticates and makes requests to a PayPal API.'''
    records_key = None
    endpoint = None

    def __init__(self, config):
        self.config = config
        oath_client = BackendApplicationClient(
            client_id=self.config['client_id'])
        self.session = OAuth2Session(client=oath_client)
        self.get_access_token()

    def get_access_token(self):
        '''Using stored credentials, gets an access token from the token API.'''
        url = urllib.parse.urljoin(BASE_URL, ENDPOINTS['token'])
        self.session.fetch_token(
            token_url=url,
            client_id=self.config['client_id'],
            client_secret=self.config['client_secret'])
            
    @backoff.on_exception(
        backoff.expo,
        (requests.exceptions.RequestException),
        max_tries=5)
    def make_request(self, url, params=None):
        '''Makes a GET request to the API and handles logging for any errors.'''
        if not params:
            params = {}
        url, addl_params = strip_query_string(url)
        params.update(addl_params)
        LOGGER.info("Making a request to '%s' using params: %s", url, params)
        try:
            response = self.session.get(url, params=params)
        except TokenExpiredError:
            self.get_access_token()
            response = self.session.get(url, params=params)
        try:
            response.raise_for_status()
        except HTTPError as error:
            message = "Request returned code {} with the following details: {}" \
                .format(response.status_code, response.json())
            DynamicExceptionClass = type(error)
            raise DynamicExceptionClass(message) from error
        else:
            return response.json()

    def paginate(self, **kwargs):
        '''
        Makes a request to the API, retrieving transactions in chunks of 100
        and handling any pagination automatically using the `next` field
        returned in the response. Returns a generator that yields 100-item
        batches.
        '''
        url = '/'.join([BASE_URL, self.endpoint])
        params = kwargs
        params['page_size'] = 100
        while True:
            response = self.make_request(url, params=params)
            batch = response[self.records_key]
            yield batch
            try:
                url = next(
                    link['href'] for link in response['links']
                    if link['rel'] == 'next')
                params = {}
            except StopIteration:
                break

class TransactionClient(PayPalClient):
    records_key = 'transaction_details'
    endpoint = ENDPOINTS['transactions']

    def get_records(self, start_date, fields='all'):
        end_date = datetime.utcnow() \
            .replace(microsecond=0, tzinfo=pytz.utc)
        delta = relativedelta(months=+1, seconds=-1)
        while start_date + delta < end_date:
            batch_end_date = start_date + delta
            batches = self.paginate(
                start_date=start_date.isoformat('T'),
                end_date=batch_end_date.isoformat('T'),
                fields=fields)
            for batch in batches:
                for transaction in batch:
                    yield transaction
            start_date = batch_end_date + relativedelta(seconds=+1)
        batches = self.paginate(
            start_date=start_date.isoformat('T'),
            end_date=end_date.isoformat('T'),
            fields=fields)
        for batch in batches:
            for transaction in batch:
                yield transaction

class InvoiceClient(PayPalClient):
    records_key = 'invoices'
    endpoint = ENDPOINTS['invoices']

    def paginate(self, **kwargs):
        '''
        Makes a request to the API, retrieving transactions in chunks of 100
        and handling any pagination automatically using the `page` and
        `page_size` fields. Returns a generator that yields 100-item batches.
        '''
        url = '/'.join([BASE_URL, self.endpoint])
        params = kwargs
        params['page'] = 0
        params['page_size'] = 100
        params['total_count_required'] = True

        while True:
            response = self.make_request(url, params=params)
            total_count = response['total_count']
            batch = response[self.records_key]
            params['page'] += params['page_size']
            if params['page'] <= total_count:
                yield batch
            else:
                break

    def get_invoice_details(self, invoice_id):
        url = '/'.join([BASE_URL, self.endpoint, invoice_id])
        response = self.make_request(url)
        try:
            del response['links']
        except KeyError:
            pass
        return response

    def get_records(self, start_date=None):
        for batch in self.paginate():
            for invoice in batch:
                record = self.get_invoice_details(invoice['id'])

                # Replace PDT with offset so it's readable by singer transformer/dateutil
                as_string = json.dumps(record)
                date_pattern = r'"(\d{4}-\d{2}-\d{2}) (?:PDT|PST)"'
                as_string = re.sub(date_pattern, r'"\1 00:00:00-7:00"', as_string)
                timestamp_pattern = r'"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:PDT|PST)"'
                as_string = re.sub(timestamp_pattern, r'"\1-7:00"', as_string)
                record = json.loads(as_string)

                created_date = dateutil.parser.parse(
                    record['metadata']['created_date'])

                if start_date is None:
                    yield record
                elif created_date >= start_date:
                    yield record
                else:
                    return
