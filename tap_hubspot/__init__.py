#!/usr/bin/env python3
import datetime
import pytz
import itertools
import os
import re
import sys
import json
import time

import attr
import backoff
import requests
import singer
import singer.messages
import singer.metrics as metrics
from singer import metadata
from singer import utils
from singer import (transform,
                    UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING,
                    Transformer, _transform_datetime)

LOGGER = singer.get_logger()
SESSION = requests.Session()
BACKOFF_SECONDS = 60
MAX_TRIES = 5

class InvalidAuthException(Exception):
    pass

class SourceUnavailableException(Exception):
    pass

class DependencyException(Exception):
    pass

class DataFields:
    offset = 'offset'

class StateFields:
    offset = 'offset'
    this_stream = 'this_stream'

BASE_URL = "https://api.hubapi.com"

CONTACTS_BY_COMPANY = "contacts_by_company"
FORM_SUBMISSIONS = "form_submissions"

# ['contacts', 'tickets', 'feedback_submissions']
CUSTOM_SCHEMA_WITHOUT_EXTRAS = ['contacts', 'tickets', 'feedback_submissions']

FORMS_TO_GET_SUBMISSIONS = {}

EMAIL_EVENT_TYPES = ['OPEN', 'CLICK', 'FORWARD', 'SPAMREPORT']

DEFAULT_CHUNK_SIZE = 1000 * 60 * 60 * 24

V3_PREFIXES = {'hs_date_entered', 'hs_date_exited', 'hs_time_in'}

CONFIG = {
    "access_token": None,
    "token_expires": None,
    "email_chunk_size": DEFAULT_CHUNK_SIZE,
    "subscription_chunk_size": DEFAULT_CHUNK_SIZE,

    # in config.json
    "redirect_uri": None,
    "client_id": None,
    "client_secret": None,
    "refresh_token": None,
    "start_date": None,
    "hapikey": None,
    "include_inactives": None,
}

ENDPOINTS = {
    "contacts_properties":          "/properties/v1/contacts/properties",
    "contacts_all":                 "/contacts/v1/lists/all/contacts/all",
    "contacts_recent":              "/contacts/v1/lists/recently_updated/contacts/recent",
    "contacts_detail":              "/contacts/v1/contact/vids/batch/",

    "companies_properties":         "/companies/v2/properties",
    "companies_all":                "/companies/v2/companies/paged",
    "companies_recent":             "/companies/v2/companies/recent/modified",
    "companies_detail":             "/companies/v2/companies/{company_id}",
    "contacts_by_company":          "/companies/v2/companies/{company_id}/vids",

    "deals_properties":             "/properties/v1/deals/properties",
    "deals_all":                    "/deals/v1/deal/paged",
    "deals_recent":                 "/deals/v1/deal/recent/modified",
    "deals_detail":                 "/deals/v1/deal/{deal_id}",

    "deals_v3_batch_read":          "/crm/v3/objects/deals/batch/read",
    "deals_v3_properties":          "/crm/v3/properties/deals",

    "deal_pipelines":               "/deals/v1/pipelines",

    "campaigns_all":                "/email/public/v1/campaigns/by-id",
    "campaigns_detail":             "/email/public/v1/campaigns/{campaign_id}",

    "engagements_all":              "/engagements/v1/engagements/paged",

    "forms":                        "/forms/v2/forms",
    "form_submissions":             "/form-integrations/v1/submissions/forms/{form_guid}",

    "v3_list_all":                  "/crm/v3/objects/{objectType}",
    "v3_batch_read":                "/crm/v3/objects/{objectType}/batch/read",
    "v3_properties":                "/crm/v3/properties/{objectType}",
    "v3_associations_batch_read":   "/crm/v3/associations/{fromObjectType}/{toObjectType}/batch/read",

    "v3_conversations":             "/conversations/v3/conversations/threads",
    "v3_conversations_messages":    "/conversations/v3/conversations/threads/{thread_id}/messages",

    "subscription_changes":         "/email/public/v1/subscriptions/timeline",
    "email_events":                 "/email/public/v1/events",
    "contact_lists":                "/contacts/v1/lists",
    "workflows":                    "/automation/v3/workflows",
    "owners":                       "/owners/v2/owners",
}

def replace_na(obj):
    """
    Treat N/A as None so that we don't need to add string variant to numbers.
    Picked from https://github.com/singer-io/tap-hubspot/pull/86, without the mess of command line args.
    """
    if isinstance(obj, dict):
        copy = {}
        for k, v in obj.items():
            copy[k] = replace_na(v)
        return copy
    elif isinstance(obj, list):
        return [replace_na(x) for x in obj]
    elif isinstance(obj, str):
        return None if obj.lower() == "n/a" else obj
    else:
        return obj

def get_start(state, tap_stream_id, bookmark_key):
    current_bookmark = singer.get_bookmark(state, tap_stream_id, bookmark_key)
    if current_bookmark is None:
        return CONFIG['start_date']
    return current_bookmark

def get_current_sync_start(state, tap_stream_id):
    current_sync_start_value = singer.get_bookmark(state, tap_stream_id, "current_sync_start")
    if current_sync_start_value is None:
        return current_sync_start_value
    return utils.strptime_to_utc(current_sync_start_value)

def write_current_sync_start(state, tap_stream_id, start):
    value = start
    if start is not None:
        value = utils.strftime(start)
    return singer.write_bookmark(state, tap_stream_id, "current_sync_start", value)

def clean_state(state):
    """ Clear deprecated keys out of state. """
    for stream, bookmark_map in state.get("bookmarks", {}).items():
        if "last_sync_duration" in bookmark_map:
            LOGGER.info("{} - Removing last_sync_duration from state.".format(stream))
            state["bookmarks"][stream].pop("last_sync_duration", None)

def get_url(endpoint, **kwargs):
    if endpoint not in ENDPOINTS:
        raise ValueError("Invalid endpoint {}".format(endpoint))

    return BASE_URL + ENDPOINTS[endpoint].format(**kwargs)


def get_field_type_schema(field_type):
    if field_type == "bool":
        return {"type": ["null", "boolean"]}

    elif field_type == "datetime":
        return {"type": ["null", "string"],
                "format": "date-time"}

    elif field_type == "number":
        # A value like 'N/A' can be returned for this type,
        # so we have to let this be a string sometimes
        # JW: it won't be a problem after replace_na, so remove string here.
        # return {"type": ["null", "number", "string"]}
        return {"type": ["null", "number"]}
    else:
        return {"type": ["null", "string"]}

def get_field_schema(field_type, extras=False):
    if extras:
        return {
            "type": "object",
            "properties": {
                "value": get_field_type_schema(field_type),
                "timestamp": get_field_type_schema("datetime"),
                "source": get_field_type_schema("string"),
                "sourceId": get_field_type_schema("string"),
            }
        }
    else:
        return {
            "type": "object",
            "properties": {
                "value": get_field_type_schema(field_type),
            }
        }

def parse_custom_schema(entity_name, data):
    extras = not entity_name in CUSTOM_SCHEMA_WITHOUT_EXTRAS
    temp_schema = {
        field['name']: get_field_schema(field['type'], extras)
        for field in data
    }

    contacts_specifics = ["objection_reason", "objection_reason_bdr", "rh_meeting_status", "rh_router_name"]
    contacts_need_first_record = ["hubspot_owner_id"]
    if entity_name == "contacts":
        for specific_property in contacts_specifics:
            temp_schema[specific_property]["properties"].update({
                "timestamp": get_field_type_schema("datetime"),
                "sourceId": get_field_type_schema("string"),
            })
        for specific_property in contacts_need_first_record:
            temp_schema[specific_property]["properties"].update({
                "first_value": get_field_type_schema("string")
            })

    return temp_schema


