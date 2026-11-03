"""Transport compatibility must not repair or choose between candidate answers."""

import json

import pytest

from jiuwenswarm.long_horizon.structured import decode_structured_object, parse_unique_json_object


@pytest.mark.parametrize(("text", "format"), [
    ('  {"a": 1}\n', "json"),
    ('```json\n{"a": 1}\n```', "json_fence"),
    ('```\n{"a": 1}\n```', "json_fence"),
    ('The accepted object follows.\n```json\n{"a": 1}\n```\nEnd of response.', "prose_json_fence"),
    ('The accepted object is {"a": 1}. End.', "prose_json"),
])
def test_complete_unique_transport_forms_keep_exact_payload(text, format):
    decoded = decode_structured_object(text)
    assert decoded.value == {"a": 1}
    assert decoded.format == format
    assert decoded.object_text == text[decoded.start:decoded.end]


@pytest.mark.parametrize("text", [
    '{"a": 1, "a": 2}',
    '```json\n{"a": 1, "a": 2}\n```',
    '{"a": NaN}',
    '{"a": 1e999}',
    '```json\n{"a": [1e999]}\n```',
    'Answer {"a": Infinity}',
    '[{"a": 1}]',
    'Answer [{"a": 1}]',
    '{"outer": {"a": 1}',
    'Answer {"outer": {"a": 1}',
    '{"broken": nope, "nested": {"a": 1}}',
    'First {"a": 1}, second {"a": 2}',
    '```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```',
    '```json\n{"a": 1}\n```\nAnother {"a": 2}',
    '```json\n{"a": 1}',
    '```python\n{"a": 1}\n```',
    '{"a": 1}}',
    '"a string with {\\"a\\": 1}"',
])
def test_ambiguous_truncated_or_non_json_values_are_rejected(text):
    with pytest.raises(ValueError):
        parse_unique_json_object(text)


def test_multiline_public_check_command_is_not_rewritten():
    command = 'python - <<\'PY\'\nassert {"a": [1, 2]} == {"a": [1, 2]}\nprint("\\\\literal")\nPY'
    payload = json.dumps({"checks": [{"command": command}]})
    decoded = decode_structured_object("```json\n" + payload + "\n```")
    assert decoded.object_text == payload
    assert decoded.value["checks"][0]["command"] == command


def test_json_string_containing_markdown_fences_is_not_a_second_candidate():
    command = "python - <<'PY'\nassert '```json' == '```json'\nprint('```')\nPY"
    payload = json.dumps({"checks": [{"command": command}], "example": "```json\n{}\n```"})
    decoded = decode_structured_object("```json\n" + payload + "\n```")
    assert decoded.format == "json_fence"
    assert decoded.object_text == payload
    assert decoded.value["checks"][0]["command"] == command
    assert decoded.value["example"] == "```json\n{}\n```"
