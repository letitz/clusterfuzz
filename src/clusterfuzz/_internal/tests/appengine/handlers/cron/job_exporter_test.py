# Copyright 2025 Google LLC
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
# pylint: disable=protected-access
"""Tests for the job exporter cronjob."""

import os
import shutil
import tempfile
from typing import List
import unittest

from google.cloud import ndb

from clusterfuzz._internal.bot.tasks.utasks import uworker_io
from clusterfuzz._internal.cron import job_exporter
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.google_cloud_utils import blobs
from clusterfuzz._internal.google_cloud_utils import storage
from clusterfuzz._internal.tests.test_libs import helpers
from clusterfuzz._internal.tests.test_libs import test_utils


def _sample_data_bundle(name='some_bundle',
                        bucket_name='some-data-bundle-bucket'):
  return data_types.DataBundle(
      name=name,
      bucket_name=bucket_name,
  )


def _data_bundles_equal(bundle, another_bundle):
  return (bundle.name == another_bundle.name and bundle.bucket_name,
          another_bundle.bucket_name)


def _sample_job_template(name: str = 'some-job',
                         environment_string: str = 'some-env'):
  return data_types.JobTemplate(
      name=name,
      environment_string=environment_string,
  )


def _job_templates_equal(template, another_template):
  return (template.name == another_template.name and
          template.environment_string == another_template.environment_string)


def _sample_job(name='some-job',
                custom_binary_key='some-key',
                platform='some-platform',
                environment_string='some-env-string'):
  return data_types.Job(
      name=name,
      custom_binary_key=custom_binary_key,
      platform=platform,
      environment_string=environment_string,
  )


def _jobs_equal(job, another_job):
  return (job.name == another_job.name and
          job.custom_binary_key == another_job.custom_binary_key and
          job.platform == another_job.platform)


def _sample_fuzzer(  # pylint: disable=dangerous-default-value
    name='some-fuzzer',
    data_bundle_name='some-data-bundle',
    jobs=['some-job'],
    blobstore_key='some-key',
    sample_testcase='some-testcase-key'):
  return data_types.Fuzzer(
      name=name,
      data_bundle_name=data_bundle_name,
      jobs=jobs,
      blobstore_key=blobstore_key,
      sample_testcase=sample_testcase,
  )


def _fuzzers_equal(fuzzer, another_fuzzer):
  return (fuzzer.name == another_fuzzer.name and
          fuzzer.data_bundle_name == another_fuzzer.data_bundle_name and
          fuzzer.jobs == another_fuzzer.jobs and
          fuzzer.blobstore_key == another_fuzzer.blobstore_key and
          fuzzer.sample_testcase == another_fuzzer.sample_testcase)


def _blob_is_present_in_gcs(blob_path):
  return storage.get(blob_path) is not None


def _blob_content_is_equal(blob_path, data):
  fetched_data = storage.read_data(blob_path)
  return data == fetched_data


def _entity_list_contains_expected_entities(blob_path, expected_entities):
  recovered_entities = set(
      storage.read_data(blob_path).decode('utf-8').split('\n'))
  return expected_entities == recovered_entities


def _upload_entity_export_data(
    entity: ndb.Model,
    entity_kind: str,
    source_bucket: str,
    blobstore_key_content: bytes | None = None,
    sample_testcase_contents: bytes | None = None,
    custom_binary_contents: bytes | None = None,
    data_bundle_blob_contents: bytes | None = None,
):
  """Dumps an entity in its blobs into the expect exported folder structure."""
  assert getattr(entity, 'name')
  entity_location = f'gs://{source_bucket}/{entity_kind}/{entity.name}'

  serialized_entity = uworker_io.entity_to_protobuf(entity).SerializeToString()
  assert storage.write_data(serialized_entity,
                            f'{entity_location}/entity.proto')

  if blobstore_key_content:
    storage.write_data(blobstore_key_content,
                       f'{entity_location}/blobstore_key')

  if sample_testcase_contents:
    storage.write_data(sample_testcase_contents,
                       f'{entity_location}/sample_testcase')

  if custom_binary_contents:
    storage.write_data(custom_binary_contents,
                       f'{entity_location}/custom_binary_key')

  if data_bundle_blob_contents:
    storage.write_data(data_bundle_blob_contents,
                       f'{entity_location}/contents/blob')


def _register_entity_and_upload_blobs(
    entity: ndb.Model,
    blobs_bucket: str,
    blobstore_key_content: bytes | None,
    sample_testcase_contents: bytes | None,
    custom_binary_contents: bytes | None,
    data_bundle_blob_contents: bytes | None = None,
):
  """Persists an entity to GCS, and its blobs into the project's
    blob bucket."""
  assert getattr(entity, 'name')
  entity.put()

  if blobstore_key_content:
    blobstore_key = getattr(entity, 'blobstore_key', None)
    assert blobstore_key
    storage.write_data(blobstore_key_content,
                       f'gs://{blobs_bucket}/{blobstore_key}')

  if sample_testcase_contents:
    sample_testcase_key = getattr(entity, 'sample_testcase', None)
    assert sample_testcase_key
    storage.write_data(sample_testcase_contents,
                       f'gs://{blobs_bucket}/{sample_testcase_key}')

  if custom_binary_contents:
    custom_binary_key = getattr(entity, 'custom_binary_key', None)
    assert custom_binary_key
    storage.write_data(custom_binary_contents,
                       f'gs://{blobs_bucket}/{custom_binary_key}')

  if data_bundle_blob_contents:
    assert isinstance(entity, data_types.DataBundle)
    bundle_bucket = entity.bucket_name
    assert bundle_bucket
    storage.create_bucket_if_needed(bundle_bucket)
    storage.write_data(data_bundle_blob_contents, f'gs://{bundle_bucket}/blob')


