import datetime
import logging
import sys
import time
from base64 import b64decode

import httplib2
import requests

from redash import settings, models
from redash.query_runner import *
from redash.utils import json_dumps, json_loads
from redash import models

logger = logging.getLogger(__name__)

try:
    import apiclient.errors
    from apiclient.discovery import build
    from apiclient.errors import HttpError
    from oauth2client.service_account import ServiceAccountCredentials
    from oauth2client.client import AccessTokenCredentials

    enabled = True
except ImportError:
    enabled = False

types_map = {
    'INTEGER': TYPE_INTEGER,
    'FLOAT': TYPE_FLOAT,
    'BOOLEAN': TYPE_BOOLEAN,
    'STRING': TYPE_STRING,
    'TIMESTAMP': TYPE_DATETIME,
}


def transform_cell(field_type, cell_value):
    if cell_value is None:
        return None
    if field_type == 'INTEGER':
        return int(cell_value)
    elif field_type == 'FLOAT':
        return float(cell_value)
    elif field_type == 'BOOLEAN':
        return cell_value.lower() == "true"
    elif field_type == 'TIMESTAMP':
        return datetime.datetime.fromtimestamp(float(cell_value))
    return cell_value


def transform_row(row, fields):
    row_data = {}

    for column_index, cell in enumerate(row["f"]):
        field = fields[column_index]
        if field.get('mode') == 'REPEATED':
            cell_value = [transform_cell(field['type'], item['v']) for item in cell['v']]
        else:
            cell_value = transform_cell(field['type'], cell['v'])

        row_data[field["name"]] = cell_value

    return row_data


def _load_key(filename):
    f = file(filename, "rb")
    try:
        return f.read()
    finally:
        f.close()


def _get_query_results(jobs, project_id, location, job_id, start_index):
    query_reply = jobs.getQueryResults(projectId=project_id,
                                       location=location,
                                       jobId=job_id,
                                       startIndex=start_index).execute()
    logging.debug('query_reply %s', query_reply)
    if not query_reply['jobComplete']:
        time.sleep(10)
        return _get_query_results(jobs, project_id, location, job_id, start_index)

    return query_reply


