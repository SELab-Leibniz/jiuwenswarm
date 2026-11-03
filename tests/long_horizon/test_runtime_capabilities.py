"""Launcher receipt verification is read-only and does not execute a command."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from jiuwenswarm.long_horizon.clarification.runtime_capabilities import read_public_runtime_capabilities


class RuntimeCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.installed = self.root / 'installed-public-tools'
        self.installed.mkdir()
        self.helper = self.installed / 'lolbench-submit'
        self.marker = self.root / 'must-not-execute'
        self.helper.write_text('#!/bin/sh\nprintf executed > ' + str(self.marker) + '\n')
        self.helper.chmod(0o755)
        self.receipt = self.root / 'public-runtime-capabilities.json'
        self.payload = {
            'schema': 'jiuwenswarm/public-runtime-capabilities/v1',
            'commands': [{
                'name': 'lolbench-submit', 'executable': str(self.helper),
                'sha256': hashlib.sha256(self.helper.read_bytes()).hexdigest(),
                'invocation': 'lolbench-submit',
                'description': 'Writes the user-requested patch artifact from the current checkout.',
            }],
            'execution_identity': {'user': 'root', 'runtime': 'generation'},
        }
        self.write_receipt()

    def tearDown(self):
        self.temporary.cleanup()

    def write_receipt(self):
        self.receipt.write_text(json.dumps(self.payload))

    def test_empty_configuration_has_no_filesystem_or_environment_discovery(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('No file should be read')):
            self.assertEqual(read_public_runtime_capabilities(''), '')

    def test_receipt_verifies_installed_bytes_without_executing_or_reading_environment(self):
        with patch.dict(os.environ, {'UNDECLARED_RUNTIME_FACT': 'must-not-be-exposed'}):
            output = read_public_runtime_capabilities(str(self.receipt))
        result = json.loads(output)
        self.assertEqual(result, self.payload)
        self.assertNotIn('must-not-be-exposed', output)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.helper.read_text(), '#!/bin/sh\nprintf executed > ' + str(self.marker) + '\n')

    def test_changed_installed_bytes_are_rejected(self):
        self.helper.write_text('changed public helper\n')
        with self.assertRaisesRegex(ValueError, 'executable changed: lolbench-submit'):
            read_public_runtime_capabilities(str(self.receipt))

    def test_relative_missing_and_directory_executables_are_rejected(self):
        for executable in ['relative-submit', str(self.root / 'missing'), str(self.installed)]:
            with self.subTest(executable=executable):
                self.payload['commands'][0]['executable'] = executable
                self.write_receipt()
                with self.assertRaisesRegex(ValueError, 'executable is unavailable'):
                    read_public_runtime_capabilities(str(self.receipt))

    def test_symbolic_installed_path_is_bound_to_actual_target_bytes(self):
        link = self.installed / 'public-submit-link'
        link.symlink_to(self.helper)
        self.payload['commands'][0]['executable'] = str(link)
        self.write_receipt()
        self.assertEqual(json.loads(read_public_runtime_capabilities(str(self.receipt))), self.payload)
        self.helper.write_text('changed link target\n')
        with self.assertRaisesRegex(ValueError, 'executable changed'):
            read_public_runtime_capabilities(str(self.receipt))

    def test_closed_manifest_and_command_schemas_reject_extra_fields(self):
        for top_level in (True, False):
            with self.subTest(top_level=top_level):
                target = self.payload if top_level else self.payload['commands'][0]
                target['unknown_field'] = 'undeclared'
                self.write_receipt()
                with self.assertRaises(ValidationError):
                    read_public_runtime_capabilities(str(self.receipt))
                del target['unknown_field']

    def test_wrong_schema_and_malformed_hash_are_rejected(self):
        self.payload['schema'] = 'chrys/public-runtime-capabilities/v1'
        self.write_receipt()
        with self.assertRaises(ValidationError):
            read_public_runtime_capabilities(str(self.receipt))
        self.payload['schema'] = 'jiuwenswarm/public-runtime-capabilities/v1'
        self.payload['commands'][0]['sha256'] = 'not-a-hash'
        self.write_receipt()
        with self.assertRaises(ValidationError):
            read_public_runtime_capabilities(str(self.receipt))

    def test_size_count_and_malformed_json_limits_are_enforced(self):
        self.receipt.write_bytes(b' ' * (128 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, 'manifest is too large'):
            read_public_runtime_capabilities(str(self.receipt))
        self.receipt.write_text('{invalid JSON')
        with self.assertRaises(ValidationError):
            read_public_runtime_capabilities(str(self.receipt))
        self.payload['commands'] *= 21
        self.write_receipt()
        with self.assertRaises(ValidationError):
            read_public_runtime_capabilities(str(self.receipt))

    def test_empty_commands_do_not_invent_public_capabilities(self):
        self.payload['commands'] = []
        self.write_receipt()
        self.assertEqual(json.loads(read_public_runtime_capabilities(str(self.receipt))), self.payload)
        self.assertFalse(self.marker.exists())
