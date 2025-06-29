# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for project_setup."""
import ast
import copy
import json
import os
import posixpath
import unittest
from unittest import mock

from google.cloud import ndb
import googleapiclient

from clusterfuzz._internal.base import utils
from clusterfuzz._internal.cron import project_setup
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.google_cloud_utils import pubsub
from clusterfuzz._internal.tests.test_libs import helpers
from clusterfuzz._internal.tests.test_libs import mock_config
from clusterfuzz._internal.tests.test_libs import test_utils

DATA_DIRECTORY = os.path.join(os.path.dirname(__file__), 'project_setup_data')

EXISTING_BUCKETS = {'lib1-logs.clusterfuzz-external.appspot.com'}


def _read_data_file(data_file):
  """Helper function to read the contents of a data file."""
  with open(
      os.path.join(DATA_DIRECTORY, data_file), encoding='utf-8') as handle:
    return handle.read()


class MockRequest:
  """Mock API request."""

  def __init__(self, raise_exception=False, return_value=None):
    self.raise_exception = raise_exception
    self.return_value = return_value

  def execute(self):
    """Mock execute()."""
    if self.raise_exception:
      raise googleapiclient.errors.HttpError(mock.Mock(status=404), b'')

    return self.return_value


def mock_bucket_get(bucket=None):
  """Mock buckets().get()."""
  if bucket in EXISTING_BUCKETS:
    return MockRequest(False, {'name': 'bucket'})

  return MockRequest(True)


def mock_get_iam_policy(bucket=None):
  """Mock buckets().getIamPolicy()."""
  response = {
      'kind': 'storage#policy',
      'resourceId': 'fake',
      'bindings': [],
      'etag': 'fake'
  }

  if bucket in ('lib1-logs.clusterfuzz-external.appspot.com',
                'lib3-logs.clusterfuzz-external.appspot.com'):
    response['bindings'].append({
        'role': 'roles/storage.objectViewer',
        'members': ['user:user@example.com',]
    })

  return MockRequest(return_value=response)


class CopyingMock(mock.MagicMock):
  """MagicMock that copies arguments."""

  def __call__(self, *args, **kwargs):
    args = copy.deepcopy(args)
    kwargs = copy.deepcopy(kwargs)
    return super().__call__(*args, **kwargs)


def mock_set_iam_policy(bucket=None, body=None):  # pylint: disable=unused-argument
  """Mock buckets().setIamPolicy()."""
  bindings = body['bindings']
  if bindings and 'user:primary@example.com' in bindings[0]['members']:
    return MockRequest(raise_exception=True)

  return MockRequest(return_value=copy.deepcopy(body))


def _mock_get_or_create_service_account(project):
  return {
      'email': project + '@serviceaccount.com',
  }, False


