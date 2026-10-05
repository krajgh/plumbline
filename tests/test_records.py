"""Record schemas, the JSON Schema subset validator, check-record and render."""
import json
import re

import pytest

import plumbline as pl
from samples import RECORD_TYPES, sample

SPEC_TYPES = {
    "change_class", "spec", "tests_record", "build_note",
    "verify_record", "review_record", "pass_record", "override_record",
    "findings_record", "defense_record", "gaps_record",
}


def errors_after(type_name, mutate):
    record = sample(type_name)
    mutate(record)
    return pl.check_record(type_name, record)


def assert_error(errors, path, text=""):
    """Some error starts with exactly this JSON path (then a colon) and holds `text`."""
    assert any(e.startswith(path + ":") and text in e for e in errors), f"no error for {path} ({text!r}) in {errors}"


# ------------------------------------------------------------- the schemas


def test_the_record_types_are_exactly_the_specs():
    assert set(pl.record_types()) == SPEC_TYPES
    assert set(RECORD_TYPES) == SPEC_TYPES


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_every_schema_is_itself_well_formed(name):
    schema = pl.load_schema(name)
    assert pl.check_schema(schema) == []
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema.get("description")


def _objects(schema, path="$"):
    """Every sub-schema that has `properties`."""
    if "properties" in schema:
        yield path, schema
        for key, sub in schema["properties"].items():
            yield from _objects(sub, f"{path}.{key}")
    if "items" in schema:
        yield from _objects(schema["items"], f"{path}[]")


# The fields a record may leave out: every other field of a fixed-shape object is required. A field joins this list for a reason that
# is written next to it: a record from an earlier version has to stay valid, because the push gate reads a run's records again.
OPTIONAL = {
    ("pass_record", "$"): {"open_findings", "gaps"},  # 0.5.0: a pass record from 0.4.2 has neither
    ("pass_record", "$.tokens"): {"orchestration"},  # 0.5.0: only a run that the orchestrator ran in legs has it
    ("defense_record", "$.defenses[]"): {"severity_claim"},  # 0.5.0: only a defender that concedes a finding worse than filed writes one
    ("review_record", "$.defenses[]"): {"severity_claim"},
    ("review_record", "$.findings[]"): {"severity_raised_from"},  # only on a finding that merge-review raised
    ("review_record", "$"): {"panel_needed", "canary"},  # 0.5.0: a review record from 0.4.2 has no panel_needed, and only a calibration round has a canary
    ("change_class", "$"): {"calibrate", "weights"},  # only the intake record of a calibration run has the first; 0.5.1: the second is there only where a file type counts for less than a full line
    ("review_record", "$.routes"): {"planner"},  # a record from 0.4.2 routes to two roles
}


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_every_field_of_a_fixed_shape_object_is_required_and_no_others_are_allowed(name):
    for path, obj in _objects(pl.load_schema(name)):
        assert obj["additionalProperties"] is False, path
        assert set(obj["required"]) == set(obj["properties"]) - OPTIONAL.get((name, path), set()), path


def test_every_optional_field_the_tests_name_is_in_its_schema_and_not_required():
    for (name, path), fields in OPTIONAL.items():
        obj = dict(_objects(pl.load_schema(name)))[path]
        assert fields <= set(obj["properties"]) and not fields & set(obj["required"]), (name, path)


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_every_property_says_what_it_is(name):
    for path, obj in _objects(pl.load_schema(name)):
        for key, sub in obj["properties"].items():
            assert {"type", "enum", "const"} & set(sub), f"{path}.{key} has no type, enum or const"


def test_the_severity_rubric_is_in_the_review_schema_description():
    severity = pl.load_schema("review_record")["properties"]["findings"]["items"]["properties"]["severity"]
    text = severity["description"]
    assert "BLOCKING: breaks a stated acceptance criterion" in text
    assert "MAJOR: wrong behaviour on a realistic path" in text
    assert "MINOR: an edge case" in text
    assert severity["enum"] == ["BLOCKING", "MAJOR", "MINOR"]