def _upload_entity_list(entities: List[str], entity_base_path: str):
  entities_payload = '\n'.join(entities).encode('utf-8')
  assert storage.write_data(entities_payload, f'{entity_base_path}/entities')


def _entity_blob_was_correctly_imported(expected_content: any, blob_id: str):
  gcs_path = blobs.get_gcs_path(blob_id)
  return (_blob_is_present_in_gcs(gcs_path) and
          _blob_content_is_equal(gcs_path, expected_content))


@test_utils.with_cloud_emulators('datastore')
class TestEntitySerializationAndDeserializastion(unittest.TestCase):
  """Test the serialization and deserialization of entities."""

  def test_data_bundle_serializes_and_deserializes_correctly(self):
    """Test data_types.JobTemplate serialization/deserialization."""
    data_bundle = _sample_data_bundle()
    entity_migrator = job_exporter.EntityMigrator(data_types.DataBundle, [],
                                                  'databundle', None, None)

    serialized_data_bundle = entity_migrator._serialize(data_bundle)
    deserialized_data_bundle = entity_migrator._deserialize(
        serialized_data_bundle)

    self.assertTrue(_data_bundles_equal(data_bundle, deserialized_data_bundle))

  def test_job_template_serializes_and_deserializes_correctly(self):
    """Test data_types.JobTemplate serialization/deserialization."""
    job_template = _sample_job_template()
    entity_migrator = job_exporter.EntityMigrator(data_types.JobTemplate, [],
                                                  'jobtemplate', None, None)

    serialized_job_template = entity_migrator._serialize(job_template)
    deserialized_job_template = entity_migrator._deserialize(
        serialized_job_template)

    self.assertTrue(
        _job_templates_equal(job_template, deserialized_job_template))

  def test_jobs_serializes_and_deserializes_correctly(self):
    """Test data_types.Job serialization/deserialization."""
    job = _sample_job()
    entity_migrator = job_exporter.EntityMigrator(
        data_types.Job, ['custom_binary_key'], 'job', None, None)

    serialized_job = entity_migrator._serialize(job)
    deserialized_job = entity_migrator._deserialize(serialized_job)

    self.assertTrue(_jobs_equal(job, deserialized_job))

  def test_fuzzer_serializes_and_deserializes_correctly(self):
    """Test data_types.Fuzzer serialization/deserialization."""
    fuzzer = _sample_fuzzer()
    entity_migrator = job_exporter.EntityMigrator(
        data_types.Fuzzer, ['blobstore_key', 'sample_testcase'], 'fuzzer', None,
        None)

    serialized_fuzzer = entity_migrator._serialize(fuzzer)
    deserialized_fuzzer = entity_migrator._deserialize(serialized_fuzzer)

    self.assertTrue(_fuzzers_equal(fuzzer, deserialized_fuzzer))