def get_custom_schema(entity_name):
    return parse_custom_schema(entity_name, request(get_url(entity_name + "_properties")).json())

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_associated_company_schema():
    associated_company_schema = load_schema("companies")
    #pylint: disable=line-too-long
    associated_company_schema['properties']['company-id'] = associated_company_schema['properties'].pop('companyId')
    associated_company_schema['properties']['portal-id'] = associated_company_schema['properties'].pop('portalId')
    return associated_company_schema

def load_schema(entity_name):
    schema = utils.load_json(get_abs_path('schemas/{}.json'.format(entity_name)))
    if entity_name in ["contacts", "companies", "deals", "tickets", "feedback_submissions"]:
        if entity_name in ["contacts", "companies", "deals"]:
            custom_schema = get_custom_schema(entity_name)
        elif entity_name in ["tickets", "feedback_submissions"]:
            custom_schema = get_v3_schema(entity_name)

        schema['properties']['properties'] = {
            "type": "object",
            "properties": custom_schema,
        }

        if entity_name in ["deals"]:
            v3_schema = get_v3_schema(entity_name)
            for key, value in v3_schema.items():
                if any(prefix in key for prefix in V3_PREFIXES):
                    custom_schema[key] = value

        # Move properties to top level
        custom_schema_top_level = {'property_{}'.format(k): v for k, v in custom_schema.items()}
        schema['properties'].update(custom_schema_top_level)

        # Make properties_versions selectable and share the same schema.
        versions_schema = utils.load_json(get_abs_path('schemas/versions.json'))
        schema['properties']['properties_versions'] = versions_schema

    if entity_name == "contacts":
        schema['properties']['associated-company'] = load_associated_company_schema()

    return schema

#pylint: disable=invalid-name
def acquire_access_token_from_refresh_token():
    payload = {
        "grant_type": "refresh_token",
        "redirect_uri": CONFIG['redirect_uri'],
        "refresh_token": CONFIG['refresh_token'],
        "client_id": CONFIG['client_id'],
        "client_secret": CONFIG['client_secret'],
    }


    resp = requests.post(BASE_URL + "/oauth/v1/token", data=payload)
    if resp.status_code == 403:
        raise InvalidAuthException(resp.content)

    resp.raise_for_status()
    auth = resp.json()
    CONFIG['access_token'] = auth['access_token']
    CONFIG['refresh_token'] = auth['refresh_token']
    CONFIG['token_expires'] = (
        datetime.datetime.utcnow() +
        datetime.timedelta(seconds=auth['expires_in'] - 600))
    LOGGER.info("Token refreshed. Expires at %s", CONFIG['token_expires'])


def giveup(exc):
    return exc.response is not None \
        and 400 <= exc.response.status_code < 500 \
        and exc.response.status_code != 429

def on_giveup(details):
    if len(details['args']) == 2:
        url, params = details['args']
    else:
        url = details['args']
        params = {}

    raise Exception("Giving up on request after {} tries with url {} and params {}" \
                    .format(details['tries'], url, params))

URL_SOURCE_RE = re.compile(BASE_URL + r'/(\w+)/')

def parse_source_from_url(url):
    match = URL_SOURCE_RE.match(url)
    if match:
        return match.group(1)
    return None

def get_params_and_headers(params):
    """
    This function makes a params object and headers object based on the
    authentication values available. If there is an `hapikey` in the config, we
    need that in `params` and not in the `headers`. Otherwise, we need to get an
    `access_token` to put in the `headers` and not in the `params`
    """
    params = params or {}
    hapikey = CONFIG['hapikey']
    if hapikey is None:
        # if CONFIG['token_expires'] is None or CONFIG['token_expires'] < datetime.datetime.utcnow():
        #     acquire_access_token_from_refresh_token()
        headers = {'Authorization': 'Bearer {}'.format(CONFIG['access_token']), 'Content-Type': 'application/json'}
    else:
        params['hapikey'] = hapikey
        headers = {}

    if 'user_agent' in CONFIG:
        headers['User-Agent'] = CONFIG['user_agent']

    return params, headers


@backoff.on_exception(backoff.constant,
                      (requests.exceptions.RequestException,
                       requests.exceptions.HTTPError),
                      max_tries=5,
                      jitter=None,
                      giveup=giveup,
                      on_giveup=on_giveup,
                      interval=10)
def request(url, params=None):

    params, headers = get_params_and_headers(params)

    req = requests.Request('GET', url, params=params, headers=headers).prepare()
    LOGGER.info("GET %s", req.url)
    with metrics.http_request_timer(parse_source_from_url(url)) as timer:
        resp = SESSION.send(req)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        if resp.status_code == 403:
            raise SourceUnavailableException(resp.content)
        else:
            resp.raise_for_status()

    return resp

def lift_properties_and_versions(record, include_versions=True):
    record = replace_na(record)
    for key, value in record.get('properties', {}).items():
        computed_key = "property_{}".format(key)
        versions = value.get('versions') if include_versions else None
        record[computed_key] = value

        if versions:
            if not record.get('properties_versions'):
                record['properties_versions'] = []
            record['properties_versions'] += versions
    return record


def lift_contact_properties_and_versions(record):
    record = replace_na(record)
    for key, value in record.get('properties', {}).items():
        computed_key = "property_{}".format(key)
        versions = value.get('versions')
        if versions:
            recent_version = versions[0]
            recent_version['first_value'] = versions[-1]['value']
            record[computed_key] = recent_version

    return record

@backoff.on_exception(backoff.constant,
                      (requests.exceptions.RequestException,
                       requests.exceptions.HTTPError),
                      max_tries=5,
                      jitter=None,
                      giveup=giveup,
                      on_giveup=on_giveup,
                      interval=10)
def post_search_endpoint(url, data, params=None):

    params, headers = get_params_and_headers(params)
    headers['content-type'] = "application/json"

    with metrics.http_request_timer(url) as timer:
        resp = requests.post(
            url=url,
            json=data,
            params=params,
            headers=headers
        )

        resp.raise_for_status()

    return resp

def merge_responses(v1_data, v3_data):
    for v1_record in v1_data:
        v1_id = v1_record.get('dealId')
        for v3_record in v3_data:
            v3_id = v3_record.get('id')
            if str(v1_id) == v3_id:
                v1_record['properties'] = {**v1_record['properties'],
                                           **v3_record['properties']}

def process_v3_deals_records(v3_data):
    """
    This function:
    1. filters out fields that don't contain 'hs_date_entered_*' and
       'hs_date_exited_*'
    2. changes a key value pair in `properties` to a key paired to an
       object with a key 'value' and the original value
    """
    transformed_v3_data = []
    for record in v3_data:
        new_properties = {field_name : {'value': field_value}
                          for field_name, field_value in record['properties'].items()
                          if any(prefix in field_name for prefix in V3_PREFIXES)}
        transformed_v3_data.append({**record, 'properties' : new_properties})
    return transformed_v3_data

def get_v3_deals(v3_fields, v1_data):
    v1_ids = [{'id': str(record['dealId'])} for record in v1_data]

    # Sending the first v3_field is enough to get them all
    v3_body = {'inputs': v1_ids,
               'properties': [v3_fields[0]],}
    v3_url = get_url('deals_v3_batch_read')
    v3_resp = post_search_endpoint(v3_url, v3_body)
    return v3_resp.json()['results']