@test_utils.with_cloud_emulators('datastore', 'pubsub')
class OssFuzzProjectSetupTest(unittest.TestCase):
  """Test project_setup for OSS-Fuzz."""

  def setUp(self):
    self.maxDiff = None

    helpers.patch_environ(self)

    data_types.Job(
        name='libfuzzer_asan_old_job',
        environment_string=('MANAGED = True\n'
                            'PROJECT_NAME = old\n')).put()
    data_types.Job(
        name='libfuzzer_msan_old_job',
        environment_string=('MANAGED = True\n'
                            'PROJECT_NAME = old\n')).put()
    data_types.Job(
        name='afl_asan_old_job',
        environment_string=('MANAGED = True\n'
                            'PROJECT_NAME = old\n')).put()
    data_types.Job(
        name='afl_msan_old_job',
        environment_string=('MANAGED = True\n'
                            'PROJECT_NAME = old\n')).put()
    data_types.Job(name='unmanaged_job', environment_string='').put()

    # Will be removed.
    data_types.ExternalUserPermission(
        entity_kind=data_types.PermissionEntityKind.JOB,
        is_prefix=False,
        auto_cc=data_types.AutoCCType.ALL,
        entity_name='libfuzzer_asan_lib1',
        email='willberemoved@example.com').put()

    # Existing CC. Makes sure no duplicates are created.
    data_types.ExternalUserPermission(
        entity_kind=data_types.PermissionEntityKind.JOB,
        is_prefix=False,
        auto_cc=data_types.AutoCCType.ALL,
        entity_name='libfuzzer_asan_lib1',
        email='primary@example.com').put()

    # Existing project settings. Should not get modified.
    # Also test disk size.
    data_types.OssFuzzProject(
        id='lib1', name='lib1', cpu_weight=1.5, disk_size_gb=500).put()

    # Should get deleted.
    data_types.OssFuzzProject(id='old_lib', name='old_lib').put()

    self.libfuzzer = data_types.Fuzzer(name='libFuzzer', jobs=[])
    self.libfuzzer.data_bundle_name = 'global'
    self.libfuzzer.jobs = ['libfuzzer_asan_old_job', 'libfuzzer_msan_old_job']
    self.libfuzzer.put()

    self.afl = data_types.Fuzzer(name='afl', jobs=[])
    self.afl.data_bundle_name = 'global'
    self.afl.jobs = ['afl_asan_old_job', 'afl_msan_old_job']
    self.afl.put()

    self.honggfuzz = data_types.Fuzzer(name='honggfuzz', jobs=[])
    self.honggfuzz.data_bundle_name = 'global'
    self.honggfuzz.put()

    self.gft = data_types.Fuzzer(name='googlefuzztest', jobs=[])
    self.gft.put()

    self.centipede = data_types.Fuzzer(name='centipede', jobs=[])
    self.centipede.data_bundle_name = 'global'
    self.centipede.put()

    helpers.patch(self, [
        'clusterfuzz._internal.config.local_config.ProjectConfig',
        ('get_application_id_2',
         'clusterfuzz._internal.base.utils.get_application_id'),
        'clusterfuzz._internal.google_cloud_utils.storage.build',
        'time.sleep',
        'handlers.base_handler.Handler.is_cron',
        'clusterfuzz._internal.cron.project_setup.get_oss_fuzz_projects',
        'clusterfuzz._internal.cron.service_accounts.get_or_create_service_account',
        'clusterfuzz._internal.cron.service_accounts.set_service_account_roles',
    ])

    self.mock.get_or_create_service_account.side_effect = (
        _mock_get_or_create_service_account)

    self.mock.ProjectConfig.return_value = mock_config.MockConfig({
        'segregate_projects':
            True,
        'project_setup': [{
            'source': 'oss-fuzz',
            'build_type': 'RELEASE_BUILD_BUCKET_PATH',
            'add_info_labels': True,
            'add_revision_mappings': True,
            'build_buckets': {
                'afl': 'clusterfuzz-builds-afl',
                'centipede': 'clusterfuzz-builds-centipede',
                'honggfuzz': 'clusterfuzz-builds-honggfuzz',
                'libfuzzer': 'clusterfuzz-builds',
                'libfuzzer_i386': 'clusterfuzz-builds-i386',
                'no_engine': 'clusterfuzz-builds-no-engine',
            }
        }]
    })

  def test_execute(self):
    """Tests executing of cron job."""
    mock_storage = mock.MagicMock()
    mock_storage.buckets().insert().execute.return_value = 'timeCreated'
    self.mock.get_application_id_2.return_value = 'clusterfuzz-external'
    self.mock.build.return_value = mock_storage

    pubsub_client = pubsub.PubSubClient()
    app_id = utils.get_application_id()
    unmanaged_topic_name = pubsub.topic_name(app_id, 'jobs-linux')
    old_topic_name = pubsub.topic_name(app_id, 'jobs-shouldbedeleted')
    old_subscription_name = pubsub.subscription_name(app_id,
                                                     'jobs-shouldbedeleted')
    other_topic_name = pubsub.topic_name(app_id, 'other')

    pubsub_client.create_topic(unmanaged_topic_name)
    pubsub_client.create_topic(old_topic_name)
    pubsub_client.create_topic(other_topic_name)
    pubsub_client.create_subscription(old_subscription_name, old_topic_name)

    self.mock.get_oss_fuzz_projects.return_value = [
        ('lib1', {
            'homepage': 'http://example.com',
            'primary_contact': 'primary@example.com',
            'auto_ccs': [
                'User@example.com',
                'user2@googlemail.com',
            ],
            'vendor_ccs': None,
        }),
        ('lib2', {
            'homepage': 'http://example2.com',
            'disabled': True,
            'fuzzing_engines': ['libfuzzer',],
        }),
        ('lib3', {
            'homepage':
                'http://example3.com',
            'sanitizers': [
                'address',
                {
                    'memory': {
                        'experimental': True,
                    },
                },
                'undefined',
            ],
            'auto_ccs':
                'User@example.com',
            'disabled':
                False,
            'fuzzing_engines': ['libfuzzer',],
            'view_restrictions':
                'none',
            'architectures': ['i386', 'x86_64'],
        }),
        ('lib4', {
            'homepage': 'http://example4.com',
            'language': 'go',
            'sanitizers': ['address'],
            'auto_ccs': 'User@example.com',
            'fuzzing_engines': ['none'],
            'blackbox': True,
        }),
        ('lib5', {
            'homepage': 'http://example5.com',
            'sanitizers': ['address'],
            'fuzzing_engines': ['libfuzzer',],
            'experimental': True,
            'selective_unpack': True,
            'main_repo': 'https://github.com/google/main-repo',
        }),
        ('lib6', {
            'homepage': 'http://example6.com',
            'sanitizers': ['address', 'memory', 'undefined'],
            'fuzzing_engines': ['libfuzzer', 'afl'],
            'auto_ccs': 'User@example.com',
            'vendor_ccs': ['vendor1@example.com', 'vendor2@example.com'],
        }),
        ('lib7', {
            'homepage': 'http://example.com',
            'primary_contact': 'primary@example.com',
            'auto_ccs': ['User@example.com',],
            'fuzzing_engines': ['libfuzzer',],
            'sanitizers': ['address'],
            'labels': {
                '*': ['custom'],
                'per-target': ['ignore']
            },
        }),
        ('lib8', {
            'homepage': 'http://example.com',
            'primary_contact': 'primary@example.com',
            'auto_ccs': ['User@example.com',],
            'fuzzing_engines': ['libfuzzer',],
            'sanitizers': ['none'],
            'architectures': ['i386', 'x86_64'],
        }),
        ('lib9', {
            'homepage:': 'http://example.com',
            'primary_contact': 'primary@example.com',
            'auto_ccs': ['User@example.com',],
            'main_repo': 'https://github.com/google/main-repo',
            'fuzzing_engines': ['centipede',],
            'sanitizers:': ['address',],
            'architectures': ['x86_64',],
        }),
    ]

    mock_storage.buckets().get.side_effect = mock_bucket_get
    mock_storage.buckets().getIamPolicy.side_effect = mock_get_iam_policy
    mock_storage.buckets().setIamPolicy = CopyingMock()
    mock_storage.buckets().setIamPolicy.side_effect = mock_set_iam_policy

    project_setup.main()

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_lib1').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib1')
    self.assertEqual(job.platform, 'LIB1_LINUX')
    self.assertCountEqual(job.templates, ['engine_asan', 'libfuzzer', 'prune'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib1/lib1-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib1\n'
        'SUMMARY_PREFIX = lib1\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = 500\n'
        'ALLOW_UNPACK_OVER_HTTP = True\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib1/lib1-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib1-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib1-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib1-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib1-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib1,Engine-libfuzzer\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_lib2').get()
    self.assertIsNone(job)

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_lib3').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib3')
    self.assertEqual(job.platform, 'LIB3_LINUX')
    self.assertCountEqual(job.templates, ['engine_asan', 'libfuzzer', 'prune'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib3/lib3-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib3\n'
        'SUMMARY_PREFIX = lib3\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib3/lib3-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib3-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib3-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib3-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib3-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib3,Engine-libfuzzer\n'
        'ISSUE_VIEW_RESTRICTIONS = none\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_i386_lib3').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib3')
    self.assertEqual(job.platform, 'LIB3_LINUX')
    self.assertCountEqual(job.templates, ['engine_asan', 'libfuzzer'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds-i386/lib3/lib3-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib3\n'
        'SUMMARY_PREFIX = lib3\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds-i386/lib3/lib3-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib3-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib3-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib3-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib3-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib3,Engine-libfuzzer\n'
        'ISSUE_VIEW_RESTRICTIONS = none\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_msan_lib3').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib3')
    self.assertEqual(job.platform, 'LIB3_LINUX')
    self.assertCountEqual(job.templates, ['engine_msan', 'libfuzzer'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib3/lib3-memory-([0-9]+).zip\n'
        'PROJECT_NAME = lib3\n'
        'SUMMARY_PREFIX = lib3\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib3/lib3-memory-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib3-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib3-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib3-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib3-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib3,Engine-libfuzzer\n'
        'EXPERIMENTAL = True\n'
        'ISSUE_VIEW_RESTRICTIONS = none\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_ubsan_lib3').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib3')
    self.assertEqual(job.platform, 'LIB3_LINUX')
    self.assertCountEqual(job.templates, ['engine_ubsan', 'libfuzzer'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib3/lib3-undefined-([0-9]+).zip\n'
        'PROJECT_NAME = lib3\n'
        'SUMMARY_PREFIX = lib3\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib3/lib3-undefined-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib3-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib3-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib3-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib3-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib3,Engine-libfuzzer\n'
        'ISSUE_VIEW_RESTRICTIONS = none\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(data_types.Job.name == 'afl_asan_lib1').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib1')
    self.assertEqual(job.platform, 'LIB1_LINUX')
    self.assertCountEqual(job.templates, ['engine_asan', 'afl'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds-afl/lib1/lib1-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib1\n'
        'SUMMARY_PREFIX = lib1\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = 500\n'
        'ALLOW_UNPACK_OVER_HTTP = True\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds-afl/lib1/lib1-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib1-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib1-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib1-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib1-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib1,Engine-afl\n'
        'MINIMIZE_JOB_OVERRIDE = libfuzzer_asan_lib1\n'
        'FILE_GITHUB_ISSUE = False\n')

    # Engine-less job. Manually managed.
    job = data_types.Job.query(data_types.Job.name == 'asan_lib4').get()
    self.assertIsNone(job)

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_lib5').get()
    self.assertEqual(job.project, 'lib5')
    self.assertEqual(job.platform, 'LIB5_LINUX')
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib5/lib5-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib5\n'
        'SUMMARY_PREFIX = lib5\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib5/lib5-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib5-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib5-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib5-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib5-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib5,Engine-libfuzzer\n'
        'EXPERIMENTAL = True\n'
        'DISABLE_DISCLOSURE = True\n'
        'UNPACK_ALL_FUZZ_TARGETS_AND_FILES = False\n'
        'MAIN_REPO = https://github.com/google/main-repo\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_lib6').get()
    self.assertEqual(job.project, 'lib6')
    self.assertEqual(job.platform, 'LIB6_LINUX')
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib6/lib6-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib6\n'
        'SUMMARY_PREFIX = lib6\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib6/lib6-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib6-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib6-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib6-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib6-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib6,Engine-libfuzzer\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_lib7').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib7')
    self.assertEqual(job.platform, 'LIB7_LINUX')
    self.assertCountEqual(job.templates, ['engine_asan', 'libfuzzer', 'prune'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds/lib7/lib7-address-([0-9]+).zip\n'
        'PROJECT_NAME = lib7\n'
        'SUMMARY_PREFIX = lib7\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds/lib7/lib7-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib7-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib7-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib7-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib7-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib7,Engine-libfuzzer,custom\n'
        'FILE_GITHUB_ISSUE = False\n')

    job = data_types.Job.query(
        data_types.Job.name == 'centipede_asan_lib9').get()
    self.assertIsNotNone(job)
    self.assertEqual(job.project, 'lib9')
    self.assertEqual(job.platform, 'LIB9_LINUX')
    self.assertCountEqual(job.templates, ['engine_asan', 'centipede'])
    self.assertEqual(
        job.environment_string, 'RELEASE_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds-centipede/lib9/lib9-none-([0-9]+).zip\n'
        'PROJECT_NAME = lib9\n'
        'SUMMARY_PREFIX = lib9\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'EXTRA_BUILD_BUCKET_PATH = '
        'gs://clusterfuzz-builds-centipede/lib9/lib9-address-([0-9]+).zip\n'
        'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
        'clusterfuzz-builds-centipede/lib9/lib9-address-%s.srcmap.json\n'
        'FUZZ_LOGS_BUCKET = lib9-logs.clusterfuzz-external.appspot.com\n'
        'CORPUS_BUCKET = lib9-corpus.clusterfuzz-external.appspot.com\n'
        'QUARANTINE_BUCKET = lib9-quarantine.clusterfuzz-external.appspot.com\n'
        'BACKUP_BUCKET = lib9-backup.clusterfuzz-external.appspot.com\n'
        'AUTOMATIC_LABELS = Proj-lib9,Engine-centipede\n'
        'MAIN_REPO = https://github.com/google/main-repo\n'
        'FILE_GITHUB_ISSUE = False\n')

    self.maxDiff = None

    libfuzzer = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'libFuzzer').get()
    self.assertCountEqual(libfuzzer.jobs, [
        'libfuzzer_asan_lib1',
        'libfuzzer_asan_lib3',
        'libfuzzer_asan_i386_lib3',
        'libfuzzer_asan_lib5',
        'libfuzzer_msan_lib3',
        'libfuzzer_ubsan_lib1',
        'libfuzzer_ubsan_lib3',
        'libfuzzer_asan_lib6',
        'libfuzzer_msan_lib6',
        'libfuzzer_ubsan_lib6',
        'libfuzzer_asan_lib7',
        'libfuzzer_nosanitizer_i386_lib8',
        'libfuzzer_nosanitizer_lib8',
    ])

    afl = data_types.Fuzzer.query(data_types.Fuzzer.name == 'afl').get()
    self.assertCountEqual(afl.jobs, [
        'afl_asan_lib1',
        'afl_asan_lib6',
    ])

    centipede = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'centipede').get()
    self.assertCountEqual(centipede.jobs, [
        'centipede_asan_lib9',
    ])

    # Test that old unused jobs are deleted.
    self.assertIsNone(
        data_types.Job.query(
            data_types.Job.name == 'libfuzzer_asan_old_job').get())
    self.assertIsNone(
        data_types.Job.query(
            data_types.Job.name == 'libfuzzer_msan_old_job').get())

    # Unmanaged job should still exist.
    self.assertIsNotNone(
        data_types.Job.query(data_types.Job.name == 'unmanaged_job').get())

    # Test that project settings are created.
    lib1_settings = ndb.Key(data_types.OssFuzzProject, 'lib1').get()
    self.assertIsNotNone(lib1_settings)
    self.assertDictEqual({
        'cpu_weight':
            1.5,
        'name':
            'lib1',
        'disk_size_gb':
            500,
        'service_account':
            'lib1@serviceaccount.com',
        'high_end':
            False,
        'ccs': [
            'primary@example.com', 'user@example.com', 'user2@googlemail.com'
        ],
    }, lib1_settings.to_dict())

    lib2_settings = ndb.Key(data_types.OssFuzzProject, 'lib2').get()
    self.assertIsNone(lib2_settings)

    lib3_settings = ndb.Key(data_types.OssFuzzProject, 'lib3').get()
    self.assertIsNotNone(lib3_settings)
    self.assertDictEqual({
        'cpu_weight': 1.0,
        'name': 'lib3',
        'disk_size_gb': None,
        'service_account': 'lib3@serviceaccount.com',
        'high_end': False,
        'ccs': ['user@example.com'],
    }, lib3_settings.to_dict())

    lib4_settings = ndb.Key(data_types.OssFuzzProject, 'lib4').get()
    self.assertIsNotNone(lib4_settings)
    self.assertDictEqual({
        'cpu_weight': 0.2,
        'name': 'lib4',
        'disk_size_gb': None,
        'service_account': 'lib4@serviceaccount.com',
        'high_end': True,
        'ccs': ['user@example.com'],
    }, lib4_settings.to_dict())

    old_lib_settings = ndb.Key(data_types.OssFuzzProject, 'old_lib').get()
    self.assertIsNone(old_lib_settings)

    mock_storage.buckets().get.assert_has_calls([
        mock.call(bucket='lib1-backup.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib1-corpus.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib1-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib1-logs.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib2-backup.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib2-corpus.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib2-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib2-logs.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib3-backup.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib3-corpus.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib3-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(bucket='lib3-logs.clusterfuzz-external.appspot.com'),
    ])

    mock_storage.buckets().insert.assert_has_calls([
        mock.call(
            body={
                'name': 'lib1-backup.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 100
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={'name': 'lib1-corpus.clusterfuzz-external.appspot.com'},
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib1-quarantine.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 90
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib2-backup.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 100
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={'name': 'lib2-corpus.clusterfuzz-external.appspot.com'},
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib2-quarantine.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 90
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib2-logs.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 14
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib3-backup.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 100
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={'name': 'lib3-corpus.clusterfuzz-external.appspot.com'},
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib3-quarantine.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 90
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
        mock.call(
            body={
                'name': 'lib3-logs.clusterfuzz-external.appspot.com',
                'lifecycle': {
                    'rule': [{
                        'action': {
                            'type': 'Delete'
                        },
                        'condition': {
                            'age': 14
                        }
                    }]
                }
            },
            project='clusterfuzz-external'),
        mock.call().execute(),
    ])

    mock_storage.buckets().setIamPolicy.assert_has_calls([
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:primary@example.com']
                }]
            },
            bucket='lib1-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user2@gmail.com']
                }]
            },
            bucket='lib1-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }]
            },
            bucket='lib1-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib1@serviceaccount.com']
                }]
            },
            bucket='lib1-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:primary@example.com']
                }]
            },
            bucket='lib1-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user2@gmail.com']
                }]
            },
            bucket='lib1-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }]
            },
            bucket='lib1-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib1@serviceaccount.com']
                }]
            },
            bucket='lib1-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role':
                        'roles/storage.objectViewer',
                    'members': [
                        'user:primary@example.com', 'user:user@example.com'
                    ]
                }]
            },
            bucket='lib1-logs.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }]
            },
            bucket='lib1-logs.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib1@serviceaccount.com']
                }]
            },
            bucket='lib1-logs.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:primary@example.com']
                }]
            },
            bucket='lib1-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user2@gmail.com']
                }]
            },
            bucket='lib1-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }]
            },
            bucket='lib1-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': [
                        'user:user2@gmail.com', 'user:user@example.com'
                    ]
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib1@serviceaccount.com']
                }]
            },
            bucket='lib1-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['serviceAccount:lib1@serviceaccount.com']
                }]
            },
            bucket='clusterfuzz-external-deployment'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['serviceAccount:lib1@serviceaccount.com']
                }]
            },
            bucket='global-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib2@serviceaccount.com']
                }]
            },
            bucket='lib2-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib2@serviceaccount.com']
                }]
            },
            bucket='lib2-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib2@serviceaccount.com']
                }]
            },
            bucket='lib2-logs.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib2@serviceaccount.com']
                }]
            },
            bucket='lib2-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['serviceAccount:lib2@serviceaccount.com']
                }]
            },
            bucket='clusterfuzz-external-deployment'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['serviceAccount:lib2@serviceaccount.com']
                }]
            },
            bucket='global-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }]
            },
            bucket='lib3-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib3@serviceaccount.com']
                }]
            },
            bucket='lib3-backup.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }]
            },
            bucket='lib3-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib3@serviceaccount.com']
                }]
            },
            bucket='lib3-corpus.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib3@serviceaccount.com']
                }]
            },
            bucket='lib3-logs.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }]
            },
            bucket='lib3-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['user:user@example.com']
                }, {
                    'role': 'roles/storage.objectAdmin',
                    'members': ['serviceAccount:lib3@serviceaccount.com']
                }]
            },
            bucket='lib3-quarantine.clusterfuzz-external.appspot.com'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['serviceAccount:lib3@serviceaccount.com']
                }]
            },
            bucket='clusterfuzz-external-deployment'),
        mock.call(
            body={
                'resourceId':
                    'fake',
                'kind':
                    'storage#policy',
                'etag':
                    'fake',
                'bindings': [{
                    'role': 'roles/storage.objectViewer',
                    'members': ['serviceAccount:lib3@serviceaccount.com']
                }]
            },
            bucket='global-corpus.clusterfuzz-external.appspot.com')
    ])

    mappings = data_types.FuzzerJob.query()
    tags_fuzzers_and_jobs = [(m.platform, m.fuzzer, m.job) for m in mappings]
    self.assertCountEqual(tags_fuzzers_and_jobs, [
        ('LIB1_LINUX', 'afl', 'afl_asan_lib1'),
        ('LIB1_LINUX', 'libFuzzer', 'libfuzzer_asan_lib1'),
        ('LIB3_LINUX', 'libFuzzer', 'libfuzzer_asan_lib3'),
        ('LIB3_LINUX', 'libFuzzer', 'libfuzzer_asan_i386_lib3'),
        ('LIB3_LINUX', 'libFuzzer', 'libfuzzer_msan_lib3'),
        ('LIB1_LINUX', 'libFuzzer', 'libfuzzer_ubsan_lib1'),
        ('LIB3_LINUX', 'libFuzzer', 'libfuzzer_ubsan_lib3'),
        ('LIB5_LINUX', 'libFuzzer', 'libfuzzer_asan_lib5'),
        ('LIB6_LINUX', 'libFuzzer', 'libfuzzer_asan_lib6'),
        ('LIB6_LINUX', 'libFuzzer', 'libfuzzer_msan_lib6'),
        ('LIB6_LINUX', 'libFuzzer', 'libfuzzer_ubsan_lib6'),
        ('LIB6_LINUX', 'afl', 'afl_asan_lib6'),
        ('LIB1_LINUX', 'honggfuzz', 'honggfuzz_asan_lib1'),
        ('LIB7_LINUX', 'libFuzzer', 'libfuzzer_asan_lib7'),
        ('LIB8_LINUX', 'libFuzzer', 'libfuzzer_nosanitizer_i386_lib8'),
        ('LIB8_LINUX', 'libFuzzer', 'libfuzzer_nosanitizer_lib8'),
        ('LIB9_LINUX', 'centipede', 'centipede_asan_lib9'),
    ])

    all_permissions = [
        entity.to_dict()
        for entity in data_types.ExternalUserPermission.query()
    ]

    self.assertCountEqual(all_permissions, [
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_asan_lib1',
            'email': 'primary@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_ubsan_lib1',
            'email': 'primary@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_ubsan_lib1',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_ubsan_lib1',
            'email': 'user2@googlemail.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_asan_lib1',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_asan_lib1',
            'email': 'user2@googlemail.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'afl_asan_lib1',
            'email': 'primary@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'afl_asan_lib1',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'afl_asan_lib1',
            'email': 'user2@googlemail.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_msan_lib3',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_ubsan_lib3',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'libfuzzer_asan_lib3',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'auto_cc': 1,
            'entity_name': 'asan_lib4',
            'email': 'user@example.com'
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_asan_i386_lib3',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_msan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_ubsan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_asan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'afl_asan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor1@example.com',
            'entity_name': 'libfuzzer_msan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor1@example.com',
            'entity_name': 'libfuzzer_ubsan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor1@example.com',
            'entity_name': 'libfuzzer_asan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor1@example.com',
            'entity_name': 'afl_asan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor2@example.com',
            'entity_name': 'libfuzzer_msan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor2@example.com',
            'entity_name': 'libfuzzer_ubsan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor2@example.com',
            'entity_name': 'libfuzzer_asan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'vendor2@example.com',
            'entity_name': 'afl_asan_lib6',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'primary@example.com',
            'entity_name': 'honggfuzz_asan_lib1',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'honggfuzz_asan_lib1',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user2@googlemail.com',
            'entity_name': 'honggfuzz_asan_lib1',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'primary@example.com',
            'entity_name': 'libfuzzer_asan_lib7',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_asan_lib7',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_nosanitizer_lib8',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'libfuzzer_nosanitizer_i386_lib8',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'primary@example.com',
            'entity_name': 'libfuzzer_nosanitizer_lib8',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'primary@example.com',
            'entity_name': 'libfuzzer_nosanitizer_i386_lib8',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'user@example.com',
            'entity_name': 'centipede_asan_lib9',
            'auto_cc': 1
        },
        {
            'entity_kind': 1,
            'is_prefix': False,
            'email': 'primary@example.com',
            'entity_name': 'centipede_asan_lib9',
            'auto_cc': 1
        },
    ])

    expected_topics = [
        'projects/clusterfuzz-external/topics/jobs-linux',
        'projects/clusterfuzz-external/topics/other',
        'projects/clusterfuzz-external/topics/jobs-lib1-linux',
        'projects/clusterfuzz-external/topics/jobs-lib3-linux',
        'projects/clusterfuzz-external/topics/jobs-lib4-linux',
        'projects/clusterfuzz-external/topics/jobs-lib5-linux',
        'projects/clusterfuzz-external/topics/jobs-lib6-linux',
        'projects/clusterfuzz-external/topics/jobs-lib7-linux',
        'projects/clusterfuzz-external/topics/jobs-lib8-linux',
        'projects/clusterfuzz-external/topics/jobs-lib9-linux',
    ]
    self.assertCountEqual(expected_topics,
                          list(pubsub_client.list_topics('projects/' + app_id)))

    for topic in expected_topics[2:]:
      lib = posixpath.basename(topic).split('-')[1]
      self.assertCountEqual([
          'projects/clusterfuzz-external/subscriptions/'
          f'jobs-{lib}-linux',
      ], pubsub_client.list_topic_subscriptions(topic))

    self.assertIsNotNone(pubsub_client.get_topic(unmanaged_topic_name))
    self.assertIsNotNone(pubsub_client.get_topic(other_topic_name))
    self.assertIsNone(pubsub_client.get_topic(old_topic_name))
    self.assertIsNone(pubsub_client.get_subscription(old_subscription_name))