@test_utils.with_cloud_emulators('datastore')
class TestEntitiesAreCorrectlyExported(unittest.TestCase):
  """Test the job exporter job with Fuzzer entities."""

  def setUp(self):
    helpers.patch_environ(self)
    self.local_gcs_buckets_path = tempfile.mkdtemp()
    self.blobs_bucket = 'BLOBS_BUCKET'
    self.target_bucket = 'TARGET_BUCKET'
    os.environ['LOCAL_GCS_BUCKETS_PATH'] = self.local_gcs_buckets_path
    os.environ['EXPORT_BUCKET'] = self.target_bucket
    storage.create_bucket_if_needed(self.blobs_bucket)
    storage.create_bucket_if_needed(self.target_bucket)
    helpers.patch(self, [
        'clusterfuzz._internal.google_cloud_utils.blobs.get_gcs_path',
    ])

  def tearDown(self):
    shutil.rmtree(self.local_gcs_buckets_path, ignore_errors=True)

  def test_fuzzers_are_correctly_exported(self):
    """Verifies fuzzer protos and blobs are uploaded. If no blobstore
        key is present, no blob is uploaded."""
    blobstore_key = 'blobstore-key'
    sample_testcase_key = 'some-blobstore-key'

    fuzzer = _sample_fuzzer(
        name='some-fuzzer',
        data_bundle_name='some-bundle',
        jobs=['some-job'],
        blobstore_key=blobstore_key,
        sample_testcase=sample_testcase_key)
    another_fuzzer = _sample_fuzzer(
        name='another-fuzzer',
        data_bundle_name='another-bundle',
        jobs=['another-job'],
        blobstore_key=None,
        sample_testcase=None)
    fuzzer.put()
    another_fuzzer.put()
    entity_migrator = job_exporter.EntityMigrator(
        data_types.Fuzzer, ['blobstore_key', 'sample_testcase'], 'fuzzer',
        job_exporter.StorageRSync(), self.target_bucket)

    blob_id = 'some-blob-id'
    sample_testcase_blob_id = 'some-testcase-blob-id'
    blob_data = b'some-blob-data'
    sample_testcase_blob_data = b'some-sample-testcase-data'
    blobstore_key_location = f'gs://{self.blobs_bucket}/{blob_id}'
    sample_testcase_location = f'gs://{self.blobs_bucket}/{sample_testcase_blob_id}'
    storage.write_data(blob_data, blobstore_key_location)
    storage.write_data(sample_testcase_blob_data, sample_testcase_location)

    fuzzer_gcs_prefix = f'gs://{self.target_bucket}/fuzzer/{fuzzer.name}'
    fuzzer_proto_location = f'{fuzzer_gcs_prefix}/entity.proto'
    fuzzer_blob_location = f'{fuzzer_gcs_prefix}/blobstore_key'
    fuzzer_testcase_location = f'{fuzzer_gcs_prefix}/sample_testcase'

    another_fuzzer_gcs_prefix = f'gs://{self.target_bucket}/fuzzer/{another_fuzzer.name}'
    another_fuzzer_proto_location = f'{another_fuzzer_gcs_prefix}/entity.proto'
    another_fuzzer_blob_location = f'{another_fuzzer_gcs_prefix}/blobstore_key'
    another_fuzzer_testcase_location = f'{another_fuzzer_gcs_prefix}/sample_testcase'

    def get_gcs_key_mock_override(blob_key: str):
      bucket_prefix = f'gs://{self.blobs_bucket}'
      return_values = {
          blobstore_key: f'{bucket_prefix}/{blob_id}',
          sample_testcase_key: f'{bucket_prefix}/{sample_testcase_blob_id}',
      }
      return return_values.get(blob_key, None)

    self.mock.get_gcs_path.side_effect = get_gcs_key_mock_override
    entity_migrator.export_entities()

    self.assertTrue(_blob_is_present_in_gcs(fuzzer_proto_location))
    serialized_fuzzer_proto = storage.read_data(fuzzer_proto_location)
    deserialized_fuzzer_proto = entity_migrator._deserialize(
        serialized_fuzzer_proto)
    self.assertTrue(_fuzzers_equal(fuzzer, deserialized_fuzzer_proto))

    self.assertTrue(_blob_is_present_in_gcs(fuzzer_blob_location))
    self.assertTrue(_blob_content_is_equal(fuzzer_blob_location, blob_data))
    self.assertTrue(_blob_is_present_in_gcs(fuzzer_testcase_location))
    self.assertTrue(
        _blob_content_is_equal(fuzzer_testcase_location,
                               sample_testcase_blob_data))

    self.assertTrue(_blob_is_present_in_gcs(another_fuzzer_proto_location))
    serialized_another_fuzzer_proto = storage.read_data(
        another_fuzzer_proto_location)
    deserialized_another_fuzzer_proto = entity_migrator._deserialize(
        serialized_another_fuzzer_proto)
    self.assertTrue(
        _fuzzers_equal(another_fuzzer, deserialized_another_fuzzer_proto))

    self.assertFalse(_blob_is_present_in_gcs(another_fuzzer_blob_location))
    self.assertFalse(_blob_is_present_in_gcs(another_fuzzer_testcase_location))

    expected_persisted_entities = {'some-fuzzer', 'another-fuzzer'}
    entity_list_location = f'gs://{self.target_bucket}/fuzzer/entities'

    self.assertTrue(_blob_is_present_in_gcs(entity_list_location))
    self.assertTrue(
        _entity_list_contains_expected_entities(entity_list_location,
                                                expected_persisted_entities))

  def test_jobs_are_correctly_exported(self):
    """Verifies job protos and custom binary blobs are uploaded. If no custom
        binary key is present, no blob is uploaded."""
    job = _sample_job(
        name='some-job', custom_binary_key='some-key', platform='some-platform')
    another_job = _sample_job(
        name='another-job',
        custom_binary_key='another-key',
        platform='another-platform')
    job.put()
    another_job.put()
    entity_migrator = job_exporter.EntityMigrator(data_types.Job,
                                                  ['custom_binary_key'], 'job',
                                                  job_exporter.StorageRSync(),
                                                  self.target_bucket)
    job_blob_data = b'some-data'
    job_blob_id = 'some-blob'
    job_proto_location = f'gs://{self.target_bucket}/job/{job.name}/entity.proto'
    blob_location = f'gs://{self.blobs_bucket}/{job_blob_id}'
    another_job_proto_location = (f'gs://{self.target_bucket}/'
                                  f'job/{another_job.name}/'
                                  f'entity.proto')
    another_job_blob_location = (f'gs://{self.target_bucket}/'
                                 f'job/{another_job.name}/'
                                 f'blobstore_key')
    storage.write_data(job_blob_data, blob_location)

    self.mock.get_gcs_path.return_value = blob_location
    entity_migrator.export_entities()

    self.assertTrue(_blob_is_present_in_gcs(job_proto_location))
    serialized_job_proto = storage.read_data(job_proto_location)
    deserialized_job_proto = entity_migrator._deserialize(serialized_job_proto)
    self.assertTrue(_jobs_equal(job, deserialized_job_proto))

    self.assertTrue(_blob_is_present_in_gcs(blob_location))
    self.assertTrue(_blob_content_is_equal(blob_location, job_blob_data))

    self.assertTrue(_blob_is_present_in_gcs(another_job_proto_location))
    serialized_another_job_proto = storage.read_data(another_job_proto_location)
    deserialized_another_job_proto = entity_migrator._deserialize(
        serialized_another_job_proto)
    self.assertTrue(_jobs_equal(another_job, deserialized_another_job_proto))

    self.assertFalse(_blob_is_present_in_gcs(another_job_blob_location))

    expected_persisted_entities = {'some-job', 'another-job'}
    entity_list_location = f'gs://{self.target_bucket}/job/entities'

    self.assertTrue(_blob_is_present_in_gcs(entity_list_location))
    self.assertTrue(
        _entity_list_contains_expected_entities(entity_list_location,
                                                expected_persisted_entities))

  def test_job_templates_are_correctly_exported(self):
    """Verifies job template proto is correctly uploaded."""
    template = _sample_job_template(
        name='some-job-template', environment_string='some-env-string')
    template.put()
    entity_migrator = job_exporter.EntityMigrator(data_types.JobTemplate, [],
                                                  'jobtemplate',
                                                  job_exporter.StorageRSync(),
                                                  self.target_bucket)
    template_proto_location = (f'gs://{self.target_bucket}/'
                               f'jobtemplate/{template.name}/'
                               f'entity.proto')
    entity_migrator.export_entities()

    self.assertTrue(_blob_is_present_in_gcs(template_proto_location))
    serialized_template_proto = storage.read_data(template_proto_location)
    deserialized_template_proto = entity_migrator._deserialize(
        serialized_template_proto)
    self.assertTrue(_job_templates_equal(template, deserialized_template_proto))

    expected_persisted_entities = {'some-job-template'}
    entity_list_location = f'gs://{self.target_bucket}/jobtemplate/entities'

    self.assertTrue(_blob_is_present_in_gcs(entity_list_location))
    self.assertTrue(
        _entity_list_contains_expected_entities(entity_list_location,
                                                expected_persisted_entities))

  def test_data_bundles_are_correctly_exported(self):
    """Verifies the proto is uploaded and blobs are rsynced correctly."""
    data_bundle = _sample_data_bundle(
        name='some-data-bundle',
        bucket_name='some-data-bundle-bucket',
    )
    data_bundle.put()
    entity_migrator = job_exporter.EntityMigrator(data_types.DataBundle, [],
                                                  'databundle',
                                                  job_exporter.StorageRSync(),
                                                  self.target_bucket)

    blob_data = b'some data'
    storage.create_bucket_if_needed(data_bundle.bucket_name)
    storage.write_data(blob_data, f'gs://{data_bundle.bucket_name}/blob')

    entity_migrator.export_entities()
    bundle_proto_location = (f'gs://{self.target_bucket}/'
                             f'databundle/{data_bundle.name}/'
                             f'entity.proto')
    bundle_contents_location = (f'gs://{self.target_bucket}/'
                                f'databundle/{data_bundle.name}/'
                                f'contents/blob')
    self.assertTrue(_blob_is_present_in_gcs(bundle_proto_location))
    serialized_bundle_proto = storage.read_data(bundle_proto_location)
    deserialized_bundle_proto = entity_migrator._deserialize(
        serialized_bundle_proto)
    self.assertTrue(_data_bundles_equal(data_bundle, deserialized_bundle_proto))

    self.assertTrue(_blob_is_present_in_gcs(bundle_proto_location))
    self.assertTrue(_blob_content_is_equal(bundle_contents_location, blob_data))

    expected_persisted_entities = {'some-data-bundle'}
    entity_list_location = f'gs://{self.target_bucket}/databundle/entities'

    self.assertTrue(_blob_is_present_in_gcs(entity_list_location))
    self.assertTrue(
        _entity_list_contains_expected_entities(entity_list_location,
                                                expected_persisted_entities))