def test_the_per_agent_review_records_use_the_review_records_item_shapes():
    # each is a piece of a review_record; the shapes are written out three times
    # because the schema subset has no references, so this is what keeps them equal
    review = pl.load_schema("review_record")["properties"]
    merged = json.loads(json.dumps(review["findings"]["items"]))  # the merged record's findings carry one more field, whether their evidence was found, and an optional one
    merged["required"].remove("evidence_unverified")
    del merged["properties"]["evidence_unverified"]
    del merged["properties"]["severity_raised_from"]
    assert pl.load_schema("findings_record")["properties"]["findings"]["items"] == merged
    assert pl.load_schema("defense_record")["properties"]["defenses"]["items"] == review["defenses"]["items"]
    assert pl.load_schema("gaps_record")["properties"]["gaps"]["items"] == review["gaps"]["items"]
    assert pl.load_schema("findings_record")["properties"]["lens"]["enum"] == review["lenses"]["items"]["enum"]  # the canary's record has a lens a review runs, as a prosecutor's does


def test_a_findings_record_has_one_lens_and_a_defense_record_one_defender():
    assert pl.load_schema("findings_record")["required"] == ["lens", "findings", "diff_sha256"]
    assert pl.load_schema("defense_record")["required"] == ["defender", "defenses", "diff_sha256"]
    assert pl.load_schema("gaps_record")["required"] == ["gaps", "diff_sha256"]
    assert_error(errors_after("findings_record", lambda r: r.update(lens=["security"])), "$.lens", "is not one of")
    assert_error(errors_after("findings_record", lambda r: r.update(lens="vibes")), "$.lens", "is not one of")
    assert_error(errors_after("defense_record", lambda r: r.update(defender="")), "$.defender", "at least 1 characters")


@pytest.mark.parametrize("name", ["findings_record", "defense_record", "gaps_record"])
def test_a_part_of_a_review_carries_the_hash_of_the_change_it_saw(name):  # C-09
    record = sample(name)
    assert re.fullmatch(r"[0-9a-f]{64}", record["diff_sha256"])
    del record["diff_sha256"]
    assert pl.check_record(name, record) == ["$.diff_sha256: missing required key"]
    record["diff_sha256"] = "abc"
    assert any("does not match ^[0-9a-f]{64}$" in e for e in pl.check_record(name, record))


def test_a_finding_of_the_merged_review_says_whether_its_evidence_was_found_and_the_record_says_where_the_survivors_go():  # C-03, C-06
    record = sample("review_record")
    assert [f["evidence_unverified"] for f in record["findings"]] == [False, True]
    assert set(record["routes"]) == {"builder", "test-writer"}
    assert_error(errors_after("review_record", lambda r: r["findings"][0].pop("evidence_unverified")), "$.findings[0].evidence_unverified", "missing required key")
    assert_error(errors_after("review_record", lambda r: r["findings"][0].update(evidence_unverified="no")), "$.findings[0].evidence_unverified")
    assert_error(errors_after("review_record", lambda r: r["routes"].pop("test-writer")), "$.routes['test-writer']", "missing required key")
    assert_error(errors_after("review_record", lambda r: r["routes"].update(everyone=[])), "$.routes.everyone", "unexpected key")


def test_a_command_summary_cannot_carry_source_code():  # C-06
    for good in ("13 passed", "2 failed, 11 passed", "clean", "no test command is configured", "1 failed, 12 passed in 0.32s", "", "x" * 80):
        assert errors_after("verify_record", lambda r: r["commands"][0].update(summary=good)) == [], good
    for bad in ('assert main() == 2, "the expected value"', "x" * 81, "line one\nline two", "a = [1, 2]", "print('x')", "`code`", "{'k': 1}"):
        assert_error(errors_after("verify_record", lambda r: r["commands"][0].update(summary=bad)), "$.commands[0].summary", "does not match")


def test_an_unknown_record_type_is_an_error_listing_the_known_ones():
    with pytest.raises(pl.PlumblineError, match="unknown record type 'nope'.*change_class"):
        pl.load_schema("nope")
    with pytest.raises(pl.PlumblineError):
        pl.load_schema("../pipeline/default")  # never a path


# ------------------------------------------------------------ valid records


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_a_valid_sample_validates(name):
    assert pl.check_record(name, sample(name)) == []