URL_RESULTS = ast.literal_eval(_read_data_file('url_results.txt'))


def mock_get_url(url):
  """Mock get_url()."""
  if url not in URL_RESULTS:
    return None

  return URL_RESULTS[url]


class MockRequestsGet:
  """Mock requests.get."""

  def __init__(self, url, params=None, auth=None, timeout=None):  # pylint: disable=unused-argument
    if url in URL_RESULTS:
      self.text = URL_RESULTS[url]
      self.status_code = 200
    else:
      self.text = None
      self.status_code = 500


@test_utils.with_cloud_emulators('datastore')
class GetLibrariesTest(unittest.TestCase):
  """Test get_oss_fuzz_projects()."""

  def setUp(self):
    data_types.Config(github_credentials='client_id;client_secret').put()

    helpers.patch(self, ['requests.get'])
    self.mock.get.side_effect = MockRequestsGet

  def test_get_oss_fuzz_projects(self):
    """Tests get_oss_fuzz_projects()."""
    libraries = project_setup.get_oss_fuzz_projects()
    self.assertListEqual(
        sorted(libraries), [('boringssl', {
            'homepage': 'https://boringssl.googlesource.com/boringssl/'
        }), ('curl', {
            'homepage': 'https://curl.haxx.se/',
            'dockerfile': {
                'git': 'fake',
                'path': 'path/Dockerfile',
            }
        })])