@test_utils.with_cloud_emulators('datastore')
class TestFuzzersAreCorrectlyImported(unittest.TestCase):
  """Test the job exporter job with Fuzzer entities."""

  def setUp(self):
    helpers.patch_environ(self)
    self.local_gcs_buckets_path = tempfile.mkdtemp()
    self.blobs_bucket = 'BLOBS_BUCKET'
    self.import_source_bucket = 'SOURCE_BUCKET'
    os.environ['LOCAL_GCS_BUCKETS_PATH'] = self.local_gcs_buckets_path
    os.environ['TEST_BLOBS_BUCKET'] = self.blobs_bucket
    os.environ['EXPORT_BUCKET'] = self.import_source_bucket
    storage.create_bucket_if_needed(self.blobs_bucket)
    storage.create_bucket_if_needed(self.import_source_bucket)
    helpers.patch(self, [
        'clusterfuzz._internal.datastore.data_handler.get_data_bundle_bucket_name',
    ])

  def tearDown(self):
    shutil.rmtree(self.local_gcs_buckets_path, ignore_errors=True)

  def test_fuzzers_are_correctly_created_from_export_data(self):
    """Verifies if a fuzzer is created in Datastore with the correct contents,
      and if its blobs were correctly mirrored from the export data."""
    fuzzer_name = 'some-fuzzer'
    data_bundle_name = 'some-bundle'
    jobs = ['some-job']
    blobstore_key = 'some-blobstore-key'
    sample_testcase = 'some-sample-testcase'
    blobstore_key_payload = b'some-blobstore-data'
    sample_testcase_payload = b'some-testcase-data'

    some_fuzzer = _sample_fuzzer(
        data_bundle_name=data_bundle_name,
        name=fuzzer_name,
        jobs=jobs,
        blobstore_key=blobstore_key,
        sample_testcase=sample_testcase,
    )
    _upload_entity_export_data(
        entity=some_fuzzer,
        entity_kind='fuzzer',
        source_bucket=self.import_source_bucket,
        blobstore_key_content=blobstore_key_payload,
        sample_testcase_contents=sample_testcase_payload,
    )

    fuzzer_base_location = f'gs://{self.import_source_bucket}/fuzzer'
    _upload_entity_list([fuzzer_name], fuzzer_base_location)

    entity_migrator = job_exporter.EntityMigrator(
        data_types.Fuzzer, ['blobstore_key', 'sample_testcase'], 'fuzzer',
        job_exporter.StorageRSync(), self.import_source_bucket)
    entity_migrator.import_entities()

    fuzzers = list(data_types.Fuzzer.query())
    self.assertEqual(1, len(fuzzers))

    imported_fuzzer = fuzzers[0]
    self.assertEqual(fuzzer_name, imported_fuzzer.name)
    self.assertEqual(data_bundle_name, imported_fuzzer.data_bundle_name)
    self.assertEqual(jobs, imported_fuzzer.jobs)

    self.assertTrue(
        _entity_blob_was_correctly_imported(blobstore_key_payload,
                                            imported_fuzzer.blobstore_key))
    self.assertTrue(
        _entity_blob_was_correctly_imported(sample_testcase_payload,
                                            imported_fuzzer.sample_testcase))

  def test_fuzzers_are_correctly_deleted(self):
    """Verifies if a preexisting fuzzer is deleted from DataStore, if it
      is not in the export list anymore."""
    fuzzer_name = 'some-fuzzer'
    data_bundle_name = 'some-bundle'
    jobs = ['some-job']
    blobstore_key = 'some-blobstore-key'
    sample_testcase = 'some-sample-testcase'
    blobstore_key_payload = b'some-blobstore-data'
    sample_testcase_payload = b'some-testcase-data'

    some_fuzzer = _sample_fuzzer(
        data_bundle_name=data_bundle_name,
        name=fuzzer_name,
        jobs=jobs,
        blobstore_key=blobstore_key,
        sample_testcase=sample_testcase,
    )
    _register_entity_and_upload_blobs(
        entity=some_fuzzer,
        blobstore_key_content=blobstore_key_payload,
        sample_testcase_contents=sample_testcase_payload,
        blobs_bucket=self.blobs_bucket,
        custom_binary_contents=None,
    )

    previous_fuzzers = list(data_types.Fuzzer.query())
    self.assertEqual(1, len(previous_fuzzers))
    self.assertTrue(_fuzzers_equal(some_fuzzer, previous_fuzzers[0]))

    fuzzer_base_location = f'gs://{self.import_source_bucket}/fuzzer'

    # Zero entities declared to be exported
    _upload_entity_list([], fuzzer_base_location)

    entity_migrator = job_exporter.EntityMigrator(
        data_types.Fuzzer, ['blobstore_key', 'sample_testcase'], 'fuzzer',
        job_exporter.StorageRSync(), self.import_source_bucket)
    entity_migrator.import_entities()

    fuzzers = list(data_types.Fuzzer.query())
    self.assertEqual(0, len(fuzzers))

  def test_fuzzers_are_correctly_modified(self):
    """Checks if a fuzzer is correctly updated with new datastore fields
      and blob contents, in case a new version is exported."""
    fuzzer_name = 'some-fuzzer'
    data_bundle_name = 'some-bundle'
    jobs = ['some-job']

    some_fuzzer = _sample_fuzzer(
        data_bundle_name=data_bundle_name,
        name=fuzzer_name,
        jobs=jobs,
        blobstore_key=None,
        sample_testcase=None,
    )

    another_data_bundle_name = 'some-bundle'
    other_jobs = ['some-job']
    other_blobstore_key = 'some-blobstore-key'
    other_sample_testcase = 'some-sample-testcase'
    updated_fuzzer = _sample_fuzzer(
        data_bundle_name=another_data_bundle_name,
        name=fuzzer_name,
        jobs=other_jobs,
        blobstore_key=other_blobstore_key,
        sample_testcase=other_sample_testcase,
    )
    other_blobstore_key_payload = b'another-blobstore-data'
    other_sample_testcase_payload = b'another-testcase-data'

    _register_entity_and_upload_blobs(
        entity=some_fuzzer,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        blobs_bucket=self.blobs_bucket,
        custom_binary_contents=None,
    )

    _upload_entity_export_data(
        entity=updated_fuzzer,
        entity_kind='fuzzer',
        blobstore_key_content=other_blobstore_key_payload,
        sample_testcase_contents=other_sample_testcase_payload,
        source_bucket=self.import_source_bucket,
    )

    previous_fuzzers = list(data_types.Fuzzer.query())
    self.assertEqual(1, len(previous_fuzzers))
    self.assertTrue(_fuzzers_equal(some_fuzzer, previous_fuzzers[0]))

    fuzzer_base_location = f'gs://{self.import_source_bucket}/fuzzer'

    _upload_entity_list([fuzzer_name], fuzzer_base_location)

    entity_migrator = job_exporter.EntityMigrator(
        data_types.Fuzzer, ['blobstore_key', 'sample_testcase'], 'fuzzer',
        job_exporter.StorageRSync(), self.import_source_bucket)
    entity_migrator.import_entities()

    fuzzers = list(data_types.Fuzzer.query())
    self.assertEqual(1, len(fuzzers))

    imported_fuzzer = fuzzers[0]
    self.assertEqual(fuzzer_name, imported_fuzzer.name)
    self.assertEqual(another_data_bundle_name, imported_fuzzer.data_bundle_name)
    self.assertEqual(other_jobs, imported_fuzzer.jobs)

    self.assertTrue(
        _entity_blob_was_correctly_imported(other_blobstore_key_payload,
                                            imported_fuzzer.blobstore_key))
    self.assertTrue(
        _entity_blob_was_correctly_imported(other_sample_testcase_payload,
                                            imported_fuzzer.sample_testcase))