def test_a_spec_at_size_l_carries_a_split_proposal():
    record = sample("spec")
    record["split_proposal"] = ["Add the retry helper", "Wire the helper into the client"]
    assert pl.check_record("spec", record) == []


def test_review_gap_and_finding_nulls_are_allowed_where_the_spec_says_or_null():
    record = sample("review_record")
    assert record["findings"][0]["outside_code"] is None
    assert record["gaps"][1]["ac"] is None
    assert pl.check_record("review_record", record) == []


def test_verify_checks_may_each_be_true_false_or_null():
    record = sample("verify_record")
    assert {v for v in record["checks"].values()} == {True, False, None}
    assert pl.check_record("verify_record", record) == []


# -------------------------------------------------- generated mutations


def _top_level_keys():
    return [(name, key) for name in sorted(SPEC_TYPES) for key in pl.load_schema(name)["required"]]


def _wrong_value(prop):
    allowed = prop["type"] if isinstance(prop.get("type"), list) else [prop["type"]] if "type" in prop else []
    for candidate in (123, "x", [], {}, True, None):
        name = pl._type_name(candidate)
        if name in allowed or (name == "integer" and "number" in allowed):
            continue
        return candidate
    raise AssertionError("no wrong value")


@pytest.mark.parametrize("name,key", _top_level_keys())
def test_a_missing_required_key_is_reported_with_its_path(name, key):
    record = sample(name)
    del record[key]
    assert_error(pl.check_record(name, record), f"$.{key}", "missing required key")


@pytest.mark.parametrize("name,key", _top_level_keys())
def test_a_wrong_type_is_reported_with_the_keys_path(name, key):
    record = sample(name)
    record[key] = _wrong_value(pl.load_schema(name)["properties"][key])
    assert_error(pl.check_record(name, record), f"$.{key}")


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_an_extra_top_level_key_is_reported(name):
    record = sample(name)
    record["bogus"] = 1
    assert_error(pl.check_record(name, record), "$.bogus", "unexpected key")


@pytest.mark.parametrize(
    "name,path,container",
    [
        ("change_class", "$.files[0].bogus", lambda r: r["files"][0]),
        ("spec", "$.acceptance_criteria[1].bogus", lambda r: r["acceptance_criteria"][1]),
        ("spec", "$.interfaces[0].bogus", lambda r: r["interfaces"][0]),
        ("tests_record", "$.stub_check.bogus", lambda r: r["stub_check"]),
        ("tests_record", "$.tests[0].bogus", lambda r: r["tests"][0]),
        ("verify_record", "$.commands[1].bogus", lambda r: r["commands"][1]),
        ("verify_record", "$.tests.bogus", lambda r: r["tests"]),
        ("verify_record", "$.checks.bogus", lambda r: r["checks"]),
        ("review_record", "$.findings[1].bogus", lambda r: r["findings"][1]),
        ("review_record", "$.defenses[0].bogus", lambda r: r["defenses"][0]),
        ("review_record", "$.gaps[0].bogus", lambda r: r["gaps"][0]),
        ("pass_record", "$.stages[1].bogus", lambda r: r["stages"][1]),
        ("pass_record", "$.tokens.bogus", lambda r: r["tokens"]),
        ("findings_record", "$.findings[0].bogus", lambda r: r["findings"][0]),
        ("defense_record", "$.defenses[1].bogus", lambda r: r["defenses"][1]),
        ("gaps_record", "$.gaps[0].bogus", lambda r: r["gaps"][0]),
    ],
)
def test_an_extra_key_in_a_nested_object_is_reported_with_its_path(name, path, container):
    assert_error(errors_after(name, lambda r: container(r).update(bogus=True)), path, "unexpected key")