def _mock_read_data(path):
  """Mock read_data."""
  if 'dbg' in path:
    return json.dumps({
        'projects': [{
            'build_path': 'gs://bucket-dbg/a-b/%ENGINE%/%SANITIZER%/'
                          '%TARGET%/([0-9]+).zip',
            'name': '//a/b',
            'fuzzing_engines': ['libfuzzer', 'honggfuzz'],
            'sanitizers': ['address']
        }]
    })

  if 'android' in path:
    return json.dumps({
        'projects': [{
            'build_path': 'gs://bucket-android/%ENGINE%/%SANITIZER%/'
                          '%TARGET%/([0-9]+).zip',
            'name': 'android_pixel7',
            'fuzzing_engines': ['libfuzzer'],
            'architectures': ['arm'],
            'sanitizers': ['hardware'],
            'platform': 'ANDROID',
            'queue_id': 'pixel7'
        }, {
            'build_path':
                'gs://bucket-android/a-b-android/%ENGINE%/%SANITIZER%/'
                '%TARGET%/([0-9]+).zip',
            'name': 'android_pixel8',
            'fuzzing_engines': ['libfuzzer', 'afl'],
            'architectures': ['x86_64'],
            'sanitizers': ['address'],
            'platform': 'ANDROID_X86',
            'queue_id': 'pixel8'
        }, {
            'build_path':
                'gs://bucket-android/a-b-android/%ENGINE%/%SANITIZER%/'
                '%TARGET%/([0-9]+).zip',
            'name': 'android_mte',
            'fuzzing_engines': ['libfuzzer'],
            'architectures': ['arm'],
            'sanitizers': ['none'],
            'platform': 'ANDROID_MTE',
            'queue_id': 'pixel8'
        }]
    })

  if 'chrome' in path:
    return json.dumps({
        'projects': [{
            'build_path': 'gs://chrome/android/hwasan-l-([0-9.]+).zip',
            'name': 'chrome_android_pixel7',
            'fuzzing_engines': ['none'],
            'architectures': ['arm'],
            'sanitizers': ['hardware'],
            'managed_engineless': True,
            'platform': 'ANDROID',
            'queue_id': 'chrome-pixel7',
            'fuzzers': ['blackbox1'],
            'additional_vars': {
                'APP_NAME':
                    'chrome.apk',
                'APP_LAUNCH_COMMAND':
                    'shell am start -a android.intent.action.MAIN -n %PKG_NAME%/.Main \'%TESTCASE_FILE_URL%\'',
                'APP_ARGS':
                    '--enable-logging=stderr',
                'REQUIRED_APP_ARGS':
                    '--disable-things',
                'PKG_NAME':
                    'org.chromium.chrome',
                'CHILD_PROCESS_TERMINATION_PATTERN':
                    'org.chromium.chrome:some_process',
                'HWASAN_OPTIONS':
                    '--hwasan-opt1'
            }
        }, {
            'build_path': 'gs://chrome/android/hwasan-l-([0-9.]+).zip',
            'name': 'webview_android_pixel7',
            'fuzzing_engines': ['none'],
            'architectures': ['arm'],
            'sanitizers': ['hardware'],
            'managed_engineless': True,
            'platform': 'ANDROID',
            'queue_id': 'chrome-pixel7',
            'fuzzers': ['blackbox2'],
            'additional_vars': {
                'APP_NAME':
                    'webview.apk',
                'APP_LAUNCH_COMMAND':
                    'shell am start -a android.intent.action.VIEW -n %PKG_NAME%/.Main \'%TESTCASE_FILE_URL%\'',
                'APP_ARGS':
                    '--enable-logging=stderr',
                'COMMAND_LINE_PATH':
                    '/data/local/tmp/webview_command_line',
                'PKG_NAME':
                    'org.chromium.webview',
                'CHILD_PROCESS_TERMINATION_PATTERN':
                    'org.chromium.webview:some_process',
            }
        }, {
            'build_path': 'gs://chrome/android/asan-l-([0-9.]+).zip',
            'name': 'chrome_android_pixel8',
            'fuzzing_engines': ['none'],
            'architectures': ['arm'],
            'sanitizers': ['address'],
            'managed_engineless': True,
            'platform': 'ANDROID',
            'queue_id': 'chrome-pixel8',
            'fuzzers': ['blackbox1', 'blackbox2'],
            'additional_vars': {
                'APP_NAME':
                    'chrome.apk',
                'APP_LAUNCH_COMMAND':
                    'shell am start -a android.intent.action.MAIN -n %PKG_NAME%/.Main \'%TESTCASE_FILE_URL%\'',
                'APP_ARGS':
                    '--enable-logging=stderr',
                'REQUIRED_APP_ARGS':
                    '--disable-things',
                'PKG_NAME':
                    'org.chromium.chrome',
                'CHILD_PROCESS_TERMINATION_PATTERN':
                    'org.chromium.chrome:some_process',
                'ASAN_OPTIONS':
                    '--asan-opt1'
            }
        }, {
            'build_path': 'gs://chrome/android/mte-l-([0-9.]+).zip',
            'name': 'chrome_android_mte',
            'fuzzing_engines': ['none'],
            'architectures': ['arm'],
            'sanitizers': ['none'],
            'managed_engineless': True,
            'platform': 'ANDROID_MTE',
            'queue_id': 'chrome-pixel8'
        }, {
            'build_path': 'gs://chrome/android/mte-l-([0-9.]+).zip',
            'name': 'chrome_android_mte_unmanaged',
            'fuzzing_engines': ['none'],
            'architectures': ['arm'],
            'sanitizers': ['none'],
            'platform': 'ANDROID_MTE',
            'queue_id': 'chrome-pixel9'
        }]
    })

  return json.dumps({
      'projects': [
          {
              'build_path':
                  'gs://bucket/a-b/%ENGINE%/%SANITIZER%/%TARGET%/([0-9]+).zip',
              'name':
                  '//a/b',
              'fuzzing_engines': ['libfuzzer', 'honggfuzz'],
              'sanitizers': ['address', 'memory']
          },
          {
              'build_path':
                  'gs://bucket/c-d/%ENGINE%/%SANITIZER%/%TARGET%/([0-9]+).zip',
              'name':
                  '//c/d',
              'fuzzing_engines': ['libfuzzer', 'googlefuzztest'],
              'sanitizers': ['address']
          },
          {
              'build_path':
                  'gs://bucket/e-f/%ENGINE%/%SANITIZER%/%TARGET%/([0-9]+).zip',
              'name':
                  '//e/f',
              'fuzzing_engines': ['libfuzzer'],
              'sanitizers': ['none']
          },
      ]
  })


