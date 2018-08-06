#!/usr/bin/env python3
import os
import json
import urllib.parse
from datetime import datetime, timedelta
import requests
from oauthlib.oauth2 import BackendApplicationClient, TokenExpiredError
from requests_oauthlib import OAuth2Session
import singer
import singer.metrics as metrics
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
    streams = []

    for schema_name, schema in raw_schemas.items():
        metadata = singer.metadata.new()
        metadata = singer.metadata.write(
            compiled_metadata=metadata,
            breadcrumb=(),
            k='selected',
            val=True)
        for key in schema['properties'].keys():
            metadata = singer.metadata.write(
                compiled_metadata=metadata,
                breadcrumb=('properties', key),
                k='inclusion',
                val='automatic')

        # create and add catalog entry
        catalog_entry = {
            'stream': schema_name,
            'tap_stream_id': schema_name,
            'schema': schema,
            'metadata': singer.metadata.to_list(metadata),
            'key_properties': [],
        }
        streams.append(catalog_entry)

    return {'streams': streams}

def stream_is_selected(metadata):
    return metadata.get((), {}).get('selected', False)

def get_selected_streams(catalog):
    selected_stream_names = []
    for stream in catalog.streams:
        metadata = singer.metadata.to_map(stream.metadata)
        if stream_is_selected(metadata):
            selected_stream_names.append(stream.tap_stream_id)
    return selected_stream_names

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
        LOGGER.info("Making a request to '%s' using params: %s", url, params)
        try:
            return self.session.get(url, params=params)
        except TokenExpiredError:
            self.get_access_token()
            return self.session.get(url, params=params)

class Stream():
    '''
    Stores schema data and a buffer of records for a particular stream.
    When a Stream's buffer reaches its `buffer_size`, it returns a
    boolean that the buffer should be emptied. Emptying the buffer resets the
    buffer and returns a generator object with all records in the buffer.
    '''
    buffer_limit = 100

    def __init__(self, stream):
        self.stream_name = stream.tap_stream_id
        self.schema = stream.schema.to_dict()
        self.key_properties = stream.key_properties
        self.counter = metrics.record_counter(self.stream_name)
        self.buffer = []

    def add_to_buffer(self, record):
        self.buffer.append(record)
        return bool(len(self.buffer) >= self.buffer_limit)

    def empty_buffer(self):
        for record in self.buffer:
            yield record
            self.counter.increment()
        self.buffer = []

    def write_schema(self):
        singer.write_schema(
            self.stream_name,
            self.schema,
            self.key_properties)

def request(client, start_date, end_date, fields='all'):
    url = urllib.parse.urljoin(BASE_URL, ENDPOINTS['transactions'])
    params = {
        'start_date': start_date.astimezone().isoformat('T'),
        'end_date': end_date.astimezone().isoformat('T'),
        'fields': ','.join(fields) if isinstance(fields, list) else fields}

    LOGGER.info(
        'Retrieving transactions between %s and %s.',
        start_date,
        end_date)
    while True:
        response = client.request(url, params=params)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            LOGGER.critical(response.json()['details'])
            raise
        else:
            response_json = response.json()
            LOGGER.info(
                'Completed retrieving all transactions on page %s of %s.',
                response_json['page'],
                response_json['total_pages'])
        chunk = response_json['transaction_details']
        yield chunk
        try:
            url = next(
                link['href'] for link in response_json['links']
                if link['rel'] == 'next')
            params = {}
        except StopIteration:
            break

def get_transactions(client, start_date, end_date, fields='all'):
    '''
    Divides the date range into segments no longer than MAX_DAYS_BETWEEN
    and iterates through them to request transaction chunks and process them.
    '''

    chunksize = timedelta(days=MAX_DAYS_BETWEEN)
    while start_date + chunksize < end_date:
        chunk_end_date = start_date + chunksize
        for chunk in request(client, start_date, chunk_end_date, fields):
            process_chunk(chunk)
        start_date = chunk_end_date + timedelta(days=1)
    for chunk in request(client, start_date, end_date, fields):
        process_chunk(chunk)

    empty_all_buffers()

def process_chunk(chunk):
    '''
    Sorts transaction data into the appropriate stream buffers, flushing the
    buffers whenever they become full.
    '''
    for transaction in chunk:
        id_ = transaction['transaction_info']['transaction_id']
        for stream_name, record in transaction.items():
            stream = STREAMS[stream_name]
            if record:
                # Save ID to all streams so we have a common key
                record['transaction_id'] = id_
                buffer_is_full = stream.add_to_buffer(record)
                if buffer_is_full:
                    for buf_record in stream.empty_buffer():
                        transformed = singer.transform(buf_record, stream.schema)
                        singer.write_record(stream.stream_name, transformed)
            else:
                continue

def empty_all_buffers():
    for stream in STREAMS.values():
        for buf_record in stream.empty_buffer():
            transformed = singer.transform(buf_record, stream.schema)
            singer.write_record(stream.stream_name, transformed)
    for stream in STREAMS.values():
        LOGGER.info(
            "Completed sync of %s total records to stream '%s'.",
            stream.counter.value,
            stream.stream_name)

def sync(config, state, catalog):
    client = PayPalClient(config)
    selected_stream_names = get_selected_streams(catalog)
    for catalog_stream in catalog.streams:
        stream_name = catalog_stream.tap_stream_id
        if stream_name in selected_stream_names:
            stream = Stream(catalog_stream)
            stream.write_schema()
            STREAMS[stream_name] = stream

    get_transactions(
        client=client,
        start_date=datetime(2018, 7, 15),
        end_date=datetime(2018, 7, 16))

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