@pytest.mark.parametrize(
    "name,path,delete",
    [
        ("change_class", "$.files[2].symlink", lambda r: r["files"][2].pop("symlink")),
        ("spec", "$.acceptance_criteria[0].verification", lambda r: r["acceptance_criteria"][0].pop("verification")),
        ("tests_record", "$.stub_check.detail", lambda r: r["stub_check"].pop("detail")),
        ("verify_record", "$.checks.graft_fresh", lambda r: r["checks"].pop("graft_fresh")),
        ("review_record", "$.findings[0].failure_scenario", lambda r: r["findings"][0].pop("failure_scenario")),
        ("review_record", "$.findings[1].outside_code", lambda r: r["findings"][1].pop("outside_code")),
        ("review_record", "$.gaps[1].ac", lambda r: r["gaps"][1].pop("ac")),
        ("pass_record", "$.tokens.by_model", lambda r: r["tokens"].pop("by_model")),
        ("pass_record", "$.stages[0].gate", lambda r: r["stages"][0].pop("gate")),
        ("findings_record", "$.findings[1].outside_code", lambda r: r["findings"][1].pop("outside_code")),
        ("defense_record", "$.defenses[0].quote", lambda r: r["defenses"][0].pop("quote")),
        ("gaps_record", "$.gaps[1].ac", lambda r: r["gaps"][1].pop("ac")),
    ],
)
def test_a_missing_nested_key_is_reported_with_its_path(name, path, delete):
    assert_error(errors_after(name, delete), path, "missing required key")


@pytest.mark.parametrize(
    "name,path,mutate",
    [
        ("change_class", "$.files[0].added", lambda r: r["files"][0].update(added="80")),
        ("change_class", "$.files[0].generated", lambda r: r["files"][0].update(generated="no")),
        ("change_class", "$.lines", lambda r: r.update(lines=True)),
        ("change_class", "$.lines", lambda r: r.update(lines=-1)),
        ("change_class", "$.files[1].removed", lambda r: r["files"][1].update(removed=-3)),
        ("change_class", "$.head", lambda r: r.update(head="not-a-sha")),
        ("spec", "$.non_goals[0]", lambda r: r["non_goals"].__setitem__(0, 5)),
        ("spec", "$.split_proposal", lambda r: r.update(split_proposal="one change")),
        ("spec", "$.split_proposal", lambda r: r.update(split_proposal=[])),
        ("tests_record", "$.stub_check.ran", lambda r: r["stub_check"].update(ran=1)),
        ("verify_record", "$.commands[0].exit_code", lambda r: r["commands"][0].update(exit_code="1")),
        ("verify_record", "$.tests.failed", lambda r: r["tests"].update(failed=1.5)),
        ("verify_record", "$.checks.lint", lambda r: r["checks"].update(lint="yes")),
        ("verify_record", "$.green", lambda r: r.update(green="false")),
        ("review_record", "$.round", lambda r: r.update(round=0)),
        ("review_record", "$.findings[0].line", lambda r: r["findings"][0].update(line=0)),
        ("review_record", "$.findings[0].line", lambda r: r["findings"][0].update(line="14")),
        ("review_record", "$.lenses", lambda r: r.update(lenses=[])),
        ("review_record", "$.blockers_surviving", lambda r: r.update(blockers_surviving=-1)),
        ("pass_record", "$.stages[1].passed", lambda r: r["stages"][1].update(passed="yes")),
        ("pass_record", "$.stages[0].rounds", lambda r: r["stages"][0].update(rounds=-1)),
        ("pass_record", "$.tokens.by_model", lambda r: r["tokens"].update(by_model=[])),
        ("override_record", "$.stages_skipped[1]", lambda r: r["stages_skipped"].__setitem__(1, 7)),
        ("findings_record", "$.findings[0].line", lambda r: r["findings"][0].update(line=0)),
        ("findings_record", "$.findings", lambda r: r.update(findings={})),
        ("defense_record", "$.defenses[0].reason", lambda r: r["defenses"][0].update(reason="")),
        ("gaps_record", "$.gaps", lambda r: r.update(gaps="none")),
    ],
)
def test_wrong_types_and_ranges_in_nested_fields_name_their_path(name, path, mutate):
    assert_error(errors_after(name, mutate), path)


# ----------------------------------------------------------- enums


