import os
import json
from datetime import datetime
import requests
import dateutil
import pytz
import singer
from singer import utils, metrics
from clients import TransactionClient, InvoiceClient

REQUIRED_CONFIG_KEYS = ['client_id', 'client_secret']
CLIENTS = {
    'transactions': TransactionClient,
    'invoices': InvoiceClient}
EXTRA_ARGS = {
    'transactions': {'fields': 'all'},
    'invoices': {}}
REPLICATION_KEYS = {
    'transactions': ['transaction_info', 'transaction_updated_date'],
    'invoices': ['metadata', 'created_date']}
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
    forced_replication_method = {
        'invoices': 'FULL_TABLE',
        'transactions': 'INCREMENTAL'
    }

    for schema_name, schema in raw_schemas.items():
        top_level_metadata = {
            'inclusion': 'available',
            'selected': True,
            'forced-replication-method': forced_replication_method[schema_name],
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

def get_replication_value(record, keys):
    '''Recursively navigate a record by a list of keys and return a value.'''
    copied_keys = keys.copy()
    first_key = copied_keys.pop(0)
    value = record[first_key]
    if copied_keys:
        value = get_replication_value(value, copied_keys)
        return value
    else:
        return value

def write_record(record, state, stream, replication_keys):
    stream_name = stream.tap_stream_id
    transformed = singer.transform(record, stream.schema.to_dict())
    singer.write_record(stream_name, transformed)
    bookmark = get_replication_value(record, replication_keys)
    state = singer.write_bookmark(
        state=state,
        tap_stream_id=stream_name,
        key=replication_keys[-1],
        val=bookmark)
    singer.write_state(state)

def sync(config, state, catalog):
    selected_stream_names = get_selected_streams(catalog)
    for stream in catalog.streams:
        stream_name = stream.tap_stream_id
        if stream_name in selected_stream_names:
            replication_keys = REPLICATION_KEYS[stream_name]
            bookmark = singer.get_bookmark(
                state=state,
                tap_stream_id=stream_name,
                key=replication_keys[-1])

            if bookmark:
                if stream_name == 'invoices':
                    tzinfos = {'PST': -8 * 3600, 'PDT': -7 * 3600}
                    start_date = dateutil.parser.parse(bookmark, tzinfos=tzinfos) + \
                        dateutil.relativedelta.relativedelta(years=-1)
                else:
                    start_date = dateutil.parser.parse(bookmark)
            else:
                if stream_name == 'transactions':
                    start_date = datetime(2016, 7, 1, tzinfo=pytz.utc)
                elif stream_name == 'invoices':
                    start_date = None
                else:
                    message = 'No state file or default start date provided for stream %s.'
                    LOGGER.critical(message, stream_name)

            client = CLIENTS[stream_name](config)
            LOGGER.info("Beginning sync of stream '%s'...", stream_name)
            singer.write_schema(
                stream_name,
                stream.schema.to_dict(),
                stream.key_properties)
            records = client.get_records(start_date=start_date, **EXTRA_ARGS[stream_name])
            with metrics.record_counter(stream_name) as counter:
                for record in records:
                    write_record(record, state, stream, replication_keys)
                    counter.increment()
            LOGGER.info("Finished syncing stream '%s'.", stream_name)

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
