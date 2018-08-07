#!/usr/bin/env python3
import os
import json
import urllib.parse
from datetime import datetime, timedelta
import dateutil.parser
import requests
from oauthlib.oauth2 import BackendApplicationClient, TokenExpiredError
from requests_oauthlib import OAuth2Session
import singer
from singer import metrics
from singer import utils

REQUIRED_CONFIG_KEYS = ['client_id', 'client_secret']
LOGGER = singer.get_logger()
MAX_DAYS_BETWEEN = 31
BASE_URL = 'https://api.paypal.com'
ENDPOINTS = {
    'transactions': 'v1/reporting/transactions',
    'token': 'v1/oauth2/token'}
STREAMS = {}

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_json(path):
    with open(path, 'r') as file:
        json_ = json.load(file)
    return json_

def load_all_schemas():
    schemas = {}
    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        schemas[file_raw] = load_json(path)
    return schemas

def load_schema(schema_name):
    path = os.path.join(get_abs_path('schemas'), schema_name + '.json')
    schema = load_json(path)
    return schema

def discover():
    raw_schemas = load_all_schemas()
    entries = []

    for schema_name, schema in raw_schemas.items():
        top_level_metadata = {
            'inclusion': 'available',
            'selected': True,
            'forced-replication-method': 'INCREMENTAL',
            'valid-replication_keys': ['transaction_updated_date'],
            'selected-by-default': True}

        metadata = singer.metadata.new()
        for key, val in top_level_metadata.items():
            metadata = singer.metadata.write(
                compiled_metadata=metadata,
                breadcrumb=(),
                k=key,
                val=val)

        for key in schema['properties'].keys():
            metadata = singer.metadata.write(
                compiled_metadata=metadata,
                breadcrumb=('properties', key),
                k='inclusion',
                val='automatic')

        catalog_entry = {
            'stream': schema_name,
            'tap_stream_id': schema_name,
            'schema': schema,
            'metadata': singer.metadata.to_list(metadata),
            'key_properties': ['transaction_id'],
            'bookmark_properties': ['transaction_updated_date']
        }
        entries.append(catalog_entry)

    return {'streams': entries}

def stream_is_selected(metadata):
    return metadata.get((), {}).get('selected', False)

def get_selected_streams(catalog):
    selected_stream_names = []
    for stream in catalog.streams:
        metadata = singer.metadata.to_map(stream.metadata)
        if stream_is_selected(metadata):
            selected_stream_names.append(stream.tap_stream_id)
    return selected_stream_names

def strip_query_string(url):
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    parsed = parsed._replace(query='')
    url = parsed.geturl()
    return url, params

class PayPalClient():
    def __init__(self, config):
        self.config = config
        oath_client = BackendApplicationClient(
            client_id=self.config['client_id'])
        self.session = OAuth2Session(client=oath_client)
        self.get_access_token()

    def get_access_token(self):
        url = urllib.parse.urljoin(BASE_URL, ENDPOINTS['token'])
        self.session.fetch_token(
            token_url=url,
            client_id=self.config['client_id'],
            client_secret=self.config['client_secret'])

    def request(self, url, params):
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
        except requests.exceptions.HTTPError as error:
            message = "Request returned code {} with the following details: {}" \
                .format(response.status_code, response.json()['details'])
            raise type(error)(message) from error
        else:
            return response.json()

class Stream():
    '''
    Stores schema data and a buffer of records for a particular stream.
    When a Stream's buffer reaches its `buffer_size`, it returns a
    boolean that the buffer should be emptied. Emptying the buffer resets the
    buffer and writes all records to stdout.

    Also tracks a bookmark for each record written out of the buffer.
    '''
    buffer_limit = 100

    def __init__(self, stream, bookmark=None):
        self.stream = stream
        self.stream_name = stream.tap_stream_id
        self.schema = stream.schema.to_dict()
        self.key_properties = stream.key_properties
        self.counter = metrics.record_counter(self.stream_name)
        self.buffer = []
        self.bookmark = bookmark

    def add_to_buffer(self, record):
        self.buffer.append(record)
        return bool(len(self.buffer) >= self.buffer_limit)

    def empty_buffer(self, state):
        for record in self.buffer:
            updated_date = dateutil.parser.parse(
                record['transaction_updated_date'])
            if updated_date >= self.bookmark:
                transformed = singer.transform(record, self.schema)
                singer.write_record(self.stream_name, transformed)
                self.bookmark = updated_date
                self.counter.increment()
        self.buffer = []
        self.save_state(state)

    def save_state(self, state):
        state = singer.write_bookmark(
            state=state,
            tap_stream_id=self.stream_name,
            key='transaction_updated_date',
            val=self.bookmark.isoformat('T'))
        singer.write_state(state)

    def write_schema(self):
        singer.write_schema(
            self.stream_name,
            self.schema,
            self.key_properties)

