# -*- coding: utf-8 -*-
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Integration tests for rewrite command."""

from __future__ import absolute_import

import logging
import os
import re

from boto.storage_uri import BucketStorageUri

from gslib.cloud_api import CryptoTuple
from gslib.cs_api_map import ApiSelector
from gslib.gcs_json_api import GcsJsonApi
from gslib.tests.rewrite_helper import EnsureRewriteRestartCallbackHandler
from gslib.tests.rewrite_helper import EnsureRewriteResumeCallbackHandler
from gslib.tests.rewrite_helper import HaltingRewriteCallbackHandler
from gslib.tests.rewrite_helper import RewriteHaltException
import gslib.tests.testcase as testcase
from gslib.tests.testcase.integration_testcase import SkipForS3
from gslib.tests.util import GenerationFromURI as urigen
from gslib.tests.util import ObjectToURI as suri
from gslib.tests.util import SetBotoConfigForTest
from gslib.tests.util import TEST_ENCRYPTION_KEY1
from gslib.tests.util import TEST_ENCRYPTION_KEY2
from gslib.tests.util import TEST_ENCRYPTION_KEY3
from gslib.tests.util import TEST_ENCRYPTION_KEY4
from gslib.tests.util import unittest
from gslib.tracker_file import DeleteTrackerFile
from gslib.tracker_file import GetRewriteTrackerFilePath
from gslib.util import DiscardMessagesQueue
from gslib.util import ONE_MIB


@SkipForS3('gsutil doesn\'t support S3 customer-supplied encryption keys.')
class TestRewrite(testcase.GsUtilIntegrationTestCase):
  """Integration tests for rewrite command."""

  def test_rewrite_missing_flag(self):
    """Tests rewrite with no transformation flag."""
    stderr = self.RunGsUtil(
        ['rewrite', '%s://some_url' % self.default_provider],
        return_stderr=True, expected_status=1)
    self.assertIn('command requires at least one transformation flag', stderr)

  def test_rewrite_generation_url(self):
    """Tests that rewrite fails on a URL that includes a generation."""
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)
    generation = object_uri.generation
    stderr = self.RunGsUtil(
        ['rewrite', '-k', '%s#%s' % (suri(object_uri), generation)],
        return_stderr=True, expected_status=1)
    self.assertIn('"rewrite" called on URL with generation', stderr)

  def test_rewrite_missing_decryption_key(self):
    """Tests that rewrite fails when no decryption key matches."""
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(object_name='foo', contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)
    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY3)]
    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(['rewrite', '-k', suri(object_uri)],
                              return_stderr=True, expected_status=1)
      self.assertIn('No decryption key matches object %s' %
                    suri(object_uri), stderr)

  def test_rewrite_stdin_args(self):
    """Tests rewrite with arguments supplied on stdin."""
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)
    stdin_arg = suri(object_uri)

    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1)]
    with SetBotoConfigForTest(boto_config_for_test):
      self.RunGsUtil(['rewrite', '-k', '-I'], stdin=stdin_arg)
    self.AssertObjectUsesEncryptionKey(stdin_arg, TEST_ENCRYPTION_KEY2)

  def test_rewrite_overwrite_acl(self):
    """Tests rewrite with the -O flag."""
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)
    self.RunGsUtil(['acl', 'ch', '-u', 'AllUsers:R', suri(object_uri)])
    stdout = self.RunGsUtil(['acl', 'get', suri(object_uri)],
                            return_stdout=True)
    self.assertIn('allUsers', stdout)

    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1)]
    with SetBotoConfigForTest(boto_config_for_test):
      self.RunGsUtil(['rewrite', '-k', '-O', suri(object_uri)])
    self.AssertObjectUsesEncryptionKey(suri(object_uri), TEST_ENCRYPTION_KEY2)
    stdout = self.RunGsUtil(['acl', 'get', suri(object_uri)],
                            return_stdout=True)
    self.assertNotIn('allUsers', stdout)

  def test_rewrite_bucket_recursive(self):
    """Tests rewrite command recursively on a bucket."""
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    bucket_uri = self.CreateBucket()
    self._test_rewrite_key_rotation_bucket(
        bucket_uri, ['rewrite', '-k', '-r', suri(bucket_uri)])

  def test_parallel_rewrite_bucket_flat_wildcard(self):
    """Tests parallel rewrite command with a flat wildcard on a bucket."""
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    bucket_uri = self.CreateBucket()
    self._test_rewrite_key_rotation_bucket(
        bucket_uri, ['-m', 'rewrite', '-k', suri(bucket_uri, '**')])

  def _test_rewrite_key_rotation_bucket(self, bucket_uri, command_args):
    """Helper function for testing key rotation on a bucket.

    Args:
      bucket_uri: bucket StorageUri to use for the test.
      command_args: list of args to gsutil command.
    """
    object_contents = 'bar'
    object_uri1 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='foo/foo',
                                    contents=object_contents,
                                    encryption_key=TEST_ENCRYPTION_KEY1)
    object_uri2 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='foo/bar',
                                    contents=object_contents,
                                    encryption_key=TEST_ENCRYPTION_KEY2)
    object_uri3 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='foo/baz',
                                    contents=object_contents,
                                    encryption_key=TEST_ENCRYPTION_KEY3)
    object_uri4 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='foo/qux',
                                    contents=object_contents)

    # Rotate all keys to TEST_ENCRYPTION_KEY1.
    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY1),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key2', TEST_ENCRYPTION_KEY3)]

    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(command_args, return_stderr=True)
      # Object one already has the correct key.
      self.assertIn('Skipping %s' % suri(object_uri1), stderr)
      # Other objects should be rotated.
      self.assertIn('Rotating', stderr)
    for object_uri_str in (suri(object_uri1), suri(object_uri2),
                           suri(object_uri3), suri(object_uri4)):
      self.AssertObjectUsesEncryptionKey(object_uri_str,
                                         TEST_ENCRYPTION_KEY1)

    # Remove all encryption.
    boto_config_for_test2 = [
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1)]

    with SetBotoConfigForTest(boto_config_for_test2):
      stderr = self.RunGsUtil(command_args, return_stderr=True)
      self.assertIn('Decrypting', stderr)

    for object_uri_str in (suri(object_uri1), suri(object_uri2),
                           suri(object_uri3), suri(object_uri4)):
      self.AssertObjectUnencrypted(object_uri_str)

  def test_rewrite_seek_ahead(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)
    # Remove encryption
    boto_config_for_test = [
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1),
        ('GSUtil', 'task_estimation_threshold', '1'),
        ('GSUtil', 'task_estimation_force', 'True')]
    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(['-m', 'rewrite', '-k', suri(object_uri)],
                              return_stderr=True)
      self.assertIn(
          'Estimated work for this command: objects: 1, total size: 3', stderr)

  def test_rewrite_unintentional_key_rotation_fails(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    encrypted_obj_uri = self.CreateObject(contents='bar',
                                          encryption_key=TEST_ENCRYPTION_KEY1)
    unencrypted_obj_uri = self.CreateObject(contents='bar')

    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1)]
    with SetBotoConfigForTest(boto_config_for_test):
      # Executing rewrite without the -k flag should fail if your boto file has
      # a different encryption_key than was last used to encrypt the object.
      stderr = self.RunGsUtil(['rewrite', '-s', 'dra', suri(encrypted_obj_uri)],
                              return_stderr=True, expected_status=1)
      self.assertIn('EncryptionException', stderr)

      # Should also fail for a previously unencrypted object.
      stderr = self.RunGsUtil(
          ['rewrite', '-s', 'dra', suri(unencrypted_obj_uri)],
          return_stderr=True, expected_status=1)
      self.assertIn('EncryptionException', stderr)

  def test_rewrite_key_rotation_single_object(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)

    # Rotate key.
    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1)]

    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(['rewrite', '-k', suri(object_uri)],
                              return_stderr=True)
      self.assertIn('Rotating', stderr)

    self.AssertObjectUsesEncryptionKey(suri(object_uri),
                                       TEST_ENCRYPTION_KEY2)

    # Remove encryption.
    boto_config_for_test2 = [
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY2)]
    with SetBotoConfigForTest(boto_config_for_test2):
      stderr = self.RunGsUtil(['rewrite', '-k', suri(object_uri)],
                              return_stderr=True)
      self.assertIn('Decrypting', stderr)

    self.AssertObjectUnencrypted(suri(object_uri))

  def test_rewrite_key_rotation_bucket_subdir(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    bucket_uri = self.CreateBucket()
    object_contents = 'bar'
    rotate_subdir = suri(bucket_uri, 'bar')
    object_uri1 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='foo/bar',
                                    contents=object_contents,
                                    encryption_key=TEST_ENCRYPTION_KEY1)
    object_uri2 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='bar/foo',
                                    contents=object_contents,
                                    encryption_key=TEST_ENCRYPTION_KEY2)
    object_uri3 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='bar/baz',
                                    contents=object_contents,
                                    encryption_key=TEST_ENCRYPTION_KEY3)
    object_uri4 = self.CreateObject(bucket_uri=bucket_uri,
                                    object_name='bar/qux',
                                    contents=object_contents)

    # Rotate subdir keys to TEST_ENCRYPTION_KEY3.
    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY3),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key2', TEST_ENCRYPTION_KEY1)]

    self.AssertNObjectsInBucket(bucket_uri, 4)

    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(['rewrite', '-r', '-k', rotate_subdir],
                              return_stderr=True)
      self.assertIn('Rotating', stderr)  # Object 2.
      self.assertIn('Skipping %s' % suri(object_uri3), stderr)
      self.assertIn('Encrypting', stderr)  # Object 4.

    # First subdir should be unaffected.
    self.AssertObjectUsesEncryptionKey(suri(object_uri1),
                                       TEST_ENCRYPTION_KEY1)

    for object_uri_str in (suri(object_uri2), suri(object_uri3),
                           suri(object_uri4)):
      self.AssertObjectUsesEncryptionKey(object_uri_str,
                                         TEST_ENCRYPTION_KEY3)

    # Remove encryption in subdir.
    boto_config_for_test2 = [
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY3)]

    with SetBotoConfigForTest(boto_config_for_test2):
      stderr = self.RunGsUtil(['rewrite', '-r', '-k', rotate_subdir],
                              return_stderr=True)
      self.assertIn('Decrypting', stderr)

    # First subdir should be unaffected.
    self.AssertObjectUsesEncryptionKey(suri(object_uri1),
                                       TEST_ENCRYPTION_KEY1)

    for object_uri_str in (suri(object_uri2), suri(object_uri3),
                           suri(object_uri4)):
      self.AssertObjectUnencrypted(object_uri_str)

  def test_rewrite_with_nonkey_transform_works_when_key_is_unchanged(self):
    # Tests that when a valid transformation flag aside from "-k" is supplied,
    # the "-k" flag is not supplied, and the encryption key previously used to
    # encrypt the target object matches the encryption_key in the user's boto
    # config file (via hash comparison), that the rewrite command properly
    # passes the same tuple for decryption and encryption, in addition to
    # performing the other desired transformations.
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)
    boto_config_for_test = [('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY1)]
    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(
          ['rewrite', '-s', 'nearline', suri(object_uri)], return_stderr=True)
      self.assertIn('Rewriting', stderr)

  def test_rewrite_key_rotation_with_storage_class_change(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar',
                                   encryption_key=TEST_ENCRYPTION_KEY1)

    # Rotate key and change storage class to nearline.
    boto_config_for_test = [
        ('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY2),
        ('GSUtil', 'decryption_key1', TEST_ENCRYPTION_KEY1)]
    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(
          ['rewrite', '-s', 'nearline', '-k', suri(object_uri)],
          return_stderr=True)
      self.assertIn('Rotating', stderr)

    self.AssertObjectUsesEncryptionKey(suri(object_uri),
                                       TEST_ENCRYPTION_KEY2)
    stdout = self.RunGsUtil(['stat', suri(object_uri)], return_stdout=True)
    self.assertRegexpMatchesWithFlags(
        stdout, r'Storage class:\s+NEARLINE', flags=re.IGNORECASE,
        msg=('Storage class appears not to have been changed.'))

  def test_rewrite_with_only_storage_class_change(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar')

    # Change storage class to nearline.
    stderr = self.RunGsUtil(['rewrite', '-s', 'nearline', suri(object_uri)],
                            return_stderr=True)
    self.assertIn('Rewriting', stderr)

    stdout = self.RunGsUtil(['stat', suri(object_uri)], return_stdout=True)
    self.assertRegexpMatchesWithFlags(
        stdout, r'Storage class:\s+NEARLINE', flags=re.IGNORECASE,
        msg=('Storage class appears not to have been changed.'))

  def test_rewrite_to_same_storage_class_is_skipped(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='bar')
    stderr = self.RunGsUtil(['rewrite', '-s', 'standard', suri(object_uri)],
                            return_stderr=True)
    self.assertIn('Skipping %s' % suri(object_uri), stderr)

  def test_rewrite_with_same_key_and_storage_class_is_skipped(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    object_uri = self.CreateObject(contents='foo',
                                   encryption_key=TEST_ENCRYPTION_KEY1,
                                   storage_class='standard')

    boto_config_for_test = [('GSUtil', 'encryption_key', TEST_ENCRYPTION_KEY1)]
    with SetBotoConfigForTest(boto_config_for_test):
      stderr = self.RunGsUtil(
          ['rewrite', '-k', '-s', 'standard', suri(object_uri)],
          return_stderr=True)
    self.assertIn('Skipping %s' % suri(object_uri), stderr)

  def test_rewrite_with_no_value_for_minus_s(self):
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    stderr = self.RunGsUtil(['rewrite', '-s', 'gs://some-random-name'],
                            return_stderr=True, expected_status=1)

    self.assertIn('CommandException', stderr)
    self.assertIn('expects at least one URL', stderr)

  def test_rewrite_resume(self):
    self._test_rewrite_resume_or_restart(
        TEST_ENCRYPTION_KEY1, TEST_ENCRYPTION_KEY2)

  def test_rewrite_resume_restart_source_encryption_changed(self):
    self._test_rewrite_resume_or_restart(
        TEST_ENCRYPTION_KEY1, TEST_ENCRYPTION_KEY2,
        new_dec_key=TEST_ENCRYPTION_KEY3)

  def test_rewrite_resume_restart_dest_encryption_changed(self):
    self._test_rewrite_resume_or_restart(
        TEST_ENCRYPTION_KEY1, TEST_ENCRYPTION_KEY2,
        new_enc_key=TEST_ENCRYPTION_KEY3)

  def test_rewrite_resume_restart_both_encryption_changed(self):
    self._test_rewrite_resume_or_restart(
        TEST_ENCRYPTION_KEY1, TEST_ENCRYPTION_KEY2,
        new_dec_key=TEST_ENCRYPTION_KEY3, new_enc_key=TEST_ENCRYPTION_KEY4)

  def _test_rewrite_resume_or_restart(self, initial_dec_key, initial_enc_key,
                                      new_dec_key=None, new_enc_key=None):
    """Tests that the rewrite command restarts if the object's key changed.

    Args:
      initial_dec_key: Initial key the object is encrypted with, used as
          decryption key in the first rewrite call.
      initial_enc_key: Initial encryption key to rewrite the object with,
          used as encryption key in the first rewrite call.
      new_dec_key: Decryption key for the second rewrite call; if specified,
          object will be overwritten with a new encryption key in between
          the first and second rewrite calls, and this key will be used for
          the second rewrite call.
      new_enc_key: Encryption key for the second rewrite call; if specified,
          this key will be used for the second rewrite call, otherwise the
          initial key will be used.

    Returns:
      None
    """
    if self.test_api == ApiSelector.XML:
      return unittest.skip('Rewrite API is only supported in JSON.')
    bucket_uri = self.CreateBucket()
    # maxBytesPerCall must be >= 1 MiB, so create an object > 2 MiB because we
    # need 2 response from the service: 1 success, 1 failure prior to
    # completion.
    object_uri = self.CreateObject(bucket_uri=bucket_uri, object_name='foo',
                                   contents=('12'*ONE_MIB) + 'bar',
                                   prefer_json_api=True,
                                   encryption_key=initial_dec_key)
    gsutil_api = GcsJsonApi(BucketStorageUri, logging.getLogger(),
                            DiscardMessagesQueue(), self.default_provider)
    with SetBotoConfigForTest(
        [('GSUtil', 'decryption_key1', initial_dec_key)]):
      src_obj_metadata = gsutil_api.GetObjectMetadata(
          object_uri.bucket_name, object_uri.object_name,
          provider=self.default_provider, fields=['bucket', 'contentType',
                                                  'etag', 'name'])
    dst_obj_metadata = src_obj_metadata
    tracker_file_name = GetRewriteTrackerFilePath(
        src_obj_metadata.bucket, src_obj_metadata.name,
        dst_obj_metadata.bucket, dst_obj_metadata.name, self.test_api)
    decryption_tuple = CryptoTuple(initial_dec_key)
    decryption_tuple2 = CryptoTuple(new_dec_key or initial_dec_key)
    encryption_tuple = CryptoTuple(initial_enc_key)
    encryption_tuple2 = CryptoTuple(new_enc_key or initial_enc_key)

    try:
      try:
        gsutil_api.CopyObject(
            src_obj_metadata, dst_obj_metadata,
            progress_callback=HaltingRewriteCallbackHandler(ONE_MIB*2).call,
            max_bytes_per_call=ONE_MIB, decryption_tuple=decryption_tuple,
            encryption_tuple=encryption_tuple)
        self.fail('Expected RewriteHaltException.')
      except RewriteHaltException:
        pass

      # Tracker file should be left over.
      self.assertTrue(os.path.exists(tracker_file_name))

      if new_dec_key:
        # Recreate the object with a different encryption key.
        self.CreateObject(
            bucket_uri=bucket_uri, object_name='foo',
            contents=('12'*ONE_MIB) + 'bar', prefer_json_api=True,
            encryption_key=new_dec_key,
            gs_idempotent_generation=urigen(object_uri))

      with SetBotoConfigForTest([
          ('GSUtil', 'decryption_key1', new_dec_key or initial_dec_key)]):
        original_md5 = gsutil_api.GetObjectMetadata(
            src_obj_metadata.bucket, src_obj_metadata.name,
            fields=['customerEncryption', 'md5Hash']).md5Hash

      if new_dec_key or new_enc_key:
        # Keys changed, rewrite should be restarted.
        progress_callback = EnsureRewriteRestartCallbackHandler(ONE_MIB).call
      else:
        # Keys are the same, rewrite should be resumed.
        progress_callback = EnsureRewriteResumeCallbackHandler(ONE_MIB*2).call

      # Now resume. Callback ensures the appropriate resume/restart behavior.
      gsutil_api.CopyObject(
          src_obj_metadata, dst_obj_metadata,
          progress_callback=progress_callback, max_bytes_per_call=ONE_MIB,
          decryption_tuple=decryption_tuple2,
          encryption_tuple=encryption_tuple2)

      # Copy completed; tracker file should be deleted.
      self.assertFalse(os.path.exists(tracker_file_name))

      final_enc_key = new_enc_key or initial_enc_key

      with SetBotoConfigForTest([
          ('GSUtil', 'encryption_key', final_enc_key)]):
        self.assertEqual(
            original_md5,
            gsutil_api.GetObjectMetadata(dst_obj_metadata.bucket,
                                         dst_obj_metadata.name,
                                         fields=['customerEncryption',
                                                 'md5Hash']).md5Hash,
            'Error: Rewritten object\'s hash doesn\'t match source object.')
    finally:
      # Clean up if something went wrong.
      DeleteTrackerFile(tracker_file_name)