@pytest.mark.parametrize(
    "name,path,mutate",
    [
        ("change_class", "$.size", lambda r: r.update(size="XL")),
        ("change_class", "$.size", lambda r: r.update(size="s")),
        ("review_record", "$.findings[0].severity", lambda r: r["findings"][0].update(severity="SEVERE")),
        ("review_record", "$.findings[0].severity", lambda r: r["findings"][0].update(severity="blocking")),
        ("review_record", "$.findings[1].lens", lambda r: r["findings"][1].update(lens="vibes")),
        ("review_record", "$.lenses[1]", lambda r: r["lenses"].__setitem__(1, "vibes")),
        ("review_record", "$.defenses[0].verdict", lambda r: r["defenses"][0].update(verdict="maybe")),
        ("review_record", "$.gaps[0].kind", lambda r: r["gaps"][0].update(kind="typo")),
        ("pass_record", "$.verdict", lambda r: r.update(verdict="maybe")),
        ("findings_record", "$.findings[1].severity", lambda r: r["findings"][1].update(severity="SEVERE")),
        ("findings_record", "$.findings[0].lens", lambda r: r["findings"][0].update(lens="vibes")),
        ("defense_record", "$.defenses[0].verdict", lambda r: r["defenses"][0].update(verdict="maybe")),
        ("gaps_record", "$.gaps[0].kind", lambda r: r["gaps"][0].update(kind="typo")),
    ],
)
def test_a_bad_enum_value_is_reported_with_its_path(name, path, mutate):
    assert_error(errors_after(name, mutate), path, "is not one of")


def test_the_enum_message_lists_the_allowed_values():
    [error] = errors_after("change_class", lambda r: r.update(size="XL"))
    assert error == "$.size: 'XL' is not one of 'S', 'M', 'L'"


# --------------------------------------------------------- patterns


@pytest.mark.parametrize("bad", ["AC1", "ac-1", "AC-", "AC-1x", " AC-1", "AC-1 ", "AC-1\n", "AC--1", "", "AC-\u0661"])
def test_a_bad_ac_id_pattern_is_reported_wherever_an_ac_is_named(bad):
    assert_error(errors_after("spec", lambda r: r["acceptance_criteria"][0].update(id=bad)), "$.acceptance_criteria[0].id", "does not match")
    assert_error(errors_after("spec", lambda r: r["test_plan"][0].update(ac=bad)), "$.test_plan[0].ac")
    assert_error(errors_after("tests_record", lambda r: r["tests"][0]["ac_ids"].append(bad)), "$.tests[0].ac_ids[1]")
    assert_error(errors_after("build_note", lambda r: r["acs_addressed"].append(bad)), "$.acs_addressed[2]")
    assert_error(errors_after("verify_record", lambda r: r["failing_acs"][0].update(ac=bad)), "$.failing_acs[0].ac")
    assert_error(errors_after("review_record", lambda r: r["gaps"][0].update(ac=bad)), "$.gaps[0].ac")
    assert_error(errors_after("gaps_record", lambda r: r["gaps"][0].update(ac=bad)), "$.gaps[0].ac")


@pytest.mark.parametrize("good", ["AC-1", "AC-0", "AC-42", "AC-1000"])
def test_good_ac_ids_pass(good):
    assert errors_after("spec", lambda r: r["acceptance_criteria"][0].update(id=good)) == []


def test_error_type_is_one_token_never_assertion_text():
    for bad in ("assert 3 == 4", "AssertionError: expected 3", "assertion failed", "x" * 81, ""):
        assert_error(errors_after("verify_record", lambda r: r["failing_acs"][0].update(error_type=bad)), "$.failing_acs[0].error_type")
    for good in ("AssertionError", "TypeError", "Timeout", "exit_1", "builtins.ValueError", "x" * 80):
        assert errors_after("verify_record", lambda r: r["failing_acs"][0].update(error_type=good)) == []


def test_an_override_reason_needs_twenty_characters():
    assert_error(errors_after("override_record", lambda r: r.update(reason="skip it")), "$.reason", "at least 20 characters")
    assert_error(errors_after("override_record", lambda r: r.update(reason="x" * 19)), "$.reason")
    assert errors_after("override_record", lambda r: r.update(reason="x" * 20)) == []


def test_empty_required_text_is_rejected():
    assert_error(errors_after("spec", lambda r: r.update(goal="")), "$.goal", "at least 1 characters")
    assert_error(errors_after("build_note", lambda r: r.update(summary="")), "$.summary")


