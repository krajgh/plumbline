#!/usr/bin/env python3
"""plumbline: the command line of the plumbline Claude Code plugin.

Standard library only (Python 3.11 or newer).

    validate-pipeline [FILE] [--project PATH]
    classify [--project PATH] [--base REF] [--intent ID] [--row ROW] [--out FILE]
    plan [--project PATH] [--base REF] [--run-id ID] [--intent ID [--spec FILE]] [--row ROW]
    check-record TYPE FILE
    render FILE [--type TYPE]
    init [--project PATH] [--graft]
    merge-review RUN STAGE [--round N] [--project PATH]
    gate RUN STAGE [--project PATH]
    tokens RUN [--project PATH]
    pass RUN [--project PATH]
    open [RUN] [--all] [--json] [--project PATH]
    override --reason TEXT [--run RUN] [--project PATH]
    status [--run RUN] [--project PATH]
    check-diff [--run RUN] [--base REF] [--project PATH]

A run is the directory .plumbline/runs/<run-id>/: one <stage-id>.json per stage,
the per-agent records of a review unit under <stage-id>/round-<n>/, and a
ledger.jsonl that only ever grows. `plan --intent ID` starts a run: it writes the
intake record (which carries the intent), copies the record an intent supplies in
place of a skipped stage, and names the run in .plumbline/runs/ACTIVE.

Gates measure and trace. A stage's record counts only with the ledger's entry for
the agent that wrote it (written by the SubagentStop hook, with the record's
sha256), and a review with the entry `merge-review` wrote. `gate` runs the
repository's own [commands] for a verify stage and a tests stage, and enters what
happened in the ledger. `pass` measures the change again and refuses a row that
selects stages the run lacks. A review that fails with blockers standing and rounds
left gets its next round's directory from `gate`: the review agents write only in
the highest round-<n> directory of their stage.

Exit status: 0 on success; 1 when what was checked is invalid, a gate fails, or
a command refuses (init over an existing plumbline.toml, pass on a dirty tree);
2 when the command could not run (bad arguments, not a git repository, an
unreadable file, no such run); 3 when a gate cannot pass because its stage has
used all its rounds.
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # tomllib arrived in 3.11
    raise SystemExit("plumbline needs Python 3.11 or newer (this is %d.%d)" % sys.version_info[:2])

import argparse
import contextlib
import copy
import datetime as dt
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = PLUGIN_ROOT / "schemas"
PIPELINE_DIR = PLUGIN_ROOT / "pipeline"

CONFIG_FILE = "plumbline.toml"
DEFAULT_PIPELINE = "default"
RUNS_DIR = ".plumbline/runs"
PASS_DIR = ".plumbline/pass"
LEDGER_FILE = "ledger.jsonl"
ACTIVE_FILE = "ACTIVE"  # .plumbline/runs/ACTIVE: the run id and a newline; written by `plan --intent`, read by the hooks
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
    "reproduces_on_head",
    "verify_green",
    "no_surviving_blockers",
    "all_gates_passed",
)
# The record type each gate reads, for checking an intent's `gates` against the stage they replace.
GATE_RECORDS = {
    "spec_complete": "spec",
    "acs_covered": "tests_record",
    "tests_fail_on_stub": "tests_record",
    "reproduces_on_head": "tests_record",
    "verify_green": "verify_record",
    "no_surviving_blockers": "review_record",
    "all_gates_passed": "pass_record",
}
REVIEW_ONLY_KEYS = ("target", "lenses", "defenders", "survive_if_unrefuted_by", "detective")
# The gates that run the repository's own commands (see measure_stage): the verify stage's and the tests stage's.
MEASURED_TESTS_GATES = ("tests_fail_on_stub", "reproduces_on_head")
DEFAULT_COMMAND_TIMEOUT = 900  # seconds each declared command may run; `timeout` under [commands] changes it
MIN_QUOTE = 6  # a defender's quote of fewer characters (whitespace-normalised) proves nothing
TESTS_TYPE = "tests"  # the file type of test files
TESTS_LENS = "tests"  # the lens that reviews tests
SINGLE_AC_INTENTS = ("fix",)  # a fix reproduces one bug: its spec has exactly one acceptance criterion
UNCHANGED_TESTS_INTENTS = ("refactor",)  # behaviour stays the same, so no test file changes
OPEN_FINDING_KEYS = ("id", "lens", "severity", "file", "line", "claim", "failure_scenario")  # what the pass record keeps of a surviving finding, after its stage
OPEN_GAP_KEYS = ("id", "kind", "detail", "ac")  # and of a gap

# The plumbline agents (agent type `plumbline:<name>`) and the record each one
# ends its work with; the SubagentStop hook validates against it.
AGENT_PREFIX = "plumbline:"
AGENT_RECORDS = {
    "planner": "spec",
    "test-writer": "tests_record",
    "builder": "build_note",
    "verifier": "verify_record",
    "prosecutor": "findings_record",
    "defender": "defense_record",
    "detective": "gaps_record",
}
AGENT_ROLES = tuple(AGENT_RECORDS)  # the roles a [roles.<name>] policy may describe
REVIEW_PART_TYPES = ("findings_record", "defense_record", "gaps_record")
# Role policies (see [roles.*] in pipeline/default.toml): where an agent may write, and what its Bash may run.
KNOWN_WRITE_TARGETS = ("record", "tests", "stubs", "code")
STUBS_DIR = "stubs"  # .plumbline/runs/<run-id>/stubs/: the stubs of brand-new modules, in the run and so outside the change
CONFIG_COMMANDS = ("test", "lint", "typecheck", "build")  # the keys of [commands] in plumbline.toml
KNOWN_COMMAND_CLASSES = (*CONFIG_COMMANDS, "git-read", "search", "plumbline-check", "graft")
DEFAULT_INTENT = "feature"
TEMPLATE_DIR = PIPELINE_DIR / "templates"
ID_PATTERN = r"^[a-z][a-z0-9_-]*$"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Paths every "matches everything" type must match (see validate_pipeline).
PROBE_PATHS = ("a", ".a", "a.b", "a/b", "a/b/c.d", ".github/workflows/x.yml", "dir with space/f g")


class PlumblineError(Exception):
    """Something to tell the user, ending the command with exit status 2."""


class NothingToClassify(PlumblineError):
    """There is no change between the merge base and the working tree."""


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


def file_type(pipeline: dict, path: str) -> str:
    """The type of a repository-relative path: the first type of the pipeline (a repository's own types come first)
    whose paths match, and the last type, which matches every path, when none does."""
    types = [(t["id"], t["paths"]) for t in pipeline["type"]]
    return next((tid for tid, patterns in types if any(glob_match(p, path) for p in patterns)), types[-1][0])


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


def _claims_on_concessions(data) -> list[str]:
    """A defender that refutes a finding says its claim is wrong, so only a conceded verdict carries a `severity_claim`."""
    return [
        f"{_child(f'$.defenses[{index}]', 'severity_claim')}: only a conceded verdict carries a severity claim (this one is {entry['verdict']})"
        for index, entry in enumerate(data["defenses"])
        if "severity_claim" in entry and entry["verdict"] != "conceded"
    ]


RECORD_RULES = {"defense_record": _claims_on_concessions, "review_record": _claims_on_concessions}  # what a schema of this subset cannot say


def check_record(type_name: str, data) -> list[str]:
    """Errors (each with its JSON path) of `data` as a record of `type_name`: what its schema says, and, for a record that is valid
    by its schema, the rules the schema cannot say (RECORD_RULES)."""
    errors = validate(data, load_schema(type_name))
    return errors or RECORD_RULES.get(type_name, lambda _data: [])(data)


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
        "roles": {"type": "object"},
        "intent": {"type": "object"},
    },
}

# [commands] in plumbline.toml: what the `test`, `lint`, `typecheck` and `build` command classes run,
# each a command prefix, or a list of prefixes; `timeout` is the seconds each may run under `gate`.
_COMMAND_PREFIXES = {"type": ["string", "array"], "minLength": 1, "minItems": 1, "items": _TEXT}

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
        "commands": {
            "type": "object",
            "additionalProperties": False,
            "properties": {**{name: _COMMAND_PREFIXES for name in CONFIG_COMMANDS}, "timeout": {"type": "integer", "minimum": 1}},
        },
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
                elif target == sid:  # an agent stage may name itself: its agent runs again with the gate's problems
                    if kind != "agent":
                        errors.append(f"{where}: a review stage cannot name itself in on_fail; it names the agent stage to go back to, or 'main'")
                elif index[target] > position:
                    errors.append(f"{where}: on_fail {target!r} must be an earlier stage (or this stage itself, to run its agent again, or 'main'), not a later one")
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
    if "roles" in pl:
        _check_roles(pl["roles"], errors)
    if "intent" in pl:
        _check_intents(pl, errors)
    return errors, notes


def _check_row_on_fail(where, table, stages, index, present, errors) -> None:
    """A row's `on_fail` table: for a stage of the row, where a failed gate sends the run in this row, in place of the stage's own on_fail.
    A key names a stage of the row that has max_rounds (a stage that goes back needs its cap). A value is `main` or a stage of the row,
    under the rules of a stage's own on_fail: an earlier agent stage, or (for an agent stage) the stage itself."""
    if not isinstance(table, dict) or not all(isinstance(value, str) for value in table.values()):
        errors.append(f"{where}: on_fail must map stage ids to a stage of the row, or 'main'")
        return
    for sid, target in table.items():
        if sid not in present:
            errors.append(f"{where}: on_fail names stage {sid!r}, which the row does not include")
            continue
        stage = stages[index[sid]]
        if "max_rounds" not in stage:
            errors.append(f"{where}: on_fail names stage {sid!r}, which has no max_rounds (a stage that goes back needs its cap)")
        if target == "main":
            continue
        if target not in present:
            errors.append(f"{where}: on_fail for stage {sid!r} names {target!r}, which the row does not include (name a stage of the row, or 'main')")
        elif target == sid:
            if stage.get("kind", "agent") != "agent":
                errors.append(f"{where}: on_fail for the review stage {sid!r} cannot name itself; it names the agent stage to go back to, or 'main'")
        elif present.index(target) > present.index(sid):
            errors.append(
                f"{where}: on_fail for stage {sid!r} names {target!r}, which comes later in the row "
                "(name an earlier stage, this stage itself to run its agent again, or 'main')"
            )
        elif stages[index[target]].get("kind", "agent") != "agent":
            errors.append(f"{where}: on_fail for stage {sid!r} names {target!r}, which is a review stage; it must name an agent stage or 'main'")


def _check_row(label, row, stages, index, input_names, bad_reads, errors, notes) -> None:
    where = f"row '{label}'"
    for key in row:
        if key not in ("stages", "lenses", "note", "on_fail"):
            errors.append(f"{where}: unknown key {key!r} (a row has stages, lenses, note and on_fail)")
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
    if "on_fail" in row:
        _check_row_on_fail(where, row["on_fail"], stages, index, present, errors)
    overridden = set(row["on_fail"]) if isinstance(row.get("on_fail"), dict) else set()  # the stages whose on_fail the row sets

    position = {sid: i for i, sid in enumerate(present)}
    for sid in present:
        for name, optional in _read_names(stages[index[sid]]):
            if optional or name in input_names or (sid, name) in bad_reads:
                continue
            if name in position and position[name] < position[sid]:
                continue
            errors.append(f"{where}: stage '{sid}' reads '{name}', which is neither an input nor an earlier stage of this row")
        target = stages[index[sid]].get("on_fail")  # where the row sets one for this stage, it replaces this (and is checked above)
        if sid not in overridden and target is not None and target != "main" and target not in position:
            notes.append(
                f"{where}: stage '{sid}' has on_fail '{target}', which this row does not include; it fails over to main "
                f"(on_fail = {{ {sid} = \"main\" }} in the row says so)"
            )

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


def _check_roles(roles, errors: list[str]) -> None:
    """The [roles.<agent>] policies: known write targets and command classes, one policy per agent."""
    for name, policy in roles.items():
        where = f"roles.{name}"
        if name not in AGENT_ROLES:
            errors.append(f"{where}: unknown role (the agents are {', '.join(AGENT_ROLES)})")
        if not isinstance(policy, dict):
            errors.append(f"{where}: must be a table with writes and commands")
            continue
        for key in policy:
            if key not in ("writes", "commands"):
                errors.append(f"{where}: unexpected key {key!r} (a role policy has writes and commands)")
        for key, known, what in (("writes", KNOWN_WRITE_TARGETS, "write target"), ("commands", KNOWN_COMMAND_CLASSES, "command")):
            value = policy.get(key)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"{where}: {key} must be a list of strings")
                continue
            for item in dict.fromkeys(value):
                if item not in known:
                    errors.append(f"{where}: unknown {what} {item!r} (known: {', '.join(known)})")
                elif value.count(item) > 1:
                    errors.append(f"{where}: {key} names {item!r} more than once")
        if isinstance(policy.get("writes"), list) and "record" not in policy["writes"]:
            errors.append(f"{where}: writes must include 'record', because every agent ends with its record")
    for name in AGENT_ROLES:
        if name not in roles:
            errors.append(f"roles: there is no policy for the agent '{name}' (every agent needs one)")


def _check_intents(pl: dict, errors: list[str]) -> None:
    """The [intent.<id>] tables. Each may name only known stages, gates and lenses; what it skips and
    supplies must be consistent; and after it is applied to every row, each required read must be
    produced by a stage left in the row or listed in `supplies`."""
    intents = pl["intent"]
    stages = pl["stage"]
    index = {stage["id"]: position for position, stage in enumerate(stages)}
    input_names = set(pl["inputs"])
    rows: list[tuple[str, dict]] = []
    for name, row in pl["matrix"].items():
        rows += _split_row(name, row, [])  # the rows' own problems are reported elsewhere
    for iid, intent in intents.items():
        where = f"intent '{iid}'"
        if not re.fullmatch(ID_PATTERN, iid):
            errors.append(f"{where}: the id must match {ID_PATTERN}")
        if not isinstance(intent, dict):
            errors.append(f"{where}: must be a table (skip, supplies, gates, lenses)")
            continue
        before = len(errors)
        for key in intent:
            if key not in ("skip", "supplies", "gates", "lenses"):
                errors.append(f"{where}: unexpected key {key!r} (an intent has skip, supplies, gates and lenses)")
        lists = {}
        for key in ("skip", "supplies"):
            value = intent.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"{where}: {key} must be a list of stage ids")
                value = []
            lists[key] = value
        skip, supplies = lists["skip"], lists["supplies"]
        for sid in dict.fromkeys(skip):
            if sid not in index:
                errors.append(f"{where}: skip names unknown stage {sid!r}")
            elif stages[index[sid]]["record"] in ("change_class", "pass_record"):
                errors.append(f"{where}: skip names {sid!r}, which every run needs (it writes the {stages[index[sid]]['record']} record)")
        for sid in dict.fromkeys(supplies):
            if sid not in index:
                errors.append(f"{where}: supplies names unknown stage {sid!r}")
            elif sid not in skip:
                errors.append(f"{where}: supplies names {sid!r}, which the intent does not skip (it supplies what it skips)")
            elif stages[index[sid]]["record"] != "spec":
                errors.append(f"{where}: supplies names {sid!r}, which writes a {stages[index[sid]]['record']} record; only a spec can be supplied")
        gates = intent.get("gates", {})
        if not isinstance(gates, dict) or not all(isinstance(g, str) for g in gates.values()):
            errors.append(f"{where}: gates must map stage ids to gate names")
            gates = {}
        for sid, gate in gates.items():
            if sid not in index:
                errors.append(f"{where}: gates names unknown stage {sid!r}")
            if gate not in KNOWN_GATES:
                errors.append(f"{where}: unknown gate {gate!r} (known: {', '.join(KNOWN_GATES)})")
            if sid in index and gate in KNOWN_GATES:
                if sid in skip:
                    errors.append(f"{where}: gates names {sid!r}, which the intent skips")
                elif GATE_RECORDS[gate] != stages[index[sid]]["record"]:
                    errors.append(f"{where}: gate {gate!r} reads a {GATE_RECORDS[gate]} record, but stage {sid!r} writes a {stages[index[sid]]['record']}")
        if "lenses" in intent:
            lenses = intent["lenses"]
            if not isinstance(lenses, list) or not lenses or not all(isinstance(x, str) for x in lenses):
                errors.append(f"{where}: lenses must be a non-empty list")
            else:
                for lens in lenses:
                    if lens not in KNOWN_LENSES:
                        errors.append(f"{where}: unknown lens {lens!r} (known: {', '.join(KNOWN_LENSES)})")
        if len(errors) > before:
            continue  # the reads cannot be judged from an intent that is malformed
        available = input_names | set(supplies)
        for label, row in rows:
            listed = row.get("stages")
            if not isinstance(listed, list):
                continue  # a malformed row is reported as such elsewhere
            seen: list[str] = []
            for sid in (s for s in listed if isinstance(s, str) and s in index and s not in skip):
                for name, optional in _read_names(stages[index[sid]]):
                    if not optional and name not in available and name not in seen:
                        errors.append(
                            f"{where}, row '{label}': stage '{sid}' reads '{name}', which no stage left in the row produces "
                            "and the intent does not supply"
                        )
                seen.append(sid)


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

    @property
    def commands(self) -> dict[str, list[str]]:
        """The repo's [commands] as {class: [command prefixes]}, only those that are set."""
        raw = (self.config or {}).get("commands")
        found: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for name in CONFIG_COMMANDS:
                value = raw.get(name)
                prefixes = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
                prefixes = [p.strip() for p in prefixes if isinstance(p, str) and p.strip()]
                if prefixes:
                    found[name] = prefixes
        return found

    @property
    def command_timeout(self) -> int:
        """The seconds each declared command may run under `gate`: `timeout` under [commands], else 900."""
        raw = (self.config or {}).get("commands")
        value = raw.get("timeout") if isinstance(raw, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else DEFAULT_COMMAND_TIMEOUT

    @property
    def roles(self) -> dict:
        """The role policies: the pipeline's own, or the shipped default's for a pipeline that has none."""
        roles = (self.pipeline or {}).get("roles")
        return roles if isinstance(roles, dict) and roles else default_roles()


@functools.lru_cache(maxsize=None)
def default_roles() -> dict:
    try:
        with open(PIPELINE_DIR / f"{DEFAULT_PIPELINE}.toml", "rb") as handle:
            roles = tomllib.load(handle).get("roles")
    except (OSError, ValueError):
        roles = None
    return roles if isinstance(roles, dict) else {}


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


def _git(root: Path, *args: str, timeout: int = 120, index: Path | None = None) -> subprocess.CompletedProcess:
    """Run git in `root` without optional locks, so that a read does not take the index lock a commit needs. With
    `index`, git works on that copy of the index. `git diff` still refreshes and rewrites the index it reads
    (--no-optional-locks does not reach it), so the commands that run `git diff` work on a copy."""
    env = {**os.environ, "GIT_INDEX_FILE": str(index)} if index is not None else None
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, timeout=timeout, env=env)
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
            ["git", "--no-optional-locks", "-C", str(path), "rev-parse", "--show-toplevel"],
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


