#!/usr/bin/env python3
"""plumbline: the command line of the plumbline Claude Code plugin.

Standard library only (Python 3.11 or newer).

    validate-pipeline [FILE] [--project PATH]
    classify [--project PATH] [--base REF] [--out FILE]
    plan [--project PATH] [--base REF] [--run-id ID]
    check-record TYPE FILE
    render FILE [--type TYPE]
    init [--project PATH] [--graft]

Exit status: 0 on success; 1 when what was checked is invalid (or init
refuses to overwrite); 2 when the command could not run (bad arguments, not a
git repository, an unreadable file).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # tomllib arrived in 3.11
    raise SystemExit("plumbline needs Python 3.11 or newer (this is %d.%d)" % sys.version_info[:2])

import argparse
import copy
import datetime as dt
import functools
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = PLUGIN_ROOT / "schemas"
PIPELINE_DIR = PLUGIN_ROOT / "pipeline"

CONFIG_FILE = "plumbline.toml"
DEFAULT_PIPELINE = "default"
RUNS_DIR = ".plumbline/runs"
IGNORE_ENTRY = ".plumbline/"
IGNORE_EQUIVALENTS = {".plumbline", ".plumbline/", "/.plumbline", "/.plumbline/"}

SIZE_LABELS = ("S", "M", "L")
KNOWN_ROLES = ("main", "planner", "test-writer", "builder", "verifier")
KNOWN_KINDS = ("agent", "review")
KNOWN_LENSES = ("correctness", "tests", "security", "data", "boundaries", "docs")
KNOWN_GATES = (
    "spec_complete",
    "acs_covered",
    "tests_fail_on_stub",
    "verify_green",
    "no_surviving_blockers",
    "all_gates_passed",
)
REVIEW_ONLY_KEYS = ("target", "lenses", "defenders", "survive_if_unrefuted_by", "detective")
ID_PATTERN = r"^[a-z][a-z0-9_-]*$"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Paths every "matches everything" type must match (see validate_pipeline).
PROBE_PATHS = ("a", ".a", "a.b", "a/b", "a/b/c.d", ".github/workflows/x.yml", "dir with space/f g")


class PlumblineError(Exception):
    """Something to tell the user, ending the command with exit status 2."""


# ------------------------------------------------------------------ globs
#
# `**` as a whole path segment matches zero or more directories (a trailing
# `**` matches everything below), `*` and `?` stay inside one segment, and
# every pattern is anchored at the repository root.


@functools.lru_cache(maxsize=None)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a plumbline glob to a regular expression for `fullmatch`."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            starts_segment = i == 0 or pattern[i - 1] == "/"
            ends_segment = j == n or pattern[j] == "/"
            if j - i == 2 and starts_segment and ends_segment:
                if j == n:
                    out.append(".+")  # trailing `**`: everything below
                    i = j
                else:
                    out.append("(?:.+/)?")  # `**/`: zero or more directories
                    i = j + 1
            else:
                out.append("[^/]*")
                i = j
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def glob_match(pattern: str, path: str) -> bool:
    return glob_to_regex(pattern).fullmatch(path) is not None


def _pattern_problem(pattern: str) -> str | None:
    if not pattern:
        return "is empty"
    if pattern.startswith("/"):
        return "must not start with '/' (patterns are relative to the repository root)"
    if pattern.endswith("/"):
        return "must not end with '/' (write 'dir/**' for everything under a directory)"
    if "//" in pattern:
        return "must not contain '//'"
    if ".." in pattern.split("/"):
        return "must not contain '..'"
    return None


# ------------------------------------------------- JSON Schema subset
#
# Supported keywords: type (a name or a list of names), required, properties,
# additionalProperties (a boolean), enum, const, items, minItems, maxItems,
# minLength, pattern, minimum, maximum, and description (ignored). A schema
# using anything else is rejected rather than half-enforced.

SCHEMA_KEYWORDS = frozenset(
    {
        "type", "required", "properties", "additionalProperties", "enum", "const",
        "items", "minItems", "maxItems", "minLength", "pattern", "minimum", "maximum",
        "description",
    }
)
JSON_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


@functools.lru_cache(maxsize=None)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """A schema pattern as a Python regex. A trailing `$` means the end of the
    string, as in JSON Schema; in Python it would also match before a final newline."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_type(value, name: str) -> bool:
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return _type_name(value) == name


def _same(a, b) -> bool:
    """JSON equality: unlike Python's, True is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f"{path}[{key!r}]"


def check_schema(schema, path: str = "$") -> list[str]:
    """Problems with a schema itself; an empty list means it is well-formed."""
    if not isinstance(schema, dict):
        return [f"{path}: a schema must be an object"]
    problems = [f"{path}: unsupported keyword {key!r}" for key in schema if key not in SCHEMA_KEYWORDS]

    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not names or not all(isinstance(n, str) and n in JSON_TYPES for n in names):
            problems.append(f"{path}.type: must be one of {', '.join(JSON_TYPES)}, or a list of them")
    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(k, str) for k in req):
            problems.append(f"{path}.required: must be a list of strings")
        elif isinstance(schema.get("properties"), dict):
            for key in req:
                if key not in schema["properties"]:
                    problems.append(f"{path}.required: {key!r} is not in properties")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            problems.append(f"{path}.properties: must be an object")
        else:
            for key, sub in schema["properties"].items():
                problems += check_schema(sub, f"{path}.properties.{key}")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        problems.append(f"{path}.additionalProperties: must be a boolean")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        problems.append(f"{path}.enum: must be a non-empty list")
    if "items" in schema:
        problems += check_schema(schema["items"], f"{path}.items")
    for key in ("minItems", "maxItems", "minLength"):
        if key in schema and not _is_type(schema[key], "integer"):
            problems.append(f"{path}.{key}: must be an integer")
    for key in ("minimum", "maximum"):
        if key in schema and not _is_type(schema[key], "number"):
            problems.append(f"{path}.{key}: must be a number")
    if "pattern" in schema:
        try:
            _compile_pattern(schema["pattern"])
        except (re.error, TypeError):
            problems.append(f"{path}.pattern: not a valid regular expression")
    if "description" in schema and not isinstance(schema["description"], str):
        problems.append(f"{path}.description: must be a string")
    return problems


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Validate `value` against `schema`; every error names its JSON path."""
    errors: list[str] = []
    _validate(value, schema, path, errors)
    return errors