def test_every_error_is_reported_not_just_the_first():
    def mutate(r):
        r["size"] = "XL"
        r["lines"] = "many"
        r["files"][0]["path"] = 5
        del r["row"]
        r["extra"] = True

    assert len(errors_after("change_class", mutate)) == 5


def test_the_root_of_a_record_must_be_an_object():
    assert pl.check_record("spec", []) == ["$: expected object, got array"]
    assert pl.check_record("spec", "text") == ["$: expected object, got string"]
    assert pl.check_record("spec", None) == ["$: expected object, got null"]


# ------------------------------------------- the schema subset itself


def test_type_may_be_a_list_and_bool_is_not_an_integer():
    schema = {"type": ["integer", "null"]}
    assert pl.validate(None, schema) == []
    assert pl.validate(3, schema) == []
    assert pl.validate(True, schema) == ["$: expected integer or null, got boolean"]
    assert pl.validate(3.5, schema) == ["$: expected integer or null, got number"]
    assert pl.validate(3, {"type": "number"}) == []
    assert pl.validate(3.5, {"type": "number"}) == []
    assert pl.validate(True, {"type": "number"}) == ["$: expected number, got boolean"]


def test_enum_and_const_do_not_confuse_true_with_one():
    assert pl.validate(True, {"enum": [1, 2]}) != []
    assert pl.validate(1, {"enum": [True]}) != []
    assert pl.validate(True, {"const": 1}) != []
    assert pl.validate(1, {"const": 1}) == []
    assert pl.validate("a", {"const": "a"}) == []


def test_length_and_range_and_count_keywords():
    assert pl.validate("ab", {"minLength": 3}) == ["$: must be at least 3 characters long (got 2)"]
    assert pl.validate([1], {"minItems": 2}) == ["$: needs at least 2 items (got 1)"]
    assert pl.validate([1, 2, 3], {"maxItems": 2}) == ["$: allows at most 2 items (got 3)"]
    assert pl.validate(5, {"minimum": 6}) == ["$: 5 is below the minimum 6"]
    assert pl.validate(7, {"maximum": 6}) == ["$: 7 is above the maximum 6"]
    assert pl.validate(6, {"minimum": 6, "maximum": 6}) == []


def test_pattern_is_a_search_so_records_anchor_their_patterns():
    assert pl.validate("xxABxx", {"pattern": "AB"}) == []
    assert pl.validate("xxABxx", {"pattern": "^AB$"}) != []


def test_a_trailing_dollar_means_the_end_of_the_string_not_before_a_final_newline():
    assert pl.validate("AB", {"pattern": "^AB$"}) == []
    assert pl.validate("AB\n", {"pattern": "^AB$"}) != []
    assert pl.validate("a$", {"pattern": "a\\$"}) == []


def test_paths_use_dots_and_indexes_and_bracket_odd_keys():
    schema = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "object", "properties": {"b": {"type": "string"}}}}}}
    assert pl.validate({"a": [{"b": "x"}, {"b": 2}]}, schema) == ["$.a[1].b: expected string, got integer"]
    loose = {"type": "object", "additionalProperties": False}
    assert pl.validate({"odd key": 1}, loose) == ["$['odd key']: unexpected key"]


def test_additional_properties_are_allowed_unless_false():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    assert pl.validate({"a": 1, "b": 2}, schema) == []
    assert pl.validate({"a": 1, "b": 2}, {**schema, "additionalProperties": True}) == []
    assert pl.validate({"a": 1, "b": 2}, {**schema, "additionalProperties": False}) == ["$.b: unexpected key"]


def test_a_type_error_stops_further_checks_on_that_value():
    assert pl.validate(5, {"type": "string", "minLength": 3, "pattern": "x"}) == ["$: expected string, got integer"]


def test_description_is_ignored():
    assert pl.validate(1, {"type": "integer", "description": "anything"}) == []