# The change, as one hash. `diff_sha256` in the verify and review records is the sha256 of what
# `git diff-tree --raw` lists between the tree of the run's merge base and the tree of the files as they
# are (untracked files included, .plumbline/ left out): the mode, blob id and path of each changed file.
# It is content-addressed, so a change hashes the same before and after it is committed, and it differs
# as soon as any file of the change differs.

HIDE_RUNS = ":(exclude).plumbline"


def _git_index(root: Path, index: Path, *args: str, timeout: int = 120) -> str:
    """Run git on the copy of the index at `index`; stdout as text."""
    result = _git(root, *args, timeout=timeout, index=index)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit status {result.returncode}"
        raise PlumblineError(f"git {' '.join(args[:2])} failed: {detail}")
    return result.stdout.decode("utf-8", "surrogateescape")


@contextlib.contextmanager
def index_copy(root: Path):
    """A private copy of the repository's index, as a path, gone when the block ends. The copy keeps the index's own
    timestamp (git's check for an edit made within the second of the last index write compares it). Whatever git
    refreshes or writes goes to the copy, so that measuring a change leaves the real index as it was."""
    named = _git_text(root, "rev-parse", "--git-path", "index").strip()
    source = Path(named) if os.path.isabs(named) else root / named
    with tempfile.TemporaryDirectory(prefix="plumbline-") as scratch:
        index = Path(scratch) / "index"
        try:
            shutil.copy2(source, index)
        except FileNotFoundError:
            pass  # no index yet: start from an empty one
        yield index


def nested_repositories(root: Path) -> list[str]:
    """The untracked directories that are repositories of their own (git lists them with a trailing slash). They are
    no part of the change: `classify` skips them, and so does the hash. `git add -A` would fail on one without a commit."""
    listing = _git_text(root, "ls-files", "--others", "--exclude-standard", "-z")
    return [name for name in listing.split("\0") if name.endswith("/")]


def worktree_tree(root: Path) -> str:
    """The tree a commit of everything would take: the tracked files as they are now, and the untracked
    files that are not ignored (except nested repositories). Nothing is staged: the work is done in a copy of the
    index. Every tracked file is read again besides, so that no timestamp can hide an edit: a file of the
    same size rewritten within the second of the last index write looks unchanged to `git add` alone. Starting
    from the real index, not an empty one, keeps the files that are tracked although .gitignore names them."""
    skipped = [f":(exclude,literal){name}" for name in nested_repositories(root)]
    with index_copy(root) as index:
        _git_index(root, index, "add", "-A", *(["--", ".", *skipped] if skipped else []))  # the untracked files, the deletions, and the edits that show in the stat data
        _git_index(root, index, "add", "--renormalize", "-u")  # and every tracked file, read again
        _git_index(root, index, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".plumbline")  # never part of the change
        return _git_index(root, index, "write-tree").strip()


def head_tree(root: Path) -> str:
    return _git_text(root, "rev-parse", "--verify", "HEAD^{tree}").strip()


def change_hash(root: Path, merge_base: str, tree: str | None = None) -> str:
    """The sha256 of the change from `merge_base` to `tree` (the files as they are, by default)."""
    if tree is None:
        tree = worktree_tree(root)
    result = _git(root, "diff-tree", "-r", "--raw", "--full-index", "--no-renames", "--no-ext-diff", "-z", merge_base, tree, "--", ".", HIDE_RUNS)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit status {result.returncode}"
        raise PlumblineError(f"cannot hash the change from {merge_base[:12]}: {detail}")
    return hashlib.sha256(result.stdout).hexdigest()


def change_diff_lines(root: Path, merge_base: str, tree: str) -> list[str]:
    """The lines of the change from `merge_base` to `tree` as the diff shows them, without the diff's own
    marks: the text of each added, removed and context line, and nothing of the headers. A quote that is in the change
    is in this text."""
    result = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U3", merge_base, tree, "--", ".", HIDE_RUNS)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit status {result.returncode}"
        raise PlumblineError(f"cannot read the change from {merge_base[:12]}: {detail}")
    lines: list[str] = []
    in_hunk = False
    for raw in result.stdout.decode("utf-8", "replace").split("\n"):  # not splitlines(): a form feed inside a source line is no line break here
        if raw.startswith("diff --git "):
            in_hunk = False
        elif raw.startswith("@@"):
            in_hunk = True
        elif in_hunk and raw[:1] in ("+", "-", " "):
            lines.append(raw[1:])
    return lines


def resolve_base(root: Path, base: str | None) -> tuple[str, str, list[str]]:
    """(the base ref, `git merge-base <base> HEAD`, notes): the base as given, or the default one."""
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
    return base, merge_base, notes


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


def row_labels(pipeline: dict) -> list[str]:
    """Every row label of the pipeline: `docs` for a flat row, `code.S`, `code.M` and `code.L` for a split one."""
    labels = []
    for type_id, row in pipeline["matrix"].items():
        labels += [type_id] if "stages" in row else [f"{type_id}.{size}" for size in SIZE_LABELS if size in row]
    return labels


# ---------------------------------------------------------------- intents
#
# The row selects stages; the intent then removes stages (`skip`), or supplies the
# record a removed stage would have written (`supplies`), replaces the gate of a
# stage (`gates`) and adds lenses to the review of the diff (`lenses`, unioned with the row's).


def intent_ids(pipeline: dict) -> list[str]:
    ids = list(pipeline.get("intent") or {})
    return ids if DEFAULT_INTENT in ids else [DEFAULT_INTENT, *ids]


def intent_table(pipeline: dict, intent_id: str) -> dict:
    """The [intent.<id>] table. `feature` is the default and skips nothing, even when the pipeline defines no intents."""
    intents = pipeline.get("intent") or {}
    if intent_id in intents:
        return intents[intent_id]
    if intent_id == DEFAULT_INTENT:
        return {}
    raise PlumblineError(f"unknown intent {intent_id!r}; the intents are {', '.join(intent_ids(pipeline))}")


@dataclass
class EffectiveRow:
    """A row with an intent applied: the stages that run, the records that are supplied instead, and what is replaced."""

    label: str
    intent: str
    stages: list[str]  # stage ids, in definition order
    supplied: list[str]  # stage ids whose records the intent supplies
    lenses: list[str] | None  # for the review of the diff
    gates: dict[str, str]  # stage id -> the gate that replaces its own
    note: str | None
    on_fail: dict[str, str] = field(default_factory=dict)  # stage id -> where a failed gate sends the run in this row, in place of the stage's own on_fail


def effective_row(pipeline: dict, label: str, intent_id: str = DEFAULT_INTENT) -> EffectiveRow:
    intent = intent_table(pipeline, intent_id)
    if label not in row_labels(pipeline):  # `code` alone is no row when code is split by size
        raise PlumblineError(f"{label!r} is not a row of this pipeline (the rows are {', '.join(row_labels(pipeline))})")
    row = find_row(pipeline, label)
    skip = set(intent.get("skip", []))
    lenses = list(row.get("lenses") or [])  # the intent's lenses add to the row's: a refactor of a workflow file keeps the security lens
    lenses += [lens for lens in intent.get("lenses") or [] if lens not in lenses]
    on_fail = row.get("on_fail")
    return EffectiveRow(
        label=label,
        intent=intent_id,
        stages=[sid for sid in row["stages"] if sid not in skip],
        supplied=list(intent.get("supplies", [])),
        lenses=lenses or None,
        gates=dict(intent.get("gates", {})),
        note=row.get("note"),
        on_fail={sid: target for sid, target in on_fail.items() if isinstance(target, str)} if isinstance(on_fail, dict) else {},
    )


def effective_stage(stage: dict, effective: EffectiveRow) -> dict:
    """The stage as this run has it: a copy, with the intent's gate in place of its own, and the row's on_fail in place of its own."""
    stage = dict(stage)
    if stage["id"] in effective.gates:
        stage["gate"] = effective.gates[stage["id"]]
    if stage["id"] in effective.on_fail:
        stage["on_fail"] = effective.on_fail[stage["id"]]
    return stage