def truncate_date(timestamp):
    '''Truncates a datetime object to only its date components.'''
    return timestamp.replace(hour=0, minute=0, second=0, microsecond=0)

def request(client, start_date, end_date, fields='all'):
    '''
    Makes a request to the API, retrieving transactions in chunks of 100 and
    yielding a generator of those 100-record chunks. Handles any pagination
    automatically using the `next` field returned in the response.
    '''

    url = urllib.parse.urljoin(BASE_URL, ENDPOINTS['transactions'])
    params = {
        'start_date': start_date.astimezone().isoformat('T'),
        'end_date': truncate_date(end_date.astimezone()).isoformat('T'),
        'fields': ','.join(fields) if isinstance(fields, list) else fields}

    LOGGER.info(
        'Retrieving transactions between %s and %s.',
        start_date.strftime('%Y-%m-%d'),
        end_date.strftime('%Y-%m-%d'))
    while True:
        response = client.request(url, params=params)
        LOGGER.info(
            'Completed retrieving all transactions on page %s of %s.',
            response['page'],
            response['total_pages'])
        chunk = response['transaction_details']
        yield chunk
        try:
            url = next(
                link['href'] for link in response['links']
                if link['rel'] == 'next')
            params = {}
        except StopIteration:
            break

def get_transactions(client, state, start_date=None, end_date=None, fields='all'):
    '''
    Divides the date range into segments no longer than MAX_DAYS_BETWEEN
    and iterates through them to request transaction chunks and process them.
    '''

    if start_date is None:
        start_date = datetime(2016, 7, 1).astimezone() # PayPal's oldest data is July 2016
    if end_date is None:
        end_date = datetime.utcnow().astimezone() - timedelta(days=1)

    chunksize = timedelta(days=MAX_DAYS_BETWEEN)
    while start_date + chunksize < end_date:
        chunk_end_date = start_date + chunksize
        for chunk in request(client, start_date, chunk_end_date, fields):
            process_chunk(chunk, state)
        start_date = chunk_end_date + timedelta(days=1)
    for chunk in request(client, start_date, end_date, fields):
        process_chunk(chunk, state)
    empty_all_buffers(state)

def write_to_stream(stream_name, record, state):
    stream = STREAMS[stream_name]
    buffer_is_full = stream.add_to_buffer(record)
    if buffer_is_full:
        stream.empty_buffer(state)

def process_chunk(chunk, state):
    '''
    Sorts transaction data into the appropriate stream buffers, emptying the
    buffers whenever they become full.
    '''

    for transaction in chunk:
        updated_date = transaction['transaction_info']['transaction_updated_date']
        id_ = transaction['transaction_info']['transaction_id']
        for stream_name, record in transaction.items():
            if record:
                record['transaction_id'] = id_
                record['transaction_updated_date'] = updated_date
                write_to_stream(stream_name, record, state)

def empty_all_buffers(state):
    for stream in STREAMS.values():
        stream.empty_buffer(state)
    for stream in STREAMS.values():
        LOGGER.info(
            "Completed sync of %s records to stream '%s'.",
            stream.counter.value,
            stream.stream_name)

def build_stream(catalog_stream, state):
    bookmark = singer.get_bookmark(
        state=state,
        tap_stream_id=catalog_stream.tap_stream_id,
        key='transaction_updated_date')
    bookmark = dateutil.parser.parse(bookmark)
    stream = Stream(catalog_stream, bookmark)
    STREAMS[catalog_stream.tap_stream_id] = stream

def sync(config, state, catalog):
    client = PayPalClient(config)
    selected_stream_names = get_selected_streams(catalog)
    for catalog_stream in catalog.streams:
        if catalog_stream.tap_stream_id in selected_stream_names:
            build_stream(catalog_stream, state)
            STREAMS[catalog_stream.tap_stream_id].write_schema()

    oldest_updated_date = min([stream.bookmark for stream in STREAMS.values()])
    LOGGER.info(oldest_updated_date)
    get_transactions(
        client=client,
        state=state,
        start_date=oldest_updated_date,
        fields=selected_stream_names)

@utils.handle_top_exception(LOGGER)
def main():
    # Parse command line arguments
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    # If discover flag was passed, run discovery mode and dump output to stdout
    if args.discover:
        catalog = discover()
        print(json.dumps(catalog, indent=2))
    # Otherwise run in sync mode
    else:
        # 'properties' is the legacy name of the catalog
        if args.properties:
            catalog = args.properties
        # 'catalog' is the current name
        elif args.catalog:
            catalog = args.catalog
            sync(args.config, args.state, catalog)

if __name__ == "__main__":
    main()
