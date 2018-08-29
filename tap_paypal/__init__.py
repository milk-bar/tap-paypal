import os
import json
from datetime import datetime
import pytz
import requests
import dateutil
import singer
from singer import utils
from clients import TransactionClient, InvoiceClient
from stream import BatchWriter, StreamBuffer, BUFFERS

REQUIRED_CONFIG_KEYS = ['client_id', 'client_secret']
CLIENTS = {
    'transactions': TransactionClient,
    'invocies': InvoiceClient
}
EXTRA_ARGS = {
    'transactions': {'fields': 'all'},
    'invoices': {}
}
REPLICATION_KEYS = {
    'transactions': 'transaction_updated_date',
    'invoices': 'created_date'
}
LOGGER = singer.get_logger()

def stream_is_selected(metadata):
    '''Checks the metadata and returns if a stream is selected or not.'''
    return metadata.get((), {}).get('selected', False)

def get_abs_path(path):
    '''Returns absolute file path.'''
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_json(path):
    '''Load a JSON file as a dictionary.'''
    with open(path, 'r') as file:
        json_ = json.load(file)
    return json_

def load_schema(schema_name):
    '''Load a single schema in ./schemas by stream name as a dictionary.'''
    path = os.path.join(get_abs_path('schemas'), schema_name + '.json')
    schema = load_json(path)
    return schema

def load_all_schemas():
    '''Load each schema in ./schemas into a dictionary with stream name keys.'''
    schemas = {}
    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        schemas[file_raw] = load_json(path)
    return schemas

def discover():
    '''
    Generates the catalog dictionary containing all metadata and
    automatically selects all fields to be included.
    '''
    raw_schemas = load_all_schemas()
    entries = []
    key_properties = {
        'invoices': 'id',
        'transactions': 'transaction_id'}
    bookmark_properties = {
        'invoices': 'created_date',
        'transactions': 'transaction_updated_date'}

    for schema_name, schema in raw_schemas.items():
        top_level_metadata = {
            'inclusion': 'available',
            'selected': True,
            'forced-replication-method': 'INCREMENTAL',
            'valid-replication-keys': bookmark_properties[schema_name]}

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
            'key_properties': key_properties[schema_name],
            'bookmark_properties': bookmark_properties[schema_name]
        }
        entries.append(catalog_entry)

    return {'streams': entries}

def get_selected_streams(catalog):
    '''Using the catalog, returns a list of selected stream names.'''
    selected_stream_names = []
    for stream in catalog.streams:
        metadata = singer.metadata.to_map(stream.metadata)
        if stream_is_selected(metadata):
            selected_stream_names.append(stream.tap_stream_id)
    return selected_stream_names

def write_batch(batch, state, stream, replication_key):
    stream_name = stream.tap_stream_id
    for record in batch:
        transformed = singer.transform(record, stream.schema)
        singer.write_record(stream_name, transformed)
        bookmark = dateutil.parser \
            .parse(record[replication_key])

    state = singer.write_bookmark(
        state=state,
        tap_stream_id=stream_name,
        key=replication_key,
        val=bookmark.isoformat('T'))

    singer.write_state(state)

def sync(config, state, catalog):
    selected_stream_names = get_selected_streams(catalog)
    for stream in catalog.streams:
        stream_name = stream.tap_stream_id
        if stream_name in selected_stream_names:
            replication_key = REPLICATION_KEYS[stream_name]
            bookmark = singer.get_bookmark(
                state=state,
                tap_stream_id=stream_name,
                key=replication_key)
            client = CLIENTS[stream_name](config)
            singer.write_schema(
                stream.stream_name,
                stream.schema,
                stream.key_properties)
            batches = client.get_records(start_date=bookmark, **EXTRA_ARGS[stream_name])
            for batch in batches:
                write_batch(batch, state, stream, replication_key)

@utils.handle_top_exception(LOGGER)
def main():
    '''Based on command line arguments, chooses discover or sync.'''
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    if args.discover:
        catalog = discover()
        print(json.dumps(catalog, indent=2))
    elif args.catalog:
        catalog = args.catalog
        sync(args.config, args.state, catalog)

if __name__ == "__main__":
    main()
