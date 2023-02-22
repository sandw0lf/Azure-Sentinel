import json
import os
from google.cloud.logging_v2 import Client
import datetime
import logging
import re
import time
import azure.functions as func
from dateutil.parser import parse as parse_date

from .sentinel_connector import AzureSentinelConnector
from .state_manager import StateManager


CREDENTIALS_FILE_CONTENT = os.environ['CREDENTIALS_FILE_CONTENT']
RESOURCE_NAMES = os.environ['RESOURCE_NAMES']
WORKSPACE_ID = os.environ['WORKSPACE_ID']
SHARED_KEY = os.environ['SHARED_KEY']
LOG_TYPE = 'GCP_IAM'


# interval of script execution
SCRIPT_EXECUTION_INTERVAL_MINUTES = 5
# if ts of last processed file is older than "now - MAX_PERIOD_MINUTES" then script will get events from "now - MAX_PERIOD_MINUTES"
MAX_PERIOD_MINUTES = 60 * 24 * 7

MAX_SCRIPT_EXEC_TIME_MINUTES = 5


LOG_ANALYTICS_URI = os.environ.get('logAnalyticsUri')

if not LOG_ANALYTICS_URI or str(LOG_ANALYTICS_URI).isspace():
    LOG_ANALYTICS_URI = 'https://' + WORKSPACE_ID + '.ods.opinsights.azure.com'

pattern = r'https:\/\/([\w\-]+)\.ods\.opinsights\.azure.([a-zA-Z\.]+)$'
match = re.match(pattern, str(LOG_ANALYTICS_URI))
if not match:
    raise Exception("Invalid Log Analytics Uri.")


CREDS_FILE_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), '.creds')


logging.getLogger('azure.core.pipeline.policies.http_logging_policy').setLevel(logging.ERROR)


def main(mytimer: func.TimerRequest):
    logging.info('Starting script')

    start_ts = int(time.time())

    create_credentials_file()

    gcp_cli = Client(_use_grpc=False)
    sentinel = AzureSentinelConnector(log_analytics_uri=LOG_ANALYTICS_URI, workspace_id=WORKSPACE_ID, shared_key=SHARED_KEY, log_type=LOG_TYPE, queue_size=3000)
    state_manager = StateManager(os.environ['AzureWebJobsStorage'])

    last_ts = get_last_ts(state_manager)

    filt = """
        protoPayload.methodName:(
            "CreateRole" OR
            "DeleteRole" OR
            "UndeleteRole" OR
            "UpdateRole" OR
            "CreateServiceAccount" OR
            "DeleteServiceAccount" OR
            "DisableServiceAccount" OR
            "EnableServiceAccount" OR
            "GetServiceAccount" OR
            "PatchServiceAccount" OR
            "SetIAMPolicy" OR
            "UndeleteServiceAccount" OR
            "UpdateServiceAccount" OR
            "CreateServiceAccountKey" OR
            "DeleteServiceAccountKey" OR
            "UploadServiceAccountKey" OR
            "CreateWorkloadIdentityPool" OR
            "DeleteWorkloadIdentityPool" OR
            "UndeleteWorkloadIdentityPool" OR
            "UpdateWorkloadIdentityPool" OR
            "CreateWorkloadIdentityPoolProvider" OR
            "DeleteWorkloadIdentityPoolProvider" OR
            "UndeleteWorkloadIdentityPoolProvider" OR
            "UpdateWorkloadIdentityPoolProvider" OR
            "ExchangeToken" OR
            "GetRole" OR
            "ListRoles" OR
            "QueryGrantableRoles" OR
            "GetEffectivePolicy" OR
            "GenerateAccessToken" OR
            "GenerateIdToken" OR
            "ListServiceAccounts" OR
            "SignBlob" OR
            "SignJwt" OR
            "GetServiceAccountKey" OR
            "ListServiceAccountKeys" OR
            "GetWorkloadIdentityPool" OR
            "ListWorkloadIdentityPools" OR
            "GetWorkloadIdentityPoolProvider" OR
            "ListWorkloadIdentityPoolProviders"
        ) AND
        timestamp>="{}"
    """.format(last_ts)

# Set the batch size and page size for pagination
    batch_size = 10
    page_size = 1000