@pytest.mark.parametrize(
    "schema,fragment",
    [
        ({"format": "date"}, "unsupported keyword 'format'"),
        ({"maxLength": 3}, "unsupported keyword 'maxLength'"),
        ({"$ref": "#/x"}, "unsupported keyword '$ref'"),
        ({"oneOf": []}, "unsupported keyword 'oneOf'"),
        ({"additionalProperties": {"type": "string"}}, "additionalProperties: must be a boolean"),
        ({"type": "float"}, "$.type"),
        ({"type": []}, "$.type"),
        ({"required": "a"}, "$.required"),
        ({"required": ["a"], "properties": {"b": {"type": "string"}}}, "'a' is not in properties"),
        ({"enum": []}, "$.enum"),
        ({"pattern": "("}, "$.pattern"),
        ({"minItems": "1"}, "$.minItems"),
        ({"minimum": "1"}, "$.minimum"),
        ({"properties": {"a": {"bogus": 1}}}, "$.properties.a: unsupported keyword 'bogus'"),
        ({"items": []}, "$.items: a schema must be an object"),
        ({"description": 5}, "$.description"),
    ],
)
def test_check_schema_rejects_what_the_validator_does_not_support(schema, fragment):
    assert any(fragment in problem for problem in pl.check_schema(schema)), pl.check_schema(schema)


def test_a_well_formed_schema_has_no_problems():
    schema = {"type": "object", "required": ["a"], "additionalProperties": False, "properties": {"a": {"type": ["string", "null"], "pattern": "^x", "minLength": 1}}}
    assert pl.check_schema(schema) == []


# --------------------------------------------------------- check-record