# v3 related
def process_v3_paging(res):
    after = res.get('paging', {}).get('next', {}).get('after', {})
    if after:
        res['after'] = after
    return res

v3_request_kwargs = {
    'path': 'results',
    'more_key': 'after',
    'offset_keys': ['after'],
    'offset_targets': ['after'],
    'process_data': process_v3_paging
}

v3_request_params = {
    'limit': 100
}

def _sync_object_ids(stream_id, catalog, ids, schema, bumble_bee, associated_type='contact', associated_key='associated-vids'):
    if len(ids) == 0:
        return

    mdata = metadata.to_map(catalog.get('metadata'))
    data = process_v3_records(post_v3_batch_read(get_url('v3_batch_read', objectType=stream_id), ids, mdata))
    associations = post_v3_associations(stream_id, associated_type, ids)
    time_extracted = utils.now()

    for record in data:
        vids = associations.get(record['id'], None)
        if vids:
            record[associated_key] = vids
        record = bumble_bee.transform(lift_properties_and_versions(record, include_versions=False), schema, mdata)
        singer.write_record(stream_id, record, catalog.get('stream_alias'), time_extracted=time_extracted)

def sync_v3_objects(STATE, ctx, bookmark_key='updatedAt'):
    stream_id = singer.get_currently_syncing(STATE)
    catalog = ctx.get_catalog_from_id(stream_id)
    mdata = metadata.to_map(catalog.get('metadata'))
    start = utils.strptime_with_tz(get_start(STATE, stream_id, bookmark_key))
    max_bk_value = start
    LOGGER.info("sync_%s from %s", stream_id, start)

    schema = load_schema(stream_id)
    singer.write_schema(stream_id, schema, ['id'], [bookmark_key], catalog.get('stream_alias'))

    url = get_url('v3_list_all', objectType=stream_id)

    ids = []
    with Transformer() as bumble_bee:
        for row in gen_request(STATE, stream_id, url, v3_request_params, **v3_request_kwargs):
            modified_time = None
            if bookmark_key in row:
                modified_time = utils.strptime_with_tz(
                    _transform_datetime( # pylint: disable=protected-access
                        row[bookmark_key]))

            if not modified_time or modified_time >= start:
                ids.append(row['id'])

            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time

            if len(ids) == 100:
                _sync_object_ids(stream_id, catalog, ids, schema, bumble_bee)
                ids = []

        _sync_object_ids(stream_id, catalog, ids, schema, bumble_bee)

    STATE = singer.write_bookmark(STATE, stream_id, bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    return STATE

def sync_v3_tickets_archived(STATE, ctx):
    stream_id = singer.get_currently_syncing(STATE)
    catalog = ctx.get_catalog_from_id('tickets_archived')
    mdata = metadata.to_map(catalog.get('metadata'))
    bookmark_key = 'archivedAt'
    start = utils.strptime_with_tz(get_start(STATE, 'tickets_archived', bookmark_key))
    max_bk_value = start
    LOGGER.info("sync_%s from %s", 'tickets_archived', start)

    schema = load_schema('tickets_archived')
    singer.write_schema('tickets_archived', schema, ['id'], [bookmark_key], catalog.get('stream_alias'))

    url = get_url('v3_list_all', objectType="tickets")
    params = {'limit': 100, 'archived': True}

    with Transformer() as bumble_bee:
        for row in gen_request(STATE, 'tickets_archived', url, params, **v3_request_kwargs):
            record = {}
            modified_time = None
            if bookmark_key in row:
                modified_time = utils.strptime_with_tz(
                    _transform_datetime( # pylint: disable=protected-access
                        row[bookmark_key]))

            if not modified_time or modified_time >= start:
                record['id'] = row['id']
                record['createdAt'] = row['createdAt']
                record['updatedAt'] = row['updatedAt']
                record['archived'] = row['archived']
                record['archivedAt'] = row['archivedAt']
                record['subject'] = row['properties']['subject']
                record['content'] = row['properties']['content']
                record['hs_lastmodifieddate'] = row['properties']['hs_lastmodifieddate'] if 'hs_lastmodifieddate' in row['properties'] else None
                record['hs_pipeline_stage'] = row['properties']['hs_pipeline_stage']
                record = replace_na(record)
                record = bumble_bee.transform(record, schema, mdata)
                singer.write_record('tickets_archived', record, catalog.get('stream_alias'), time_extracted=utils.now())

            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time

    STATE = singer.write_bookmark(STATE, 'tickets_archived', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    return STATE

def get_v3_schema(entity_name):
    url = get_url("v3_properties", objectType=entity_name)
    return parse_custom_schema(entity_name, request(url).json()['results'])

def post_v3_batch_read(url, ids, mdata):
    inputs = [{ 'id': id } for id in ids]
    has_selected_properties = mdata.get(('properties', 'properties'), {}).get('selected')
    properties = [breadcrumb[1].replace('property_', '')
                  for breadcrumb, mdata_map in mdata.items()
                  if breadcrumb
                  and (mdata_map.get('selected') == True or has_selected_properties)]
    body = {
        'inputs': inputs,
        'properties': properties
    }
    resp = post_search_endpoint(url, body)
    return resp.json()['results']

def post_v3_associations(fromObjectType, toObjectType, ids):
    url = get_url("v3_associations_batch_read", fromObjectType=fromObjectType, toObjectType=toObjectType)
    inputs = [{ 'id': id } for id in ids]
    body = { 'inputs': inputs }
    resp = post_search_endpoint(url, body)
    results = resp.json()['results']
    from_ids = [pair['from']['id'] for pair in results]
    to_ids = [[to['id'] for to in pair['to']] for pair in results]
    return dict(zip(from_ids, to_ids))

def process_v3_records(v3_data):
    transformed_v3_data = []
    for record in v3_data:
        new_properties = {field_name : {'value': field_value}
                          for field_name, field_value in record['properties'].items()}
        transformed_v3_data.append({**record, 'properties' : new_properties})
    return transformed_v3_data

def process_threads_messages(STATE, thread_id):
    url = get_url('v3_conversations_messages', thread_id=thread_id)
    messages = []
    params, headers = {'limit': 100}, {'Authorization': 'Bearer {}'.format(CONFIG['access_token']), 'Content-Type': 'application/json'}
    req = requests.Request('GET', url, params=params, headers=headers).prepare()
    attempt = 0
    temp_message_id = ""
    while True:
        LOGGER.info("GET %s", req.url)
        resp = SESSION.send(req)
        if resp.status_code == 200:
            resp = json.loads(resp.text)
            for row in resp['results']:
                if row['type'] in ("MESSAGE", "COMMENT", "WELCOME_MESSAGE"):
                    if row['id'] == temp_message_id:
                        continue
                    temp_message_id = row['id']

                    row['clientType'] = row['client']['clientType']
                    if len(row['senders']) > 0:
                        row['senders'] = row['senders'][0]
                        LOGGER.info(f"thread: {thread_id},  loading: {row['id']}")
                        if 'deliveryIdentifier' in row['senders']\
                                and row['senders']['deliveryIdentifier']['type'] == "HS_EMAIL_ADDRESS":
                            row['senders']['email'] = row['senders']['deliveryIdentifier']['value']
                    else:
                        row['senders'] = {}
                    messages.append(row)
                else:
                    continue

            if 'paging' in resp and ('after' not in params or params['after'] != resp['paging']['next']['after']):
                params['after'] = resp['paging']['next']['after']
                req = requests.Request('GET', url, params=params, headers=headers).prepare()
            else:
                break
        elif resp.status_code == 401 or attempt > MAX_TRIES:
            break
        else:
            LOGGER.warning(f"Response {resp.status_code}. Waiting {BACKOFF_SECONDS} seconds...")
            time.sleep(BACKOFF_SECONDS)
            req = requests.Request('GET', url, params=params, headers=headers).prepare()
            attempt += 1


    return messages

def sync_v3_conversations(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    bookmark_key='latestMessageTimestamp'
    start = utils.strptime_with_tz(get_start(STATE, 'conversations', bookmark_key))
    max_bk_value = start
    LOGGER.info("sync_conversations from %s", start)

    schema = load_schema('conversations')
    singer.write_schema('conversations', schema, ['id', 'inboxId'], [bookmark_key], catalog.get('stream_alias'))

    url = get_url('v3_conversations')
    v3_conversations_params = {
        'limit': 100,
        'sort': 'latestMessageTimestamp',
        'latestMessageTimestampAfter': start.strftime("%Y-%m-%d")
        }
    ids = []
    with Transformer() as bumble_bee:
        for row in gen_request(STATE, 'conversations', url, v3_conversations_params, **v3_request_kwargs):

            if row['spam'] == True:
                continue

            modified_time = None
            if bookmark_key in row:
                modified_time = utils.strptime_with_tz(
                    _transform_datetime( # pylint: disable=protected-access
                        row[bookmark_key]))
            if not modified_time or modified_time >= start:
                record = row
                record["messages"] = process_threads_messages(STATE, row['id'])
                LOGGER.info(f"THREAD {row['id']}: MESSAGES PROCESSED")

                record = replace_na(record)
                record = bumble_bee.transform(record, schema, mdata)
                singer.write_record('conversations', record, catalog.get('stream_alias'), time_extracted=utils.now())

            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time


    STATE = singer.write_bookmark(STATE, 'conversations', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    return STATE

#pylint: disable=line-too-long
def gen_request(STATE, tap_stream_id, url, params, path, more_key, offset_keys, offset_targets, v3_fields=None, process_data=None):
    if len(offset_keys) != len(offset_targets):
        raise ValueError("Number of offset_keys must match number of offset_targets")

    if singer.get_offset(STATE, tap_stream_id):
        params.update(singer.get_offset(STATE, tap_stream_id))

    with metrics.record_counter(tap_stream_id) as counter:
        while True:
            data = request(url, params).json()

            if data.get(path) is None:
                raise RuntimeError("Unexpected API response: {} not in {}".format(path, data.keys()))

            if process_data:
                data = process_data(data)

            if v3_fields:
                v3_data = get_v3_deals(v3_fields, data[path])

                # The shape of v3_data is different than the V1 response,
                # so we transform v3 to look like v1
                transformed_v3_data = process_v3_deals_records(v3_data)
                merge_responses(data[path], transformed_v3_data)

            for row in data[path]:
                counter.increment()
                yield row

            if not data.get(more_key, False):
                break

            if more_key in params and params[more_key] == data.get(more_key, False):
                break

            STATE = singer.clear_offset(STATE, tap_stream_id)
            for key, target in zip(offset_keys, offset_targets):
                if key in data:
                    params[target] = data[key]
                    STATE = singer.set_offset(STATE, tap_stream_id, target, data[key])

            singer.write_state(STATE)

    STATE = singer.clear_offset(STATE, tap_stream_id)
    singer.write_state(STATE)


def _sync_contact_vids(catalog, vids, schema, bumble_bee):
    if len(vids) == 0:
        return

    data = request(get_url("contacts_detail"), params={'vid': vids, 'showListMemberships' : False, "formSubmissionMode" : "all", "propertyMode": "value_and_history"}).json()
    time_extracted = utils.now()
    mdata = metadata.to_map(catalog.get('metadata'))

    for record in data.values():
        record = bumble_bee.transform(lift_contact_properties_and_versions(record), schema, mdata)
        singer.write_record("contacts", record, catalog.get('stream_alias'), time_extracted=time_extracted)

default_contact_params = {
    'showListMemberships': True,
    'includeVersion': True,
    'count': 100,
}

def sync_contacts(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    bookmark_key = 'versionTimestamp'
    start = utils.strptime_with_tz(get_start(STATE, "contacts", bookmark_key))
    LOGGER.info("sync_contacts from %s", start)

    max_bk_value = start
    # schema = catalog.get("schema")
    schema = load_schema("contacts")

    singer.write_schema("contacts", schema, ["vid"], [bookmark_key], catalog.get('stream_alias'))

    url = get_url("contacts_all")

    vids = []
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in gen_request(STATE, 'contacts', url, default_contact_params, 'contacts', 'has-more', ['vid-offset'], ['vidOffset']):
            modified_time = None
            if bookmark_key in row:
                modified_time = utils.strptime_with_tz(
                    _transform_datetime( # pylint: disable=protected-access
                        row[bookmark_key],
                        UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING))

            if not modified_time or modified_time >= start:
                vids.append(row['vid'])

            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time

            if len(vids) == 100:
                _sync_contact_vids(catalog, vids, schema, bumble_bee)
                vids = []

        _sync_contact_vids(catalog, vids, schema, bumble_bee)

    STATE = singer.write_bookmark(STATE, 'contacts', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    return STATE

def sync_contacts_list_memberships(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    bookmark_key = 'timestamp'
    mdata = metadata.to_map(catalog.get('metadata'))
    start = utils.strptime_with_tz(get_start(STATE, "contacts_list_memberships", bookmark_key))
    LOGGER.info("sync_contacts_list_memberships from %s", start)

    max_bk_value = start
    schema = load_schema("contacts_list_memberships")

    singer.write_schema("contacts_list_memberships", schema, ["vid", "static-list-id"], [bookmark_key], catalog.get('stream_alias'))

    url = get_url("contacts_all")

    vids = []
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in gen_request(STATE, 'contacts_list_memberships', url, default_contact_params, 'contacts', 'has-more', ['vid-offset'], ['vidOffset']):
            vid = row['vid']
            lm = row['list-memberships']
            for record in lm:
                modified_time = None
                if bookmark_key in record:
                    modified_time = utils.strptime_with_tz(
                        _transform_datetime( # pylint: disable=protected-access
                            record[bookmark_key],
                            UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING))

                if not modified_time or modified_time >= start:
                    record['vid'] = vid
                    record = bumble_bee.transform(record, schema, mdata)
                    singer.write_record('contacts_list_memberships', record, catalog.get('stream_alias'), time_extracted=utils.now())

                if modified_time and modified_time >= max_bk_value:
                    max_bk_value = modified_time

    STATE = singer.write_bookmark(STATE, 'contacts_list_memberships', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    default_contact_params.pop("vidOffset", None)
    return STATE

class ValidationPredFailed(Exception):
    pass

# companies_recent only supports 10,000 results. If there are more than this,
# we'll need to use the companies_all endpoint
def use_recent_companies_endpoint(response):
    return response["total"] < 10000

default_contacts_by_company_params = {'count' : 100}

# NB> to do: support stream aliasing and field selection
def _sync_contacts_by_company(STATE, ctx, company_id):
    schema = load_schema(CONTACTS_BY_COMPANY)
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    url = get_url("contacts_by_company", company_id=company_id)
    path = 'vids'
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        with metrics.record_counter(CONTACTS_BY_COMPANY) as counter:
            data = request(url, default_contacts_by_company_params).json()

            if data.get(path) is None:
                raise RuntimeError("Unexpected API response: {} not in {}".format(path, data.keys()))

            for row in data[path]:
                counter.increment()
                record = {'company-id' : company_id,
                          'contact-id' : row}
                record = bumble_bee.transform(lift_properties_and_versions(record), schema, mdata)
                singer.write_record("contacts_by_company", record, time_extracted=utils.now())

    return STATE

default_company_params = {
    'limit': 250, 'properties': ["createdate", "hs_lastmodifieddate"]
}

def sync_companies(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    bumble_bee = Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING)
    bookmark_key = 'hs_lastmodifieddate'
    start = utils.strptime_to_utc(get_start(STATE, "companies", bookmark_key))
    LOGGER.info("sync_companies from %s", start)
    schema = load_schema('companies')
    singer.write_schema("companies", schema, ["companyId"], [bookmark_key], catalog.get('stream_alias'))

    # Because this stream doesn't query by `lastUpdated`, it cycles
    # through the data set every time. The issue with this is that there
    # is a race condition by which records may be updated between the
    # start of this table's sync and the end, causing some updates to not
    # be captured, in order to combat this, we must store the current
    # sync's start in the state and not move the bookmark past this value.
    current_sync_start = get_current_sync_start(STATE, "companies") or utils.now()
    STATE = write_current_sync_start(STATE, "companies", current_sync_start)
    singer.write_state(STATE)

    url = get_url("companies_all")
    max_bk_value = start
    if CONTACTS_BY_COMPANY in ctx.selected_stream_ids:
        contacts_by_company_schema = load_schema(CONTACTS_BY_COMPANY)
        singer.write_schema("contacts_by_company", contacts_by_company_schema, ["company-id", "contact-id"])

    with bumble_bee:
        for row in gen_request(STATE, 'companies', url, default_company_params, 'companies', 'has-more', ['offset'], ['offset']):
            row_properties = row['properties']
            modified_time = None
            if bookmark_key in row_properties:
                # Hubspot returns timestamps in millis
                timestamp_millis = row_properties[bookmark_key]['timestamp'] / 1000.0
                modified_time = datetime.datetime.fromtimestamp(timestamp_millis, datetime.timezone.utc)
            elif 'createdate' in row_properties:
                # Hubspot returns timestamps in millis
                timestamp_millis = row_properties['createdate']['timestamp'] / 1000.0
                modified_time = datetime.datetime.fromtimestamp(timestamp_millis, datetime.timezone.utc)

            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time

            if not modified_time or modified_time >= start:
                record = request(get_url("companies_detail", company_id=row['companyId'])).json()
                record = bumble_bee.transform(lift_properties_and_versions(record), schema, mdata)
                singer.write_record("companies", record, catalog.get('stream_alias'), time_extracted=utils.now())
                if CONTACTS_BY_COMPANY in ctx.selected_stream_ids:
                    STATE = _sync_contacts_by_company(STATE, ctx, record['companyId'])

    # Don't bookmark past the start of this sync to account for updated records during the sync.
    new_bookmark = min(max_bk_value, current_sync_start)
    STATE = singer.write_bookmark(STATE, 'companies', bookmark_key, utils.strftime(new_bookmark))
    STATE = write_current_sync_start(STATE, 'companies', None)
    singer.write_state(STATE)
    return STATE

def get_selected_custom_field(mdata):
    selected_custom_fields = []
    top_level_custom_props = [x for x in mdata if len(x) == 2 and 'property_' in x[1]]
    for prop in top_level_custom_props:
        if mdata.get(prop, {}).get('selected') == True:
            selected_custom_fields.append(prop[1])
    return selected_custom_fields

def sync_deals(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    bookmark_key = 'hs_lastmodifieddate'
    start = utils.strptime_with_tz(get_start(STATE, "deals", bookmark_key))
    max_bk_value = start
    LOGGER.info("sync_deals from %s", start)
    most_recent_modified_time = start
    params = {'limit': 100,
              'includeAssociations': False,
              'properties' : []}

    schema = load_schema("deals")

    # Check if we should  include associations
    for key in mdata.keys():
        if 'associations' in key:
            assoc_mdata = mdata.get(key)
            if (assoc_mdata.get('selected') and assoc_mdata.get('selected') == True):
                params['includeAssociations'] = True

    v3_fields = None
    has_selected_properties = mdata.get(('properties', 'properties'), {}).get('selected')
    selected_custom_fields = get_selected_custom_field(mdata)
    if has_selected_properties or len(selected_custom_fields) > 0:
        # On 2/12/20, hubspot added a lot of additional properties for
        # deals, and appending all of them to requests ended up leading to
        # 414 (url-too-long) errors. Hubspot recommended we use the
        # `includeAllProperties` and `allpropertiesFetchMode` params
        # instead.
        params['includeAllProperties'] = True
        params['allPropertiesFetchMode'] = 'latest_version'

        # Grab selected `hs_date_entered/exited` fields to call the v3 endpoint with
        v3_fields = [breadcrumb[1].replace('property_', '')
                     for breadcrumb, mdata_map in mdata.items()
                     if breadcrumb
                     and (mdata_map.get('selected') == True or has_selected_properties)
                     and any(prefix in breadcrumb[1] for prefix in V3_PREFIXES)]

    raw_schema_keys = list(schema['properties'].keys())
    for prop in raw_schema_keys:
        if 'property_' in prop and prop not in selected_custom_fields:
            schema['properties'].pop(prop)
    singer.write_schema("deals", schema, ["dealId"], [bookmark_key], catalog.get('stream_alias'))


    url = get_url('deals_all')
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in gen_request(STATE, 'deals', url, params, 'deals', "hasMore", ["offset"], ["offset"], v3_fields=v3_fields):
            row_properties = row['properties']
            modified_time = None
            if bookmark_key in row_properties:
                # Hubspot returns timestamps in millis
                timestamp_millis = row_properties[bookmark_key]['timestamp'] / 1000.0
                modified_time = datetime.datetime.fromtimestamp(timestamp_millis, datetime.timezone.utc)
            elif 'createdate' in row_properties:
                # Hubspot returns timestamps in millis
                timestamp_millis = row_properties['createdate']['timestamp'] / 1000.0
                modified_time = datetime.datetime.fromtimestamp(timestamp_millis, datetime.timezone.utc)
            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time

            if not modified_time or modified_time >= start:
                record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)
                singer.write_record("deals", record, catalog.get('stream_alias'), time_extracted=utils.now())

    STATE = singer.write_bookmark(STATE, 'deals', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    return STATE


def sync_deal_owner_history(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    bookmark_key = 'lastupdateddate'
    start = utils.strptime_with_tz(get_start(STATE, "deal_owner_history", bookmark_key))
    max_bk_value = start
    LOGGER.info("sync_deals_owner_history from %s", start)
    most_recent_modified_time = start

    key_property = 'hubspot_owner_id'
    params = {'limit': 100,
              'includeAssociations': False,
              'propertiesWithHistory' : ['hubspot_owner_id']}

    schema = load_schema("deal_owner_history")
    singer.write_schema("deal_owner_history", schema, ["dealId", "hubspot_owner_id", "updated_at"], [bookmark_key], catalog.get('stream_alias'))

    url = get_url('deals_all')
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in gen_request(STATE, 'deal_owner_history', url, params, 'deals', "hasMore", ["offset"], ["offset"]):
            row_properties = row['properties']
            modified_time = None
            if key_property in row_properties:
                timestamp_millis = row_properties['hubspot_owner_id']['timestamp'] / 1000.0
                modified_time = datetime.datetime.fromtimestamp(timestamp_millis, datetime.timezone.utc)

            if modified_time and modified_time >= max_bk_value:
                max_bk_value = modified_time

            if key_property in row_properties and (not modified_time or modified_time >= start):
                row = replace_na(row)
                versions = row['properties'][key_property].get('versions')
                for v in versions:
                    value = "No Owner" if v['value'] == "" else v['value']
                    record = bumble_bee.transform({
                        'portalId': row['portalId'],
                        'dealId': row['dealId'],
                        key_property: value,
                        'updated_at': v.get('timestamp'),
                        'sourceId': v.get('sourceId'),
                        'source': v.get('source'),
                    }, schema, mdata)
                    singer.write_record("deal_owner_history", record, catalog.get('stream_alias'), time_extracted=utils.now())

    STATE = singer.write_bookmark(STATE, 'deal_owner_history', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)
    return STATE

#NB> no suitable bookmark is available: https://developers.hubspot.com/docs/methods/email/get_campaigns_by_id
def sync_campaigns(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema("campaigns")
    singer.write_schema("campaigns", schema, ["id"], catalog.get('stream_alias'))
    LOGGER.info("sync_campaigns(NO bookmarks)")
    url = get_url("campaigns_all")
    params = {'limit': 500}

    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in gen_request(STATE, 'campaigns', url, params, "campaigns", "hasMore", ["offset"], ["offset"]):
            record = request(get_url("campaigns_detail", campaign_id=row['id'])).json()
            record = bumble_bee.transform(lift_properties_and_versions(record), schema, mdata)
            singer.write_record("campaigns", record, catalog.get('stream_alias'), time_extracted=utils.now())

    return STATE


def sync_entity_chunked(STATE, catalog, entity_name, key_properties, path):
    schema = load_schema(entity_name)
    bookmark_key = 'startTimestamp'

    singer.write_schema(entity_name, schema, key_properties, [bookmark_key], catalog.get('stream_alias'))

    start = get_start(STATE, entity_name, bookmark_key)
    LOGGER.info("sync_%s from %s", entity_name, start)

    now = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
    now_ts = int(now.timestamp() * 1000)

    start_ts = int(utils.strptime_with_tz(start).timestamp() * 1000)
    url = get_url(entity_name)

    mdata = metadata.to_map(catalog.get('metadata'))

    if entity_name == 'email_events':
        window_size = int(CONFIG['email_chunk_size'])
    elif entity_name == 'subscription_changes':
        window_size = int(CONFIG['subscription_chunk_size'])

    with metrics.record_counter(entity_name) as counter:
        while start_ts < now_ts:
            end_ts = start_ts + window_size
            default_params = {
                'startTimestamp': start_ts,
                'endTimestamp': end_ts,
                'limit': 1000,
            }
            with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
                for eventType in EMAIL_EVENT_TYPES:
                    STATE = singer.clear_offset(STATE, entity_name)
                    singer.write_state(STATE)
                    while True:
                        params = default_params.copy()

                        our_offset = singer.get_offset(STATE, entity_name)
                        if bool(our_offset) and our_offset.get('offset') != None:
                            params[StateFields.offset] = our_offset.get('offset')

                        params['eventType'] = eventType

                        data = request(url, params).json()
                        time_extracted = utils.now()

                        if data.get(path) is None:
                            raise RuntimeError("Unexpected API response: {} not in {}".format(path, data.keys()))

                        for row in data[path]:
                            counter.increment()
                            record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)
                            singer.write_record(entity_name,
                                                record,
                                                catalog.get('stream_alias'),
                                                time_extracted=time_extracted)
                        if data.get('hasMore'):
                            STATE = singer.set_offset(STATE, entity_name, 'offset', data['offset'])
                            singer.write_state(STATE)
                        else:
                            STATE = singer.clear_offset(STATE, entity_name)
                            singer.write_state(STATE)
                            break
            STATE = singer.write_bookmark(STATE, entity_name, 'startTimestamp', utils.strftime(datetime.datetime.fromtimestamp((start_ts / 1000), datetime.timezone.utc ))) # pylint: disable=line-too-long
            singer.write_state(STATE)
            start_ts = end_ts

    STATE = singer.clear_offset(STATE, entity_name)
    singer.write_state(STATE)
    return STATE

def sync_subscription_changes(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    STATE = sync_entity_chunked(STATE, catalog, "subscription_changes", ["timestamp", "portalId", "recipient"],
                                "timeline")
    return STATE

def sync_email_events(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    STATE = sync_entity_chunked(STATE, catalog, "email_events", ["id"], "events")
    return STATE

def sync_contact_lists(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema("contact_lists")
    bookmark_key = 'updatedAt'
    singer.write_schema("contact_lists", schema, ["listId"], [bookmark_key], catalog.get('stream_alias'))

    start = get_start(STATE, "contact_lists", bookmark_key)
    max_bk_value = start

    LOGGER.info("sync_contact_lists from %s", start)

    url = get_url("contact_lists")
    params = {'count': 250}
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in gen_request(STATE, 'contact_lists', url, params, "lists", "has-more", ["offset"], ["offset"]):
            record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)

            if record[bookmark_key] >= start:
                singer.write_record("contact_lists", record, catalog.get('stream_alias'), time_extracted=utils.now())
            if record[bookmark_key] >= max_bk_value:
                max_bk_value = record[bookmark_key]

    STATE = singer.write_bookmark(STATE, 'contact_lists', bookmark_key, max_bk_value)
    singer.write_state(STATE)

    return STATE

default_form_submissions_params = {'limit': 50}

def sync_form_submissions(STATE, ctx, form_guids):
    if len(form_guids) == 0:
        return STATE

    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema(FORM_SUBMISSIONS)

    singer.write_schema(FORM_SUBMISSIONS, schema, [])

    bookmark_key = 'submittedAt'
    current_sync_start = get_current_sync_start(STATE, FORM_SUBMISSIONS) or utils.now()
    STATE = write_current_sync_start(STATE, FORM_SUBMISSIONS, current_sync_start)

    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for form_guid in form_guids:
            sub_stream_id = '{}_{}'.format(FORM_SUBMISSIONS, form_guid)
            start = utils.strptime_to_utc(get_start(STATE, sub_stream_id, bookmark_key))
            max_bk_value = start

            LOGGER.info("sync_form_submissions for %s from %s", FORMS_TO_GET_SUBMISSIONS[form_guid], start)
            url = get_url(FORM_SUBMISSIONS, form_guid=form_guid)
            STATE = singer.clear_offset(STATE, FORM_SUBMISSIONS) # do not use previous offset since multiple submissions for multiple forms are synced
            singer.write_state(STATE)

            # use default_form_submissions_params.copy() ! gen_request will modify the params argument
            for row in gen_request(STATE, FORM_SUBMISSIONS, url, default_form_submissions_params.copy(), **v3_request_kwargs):
                submitted_time = None
                if bookmark_key in row:
                    submitted_time = utils.strptime_with_tz(
                        _transform_datetime(  # pylint: disable=protected-access
                            row[bookmark_key],
                            UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING))

                if not submitted_time or submitted_time >= start:
                    row['formGuid'] = form_guid
                    record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)
                    singer.write_record(FORM_SUBMISSIONS, record, catalog.get('stream_alias'), time_extracted=utils.now())

                if submitted_time and submitted_time >= max_bk_value:
                    max_bk_value = submitted_time

            STATE = singer.clear_offset(STATE, FORM_SUBMISSIONS) # do not use previous offset since multiple submissions for multiple forms are synced
            new_bookmark = min(max_bk_value, current_sync_start)
            STATE = singer.write_bookmark(STATE, sub_stream_id, bookmark_key, utils.strftime(new_bookmark))
            singer.write_state(STATE)

    STATE = write_current_sync_start(STATE, FORM_SUBMISSIONS, None)
    singer.write_state(STATE)

    return STATE

def sync_forms(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema("forms")
    bookmark_key = 'updatedAt'

    singer.write_schema("forms", schema, ["guid"], [bookmark_key], catalog.get('stream_alias'))
    start = utils.strptime_to_utc(get_start(STATE, "forms", bookmark_key))
    max_bk_value = start

    LOGGER.info("sync_forms from %s", start)

    data = request(get_url("forms")).json()
    time_extracted = utils.now()

    form_submissions_guids = []

    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in data:
            record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)

            modified_time = utils.strptime_with_tz(
                    _transform_datetime(  # pylint: disable=protected-access
                        record[bookmark_key],
                        UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING))

            if modified_time >= start:
                singer.write_record("forms", record, catalog.get('stream_alias'), time_extracted=time_extracted)
            if modified_time >= max_bk_value:
                max_bk_value = modified_time

            form_guid = record['guid']
            if FORM_SUBMISSIONS in ctx.selected_stream_ids and form_guid in FORMS_TO_GET_SUBMISSIONS:
                form_submissions_guids.append(form_guid)

    STATE = singer.write_bookmark(STATE, 'forms', bookmark_key, utils.strftime(max_bk_value))
    singer.write_state(STATE)

    if FORM_SUBMISSIONS in ctx.selected_stream_ids:
        STATE = singer.set_currently_syncing(STATE, FORM_SUBMISSIONS)
        STATE = sync_form_submissions(STATE, ctx, form_submissions_guids)

    return STATE

def sync_workflows(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema("workflows")
    bookmark_key = 'updatedAt'
    singer.write_schema("workflows", schema, ["id"], [bookmark_key], catalog.get('stream_alias'))
    start = get_start(STATE, "workflows", bookmark_key)
    max_bk_value = start

    STATE = singer.write_bookmark(STATE, 'workflows', bookmark_key, max_bk_value)
    singer.write_state(STATE)

    LOGGER.info("sync_workflows from %s", start)

    data = request(get_url("workflows")).json()
    time_extracted = utils.now()

    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in data['workflows']:
            record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)
            if record[bookmark_key] >= start:
                singer.write_record("workflows", record, catalog.get('stream_alias'), time_extracted=time_extracted)
            if record[bookmark_key] >= max_bk_value:
                max_bk_value = record[bookmark_key]

    STATE = singer.write_bookmark(STATE, 'workflows', bookmark_key, max_bk_value)
    singer.write_state(STATE)
    return STATE

def sync_owners(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema("owners")
    bookmark_key = 'updatedAt'

    singer.write_schema("owners", schema, ["ownerId"], [bookmark_key], catalog.get('stream_alias'))
    start = get_start(STATE, "owners", bookmark_key)
    max_bk_value = start

    LOGGER.info("sync_owners from %s", start)

    params = {}
    if CONFIG.get('include_inactives'):
        params['includeInactives'] = "true"
    data = request(get_url("owners"), params).json()

    time_extracted = utils.now()

    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in data:
            record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)
            if record[bookmark_key] >= max_bk_value:
                max_bk_value = record[bookmark_key]

            if record[bookmark_key] >= start:
                singer.write_record("owners", record, catalog.get('stream_alias'), time_extracted=time_extracted)

    STATE = singer.write_bookmark(STATE, 'owners', bookmark_key, max_bk_value)
    singer.write_state(STATE)
    return STATE

def sync_engagements(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema("engagements")
    bookmark_key = 'lastUpdated'
    singer.write_schema("engagements", schema, ["engagement_id"], [bookmark_key], catalog.get('stream_alias'))
    start = get_start(STATE, "engagements", bookmark_key)

    # Because this stream doesn't query by `lastUpdated`, it cycles
    # through the data set every time. The issue with this is that there
    # is a race condition by which records may be updated between the
    # start of this table's sync and the end, causing some updates to not
    # be captured, in order to combat this, we must store the current
    # sync's start in the state and not move the bookmark past this value.
    current_sync_start = get_current_sync_start(STATE, "engagements") or utils.now()
    STATE = write_current_sync_start(STATE, "engagements", current_sync_start)
    singer.write_state(STATE)

    max_bk_value = start
    LOGGER.info("sync_engagements from %s", start)

    STATE = singer.write_bookmark(STATE, 'engagements', bookmark_key, start)
    singer.write_state(STATE)

    url = get_url("engagements_all")
    params = {'limit': 250}
    top_level_key = "results"
    engagements = gen_request(STATE, 'engagements', url, params, top_level_key, "hasMore", ["offset"], ["offset"])

    time_extracted = utils.now()

    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for engagement in engagements:
            record = bumble_bee.transform(lift_properties_and_versions(engagement), schema, mdata)
            if record['engagement'][bookmark_key] >= start:
                # hoist PK and bookmark field to top-level record
                record['engagement_id'] = record['engagement']['id']
                record[bookmark_key] = record['engagement'][bookmark_key]
                singer.write_record("engagements", record, catalog.get('stream_alias'), time_extracted=time_extracted)
                if record['engagement'][bookmark_key] >= max_bk_value:
                    max_bk_value = record['engagement'][bookmark_key]

    # Don't bookmark past the start of this sync to account for updated records during the sync.
    new_bookmark = min(utils.strptime_to_utc(max_bk_value), current_sync_start)
    STATE = singer.write_bookmark(STATE, 'engagements', bookmark_key, utils.strftime(new_bookmark))
    STATE = write_current_sync_start(STATE, 'engagements', None)
    singer.write_state(STATE)
    return STATE

def sync_deal_pipelines(STATE, ctx):
    catalog = ctx.get_catalog_from_id(singer.get_currently_syncing(STATE))
    mdata = metadata.to_map(catalog.get('metadata'))
    schema = load_schema('deal_pipelines')
    singer.write_schema('deal_pipelines', schema, ['pipelineId'], catalog.get('stream_alias'))
    LOGGER.info('sync_deal_pipelines')
    data = request(get_url('deal_pipelines')).json()
    with Transformer(UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING) as bumble_bee:
        for row in data:
            record = bumble_bee.transform(lift_properties_and_versions(row), schema, mdata)
            singer.write_record("deal_pipelines", record, catalog.get('stream_alias'), time_extracted=utils.now())
    singer.write_state(STATE)
    return STATE
@attr.s
class Stream(object):
    tap_stream_id = attr.ib()
    sync = attr.ib()
    key_properties = attr.ib()
    replication_key = attr.ib()
    replication_method = attr.ib()

STREAMS = [
    # Do these first as they are incremental
    Stream('subscription_changes', sync_subscription_changes, ['timestamp', 'portalId', 'recipient'], 'startTimestamp', 'INCREMENTAL'),
    Stream('email_events', sync_email_events, ['id'], 'startTimestamp', 'INCREMENTAL'),
    Stream('contacts_list_memberships', sync_contacts_list_memberships, ["vid", "static-list-id"], 'timestamp', 'INCREMENTAL'),
    Stream('deal_owner_history', sync_deal_owner_history, ["dealId", "hubspot_owner_id", "updated_at"], 'lastupdateddate', 'INCREMENTAL'),

    # Do these last as they are full table
    Stream('forms', sync_forms, ['guid'], 'updatedAt', 'FULL_TABLE'),
    Stream('workflows', sync_workflows, ['id'], 'updatedAt', 'FULL_TABLE'),
    Stream('owners', sync_owners, ["ownerId"], 'updatedAt', 'FULL_TABLE'),
    Stream('campaigns', sync_campaigns, ["id"], None, 'FULL_TABLE'),
    Stream('contact_lists', sync_contact_lists, ["listId"], 'updatedAt', 'FULL_TABLE'),
    Stream('contacts', sync_contacts, ["vid"], 'versionTimestamp', 'FULL_TABLE'),
    Stream('companies', sync_companies, ["companyId"], 'hs_lastmodifieddate', 'FULL_TABLE'),
    Stream('deals', sync_deals, ["dealId"], 'hs_lastmodifieddate', 'FULL_TABLE'),
    Stream('deal_pipelines', sync_deal_pipelines, ['pipelineId'], None, 'FULL_TABLE'),
    Stream('engagements', sync_engagements, ["engagement_id"], 'lastUpdated', 'FULL_TABLE'),
    Stream('tickets', sync_v3_objects, ['id'], 'updatedAt', 'FULL_TABLE'),
    Stream('feedback_submissions', sync_v3_objects, ['id'], 'updatedAt', 'FULL_TABLE'),
    Stream('tickets_archived', sync_v3_tickets_archived, ['id'], 'archivedAt', 'FULL_TABLE'),
    Stream('conversations', sync_v3_conversations, ['id', 'inboxId'], 'latestMessageTimestamp', 'FULL_TABLE')
]

def get_streams_to_sync(streams, state):
    target_stream = singer.get_currently_syncing(state)
    result = streams
    if target_stream:
        skipped = list(itertools.takewhile(
            lambda x: x.tap_stream_id != target_stream, streams))
        rest = list(itertools.dropwhile(
            lambda x: x.tap_stream_id != target_stream, streams))
        result = rest + skipped # Move skipped streams to end
    if not result:
        raise Exception('Unknown stream {} in state'.format(target_stream))
    return result

def get_selected_streams(remaining_streams, ctx):
    selected_streams = []
    for stream in remaining_streams:
        if stream.tap_stream_id in ctx.selected_stream_ids:
            selected_streams.append(stream)
    return selected_streams

def do_sync(STATE, catalog):
    # Clear out keys that are no longer used
    clean_state(STATE)

    ctx = Context(catalog)
    validate_dependencies(ctx)

    remaining_streams = get_streams_to_sync(STREAMS, STATE)
    selected_streams = get_selected_streams(remaining_streams, ctx)
    LOGGER.info('Starting sync. Will sync these streams: %s',
                [stream.tap_stream_id for stream in selected_streams])
    for stream in selected_streams:
        LOGGER.info('Syncing %s', stream.tap_stream_id)
        STATE = singer.set_currently_syncing(STATE, stream.tap_stream_id)
        singer.write_state(STATE)

        try:
            STATE = stream.sync(STATE, ctx) # pylint: disable=not-callable
        except SourceUnavailableException as ex:
            error_message = str(ex).replace(CONFIG['access_token'], 10 * '*')
            LOGGER.error(error_message)
            pass

    STATE = singer.set_currently_syncing(STATE, None)
    singer.write_state(STATE)
    LOGGER.info("Sync completed")

class Context(object):
    def __init__(self, catalog):
        self.selected_stream_ids = set()

        for stream in catalog.get('streams'):
            mdata = metadata.to_map(stream['metadata'])
            if metadata.get(mdata, (), 'selected'):
                self.selected_stream_ids.add(stream['tap_stream_id'])

        self.catalog = catalog

    def get_catalog_from_id(self,tap_stream_id):
        return [c for c in self.catalog.get('streams')
               if c.get('stream') == tap_stream_id][0]

# stream a is dependent on stream STREAM_DEPENDENCIES[a]
STREAM_DEPENDENCIES = {
    CONTACTS_BY_COMPANY: 'companies'
}

def validate_dependencies(ctx):
    errs = []
    msg_tmpl = ("Unable to extract {0} data. "
                "To receive {0} data, you also need to select {1}.")

    for k,v in STREAM_DEPENDENCIES.items():
        if k in ctx.selected_stream_ids and v not in ctx.selected_stream_ids:
            errs.append(msg_tmpl.format(k, v))
    if errs:
        raise DependencyException(" ".join(errs))

def load_discovered_schema(stream):
    schema = load_schema(stream.tap_stream_id)
    mdata = metadata.new()

    mdata = metadata.write(mdata, (), 'table-key-properties', stream.key_properties)
    mdata = metadata.write(mdata, (), 'forced-replication-method', stream.replication_method)

    if stream.replication_key:
        mdata = metadata.write(mdata, (), 'valid-replication-keys', [stream.replication_key])

    for field_name, props in schema['properties'].items():
        if field_name in stream.key_properties or field_name == stream.replication_key:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'automatic')
        else:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'available')

    # The engagements stream has nested data that we synthesize; The engagement field needs to be automatic
    if stream.tap_stream_id == "engagements":
        mdata = metadata.write(mdata, ('properties', 'engagement'), 'inclusion', 'automatic')
        mdata = metadata.write(mdata, ('properties', 'lastUpdated'), 'inclusion', 'automatic')

    return schema, metadata.to_list(mdata)

def discover_schemas():
    result = {'streams': []}
    for stream in STREAMS:
        LOGGER.info('Loading schema for %s', stream.tap_stream_id)
        schema, mdata = load_discovered_schema(stream)
        result['streams'].append({'stream': stream.tap_stream_id,
                                  'tap_stream_id': stream.tap_stream_id,
                                  'schema': schema,
                                  'metadata': mdata})
    # Load the contacts_by_company schema
    LOGGER.info('Loading schema for contacts_by_company')
    contacts_by_company = Stream('contacts_by_company', _sync_contacts_by_company, ['company-id', 'contact-id'], None, 'FULL_TABLE')
    schema, mdata = load_discovered_schema(contacts_by_company)

    result['streams'].append({'stream': CONTACTS_BY_COMPANY,
                              'tap_stream_id': CONTACTS_BY_COMPANY,
                              'schema': schema,
                              'metadata': mdata})

    return result

def do_discover():
    LOGGER.info('Loading schemas')
    json.dump(discover_schemas(), sys.stdout, indent=2)

def main_impl():
    args = utils.parse_args(
        ["redirect_uri",
         "client_id",
         "client_secret",
         "refresh_token",
         "start_date"])

    CONFIG.update(args.config)
    STATE = {}
    FORMS_TO_GET_SUBMISSIONS.update(CONFIG["form_to_get_submissions"])

    if args.state:
        STATE.update(args.state)

    if args.discover:
        do_discover()
    elif args.properties:
        do_sync(STATE, args.properties)
    else:
        LOGGER.info("No properties were selected")

def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc

if __name__ == '__main__':
    main()