# Get the list of resources and split them into batches
    resources = get_recource_names()

    resource_batches = [resources[i:i+batch_size] for i in range(0, len(resources), batch_size)]
    logging.info('Processing {} resource batches'.format(len(resource_batches)))

    last_max_ts = None
    with sentinel:
        for resource_batch in resource_batches:
            resource_filter = ' OR '.join(['resource.type="{}" AND resource.labels.project_id="{}"'.format(resource_type, project_id) for resource_type, project_id in [resource.split('/') for resource in resource_batch]])
            batch_filt = '{} AND ({})'.format(filt, resource_filter)
            logging.info('Processing batch with filter: {}'.format(batch_filt))
             # Initialize the page token to None to start from the beginning

            page_token = None
            while True:
                # Call the list_entries() function with the page token and page size
                results = gcp_cli.list_entries(filter_=batch_filt, order_by='timestamp', page_size=page_size, page_token=page_token)

                # Iterate through the results and process each event
                for entry in results:
                    event = parse_entry(entry)
                    sentinel.send(event)

                    last_ts = event['timestamp']
                    if last_max_ts is None or last_ts > last_max_ts:
                        last_max_ts = last_ts

                    if sentinel.is_empty():
                        logging.info('Saving last max timestamp - {}'.format(last_max_ts))
                        state_manager.post(last_max_ts)
                        if check_if_script_runs_too_long(start_ts):
                            logging.info('Script is running too long. Saving progress and exit.')
                            break

                # If the page token is None, there are no more pages to process
                if not results.next_page_token:
                    break

                # Update the page token to the next page
                page_token = results.next_page_token

            if sentinel.is_empty() and last_max_ts:
                logging.info('Saving max last timestamp - {}'.format(last_max_ts))
                state_manager.post(last_max_ts)

            if check_if_script_runs_too_long(start_ts):
                logging.info('Script is running too long. Saving progress and exit.')
                break

    remove_credentials_file()
    logging.info('Script finished. Sent events number: {}'.format(sentinel.successfull_sent_events_number))


def create_credentials_file():
    with open(CREDS_FILE_PATH, 'w') as f:
        content = CREDENTIALS_FILE_CONTENT.strip().replace('\n', '\\n')
        f.write(content)
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = CREDS_FILE_PATH


def remove_credentials_file():
    if os.path.exists(CREDS_FILE_PATH):
        os.remove(CREDS_FILE_PATH)


def get_recource_names():
    return [x for x in RESOURCE_NAMES.split(',') if x]


def parse_entry(entry):
    event = entry._asdict()
    if isinstance(event.get('timestamp'), datetime.datetime):
        event['timestamp'] = event['timestamp'].isoformat()
    if 'logger' in event:
        del event['logger']
    if event.get('resource'):
        event['resource'] = event['resource']._asdict()
    return event


def get_last_ts(state_manager: StateManager):
    logging.info('Getting last timestamp')
    last_ts = state_manager.get()
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    if last_ts:
        last_ts = parse_date(last_ts)
        logging.info('Last timestamp - {}'.format(last_ts.isoformat()))
    else:
        last_ts = now - datetime.timedelta(minutes=SCRIPT_EXECUTION_INTERVAL_MINUTES)
        logging.info('Last timestamp is not known')

    diff_seconds = (now - last_ts).days * 86400 + (now - last_ts).seconds
    if diff_seconds > MAX_PERIOD_MINUTES * 60:
        old_last_ts = last_ts
        last_ts = now - datetime.timedelta(minutes=MAX_PERIOD_MINUTES)
        logging.info('Last timestamp {} is older than max search period ({} minutes). Getting data for max search period (from {})'.format(old_last_ts, MAX_PERIOD_MINUTES, last_ts))
    else:
        last_ts += datetime.timedelta(microseconds=1)
        logging.info('Getting data from {}'.format(last_ts.isoformat()))

    return last_ts.isoformat()


def check_if_script_runs_too_long(start_ts):
    now = int(time.time())
    duration = now - start_ts
    max_duration = int(MAX_SCRIPT_EXEC_TIME_MINUTES * 60 * 0.85)
    return duration > max_duration
