# Copyright 2010-2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# This file is licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License. A copy of the
# License is located at
#
# http://aws.amazon.com/apache2.0/
#
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS
# OF ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
#
# ABOUT THIS PYTHON SAMPLE: This sample is part of the AWS General Reference
# Signing AWS API Requests top available at
# https://docs.aws.amazon.com/general/latest/gr/sigv4-signed-request-examples.html
#

import base64
import datetime
import hashlib
import hmac
import os
import sys
from urllib.parse import quote_plus


# Key derivation functions. See:
# http://docs.aws.amazon.com/general/latest/gr/signature-v4-examples.html#signature-v4-examples-python
def sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def getSignatureKey(key, dateStamp, regionName, serviceName):
    kDate = sign(('AWS4' + key).encode('utf-8'), dateStamp)
    kRegion = sign(kDate, regionName)
    kService = sign(kRegion, serviceName)
    kSigning = sign(kService, 'aws4_request')

    return kSigning


def get_rds_logfile_url(dbname, log_file, access_key=None, secret_key=None,
                        session_token=None, region='us-east-1'):
    if not access_key:
        access_key = os.environ.get('AWS_ACCESS_KEY_ID')

    if not secret_key:
        secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    # required for role-based (e.g. Lambda function) authentication to an
    # HTTPS service (as opposed to user-based auth.) See
    #
    # https://docs.aws.amazon.com/AmazonS3/latest/dev/RESTAuthentication.html
    # https://forums.aws.amazon.com/thread.jspa?threadID=217933
    # https://docs.aws.amazon.com/IAM/latest/UserGuide/id.html
    if not session_token:
        session_token = os.environ.get('AWS_SESSION_TOKEN')

    if not access_key or not secret_key or not session_token:
        raise ValueError('No AWS credentials')

    # ************* REQUEST VALUES *************
    method = 'GET'
    service = 'rds'
    host = f'rds.{region}.amazonaws.com'
    request_parameters = ''

    # Create a date for headers and the credential string
    t = datetime.datetime.utcnow()
    amz_date = t.strftime('%Y%m%dT%H%M%SZ') # Format date as YYYYMMDD'T'HHMMSS'Z'
    datestamp = t.strftime('%Y%m%d') # Date w/o time, used in credential scope

    # ************* TASK 1: CREATE A CANONICAL REQUEST *************
    # http://docs.aws.amazon.com/general/latest/gr/sigv4-create-canonical-request.html

    # Because almost all information is being passed in the query string,
    # the order of these steps is slightly different than examples that
    # use an authorization header.

    # Step 1: Define the verb (GET, POST, etc.)--already done.

    # Step 2: Create canonical URI--the part of the URI from domain to query
    # string (use '/' if no path)
    canonical_uri = f'/v13/downloadCompleteLogFile/{dbname}/{log_file}'
    request_url = f'https://rds.{region}.amazonaws.com{canonical_uri}'

    # Step 3: Create the canonical headers and signed headers. Header names
    # must be trimmed and lowercase, and sorted in code point order from
    # low to high. Note trailing \n in canonical_headers.
    # signed_headers is the list of headers that are being included
    # as part of the signing process. For requests that use query strings,
    # only "host" is included in the signed headers.
    canonical_headers = f'host:{host}\n'
    signed_headers = 'host'

    # Match the algorithm to the hashing algorithm you use, either SHA-1 or
    # SHA-256 (recommended)
    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = '/'.join([datestamp, region, service, 'aws4_request'])
    amz_credential = quote_plus('/'.join([access_key, credential_scope]))

    # Step 4: Create the canonical query string. In this example, request
    # parameters are in the query string. Query string values must
    # be URL-encoded (space=%20). The parameters must be sorted by name.
    canonical_querystring = '&'.join([
        'X-Amz-Algorithm=AWS4-HMAC-SHA256',
        f'X-Amz-Credential={amz_credential}',
        f'X-Amz-Date={amz_date}',
        'X-Amz-Expires=300',
        f'X-Amz-Security-Token={quote_plus(session_token)}',
        f'X-Amz-SignedHeaders={signed_headers}'
    ])

    # Step 5: Create payload hash. For GET requests, the payload is an
    # empty string ("").
    payload_hash = hashlib.sha256(('').encode('utf-8')).hexdigest()

    # Step 6: Combine elements to create canonical request
    canonical_request = '\n'.join([
        method, canonical_uri, canonical_querystring, canonical_headers,
        signed_headers, payload_hash
    ])

    # ************* TASK 2: CREATE THE STRING TO SIGN*************
    string_to_sign = '\n'.join([
        algorithm, amz_date, credential_scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()
    ])

    # ************* TASK 3: CALCULATE THE SIGNATURE *************
    # Create the signing key
    signing_key = getSignatureKey(secret_key, datestamp, region, service)

    # Sign the string_to_sign using the signing_key
    signature = hmac.new(
        signing_key,
        string_to_sign.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # ************* TASK 4: ADD SIGNING INFORMATION TO THE REQUEST *************
    # The auth information can be either in a query string
    # value or in a header named Authorization. This code shows how to put
    # everything into a query string.
    canonical_querystring += '&X-Amz-Signature=' + signature

    request_url = f'{request_url}?{canonical_querystring}'

    return request_url


if __name__ == '__main__':
    try:
        print(get_rds_logfile_url(sys.argv[1], sys.argv[2]))
    except IndexError:
        print(f'usage: {sys.argv[0]} <rds db name> <log file>')
