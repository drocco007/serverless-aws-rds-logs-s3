# serverless-aws-rds-logs-s3

A serverless example demonstrating a periodic scheduled task to ship logs
from RDS to S3.

Quick start:

* `npm install -g serverless`
* `npm install`
* edit `serverless.yml` with the name of your RDS instance
* `sls deploy --stage live`


# Introduction

For this application, we'd like to build a system to preserve log files
from AWS RDS into an S3 bucket. Logs are available in RDS itself for a
limited period of time; although RDS offers streaming to CloudWatch for
more durable storage, not all RDS backend versions support it. S3
offers highly reliable, configurable storage and a convenient interface
if we wish to perform our own analysis of the logs.

This example assumes an existing RDS instance, to which we'll add

* an S3 bucket to store our log files
* a Python service, executed on a schedule, to sync the logs from RDS
  to the S3 bucket
* an IAM Role granting permission to the service to read the RDS logs
  and read and write to the bucket

We'll also use the [serverless-python-requirements plugin](https://serverless.com/plugins/serverless-python-requirements/) to manage the external Python libraries
our service requires.


# A Digression

The RDS SDKs (e.g. `boto3` for Python) advertise a function
[`download_db_log_file_portion`](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/rds.html#RDS.Client.download_db_log_file_portion)
for retrieving RDS log files. This function, unfortunately, has a somewhat
awkward interface: the paging interval specified by the API is expressed
as some number of lines, but the service _also_ imposes an absolute size
limit on the payload. If the requested number of lines causes the data
maximum to be breached, the service truncates the last line returned and
inserts a warning marker into the data stream.

One _could_ write code like this to circumvent this problem

```python
    TRUNCATE_MARKER='[Your log message was truncated]'

    def _get_chunk(self, log_file, marker='0', n_lines=3000, retry=True):
        record = rds.download_db_log_file_portion(
            DBInstanceIdentifier=self.db_name,
            LogFileName=log_file,
            Marker=marker,
            NumberOfLines=n_lines,
        )

        # If RDS hits its max log download size in the middle of a line,
        # it truncates the line to fit (!!). If we detect a truncated line,
        # retry with fewer lines, which should allow the fetch to succeed
        # without truncating.
        if retry and TRUNCATE_MARKER in record['LogFileData'][-100:]:
            return self._get_chunk(log_file, marker=marker,
                                   n_lines=n_lines // 2, retry=False)
        else:
            return record
```

but on balance, one would rather not!

A more serious concern involves multibyte character sets: log files that
[contain non-ASCII characters _cannot_ be pulled reliably by the AWS SDKs](https://github.com/aws/aws-cli/issues/2724).

RDS quietly advertises a [REST interface for downloading an entire
RDS log file in one step](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_LogAccess.html#DownloadCompleteDBLogFile); indeed, this is the same interface that
the AWS management console invokes if you download an entire log file via
the web interface! Our service will utilize this REST endpoint for fetching
logs from RDS; see the end of this document for additional discussion.


# `serverless` Configuration

## The S3 bucket

First we define a new S3 bucket resource.

```yaml
    resources:
      Resources:
        RDSLogBucket:
          Type: AWS::S3::Bucket
          Properties:
            BucketName: !Join
              - "-"
              - - !Ref AWS::AccountId
                - "${self:custom.bucket.${self:provider.stage}}"
                - !Ref AWS::Region

    custom:
      bucket:
        live: prod-rds-logs
        stage: stage-rds-logs
```

As a convenience, we also define a custom `bucket` attribute to hold a
configurable portion of our S3 bucket name; during the deploy, CloudFormation
will concatenate this with the AWS account ID and region to produce the
final bucket name in S3; adjust this to suit local preference or policy.


## The function configuration

Our function configuration specifies the handler and other Lambda parameters
as usual, but also

* collects the RDS DB name and the target S3 bucket, which will be passed
  to the function in two environment variables at runtime

* specifies a [_schedule_ event](https://serverless.com/framework/docs/providers/aws/events/schedule/) to trigger the function at intervals of
  5 minutes (think `cron`)

```yaml
    functions:
      sync_s3:
        handler: handler.sync_s3
        timeout: 600
        memorySize: 256
        environment:
          DBNAME: ${self:custom.db.${self:provider.stage}}
          TARGET_BUCKET: !Ref RDSLogBucket
        events:
         - schedule: rate(5 minutes)

    custom:
      db:
        live: prod-rds
        stage: stage-rds

```


## Additional IAM permissions

We also need to give our function permission to read the RDS logs and
read and write the S3 bucket, so we'll add those policies to the
execution role created by Serverless when we deploy:

```yaml
    provider:
      name: aws
      runtime: python3.8
      stage: ${opt:stage, "stage"}
      iamRoleStatements:
        - Effect: 'Allow'
          Action:
            - 's3:ListBucket'
          Resource: !GetAtt RDSLogBucket.Arn
        - Effect: 'Allow'
          Action:
            - 's3:GetObject'
            - 's3:PutObject'
          Resource: !Join ["", [!GetAtt RDSLogBucket.Arn, "/*"]]
        - Effect: Allow
          Action:
            - rds:DownloadCompleteDBLogFile
          Resource: '*'
        - Effect: Allow
          Action:
            - rds:DescribeDBLogFiles
          Resource: !Join
            - ":"
            - - "arn:aws:rds"
              - !Ref AWS::Region
              - !Ref AWS::AccountId
              - "db"
              - ${self:custom.db.${self:provider.stage}}
```

`rds:DescribeDBLogFiles` allows the role to query RDS for the list of log
files and their sizes; we'll use that later to only sync logs that have
changed.

`rds:DownloadCompleteDBLogFile` grants access to the REST endpoint for
fetching an entire log file. Note the `Resource` specification of `*`: this
is required by IAM, so if you need to restrict the role's access to only
a subset of your RDS instances, you will need a condition statement or some
other mechanism to do so.


# The `sync_s3` function

Now that we have our Serverless configuration established, let's have a
look at the `sync_s3` function itself. In outline, the function

* retrieves the configured RDS database and S3 bucket from the execution
  environment

* retrieves the list of available log files from RDS

* compares the reported size of each log with the corresponding file in S3

* if they differ, streams the contents from RDS to S3

Here is the function in full:

```python
    def sync_s3(event, context):
        "Sync RDS logs to S3."

        # passed in from serverless
        db = os.environ['DBNAME']
        bucket = os.environ['TARGET_BUCKET']

        s3 = boto3.resource('s3')
        streamer = RDSLogStreamer(db)

        for log_file in streamer.log_files:
            obj = s3.Object(bucket, log_file.target_path)

            try:
                if obj.content_length == log_file.rds_size:
                    print(f'Skipping existing {log_file}')
                    continue
            except ClientError:
                pass  # object does not exist

            print(f'Sync {log_file}')

            f = streamer.stream(log_file)

            obj.upload_fileobj(f)
```

A couple of helper classes handle bookkeeping chores for us like collecting
the available logs and their sizes and organizing the destination bucket
into date-based directories.

To fetch a log file, we create a signed URL for the REST endpoint and have
`requests` open the URL for streaming:

```python
    @attr.s
    class RDSLogStreamer:
        def stream(self, log_file):
            signed_url = get_rds_logfile_url(self.db_name, log_file)

            response = requests.get(signed_url, stream=True)

            response.raise_for_status()

            # https://github.com/psf/requests/issues/2155
            response.raw.decode_content = True

            return response.raw
```


# Additional context: signing the REST request

Invoking the REST call to retrieve an RDS log file requires that the
request be signed with credentials for a user or role with the
`rds:DownloadCompleteDBLogFile` permission. Our goal is to use the
execution role we created as part of our deployment — and to which we
carefully added the required permission — to sign the request.

When generating a signed request for a role, we [are required to pass a
session token](https://docs.aws.amazon.com/STS/latest/APIReference/CommonParameters.html)
with our request. Fortunately, the Lambda execution environment includes
[the required keys and session token](https://forums.aws.amazon.com/thread.jspa?threadID=217933)
as environment variables

    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN

To generate the signed request, our service packages a [lightly customized
version of the SigV4 signing example](https://docs.aws.amazon.com/general/latest/gr/sigv4-signed-request-examples.html#sig-v4-examples-get-query-string), tailored
for the RDS endpoint we need and assuming role-based authentication with
a session token.