def _validate(value, schema: dict, path: str, errors: list[str]) -> None:
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, name) for name in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {_type_name(value)}")
            return
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        errors.append(f"{path}: {value!r} is not one of {', '.join(repr(o) for o in schema['enum'])}")
        return
    if "const" in schema and not _same(value, schema["const"]):
        errors.append(f"{path}: must be {schema['const']!r}, got {value!r}")
        return

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: must be at least {schema['minLength']} characters long (got {len(value)})")
        if "pattern" in schema and not _compile_pattern(schema["pattern"]).search(value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above the maximum {schema['maximum']}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items (got {len(value)})")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items (got {len(value)})")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{index}]", errors)
    elif isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{_child(path, key)}: missing required key")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                _validate(value[key], sub, _child(path, key), errors)
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{_child(path, key)}: unexpected key")


# ---------------------------------------------------------------- records


@functools.lru_cache(maxsize=None)
def record_types() -> tuple[str, ...]:
    """The record types: one schemas/<type>.json each."""
    return tuple(sorted(p.stem for p in SCHEMA_DIR.glob("*.json")))


@functools.lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    if name not in record_types():
        raise PlumblineError(f"unknown record type {name!r}; the types are {', '.join(record_types())}")
    path = SCHEMA_DIR / f"{name}.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlumblineError(f"cannot read schemas/{name}.json: {exc}") from None
    problems = check_schema(schema)
    if problems:
        raise PlumblineError(f"schemas/{name}.json is not a valid schema: {problems[0]}")
    return schema


def check_record(type_name: str, data) -> list[str]:
    """Errors (each with its JSON path) of `data` as a record of `type_name`."""
    return validate(data, load_schema(type_name))


# ---------------------------------------------------------------- pipeline

_ID = {"type": "string", "pattern": ID_PATTERN}
_TEXT = {"type": "string", "minLength": 1}
_TEXTS = {"type": "array", "items": _TEXT}

TYPE_SHAPE = {
    "type": "object",
    "required": ["id", "paths"],
    "additionalProperties": False,
    "properties": {
        "id": _ID,
        "paths": {"type": "array", "minItems": 1, "items": _TEXT},
    },
}

STAGE_SHAPE = {
    "type": "object",
    "required": ["id", "record"],
    "additionalProperties": False,
    "properties": {
        "id": _ID,
        "kind": {"type": "string"},
        "role": {"type": "string"},
        "target": {"type": "string"},
        "reads": _TEXTS,
        "record": _TEXT,
        "gate": {"type": "string"},
        "lenses": _TEXTS,
        "defenders": {"type": "integer", "minimum": 1},
        "survive_if_unrefuted_by": {"type": "integer", "minimum": 1},
        "detective": {"type": "boolean"},
        "on_fail": {"type": "string"},
        "max_rounds": {"type": "integer", "minimum": 1},
    },
}

PIPELINE_SHAPE = {
    "type": "object",
    "required": ["schema", "name", "inputs", "generated", "precedence", "sizes", "type", "stage", "matrix"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": 1},
        "name": _TEXT,
        "inputs": _TEXTS,
        "generated": _TEXTS,
        "precedence": {"type": "array", "minItems": 1, "items": _TEXT},
        "sizes": {
            "type": "object",
            "required": ["S", "M"],
            "additionalProperties": False,
            "properties": {
                "S": {"type": "integer", "minimum": 1},
                "M": {"type": "integer", "minimum": 1},
            },
        },
        "type": {"type": "array", "minItems": 1, "items": TYPE_SHAPE},
        "stage": {"type": "array", "minItems": 1, "items": STAGE_SHAPE},
        "matrix": {"type": "object"},
    },
}

CONFIG_SHAPE = {
    "type": "object",
    "required": ["schema"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": 1},
        "pipeline": _TEXT,
        "graft": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean"}},
        },
        "precedence": {"type": "array", "minItems": 1, "items": _TEXT},
        "type": {"type": "array", "items": TYPE_SHAPE},
        "matrix": {"type": "object"},
    },
}


