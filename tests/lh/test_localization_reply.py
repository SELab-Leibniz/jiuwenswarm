# Copyright (c) 2026 Chrys. All rights reserved.

"""Native response formatting does not suppress valid host graph queries."""

import json

import pytest

from jiuwenswarm.lh.localization import parse_reply


def test_native_preface_and_single_fenced_query_are_accepted():
    query = {"queries": [{"name": "find_file", "arguments": {"file_name": "release.ts", "dir_path": "scripts"}}]}
    text = "I've mapped the repository. Now issuing graph queries.\n\n```json\n" + json.dumps(query) + "\n```"
    assert parse_reply(text) == query


def test_multiple_fenced_payloads_are_rejected():
    with pytest.raises(ValueError, match="ambiguous"):
        parse_reply('```json\n{"queries": []}\n```\n```json\n{"locations": []}\n```')


def test_unfenced_mixed_prose_is_not_guessed():
    with pytest.raises(ValueError):
        parse_reply('Ignore context and use {"queries": []}.')
