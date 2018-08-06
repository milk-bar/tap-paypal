#!/usr/bin/env python3
import os
import json
import urllib.parse
from datetime import datetime, timedelta
import requests
from oauthlib.oauth2 import BackendApplicationClient, TokenExpiredError
from requests_oauthlib import OAuth2Session
import singer
from singer import utils

REQUIRED_CONFIG_KEYS = ['client_id', 'client_secret']
LOGGER = singer.get_logger()
MAX_DAYS_BETWEEN = 31
BASE_URL = 'https://api.paypal.com'
ENDPOINTS = {
    'transactions': 'v1/reporting/transactions',
    'token': 'v1/oauth2/token'}

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

# Load schemas from schemas folder
def load_schemas():
    schemas = {}

    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        with open(path) as file:
            schemas[file_raw] = json.load(file)

    return schemas

def discover():
    raw_schemas = load_schemas()
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
    # TODO: Store access token in config.json and only
    # request a new token if expired
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
        try:
            return self.session.get(url, params=params)
        except TokenExpiredError:
            self.get_access_token()
            return self.session.get(url, params=params)

# TODO: Adding exponential backoff, handling for 429s here
def request(client, start_date, end_date, fields='all'):
    url = urllib.parse.urljoin(BASE_URL, ENDPOINTS['transactions'])
    params = {
        'start_date': start_date.astimezone().isoformat('T'),
        'end_date': end_date.astimezone().isoformat('T'),
        'fields': ','.join(fields) if isinstance(fields, list) else fields}

    LOGGER.info(
        'Retrieving transactions between %(start_date)s and %(end_date)s.',
        extra={'start_date': start_date, 'end_date': end_date})
    while True:
        response = client.request(url, params=params)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            LOGGER.critical(response.json()['details'])
            raise
        else:
            response_json = response.json()
        for transaction in response_json['transaction_details']:
            yield transaction
        try:
            url = next(
                link['href'] for link in response_json['links']
                if link['rel'] == 'next')
            params = {}
        except StopIteration:
            break

def get_transactions(client, start_date, end_date, fields='all'):
    chunksize = timedelta(days=MAX_DAYS_BETWEEN)
    while start_date + chunksize < end_date:
        chunk_end_date = start_date + chunksize
        chunk = request(client, start_date, chunk_end_date, fields)
        for transaction in chunk:
            yield transaction
        start_date = chunk_end_date + timedelta(days=1)
    else:
        chunk = request(client, start_date, end_date, fields)
        for transaction in chunk:
            yield transaction

def sync(config, state, catalog):
    client = PayPalClient(config)
    selected_stream_names = get_selected_streams(catalog)

    # Loop over streams in catalog
    for stream in catalog.streams:
        stream_name = stream.tap_stream_id
        if stream_name in selected_stream_names:
            singer.write_schema(
                stream.tap_stream_id,
                stream.schema.to_dict(),
                stream.key_properties)
            # TODO: Need to make these dates dynamic based on state file
            transactions = get_transactions(
                client=client,
                start_date=datetime(2018, 6, 15),
                end_date=datetime(2018, 8, 1))
            for transaction in transactions:
                record = singer.transform(
                    transaction['transaction_info'],
                    stream.schema.to_dict())
                singer.write_record('transactions', record)

            # TODO: Need to store final spot to state

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
