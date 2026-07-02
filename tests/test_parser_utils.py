"""Unit tests for the pure helpers in reliefplan.parser.

Only _to_schema and _extract_fc_args are exercised — nothing here creates a
Gemini client or requires GEMINI_API_KEY.
"""
from types import SimpleNamespace

from google.genai import types as gtypes

from reliefplan.parser import _extract_fc_args, _to_schema

# ---------------------------------------------------------------------------
# _to_schema
# ---------------------------------------------------------------------------

class TestToSchema:
    def test_scalar_types_mapped(self):
        assert _to_schema({"type": "string"}).type == gtypes.Type.STRING
        assert _to_schema({"type": "integer"}).type == gtypes.Type.INTEGER
        assert _to_schema({"type": "boolean"}).type == gtypes.Type.BOOLEAN
        assert _to_schema({"type": "number"}).type == gtypes.Type.NUMBER

    def test_missing_or_unknown_type_defaults_to_string(self):
        assert _to_schema({}).type == gtypes.Type.STRING
        assert _to_schema({"type": "flurble"}).type == gtypes.Type.STRING

    def test_nullable_type_list_picks_non_null(self):
        assert _to_schema({"type": ["null", "integer"]}).type == gtypes.Type.INTEGER
        # All-null list falls back to string
        assert _to_schema({"type": ["null"]}).type == gtypes.Type.STRING

    def test_description_propagated(self):
        s = _to_schema({"type": "string", "description": "a name"})
        assert s.description == "a name"

    def test_nested_object_with_array(self):
        schema = _to_schema({
            "type": "object",
            "properties": {
                "rooms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "flags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id"],
                    },
                },
            },
            "required": ["rooms"],
        })
        assert schema.type == gtypes.Type.OBJECT
        assert schema.required == ["rooms"]
        rooms = schema.properties["rooms"]
        assert rooms.type == gtypes.Type.ARRAY
        item = rooms.items
        assert item.type == gtypes.Type.OBJECT
        assert item.required == ["id"]
        assert item.properties["id"].type == gtypes.Type.INTEGER
        flags = item.properties["flags"]
        assert flags.type == gtypes.Type.ARRAY
        assert flags.items.type == gtypes.Type.STRING

    def test_enum_strips_empty_strings(self):
        s = _to_schema({"type": "string", "enum": ["R2", "R3", "R4", ""]})
        assert s.enum == ["R2", "R3", "R4"]

    def test_enum_strips_none_and_coerces_to_str(self):
        s = _to_schema({"type": "string", "enum": [None, "a", 1]})
        assert s.enum == ["a", "1"]

    def test_enum_all_empty_is_dropped(self):
        s = _to_schema({"type": "string", "enum": ["", None]})
        assert s.enum is None

    def test_unsupported_keywords_dropped(self):
        s = _to_schema({
            "type": "string",
            "format": "date-time",
            "minLength": 3,
            "pattern": "^x",
            "default": "y",
        })
        assert s.type == gtypes.Type.STRING
        # None of the unsupported keywords should have been forwarded
        assert s.format is None
        assert s.min_length is None
        assert s.pattern is None
        assert s.default is None

    def test_object_without_properties_has_none(self):
        s = _to_schema({"type": "object"})
        assert s.type == gtypes.Type.OBJECT
        assert s.properties is None


# ---------------------------------------------------------------------------
# _extract_fc_args
# ---------------------------------------------------------------------------

def _response(parts_per_candidate):
    """Build a fake Gemini response: list of candidates, each with content.parts."""
    return SimpleNamespace(candidates=[
        SimpleNamespace(content=SimpleNamespace(parts=parts))
        for parts in parts_per_candidate
    ])


def _fc_part(name, args):
    return SimpleNamespace(function_call=SimpleNamespace(name=name, args=args))


class TestExtractFcArgs:
    def test_matching_call_returns_plain_dict(self):
        resp = _response([[_fc_part("extract_schedule", {"rooms": [{"id": "44"}]})]])
        args = _extract_fc_args(resp, "extract_schedule")
        assert args == {"rooms": [{"id": "44"}]}
        assert type(args) is dict

    def test_wrong_tool_name_returns_none(self):
        resp = _response([[_fc_part("other_tool", {"x": 1})]])
        assert _extract_fc_args(resp, "extract_schedule") is None

    def test_no_candidates_returns_none(self):
        assert _extract_fc_args(SimpleNamespace(candidates=None), "t") is None
        assert _extract_fc_args(SimpleNamespace(candidates=[]), "t") is None

    def test_none_parts_returns_none(self):
        resp = SimpleNamespace(candidates=[
            SimpleNamespace(content=SimpleNamespace(parts=None))
        ])
        assert _extract_fc_args(resp, "t") is None

    def test_part_without_function_call_skipped(self):
        text_part = SimpleNamespace(function_call=None)
        resp = _response([[text_part, _fc_part("mytool", {"a": 1})]])
        assert _extract_fc_args(resp, "mytool") == {"a": 1}

    def test_none_args_returns_empty_dict(self):
        resp = _response([[_fc_part("mytool", None)]])
        assert _extract_fc_args(resp, "mytool") == {}

    def test_first_matching_call_wins_across_candidates(self):
        resp = _response([
            [_fc_part("mytool", {"n": 1})],
            [_fc_part("mytool", {"n": 2})],
        ])
        assert _extract_fc_args(resp, "mytool") == {"n": 1}

    def test_non_json_serializable_args_fall_back_to_dict(self):
        resp = _response([[_fc_part("mytool", {"floors": {"L2", "L3"}})]])
        args = _extract_fc_args(resp, "mytool")
        assert args == {"floors": {"L2", "L3"}}

    def test_json_roundtrip_normalizes_values(self):
        # Mapping-like args (as Gemini returns) round-trip to plain types
        resp = _response([[_fc_part("mytool", {"count": 3, "ok": True, "name": "x"})]])
        args = _extract_fc_args(resp, "mytool")
        assert args == {"count": 3, "ok": True, "name": "x"}
