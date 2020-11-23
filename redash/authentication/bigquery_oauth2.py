import logging
from flask import url_for, Blueprint, request, redirect
from flask_oauthlib.client import OAuth
from redash import models
from flask_login import login_required, current_user
from redash.authentication import current_org
from redash.query_runner import big_query
import requests
import json

logger = logging.getLogger('bigquery_oauth2')

blueprint = Blueprint('bigquery_oauth2', __name__)


def get_google_oauth(clientid, clientsecret, dsid=None) :
    oauth = OAuth()
    google = oauth.remote_app(
        'google',
        consumer_key=clientid,
        consumer_secret=clientsecret,
        request_token_params={
            'scope': 'https://www.googleapis.com/auth/bigquery https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/gmail.labels',
            'state' : dsid,
            'access_type' : 'offline',
            'prompt': 'consent',
        },
        base_url='https://www.googleapis.com/oauth2/v1/',
        request_token_url=None,
        access_token_method='POST',
        access_token_url='https://accounts.google.com/o/oauth2/token',
        authorize_url='https://accounts.google.com/o/oauth2/auth',
    )
    return google


@blueprint.route('/bqauthorize/<dsid>', endpoint="bqauthorize")
@login_required
def bqauthorize(dsid):
    data_source = models.DataSource.get_by_id_and_org(dsid, current_org)
    ds = data_source.to_dict(all=True)
    client_id = ds["options"]["clientId"]
    client_secret = ds["options"]["clientSecret"]
    google = get_google_oauth(client_id, client_secret, dsid)
    return google.authorize(callback=url_for(".bqauthorized", _external=True))


@blueprint.route('/bqauthorized', endpoint="bqauthorized")
@login_required
def bqauthorized():
    dsid = request.args['state']
    data_source = models.DataSource.get_by_id_and_org(dsid, current_org)
    ds = data_source.to_dict(all=True)
    client_id = ds["options"]["clientId"]
    client_secret = ds["options"]["clientSecret"]
    project_id = ds["options"]["projectId"]
    google = get_google_oauth(client_id, client_secret)
    resp = google.authorized_response()
    if resp is None:
        return 'Access denied: reason=%s error=%s' % (
            request.args['error_reason'],
            request.args['error_description']
        )
    current_user.update_credentials(project_id, resp["refresh_token"])

    return redirect(url_for("redash.index"))