def classify(root: Path, pipeline: dict, base: str | None = None, intent: str = DEFAULT_INTENT, row: str | None = None) -> dict:
    """The change_class record for the work tree at `root`: the diff from
    `git merge-base <base> HEAD` to the working tree, plus untracked files that
    are not ignored (counted as added lines). The record carries the `intent`.
    A change that does not exist yet has nothing to measure: `row` then declares
    the row, and the record says so."""
    intent_table(pipeline, intent)  # an unknown intent is refused before anything is measured
    if row is not None and row not in row_labels(pipeline):
        raise PlumblineError(f"{row!r} is not a row of this pipeline (the rows are {', '.join(row_labels(pipeline))})")
    head_result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head_result.returncode != 0:
        raise PlumblineError("the repository has no commits yet; there is nothing to compare against")
    head = head_result.stdout.decode().strip()

    base, merge_base, notes = resolve_base(root, base)

    entries: dict[str, dict] = {}
    with index_copy(root) as index:  # `git diff` refreshes the index it reads: on a copy, the real one stays as it was
        raw_text = _git_index(root, index, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", merge_base, "--")
        numstat = _git_index(root, index, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", merge_base, "--")
    raw = raw_text.split("\0")
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

    if not entries and row is None:
        raise NothingToClassify(
            f"no changes between the merge base of '{base}' and the working tree; there is nothing to classify "
            "(a change that does not exist yet needs its row declared: --row ROW)"
        )

    files = []
    for path in sorted(entries):
        entry = entries[path]
        files.append(
            {
                "path": path,
                "type": file_type(pipeline, path),
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
    if row is None:
        chosen = row_label(pipeline, types[0], size)
    else:
        chosen = row
        type_id, _, declared_size = row.partition(".")
        if not files:
            types, size = [type_id], declared_size or "S"
            notes.append(f"nothing has changed yet, so the row {row} was declared, not measured")
        elif row_label(pipeline, types[0], size) != row:
            notes.append(f"the row {row} was declared; the change measures as {row_label(pipeline, types[0], size)}")
    return {
        "base": base,
        "head": head,
        "merge_base": merge_base,
        "files": files,
        "types": types,
        "lines": lines,
        "size": size,
        "row": chosen,
        "intent": intent,
        "symlinks": [f["path"] for f in files if f["symlink"]],
        "notes": notes,
    }


# ------------------------------------------------------------------ plan


def default_run_id(root: Path) -> str:
    short = _git_text(root, "rev-parse", "--short", "HEAD").strip()
    return f"{short}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def build_plan(pipeline: dict, record: dict, run_id: str, commands: dict | None = None, graft: bool = False) -> dict:
    """The stages to run for a classified change, in order, with every read
    resolved to a record path and every on_fail resolved within the row. The
    intake record's intent is applied to the row: skipped stages are left out,
    the records it supplies count as present, and its gates and lenses replace
    the stages' own."""
    intent = record.get("intent", DEFAULT_INTENT)
    effective = effective_row(pipeline, record["row"], intent)
    by_id = {s["id"]: s for s in pipeline["stage"]}
    in_row = list(effective.stages)
    present = set(in_row) | set(effective.supplied)
    record_dir = f"{RUNS_DIR}/{run_id}"

    stages = []
    for sid in in_row:
        stage = effective_stage(by_id[sid], effective)
        kind = stage.get("kind", "agent")
        lenses = stage.get("lenses")
        if _is_diff_review(stage) and effective.lenses:
            lenses = list(effective.lenses)
        reads = []
        for name, optional in _read_names(stage):
            is_input = name in pipeline["inputs"]
            path = None if is_input or name not in present else f"{record_dir}/{name}.json"
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
        "intent": intent,
        "row": record["row"],
        "size": record["size"],
        "lines": record["lines"],
        "types": record["types"],
        "head": record["head"],
        "base": record["base"],
        "merge_base": record["merge_base"],
        "note": effective.note,
        "record_dir": record_dir,
        "stubs_dir": f"{record_dir}/{STUBS_DIR}",
        "commands": commands or {},
        "graft": graft,
        "supplied": [
            {"stage": sid, "record": by_id[sid]["record"], "path": f"{record_dir}/{sid}.json"} for sid in effective.supplied
        ],
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
#
# The commands the verifier and the test-writer run, and that `plumbline.py gate` runs itself from the repository
# root (each a command prefix, or a list of prefixes; `gate` runs the first). The verify stage needs `test`: without it
# the gate has nothing to run. A pytest command also gets a junit report, which keeps the count of tests from shrinking.
# [commands]
# test = "python3 -m pytest"
# lint = "ruff check"
# typecheck = "mypy ."
# build = "make build"
# timeout = 900               # seconds each command may run under `gate`
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
    "survivors": "review_record",
    "lens": "findings_record",
    "defender": "defense_record",
    "run_id": "pass_record",
    "stages_skipped": "override_record",
}


def infer_record_type(data) -> str:
    """The record type, from a key only that type has. A gaps_record has only
    `gaps`, which a review_record has too, so it is what is left when no
    review_record key is there."""
    if isinstance(data, dict):
        hits = {MARKERS[key] for key in data if key in MARKERS}
        if len(hits) == 1:
            return hits.pop()
        if not hits and "gaps" in data:
            return "gaps_record"
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


def _change_line(d) -> list[str]:
    """The change a record covers, as the hash `check-diff` prints."""
    return ["", f"Change (diff_sha256): `{_s(_get(d, 'diff_sha256'))}`"]


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
    out += ["", f"- Intent: `{_s(d.get('intent'))}`", f"- Base: `{_s(d.get('base'))}`, merge base `{_short(d.get('merge_base'))}`", f"- Head: `{_short(d.get('head'))}`"]
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
    out = [f"# Verify: {'green' if d.get('green') is True else 'not green'}", *_change_line(d), "", "## Commands", ""]
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


def _finding_lines(f) -> list[str]:
    lines = [
        f"- **{_s(_get(f, 'id'))}** [{_s(_get(f, 'severity'))}] ({_s(_get(f, 'lens'))}) `{_s(_get(f, 'file'))}:{_s(_get(f, 'line'))}` {_s(_get(f, 'claim'))}",
        f"  - Failure scenario: {_s(_get(f, 'failure_scenario'))}",
        f"  - Rule: {_s(_get(f, 'rule'))}",
        f"  - Evidence: `{_s(_get(f, 'evidence'))}`",
    ]
    if _get(f, "outside_code") is not None:
        lines.append(f"  - Outside the code: {_s(_get(f, 'outside_code'))}")
    if _get(f, "evidence_unverified") is True:
        lines.append("  - The evidence was found in neither the change nor its file (unverified).")
    if _get(f, "severity_raised_from") is not None:
        lines.append(f"  - Severity raised from {_s(_get(f, 'severity_raised_from'))} by the defenders' claims.")
    return lines


def _defense_text(x) -> str:
    claim = f" (claims {_s(_get(x, 'severity_claim'))})" if _get(x, "severity_claim") is not None else ""
    return f"{_s(_get(x, 'finding_id'))}, {_s(_get(x, 'defender'))}: {_s(_get(x, 'verdict'))}{claim}. {_s(_get(x, 'reason'))}"


def _gap_text(g) -> str:
    return (
        f"**{_s(_get(g, 'id'))}** ({_s(_get(g, 'kind'))}"
        + (f", {_s(_get(g, 'ac'))}" if _get(g, "ac") is not None else "")
        + f") {_s(_get(g, 'detail'))}"
    )


def _render_review_record(d) -> list[str]:
    out = [
        f"# Review of {_s(d.get('target'))}, round {_s(d.get('round'))}",
        "",
        f"Lenses: {', '.join(_s(x) for x in _list(d.get('lenses'))) or 'none'}. "
        f"Blockers surviving: {_s(d.get('blockers_surviving'))}.",
        *_change_line(d),
        "",
        "## Findings",
        "",
    ]
    findings = _list(d.get("findings"))
    for f in findings:
        out += _finding_lines(f)
    if not findings:
        out.append("None.")
    out += _section("Defenses", d.get("defenses"), _defense_text)
    out += _section("Survivors", d.get("survivors"))
    routes = _get(d, "routes", {})
    out += _section("Survivors for the builder", _get(routes, "builder"))
    out += _section("Survivors for the test-writer", _get(routes, "test-writer"))
    out += _section("Gaps", d.get("gaps"), _gap_text)
    return out


def _render_findings_record(d) -> list[str]:
    out = [f"# Findings through the {_s(d.get('lens'))} lens", *_change_line(d), "", "## Findings", ""]
    findings = _list(d.get("findings"))
    for f in findings:
        out += _finding_lines(f)
    if not findings:
        out.append("None.")
    return out


def _render_defense_record(d) -> list[str]:
    return [f"# Defenses by {_s(d.get('defender'))}", *_change_line(d)] + _section("Defenses", d.get("defenses"), _defense_text)


def _render_gaps_record(d) -> list[str]:
    return ["# Gaps", *_change_line(d)] + _section("Gaps", d.get("gaps"), _gap_text)


def _open_finding_text(f) -> str:
    return (
        f"**{_s(_get(f, 'stage'))}/{_s(_get(f, 'id'))}** [{_s(_get(f, 'severity'))}] ({_s(_get(f, 'lens'))}) "
        f"`{_s(_get(f, 'file'))}:{_s(_get(f, 'line'))}` {_s(_get(f, 'claim'))} Failure: {_s(_get(f, 'failure_scenario'))}"
    )


def _open_gap_text(g) -> str:
    return (
        f"**{_s(_get(g, 'stage'))}/{_s(_get(g, 'id'))}** ({_s(_get(g, 'kind'))}"
        + (f", {_s(_get(g, 'ac'))}" if _get(g, "ac") is not None else "")
        + f") {_s(_get(g, 'detail'))}"
    )


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
            [
                [m, f"at least {_get(u, 'output')}" if _get(u, "output_lower_bound") else _get(u, "output"), _get(u, "fresh_input"), _get(u, "cache_read")]
                for m, u in by_model.items()
            ],
            align={1, 2, 3},
        )
    else:
        out.append("None recorded.")
    out += _section("Notes", d.get("notes"))
    if "open_findings" in d:  # a pass record from before 0.5.0 has neither list
        out += _section("Open findings", d.get("open_findings"), _open_finding_text)
    if "gaps" in d:
        out += _section("Gaps", d.get("gaps"), _open_gap_text)
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
    "findings_record": _render_findings_record,
    "defense_record": _render_defense_record,
    "gaps_record": _render_gaps_record,
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


# ----------------------------------------------------- runs and the ledger
#
# A run is a directory, .plumbline/runs/<run-id>/. It holds one <stage-id>.json
# per stage, the per-agent records of a review unit under
# <stage-id>/round-<n>/, and ledger.jsonl, which only ever grows: a line for
# each plumbline agent that stopped, and again each time its record or the
# record's validity changed (written by the SubagentStop hook), and a line for
# each gate evaluation (written by `gate` and `pass`).


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PlumblineError(f"run id {run_id!r} must be letters, digits, '.', '_' or '-', and start with a letter or digit")
    if run_id.lower() == ACTIVE_FILE.lower():  # the file that names the active run sits beside the run directories
        raise PlumblineError(f"run id {run_id!r} is taken by the file {RUNS_DIR}/{ACTIVE_FILE}; pick another --run-id")
    return run_id


def run_dir(root: Path, run_id: str) -> Path:
    return root / RUNS_DIR / check_run_id(run_id)


def existing_run_dir(root: Path, run_id: str) -> Path:
    path = run_dir(root, run_id)
    if not path.is_dir():
        raise PlumblineError(f"there is no run '{run_id}' in {root} (expected the directory {RUNS_DIR}/{run_id}/)")
    return path


def rel_path(root: Path, path: Path) -> str:
    """`path` relative to `root`, with forward slashes; as given when it lies outside."""
    for base, candidate in ((root, path), (root.resolve(), path.resolve())):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def load_json_file(path: Path):
    """(data, problem): the parsed JSON, or None and a one-line problem."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "no such file"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read: {exc}"
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"


def json_text(data) -> str:
    """`data` as the text a record file holds."""
    return json.dumps(data, indent=2) + "\n"


def write_json_atomic(path: Path, data) -> None:
    """Write `data` as JSON so that a reader sees the old file or the whole new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json_text(data), encoding="utf-8")
    os.replace(temp, path)


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / LEDGER_FILE


def append_ledger(root: Path, run_id: str, entry: dict) -> None:
    """Append one line to the run's ledger, in a single write."""
    path = ledger_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": utc_now(), **entry}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_ledger(root: Path, run_id: str) -> list[dict]:
    """The ledger's entries in order; a line that is not a JSON object is skipped."""
    try:
        text = ledger_path(root, run_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def latest_entry(ledger: list[dict], kind: str, **fields) -> dict | None:
    """The last entry of the ledger that has this kind and these fields."""
    for entry in reversed(ledger):
        if entry.get("kind") == kind and all(entry.get(key) == value for key, value in fields.items()):
            return entry
    return None


def latest_run_id(root: Path) -> str | None:
    """The run touched most recently: the newest file in a run directory wins."""
    best: tuple[float, str] | None = None
    try:
        candidates = [d for d in (root / RUNS_DIR).iterdir() if d.is_dir() and RUN_ID_PATTERN.fullmatch(d.name)]
    except OSError:
        return None
    for directory in candidates:
        try:
            newest = max([directory.stat().st_mtime] + [child.stat().st_mtime for child in directory.iterdir()])
        except OSError:
            continue
        if best is None or (newest, directory.name) > best:
            best = (newest, directory.name)
    return best[1] if best else None


def active_run_id(root: Path) -> str | None:
    """The run in progress: the one .plumbline/runs/ACTIVE names (`plan --intent` writes it), when that run's directory
    exists; otherwise the newest run. The hooks read the same file: the run id, and a newline."""
    try:
        named = (root / RUNS_DIR / ACTIVE_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        named = ""
    if RUN_ID_PATTERN.fullmatch(named) and (root / RUNS_DIR / named).is_dir():
        return named
    return latest_run_id(root)


def write_active(root: Path, run_id: str) -> Path:
    """Name `run_id` as the run in progress."""
    path = root / RUNS_DIR / ACTIVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{ACTIVE_FILE}.tmp")
    temp.write_text(run_id + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


def head_sha(root: Path) -> str:
    result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    sha = result.stdout.decode().strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise PlumblineError("the repository has no commits yet")
    return sha


def read_stage_record(root: Path, run_id: str, stage: dict) -> tuple[dict | None, list[str]]:
    """A stage's record as (data, problems): the problems say the file is missing
    or unreadable, or list the schema errors; data is None when there are any."""
    path = run_dir(root, run_id) / f"{stage['id']}.json"
    data, problem = load_json_file(path)
    where = rel_path(root, path)
    if problem:
        return None, [f"{where}: {problem}"]
    errors = check_record(stage["record"], data)
    if errors:
        return None, [f"{where}: {error}" for error in errors]
    return data, []


@dataclass
class Run:
    """A run's row and the stages it goes through, from its intake record."""

    root: Path
    run_id: str
    row: str  # the matrix row, such as code.M
    stages: list[dict]  # the stage definitions that run, in order: the row's, less what the intent skips, with its gates
    lenses: list[str] | None  # the review lenses of the row or the intent, when either sets any
    note: str | None
    intent: str = DEFAULT_INTENT
    supplied: list[dict] = field(default_factory=list)  # the stages whose records the intent supplies instead of running them
    base: str = ""  # the intake record's base ref and merge base: the change is measured from there
    merge_base: str = ""


def intake_stage_of(pipeline: dict) -> dict:
    intake = next((s for s in pipeline["stage"] if s["record"] == "change_class"), None)
    if intake is None:
        raise PlumblineError("the pipeline has no stage that writes a change_class record")
    return intake


def load_run(project: Project, run_id: str) -> Run:
    """The run as its intake record describes it. The intake record is what `plan --intent` wrote: the ledger holds
    its hash, and a record edited since is refused, because it names the row, the intent and the merge base that
    every later check measures from."""
    existing_run_dir(project.root, run_id)
    by_id = {s["id"]: s for s in project.pipeline["stage"]}
    intake = intake_stage_of(project.pipeline)
    data, problems = read_stage_record(project.root, run_id, intake)
    if data is None:
        raise PlumblineError(f"the run's intake record is not usable: {problems[0]}")
    entry = latest_entry(read_ledger(project.root, run_id), "intake")
    if entry is None:
        raise PlumblineError("the run's intake record has no entry from `plan --intent` in the ledger; start the run with `plan --intent`")
    if entry.get("record_sha256") != file_sha256(run_dir(project.root, run_id) / f"{intake['id']}.json"):
        raise PlumblineError("the run's intake record changed after `plan --intent` wrote it (the ledger holds its hash from then); start a new run with `plan --intent`")
    try:
        effective = effective_row(project.pipeline, data["row"], data["intent"])
        stages = [effective_stage(by_id[sid], effective) for sid in effective.stages]
        supplied = [effective_stage(by_id[sid], effective) for sid in effective.supplied]
    except PlumblineError as exc:
        raise PlumblineError(f"the run's intake record does not fit this repository's pipeline: {exc}") from None
    except (KeyError, TypeError):
        raise PlumblineError(f"the run's row {data['row']!r} is not a row of this repository's pipeline") from None
    return Run(project.root, run_id, data["row"], stages, effective.lenses, effective.note, data["intent"], supplied, data["base"], data["merge_base"])


# ------------------------------------------------------------------ gates
#
# A gate is a mechanical check of a stage's record; no model is asked. Every gate
# first requires the record to exist and validate against its schema, and the
# ledger to trace it: an agent stage's record counts only with the entry of the
# agent that wrote it (the SubagentStop hook writes it, with the record's hash),
# and a review only with the entry `merge-review` wrote. The gates of the verify
# stage and of the tests stage also run the repository's own commands (see
# measure_stage), so that they rest on what happened and not on what an agent typed.


@dataclass
class GateOutcome:
    stage: str
    gate: str | None
    passed: bool
    problems: list[str]
    record: str  # the stage's record, relative to the repository root
    record_sha256: str | None
    data: dict | None = None  # the record itself, when it exists and validates
    checked: bool = False  # the record is valid and traced, and the gate's own check ran: its problems are the gate's, not the ledger's


@dataclass
class GateContext:
    """What a gate check may look at besides the record it checks."""

    project: Project
    run_id: str
    stage: dict
    run: Run | None  # None when the run has no usable intake record
    ledger: list[dict]
    measuring: bool  # this evaluation ran the repository's commands just now

    @property
    def root(self) -> Path:
        return self.project.root

    def spec(self) -> tuple[dict | None, list[str]]:
        """The spec this stage reads, as (record, problems)."""
        for name, _optional in _read_names(self.stage):
            source = next((s for s in self.project.pipeline["stage"] if s["id"] == name), None)
            if source is not None and source["record"] == "spec":
                return read_stage_record(self.root, self.run_id, source)
        return None, [f"stage '{self.stage['id']}' reads no spec, so {self.stage['gate']} has nothing to compare against"]

    def run_entry(self) -> dict | None:
        """The latest measured run of this stage's commands (see measure_stage)."""
        return latest_entry(self.ledger, "run", stage=self.stage["id"])


# ---- the spec

def single_ac_problems(intent: str | None, spec: dict) -> list[str]:
    count = len(spec["acceptance_criteria"])
    if intent in SINGLE_AC_INTENTS and count > 1:
        return [f"the intent '{intent}' reproduces one bug, so its spec has exactly one acceptance criterion (this one has {count})"]
    return []


def _gate_spec_complete(spec: dict, ctx: GateContext | None) -> list[str]:
    criteria = spec["acceptance_criteria"]
    ids = [ac["id"] for ac in criteria]
    planned = [entry["ac"] for entry in spec["test_plan"]]
    problems = [] if criteria else ["the spec has no acceptance criteria"]
    problems += [f"{ac} is defined more than once" for ac in dict.fromkeys(ids) if ids.count(ac) > 1]
    problems += [f"{ac} has no test_plan entry" for ac in dict.fromkeys(ids) if ac not in planned]
    problems += [f"the test_plan has an entry for {ac}, which the spec does not define" for ac in dict.fromkeys(planned) if ac not in ids]
    if ctx is not None and ctx.run is not None:
        problems += single_ac_problems(ctx.run.intent, spec)
    return problems


# ---- where a problem comes from
#
# The gates of the tests and verify stages have two parts: what the agent's record says (typed), and what `gate` saw when it
# ran the repository's commands (measured). Each problem they report starts by saying which part it comes from, so that a
# failure can be read without the record and the ledger side by side.

def _typed(problems: list[str]) -> list[str]:
    """Problems found in the agent's record: what it says, and what it says against the spec and the files it names."""
    return [f"the agent's record: {problem}" for problem in problems]


def _measured(problems: list[str]) -> list[str]:
    """Problems found by the measured run: what `gate` saw when it ran the repository's commands on the change."""
    return [f"the measured run: {problem}" for problem in problems]


# ---- the tests

def _test_names(name: str) -> list[str]:
    """The strings a test's name may appear as in its file: as given, without a parametrised suffix, and its last `::` part."""
    plain = re.sub(r"\[[^\]]*\]$", "", name)
    return list(dict.fromkeys([name, plain, plain.rsplit("::", 1)[-1]]))


def _test_file_problem(project: Project, test: dict) -> str | None:
    """Why the file a test entry names is no test of this repository, or None: it must exist inside the repository,
    be of the tests type, and hold the test's name."""
    root = project.root
    name = test["file"]
    where = f"test {test['id']}"
    relative = os.path.normpath(name)
    if os.path.isabs(name) or relative == ".." or relative.startswith(".." + os.sep):
        return f"{where}: the file {name} is outside the repository"
    path = root / relative
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        return f"{where}: the file {name} is outside the repository"
    if not path.is_file():
        return f"{where}: the file {name} does not exist"
    kind = file_type(project.pipeline, Path(relative).as_posix())
    if kind != TESTS_TYPE:
        return f"{where}: the file {name} is a {kind} file, not a {TESTS_TYPE} file"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"{where}: the file {name} cannot be read ({exc})"
    if not any(candidate in text for candidate in _test_names(test["name"])):
        return f"{where}: the file {name} has no test named '{test['name']}'"
    return None


def _acs_covered_problems(tests: dict, ctx: GateContext) -> list[str]:
    """Every test's file exists, is of the tests type and holds the test's name; every criterion a test claims is one of
    the spec's; every criterion of the spec is covered by such a test."""
    spec, problems = ctx.spec()
    if spec is None:
        return problems
    known = [ac["id"] for ac in spec["acceptance_criteria"]]
    covered: set[str] = set()
    for test in tests["tests"]:
        problem = _test_file_problem(ctx.project, test)
        if problem:
            problems.append(problem)
        problems += [f"test {test['id']} claims {ac}, which the spec does not have" for ac in test["ac_ids"] if ac not in known]
        if problem is None:
            covered.update(test["ac_ids"])
    problems += [f"{ac} is covered by no test" for ac in dict.fromkeys(known) if ac not in covered]
    return problems


def _gate_acs_covered(tests: dict, ctx: GateContext) -> list[str]:
    return _acs_covered_problems(tests, ctx)


def is_pytest(command: str) -> bool:
    """Does the command run pytest: a `pytest` program, `python -m pytest`, `uv run ... pytest`?"""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(os.path.basename(word) in ("pytest", "py.test") or (word == "-m" and words[i + 1 : i + 2] == ["pytest"]) for i, word in enumerate(words))


def _junit_of(entry: dict | None) -> dict | None:
    """The junit counts {tests, failures, errors, skipped} of a measured run, when its test command was pytest."""
    for command in (entry or {}).get("commands", []):
        if isinstance(command.get("junit"), dict):
            return command["junit"]
    return None


def _run_entry_problems(entry: dict) -> list[str]:
    """What a measured run shows apart from the commands' exit codes: a command that timed out, and any guarded file
    that changed while the commands ran."""
    problems = [
        f"the {c['name']} command timed out after {entry.get('timeout', DEFAULT_COMMAND_TIMEOUT)} s ({c['cmd']}); `timeout` under [commands] in plumbline.toml allows more"
        for c in entry.get("commands", [])
        if c.get("timed_out")
    ]
    before, after = entry.get("guarded_before") or {}, entry.get("guarded_after") or {}
    problems += [f"the test run changed {name}" for name in sorted({*before, *after}) if before.get(name) != after.get(name)]
    return problems


def tests_revision(run: Run | None, stage_id: str, ledger: list[dict]) -> bool:
    """Is the tests stage being run again after the build began, having passed its gate before it? Then the code exists,
    so the tests are not expected to fail: the run is recorded, and the tests must still cover the spec and hold the
    inventory. A tests stage that never passed before the build gets no such allowance."""
    if run is None:
        return False
    builders = {s["id"] for s in run.stages if s.get("role") == "builder"}
    passed_before = False
    for entry in ledger:
        if entry.get("kind") == "gate" and entry.get("stage") == stage_id and entry.get("passed") is True:
            passed_before = True
        elif entry.get("kind") == "agent" and entry.get("stage") in builders:
            return passed_before
    return False


def _measured_tests_problems(ctx: GateContext, revision: bool) -> list[str]:
    """The tests stage's own run of the repository's test command: before the code exists it must fail. A pytest
    command fails with exit status 1; 2 is a collection or usage error, and 5 collected no tests: neither is a test that
    fails. Another command fails with any status but 0, apart from 126 and 127, which say it did not run at all."""
    entry = ctx.run_entry()
    if entry is None:
        return [f"the test command has not been run: `plumbline.py gate {ctx.run_id} {ctx.stage['id']}` runs the repository's test command itself"]
    problems = _run_entry_problems(entry)
    if revision:
        return problems
    test = next((c for c in entry["commands"] if c["name"] == "test"), None)
    if test is None:
        return problems + ["no test command was run"]
    code, command = test["exit_code"], test["cmd"]
    if test.get("timed_out"):
        return problems
    if code in (126, 127):
        problems.append(f"the test command could not run (exit status {code}): check `{command}` under [commands] in plumbline.toml")
    elif is_pytest(command):
        if code == 0:
            problems.append("the test command exited 0: every test passed, so none of them fails without the change it tests")
        elif code == 2:
            problems.append(
                "the test command exited 2: pytest could not collect the tests (an import or syntax error), and a test that cannot run reproduces nothing; "
                "import what a test needs from the change inside the test function, so that a missing name fails that test when it runs"
            )
        elif code == 5:
            problems.append("the test command exited 5: pytest collected no tests")
        elif code != 1:
            problems.append(f"the test command exited {code}: pytest did not report failing tests (a failing test exits 1)")
    elif code == 0:
        problems.append("the test command exited 0: every test passed, so none of them fails without the change it tests")
    return problems


def _stub_problems(tests: dict, ctx: GateContext, on_stubs: bool) -> list[str]:
    """The tests stage's checks in one place: coverage, at least one test, what the test-writer says its run showed, and
    what the measured run showed. The tests run against today's code (a change to modules that exist imports its new names
    inside the tests, so each test fails when it runs) or, for a brand-new module, against its stubs; a fix runs against
    today's code and must fail on an assertion. Each problem says which part it comes from: the agent's record or the measured run."""
    problems = _acs_covered_problems(tests, ctx)
    if not tests["tests"]:
        problems.append("there are no tests")
    revision = tests_revision(ctx.run, ctx.stage["id"], ctx.ledger)
    stub = tests["stub_check"]
    if not revision:  # once the code exists, the test-writer's own stub check is about a run that is over
        if not stub["ran"]:
            problems.append("the stub check did not run" if on_stubs else "the new tests were not run against today's code")
        elif not stub["all_failed_on_assertions"]:
            problems.append(
                "stub_check says not every test failed when it ran (a test that passed, or that could not be collected, shows nothing)"
                if on_stubs
                else "stub_check says not every new test fails on an assertion against today's code (one that passes there, or fails on an import or syntax error, reproduces nothing)"
            )
    return _typed(problems) + _measured(_measured_tests_problems(ctx, revision))


def _gate_tests_fail_on_stub(tests: dict, ctx: GateContext) -> list[str]:
    """The gate of the tests stage: the tests cover the spec, and the repository's test command, run by `gate`, fails on
    the stubs. The test-writer's typed stub check must agree."""
    return _stub_problems(tests, ctx, True)


def _gate_reproduces_on_head(tests: dict, ctx: GateContext) -> list[str]:
    """The gate of a fix's tests, in place of `tests_fail_on_stub`: the new tests must fail on today's code, and on an
    assertion. The code is HEAD's own, so there are no stubs. It measures the same run and keeps the coverage rule."""
    return _stub_problems(tests, ctx, False)


# ---- the change is verified

def _typed_verify_problems(verify: dict) -> list[str]:
    """What the verifier's record says about itself: green must be what its commands, checks and tests add up to, and when it
    is not green, every reason is listed."""
    failing = [entry["ac"] for entry in verify["failing_acs"]]
    bad_commands = [c for c in verify["commands"] if c["exit_code"] != 0]
    bad_checks = [name for name, value in verify["checks"].items() if value is False]
    failed = verify["tests"]["failed"]
    if not verify["green"]:
        problems = ["the verify record is not green" + (f" (failing: {', '.join(failing)})" if failing else "")]
        problems += [f"the record's {c['name']} command exited {c['exit_code']}" for c in bad_commands]
        problems += [f"the record's check {name} is false" for name in bad_checks]
        if failed:
            problems.append(f"the record shows {failed} failed test{'' if failed == 1 else 's'}")
        return problems
    problems = []
    if not verify["commands"]:
        problems.append("the verify record lists no commands, so nothing shows it verified anything")
    if failing:
        problems.append(f"green is true, but failing_acs lists {', '.join(failing)}")
    if failed:
        problems.append(f"green is true, but tests.failed is {failed}")
    problems += [f"green is true, but the {c['name']} command's exit_code is {c['exit_code']}" for c in bad_commands]
    problems += [f"green is true, but checks.{name} is false" for name in bad_checks]
    return problems


def changed_files(project: Project, run: Run) -> tuple[dict | None, list[str]]:
    """The change from the run's merge base to the files as they are, classified: (the change_class record, problems).
    The record is None when nothing changed."""
    try:
        return classify(project.root, project.pipeline, run.merge_base, run.intent), []
    except NothingToClassify:
        return None, []
    except PlumblineError as exc:
        return None, [str(exc)]


def tests_touched_problems(project: Project, run: Run | None, measured: dict | None = None) -> list[str]:
    """Under an intent that leaves the tests as they are (a refactor), the change touches no test file."""
    if run is None or run.intent not in UNCHANGED_TESTS_INTENTS:
        return []
    record, problems = (measured, []) if measured is not None else changed_files(project, run)
    if record is None:
        return problems
    files = [f["path"] for f in record["files"] if f["type"] == TESTS_TYPE]
    if files:
        problems.append(f"the intent {run.intent} leaves the tests as they are, but the change touches {', '.join(files)}")
    return problems


def _inventory_problems(ctx: GateContext, entry: dict) -> list[str]:
    """The tests the verify run ran must be at least those the tests stage's run ran, and no more of them skipped: the build
    cannot make a failing test go away by deleting it or marking it skipped. Only pytest commands report an inventory."""
    tests_stage = next((s for s in ctx.run.stages if s.get("gate") in MEASURED_TESTS_GATES), None) if ctx.run is not None else None
    if tests_stage is None:
        return []
    before, now = _junit_of(latest_entry(ctx.ledger, "run", stage=tests_stage["id"])), _junit_of(entry)
    if before is None or now is None:
        return []
    problems = []
    if now["tests"] < before["tests"]:
        problems.append(f"the verify run ran {now['tests']} tests, fewer than the {before['tests']} the tests stage's run ran: a test went missing")
    if now["skipped"] > before["skipped"]:
        problems.append(f"the verify run skipped {now['skipped']} tests, more than the {before['skipped']} the tests stage's run skipped: a test was marked skipped")
    return problems


def _measured_verify_problems(verify: dict, ctx: GateContext) -> list[str]:
    entry = ctx.run_entry()
    if entry is None:
        return [f"the repository's commands have not been run: `plumbline.py gate {ctx.run_id} {ctx.stage['id']}` runs them itself"]
    problems = _run_entry_problems(entry)
    commands = entry["commands"]
    if not commands:
        problems.append("no commands were run")
    problems += [f"the {c['name']} command exited {c['exit_code']} ({c['cmd']})" for c in commands if c["exit_code"] != 0 and not c.get("timed_out")]
    if entry.get("diff_sha256") != verify["diff_sha256"]:
        problems.append(
            f"the record covers the change {verify['diff_sha256'][:12]}, but the commands ran on the change {str(entry.get('diff_sha256'))[:12]}; "
            "the change was edited after the verifier's record, so run the verifier again"
        )
    for name in CONFIG_COMMANDS:
        declared = ctx.project.commands.get(name)
        if declared and not any(c["name"] == name and c["cmd"] == declared[0] for c in commands):
            problems.append(f"the {name} command is declared as `{declared[0]}`, but it was not run: evaluate the gate again")
    return problems + _inventory_problems(ctx, entry)


def _gate_verify_green(verify: dict, ctx: GateContext) -> list[str]:
    """The verifier's record says green and agrees with itself; the repository's own commands, run by `gate`, all exited 0 on
    the change the record covers; no guarded file changed while they ran; the inventory of tests did not shrink. Each problem says
    which part it comes from: the agent's record or the measured run."""
    return _typed(_typed_verify_problems(verify)) + _measured(_measured_verify_problems(verify, ctx) + tests_touched_problems(ctx.project, ctx.run))


# ---- the review

def standing_blockers(review: dict) -> list[str]:
    """The findings of a review record that survive and are BLOCKING, by id: recomputed from the findings and the survivors, so that
    the count the record states is not taken at its word."""
    blocking = {f["id"] for f in review["findings"] if f["severity"] == "BLOCKING"}
    return [fid for fid in dict.fromkeys(review["survivors"]) if fid in blocking]


def _gate_no_surviving_blockers(review: dict, ctx: GateContext) -> list[str]:
    """The count of standing blockers is recomputed from the findings and the survivors, not taken from the record."""
    ids = {f["id"] for f in review["findings"]}
    survivors = list(dict.fromkeys(review["survivors"]))
    standing = standing_blockers(review)
    problems = [f"survivors names '{fid}', which is no finding of this round" for fid in survivors if fid not in ids]
    if review["blockers_surviving"] != len(standing):
        problems.append(f"blockers_surviving says {review['blockers_surviving']}, but the findings and survivors give {len(standing)}")
    if standing:
        problems.append(f"{len(standing)} blocker(s) survive: {', '.join(standing)}")
    if ctx.measuring and ctx.run is not None and _is_diff_review(ctx.stage):
        current = change_hash(ctx.root, ctx.run.merge_base)
        if review["diff_sha256"] != current:
            problems.append(
                f"the record covers the change {review['diff_sha256'][:12]}, but the change hashes to {current[:12]} now; "
                "the change was edited after this review, so run the review again"
            )
    return problems


GATE_CHECKS = {
    "spec_complete": _gate_spec_complete,
    "acs_covered": _gate_acs_covered,
    "tests_fail_on_stub": _gate_tests_fail_on_stub,
    "reproduces_on_head": _gate_reproduces_on_head,
    "verify_green": _gate_verify_green,
    "no_surviving_blockers": _gate_no_surviving_blockers,
}


# ---- provenance: the ledger shows who wrote a record

PART_ROLES = {AGENT_RECORDS[role]: role for role in ("prosecutor", "defender", "detective")}  # findings_record -> prosecutor, ...


def _agent_entry_problems(entry: dict | None, role: str, sha: str | None, what: str) -> list[str]:
    """Does this ledger entry show that plumbline:<role> stopped with a valid record whose hash is `sha`?"""
    expected = AGENT_PREFIX + role
    if entry is None:
        return [f"{what} has no entry from {expected}; run the {role}"]
    if entry.get("agent_type") != expected:
        return [f"the latest entry for {what} is from {entry.get('agent_type')}, not {expected}; run the {role}"]
    if entry.get("valid") is not True:
        return [f"{expected}'s last stop left {what} invalid; run the {role} again"]
    if entry.get("record_sha256") != sha:
        return [f"{what} changed after {expected} stopped; run the {role} again"]
    return []


def _agent_provenance(root: Path, run_id: str, stage: dict, ledger: list[dict]) -> list[str]:
    entry = latest_entry(ledger, "agent", stage=stage["id"])
    sha = file_sha256(run_dir(root, run_id) / f"{stage['id']}.json")
    return _agent_entry_problems(entry, stage["role"], sha, "its record")


def _merge_provenance(root: Path, run_id: str, stage: dict, ledger: list[dict]) -> list[str]:
    """A review's record is what `merge-review` wrote (its ledger entry holds the record's hash), and each part it was
    built from is what its agent left (the agent's entry holds the part's hash)."""
    sid = stage["id"]
    entry = latest_entry(ledger, "merge", stage=sid)
    if entry is None:
        return [f"its record has no entry from merge-review; run `plumbline.py merge-review {run_id} {sid}`"]
    if entry.get("record_sha256") != file_sha256(run_dir(root, run_id) / f"{sid}.json"):
        return [f"its record changed after merge-review wrote it; run `plumbline.py merge-review {run_id} {sid}` again"]
    problems = []
    for part in entry.get("parts") if isinstance(entry.get("parts"), list) else []:
        path, sha = part.get("path"), part.get("sha256")
        if not isinstance(path, str) or file_sha256(root / path) != sha:
            problems.append(f"{path} changed after merge-review read it; run the agent and merge-review again")
            continue
        data, problem = load_json_file(root / path)
        role = PART_ROLES.get(_part_type(data)) if problem is None else None
        if role is None:
            problems.append(f"{path} is no findings, defense or gaps record")
            continue
        problems += _agent_entry_problems(latest_entry(ledger, "agent", record=path), role, sha, path)
    return problems


def _supplied_provenance(root: Path, run_id: str, stage: dict, ledger: list[dict]) -> list[str]:
    entry = latest_entry(ledger, "supplied", stage=stage["id"])
    if entry is None:
        return ["its record has no entry from `plan --intent`, which supplies it; start the run with `plan --intent`"]
    if entry.get("record_sha256") != file_sha256(run_dir(root, run_id) / f"{stage['id']}.json"):
        return ["its record changed after `plan --intent` supplied it; start a new run with the record you mean"]
    return []


def provenance_problems(root: Path, run_id: str, stage: dict, run: Run | None, ledger: list[dict]) -> list[str]:
    """What is missing from the ledger for this stage's record to count: the agent that wrote it, the merge that built it,
    or the intent that supplied it. The main session's own stages (intake, reduce) are traced elsewhere."""
    if run is not None and any(s["id"] == stage["id"] for s in run.supplied):
        return _supplied_provenance(root, run_id, stage, ledger)
    if stage.get("kind", "agent") == "review":
        return _merge_provenance(root, run_id, stage, ledger)
    if stage.get("role") in (None, "main"):
        return []
    return _agent_provenance(root, run_id, stage, ledger)


# ---- measured runs: `gate` runs the repository's commands

GUARDED_FILES = (".gitattributes", ".claude/settings.json", ".claude/settings.local.json", ".mcp.json", "CLAUDE.md", "AGENTS.md", CONFIG_FILE)


def _git_path(root: Path, name: str) -> Path:
    named = _git_text(root, "rev-parse", "--git-path", name).strip()
    return Path(named) if os.path.isabs(named) else root / named


def guarded_snapshot(project: Project) -> dict[str, str | None]:
    """The hash (None for a file that is not there) of every file a test run must leave alone: git's config and hooks, the
    files that steer git and the agents, the settings of Claude Code, and the pipeline's own configuration."""
    root = project.root
    snapshot: dict[str, str | None] = {name: file_sha256(root / name) for name in GUARDED_FILES}
    value = (project.config or {}).get("pipeline")
    if isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9_-]+", value):  # a pipeline file of the repository's own
        with contextlib.suppress(PlumblineError):
            pipeline_file = resolve_pipeline_path(value, root)
            snapshot[rel_path(root, pipeline_file)] = file_sha256(pipeline_file)
    snapshot[".git/config"] = file_sha256(_git_path(root, "config"))
    hooks = _git_path(root, "hooks")
    if hooks.is_dir():
        for path in sorted(p for p in hooks.rglob("*") if p.is_file()):
            snapshot[f".git/hooks/{path.relative_to(hooks).as_posix()}"] = file_sha256(path)
    return snapshot


def read_junit(path: Path) -> dict | None:
    """{tests, failures, errors, skipped} summed over the test suites of a junit file, or None when it cannot be read."""
    try:
        if path.stat().st_size > 8 << 20:
            return None
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [s for s in root.iter("testsuite") if s.find(".//testsuite") is None]  # the innermost suites: no test counted twice
    if not suites:
        return None
    try:
        return {key: sum(int(s.get(key) or 0) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
    except ValueError:
        return None


def run_declared_command(root: Path, command: str, timeout: int, junit: Path | None = None) -> dict:
    """Run one declared command from the repository root, as `sh -c`, with no input and its output discarded: what plumbline
    keeps is that it ran, how it ended, and how long it took. Past `timeout` seconds it is killed with everything it started.
    With `junit`, a pytest command also writes its results there."""
    full = command
    if junit is not None:
        junit.unlink(missing_ok=True)
        full = f"{command} --junitxml={shlex.quote(str(junit))}"
    started = time.monotonic()
    result: dict = {"cmd": command}
    try:
        process = subprocess.Popen(
            ["sh", "-c", full], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except OSError:
        return {**result, "exit_code": 127, "seconds": 0.0}

    def kill_group() -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)

    def interrupted(signum, _frame) -> None:  # the command is a session of its own: ending `gate` ends it too
        kill_group()
        raise SystemExit(128 + signum)

    previous = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        with contextlib.suppress(ValueError, OSError):  # only the main thread may set a handler
            previous[getattr(signal, name)] = signal.signal(getattr(signal, name), interrupted)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group()
        process.wait()
        code, result["timed_out"] = 124, True
    finally:
        for number, handler in previous.items():
            if handler is not None:  # None: a handler that was not set from Python
                signal.signal(number, handler)
    result.update(exit_code=code, seconds=round(time.monotonic() - started, 2))
    if junit is not None:
        counts = read_junit(junit)
        if counts is not None:
            result["junit"] = counts
    return result


def measure_stage(project: Project, run: Run | None, run_id: str, stage: dict) -> tuple[dict | None, list[str]]:
    """Run the repository's declared commands for a stage whose gate measures: a verify stage runs its test command and its
    lint, typecheck and build commands where it declares them; a tests stage runs the test command. Enter the run in the
    ledger: {kind: "run", stage, commands: [{name, cmd, exit_code, seconds}], diff_sha256, guarded_before, guarded_after}.
    Returns (that entry, the problems that kept the commands from running)."""
    root = project.root
    declared = project.commands
    if not declared.get("test"):
        return None, ["no test command is declared: add `test = \"...\"` under [commands] in plumbline.toml and commit it, then evaluate the gate again"]
    names = [name for name in CONFIG_COMMANDS if declared.get(name)] if stage["gate"] == "verify_green" else ["test"]
    digest = change_hash(root, run.merge_base) if run is not None else None  # the change as it is before the commands run
    before = guarded_snapshot(project)
    commands = []
    for name in names:
        junit = run_dir(root, run_id) / f"junit-{stage['id']}.xml" if name == "test" and is_pytest(declared[name][0]) else None
        commands.append({"name": name, **run_declared_command(root, declared[name][0], project.command_timeout, junit)})
    after = guarded_snapshot(project)
    entry = {
        "kind": "run", "stage": stage["id"], "gate": stage["gate"], "commands": commands, "diff_sha256": digest,
        "guarded_before": before, "guarded_after": after, "timeout": project.command_timeout,
    }
    append_ledger(root, run_id, entry)
    return entry, []


# ---- rounds

def stage_agents(ledger: list[dict], stage_id: str, since_gate_passed: bool = False) -> int:
    """The number of attempts the ledger shows for a stage. An attempt is an agent's work up to the stage's next gate: an
    agent that stops several times before the gate (the harness asks it again for a report it has not delivered) makes one
    attempt, and the same agent resumed after a failed gate makes another. A second agent before the gate, or a stop that
    names no agent, is an attempt of its own. With `since_gate_passed`, counting starts again after the gate last passed."""
    attempts = 0
    seen: set[str] = set()  # the agents already counted since the stage's last gate
    for entry in ledger:
        if entry.get("stage") != stage_id:
            continue
        if entry.get("kind") == "gate":
            seen = set()
            if since_gate_passed and entry.get("passed") is True:
                attempts = 0
        elif entry.get("kind") == "agent":
            agent_id = entry.get("agent_id")
            if not (isinstance(agent_id, str) and agent_id):
                attempts += 1
            elif agent_id not in seen:
                seen.add(agent_id)
                attempts += 1
    return attempts


def stage_round(stage: dict, data: dict | None, ledger: list[dict]) -> tuple[int, int | None]:
    """(the round this stage is in, its max_rounds). A review stage is in the round its record says. An agent stage is in the
    round of its attempts since its gate last passed (see stage_agents): an agent counts once however often it stops before
    the gate, again when it is resumed after a failed gate, and a stage that passed and is run again starts counting again."""
    limit = stage.get("max_rounds")
    if stage.get("kind", "agent") == "review":
        return (data["round"] if data is not None else 0), limit
    return stage_agents(ledger, stage["id"], since_gate_passed=True), limit


# ---- evaluating a stage

def check_all_gates_passed(project: Project, run_id: str, own_stage: dict) -> list[str]:
    """Every other stage of the run's row has a valid record, traced in the ledger (see provenance_problems) and, where it has a
    gate, passed it at its last evaluation, with its record unchanged since."""
    root = project.root
    run = load_run(project, run_id)
    ledger = read_ledger(root, run_id)
    latest: dict[str, dict] = {}
    for entry in ledger:
        if entry.get("kind") == "gate" and isinstance(entry.get("stage"), str):
            latest[entry["stage"]] = entry
    problems = []
    for stage in [*run.supplied, *run.stages]:  # what an intent supplies is part of the run: it must still hold
        if stage["id"] == own_stage["id"]:
            continue
        _data, record_problems = read_stage_record(root, run_id, stage)
        if record_problems:
            problems.append(f"stage '{stage['id']}': {record_problems[0]}")
            continue
        untraced = provenance_problems(root, run_id, stage, run, ledger)
        if untraced:
            problems += [f"stage '{stage['id']}': {p}" for p in untraced]
            continue
        gate = stage.get("gate")
        if gate is None:
            continue
        entry = latest.get(stage["id"])
        if entry is None or entry.get("gate") != gate:
            problems.append(f"stage '{stage['id']}': its gate {gate} has not been evaluated")
        elif entry.get("passed") is not True:
            problems.append(f"stage '{stage['id']}': its gate {gate} failed at its last evaluation")
        elif entry.get("record_sha256") != file_sha256(run_dir(root, run_id) / f"{stage['id']}.json"):
            problems.append(f"stage '{stage['id']}': its record changed after its gate {gate} passed; evaluate the gate again")
    return problems


MEASURED_GATES = ("verify_green", *MEASURED_TESTS_GATES)


def evaluate_stage(
    project: Project, run_id: str, stage: dict, measure: bool = False, run: Run | None = None, run_problem: str | None = None
) -> GateOutcome:
    """The stage's record must exist and validate, and the ledger must trace it; then its gate, if it has one, must pass.
    With `measure`, a gate that measures (verify_green, tests_fail_on_stub, reproduces_on_head) first runs the repository's
    commands and enters the run in the ledger; without it, such a gate reads the latest run the ledger holds."""
    root = project.root
    path = run_dir(root, run_id) / f"{stage['id']}.json"
    gate = stage.get("gate")
    data, problems = read_stage_record(root, run_id, stage)
    checked = False
    if run is None and run_problem is None:
        try:
            run = load_run(project, run_id)
        except PlumblineError as exc:
            run_problem = str(exc)
    if gate == "all_gates_passed":
        problems = check_all_gates_passed(project, run_id, stage)
    elif data is not None:
        ledger = read_ledger(root, run_id)
        untraced = provenance_problems(root, run_id, stage, run, ledger)
        if untraced:
            problems = untraced
        elif gate is not None:
            if measure and gate in MEASURED_GATES:
                if run is None and gate == "verify_green":
                    raise PlumblineError(f"{run_problem}: the change cannot be measured")
                _entry, kept_from_running = measure_stage(project, run, run_id, stage)
                problems = _measured(kept_from_running)  # the commands could not run at all: the measured part has nothing to show
                ledger = read_ledger(root, run_id)  # with the run that was just entered
            if not problems:
                problems = GATE_CHECKS[gate](data, GateContext(project, run_id, stage, run, ledger, measure))
                checked = True
    return GateOutcome(stage["id"], gate, not problems, problems, rel_path(root, path), file_sha256(path), data, checked)


def ledger_gate_entry(outcome: GateOutcome) -> dict:
    return {
        "kind": "gate",
        "stage": outcome.stage,
        "gate": outcome.gate,
        "passed": outcome.passed,
        "record_sha256": outcome.record_sha256,
        "problems": outcome.problems[:10],
    }


# ----------------------------------------------------------- review units
#
# A review unit's agents each write a small record under
# .plumbline/runs/<run>/<stage>/round-<n>/: a findings_record per prosecutor,
# a defense_record per defender and, once no blocker stands, one gaps_record.
# merge_review turns them into the stage's review_record.


def _round_number(path: Path) -> int | None:
    match = re.fullmatch(r"round-([1-9][0-9]*)", path.name)
    return int(match.group(1)) if match and path.is_dir() else None


def _part_type(data) -> str | None:
    """Which per-agent review record `data` is, or None when it is none of them."""
    return next((name for name in REVIEW_PART_TYPES if not check_record(name, data)), None)


def _guess_part_type(data) -> str:
    """The type a record that validates as none of them was most likely meant to be."""
    if isinstance(data, dict):
        if "defender" in data or "defenses" in data:
            return "defense_record"
        if "lens" in data or "findings" in data:
            return "findings_record"
    return "gaps_record"


def _majority(count: int) -> int:
    return count // 2 + 1


def normalise_quote(text: str) -> str:
    """Whitespace collapsed to single spaces: a quote is compared with the code by its words, not by its line breaks."""
    return " ".join(text.split())


class Evidence:
    """Checks a quote against the change: it counts when, whitespace-normalised, it occurs in the change's diff (from the
    merge base to the files as they are) or in the current content of the file the finding names."""

    def __init__(self, root: Path, diff_lines: list[str]) -> None:
        self.root = root
        self.diff = normalise_quote("\n".join(diff_lines))
        self._files: dict[str, str] = {}

    def file_text(self, name: str) -> str:
        if name not in self._files:
            text = ""
            relative = os.path.normpath(name)
            path = self.root / relative
            try:
                inside = not os.path.isabs(name) and path.resolve().is_relative_to(self.root.resolve())
                if inside and path.is_file() and path.stat().st_size <= 8 << 20:
                    text = normalise_quote(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            self._files[name] = text
        return self._files[name]

    def holds(self, quote: str, name: str, at_least: int = 1) -> bool:
        wanted = normalise_quote(quote)
        return len(wanted) >= at_least and (wanted in self.diff or wanted in self.file_text(name))


def route_of(pipeline: dict, finding: dict) -> str:
    """Who a surviving finding goes to: the test-writer for the tests lens and for a finding about a test file (the
    builder works without the tests), the builder for the rest."""
    return "test-writer" if finding["lens"] == TESTS_LENS or file_type(pipeline, os.path.normpath(finding["file"]).replace(os.sep, "/")) == TESTS_TYPE else "builder"


SEVERITIES = ("MINOR", "MAJOR", "BLOCKING")  # lowest first


def severity_claims(findings: list[dict], survivors: list[str], defenses: list[dict], threshold: int, warnings: list[str]) -> dict[str, str]:
    """The surviving findings whose severity the defenders raise, as {finding id: the new severity}. A defender that concedes a finding
    may claim a severity for it (`severity_claim`). A claim equal to or below the severity the prosecutor filed is ignored, with a warning.
    When at least `threshold` defenders claim a higher severity, the finding is raised to the highest level that many of them claimed:
    the threshold-th highest claim, because a claim of BLOCKING asks for MAJOR as well."""
    filed = {finding["id"]: finding["severity"] for finding in findings}
    claims: dict[str, list[str]] = {}
    for defense in defenses:
        claim = defense.get("severity_claim")
        if claim is None:
            continue
        fid = defense["finding_id"]
        if SEVERITIES.index(claim) <= SEVERITIES.index(filed[fid]):
            warnings.append(f"defender '{defense['defender']}' claims {claim} for '{fid}', which is not above the {filed[fid]} the prosecutor filed, so the claim is ignored")
        else:
            claims.setdefault(fid, []).append(claim)
    raised = {}
    for fid in survivors:
        higher = sorted(claims.get(fid, []), key=SEVERITIES.index, reverse=True)
        if threshold and len(higher) >= threshold:
            raised[fid] = higher[threshold - 1]
    return raised


@dataclass
class MergeResult:
    record: dict | None
    problems: list[str]
    warnings: list[str]
    parts: list[dict] = field(default_factory=list)  # [{path, sha256}] of the agents' records the review record was built from
    unverified: list[str] = field(default_factory=list)  # the findings whose evidence is in neither the diff nor the file, as `id (file:line)`


def merge_round(project: Project, run_id: str, stage_id: str, round_no: int | None = None) -> MergeResult:
    """The review_record of a review stage's round, built from the per-agent records: the record (None when there are problems)
    with its problems and warnings, and what the ledger needs to trace it.

    Each part must be what its agent left (the ledger holds the part's hash from the agent's stop) and carry the hash of the
    change as the files are now. A finding survives when at least `survive_if_unrefuted_by` of the stage's `defenders` did
    not refute it (the majority when the stage sets none). A defender refutes a finding by a verdict of `refuted` with a
    quote that occurs in the change's diff or in the finding's file; a defender that is silent on a finding, concedes it,
    or refutes without such a quote did not refute it. A finding whose own evidence is in neither place stays, and is
    marked evidence_unverified. A surviving finding that enough defenders claim a higher severity for is raised to it (see
    severity_claims), and marked severity_raised_from. A stage without defenders lets every finding survive."""
    root = project.root
    stage = next((s for s in project.pipeline["stage"] if s["id"] == stage_id), None)
    if stage is None:
        raise PlumblineError(f"the pipeline has no stage '{stage_id}'")
    if stage.get("kind", "agent") != "review":
        raise PlumblineError(f"stage '{stage_id}' is not a review stage")
    run = load_run(project, run_id)
    if stage_id not in [s["id"] for s in run.stages]:
        raise PlumblineError(f"stage '{stage_id}' is not part of row {run.row}, the row of run '{run_id}'")
    expected = list(run.lenses) if _is_diff_review(stage) and run.lenses else list(stage["lenses"])

    unit = run_dir(root, run_id) / stage_id
    rounds = sorted(n for n in (_round_number(p) for p in unit.glob("round-*")) if n is not None)
    if round_no is None:
        if not rounds:
            raise PlumblineError(f"no per-agent records for stage '{stage_id}': expected files in {rel_path(root, unit)}/round-<n>/")
        round_no = rounds[-1]
    if round_no < 1:
        raise PlumblineError("the round must be 1 or more")
    if stage.get("max_rounds") and round_no > stage["max_rounds"]:
        raise PlumblineError(
            f"round {round_no} is past the {stage['max_rounds']} rounds stage '{stage_id}' has: stop, and bring the findings and the failing criteria to the builder"
        )
    round_dir = unit / f"round-{round_no}"
    if not round_dir.is_dir():
        raise PlumblineError(f"there is no {rel_path(root, round_dir)}/")

    problems: list[str] = []
    warnings: list[str] = []
    findings_records: list[tuple[str, dict]] = []
    defense_records: list[tuple[str, dict]] = []
    gaps_records: list[tuple[str, dict]] = []
    for path in sorted(round_dir.glob("*.json")):
        where = rel_path(root, path)
        data, problem = load_json_file(path)
        if problem:
            problems.append(f"{where}: {problem}")
            continue
        kind = _part_type(data)
        if kind is None:
            guess = _guess_part_type(data)
            errors = check_record(guess, data)
            problems.append(f"{where}: not a valid {guess}: {errors[0]}" + (f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""))
            continue
        {"findings_record": findings_records, "defense_record": defense_records, "gaps_record": gaps_records}[kind].append((where, data))

    defenders = stage.get("defenders", 0)
    used = [
        *((where, data, "prosecutor") for where, data in findings_records),
        *((where, data, "defender") for where, data in (defense_records if defenders else [])),
        *((where, data, "detective") for where, data in gaps_records),
    ]

    # each part is what its agent left, and was made against the change as it is now
    ledger = read_ledger(root, run_id)
    tree = worktree_tree(root)
    current = change_hash(root, run.merge_base, tree)
    parts = []
    for where, data, role in used:
        sha = file_sha256(root / where)
        parts.append({"path": where, "sha256": sha})
        problems += _agent_entry_problems(latest_entry(ledger, "agent", record=where), role, sha, where)
        if data["diff_sha256"] != current:
            problems.append(
                f"{where}: it covers the change {data['diff_sha256'][:12]}, but the files now hash to {current[:12]}; "
                "the change was edited after this agent read it, so run the agent again"
            )

    by_lens: dict[str, str] = {}
    for where, data in findings_records:
        if data["lens"] in by_lens:
            problems.append(f"{where}: a second findings_record for lens '{data['lens']}' (the first is {by_lens[data['lens']]})")
        elif data["lens"] not in expected:
            problems.append(f"{where}: lens '{data['lens']}' is not one of this round's lenses ({', '.join(expected)})")
        else:
            by_lens[data["lens"]] = where
    for lens in expected:
        if lens not in by_lens:
            problems.append(f"no findings_record for lens '{lens}' in {rel_path(root, round_dir)}/")
    if len(gaps_records) > 1:
        problems.append(f"{len(gaps_records)} gaps_records ({', '.join(w for w, _ in gaps_records)}); the detective writes one")

    findings: list[dict] = []
    owner: dict[str, str] = {}
    for lens in expected:
        for where, data in findings_records:
            if data["lens"] != lens:
                continue
            for finding in data["findings"]:
                if finding["lens"] != lens:
                    warnings.append(f"{where}: finding '{finding['id']}' carries lens '{finding['lens']}' but was written by the '{lens}' prosecutor")
                if finding["id"] in owner:
                    problems.append(f"{where}: finding id '{finding['id']}' is also used in {owner[finding['id']]}; ids must be unique across the round")
                else:
                    owner[finding["id"]] = where
                    findings.append(finding)

    threshold = stage.get("survive_if_unrefuted_by", _majority(defenders)) if defenders else 0
    names = sorted({data["defender"] for _, data in defense_records}) if defenders else []
    seen_pairs: set[tuple[str, str]] = set()
    defenses: list[dict] = []
    for where, data in defense_records if defenders else []:
        for defense in data["defenses"]:
            pair = (defense["finding_id"], defense["defender"])
            if defense["defender"] != data["defender"]:
                problems.append(f"{where}: an entry names defender '{defense['defender']}' in the record of defender '{data['defender']}'")
            elif defense["finding_id"] not in owner:
                problems.append(f"{where}: a defense of unknown finding '{defense['finding_id']}'")
            elif pair in seen_pairs:
                problems.append(f"{where}: defender '{defense['defender']}' defends finding '{defense['finding_id']}' more than once")
            else:
                seen_pairs.add(pair)
                defenses.append(defense)
    if defenders and len(names) > defenders:
        problems.append(f"{len(names)} defenders reported ({', '.join(names)}) but the stage has {defenders}")
    if not defenders and defense_records:
        warnings.append("this stage has no defenders, so its defense records were ignored and every finding survives")
    if defenders and findings and len(names) < defenders:
        warnings.append(f"only {len(names)} of {defenders} defenders reported; a missing defender did not refute anything")
    if problems:
        return MergeResult(None, problems, warnings)

    evidence = Evidence(root, change_diff_lines(root, run.merge_base, tree))
    file_of = {finding["id"]: finding["file"] for finding in findings}
    position = {finding["id"]: index for index, finding in enumerate(findings)}
    refuted_by: dict[str, set[str]] = {}
    for defense in defenses:
        if defense["verdict"] != "refuted":
            continue
        who, fid, quote = defense["defender"], defense["finding_id"], defense["quote"]
        if not quote.strip():
            warnings.append(f"defender '{who}' refuted '{fid}' without quoting code, which does not count")
        elif len(normalise_quote(quote)) < MIN_QUOTE:
            warnings.append(f"defender '{who}' refuted '{fid}' with a quote of fewer than {MIN_QUOTE} characters, which does not count")
        elif not evidence.holds(quote, file_of[fid], MIN_QUOTE):
            warnings.append(f"defender '{who}' refuted '{fid}' with a quote that is in neither the change nor {file_of[fid]}, which does not count")
        else:
            refuted_by.setdefault(fid, set()).add(who)
    survivors = [f["id"] for f in findings if defenders - len(refuted_by.get(f["id"], ())) >= threshold]
    raised = severity_claims(findings, survivors, defenses, threshold, warnings)
    severity = {f["id"]: raised.get(f["id"], f["severity"]) for f in findings}
    unverified = [f for f in findings if not evidence.holds(f["evidence"], f["file"])]
    unverified_ids = {f["id"] for f in unverified}
    marked = [
        {**f, "severity": severity[f["id"]], "evidence_unverified": f["id"] in unverified_ids, **({"severity_raised_from": f["severity"]} if f["id"] in raised else {})}
        for f in findings
    ]
    by_id = {f["id"]: f for f in findings}
    routes: dict[str, list[str]] = {"builder": [], "test-writer": []}
    for fid in survivors:
        routes[route_of(project.pipeline, by_id[fid])].append(fid)
    defenses.sort(key=lambda d: (position[d["finding_id"]], d["defender"]))
    record = {
        "target": stage["target"],
        "round": round_no,
        "lenses": expected,
        "findings": marked,
        "defenses": defenses,
        "survivors": survivors,
        "gaps": gaps_records[0][1]["gaps"] if gaps_records else [],
        "blockers_surviving": sum(1 for fid in survivors if severity[fid] == "BLOCKING"),
        "routes": routes,
        "diff_sha256": current,
    }
    errors = check_record("review_record", record)
    if errors:
        raise PlumblineError("internal error: the merged review_record does not validate: " + "; ".join(errors[:3]))
    return MergeResult(record, [], warnings, parts, [f"{f['id']} ({f['file']}:{f['line']})" for f in unverified])


def merge_review(project: Project, run_id: str, stage_id: str, round_no: int | None = None) -> tuple[dict | None, list[str], list[str]]:
    """merge_round's record, problems and warnings."""
    result = merge_round(project, run_id, stage_id, round_no)
    return result.record, result.problems, result.warnings


# ------------------------------------------------------------------ tokens

USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def usage_by_model(transcripts: list[Path]) -> dict:
    """Token usage per model from Claude Code transcripts. A transcript holds
    several records for one API message (one per streamed content block), each
    with the usage known when it was written, so the maximum of each counter per
    message id is what counts: the counters only grow as a message streams, so
    that is the last record's. Output is output_tokens, fresh input is
    input_tokens plus cache writes, and cache reads stand alone.

    The first records of a message carry a snapshot taken as it starts (a small
    output_tokens, stop_reason null) and the last carries the real count
    (stop_reason set). When the last never reached the transcript, the message
    has only snapshots and its output is a lower bound: `output_lower_bound`
    counts those messages per model. Input and cache counters are known as a
    message starts, so they are exact either way."""
    best: dict[str, dict] = {}
    for path in transcripts:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            message_id, model = message.get("id"), message.get("model")
            if not isinstance(message_id, str) or not message_id or model == "<synthetic>":
                continue
            entry = best.setdefault(message_id, {"model": None, "final": False, **{name: 0 for name in USAGE_FIELDS}})
            if isinstance(model, str) and model:
                entry["model"] = model
            if isinstance(message.get("stop_reason"), str) and message["stop_reason"]:
                entry["final"] = True  # the record that ends the message: its usage is the real count
            for name in USAGE_FIELDS:
                value = message["usage"].get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value > entry[name]:
                    entry[name] = value
    by_model: dict[str, dict] = {}
    for entry in best.values():
        totals = by_model.setdefault(entry["model"] or "unknown", {"output": 0, "fresh_input": 0, "cache_read": 0, "output_lower_bound": 0})
        totals["output"] += entry["output_tokens"]
        totals["fresh_input"] += entry["input_tokens"] + entry["cache_creation_input_tokens"]
        totals["cache_read"] += entry["cache_read_input_tokens"]
        totals["output_lower_bound"] += 0 if entry["final"] else 1
    return dict(sorted(by_model.items()))


def ledger_transcripts(root: Path, run_id: str) -> tuple[list[Path], list[str]]:
    """The agent transcripts a run's ledger points to, and notes about those it cannot find.
    The path Claude Code reported for the agent is used; failing that, it is derived
    from the session transcript as <dir>/<session_id>/subagents/agent-<agent_id>.jsonl."""
    found: list[Path] = []
    notes: list[str] = []
    for entry in read_ledger(root, run_id):
        if entry.get("kind") != "agent":
            continue
        candidates = []
        if isinstance(entry.get("transcript"), str) and entry["transcript"]:
            candidates.append(Path(entry["transcript"]))
        session, session_id, agent_id = entry.get("session_transcript"), entry.get("session_id"), entry.get("agent_id")
        if isinstance(session, str) and isinstance(session_id, str) and isinstance(agent_id, str) and session and session_id and agent_id:
            candidates.append(Path(session).parent / session_id / "subagents" / f"agent-{agent_id}.jsonl")
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            note = f"no transcript found for agent {entry.get('agent_id')} ({entry.get('agent_type')}, stage {entry.get('stage')})"
            if note not in notes:  # an agent that stopped several times is one agent
                notes.append(note)
        elif path not in found:
            found.append(path)
    return found, notes


def tokens_for_run(root: Path, run_id: str) -> tuple[dict, list[str]]:
    """The run's `tokens` object, {"by_model": {...}}, and notes: the transcripts that could not be found, and each model whose output
    count is a lower bound because some of its messages never got their final usage entry (see usage_by_model)."""
    existing_run_dir(root, run_id)
    transcripts, notes = ledger_transcripts(root, run_id)
    by_model = usage_by_model(transcripts)
    for model, usage in by_model.items():
        if usage["output_lower_bound"]:
            notes.append(
                f"the output tokens of {model} are a lower bound: {_plural(usage['output_lower_bound'], 'message')} "
                "had no final usage entry, only the snapshot taken as a message starts"
            )
    return {"by_model": by_model}, notes


# ------------------------------------------- pass records and overrides


def dirty_paths(root: Path) -> list[str]:
    """Paths with uncommitted changes (untracked files included), apart from .plumbline/."""
    output = _git_text(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = output.split("\0")
    paths = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            index += 1  # a rename or copy is followed by its source path
        paths.append(entry[3:])
    return [p for p in paths if not p.startswith(IGNORE_ENTRY)]


def coverage(root: Path, head: str, project: Project) -> tuple[str | None, str]:
    """How HEAD is covered: ("pass" or "override", detail), or (None, why not).
    The record must validate and name this very commit; a pass record must say pass.
    A pass record is not trusted on its own: its run is evaluated again (see
    verify_pass), which is what the push gate and `status` do."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        return None, "HEAD is not a commit id"
    pass_file = root / PASS_DIR / f"{head}.json"
    data, problem = load_json_file(pass_file)
    if problem is None:
        errors = check_record("pass_record", data)
        if errors:
            reason = f"{rel_path(root, pass_file)} is not a valid pass_record ({errors[0]})"
        elif data["commit"] != head:
            reason = f"{rel_path(root, pass_file)} is for another commit"
        elif data["verdict"] != "pass":
            reason = f"{rel_path(root, pass_file)} says {data['verdict']}, not pass"
        else:
            doubts = verify_pass(project, head, data)
            if not doubts:
                return "pass", f"run {data['run_id']}"
            reason = f"{rel_path(root, pass_file)} does not hold up: " + "; ".join(doubts[:3]) + (f"; and {len(doubts) - 3} more" if len(doubts) > 3 else "")
    elif problem == "no such file":
        reason = "no pass or override record"
    else:
        reason = f"{rel_path(root, pass_file)} cannot be used: {problem}"
    override_file = root / PASS_DIR / f"{head}.override.json"
    data, problem = load_json_file(override_file)
    if problem is None and not check_record("override_record", data) and data["commit"] == head:
        return "override", data["reason"]
    return None, reason


def rounds_taken(stage: dict, record: dict | None, ledger: list[dict]) -> int:
    """How many rounds a stage took: a review stage says so itself, an agent stage is counted by its attempts over the whole
    run (see stage_agents: an agent that stopped several times before a gate is one), a main-session stage took one."""
    if stage.get("kind", "agent") == "review" and record is not None:
        return record["round"]
    return stage_agents(ledger, stage["id"]) or 1


def open_items(stage_id: str, review: dict) -> tuple[list[dict], list[dict]]:
    """What a merged review record leaves open: its surviving findings that are not BLOCKING and the detective's gaps, each
    with the stage it came from. The review's own files stay in the run, which `.plumbline/` keeps out of git, so the pass
    record carries them on."""
    by_id = {finding["id"]: finding for finding in review["findings"]}
    found = [
        {"stage": stage_id, **{key: by_id[fid][key] for key in OPEN_FINDING_KEYS}}
        for fid in dict.fromkeys(review["survivors"])
        if fid in by_id and by_id[fid]["severity"] != "BLOCKING"
    ]
    return found, [{"stage": stage_id, **{key: gap[key] for key in OPEN_GAP_KEYS}} for gap in review["gaps"]]


def stale_change_problems(project: Project, run: Run) -> list[str]:
    """The verify record and the review of the diff must cover the change HEAD holds: the diff from the
    run's merge base to HEAD must hash to the `diff_sha256` of each. A change edited after them does not."""
    root = project.root
    covered = [s for s in run.stages if s["record"] == "verify_record" or _is_diff_review(s)]
    if not covered:
        return []
    try:
        current = change_hash(root, run.merge_base, head_tree(root))
    except PlumblineError as exc:
        return [str(exc)]
    problems = []
    for stage in covered:
        data, _ = read_stage_record(root, run.run_id, stage)
        if data is not None and data["diff_sha256"] != current:
            problems.append(
                f"stage '{stage['id']}': its record covers the change {data['diff_sha256'][:12]}, but the change of HEAD from the merge base "
                f"hashes to {current[:12]}; the change was edited after that record was made, so run the pipeline again from verify"
            )
    return problems


def measure_row(project: Project, run: Run) -> tuple[dict | None, list[str]]:
    """Measure the change again, from the run's merge base to the files as they are: (its change_class record, problems).
    The record is None when nothing has changed, or when the change could not be measured (the problems say so). The
    row a run was declared with is an estimate; this is what the change measures as."""
    try:
        return classify(project.root, project.pipeline, run.merge_base, run.intent), []
    except NothingToClassify:
        return None, []
    except PlumblineError as exc:
        return None, [f"the change could not be measured again: {exc}"]


def missing_stages(project: Project, run: Run, measured: dict | None) -> list[str]:
    """The stages the measured row selects (the intent applied) that the run does not have."""
    if measured is None:
        return []
    have = {s["id"] for s in run.stages}
    return [sid for sid in effective_row(project.pipeline, measured["row"], run.intent).stages if sid not in have]


MAX_NAMED_FILES = 5  # how many files a problem about the size of a change names


def biggest_files(record: dict, limit: int = MAX_NAMED_FILES) -> str:
    """A sentence naming the files of a measured change (a change_class record) that contribute the most changed lines, up to `limit`,
    each with its count, or "" when none counts. The size is the sum over the same files: untracked files are in it as added lines, and
    generated files are not."""
    counted = sorted(
        ((f["added"] + f["removed"], f["path"]) for f in record["files"] if not f["generated"] and f["added"] + f["removed"] > 0),
        key=lambda item: (-item[0], item[1]),
    )
    if not counted:
        return ""
    named = ", ".join(f"{path} ({_plural(lines, 'line')})" for lines, path in counted[:limit])
    rest = f" and {_plural(len(counted) - limit, 'file')} more" if len(counted) > limit else ""
    return f" The files that contribute most: {named}{rest}."


def row_problems(project: Project, run: Run, measured: dict | None) -> list[str]:
    """What the measured row asks for that the run does not have. The row selects stages once the intent is applied: a change
    that measures larger than it was declared selects stages the run lacks, and a change of a size that ends before reduce
    (nothing is built at size L) cannot be passed at all. Each problem names the files that contribute most to the size."""
    if measured is None:
        return []
    by_id = {s["id"]: s for s in project.pipeline["stage"]}
    label = measured["row"]
    stages = effective_row(project.pipeline, label, run.intent).stages
    problems = []
    if not any(by_id[sid]["record"] == "pass_record" for sid in stages):
        problems.append(
            f"this row ends before reduce: split the change (it measures as {label}, {measured['lines']} changed lines, and nothing is built at that size)."
            + biggest_files(measured)
        )
    missing = missing_stages(project, run, measured)
    if missing:
        problems.append(
            f"the change measures as {label}, which selects {', '.join(missing)}; run '{run.run_id}' (row {run.row}) has no such stage"
            f"{'' if len(missing) == 1 else 's'}: start a new run for this row (`plan --intent {run.intent} --row {label}`)."
            + biggest_files(measured)
        )
    return problems


def make_pass_record(project: Project, run_id: str) -> tuple[dict | None, list[str], Path]:
    """The pass_record for HEAD, as (record, problems, the run's copy of it). It
    needs a clean working tree (apart from .plumbline/), a row that reaches reduce,
    and every gate of the run's row to pass, each gate being evaluated afresh and
    entered in the ledger. The change is measured again: its row must not select
    a stage the run lacks. Once the gates pass, the verify record and the review of
    the diff must also cover the change HEAD holds (see stale_change_problems).
    What the run's intent supplies is evaluated like any stage, with rounds 0.
    What the review stages leave open (see open_items) goes into the record."""
    root = project.root
    head = head_sha(root)
    run = load_run(project, run_id)
    problems: list[str] = []
    dirty = dirty_paths(root)
    if dirty:
        listed = ", ".join(dirty[:10]) + (f" and {len(dirty) - 10} more" if len(dirty) > 10 else "")
        problems.append(f"the working tree is not clean apart from {IGNORE_ENTRY}: {listed} (commit the change, then run pass)")

    reduce_stage = next((s for s in run.stages if s["record"] == "pass_record"), None)
    if reduce_stage is None:
        problems.append(f"this row ends before reduce: split the change (row {run.row} has no reduce stage, so there is nothing to pass at this size)")
    measured, measure_problems = measure_row(project, run)
    problems += measure_problems + row_problems(project, run, measured)
    ledger = read_ledger(root, run_id)
    order = {s["id"]: position for position, s in enumerate(project.pipeline["stage"])}
    entries: list[dict] = []
    open_findings: list[dict] = []
    gaps: list[dict] = []
    supplied_ids = {s["id"] for s in run.supplied}
    for stage in [*run.supplied, *run.stages]:
        if stage is reduce_stage:
            continue
        outcome = evaluate_stage(project, run_id, stage, run=run)
        if outcome.gate is not None:
            append_ledger(root, run_id, ledger_gate_entry(outcome))
        if not outcome.passed:
            problems.append(f"stage '{stage['id']}'" + (f" (gate {outcome.gate})" if outcome.gate else "") + ": " + "; ".join(outcome.problems[:3]))
        rounds = 0 if stage["id"] in supplied_ids else rounds_taken(stage, outcome.data, ledger)
        entries.append({"id": stage["id"], "record": outcome.record, "gate": outcome.gate, "passed": outcome.passed, "rounds": rounds})
        if stage.get("kind", "agent") == "review" and outcome.data is not None:
            found, missing = open_items(stage["id"], outcome.data)
            open_findings += found
            gaps += missing
    reduce_id = reduce_stage["id"] if reduce_stage else "reduce"
    reduce_path = run_dir(root, run_id) / f"{reduce_id}.json"
    if reduce_stage is not None and reduce_stage.get("gate") == "all_gates_passed" and not problems:
        final = check_all_gates_passed(project, run_id, reduce_stage)
        problems += [f"stage '{reduce_id}' (gate all_gates_passed): {p}" for p in final]
    if not problems:
        problems += stale_change_problems(project, run)
    if problems:
        return None, problems, reduce_path
    if reduce_stage is not None:
        entries.append({"id": reduce_id, "record": rel_path(root, reduce_path), "gate": reduce_stage.get("gate"), "passed": True, "rounds": 1})
    entries.sort(key=lambda entry: order.get(entry["id"], len(order)))
    tokens, notes = tokens_for_run(root, run_id)
    notes.insert(0, f"declared row {run.row}, measured row {measured['row'] if measured is not None else 'none (nothing has changed)'}")
    if run.note:
        notes.insert(0, run.note)
    if run.supplied:
        notes.append(f"intent {run.intent}: the record of {', '.join(sorted(supplied_ids))} was supplied, not written by an agent")
    record = {
        "commit": head, "run_id": run_id, "row": run.row, "stages": entries, "tokens": tokens, "verdict": "pass", "notes": notes,
        "open_findings": open_findings, "gaps": gaps,
    }
    errors = check_record("pass_record", record)
    if errors:
        raise PlumblineError("internal error: the pass_record does not validate: " + "; ".join(errors[:3]))
    return record, [], reduce_path


def note_pass(root: Path, run_id: str, record: dict, pass_file: Path) -> None:
    """Enter a pass in the run's ledger, with the hash of every record the pass record lists and of the pass
    record itself, so that `verify_pass` can tell later whether any of them was altered."""
    append_ledger(
        root,
        run_id,
        {
            "kind": "pass",
            "commit": record["commit"],
            "pass_record": rel_path(root, pass_file),
            "pass_sha256": file_sha256(pass_file),
            "records": {entry["record"]: file_sha256(root / entry["record"]) for entry in record["stages"]},
        },
    )


def verify_pass(project: Project, head: str, data: dict) -> list[str]:
    """What makes a valid pass record for `head` untrustworthy, as problems (none when it holds). The pass
    file is not taken at its word: the ledger of its run must show that `pass` wrote it, none of the records
    it lists (nor the pass file) may have changed since, and the gate `all_gates_passed` is evaluated again."""
    root = project.root
    run_id = data["run_id"]
    try:
        run = load_run(project, run_id)
    except PlumblineError as exc:
        return [f"its run cannot be read ({exc})"]
    entry = next((e for e in reversed(read_ledger(root, run_id)) if e.get("kind") == "pass" and e.get("commit") == head), None)
    if entry is None:
        return [f"the ledger of run {run_id} does not show `pass` writing it"]
    problems = []
    if entry.get("pass_sha256") != file_sha256(root / PASS_DIR / f"{head}.json"):
        problems.append("the pass record was altered after `pass` wrote it")
    hashed = entry.get("records") if isinstance(entry.get("records"), dict) else {}
    altered = []
    for listed in data["stages"]:
        path = listed["record"]
        if path not in hashed:
            problems.append(f"the ledger has no hash of {path}, which the pass record lists")
        elif file_sha256(root / path) != hashed[path]:
            altered.append(path)
    if altered:
        problems.append("records altered after the pass: " + ", ".join(altered))
    reduce_stage = next((s for s in run.stages if s["record"] == "pass_record"), None)
    if reduce_stage is not None and reduce_stage.get("gate") == "all_gates_passed":
        problems += [f"all_gates_passed: {p}" for p in check_all_gates_passed(project, run_id, reduce_stage)]
    return problems


def git_identity(root: Path) -> str:
    """Who is running this: the git author name, else the login name, else 'unknown'."""
    result = _git(root, "var", "GIT_AUTHOR_IDENT")
    ident = result.stdout.decode("utf-8", "replace").strip()
    name = ident.split(" <", 1)[0].strip() if result.returncode == 0 else ""
    if name:
        return name
    try:
        import getpass

        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def make_override(project: Project, reason: str, run_id: str | None) -> dict:
    """The override_record for HEAD. `stages_skipped` are the stages of the run's
    row (the latest run when none is named) that did not pass, or every stage of
    the pipeline when there is no run."""
    root = project.root
    head = head_sha(root)
    reason = reason.strip()
    if run_id is not None:
        existing_run_dir(root, run_id)  # a run that was named must exist
    else:
        run_id = active_run_id(root)
    skipped = None
    if run_id is not None:
        try:
            run = load_run(project, run_id)
            skipped = [s["id"] for s in [*run.supplied, *run.stages] if s["record"] != "pass_record" and not evaluate_stage(project, run_id, s).passed]
        except PlumblineError:
            pass  # a run whose intake cannot be read did not get anywhere
    if skipped is None:
        skipped = [s["id"] for s in project.pipeline["stage"]]
    record = {"commit": head, "reason": reason, "by": git_identity(root), "at": utc_now(), "stages_skipped": skipped}
    errors = check_record("override_record", record)
    if errors:
        raise PlumblineError("the override needs a reason of at least 20 characters" if any(e.startswith("$.reason") for e in errors) else errors[0])
    return record


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
    record = classify(project.root, project.pipeline, args.base, args.intent, args.row)
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


def supplied_records(effective: EffectiveRow, spec_file: str | None) -> tuple[str | None, dict | None, list[str]]:
    """The spec an intent supplies, as (where it came from, the record, problems). A usage problem (no spec
    where one is needed, a spec where none is wanted, an unreadable file) is an exception; a spec that does
    not hold is a list of problems. The record is validated against the spec schema and must pass the gate
    of the stage it stands for (`spec_complete`): supplying a record does not skip what the stage would have had to meet."""
    if not effective.supplied:
        if spec_file:
            raise PlumblineError(f"the intent '{effective.intent}' supplies no record, so --spec does not apply")
        return None, None, []
    template = TEMPLATE_DIR / f"{effective.intent}.json"
    if template.is_file():
        if spec_file:
            raise PlumblineError(f"the intent '{effective.intent}' uses its shipped template (pipeline/templates/{template.name}), so --spec does not apply")
        path, source = template, f"the shipped template pipeline/templates/{template.name}"
    else:
        if not spec_file:
            raise PlumblineError(f"the intent '{effective.intent}' supplies the spec: pass --spec FILE, a spec record (see schemas/spec.json)")
        path, source = Path(spec_file).expanduser(), spec_file
    data, problem = load_json_file(path)
    if problem:
        if problem.startswith("not valid JSON"):
            return source, None, [f"{source}: {problem}"]
        raise PlumblineError(f"{source}: {problem}")
    problems = [f"{source}: {error}" for error in check_record("spec", data)]
    if not problems:
        problems = [f"{source}: {problem}" for problem in [*_gate_spec_complete(data, None), *single_ac_problems(effective.intent, data)]]
    return source, (None if problems else data), problems


def start_run(project: Project, run_id: str, record: dict, effective: EffectiveRow, source: str | None, supplied: dict | None) -> list[str]:
    """Begin a run: copy what the intent supplies, then write the intake record, which carries the intent.
    The intake record goes last: a run has begun once it exists, and a start that failed halfway can be redone.
    Returns the reasons the run was not started (the run already began), or nothing."""
    root = project.root
    intake_stage = intake_stage_of(project.pipeline)
    intake_path = run_dir(root, run_id) / f"{intake_stage['id']}.json"
    if intake_path.exists():
        return [f"run '{run_id}' has begun ({rel_path(root, intake_path)} exists); a run is never restarted, so pick another --run-id"]
    by_id = {s["id"]: s for s in project.pipeline["stage"]}
    run = Run(  # the run as it will be, judged before its intake record exists
        root, run_id, record["row"], [effective_stage(by_id[sid], effective) for sid in effective.stages], effective.lenses, effective.note,
        record["intent"], [effective_stage(by_id[sid], effective) for sid in effective.supplied], record["base"], record["merge_base"],
    )
    for stage in run.supplied:
        path = run_dir(root, run_id) / f"{stage['id']}.json"
        write_json_atomic(path, supplied)
        append_ledger(root, run_id, {"kind": "supplied", "stage": stage["id"], "record": rel_path(root, path), "source": source, "record_sha256": file_sha256(path)})
        outcome = evaluate_stage(project, run_id, stage, run=run)
        if outcome.gate is not None:
            append_ledger(root, run_id, ledger_gate_entry(outcome))
    text = json_text(record)  # the ledger holds the intake record's hash, entered before the file exists: a start that stops between the two can be redone
    append_ledger(
        root, run_id,
        {"kind": "intake", "stage": intake_stage["id"], "record": rel_path(root, intake_path), "record_sha256": text_sha256(text), "intent": record["intent"], "row": record["row"], "merge_base": record["merge_base"]},
    )
    write_json_atomic(intake_path, record)
    return []


def cmd_plan(args) -> int:
    starting = args.intent is not None  # `--intent` starts the run: it writes its intake record, so it needs a repository that has adopted plumbline
    project = _adopted_project(args.project) if starting else _ready_project(args.project)
    if args.spec and not starting:
        raise PlumblineError("--spec goes with --intent (the intent that supplies the spec)")
    intent = args.intent or DEFAULT_INTENT
    run_id = check_run_id(args.run_id if args.run_id is not None else default_run_id(project.root))
    record = classify(project.root, project.pipeline, args.base, intent, args.row)
    effective = effective_row(project.pipeline, record["row"], intent)
    plan = build_plan(project.pipeline, record, run_id, project.commands, project.graft_enabled)
    if starting:
        source, supplied, problems = supplied_records(effective, args.spec)
        if problems:
            for problem in problems:
                print(f"error: {problem}")
            print(f"plan: the spec for intent '{intent}' does not hold ({_plural(len(problems), 'problem')}); nothing was written")
            return 1
        refused = start_run(project, run_id, record, effective, source, supplied)
        if refused:
            for reason in refused:
                print(f"plumbline: {reason}. Nothing was changed.", file=sys.stderr)
            return 1
        write_active(project.root, run_id)  # the hooks read this file to know which run is in progress
        for entry in plan["supplied"]:
            entry["source"] = source
    sys.stdout.write(json.dumps(plan, indent=2) + "\n")
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


def _one_line(text) -> str:
    return " ".join(str(text).split())


def route_lines(record: dict) -> list[str]:
    """The surviving findings of a merged review, kept apart by who they go to: the text of each (no quote of code, no path
    of a review file), so that the builder can be briefed with the first list alone and never sees the second."""
    by_id = {f["id"]: f for f in record["findings"]}
    lines = []
    for who, note in (
        ("builder", "give the builder this text and nothing else"),
        ("test-writer", "the tests lens, and findings about test files: the builder never sees these"),
    ):
        ids = record["routes"][who]
        if not ids:
            continue
        lines.append(f"surviving findings for the {who} ({note}):")
        for fid in ids:
            f = by_id[fid]
            lines.append(
                f"  - {fid} [{f['severity']}] {f['file']}:{f['line']}: {_one_line(f['claim'])} "
                f"Failure: {_one_line(f['failure_scenario'])} Rule: {_one_line(f['rule'])}"
            )
    return lines


def raise_lines(record: dict) -> list[str]:
    """One line for each finding whose severity `merge-review` raised, naming the defenders whose claims did it."""
    lines = []
    for f in record["findings"]:
        if "severity_raised_from" not in f:
            continue
        asked = [
            d["defender"] for d in record["defenses"]
            if d["finding_id"] == f["id"] and "severity_claim" in d and SEVERITIES.index(d["severity_claim"]) >= SEVERITIES.index(f["severity"])
        ]
        lines.append(f"severity raised: {f['id']} from {f['severity_raised_from']} to {f['severity']} (claimed by {', '.join(asked)})")
    return lines


def cmd_merge_review(args) -> int:
    project = _ready_project(args.project)
    result = merge_round(project, args.run_id, args.stage, args.round)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    record = result.record
    if record is None:
        for problem in result.problems:
            print(f"error: {problem}")
        print(f"merge-review: stage '{args.stage}' cannot be merged ({_plural(len(result.problems), 'problem')})")
        return 1
    path = run_dir(project.root, args.run_id) / f"{args.stage}.json"
    write_json_atomic(path, record)
    append_ledger(
        project.root, args.run_id,
        {"kind": "merge", "stage": args.stage, "round": record["round"], "record": rel_path(project.root, path), "record_sha256": file_sha256(path), "parts": result.parts},
    )
    print(
        f"wrote {rel_path(project.root, path)}: round {record['round']}, {_plural(len(record['findings']), 'finding')}, "
        f"{len(record['survivors'])} surviving ({record['blockers_surviving']} blocking), {_plural(len(record['gaps']), 'gap')}"
    )
    for line in raise_lines(record):
        print(line)
    for line in route_lines(record):
        print(line)
    if result.unverified:
        print(f"evidence found in neither the change nor its file (the findings stay, marked evidence_unverified): {', '.join(result.unverified)}")
    return 0


def open_next_round(root: Path, run_id: str, stage: dict, outcome: GateOutcome, round_no: int, limit: int | None) -> Path | None:
    """A review whose round ended with blockers standing, in a stage that has rounds left, goes back for another round. Its agents write
    only in the highest round-<n> directory of their stage (the PreToolUse hook holds them to it) and `merge-review` merges the highest
    round by default, so the directory of round N+1 is made here, and the main session has no step of its own to take. Returns it, or
    None when the stage is no review, its rounds are used, the failure is the ledger's (a record that is missing, changed, or not traced
    to its agents: that round is merged again, not left behind), or no blocker stands."""
    if stage.get("kind", "agent") != "review" or limit is None or round_no >= limit or not outcome.checked or outcome.data is None:
        return None
    if not standing_blockers(outcome.data):
        return None
    directory = run_dir(root, run_id) / stage["id"] / f"round-{round_no + 1}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def cmd_gate(args) -> int:
    """Evaluate a stage's gate: exit 0 when it passes, 1 when it fails, 3 when it fails (or would pass) past the stage's
    max_rounds, because the stage has used its rounds. The gates of a verify stage and of a tests stage run the repository's
    commands themselves and enter what happened in the ledger. A review that fails on standing blockers with rounds left
    opens the directory of its next round (see open_next_round)."""
    project = _ready_project(args.project)
    existing_run_dir(project.root, args.run_id)
    stage = next((s for s in project.pipeline["stage"] if s["id"] == args.stage), None)
    if stage is None:
        raise PlumblineError(f"the pipeline has no stage '{args.stage}'")
    run, run_problem = None, "the run has no usable intake record"
    try:
        run = load_run(project, args.run_id)
    except PlumblineError as exc:
        run_problem = str(exc)  # a run without a usable intake record has no intent, so the stage's own gate applies
    if run is not None:
        stage = next((s for s in [*run.supplied, *run.stages] if s["id"] == stage["id"]), stage)  # the intent may have replaced its gate
    if stage.get("gate") is None:
        print(f"stage '{args.stage}' has no gate; nothing to evaluate")
        return 0
    outcome = evaluate_stage(project, args.run_id, stage, measure=True, run=run, run_problem=run_problem)
    round_no, limit = stage_round(stage, outcome.data, read_ledger(project.root, args.run_id))
    exhausted = False
    if limit is not None and outcome.gate != "all_gates_passed":
        if round_no > limit:
            outcome.problems.append(f"round {round_no} is past the {limit} rounds stage '{stage['id']}' has")
        exhausted = round_no > limit or (round_no == limit and bool(outcome.problems))
        outcome.passed = not outcome.problems
    append_ledger(project.root, args.run_id, ledger_gate_entry(outcome))
    verdict = "pass" if outcome.passed else "FAIL"
    print(f"gate {outcome.gate} for stage '{stage['id']}': {verdict}")
    for problem in outcome.problems:
        print(f"  - {problem}")
    if limit is not None and outcome.gate != "all_gates_passed":
        print(f"  round {round_no} of {limit}")
    if exhausted and not outcome.passed:
        print(f"  stage '{stage['id']}' has used its rounds: stop, and bring these problems and the surviving findings to the builder")
    elif not outcome.passed:
        opened = open_next_round(project.root, args.run_id, stage, outcome, round_no, limit)
        if opened is not None:
            print(f"  round {round_no + 1} of {limit} is open: {rel_path(project.root, opened)}/ (the next round's agents write there)")
    return 0 if outcome.passed else (3 if exhausted else 1)


def cmd_tokens(args) -> int:
    root = resolve_root(args.project, require_git=True)
    tokens, notes = tokens_for_run(root, args.run_id)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    print(json.dumps(tokens, indent=2))
    return 0


def _adopted_project(project_arg: str | None) -> Project:
    project = _ready_project(project_arg)
    if not project.adopted:
        raise PlumblineError(f"{project.root} has not adopted plumbline (there is no {CONFIG_FILE}); /plumbline:init adopts it")
    return project


def cmd_pass(args) -> int:
    project = _adopted_project(args.project)
    record, problems, run_copy = make_pass_record(project, args.run_id)
    if record is None:
        for problem in problems:
            print(f"error: {problem}")
        print(f"pass: refused for run '{args.run_id}' ({_plural(len(problems), 'problem')}); nothing was written")
        return 1
    pass_file = project.root / PASS_DIR / f"{record['commit']}.json"
    write_json_atomic(run_copy, record)
    write_json_atomic(pass_file, record)
    note_pass(project.root, args.run_id, record, pass_file)
    print(f"plumbline: pass recorded for {record['commit'][:7]} (run {args.run_id}, row {record['row']})")
    print(f"  wrote {rel_path(project.root, run_copy)} and {rel_path(project.root, pass_file)}")
    for model, usage in record["tokens"]["by_model"].items():
        output = f"at least {usage['output']}" if usage.get("output_lower_bound") else str(usage["output"])  # some of its messages never got their final count
        print(f"  tokens {model}: output {output}, fresh input {usage['fresh_input']}, cache read {usage['cache_read']}")
    return 0


def open_summary(record: dict) -> str | None:
    """`open: 3 findings, 4 gaps; ...` for a pass record, `open: none` when it leaves nothing open, and None for one that
    predates the lists (it says nothing about what was left)."""
    if "open_findings" not in record or "gaps" not in record:
        return None
    findings, gaps = len(record["open_findings"]), len(record["gaps"])
    if not findings and not gaps:
        return "open: none"
    return f"open: {_plural(findings, 'finding')}, {_plural(gaps, 'gap')}; `plumbline.py open` lists them"


def run_pass_path(project: Project, run_id: str) -> Path:
    """Where `pass` leaves a run's own copy of its pass record: the file of the pipeline's reduce stage."""
    stage_id = next((s["id"] for s in project.pipeline["stage"] if s["record"] == "pass_record"), "reduce")
    return run_dir(project.root, run_id) / f"{stage_id}.json"


def run_pass_record(project: Project, run_id: str) -> tuple[dict | None, str]:
    """A passed run's own copy of its pass record, as (record, why not): the file must be there, validate, and say pass."""
    path = run_pass_path(project, run_id)
    data, problem = load_json_file(path)
    if problem == "no such file":
        return None, f"run '{run_id}' has no pass record ({rel_path(project.root, path)}); `pass` writes it once every gate has passed"
    if problem:
        return None, f"{rel_path(project.root, path)} cannot be used: {problem}"
    errors = check_record("pass_record", data)
    if errors:
        return None, f"{rel_path(project.root, path)} is not a valid pass_record ({errors[0]})"
    if data["verdict"] != "pass":
        return None, f"run '{run_id}' did not pass ({rel_path(project.root, path)} says {data['verdict']})"
    return data, ""


def open_entry(record: dict) -> dict:
    """What `open` reports of a pass record. `recorded` is false for a record from before 0.5.0, which carries no lists: an empty
    list there means nothing was kept, not that nothing was left."""
    return {
        "run_id": record["run_id"], "commit": record["commit"], "row": record["row"], "recorded": "open_findings" in record and "gaps" in record,
        "open_findings": record.get("open_findings", []), "gaps": record.get("gaps", []),
    }


def open_lines(entry: dict) -> list[str]:
    head = f"run {entry['run_id']} (commit {entry['commit'][:7]}, row {entry['row']})"
    if not entry["recorded"]:
        return [f"{head}: not recorded (the pass record is from before 0.5.0 and keeps no open findings)"]
    findings, gaps = entry["open_findings"], entry["gaps"]
    if not findings and not gaps:
        return [f"{head}: nothing open"]
    lines = [f"{head}: {_plural(len(findings), 'open finding')}, {_plural(len(gaps), 'gap')}"]
    if findings:
        lines.append("  findings:")
        for f in findings:
            lines.append(f"    {f['stage']}/{f['id']} [{f['severity']}] {f['file']}:{f['line']}: {_one_line(f['claim'])}")
            lines.append(f"      failure: {_one_line(f['failure_scenario'])}")
    if gaps:
        lines.append("  gaps:")
        for g in gaps:
            lines.append(f"    {g['stage']}/{g['id']} ({g['kind']}{', ' + g['ac'] if g['ac'] else ''}): {_one_line(g['detail'])}")
    return lines


def cmd_open(args) -> int:
    """List what passed runs left open. The run is the one named, else the one whose pass record covers HEAD; `--all` lists every passed run."""
    project = _ready_project(args.project)
    root = project.root
    if args.all and args.run_id:
        raise PlumblineError("--all lists every passed run, so it takes no RUN")
    entries = []
    if args.all:
        found = []  # (when the pass record was written, run id, record): the oldest run first
        try:
            candidates = [d.name for d in (root / RUNS_DIR).iterdir() if d.is_dir() and RUN_ID_PATTERN.fullmatch(d.name)]
        except OSError:
            candidates = []
        for name in candidates:
            record, _why = run_pass_record(project, name)
            if record is not None:
                found.append((run_pass_path(project, name).stat().st_mtime, name, record))
        entries = [open_entry(record) for _when, _name, record in sorted(found, key=lambda item: item[:2])]
    elif args.run_id:
        existing_run_dir(root, args.run_id)
        record, why = run_pass_record(project, check_run_id(args.run_id))
        if record is None:
            print(f"plumbline: {why}. Nothing to list.", file=sys.stderr)
            return 1
        entries = [open_entry(record)]
    else:
        head = head_sha(root)
        how, detail = coverage(root, head, project)
        if how != "pass":
            print(
                f"plumbline: HEAD {head[:7]} is not covered by a pass record ({detail}), so there is no run to list. "
                "Name a run (`open RUN`) or list every passed run (`open --all`). Nothing to list.",
                file=sys.stderr,
            )
            return 1
        record, _problem = load_json_file(root / PASS_DIR / f"{head}.json")
        entries = [open_entry(record)]
    if args.json:
        print(json.dumps({"runs": entries}, indent=2))
    elif not entries:
        print("no run has a pass record")
    else:
        for entry in entries:
            for line in open_lines(entry):
                print(line)
    return 0


def cmd_override(args) -> int:
    project = _adopted_project(args.project)
    reason = (sys.stdin.read() if args.reason == "-" else args.reason).strip()  # `-`: the reason arrives on stdin, so that no quoting can alter it
    if len(reason) < 20:
        print(f"plumbline: the reason must be at least 20 characters (this one has {len(reason)}); nothing was written", file=sys.stderr)
        return 1
    record = make_override(project, reason, args.run_id)
    path = project.root / PASS_DIR / f"{record['commit']}.override.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(record, indent=2) + "\n")
    except FileExistsError:
        print(f"plumbline: {rel_path(project.root, path)} already exists; an override is never overwritten. Nothing was changed.", file=sys.stderr)
        return 1
    print(f"plumbline: override recorded for {record['commit'][:7]} in {rel_path(project.root, path)}")
    print(f"  pushes of this commit are no longer held back. By {record['by']}. Stages skipped: {', '.join(record['stages_skipped']) or 'none'}")
    return 0


class OnIndexCopy:
    """This module, with its git commands working on a copy of the index: `git diff` refreshes and rewrites the index it reads,
    so the commit checks (which live in the hook and take the module as an argument) run on a copy: `check-diff` and the hook's
    check of a `git commit` both hand `commit_problems` this stand-in, and it uses nothing of the module but `_git`."""

    def __init__(self, index: Path) -> None:
        self._index = index

    def _git(self, root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return _git(root, *args, timeout=timeout, index=self._index)

    def __getattr__(self, name: str):
        return getattr(sys.modules[__name__], name)


def cmd_check_diff(args) -> int:
    root = resolve_root(args.project, require_git=True)
    project = run = None
    if args.run_id:
        project = _ready_project(args.project)
        run = load_run(project, args.run_id)
        merge_base = run.merge_base  # the change is measured from where the run began
    else:
        _base, merge_base, _notes = resolve_base(root, args.base)
    digest = change_hash(root, merge_base)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pre_tool_use  # the commit checks live with the hook that applies them at commit time

    with index_copy(root) as index:
        problems = pre_tool_use.commit_problems(OnIndexCopy(index), root, "all", base=merge_base)
    kinds = {"symlinks": "adds a symlink", "abs_paths": "adds an absolute home path", "secrets": "adds a key-shaped secret"}
    checks = {name: not any(problem.startswith(prefix) for problem in problems) for name, prefix in kinds.items()}
    report = {"merge_base": merge_base, "diff_sha256": digest, "checks": checks, "problems": problems}
    if run is not None:  # the change is measured again: its row against the row the run was declared with, and the tests of a refactor
        measured, measure_problems = measure_row(project, run)
        report["row"] = {"declared": run.row, "measured": measured["row"] if measured else None, "missing_stages": missing_stages(project, run, measured)}
        if not any(s["record"] == "pass_record" for s in run.stages):
            problems.append(f"this row ends before reduce: split the change (row {run.row} has no reduce stage)")
        problems += measure_problems + row_problems(project, run, measured) + tests_touched_problems(project, run, measured)
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


def _stage_state(project: Project, run_id: str, stage: dict) -> tuple[str, list[str]]:
    """A stage's state for `status`, judged afresh and without writing anything."""
    if stage["record"] == "pass_record":
        data, _ = read_stage_record(project.root, run_id, stage)
        return ("recorded" if data is not None else "pending"), []
    outcome = evaluate_stage(project, run_id, stage)
    if outcome.data is None:
        return ("missing" if outcome.problems[0].endswith(": no such file") else "invalid"), outcome.problems[:1]
    if outcome.gate is None:
        return "recorded", []
    return ("pass" if outcome.passed else "FAIL"), outcome.problems[:3]


def cmd_status(args) -> int:
    project = _ready_project(args.project)
    root = project.root
    if project.adopted:
        print(f"plumbline: adopted in {root} (pipeline '{project.pipeline['name']}', graft {'on' if project.graft_enabled else 'off'})")
    else:
        print(f"plumbline: not adopted in {root} (there is no {CONFIG_FILE}); /plumbline:init adopts it")
    try:
        head = head_sha(root)
    except PlumblineError as exc:
        print(f"HEAD: {exc}")
    else:
        how, detail = coverage(root, head, project)
        if how == "pass":
            print(f"HEAD {head[:7]}: covered by a pass record ({detail})")
            record, _problem = load_json_file(root / PASS_DIR / f"{head}.json")
            summary = open_summary(record) if isinstance(record, dict) else None
            if summary:
                print(summary)
        elif how == "override":
            print(f"HEAD {head[:7]}: covered by an override ({detail})")
        else:
            print(f"HEAD {head[:7]}: NOT covered ({detail})")
    run_id = check_run_id(args.run_id) if args.run_id else active_run_id(root)
    if run_id is None:
        print("latest run: none")
        return 0
    try:
        run = load_run(project, run_id)
    except PlumblineError as exc:
        print(f"run {run_id}: {exc}")
        return 0
    print(f"run {run_id}: row {run.row}, {_plural(len(run.stages), 'stage')}, intent {run.intent}")
    supplied_ids = {s["id"] for s in run.supplied}
    order = {s["id"]: position for position, s in enumerate(project.pipeline["stage"])}
    for stage in sorted([*run.supplied, *run.stages], key=lambda s: order.get(s["id"], len(order))):
        state, problems = _stage_state(project, run_id, stage)
        if stage["id"] in supplied_ids and state in ("pass", "recorded"):
            state = "supplied"
        print(f"  {stage['id']:<13}{stage['record']:<15}{state:<10}{stage.get('gate') or '-'}")
        for problem in problems:
            print(f"      {problem}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plumbline", description="The plumbline pipeline CLI.", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("validate-pipeline", allow_abbrev=False, help="validate the default pipeline, or FILE, merged with the repo's plumbline.toml")
    p.add_argument("file", nargs="?", help="a pipeline file (default: the plugin's pipeline/default.toml)")
    p.add_argument("--project", metavar="PATH", help="a repository whose plumbline.toml is merged in")
    p.set_defaults(run=cmd_validate_pipeline)

    p = sub.add_parser("classify", allow_abbrev=False, help="write the change_class record for the current change")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--base", metavar="REF", help="default: the remote's default branch, else origin/main, else main")
    p.add_argument("--intent", metavar="ID", default=DEFAULT_INTENT, help="why the change is made; the record carries it (default: feature)")
    p.add_argument("--row", metavar="ROW", help="declare the row (for example code.M) instead of measuring it: needed when nothing has changed yet")
    p.add_argument("--out", metavar="FILE", help="write the record here instead of to stdout")
    p.set_defaults(run=cmd_classify)

    p = sub.add_parser("plan", allow_abbrev=False, help="print the stages to run for the current change, as JSON; with --intent, start the run (in an adopted repository) and name it in .plumbline/runs/ACTIVE")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--base", metavar="REF")
    p.add_argument("--run-id", metavar="ID", help="default: <short HEAD sha>-<UTC timestamp>")
    p.add_argument("--intent", metavar="ID", help="start the run with this intent: writes its intake record and copies the record the intent supplies")
    p.add_argument("--spec", metavar="FILE", help="the spec that an intent which supplies one (spec-supplied, fix) takes as the plan record")
    p.add_argument("--row", metavar="ROW", help="declare the row (for example code.M) instead of measuring it: needed when nothing has changed yet")
    p.set_defaults(run=cmd_plan)

    p = sub.add_parser("check-record", allow_abbrev=False, help="validate a record file against its schema")
    p.add_argument("type", help="a record type: one of the files in schemas/")
    p.add_argument("file")
    p.set_defaults(run=cmd_check_record)

    p = sub.add_parser("render", allow_abbrev=False, help="print a record as markdown")
    p.add_argument("file")
    p.add_argument("--type", help="the record type (default: inferred from the record's keys)")
    p.set_defaults(run=cmd_render)

    p = sub.add_parser("init", allow_abbrev=False, help="adopt plumbline in a repository: write plumbline.toml, ignore .plumbline/")
    p.add_argument("--project", metavar="PATH")
    p.add_argument("--graft", action="store_true", help="record graft as enabled")
    p.set_defaults(run=cmd_init)

    p = sub.add_parser("merge-review", allow_abbrev=False, help="build a review stage's record from its per-agent records, applying the survival rule and checking quotes against the change")
    p.add_argument("run_id", metavar="RUN")
    p.add_argument("stage")
    p.add_argument("--round", type=int, metavar="N", help="default: the latest round directory")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_merge_review)

    p = sub.add_parser("gate", allow_abbrev=False, help="evaluate a stage's gate: exit 0 if it passes, 1 if not, 3 when the stage has used its rounds (a verify or tests stage runs the repository's [commands] itself)")
    p.add_argument("run_id", metavar="RUN")
    p.add_argument("stage")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_gate)

    p = sub.add_parser("tokens", allow_abbrev=False, help="print the token usage of a run's agents, per model, as JSON")
    p.add_argument("run_id", metavar="RUN")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_tokens)

    p = sub.add_parser("pass", allow_abbrev=False, help="write the pass record for HEAD, if the tree is clean, the change measures as no larger a row than the run has, and every gate of the run passed")
    p.add_argument("run_id", metavar="RUN")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_pass)

    p = sub.add_parser("open", allow_abbrev=False, help="list what passed runs left open: the surviving non-blocking findings and the detective's gaps (default: the run whose pass record covers HEAD)")
    p.add_argument("run_id", nargs="?", metavar="RUN", help="a passed run (default: the run that covers HEAD)")
    p.add_argument("--all", action="store_true", help="every passed run, oldest first")
    p.add_argument("--json", action="store_true", help="print the runs as JSON")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_open)

    p = sub.add_parser("override", allow_abbrev=False, help="record an override for HEAD, so that it can be pushed without a pass (only when the builder asks)")
    p.add_argument("--reason", required=True, metavar="TEXT", help="why the pipeline is bypassed: at least 20 characters (`-` reads it from stdin)")
    p.add_argument("--run", dest="run_id", metavar="RUN", help="the run whose unfinished stages are listed (default: the latest)")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_override)

    p = sub.add_parser("status", allow_abbrev=False, help="show the latest run, its stages and gates, and whether HEAD is covered")
    p.add_argument("--run", dest="run_id", metavar="RUN", help="default: the latest run")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_status)

    p = sub.add_parser("check-diff", allow_abbrev=False, help="run the commit checks on the whole change and print its hash, as JSON (with --run, the row it measures as too): exit 1 when a check fails")
    p.add_argument("--run", dest="run_id", metavar="RUN", help="measure the change from where this run began (default: from the merge base with the base)")
    p.add_argument("--base", metavar="REF", help="default: the remote's default branch, else origin/main, else main")
    p.add_argument("--project", metavar="PATH")
    p.set_defaults(run=cmd_check_diff)
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