@test_utils.with_cloud_emulators('datastore')
class TestJobsAreCorrectlyImported(unittest.TestCase):
  """Test the job exporter job with Fuzzer entities."""

  def setUp(self):
    helpers.patch_environ(self)
    self.local_gcs_buckets_path = tempfile.mkdtemp()
    self.blobs_bucket = 'BLOBS_BUCKET'
    self.import_source_bucket = 'SOURCE_BUCKET'
    os.environ['LOCAL_GCS_BUCKETS_PATH'] = self.local_gcs_buckets_path
    os.environ['TEST_BLOBS_BUCKET'] = self.blobs_bucket
    os.environ['EXPORT_BUCKET'] = self.import_source_bucket
    storage.create_bucket_if_needed(self.blobs_bucket)
    storage.create_bucket_if_needed(self.import_source_bucket)
    helpers.patch(self, [
        'clusterfuzz._internal.datastore.data_handler.get_data_bundle_bucket_name',
    ])

  def tearDown(self):
    shutil.rmtree(self.local_gcs_buckets_path, ignore_errors=True)

  def test_fuzzers_are_correctly_created_from_export_data(self):
    """Verifies if a Job is created in Datastore with the correct contents,
      if its blobs were correctly mirrored from the export data, and if
      the environment string is correctly substituted."""
    job_name = 'some-job'
    custom_binary_key = 'some-key'
    platform = 'some-platform'
    prod_corpus_bucket = 'PROD_CORPUS_BUCKET'
    prod_log_bucket = 'PROD_LOG_BUCKET'
    original_env_string = (f'FUZZ_LOGS_BUCKET={prod_log_bucket};'
                           f'CORPUS_BUCKET={prod_corpus_bucket}')
    job_blob_data = b'some-data'

    job = _sample_job(
        name=job_name,
        custom_binary_key=custom_binary_key,
        platform=platform,
        environment_string=original_env_string)

    _upload_entity_export_data(
        entity=job,
        entity_kind='job',
        source_bucket=self.import_source_bucket,
        custom_binary_contents=job_blob_data,
    )

    job_base_location = f'gs://{self.import_source_bucket}/job'
    _upload_entity_list([job_name], job_base_location)

    test_log_bucket = 'TEST_LOG_BUCKET'
    test_corpus_bucket = 'TEST_CORPUS_BUCKET'
    substitutions = {
        prod_log_bucket: test_log_bucket,
        prod_corpus_bucket: test_corpus_bucket,
    }
    expected_env_string = (f'FUZZ_LOGS_BUCKET={test_log_bucket};'
                           f'CORPUS_BUCKET={test_corpus_bucket}')

    entity_migrator = job_exporter.EntityMigrator(
        data_types.Job, ['custom_binary_key'],
        'job',
        job_exporter.StorageRSync(),
        self.import_source_bucket,
        env_string_substitutions=substitutions)
    entity_migrator.import_entities()

    jobs = list(data_types.Job.query())

    self.assertEqual(1, len(jobs))
    imported_job = jobs[0]

    self.assertEqual(expected_env_string, imported_job.environment_string)
    self.assertEqual(job_name, imported_job.name)
    self.assertEqual(platform, imported_job.platform)

    self.assertTrue(
        _entity_blob_was_correctly_imported(job_blob_data,
                                            imported_job.custom_binary_key))

  def test_jobs_are_correctly_deleted(self):
    """Verifies that a job is deleted, if it is present in the environment
      but absent in the entity export list."""
    job_name = 'some-job'
    platform = 'some-platform'
    env_string = 'some-env-string'
    custom_binary_key = None

    job = _sample_job(
        name=job_name,
        custom_binary_key=custom_binary_key,
        platform=platform,
        environment_string=env_string)

    _register_entity_and_upload_blobs(
        entity=job,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        blobs_bucket=self.blobs_bucket,
        custom_binary_contents=None,
    )

    jobs = list(data_types.Job.query())

    self.assertEqual(1, len(jobs))
    imported_job = jobs[0]

    self.assertEqual(env_string, imported_job.environment_string)
    self.assertEqual(job_name, imported_job.name)
    self.assertEqual(platform, imported_job.platform)
    self.assertEqual(custom_binary_key, imported_job.custom_binary_key)

    job_base_location = f'gs://{self.import_source_bucket}/job'
    _upload_entity_list([], job_base_location)

    entity_migrator = job_exporter.EntityMigrator(data_types.Job,
                                                  ['custom_binary_key'], 'job',
                                                  job_exporter.StorageRSync(),
                                                  self.import_source_bucket)
    entity_migrator.import_entities()

    post_import_jobs = list(data_types.Job.query())

    self.assertEqual(0, len(post_import_jobs))

  def test_jobs_are_correctly_updated(self):
    """Verifies if a preexisting job is updated with new fields, custom binary, and the env
      string is correctly substituted."""
    job_name = 'some-job'
    platform = 'some-platform'
    env_string = 'some-env-string'
    custom_binary_key = None

    job = _sample_job(
        name=job_name,
        custom_binary_key=custom_binary_key,
        platform=platform,
        environment_string=env_string)

    _register_entity_and_upload_blobs(
        entity=job,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        blobs_bucket=self.blobs_bucket,
    )

    jobs = list(data_types.Job.query())

    self.assertEqual(1, len(jobs))
    imported_job = jobs[0]

    self.assertEqual(env_string, imported_job.environment_string)
    self.assertEqual(job_name, imported_job.name)
    self.assertEqual(platform, imported_job.platform)
    self.assertEqual(custom_binary_key, imported_job.custom_binary_key)

    another_custom_binary_data = b'some-other-data'
    another_platform = 'another-platform'
    env_string_before_import = 'some-unchanged-data'

    updated_job = _sample_job(
        name=job_name,
        platform=another_platform,
        environment_string=env_string_before_import,
    )

    _upload_entity_export_data(
        entity=updated_job,
        entity_kind='job',
        source_bucket=self.import_source_bucket,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=another_custom_binary_data,
    )

    env_string_after_import = 'some-changed-data'
    substitutions = {
        env_string_before_import: env_string_after_import,
    }

    job_base_location = f'gs://{self.import_source_bucket}/job'
    _upload_entity_list(
        [job_name],
        job_base_location,
    )

    entity_migrator = job_exporter.EntityMigrator(
        data_types.Job, ['custom_binary_key'],
        'job',
        job_exporter.StorageRSync(),
        self.import_source_bucket,
        env_string_substitutions=substitutions)
    entity_migrator.import_entities()

    post_import_jobs = list(data_types.Job.query())

    imported_job = post_import_jobs[0]

    self.assertEqual(1, len(post_import_jobs))

    self.assertEqual(env_string_after_import, imported_job.environment_string)
    self.assertEqual(job_name, imported_job.name)
    self.assertEqual(another_platform, imported_job.platform)

    self.assertTrue(
        _entity_blob_was_correctly_imported(another_custom_binary_data,
                                            imported_job.custom_binary_key))