@test_utils.with_cloud_emulators('datastore', 'pubsub')
class GenericProjectSetupTest(unittest.TestCase):
  """Test generic project setup."""

  def setUp(self):
    self.maxDiff = None

    helpers.patch_environ(self)

    data_types.Job(name='old_unmanaged').put()
    data_types.Job(
        name='old_managed',
        environment_string='MANAGED = True\nPROJECT_NAME = old').put()

    self.libfuzzer = data_types.Fuzzer(
        name='libFuzzer', jobs=['old_unmanaged', 'old_managed'])
    self.libfuzzer.put()

    self.afl = data_types.Fuzzer(name='afl', jobs=[])
    self.afl.put()

    self.honggfuzz = data_types.Fuzzer(name='honggfuzz', jobs=[])
    self.honggfuzz.put()

    self.gft = data_types.Fuzzer(name='googlefuzztest', jobs=[])
    self.gft.put()

    self.centipede = data_types.Fuzzer(name='centipede', jobs=[])
    self.centipede.put()

    # blackbox fuzzers
    self.blackbox1 = data_types.Fuzzer(name='blackbox1', jobs=[])
    self.blackbox1.put()
    self.blackbox2 = data_types.Fuzzer(name='blackbox2', jobs=[])
    self.blackbox2.put()

    helpers.patch(self, [
        'clusterfuzz._internal.config.local_config.ProjectConfig',
        ('get_application_id_2',
         'clusterfuzz._internal.base.utils.get_application_id'),
        'clusterfuzz._internal.google_cloud_utils.storage.build',
        'clusterfuzz._internal.google_cloud_utils.storage.read_data',
        'time.sleep',
        'handlers.base_handler.Handler.is_cron',
    ])

    self.mock.read_data.side_effect = _mock_read_data

    self.mock.ProjectConfig.return_value = mock_config.MockConfig({
        'project_setup': [
            {
                'source': 'gs://bucket/projects.json',
                'build_type': 'FUZZ_TARGET_BUILD_BUCKET_PATH',
                'experimental_sanitizers': ['memory'],
                'build_buckets': {
                    'afl': 'clusterfuzz-builds-afl',
                    'honggfuzz': 'clusterfuzz-builds-honggfuzz',
                    'googlefuzztest': 'clusterfuzz-builds-googlefuzztest',
                    'libfuzzer': 'clusterfuzz-builds',
                    'libfuzzer_i386': 'clusterfuzz-builds-i386',
                    'no_engine': 'clusterfuzz-builds-no-engine',
                },
                'additional_vars': {
                    'all': {
                        'STRING_VAR': 'VAL',
                        'BOOL_VAR': True,
                        'INT_VAR': 0,
                    },
                    'libfuzzer': {
                        'address': {
                            'ASAN_VAR': 'VAL',
                        },
                        'memory': {
                            'MSAN_VAR': 'VAL',
                        },
                        'none': {},
                    }
                }
            },
            {
                'source': 'gs://bucket-dbg/projects.json',
                'job_suffix': '_dbg',
                'external_config': {
                    'reproduction_topic':
                        'projects/proj/topics/reproduction',
                    'updates_subscription':
                        'projects/proj/subscriptions/updates',
                },
                'build_type': 'FUZZ_TARGET_BUILD_BUCKET_PATH',
                'build_buckets': {
                    'afl': 'clusterfuzz-builds-afl-dbg',
                    'honggfuzz': 'clusterfuzz-builds-honggfuzz-dbg',
                    'googlefuzztest': 'clusterfuzz-builds-googlefuzztest-dbg',
                    'libfuzzer': 'clusterfuzz-builds-dbg',
                    'libfuzzer_i386': 'clusterfuzz-builds-i386-dbg',
                    'no_engine': 'clusterfuzz-builds-no-engine-dbg',
                },
                'additional_vars': {
                    'all': {
                        'STRING_VAR': 'VAL-dbg',
                        'BOOL_VAR': True,
                        'INT_VAR': 0,
                    },
                    'libfuzzer': {
                        'address': {
                            'ASAN_VAR': 'VAL-dbg',
                        },
                        'memory': {
                            'MSAN_VAR': 'VAL-dbg',
                        }
                    }
                }
            },
            {
                'source': 'gs://bucket-android/projects.json',
                'build_type': 'FUZZ_TARGET_BUILD_BUCKET_PATH',
                'build_buckets': {
                    'afl': 'clusterfuzz-builds-afl-android',
                    'libfuzzer': 'clusterfuzz-builds-android',
                    'libfuzzer_arm': 'clusterfuzz-builds-android',
                    'no_engine': 'clusterfuzz-builds-no-engine-android',
                },
                'additional_vars': {
                    'all': {
                        'STRING_VAR': 'VAL-android',
                        'BOOL_VAR': True,
                        'INT_VAR': 0,
                    },
                    'libfuzzer': {
                        'address': {
                            'ASAN_VAR': 'VAL-android',
                        },
                        'memory': {
                            'MSAN_VAR': 'VAL-android',
                        }
                    },
                    'afl': {
                        'address': {
                            'ASAN_VAR': 'VAL-android',
                        },
                    }
                }
            },
            {
                'source': 'gs://bucket-chrome/projects.json',
                'build_type': 'RELEASE_BUILD_BUCKET_PATH',
                'build_buckets': {
                    'no_engine': 'clusterfuzz-builds-no-engine-chrome',
                },
                'additional_vars': {
                    'all': {
                        'STRING_VAR': 'VAL-chrome',
                        'BOOL_VAR': True,
                        'INT_VAR': 0,
                    },
                    'none': {
                        'address': {
                            'ASAN_VAR': 'VAL-chrome',
                        },
                        'hwardware': {
                            'HWASAN_VAR': 'VAL-chrome',
                        },
                        'none': {
                            'NONE_VAR': 'VAL-chrome',
                        },
                    },
                }
            },
        ],
    })

    # Should be deleted.
    job = data_types.Job(
        name='libfuzzer_asan_c-d_dbg', environment_string='MANAGED = True')
    job.put()

  def test_execute(self):
    """Tests executing of cron job."""
    pubsub_client = pubsub.PubSubClient()
    self.mock.get_application_id_2.return_value = 'clusterfuzz-external'
    app_id = utils.get_application_id()
    unmanaged_topic_name = pubsub.topic_name(app_id, 'jobs-linux')
    other_topic_name = pubsub.topic_name(app_id, 'other')
    pubsub_client.create_topic(unmanaged_topic_name)
    pubsub_client.create_topic(other_topic_name)
    project_setup.main()

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_a-b').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket/a-b/libfuzzer/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //a/b\nSUMMARY_PREFIX = //a/b\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'ASAN_VAR = VAL\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'libfuzzer', 'prune'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_msan_a-b').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket/a-b/libfuzzer/memory/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //a/b\nSUMMARY_PREFIX = //a/b\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'EXPERIMENTAL = True\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'MSAN_VAR = VAL\n'
        'STRING_VAR = VAL\n', job.environment_string)
    self.assertCountEqual(['engine_msan', 'libfuzzer'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_c-d').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket/c-d/libfuzzer/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //c/d\nSUMMARY_PREFIX = //c/d\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'ASAN_VAR = VAL\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'libfuzzer', 'prune'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_nosanitizer_e-f').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket/e-f/libfuzzer/none/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //e/f\nSUMMARY_PREFIX = //e/f\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL\n', job.environment_string)
    self.assertCountEqual(['libfuzzer', 'prune'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_c-d_dbg').get()
    self.assertIsNone(job)

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_a-b_dbg').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket-dbg/a-b/libfuzzer/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //a/b\nSUMMARY_PREFIX = //a/b\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'ASAN_VAR = VAL-dbg\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-dbg\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'libfuzzer', 'prune'], job.templates)
    self.assertEqual('projects/proj/topics/reproduction',
                     job.external_reproduction_topic)
    self.assertEqual('projects/proj/subscriptions/updates',
                     job.external_updates_subscription)
    self.assertTrue(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'honggfuzz_asan_a-b').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket/a-b/honggfuzz/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //a/b\nSUMMARY_PREFIX = //a/b\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'MINIMIZE_JOB_OVERRIDE = libfuzzer_asan_a-b\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'honggfuzz'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'honggfuzz_asan_a-b_dbg').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket-dbg/a-b/honggfuzz/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //a/b\nSUMMARY_PREFIX = //a/b\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'MINIMIZE_JOB_OVERRIDE = libfuzzer_asan_a-b_dbg\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-dbg\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'honggfuzz'], job.templates)
    self.assertEqual('projects/proj/topics/reproduction',
                     job.external_reproduction_topic)
    self.assertEqual('projects/proj/subscriptions/updates',
                     job.external_updates_subscription)
    self.assertTrue(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'googlefuzztest_asan_c-d').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket/c-d/googlefuzztest/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = //c/d\nSUMMARY_PREFIX = //c/d\nMANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'googlefuzztest'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_hwasan_android_pixel7').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket-android/libfuzzer/hardware/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = android\n'
        'SUMMARY_PREFIX = android\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-android\n', job.environment_string)
    self.assertCountEqual(['engine_asan', 'libfuzzer', 'android'],
                          job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID:PIXEL7", job.platform)

    job = data_types.Job.query(
        data_types.Job.name == 'afl_asan_android_pixel8').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket-android/a-b-android/afl/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = android\n'
        'SUMMARY_PREFIX = android\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'MINIMIZE_JOB_OVERRIDE = libfuzzer_asan_android_pixel8\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'ASAN_VAR = VAL-android\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-android\n', job.environment_string)
    self.assertCountEqual(['afl', 'android', 'engine_asan'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID_X86:PIXEL8", job.platform)

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_asan_android_pixel8').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket-android/a-b-android/libfuzzer/address/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = android\n'
        'SUMMARY_PREFIX = android\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'ASAN_VAR = VAL-android\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-android\n', job.environment_string)
    self.assertCountEqual(['libfuzzer', 'android', 'engine_asan', 'prune'],
                          job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID_X86:PIXEL8", job.platform)

    job = data_types.Job.query(
        data_types.Job.name == 'libfuzzer_nosanitizer_android_mte').get()
    self.assertEqual(
        'FUZZ_TARGET_BUILD_BUCKET_PATH = '
        'gs://bucket-android/a-b-android/libfuzzer/none/%TARGET%/([0-9]+).zip\n'
        'PROJECT_NAME = android\n'
        'SUMMARY_PREFIX = android\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-android\n', job.environment_string)
    self.assertCountEqual(['libfuzzer', 'android', 'prune'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID_MTE:PIXEL8", job.platform)

    expected_topics = [
        'projects/clusterfuzz-external/topics/jobs-linux',
        'projects/clusterfuzz-external/topics/other',
        'projects/clusterfuzz-external/topics/jobs-android-pixel7',
        'projects/clusterfuzz-external/topics/jobs-android-x86-pixel8',
        'projects/clusterfuzz-external/topics/jobs-android-mte-pixel8',
        'projects/clusterfuzz-external/topics/jobs-android-chrome-pixel7',
        'projects/clusterfuzz-external/topics/jobs-android-chrome-pixel8',
        'projects/clusterfuzz-external/topics/jobs-android-mte-chrome-pixel8',
    ]
    self.assertCountEqual(expected_topics,
                          list(pubsub_client.list_topics('projects/' + app_id)))

    self.assertCountEqual(
        ['projects/clusterfuzz-external/subscriptions/jobs-android-pixel7'],
        pubsub_client.list_topic_subscriptions(
            'projects/clusterfuzz-external/topics/jobs-android-pixel7'))

    self.assertCountEqual(
        ['projects/clusterfuzz-external/subscriptions/jobs-android-x86-pixel8'],
        pubsub_client.list_topic_subscriptions(
            'projects/clusterfuzz-external/topics/jobs-android-x86-pixel8'))

    self.assertCountEqual(
        ['projects/clusterfuzz-external/subscriptions/jobs-android-mte-pixel8'],
        pubsub_client.list_topic_subscriptions(
            'projects/clusterfuzz-external/topics/jobs-android-mte-pixel8'))

    self.assertIsNotNone(pubsub_client.get_topic(unmanaged_topic_name))
    self.assertIsNotNone(pubsub_client.get_topic(other_topic_name))

    libfuzzer = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'libFuzzer').get()
    self.assertCountEqual([
        'libfuzzer_asan_a-b',
        'libfuzzer_asan_c-d',
        'libfuzzer_msan_a-b',
        'libfuzzer_nosanitizer_e-f',
        'libfuzzer_nosanitizer_android_mte',
        'libfuzzer_hwasan_android_pixel7',
        'libfuzzer_asan_android_pixel8',
        'old_unmanaged',
    ], libfuzzer.jobs)

    afl = data_types.Fuzzer.query(data_types.Fuzzer.name == 'afl').get()
    self.assertCountEqual([
        'afl_asan_android_pixel8',
    ], afl.jobs)

    honggfuzz = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'honggfuzz').get()
    self.assertCountEqual([
        'honggfuzz_asan_a-b',
    ], honggfuzz.jobs)

    gft = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'googlefuzztest').get()
    self.assertCountEqual(['googlefuzztest_asan_c-d'], gft.jobs)

    self.assertCountEqual(
        [
            'projects/clusterfuzz-external/subscriptions/jobs-android-chrome-pixel7'
        ],
        pubsub_client.list_topic_subscriptions(
            'projects/clusterfuzz-external/topics/jobs-android-chrome-pixel7'))

    self.assertCountEqual(
        [
            'projects/clusterfuzz-external/subscriptions/jobs-android-chrome-pixel8'
        ],
        pubsub_client.list_topic_subscriptions(
            'projects/clusterfuzz-external/topics/jobs-android-chrome-pixel8'))

    self.assertCountEqual(
        [
            'projects/clusterfuzz-external/subscriptions/jobs-android-mte-chrome-pixel8'
        ],
        pubsub_client.list_topic_subscriptions(
            'projects/clusterfuzz-external/topics/jobs-android-mte-chrome-pixel8'
        ))

    job = data_types.Job.query(
        data_types.Job.name == 'noengine_hwasan_chrome_android_pixel7').get()
    self.assertEqual(
        'RELEASE_BUILD_BUCKET_PATH = gs://chrome/android/hwasan-l-([0-9.]+).zip\n'
        'PROJECT_NAME = chrome_android_pixel7\n'
        'SUMMARY_PREFIX = chrome_android_pixel7\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-chrome\n'
        'APP_ARGS = --enable-logging=stderr\n'
        'APP_LAUNCH_COMMAND = shell am start -a android.intent.action.MAIN -n %PKG_NAME%/.Main \'%TESTCASE_FILE_URL%\'\n'
        'APP_NAME = chrome.apk\n'
        'CHILD_PROCESS_TERMINATION_PATTERN = org.chromium.chrome:some_process\n'
        'HWASAN_OPTIONS = --hwasan-opt1\n'
        'PKG_NAME = org.chromium.chrome\n'
        'REQUIRED_APP_ARGS = --disable-things\n', job.environment_string)
    self.assertCountEqual(['android'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID:CHROME-PIXEL7", job.platform)

    job = data_types.Job.query(
        data_types.Job.name == 'noengine_hwasan_webview_android_pixel7').get()
    self.assertEqual(
        'RELEASE_BUILD_BUCKET_PATH = gs://chrome/android/hwasan-l-([0-9.]+).zip\n'
        'PROJECT_NAME = webview_android_pixel7\n'
        'SUMMARY_PREFIX = webview_android_pixel7\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-chrome\n'
        'APP_ARGS = --enable-logging=stderr\n'
        'APP_LAUNCH_COMMAND = shell am start -a android.intent.action.VIEW -n %PKG_NAME%/.Main \'%TESTCASE_FILE_URL%\'\n'
        'APP_NAME = webview.apk\n'
        'CHILD_PROCESS_TERMINATION_PATTERN = org.chromium.webview:some_process\n'
        'COMMAND_LINE_PATH = /data/local/tmp/webview_command_line\n'
        'PKG_NAME = org.chromium.webview\n', job.environment_string)
    self.assertCountEqual(['android'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID:CHROME-PIXEL7", job.platform)

    job = data_types.Job.query(
        data_types.Job.name == 'asan_chrome_android_pixel8').get()
    self.assertEqual(
        'RELEASE_BUILD_BUCKET_PATH = gs://chrome/android/asan-l-([0-9.]+).zip\n'
        'PROJECT_NAME = chrome_android_pixel8\n'
        'SUMMARY_PREFIX = chrome_android_pixel8\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'ASAN_VAR = VAL-chrome\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'STRING_VAR = VAL-chrome\n'
        'APP_ARGS = --enable-logging=stderr\n'
        'APP_LAUNCH_COMMAND = shell am start -a android.intent.action.MAIN -n %PKG_NAME%/.Main \'%TESTCASE_FILE_URL%\'\n'
        'APP_NAME = chrome.apk\n'
        'ASAN_OPTIONS = --asan-opt1\n'
        'CHILD_PROCESS_TERMINATION_PATTERN = org.chromium.chrome:some_process\n'
        'PKG_NAME = org.chromium.chrome\n'
        'REQUIRED_APP_ARGS = --disable-things\n', job.environment_string)
    self.assertCountEqual(['android'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID:CHROME-PIXEL8", job.platform)

    job = data_types.Job.query(
        data_types.Job.name == 'noengine_nosanitizer_chrome_android_mte').get()
    self.assertEqual(
        'RELEASE_BUILD_BUCKET_PATH = gs://chrome/android/mte-l-([0-9.]+).zip\n'
        'PROJECT_NAME = chrome_android_mte\n'
        'SUMMARY_PREFIX = chrome_android_mte\n'
        'MANAGED = True\n'
        'DISK_SIZE_GB = None\n'
        'DISABLE_DISCLOSURE = True\n'
        'FILE_GITHUB_ISSUE = False\n'
        'BOOL_VAR = True\n'
        'INT_VAR = 0\n'
        'NONE_VAR = VAL-chrome\n'
        'STRING_VAR = VAL-chrome\n', job.environment_string)
    self.assertCountEqual(['android'], job.templates)
    self.assertEqual(None, job.external_reproduction_topic)
    self.assertEqual(None, job.external_updates_subscription)
    self.assertFalse(job.is_external())
    self.assertEqual("ANDROID_MTE:CHROME-PIXEL8", job.platform)

    job = data_types.Job.query(
        data_types.Job.name ==
        'noengine_nosanitizer_chrome_android_mte_unmanaged').get()
    self.assertIsNone(job)

    blackbox1 = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'blackbox1').get()
    self.assertCountEqual(
        ['noengine_hwasan_chrome_android_pixel7', 'asan_chrome_android_pixel8'],
        blackbox1.jobs)

    blackbox2 = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'blackbox2').get()
    self.assertCountEqual([
        'noengine_hwasan_webview_android_pixel7', 'asan_chrome_android_pixel8'
    ], blackbox2.jobs)