class BigQuery(BaseQueryRunner):
    should_annotate_query = False
    noop_query = "SELECT 1"

    @classmethod
    def enabled(cls):
        return enabled

    @classmethod
    def configuration_schema(cls):
        return {
            'type': 'object',
            'properties': {
                'projectId': {
                    'type': 'string',
                    'title': 'Project ID'
                },
                'clientId': {
                    'type': 'string',
                    'title': 'Client ID'
                },
                'clientSecret': {
                    'type': 'string',
                    'title': 'Client Secret'
                },
                'totalMBytesProcessedLimit': {
                    "type": "number",
                    'title': 'Scanned Data Limit (MB)'
                },
                'userDefinedFunctionResourceUri': {
                    "type": "string",
                    'title': 'UDF Source URIs (i.e. gs://bucket/date_utils.js, gs://bucket/string_utils.js )'
                },
                'useStandardSql': {
                    "type": "boolean",
                    'title': "Use Standard SQL",
                    "default": True,
                },
                'location': {
                    "type": "string",
                    "title": "Processing Location",
                },
                'loadSchema': {
                    "type": "boolean",
                    "title": "Load Schema"
                },
                'maximumBillingTier': {
                    "type": "number",
                    "title": "Maximum Billing Tier"
                }
            },
            'required': ['projectId', 'clientId', 'clientSecret'],
            "order": ['projectId', 'loadSchema', 'useStandardSql', 'clientId', 'clientSecret', 'location',
                      'totalMBytesProcessedLimit', 'maximumBillingTier', 'userDefinedFunctionResourceUri'],
        }

    def _get_bigquery_service(self, user):

        client_id = self.configuration["clientId"]
        client_secret = self.configuration["clientSecret"]
        project_id = self.configuration["projectId"]

        if project_id not in user.credentials:
            return None

        refresh_token = user.fetch_credentials(project_id)

        params = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token
        }
        authorization_url = "https://www.googleapis.com/oauth2/v4/token"
        r = requests.post(authorization_url, data=params)
        if r.ok:
            tok = r.json()['access_token']
        else:
            return None

        creds = AccessTokenCredentials(tok,
                                       'my-user-agent/1.0')
        http = httplib2.Http(timeout=settings.BIGQUERY_HTTP_TIMEOUT)
        http = creds.authorize(http)

        return build("bigquery", "v2", http=http)

    def _get_project_id(self):
        return self.configuration["projectId"]

    def _get_location(self):
        return self.configuration.get("location")

    def _get_total_bytes_processed(self, jobs, query):
        job_data = {
            "query": query,
            "dryRun": True,
        }

        if self._get_location():
            job_data['location'] = self._get_location()

        if self.configuration.get('useStandardSql', False):
            job_data['useLegacySql'] = False

        response = jobs.query(projectId=self._get_project_id(), body=job_data).execute()
        return int(response["totalBytesProcessed"])

    def _get_job_data(self, query):
        job_data = {
            "configuration": {
                "query": {
                    "query": query,
                }
            }
        }

        if self._get_location():
            job_data['jobReference'] = {
                'location': self._get_location()
            }

        if self.configuration.get('useStandardSql', False):
            job_data['configuration']['query']['useLegacySql'] = False

        if self.configuration.get('userDefinedFunctionResourceUri'):
            resource_uris = self.configuration["userDefinedFunctionResourceUri"].split(',')
            job_data["configuration"]["query"]["userDefinedFunctionResources"] = map(
                lambda resource_uri: {"resourceUri": resource_uri}, resource_uris)

        if "maximumBillingTier" in self.configuration:
            job_data["configuration"]["query"]["maximumBillingTier"] = self.configuration["maximumBillingTier"]

        return job_data

    def _get_query_result(self, jobs, query):
        project_id = self._get_project_id()
        job_data = self._get_job_data(query)
        insert_response = jobs.insert(projectId=project_id, body=job_data).execute()
        current_row = 0
        query_reply = _get_query_results(jobs, project_id=project_id, location=self._get_location(),
                                         job_id=insert_response['jobReference']['jobId'], start_index=current_row)

        logger.debug("bigquery replied: %s", query_reply)

        rows = []

        while ("rows" in query_reply) and current_row < query_reply['totalRows']:
            for row in query_reply["rows"]:
                rows.append(transform_row(row, query_reply["schema"]["fields"]))

            current_row += len(query_reply['rows'])

            query_result_request = {
                'projectId': project_id,
                'jobId': query_reply['jobReference']['jobId'],
                'startIndex': current_row
            }

            if self._get_location():
                query_result_request['location'] = self._get_location()

            query_reply = jobs.getQueryResults(**query_result_request).execute()

        columns = [{
            'name': f["name"],
            'friendly_name': f["name"],
            'type': "string" if f.get('mode') == "REPEATED"
            else types_map.get(f['type'], "string")
        } for f in query_reply["schema"]["fields"]]

        data = {
            "columns": columns,
            "rows": rows,
            'metadata': {'data_scanned': int(query_reply['totalBytesProcessed'])}
        }

        return data

    def _get_columns_schema(self, table_data):
        columns = []
        for column in table_data.get('schema', {}).get('fields', []):
            columns.extend(self._get_columns_schema_column(column))

        project_id = self._get_project_id()
        table_name = table_data['id'].replace("%s:" % project_id, "")
        return {'name': table_name, 'columns': columns}

    def _get_columns_schema_column(self, column):
        columns = []
        if column['type'] == 'RECORD':
            for field in column['fields']:
                columns.append(u"{}.{}".format(column['name'], field['name']))
        else:
            columns.append(column['name'])

        return columns

    def get_mbs_processed(self, query, user):
        bigquery_service = self._get_bigquery_service(user)
        if bigquery_service is None:
            return None, "User credentials is expired. Please click on Auth button to get new credentials."
        jobs = bigquery_service.jobs()

        try:
            processed_mb = self._get_total_bytes_processed(jobs, query) / 1000.0 / 1000.0
            error = None
            json_data = {'processedMBs': round(processed_mb, 1)}
        except apiclient.errors.HttpError as e:
            json_data = None
            error = json_loads(e.content)

        return json_data, error

    def get_schema(self, user, get_stats=False):
        if not self.configuration.get('loadSchema', False):
            return []

        service = self._get_bigquery_service(user)

        if service is None:
            return

        project_id = self._get_project_id()
        datasets = service.datasets().list(projectId=project_id).execute()
        schema = []
        for dataset in datasets.get('datasets', []):
            dataset_id = dataset['datasetReference']['datasetId']
            tables = service.tables().list(projectId=project_id, datasetId=dataset_id).execute()
            while True:
                for table in tables.get('tables', []):
                    table_data = service.tables().get(projectId=project_id,
                                                      datasetId=dataset_id,
                                                      tableId=table['tableReference']['tableId']).execute()
                    table_schema = self._get_columns_schema(table_data)
                    schema.append(table_schema)

                next_token = tables.get('nextPageToken', None)
                if next_token is None:
                    break

                tables = service.tables().list(projectId=project_id,
                                               datasetId=dataset_id,
                                               pageToken=next_token).execute()

        return schema

    def run_query(self, query, user):
        logger.debug("BigQuery got query: %s", query)

        bigquery_service = self._get_bigquery_service(user)
        if bigquery_service is None:
            return None, "User credentials is expired. Please click on Auth button to get new credentials."
        jobs = bigquery_service.jobs()

        try:
            if "totalMBytesProcessedLimit" in self.configuration:
                limitMB = self.configuration["totalMBytesProcessedLimit"]
                processedMB = self._get_total_bytes_processed(jobs, query) / 1000.0 / 1000.0
                if limitMB < processedMB:
                    return None, "Larger than %d MBytes will be processed (%f MBytes)" % (limitMB, processedMB)

            data = self._get_query_result(jobs, query)
            error = None

            json_data = json_dumps(data, ignore_nan=True)
        except apiclient.errors.HttpError as e:
            json_data = None
            if e.resp.status == 400:
                error = json_loads(e.content)['error']['message']
            else:
                error = e.content
        except KeyboardInterrupt:
            error = "Query cancelled by user."
            json_data = None

        return json_data, error


register(BigQuery)