@test_utils.with_cloud_emulators('datastore')
class TestJobTemplatesAreCorrectlyImported(unittest.TestCase):
  """Test the job exporter job with Fuzzer entities."""

  def setUp(self):
    helpers.patch_environ(self)
    self.local_gcs_buckets_path = tempfile.mkdtemp()
    self.blobs_bucket = 'BLOBS_BUCKET'
    self.import_source_bucket = 'SOURCE_BUCKET'
    os.environ['LOCAL_GCS_BUCKETS_PATH'] = self.local_gcs_buckets_path
    os.environ['TEST_BLOBS_BUCKET'] = self.blobs_bucket
    os.environ['EXPORT_BUCKET'] = self.import_source_bucket
    storage.create_bucket_if_needed(self.blobs_bucket)
    storage.create_bucket_if_needed(self.import_source_bucket)
    helpers.patch(self, [
        'clusterfuzz._internal.datastore.data_handler.get_data_bundle_bucket_name',
    ])

  def tearDown(self):
    shutil.rmtree(self.local_gcs_buckets_path, ignore_errors=True)

  def test_job_templates_are_correctly_imported(self):
    """Tests if a job template, previously absent, is correctly created."""
    template_name = 'some-template'
    prod_corpus_bucket = 'PROD_CORPUS_BUCKET'
    test_corpus_bucket = 'TEST_CORPUS_BUCKET'
    prod_log_bucket = 'PROD_LOG_BUCKET'
    test_log_bucket = 'TEST_LOG_BUCKET'
    original_env_string = f'FUZZ_LOGS_BUCKET={prod_log_bucket};CORPUS_BUCKET={prod_corpus_bucket}'
    expected_env_string = f'FUZZ_LOGS_BUCKET={test_log_bucket};CORPUS_BUCKET={test_corpus_bucket}'
    substitutions = {
        prod_log_bucket: test_log_bucket,
        prod_corpus_bucket: test_corpus_bucket,
    }
    template = _sample_job_template(
        name=template_name, environment_string=original_env_string)
    _upload_entity_export_data(
        entity=template,
        entity_kind='jobtemplate',
        source_bucket=self.import_source_bucket,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
    )

    job_template_base_location = f'gs://{self.import_source_bucket}/jobtemplate'
    _upload_entity_list([template_name], job_template_base_location)

    entity_migrator = job_exporter.EntityMigrator(
        data_types.JobTemplate, [],
        'jobtemplate',
        job_exporter.StorageRSync(),
        self.import_source_bucket,
        env_string_substitutions=substitutions)
    entity_migrator.import_entities()

    templates = list(data_types.JobTemplate.query())
    self.assertEqual(1, len(templates))

    imported_template = templates[0]
    self.assertEqual(template_name, imported_template.name)
    self.assertEqual(expected_env_string, imported_template.environment_string)

  def test_job_templates_are_correctly_deleted(self):
    """Tests if a previous job template is deleted, if it is not on
      the exported list."""
    template_name = 'some-template'
    env_string = 'some-env-string'
    template = _sample_job_template(
        name=template_name, environment_string=env_string)
    _register_entity_and_upload_blobs(
        entity=template,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        blobs_bucket=self.blobs_bucket,
    )

    templates = list(data_types.JobTemplate.query())
    self.assertEqual(1, len(templates))

    imported_template = templates[0]
    self.assertEqual(template_name, imported_template.name)
    self.assertEqual(env_string, imported_template.environment_string)

    job_template_base_location = f'gs://{self.import_source_bucket}/jobtemplate'
    _upload_entity_list([], job_template_base_location)

    entity_migrator = job_exporter.EntityMigrator(data_types.JobTemplate, [],
                                                  'jobtemplate',
                                                  job_exporter.StorageRSync(),
                                                  self.import_source_bucket)
    entity_migrator.import_entities()

    templates = list(data_types.JobTemplate.query())
    self.assertEqual(0, len(templates))

  def test_job_templates_are_correctly_updated(self):
    """Tests if a Job Template is correctly updated once it changes in the
      last export."""
    template_name = 'some-template'
    env_string = 'some-env-string'
    template = _sample_job_template(
        name=template_name, environment_string=env_string)
    _register_entity_and_upload_blobs(
        entity=template,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        blobs_bucket=self.blobs_bucket,
    )

    templates = list(data_types.JobTemplate.query())
    self.assertEqual(1, len(templates))

    imported_template = templates[0]
    self.assertEqual(template_name, imported_template.name)
    self.assertEqual(env_string, imported_template.environment_string)

    env_string_before_import = 'some-data'
    env_string_after_import = 'another-data'
    substitutions = {
        env_string_before_import: env_string_after_import,
    }

    updated_template = _sample_job_template(
        name=template_name,
        environment_string=env_string_before_import,
    )

    _upload_entity_export_data(
        entity=updated_template,
        entity_kind='jobtemplate',
        source_bucket=self.import_source_bucket,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
    )

    job_template_base_location = f'gs://{self.import_source_bucket}/jobtemplate'
    _upload_entity_list([template_name], job_template_base_location)

    entity_migrator = job_exporter.EntityMigrator(
        data_types.JobTemplate, [],
        'jobtemplate',
        job_exporter.StorageRSync(),
        self.import_source_bucket,
        env_string_substitutions=substitutions)
    entity_migrator.import_entities()

    templates = list(data_types.JobTemplate.query())
    self.assertEqual(1, len(templates))

    imported_template = templates[0]
    self.assertEqual(template_name, imported_template.name)
    self.assertEqual(env_string_after_import,
                     imported_template.environment_string)