def load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise PlumblineError(f"{path}: file not found") from None
    except tomllib.TOMLDecodeError as exc:
        raise PlumblineError(f"{path.name}: invalid TOML: {exc}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PlumblineError(f"{path}: cannot read: {exc}") from None


def _read_names(stage: dict) -> list[tuple[str, bool]]:
    """A stage's reads as (name, optional) pairs; a trailing `?` marks optional."""
    pairs = []
    for raw in stage.get("reads", []):
        optional = raw.endswith("?")
        pairs.append((raw[:-1] if optional else raw, optional))
    return pairs


def _split_row(name: str, row, errors: list[str]) -> list[tuple[str, dict]]:
    """A matrix row as (label, flat row) pairs: one if flat, S, M and L if split."""
    if not isinstance(row, dict):
        errors.append(f"row '{name}': must be a table")
        return []
    if "stages" in row:
        if any(key in row for key in SIZE_LABELS):
            errors.append(f"row '{name}': has both 'stages' and size keys; a row is flat or split by size, not both")
        return [(name, row)]
    if not any(key in row for key in SIZE_LABELS):
        errors.append(f"row '{name}': needs 'stages' (a flat row) or the size keys S, M and L")
        return []
    for key in row:
        if key not in SIZE_LABELS:
            errors.append(f"row '{name}': unexpected key {key!r} (a row split by size has only S, M and L)")
    flat = []
    for size in SIZE_LABELS:
        sub = row.get(size)
        if sub is None:
            errors.append(f"row '{name}': missing size {size} (a row split by size needs S, M and L)")
        elif not isinstance(sub, dict) or "stages" not in sub:
            errors.append(f"row '{name}.{size}': needs 'stages'")
        else:
            flat.append((f"{name}.{size}", sub))
    return flat


def validate_pipeline(pl: dict) -> tuple[list[str], list[str]]:
    """Check a pipeline definition; returns (errors, notes)."""
    errors = validate(pl, PIPELINE_SHAPE)
    notes: list[str] = []
    if errors:
        return errors, notes  # the shape must be right before anything else is checked

    inputs = pl["inputs"]
    stages = pl["stage"]
    types = pl["type"]
    records = record_types()

    # stages: ids, kinds, roles, records, gates
    index: dict[str, int] = {}
    for position, stage in enumerate(stages):
        if stage["id"] in index:
            errors.append(f"stage id '{stage['id']}' is defined more than once")
        else:
            index[stage["id"]] = position
    for name in inputs:
        if name in index:
            errors.append(f"input '{name}' has the same name as a stage")
    if len(set(inputs)) != len(inputs):
        errors.append("inputs names the same input twice")
    input_names = set(inputs)

    bad_reads: set[tuple[str, str]] = set()
    for position, stage in enumerate(stages):
        sid = stage["id"]
        where = f"stage '{sid}'"
        kind = stage.get("kind", "agent")
        if kind not in KNOWN_KINDS:
            errors.append(f"{where}: unknown kind {kind!r} (known: {', '.join(KNOWN_KINDS)})")
        if stage["record"] not in records:
            errors.append(f"{where}: unknown record {stage['record']!r} (there is no schemas/{stage['record']}.json)")
        if "gate" in stage and stage["gate"] not in KNOWN_GATES:
            errors.append(f"{where}: unknown gate {stage['gate']!r} (known: {', '.join(KNOWN_GATES)})")

        if kind == "review":
            if "role" in stage:
                errors.append(f"{where}: a review stage has no role")
            target = stage.get("target")
            if target is None:
                errors.append(f"{where}: a review stage needs a target")
            elif target not in input_names and not (target in index and index[target] < position):
                errors.append(f"{where}: target {target!r} must be an input or an earlier stage")
            lenses = stage.get("lenses")
            if not lenses:
                errors.append(f"{where}: a review stage needs a non-empty lenses list")
            else:
                for lens in lenses:
                    if lens not in KNOWN_LENSES:
                        errors.append(f"{where}: unknown lens {lens!r} (known: {', '.join(KNOWN_LENSES)})")
            if "survive_if_unrefuted_by" in stage:
                if "defenders" not in stage:
                    errors.append(f"{where}: survive_if_unrefuted_by needs defenders")
                elif stage["survive_if_unrefuted_by"] > stage["defenders"]:
                    errors.append(f"{where}: survive_if_unrefuted_by cannot exceed defenders")
        elif kind == "agent":
            if "role" not in stage:
                errors.append(f"{where}: an agent stage needs a role")
            elif stage["role"] not in KNOWN_ROLES:
                errors.append(f"{where}: unknown role {stage['role']!r} (known: {', '.join(KNOWN_ROLES)})")
            for key in REVIEW_ONLY_KEYS:
                if key in stage:
                    errors.append(f"{where}: '{key}' only applies to review stages")

        for name, _optional in _read_names(stage):
            if name in input_names:
                continue
            if name in index and index[name] < position:
                continue
            if name in index:
                errors.append(f"{where}: reads '{name}', which is not defined before it (a stage reads only inputs and earlier stages)")
            else:
                errors.append(f"{where}: reads unknown name '{name}' (neither an input nor a stage)")
            bad_reads.add((sid, name))

        if "on_fail" in stage:
            target = stage["on_fail"]
            if target != "main":
                if target not in index:
                    errors.append(f"{where}: on_fail names unknown stage {target!r}")
                elif index[target] >= position:
                    errors.append(f"{where}: on_fail {target!r} must be an earlier stage (or 'main'), not a later one")
                elif stages[index[target]].get("kind", "agent") != "agent":
                    errors.append(f"{where}: on_fail {target!r} is a review stage; it must name an agent stage or 'main'")
            if "max_rounds" not in stage:
                errors.append(f"{where}: on_fail needs max_rounds of at least 1")

    # types: unique ids, precedence, patterns, a last type that matches everything
    type_ids = [t["id"] for t in types]
    for tid in dict.fromkeys(type_ids):
        if type_ids.count(tid) > 1:
            errors.append(f"type id '{tid}' is defined more than once")
    precedence = pl["precedence"]
    for tid in dict.fromkeys(type_ids):
        if tid not in precedence:
            errors.append(f"precedence is missing type '{tid}' (it must name every type exactly once)")
    for name in dict.fromkeys(precedence):
        if name not in type_ids:
            errors.append(f"precedence names unknown type '{name}'")
        elif precedence.count(name) > 1:
            errors.append(f"precedence names type '{name}' more than once")
    for t in types:
        for pattern in t["paths"]:
            problem = _pattern_problem(pattern)
            if problem:
                errors.append(f"type '{t['id']}': path pattern {pattern!r} {problem}")
    for pattern in pl["generated"]:
        problem = _pattern_problem(pattern)
        if problem:
            errors.append(f"generated: path pattern {pattern!r} {problem}")
    last = types[-1]
    if all(_pattern_problem(p) is None for p in last["paths"]):
        missed = [probe for probe in PROBE_PATHS if not any(glob_match(p, probe) for p in last["paths"])]
        if missed:
            errors.append(
                f"the last type ('{last['id']}') must match every path, for example paths = [\"**\"]; "
                f"it does not match {missed[0]!r}"
            )
    if pl["sizes"]["S"] >= pl["sizes"]["M"]:
        errors.append("sizes: S must be smaller than M")

    # matrix: a row for every type, and every row consistent with the stages
    matrix = pl["matrix"]
    for tid in dict.fromkeys(type_ids):
        if tid not in matrix:
            errors.append(f"type '{tid}' has no matrix row (add [matrix.{tid}], or [matrix.{tid}.S], .M and .L)")
    for name in matrix:
        if name not in type_ids:
            errors.append(f"matrix row '{name}' does not belong to any type")
    for name, row in matrix.items():
        for label, flat in _split_row(name, row, errors):
            _check_row(label, flat, stages, index, input_names, bad_reads, errors, notes)
    return errors, notes


def _check_row(label, row, stages, index, input_names, bad_reads, errors, notes) -> None:
    where = f"row '{label}'"
    for key in row:
        if key not in ("stages", "lenses", "note"):
            errors.append(f"{where}: unknown key {key!r} (a row has stages, lenses and note)")
    if "note" in row and not isinstance(row["note"], str):
        errors.append(f"{where}: note must be a string")
    names = row["stages"]
    if not isinstance(names, list) or not all(isinstance(s, str) for s in names):
        errors.append(f"{where}: stages must be a list of stage ids")
        return

    present = []
    for sid in names:
        if sid not in index:
            errors.append(f"{where}: unknown stage {sid!r}")
        elif sid in present:
            errors.append(f"{where}: stage {sid!r} is listed twice")
        else:
            present.append(sid)
    ordered = sorted(present, key=index.__getitem__)
    if present != ordered:
        errors.append(
            f"{where}: stages must appear in the pipeline's definition order "
            f"(expected {', '.join(ordered)}; a row selects stages, it never reorders them)"
        )

    position = {sid: i for i, sid in enumerate(present)}
    for sid in present:
        # A stage run by the main session reduces whatever the row produced, so a
        # row that leaves out a stage it reads (a repo's row of just intake and
        # reduce) is a note. For every other stage it is an error.
        by_main = stages[index[sid]].get("role") == "main"
        for name, optional in _read_names(stages[index[sid]]):
            if optional or name in input_names or (sid, name) in bad_reads:
                continue
            if name in position and position[name] < position[sid]:
                continue
            if by_main and name not in position:
                notes.append(
                    f"{where}: stage '{sid}' is run by the main session and reads '{name}', "
                    "which this row does not include; it reduces what the row produced"
                )
            else:
                errors.append(f"{where}: stage '{sid}' reads '{name}', which is neither an input nor an earlier stage of this row")
        target = stages[index[sid]].get("on_fail")
        if target is not None and target != "main" and target not in position:
            notes.append(f"{where}: stage '{sid}' has on_fail '{target}', which this row does not include; it fails over to main")

    if "lenses" in row:
        lenses = row["lenses"]
        if not isinstance(lenses, list) or not lenses or not all(isinstance(x, str) for x in lenses):
            errors.append(f"{where}: lenses must be a non-empty list")
        else:
            for lens in lenses:
                if lens not in KNOWN_LENSES:
                    errors.append(f"{where}: unknown lens {lens!r} (known: {', '.join(KNOWN_LENSES)})")
        if not any(_is_diff_review(stages[index[sid]]) for sid in present):
            errors.append(f"{where}: sets lenses but includes no review stage whose target is diff")


def _is_diff_review(stage: dict) -> bool:
    return stage.get("kind", "agent") == "review" and stage.get("target") == "diff"


def check_pipeline_file(path: Path) -> tuple[dict | None, list[str], list[str]]:
    """Load and validate one pipeline file; returns (pipeline or None, errors, notes)."""
    try:
        pipeline = load_toml(path)
    except PlumblineError as exc:
        return None, [str(exc)], []
    errors, notes = validate_pipeline(pipeline)
    return pipeline, errors, notes


# -------------------------------------------------------- project (repo)


@dataclass
class Project:
    """A repository's resolved plumbline setup."""

    root: Path
    adopted: bool  # a plumbline.toml exists (valid or not)
    config: dict | None
    pipeline: dict | None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def graft_enabled(self) -> bool:
        graft = (self.config or {}).get("graft")
        return isinstance(graft, dict) and graft.get("enabled") is True


def shipped_pipelines() -> list[str]:
    return sorted(p.stem for p in PIPELINE_DIR.glob("*.toml"))


def resolve_pipeline_path(value: str, root: Path) -> Path:
    """A pipeline named in plumbline.toml: a shipped name, or a repo-relative path."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        path = PIPELINE_DIR / f"{value}.toml"
        if not path.is_file():
            raise PlumblineError(f"pipeline {value!r} is not shipped with plumbline (shipped: {', '.join(shipped_pipelines())})")
        return path
    relative = Path(value)
    if relative.is_absolute():
        raise PlumblineError(f"pipeline {value!r} must be a shipped name or a path relative to the repository")
    resolved = (root / relative).resolve()
    if root.resolve() not in resolved.parents:
        raise PlumblineError(f"pipeline {value!r} points outside the repository")
    return resolved


def merge_config(pipeline: dict, config: dict) -> dict:
    """The pipeline with a repo's overrides applied: its precedence replaces the
    pipeline's, its types are tried first, its rows replace or add rows."""
    merged = copy.deepcopy(pipeline)
    if "precedence" in config:
        merged["precedence"] = list(config["precedence"])
    if config.get("type"):
        base_types = merged.get("type")
        merged["type"] = copy.deepcopy(config["type"]) + (base_types if isinstance(base_types, list) else [])
    if config.get("matrix"):
        matrix = merged.get("matrix")
        matrix = matrix if isinstance(matrix, dict) else {}
        matrix.update(copy.deepcopy(config["matrix"]))
        merged["matrix"] = matrix
    return merged


def load_project(root: Path, base_file: Path | None = None) -> Project:
    """Resolve the pipeline for a repository: its plumbline.toml (if any) merged
    over the pipeline it names, or over `base_file` when one is given."""
    config_path = root / CONFIG_FILE
    if not config_path.is_file():
        pipeline, errors, notes = check_pipeline_file(base_file or PIPELINE_DIR / f"{DEFAULT_PIPELINE}.toml")
        return Project(root, False, None, pipeline, errors, notes)
    try:
        config = load_toml(config_path)
    except PlumblineError as exc:
        return Project(root, True, None, None, [str(exc)])
    problems = validate(config, CONFIG_SHAPE)
    if problems:
        return Project(root, True, config, None, [f"{CONFIG_FILE}: {p}" for p in problems])
    try:
        path = base_file or resolve_pipeline_path(config.get("pipeline", DEFAULT_PIPELINE), root)
        base = load_toml(path)
    except PlumblineError as exc:
        return Project(root, True, config, None, [f"{CONFIG_FILE}: {exc}"])
    merged = merge_config(base, config)
    errors, notes = validate_pipeline(merged)
    return Project(root, True, config, merged, errors, notes)


# ------------------------------------------------------------------- git


def _git(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise PlumblineError("git was not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise PlumblineError(f"git {args[0]} timed out") from None


def _git_text(root: Path, *args: str) -> str:
    result = _git(root, *args)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit status {result.returncode}"
        raise PlumblineError(f"git {' '.join(args[:2])} failed: {detail}")
    return result.stdout.decode("utf-8", "surrogateescape")


def git_toplevel(path: Path, timeout: int = 10) -> Path | None:
    """The top level of the git work tree containing `path`, or None. Never raises."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and top else None


def resolve_root(project: str | None, require_git: bool = False) -> Path:
    """The repository root for --project (default: the current directory)."""
    start = Path(project).expanduser().resolve() if project else Path.cwd().resolve()
    if not start.is_dir():
        raise PlumblineError(f"{start} is not a directory")
    top = git_toplevel(start)
    if top is None and require_git:
        raise PlumblineError(f"{start} is not inside a git repository")
    return top or start


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def default_base(root: Path) -> tuple[str, str | None]:
    """The base to measure against: the remote's default branch, else origin/main,
    else main. Returns (ref, note); the note says when a fallback was used."""
    result = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    remote_head = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode == 0 and remote_head and _ref_exists(root, remote_head):
        return remote_head, None
    for ref in ("origin/main", "main"):
        if _ref_exists(root, ref):
            return ref, f"the remote's default branch is not known here, so the base fell back to '{ref}'"
    raise PlumblineError("no base found: the remote's default branch, origin/main and main are all missing; pass --base REF")


def _count_lines(path: Path) -> int | None:
    """Lines in a text file; None when it looks binary (a NUL in the first 8 KiB)."""
    lines, last, first = 0, b"", True
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 16):
            if first and b"\0" in chunk[:8192]:
                return None
            first = False
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    return lines + (1 if last and last != b"\n" else 0)


def size_for(lines: int, sizes: dict) -> str:
    if lines <= sizes["S"]:
        return "S"
    return "M" if lines <= sizes["M"] else "L"


def row_label(pipeline: dict, type_id: str, size: str) -> str:
    """`code.M` for a row split by size, `docs` for a flat row."""
    row = pipeline["matrix"][type_id]
    return type_id if "stages" in row else f"{type_id}.{size}"


def find_row(pipeline: dict, label: str) -> dict:
    """The flat row a label such as `code.M` or `docs` names."""
    type_id, _, size = label.partition(".")
    row = pipeline["matrix"][type_id]
    return row[size] if size else row


def classify(root: Path, pipeline: dict, base: str | None = None) -> dict:
    """The change_class record for the work tree at `root`: the diff from
    `git merge-base <base> HEAD` to the working tree, plus untracked files that
    are not ignored (counted as added lines)."""
    head_result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head_result.returncode != 0:
        raise PlumblineError("the repository has no commits yet; there is nothing to compare against")
    head = head_result.stdout.decode().strip()

    notes: list[str] = []
    if base is None:
        base, note = default_base(root)
        if note:
            notes.append(note)
    elif base.startswith("-") or not _ref_exists(root, base):
        raise PlumblineError(f"base {base!r} is not a commit in this repository")
    merge_result = _git(root, "merge-base", base, "HEAD")
    merge_base = merge_result.stdout.decode().strip()
    if merge_result.returncode != 0 or not merge_base:
        raise PlumblineError(f"'{base}' and HEAD have no common ancestor")

    entries: dict[str, dict] = {}
    raw = _git_text(root, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", merge_base, "--").split("\0")
    position = 0
    while position < len(raw):
        token = raw[position]
        if token.startswith(":") and position + 1 < len(raw):
            new_mode = token[1:].split(" ")[1]
            entries[raw[position + 1]] = {"added": 0, "removed": 0, "symlink": new_mode == "120000"}
            position += 2
        else:
            position += 1

    binary: list[str] = []
    numstat = _git_text(root, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", merge_base, "--")
    for record in numstat.split("\0"):
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        entry = entries.setdefault(path, {"added": 0, "removed": 0, "symlink": False})
        if added == "-":
            binary.append(path)
        else:
            entry["added"], entry["removed"] = int(added), int(removed)

    untracked = _git_text(root, "ls-files", "--others", "--exclude-standard", "-z")
    counted = 0
    for path in untracked.split("\0"):
        if not path or path.startswith(".plumbline/") or path in entries:
            continue
        full = root / path
        if path.endswith("/"):
            notes.append(f"skipped the untracked directory {path} (a nested repository?)")
        elif full.is_symlink():
            entries[path] = {"added": 1, "removed": 0, "symlink": True}
            counted += 1
        elif full.is_file():
            lines = _count_lines(full)
            if lines is None:
                binary.append(path)
                lines = 0
            entries[path] = {"added": lines, "removed": 0, "symlink": False}
            counted += 1
    if counted:
        notes.append(f"{counted} untracked file(s) that are not ignored were counted as added lines")
    if binary:
        notes.append("binary files count as 0 lines: " + ", ".join(sorted(binary)))

    if not entries:
        raise PlumblineError(f"no changes between the merge base of '{base}' and the working tree; there is nothing to classify")

    type_patterns = [(t["id"], t["paths"]) for t in pipeline["type"]]
    files = []
    for path in sorted(entries):
        entry = entries[path]
        file_type = next((tid for tid, patterns in type_patterns if any(glob_match(p, path) for p in patterns)), type_patterns[-1][0])
        files.append(
            {
                "path": path,
                "type": file_type,
                "added": entry["added"],
                "removed": entry["removed"],
                "generated": any(glob_match(p, path) for p in pipeline["generated"]),
                "symlink": entry["symlink"],
            }
        )
    lines = sum(f["added"] + f["removed"] for f in files if not f["generated"])
    size = size_for(lines, pipeline["sizes"])
    present = {f["type"] for f in files}
    types = [tid for tid in pipeline["precedence"] if tid in present]
    return {
        "base": base,
        "head": head,
        "merge_base": merge_base,
        "files": files,
        "types": types,
        "lines": lines,
        "size": size,
        "row": row_label(pipeline, types[0], size),
        "symlinks": [f["path"] for f in files if f["symlink"]],
        "notes": notes,
    }


# ------------------------------------------------------------------ plan


def default_run_id(root: Path) -> str:
    short = _git_text(root, "rev-parse", "--short", "HEAD").strip()
    return f"{short}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def build_plan(pipeline: dict, record: dict, run_id: str) -> dict:
    """The stages to run for a classified change, in order, with every read
    resolved to a record path and every on_fail resolved within the row."""
    row = find_row(pipeline, record["row"])
    by_id = {s["id"]: s for s in pipeline["stage"]}
    in_row = list(row["stages"])
    record_dir = f"{RUNS_DIR}/{run_id}"

    stages = []
    for sid in in_row:
        stage = by_id[sid]
        kind = stage.get("kind", "agent")
        lenses = stage.get("lenses")
        if _is_diff_review(stage) and "lenses" in row:
            lenses = list(row["lenses"])
        reads = []
        for name, optional in _read_names(stage):
            is_input = name in pipeline["inputs"]
            path = None if is_input or name not in in_row else f"{record_dir}/{name}.json"
            reads.append({"name": name, "kind": "input" if is_input else "record", "path": path, "optional": optional})
        on_fail = stage.get("on_fail")
        if on_fail is not None and on_fail != "main" and on_fail not in in_row:
            on_fail = "main"  # its target is not in this row
        entry = {
            "id": sid,
            "kind": kind,
            "role": stage.get("role"),
            "reads": reads,
            "record": stage["record"],
            "path": f"{record_dir}/{sid}.json",
            "gate": stage.get("gate"),
            "on_fail": on_fail,
            "max_rounds": stage.get("max_rounds"),
            "lenses": lenses,
        }
        if kind == "review":
            for key in ("target", "defenders", "survive_if_unrefuted_by", "detective"):
                entry[key] = stage.get(key)
        stages.append(entry)
    return {
        "run_id": run_id,
        "pipeline": pipeline["name"],
        "row": record["row"],
        "size": record["size"],
        "lines": record["lines"],
        "types": record["types"],
        "head": record["head"],
        "base": record["base"],
        "merge_base": record["merge_base"],
        "note": row.get("note"),
        "record_dir": record_dir,
        "stages": stages,
    }


# ------------------------------------------------------------------ init


def config_template(graft: bool) -> str:
    enabled = "true" if graft else "false"
    return f"""# plumbline: per-repository configuration. Commit this file.
schema = 1
pipeline = "default"        # a pipeline shipped in the plugin's pipeline/, or a repo-relative path

# Optional overrides. Top-level keys such as `precedence` must stay above the first [table].
# precedence = ["voice", "code", "config", "tests", "docs"]   # replaces the pipeline's list; it must name every type

[graft]
enabled = {enabled}

# A type of your own is tried before the pipeline's own types:
# [[type]]
# id = "voice"
# paths = ["voice/**"]
#
# A row replaces the pipeline's row for that type, or adds one for a new type:
# [matrix.voice]
# stages = ["intake", "reduce"]
# note = "Voice and prompt changes go through this repository's own evals."
"""


def init_repo(root: Path, graft: bool) -> dict:
    """Write plumbline.toml and ignore .plumbline/. Raises FileExistsError, before
    changing anything, when plumbline.toml already exists. Commits nothing."""
    with open(root / CONFIG_FILE, "x", encoding="utf-8") as handle:
        handle.write(config_template(graft))
    gitignore = root / ".gitignore"
    try:
        text = gitignore.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    if text is not None and any(line.strip() in IGNORE_EQUIVALENTS for line in text.splitlines()):
        state = "unchanged"
    else:
        separator = "" if not text or text.endswith("\n") else "\n"
        with open(gitignore, "a", encoding="utf-8") as handle:
            handle.write(separator + IGNORE_ENTRY + "\n")
        state = "created" if text is None else "appended"
    return {"root": root, "graft": graft, "gitignore": state}


# ---------------------------------------------------------------- render
#
# Markdown for each record type. Rendering never fails on a malformed record:
# the accessors tolerate missing keys and wrong types, and anything that still
# goes wrong falls back to the record as JSON.

MARKERS = {
    "merge_base": "change_class",
    "acceptance_criteria": "spec",
    "stub_check": "tests_record",
    "acs_addressed": "build_note",
    "failing_acs": "verify_record",
    "findings": "review_record",
    "run_id": "pass_record",
    "stages_skipped": "override_record",
}


def infer_record_type(data) -> str:
    """The record type, from a key only that type has."""
    if isinstance(data, dict):
        hits = {MARKERS[key] for key in data if key in MARKERS}
        if len(hits) == 1:
            return hits.pop()
    raise PlumblineError("cannot tell the record type from its keys; pass --type")


def _s(value) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _cell(value) -> str:
    return _s(value).replace("|", "\\|").replace("\n", " ")


def _short(sha) -> str:
    return _s(sha)[:7]


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _get(mapping, key, default=None):
    return mapping.get(key, default) if isinstance(mapping, dict) else default


def _check(value) -> str:
    return "n/a" if value is None else ("pass" if value else "fail")


def _section(title: str, items, fmt=_s) -> list[str]:
    items = _list(items)
    return ["", f"## {title}", ""] + ([f"- {fmt(item)}" for item in items] if items else ["None."])


def _table(headers: list[str], rows: list[list], align: frozenset | set = frozenset()) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---:" if c in align else "---" for c in range(len(headers))) + " |"]
    return lines + ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]


def _render_change_class(d) -> list[str]:
    out = ["# Change class", ""]
    out.append(
        f"Row `{_s(d.get('row'))}`: size {_s(d.get('size'))}, {_s(d.get('lines'))} changed lines. "
        f"Types: {', '.join(_s(t) for t in _list(d.get('types'))) or 'none'}."
    )
    out += ["", f"- Base: `{_s(d.get('base'))}`, merge base `{_short(d.get('merge_base'))}`", f"- Head: `{_short(d.get('head'))}`"]
    out += ["", "## Files", ""]
    files = _list(d.get("files"))
    if files:
        rows = []
        for f in files:
            flags = ", ".join(name for name in ("generated", "symlink") if _get(f, name) is True)
            rows.append([_get(f, "path"), _get(f, "type"), _get(f, "added"), _get(f, "removed"), flags])
        out += _table(["Path", "Type", "Added", "Removed", "Flags"], rows, align={2, 3})
    else:
        out.append("None.")
    out += _section("Symlinks", d.get("symlinks"))
    out += _section("Notes", d.get("notes"))
    return out


def _render_spec(d) -> list[str]:
    out = ["# Spec", "", "## Goal", "", _s(d.get("goal"))]
    out += _section("Non-goals", d.get("non_goals"))
    out += ["", "## Acceptance criteria", ""]
    criteria = _list(d.get("acceptance_criteria"))
    for ac in criteria:
        out += [f"- **{_s(_get(ac, 'id'))}** {_s(_get(ac, 'statement'))}", f"  - Verification: {_s(_get(ac, 'verification'))}"]
    if not criteria:
        out.append("None.")
    out += _section("Interfaces", d.get("interfaces"), lambda i: f"`{_s(_get(i, 'name'))}` in `{_s(_get(i, 'file'))}`: `{_s(_get(i, 'signature'))}`")
    out += _section("Test plan", d.get("test_plan"), lambda t: f"**{_s(_get(t, 'ac'))}**: {_s(_get(t, 'scenario'))}")
    out += _section("Risks", d.get("risks"))
    if d.get("split_proposal") is None:
        out += ["", "## Split proposal", "", "None: the change is size M or smaller."]
    else:
        out += _section("Split proposal", d.get("split_proposal"))
    return out


def _render_tests_record(d) -> list[str]:
    out = ["# Tests written"]
    out += _section(
        "Tests",
        d.get("tests"),
        lambda t: f"**{_s(_get(t, 'id'))}** `{_s(_get(t, 'name'))}` in `{_s(_get(t, 'file'))}`, "
        f"covers {', '.join(_s(a) for a in _list(_get(t, 'ac_ids'))) or 'no criterion'}: {_s(_get(t, 'scenario'))}",
    )
    out += _section("Files written", d.get("files_written"), lambda f: f"`{_s(f)}`")
    stub = d.get("stub_check")
    out += [
        "",
        "## Stub check",
        "",
        f"- Ran: {_s(_get(stub, 'ran'))}",
        f"- All failed on assertions: {_s(_get(stub, 'all_failed_on_assertions'))}",
        f"- Detail: {_s(_get(stub, 'detail'))}",
    ]
    return out


def _render_build_note(d) -> list[str]:
    out = ["# Build note", "", _s(d.get("summary"))]
    out += _section("Files changed", d.get("files_changed"), lambda f: f"`{_s(f)}`")
    out += _section("Acceptance criteria addressed", d.get("acs_addressed"))
    out += _section("Assumptions", d.get("assumptions"))
    return out


def _render_verify_record(d) -> list[str]:
    out = [f"# Verify: {'green' if d.get('green') is True else 'not green'}", "", "## Commands", ""]
    commands = _list(d.get("commands"))
    if commands:
        out += _table(
            ["Name", "Command", "Exit", "Summary"],
            [[_get(c, "name"), f"`{_s(_get(c, 'command'))}`", _get(c, "exit_code"), _get(c, "summary")] for c in commands],
        )
    else:
        out.append("None.")
    tests = d.get("tests")
    out += [
        "",
        "## Tests",
        "",
        f"- Passed: {_s(_get(tests, 'passed'))}",
        f"- Failed: {_s(_get(tests, 'failed'))}",
        f"- Skipped: {_s(_get(tests, 'skipped'))}",
    ]
    out += _section("Failing acceptance criteria", d.get("failing_acs"), lambda f: f"**{_s(_get(f, 'ac'))}**: {_s(_get(f, 'error_type'))}")
    checks = _get(d, "checks", {})
    out += ["", "## Checks", ""]
    for name in ("lint", "secrets", "symlinks", "abs_paths", "graft_fresh"):
        out.append(f"- {name}: {_check(_get(checks, name))}")
    return out


def _render_review_record(d) -> list[str]:
    out = [
        f"# Review of {_s(d.get('target'))}, round {_s(d.get('round'))}",
        "",
        f"Lenses: {', '.join(_s(x) for x in _list(d.get('lenses'))) or 'none'}. "
        f"Blockers surviving: {_s(d.get('blockers_surviving'))}.",
        "",
        "## Findings",
        "",
    ]
    findings = _list(d.get("findings"))
    for f in findings:
        out += [
            f"- **{_s(_get(f, 'id'))}** [{_s(_get(f, 'severity'))}] ({_s(_get(f, 'lens'))}) `{_s(_get(f, 'file'))}:{_s(_get(f, 'line'))}` {_s(_get(f, 'claim'))}",
            f"  - Failure scenario: {_s(_get(f, 'failure_scenario'))}",
            f"  - Rule: {_s(_get(f, 'rule'))}",
            f"  - Evidence: `{_s(_get(f, 'evidence'))}`",
        ]
        if _get(f, "outside_code") is not None:
            out.append(f"  - Outside the code: {_s(_get(f, 'outside_code'))}")
    if not findings:
        out.append("None.")
    out += _section(
        "Defenses",
        d.get("defenses"),
        lambda x: f"{_s(_get(x, 'finding_id'))}, {_s(_get(x, 'defender'))}: {_s(_get(x, 'verdict'))}. {_s(_get(x, 'reason'))}",
    )
    out += _section("Survivors", d.get("survivors"))
    out += _section(
        "Gaps",
        d.get("gaps"),
        lambda g: f"**{_s(_get(g, 'id'))}** ({_s(_get(g, 'kind'))}"
        + (f", {_s(_get(g, 'ac'))}" if _get(g, "ac") is not None else "")
        + f") {_s(_get(g, 'detail'))}",
    )
    return out


def _render_pass_record(d) -> list[str]:
    out = [
        f"# Pass record: {_s(d.get('verdict'))}",
        "",
        f"- Commit: `{_short(d.get('commit'))}`",
        f"- Run: `{_s(d.get('run_id'))}`",
        f"- Row: `{_s(d.get('row'))}`",
        "",
        "## Stages",
        "",
    ]
    stages = _list(d.get("stages"))
    if stages:
        out += _table(
            ["Stage", "Record", "Gate", "Passed", "Rounds"],
            [[_get(s, "id"), f"`{_s(_get(s, 'record'))}`", _get(s, "gate"), _get(s, "passed"), _get(s, "rounds")] for s in stages],
            align={4},
        )
    else:
        out.append("None.")
    by_model = _get(_get(d, "tokens"), "by_model", {})
    out += ["", "## Tokens", ""]
    if isinstance(by_model, dict) and by_model:
        out += _table(
            ["Model", "Output", "Fresh input", "Cache read"],
            [[m, _get(u, "output"), _get(u, "fresh_input"), _get(u, "cache_read")] for m, u in by_model.items()],
            align={1, 2, 3},
        )
    else:
        out.append("None recorded.")
    out += _section("Notes", d.get("notes"))
    return out


def _render_override_record(d) -> list[str]:
    out = ["# Override", "", f"- Commit: `{_short(d.get('commit'))}`", f"- By: {_s(d.get('by'))}", f"- At: {_s(d.get('at'))}", "", "## Reason", "", _s(d.get("reason"))]
    out += _section("Stages skipped", d.get("stages_skipped"))
    return out


RENDERERS = {
    "change_class": _render_change_class,
    "spec": _render_spec,
    "tests_record": _render_tests_record,
    "build_note": _render_build_note,
    "verify_record": _render_verify_record,
    "review_record": _render_review_record,
    "pass_record": _render_pass_record,
    "override_record": _render_override_record,
}


def render_record(type_name: str, data) -> str:
    """The record as markdown."""
    lines = None
    if isinstance(data, dict) and type_name in RENDERERS:
        try:
            lines = RENDERERS[type_name](data)
        except Exception:  # a malformed record still renders, as JSON
            lines = None
    if lines is None:
        lines = [f"# {type_name}", "", "```json", json.dumps(data, indent=2), "```"]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- CLI


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _print_report(label: str, errors: list[str], notes: list[str]) -> int:
    for error in errors:
        print(f"error: {error}")
    for note in notes:
        print(f"note: {note}")
    print(f"{label}: {'invalid' if errors else 'valid'} ({_plural(len(errors), 'error')}, {_plural(len(notes), 'note')})")
    return 1 if errors else 0


def cmd_validate_pipeline(args) -> int:
    base_file = Path(args.file).expanduser() if args.file else None
    if args.project is None:
        pipeline, errors, notes = check_pipeline_file(base_file or PIPELINE_DIR / f"{DEFAULT_PIPELINE}.toml")
        label = f"pipeline '{pipeline['name']}'" if pipeline and isinstance(pipeline.get("name"), str) else (args.file or DEFAULT_PIPELINE)
        return _print_report(label, errors, notes)
    root = resolve_root(args.project)
    project = load_project(root, base_file)
    notes = list(project.notes)
    if not project.adopted:
        notes.insert(0, f"no {CONFIG_FILE} in {root}; validated the pipeline alone")
    pipeline = project.pipeline
    label = f"pipeline '{pipeline['name']}' for {root}" if pipeline and isinstance(pipeline.get("name"), str) else str(root)
    return _print_report(label, project.errors, notes)


def _ready_project(project_arg: str | None) -> Project:
    root = resolve_root(project_arg, require_git=True)
    project = load_project(root)
    if project.errors:
        for error in project.errors:
            print(f"error: {error}", file=sys.stderr)
        raise PlumblineError(f"the pipeline configuration for {root} is invalid; `validate-pipeline --project` lists the problems")
    return project


def cmd_classify(args) -> int:
    project = _ready_project(args.project)
    record = classify(project.root, project.pipeline, args.base)
    problems = check_record("change_class", record)
    if problems:
        raise PlumblineError("internal error: the change_class record does not validate: " + "; ".join(problems))
    text = json.dumps(record, indent=2) + "\n"
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: row {record['row']}, size {record['size']}, {record['lines']} changed lines")
    else:
        sys.stdout.write(text)
    return 0


def cmd_plan(args) -> int:
    project = _ready_project(args.project)
    run_id = args.run_id if args.run_id is not None else default_run_id(project.root)
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PlumblineError(f"run id {run_id!r} must be letters, digits, '.', '_' or '-', and start with a letter or digit")
    record = classify(project.root, project.pipeline, args.base)
    sys.stdout.write(json.dumps(build_plan(project.pipeline, record, run_id), indent=2) + "\n")
    return 0


def _read_json(path_arg: str):
    path = Path(path_arg).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlumblineError(f"{path_arg}: no such file") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PlumblineError(f"{path_arg}: cannot read: {exc}") from None


def cmd_check_record(args) -> int:
    load_schema(args.type)  # an unknown type is a usage problem, whatever the file holds
    try:
        data = _read_json(args.file)
    except ValueError as exc:
        print(f"{args.file}: not valid JSON: {exc}")
        return 1
    errors = check_record(args.type, data)
    for error in errors:
        print(error)
    if errors:
        print(f"{args.file}: not a valid {args.type} ({_plural(len(errors), 'error')})")
        return 1
    print(f"{args.file}: a valid {args.type}")
    return 0


def cmd_render(args) -> int:
    try:
        data = _read_json(args.file)
    except ValueError as exc:
        raise PlumblineError(f"{args.file}: not valid JSON: {exc}") from None
    type_name = args.type or infer_record_type(data)
    load_schema(type_name)  # an unknown type is a usage problem
    sys.stdout.write(render_record(type_name, data))
    return 0


def cmd_init(args) -> int:
    root = resolve_root(args.project)
    try:
        result = init_repo(root, args.graft)
    except FileExistsError:
        print(f"plumbline: {CONFIG_FILE} already exists in {root}; it is never overwritten. Nothing was changed.", file=sys.stderr)
        return 1
    changed = [CONFIG_FILE]
    print(f"plumbline: adopted {root}")
    print(f"  wrote {CONFIG_FILE} (pipeline '{DEFAULT_PIPELINE}', graft {'on' if args.graft else 'off'})")
    if result["gitignore"] == "unchanged":
        print(f"  .gitignore already ignores {IGNORE_ENTRY} (unchanged)")
    else:
        changed.append(".gitignore")
        print(f"  {'created .gitignore with' if result['gitignore'] == 'created' else 'appended to .gitignore:'} {IGNORE_ENTRY}")
    print(f"Commit {' and '.join(changed)}; plumbline commits nothing itself.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plumbline", description="The plumbline pipeline CLI.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("validate-pipeline", help="validate the default pipeline, or FILE, merged with the repo's plumbline.toml")
    p.add_argument("file", nargs="?", help="a pipeline file (default: the plugin's pipeline/default.toml)")
    p.add_argument("--project", metavar="PATH", help="a repository whose plumbline.toml is merged in")
    p.set_defaults(run=cmd_validate_pipeline)

    p = sub.add_parser("classify", help="write the change_class record for the current change")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--base", metavar="REF", help="default: the remote's default branch, else origin/main, else main")
    p.add_argument("--out", metavar="FILE", help="write the record here instead of to stdout")
    p.set_defaults(run=cmd_classify)

    p = sub.add_parser("plan", help="print the stages to run for the current change, as JSON")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--base", metavar="REF")
    p.add_argument("--run-id", metavar="ID", help="default: <short HEAD sha>-<UTC timestamp>")
    p.set_defaults(run=cmd_plan)

    p = sub.add_parser("check-record", help="validate a record file against its schema")
    p.add_argument("type", help="a record type: one of the files in schemas/")
    p.add_argument("file")
    p.set_defaults(run=cmd_check_record)

    p = sub.add_parser("render", help="print a record as markdown")
    p.add_argument("file")
    p.add_argument("--type", help="the record type (default: inferred from the record's keys)")
    p.set_defaults(run=cmd_render)

    p = sub.add_parser("init", help="adopt plumbline in a repository: write plumbline.toml, ignore .plumbline/")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--graft", action="store_true", help="record graft as enabled")
    p.set_defaults(run=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except PlumblineError as exc:
        print(f"plumbline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
