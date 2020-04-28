import os

import attr
import boto3
from botocore.exceptions import ClientError
import requests

from awssigner import get_rds_logfile_url


rds = boto3.client('rds')


def target_path(log_file):
    """Return a target location for an RDS log file.

    Given an RDS log file name ('error/postgresql.log.2020-03-03-21'),
    return a 2-tuple ('error/<date>/', 'postgresql.log.2020-03-03-21')
    representing the target path and filename to save the file.

    """

    date = log_file.rsplit('.', 1)[-1].rsplit('-', 1)[0]
    path, name = log_file.rsplit('/', 1)

    return f'{path}/{date}/', name


@attr.s
class LogFile:
    """An RDS log file.

    rds_name is the full name of the log file from RDS, e.g.
    'error/postgresql.log.2020-03-03-21'.

    rds_size is the size of the log file as reported by RDS.

    The property 'target_path' yields the full target path for
    saving this log file, assuming log files grouped by date.

    """

    rds_name = attr.ib()
    rds_size = attr.ib()

    @property
    def target_path(self):
        return ''.join(target_path(self.rds_name))


@attr.s
class RDSLogStreamer:
    """Manage fetching log files from a given RDS instance.

    db_name should be the name of the DB instance in RDS, e.g.
    'prod-crucible-clarus-postgres-master-v00'.

    """

    db_name = attr.ib()

    @property
    def log_files(self):
        """List of available log file names and thier sizes.

        Returns a list of 2-tuples (log_file_name, size) of the available
        log files for this RDS instance.

        """

        try:
            return self._log_files
        except AttributeError:
            self._log_files = [
                LogFile(log_file['LogFileName'], log_file['Size'])
                for log_file in rds.describe_db_log_files(
                    DBInstanceIdentifier=self.db_name
                )['DescribeDBLogFiles']
            ]

            return self._log_files

    def stream(self, log_file):
        """Fetch a log file from RDS.

        Given a log file name associated with this RDS instance or a LogFile
        metadata instance representing an RDS log file, fetch the
        contents of the flie.

        Generator that yields the log file data in chunks as it is read
        from RDS.

        """

        try:
            log_file = log_file.rds_name
        except AttributeError:
            pass

        signed_url = get_rds_logfile_url(self.db_name, log_file)

        response = requests.get(signed_url, stream=True)

        response.raise_for_status()

        # https://github.com/psf/requests/issues/2155
        response.raw.decode_content = True

        return response.raw


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


def main():
    "Default: sync RDS logs to local filesystem"

    import os

    streamer = RDSLogStreamer('prod-crucible-clarus-postgres-master-v00')

    known_files = [
        (log_file.target_path, os.path.getsize(log_file.target_path))
        for log_file in streamer.log_files
        if os.path.exists(log_file.target_path)
    ]

    log_files_to_sync = [
        log_file for log_file in streamer.log_files
        if (log_file.target_path, log_file.rds_size) not in known_files
    ]

    print(log_files_to_sync)

    for log_file in log_files_to_sync:
        os.makedirs(os.path.dirname(log_file.target_path), exist_ok=True)

        with open(log_file.target_path, 'wb') as f:
            _logfile = streamer.stream(log_file)
            f.write(_logfile.read())


if __name__ == '__main__':
    main()