@test_utils.with_cloud_emulators('datastore')
class TestDataBundlesAreCorrectlyImported(unittest.TestCase):
  """Test the job exporter job with DataBundle entities."""

  def setUp(self):
    helpers.patch_environ(self)
    self.local_gcs_buckets_path = tempfile.mkdtemp()
    self.blobs_bucket = 'BLOBS_BUCKET'
    self.import_source_bucket = 'SOURCE_BUCKET'
    os.environ['LOCAL_GCS_BUCKETS_PATH'] = self.local_gcs_buckets_path
    os.environ['TEST_BLOBS_BUCKET'] = self.blobs_bucket
    os.environ['EXPORT_BUCKET'] = self.import_source_bucket
    storage.create_bucket_if_needed(self.blobs_bucket)
    storage.create_bucket_if_needed(self.import_source_bucket)
    helpers.patch(self, [
        'clusterfuzz._internal.datastore.data_handler.get_data_bundle_bucket_name',
    ])

  def tearDown(self):
    shutil.rmtree(self.local_gcs_buckets_path, ignore_errors=True)

  def test_data_bundles_are_correctly_imported(self):
    """Tests if a data bundle entity was correctly imported, and the data
      bundle contents were correctly copied to a new bucket."""
    bundle_name = 'some-bundle'
    bucket_name = 'old-bucket'
    blob_data = b'some-data'
    bundle = _sample_data_bundle(
        name=bundle_name,
        bucket_name=bucket_name,
    )
    _upload_entity_export_data(
        entity=bundle,
        entity_kind='databundle',
        source_bucket=self.import_source_bucket,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        data_bundle_blob_contents=blob_data,
    )

    data_bundle_base_location = f'gs://{self.import_source_bucket}/databundle'
    _upload_entity_list([bundle_name], data_bundle_base_location)

    new_data_bundle_bucket = 'new-bundle-bucket'
    self.mock.get_data_bundle_bucket_name.return_value = new_data_bundle_bucket

    entity_migrator = job_exporter.EntityMigrator(data_types.DataBundle, [],
                                                  'databundle',
                                                  job_exporter.StorageRSync(),
                                                  self.import_source_bucket)
    entity_migrator.import_entities()

    data_bundles = list(data_types.DataBundle.query())
    self.assertEqual(1, len(data_bundles))

    imported_bundle = data_bundles[0]
    self.assertEqual(bundle_name, imported_bundle.name)
    self.assertEqual(new_data_bundle_bucket, imported_bundle.bucket_name)

    bundle_blob_location = f'gs://{new_data_bundle_bucket}/blob'

    self.assertTrue(_blob_is_present_in_gcs(bundle_blob_location))
    self.assertTrue(_blob_content_is_equal(bundle_blob_location, blob_data))

  def test_data_bundles_are_correctly_deleted(self):
    """Tests if a Data Bundle is correctly deleted if its name is not
      in the export list anymore."""
    bundle_name = 'some-bundle'
    bucket_name = 'old-bucket'
    blob_data = b'some-data'
    bundle = _sample_data_bundle(
        name=bundle_name,
        bucket_name=bucket_name,
    )
    _register_entity_and_upload_blobs(
        entity=bundle,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        blobs_bucket=self.blobs_bucket,
        data_bundle_blob_contents=blob_data,
    )

    data_bundles = list(data_types.DataBundle.query())
    self.assertEqual(1, len(data_bundles))

    imported_bundle = data_bundles[0]
    self.assertEqual(bundle_name, imported_bundle.name)
    self.assertEqual(bucket_name, imported_bundle.bucket_name)

    bundle_blob_location = f'gs://{bucket_name}/blob'

    self.assertTrue(_blob_is_present_in_gcs(bundle_blob_location))
    self.assertTrue(_blob_content_is_equal(bundle_blob_location, blob_data))

    # No entities to be imported, implies deletion of the current one
    data_bundle_base_location = f'gs://{self.import_source_bucket}/databundle'
    _upload_entity_list([], data_bundle_base_location)

    entity_migrator = job_exporter.EntityMigrator(data_types.DataBundle, [],
                                                  'databundle',
                                                  job_exporter.StorageRSync(),
                                                  self.import_source_bucket)
    entity_migrator.import_entities()

    remaining_data_bundles = list(data_types.DataBundle.query())
    self.assertEqual(0, len(remaining_data_bundles))

  def test_data_bundles_are_correctly_modified(self):
    """Tests if a Data Bundle and its blobs are correctly modified
      once a newer version is exported."""
    bundle_name = 'some-bundle'
    bucket_name = 'some-bucket'
    blob_data = b'some-data'
    bundle = _sample_data_bundle(
        name=bundle_name,
        bucket_name=bucket_name,
    )
    _register_entity_and_upload_blobs(
        entity=bundle,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        blobs_bucket=self.blobs_bucket,
        data_bundle_blob_contents=blob_data,
    )

    data_bundles = list(data_types.DataBundle.query())
    self.assertEqual(1, len(data_bundles))

    imported_bundle = data_bundles[0]
    self.assertEqual(bundle_name, imported_bundle.name)
    self.assertEqual(bucket_name, imported_bundle.bucket_name)

    bundle_blob_location = f'gs://{bucket_name}/blob'

    self.assertTrue(_blob_is_present_in_gcs(bundle_blob_location))
    self.assertTrue(_blob_content_is_equal(bundle_blob_location, blob_data))

    another_bucket = 'some-other-bucket'
    new_blob_data = b'new-data'
    updated_bundle = _sample_data_bundle(
        name=bundle_name,
        bucket_name=another_bucket,
    )
    _upload_entity_export_data(
        entity=updated_bundle,
        entity_kind='databundle',
        source_bucket=self.import_source_bucket,
        blobstore_key_content=None,
        sample_testcase_contents=None,
        custom_binary_contents=None,
        data_bundle_blob_contents=new_blob_data,
    )

    data_bundle_base_location = f'gs://{self.import_source_bucket}/databundle'
    _upload_entity_list([bundle_name], data_bundle_base_location)

    self.mock.get_data_bundle_bucket_name.return_value = another_bucket

    entity_migrator = job_exporter.EntityMigrator(data_types.DataBundle, [],
                                                  'databundle',
                                                  job_exporter.StorageRSync(),
                                                  self.import_source_bucket)
    entity_migrator.import_entities()

    data_bundles = list(data_types.DataBundle.query())
    self.assertEqual(1, len(data_bundles))

    imported_bundle = data_bundles[0]
    self.assertEqual(bundle_name, imported_bundle.name)
    self.assertEqual(another_bucket, imported_bundle.bucket_name)

    bundle_blob_location = f'gs://{another_bucket}/blob'

    self.assertTrue(_blob_is_present_in_gcs(bundle_blob_location))
    self.assertTrue(_blob_content_is_equal(bundle_blob_location, new_blob_data))