def write_record(tmp_path, data, name="record.json"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_check_record_accepts_a_valid_record(run_cli, tmp_path, name):
    result = run_cli("check-record", name, write_record(tmp_path, sample(name)), cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert f"a valid {name}" in result.stdout


def test_check_record_lists_every_error_with_its_path_and_exits_1(run_cli, tmp_path):
    record = sample("spec")
    record["acceptance_criteria"][0]["id"] = "AC1"
    del record["goal"]
    result = run_cli("check-record", "spec", write_record(tmp_path, record), cwd=tmp_path)
    assert result.returncode == 1
    assert "$.acceptance_criteria[0].id: 'AC1' does not match ^AC-[0-9]+$" in result.stdout
    assert "$.goal: missing required key" in result.stdout
    assert "not a valid spec (2 errors)" in result.stdout


def test_check_record_with_invalid_json_exits_1(run_cli, tmp_path):
    result = run_cli("check-record", "spec", write_record(tmp_path, "{not json"), cwd=tmp_path)
    assert result.returncode == 1
    assert "not valid JSON" in result.stdout


def test_check_record_with_an_unknown_type_exits_2(run_cli, tmp_path):
    result = run_cli("check-record", "nope", write_record(tmp_path, {}), cwd=tmp_path)
    assert result.returncode == 2
    assert "unknown record type 'nope'" in result.stderr


def test_check_record_with_a_missing_file_exits_2(run_cli, tmp_path):
    result = run_cli("check-record", "spec", tmp_path / "missing.json", cwd=tmp_path)
    assert result.returncode == 2
    assert "no such file" in result.stderr


# ------------------------------------------------------------------ render


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_render_produces_markdown_for_each_sample(name):
    text = pl.render_record(name, sample(name))
    assert text.startswith("# ")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert len(text.splitlines()) > 5


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_render_infers_the_type_from_the_keys(name):
    assert pl.infer_record_type(sample(name)) == name


@pytest.mark.parametrize("name", sorted(SPEC_TYPES))
def test_render_through_the_cli_with_and_without_type(run_cli, tmp_path, name):
    path = write_record(tmp_path, sample(name))
    inferred = run_cli("render", path, cwd=tmp_path)
    explicit = run_cli("render", path, "--type", name, cwd=tmp_path)
    assert inferred.returncode == 0, inferred.stderr
    assert inferred.stdout == explicit.stdout == pl.render_record(name, sample(name))


def test_render_carries_the_records_content():
    assert "Add a retry to the fetch helper." in pl.render_record("spec", sample("spec"))
    assert "**AC-1** A failed fetch is retried up to three times." in pl.render_record("spec", sample("spec"))
    assert "None: the change is size M or smaller." in pl.render_record("spec", sample("spec"))
    review = pl.render_record("review_record", sample("review_record"))
    assert "[BLOCKING]" in review and "`src/app.py:14`" in review and "Blockers surviving: 1" in review
    assert "F-1, d1: conceded" in review
    verify = pl.render_record("verify_record", sample("verify_record"))
    assert verify.startswith("# Verify: not green") and "- graft_fresh: n/a" in verify and "**AC-1**: AssertionError" in verify
    change = pl.render_record("change_class", sample("change_class"))
    assert "| uv.lock | code | 300 | 0 | generated |" in change and "| docs/link.md | docs | 1 | 0 | symlink |" in change
    assert "Row `code.M`: size M, 101 weighted lines (code 100, docs 1). Types: code, docs." in change
    passed = pl.render_record("pass_record", sample("pass_record"))
    assert "| sonnet | 1200 | 3400 | 56000 |" in passed and "# Pass record: pass" in passed
    lower = sample("pass_record")
    lower["tokens"]["by_model"]["sonnet"]["output_lower_bound"] = 3
    assert "| sonnet | at least 1200 | 3400 | 56000 |" in pl.render_record("pass_record", lower)  # a count with messages missing their final entry is shown as at least
    assert "The pipeline cannot run offline" in pl.render_record("override_record", sample("override_record"))
    assert "`src/app.py`" in pl.render_record("build_note", sample("build_note"))
    assert "covers AC-1" in pl.render_record("tests_record", sample("tests_record"))
    findings = pl.render_record("findings_record", sample("findings_record"))
    assert findings.startswith("# Findings through the correctness lens") and "[BLOCKING]" in findings and "`src/app.py:14`" in findings
    defenses = pl.render_record("defense_record", sample("defense_record"))
    assert defenses.startswith("# Defenses by defender-1") and "correctness-1, defender-1: conceded" in defenses
    gaps = pl.render_record("gaps_record", sample("gaps_record"))
    assert gaps.startswith("# Gaps") and "**G-1** (uncovered_ac, AC-2) No test covers AC-2." in gaps


def test_render_shows_the_intent_and_the_hash_of_the_change():  # C-26
    assert "- Intent: `feature`" in pl.render_record("change_class", sample("change_class"))
    for name in ("verify_record", "review_record", "findings_record", "defense_record", "gaps_record"):
        assert f"Change (diff_sha256): `{sample(name)['diff_sha256']}`" in pl.render_record(name, sample(name)), name


def test_render_marks_unverified_evidence_and_lists_the_routes():
    review = pl.render_record("review_record", sample("review_record"))
    assert "The evidence was found in neither the change nor its file (unverified)." in review
    assert "## Survivors for the builder" in review and "## Survivors for the test-writer" in review


def test_render_escapes_pipes_in_table_cells():
    record = sample("change_class")
    record["files"][0]["path"] = "a|b.py"
    assert "| a\\|b.py |" in pl.render_record("change_class", record)


def test_render_never_fails_on_a_malformed_record():
    assert pl.render_record("spec", {"goal": 5}).startswith("# Spec")
    fallback = pl.render_record("spec", ["not", "a", "record"])
    assert fallback.startswith("# spec") and '"not"' in fallback
    broken = sample("review_record")
    broken["findings"] = "nope"
    broken["defenses"] = [None, 3]
    assert pl.render_record("review_record", broken).startswith("# Review of diff, round 1")


def test_render_cannot_guess_from_unmarked_or_ambiguous_keys(run_cli, tmp_path):
    result = run_cli("render", write_record(tmp_path, {"hello": 1}), cwd=tmp_path)
    assert result.returncode == 2
    assert "pass --type" in result.stderr
    both = {**sample("spec"), **sample("build_note")}
    assert run_cli("render", write_record(tmp_path, both), cwd=tmp_path).returncode == 2


def test_render_with_an_unknown_type_or_bad_json_exits_2(run_cli, tmp_path):
    assert run_cli("render", write_record(tmp_path, {}), "--type", "nope", cwd=tmp_path).returncode == 2
    assert run_cli("render", write_record(tmp_path, "{oops"), cwd=tmp_path).returncode == 2
