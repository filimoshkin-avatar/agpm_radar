#!/usr/bin/env python3
"""Validate Radar V2 Stage 1 machine-readable contracts and examples."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts" / "v1"
EXAMPLES = CONTRACTS / "examples"

SCHEMAS = {
    "llm-outcome": CONTRACTS / "llm-outcome.schema.json",
    "candidate": CONTRACTS / "candidate.schema.json",
    "delta": CONTRACTS / "delta.schema.json",
    "publisher-result": CONTRACTS / "publisher-result.schema.json",
    "project-manager-report": CONTRACTS / "project-manager-report.schema.json",
    "compatibility-manifest": CONTRACTS / "compatibility-manifest.schema.json",
}

EXAMPLE_SCHEMAS = {
    "candidate-daily-no-llm.json": "candidate",
    "candidate-correction.json": "candidate",
    "candidate-gazette.json": "candidate",
    "delta-daily.json": "delta",
    "publisher-result-published-no-llm.json": "publisher-result",
    "publisher-result-rolled-back.json": "publisher-result",
    "project-manager-report-published-no-llm.json": "project-manager-report",
    "compatibility-manifest.json": "compatibility-manifest",
}

HOST_PATH = re.compile(r"(?:^|[\s'\"])/(?:root|mnt|etc|srv|opt|var)(?:/|$)")
FORBIDDEN_KEYS = {
    "sql", "ddl", "command", "shell", "password", "secret", "token",
    "authorization", "api_key", "apikey", "request_path", "response_path",
    "docx_source_path", "md_source_path", "report_md_path", "report_docx_path",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise AssertionError(message)


def canonical_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")


def validate_no_host_paths_or_executable_keys(path: Path, value: Any) -> None:
    for location, item in walk(value):
        if isinstance(item, dict):
            for key in item:
                if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
                    fail(f"{path}: forbidden executable/secret/path key at {location}.{key}")
        elif isinstance(item, str) and HOST_PATH.search(item):
            fail(f"{path}: absolute host path at {location}: {item!r}")


def validate_json_schemas() -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    checker = FormatChecker()
    for name, path in SCHEMAS.items():
        schema = load_json(path)
        Draft202012Validator.check_schema(schema)
        loaded[name] = schema
    store = {schema.get("$id"): schema for schema in loaded.values() if schema.get("$id")}
    store["https://radar.aipractice.space/contracts/v1/llm-outcome.schema.json"] = loaded["llm-outcome"]
    for filename, schema_name in EXAMPLE_SCHEMAS.items():
        example_path = EXAMPLES / filename
        example = load_json(example_path)
        resolver = RefResolver.from_schema(loaded[schema_name], store=store)
        errors = sorted(
            Draft202012Validator(loaded[schema_name], format_checker=checker, resolver=resolver).iter_errors(example),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            details = "\n".join(
                f"  {list(error.absolute_path)}: {error.message}" for error in errors
            )
            fail(f"{filename} does not validate against {schema_name}:\n{details}")
        validate_no_host_paths_or_executable_keys(example_path, example)
    return loaded


def validate_negative_examples(loaded: dict[str, Any]) -> None:
    import copy
    checker = FormatChecker()
    store = {schema.get("$id"): schema for schema in loaded.values() if schema.get("$id")}
    def must_fail(schema_name: str, value: Any, label: str) -> None:
        resolver = RefResolver.from_schema(loaded[schema_name], store=store)
        if not list(Draft202012Validator(loaded[schema_name], format_checker=checker, resolver=resolver).iter_errors(value)):
            fail(f"negative case unexpectedly validated: {label}")
    daily = load_json(EXAMPLES / "candidate-daily-no-llm.json")
    bad = copy.deepcopy(daily); bad["sql"] = "DROP TABLE issues"; must_fail("candidate", bad, "candidate unknown executable field")
    bad = copy.deepcopy(daily); bad["desiredIssue"]["materials"] = [{"materialId":"material_bad","position":1,"title":"x","url":"javascript:alert(1)","canonicalUrl":None,"sourceName":None,"publishedAt":None,"publicationDateStatus":"unresolved","perimeter":"far","verdict":"adjacent","summary":None,"agpmTakeaway":None,"brief":None,"keyMaterial":False,"signalScore":None,"signalStrength":"watch","theses":[],"trendNotes":None,"flags":[],"rubrics":[],"llmStatus":"unavailable","llmShortText":None,"llmAgpmAngle":None}]; must_fail("candidate", bad, "unsafe material URL")
    delta = load_json(EXAMPLES / "delta-daily.json"); bad = copy.deepcopy(delta); bad["operations"][0]["values"]["unknown_column"] = "x"; must_fail("delta", bad, "delta unknown column")
    result = load_json(EXAMPLES / "publisher-result-published-no-llm.json"); bad = copy.deepcopy(result); bad["publicationSucceeded"] = False; must_fail("publisher-result", bad, "contradictory published result")
    report = load_json(EXAMPLES / "project-manager-report-published-no-llm.json"); bad = copy.deepcopy(report); bad["publicationSucceeded"] = False; must_fail("project-manager-report", bad, "contradictory published report")


def validate_sqlite_contract() -> dict[str, Any]:
    path = CONTRACTS / "sqlite-contract.yaml"
    contract = load_yaml(path)
    if contract["contractVersion"] != "1.0.0":
        fail("sqlite contract version mismatch")
    if contract["sqlite"]["requiredRuntimeVersion"] != "3.45.1":
        fail("SQLite runtime must be pinned to 3.45.1 in v1")
    if "ENABLE_FTS5" not in contract["sqlite"]["requiredCompileOptions"]:
        fail("FTS5 requirement missing")
    tables = contract["tables"]
    mutation_tables = set(contract["contentMutationTables"])
    derived = set(contract["derivedTables"])
    if mutation_tables & derived:
        fail("derived tables cannot be content mutation tables")
    for table_name, table in tables.items():
        columns = set(table["columns"])
        primary_key = table["primaryKey"]
        if not primary_key or not set(primary_key) <= columns:
            fail(f"{table_name}: invalid primary key")
        mutations = set(table.get("contentMutations", []))
        if not mutations <= {"insert", "upsert", "delete"}:
            fail(f"{table_name}: invalid mutation action")
        if bool(mutations) != (table_name in mutation_tables):
            fail(f"{table_name}: contentMutationTables mismatch")
        for foreign_key in table.get("foreignKeys", []):
            if not set(foreign_key["columns"]) <= columns:
                fail(f"{table_name}: invalid local foreign key columns")
            referenced = foreign_key["references"]
            if referenced not in tables:
                fail(f"{table_name}: unknown referenced table {referenced}")
            if not set(foreign_key["referencedColumns"]) <= set(tables[referenced]["columns"]):
                fail(f"{table_name}: invalid referenced columns")
    validate_no_host_paths_or_executable_keys(path, contract)
    return contract


def validate_mutation(mutation: dict[str, Any], sqlite_contract: dict[str, Any], *, candidate: bool) -> None:
    table_name = mutation["table"]
    table = sqlite_contract["tables"].get(table_name)
    if table is None or table_name not in sqlite_contract["contentMutationTables"]:
        fail(f"unknown/non-mutable table: {table_name}")
    if candidate and table_name == "content_releases":
        fail("Project Manager candidate cannot author content_releases")
    action = mutation["action"]
    if action not in table["contentMutations"]:
        fail(f"{table_name}: action {action} not allowed")
    if set(mutation["key"]) != set(table["primaryKey"]):
        fail(f"{table_name}: key must exactly match primary key")
    unknown_key_columns = set(mutation["key"]) - set(table["columns"])
    if unknown_key_columns:
        fail(f"{table_name}: unknown key columns {sorted(unknown_key_columns)}")
    values = mutation.get("values")
    if action == "delete":
        if values is not None:
            fail(f"{table_name}: delete cannot carry values")
    else:
        if not isinstance(values, dict):
            fail(f"{table_name}: mutation requires values")
        unknown = set(values) - set(table["columns"])
        if unknown:
            fail(f"{table_name}: unknown value columns {sorted(unknown)}")
        for key_column, key_value in mutation["key"].items():
            if key_column in values and values[key_column] != key_value:
                fail(f"{table_name}: key/value mismatch for {key_column}")


def validate_llm_outcome(outcome: dict[str, Any], context: str) -> None:
    attempts = outcome["attempts"]
    if [item["order"] for item in attempts] != list(range(1, len(attempts) + 1)):
        fail(f"{context}: LLM attempts must be contiguous and ordered")
    accepted = [item for item in attempts if item["accepted"]]
    if outcome["status"] == "success":
        if len(accepted) != 1 or accepted[0]["order"] != 1:
            fail(f"{context}: success must accept requested first attempt")
    elif outcome["status"] == "fallback":
        if len(accepted) != 1 or accepted[0]["order"] != outcome["effectiveAttemptOrder"] or accepted[0]["order"] == 1:
            fail(f"{context}: fallback must reference one accepted non-primary attempt")
    elif outcome["status"] == "unavailable":
        if accepted or outcome["deterministicFallback"] is None:
            fail(f"{context}: unavailable must have no accepted model and a deterministic fallback")
    elif outcome["status"] == "not_requested":
        if attempts:
            fail(f"{context}: not_requested cannot have attempts")


def validate_candidate_examples(sqlite_contract: dict[str, Any]) -> None:
    for path in sorted(EXAMPLES.glob("candidate-*.json")):
        candidate = load_json(path)
        validate_llm_outcome(candidate["llmOutcome"], path.name)
        operation = candidate["operation"]
        if operation == "daily":
            if candidate["desiredIssue"]["issueDate"] != candidate["snapshot"]["snapshotId"].split("_")[1][:4] + "-" + candidate["snapshot"]["snapshotId"].split("_")[1][4:6] + "-" + candidate["snapshot"]["snapshotId"].split("_")[1][6:8]:
                fail(f"{path.name}: daily snapshot/issue date mismatch")
        elif operation == "correction":
            if candidate["targetIssueDate"] != candidate["desiredIssue"]["issueDate"]:
                fail(f"{path.name}: correction target/desired issue mismatch")
        elif operation == "gazette":
            entry = candidate["htmlEntrypoint"]
            matches = [asset for asset in candidate["inputAssets"] if asset["relativePath"] == entry and asset["mediaType"] == "text/html"]
            if len(matches) != 1:
                fail(f"{path.name}: gazette requires exactly one HTML entrypoint asset")


def validate_delta_example(sqlite_contract: dict[str, Any]) -> None:
    path = EXAMPLES / "delta-daily.json"
    delta = load_json(path)
    if delta["targetSequence"] != delta["baseSequence"] + 1:
        fail("delta targetSequence must equal baseSequence + 1")
    sequences = [item["sequence"] for item in delta["operations"]]
    if sequences != list(range(1, len(sequences) + 1)):
        fail("delta operation sequences must be contiguous and ordered")
    for operation in delta["operations"]:
        validate_mutation(operation, sqlite_contract, candidate=False)
    markers = [operation for operation in delta["operations"] if operation["table"] == "content_releases" and operation["action"] == "insert"]
    if len(markers) != 1:
        fail("delta must contain exactly one immutable content release marker")
    marker = markers[0]["values"]
    if marker["release_id"] != delta["releaseId"] or marker["candidate_id"] != delta["candidateId"] or marker["sequence"] != delta["targetSequence"]:
        fail("delta release marker identity/sequence mismatch")
    expected_tables = [item["table"] for item in delta["expectedTables"]]
    if expected_tables != list(sqlite_contract["tables"]):
        fail("delta expectedTables must contain every replicated table exactly once in contract order")
    if delta["schemaVersionBefore"] != delta["schemaVersionAfter"]:
        fail("content delta cannot change schema")


def resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("#/"):
        fail(f"external/non-local OpenAPI reference is not allowed: {pointer}")
    value = document
    for part in pointer[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def validate_openapi() -> None:
    path = CONTRACTS / "public-api.openapi.yaml"
    api = load_yaml(path)
    if api.get("openapi") != "3.1.0":
        fail("OpenAPI must be 3.1.0")
    if "published-only" not in api["info"]["description"]:
        fail("OpenAPI must state published-only boundary")
    for api_path, item in api["paths"].items():
        if not api_path.startswith("/api/"):
            fail(f"non-API path in OpenAPI: {api_path}")
        if "/internal" in api_path or "draft" in api_path:
            fail(f"forbidden public path: {api_path}")
        methods = {key.lower() for key in item if key.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}}
        if methods != {"get"}:
            fail(f"{api_path}: only GET may be explicitly defined, got {methods}")
    for location, item in walk(api):
        if isinstance(item, dict) and "$ref" in item:
            resolve_pointer(api, item["$ref"])
        if isinstance(item, str) and item in {"request_path", "response_path", "docx_source_path", "md_source_path"}:
            fail(f"internal path field in OpenAPI at {location}")
    validate_no_host_paths_or_executable_keys(path, api)


def validate_state_and_errors() -> None:
    state_path = CONTRACTS / "publisher-state-machine.yaml"
    error_path = CONTRACTS / "error-taxonomy.yaml"
    state = load_yaml(state_path)
    taxonomy = load_yaml(error_path)
    states = state["states"]
    initial = state["initialState"]
    if initial not in states:
        fail("state machine initial state missing")
    graph: dict[str, set[str]] = {name: set() for name in states}
    for transition in state["transitions"]:
        if transition["from"] not in states or transition["to"] not in states:
            fail(f"invalid transition {transition}")
        graph[transition["from"]].add(transition["to"])
    reached = {initial}
    queue = deque([initial])
    while queue:
        current = queue.popleft()
        for target in graph[current]:
            if target not in reached:
                reached.add(target); queue.append(target)
    if reached != set(states):
        fail(f"unreachable publisher states: {sorted(set(states) - reached)}")
    for name, definition in states.items():
        if definition.get("terminal") and graph[name]:
            fail(f"terminal state {name} has outgoing transitions")
    exit_codes = state["exitCodes"]
    if len(set(exit_codes.values())) != len(exit_codes):
        fail("duplicate state-machine error symbols")
    for code, definition in taxonomy["errors"].items():
        if definition["exitCode"] not in exit_codes:
            fail(f"taxonomy {code}: exit code is absent from state machine")
        if exit_codes[definition["exitCode"]] != code:
            fail(f"taxonomy/state symbol mismatch for {code}")
    validate_no_host_paths_or_executable_keys(state_path, state)
    validate_no_host_paths_or_executable_keys(error_path, taxonomy)


def validate_historical_and_compatibility() -> None:
    inference_path = CONTRACTS / "historical-publication-inference.yaml"
    inference = load_yaml(inference_path)
    if inference["baseline"]["expectedPublicIssueCount"] != 74:
        fail("historical inference count must match frozen Stage 0 baseline")
    if inference["principles"]["legacyStatusIsAuthority"]:
        fail("Legacy status cannot be publication authority")
    validate_no_host_paths_or_executable_keys(inference_path, inference)


def main() -> int:
    loaded_schemas = validate_json_schemas()
    validate_negative_examples(loaded_schemas)
    sqlite_contract = validate_sqlite_contract()
    validate_candidate_examples(sqlite_contract)
    validate_delta_example(sqlite_contract)
    validate_openapi()
    validate_state_and_errors()
    validate_historical_and_compatibility()
    print("Radar V2 contracts validation: PASS")
    print(f"JSON schemas: {len(SCHEMAS)}")
    print(f"Examples: {len(EXAMPLE_SCHEMAS)}")
    print(f"SQLite tables: {len(sqlite_contract['tables'])}")
    print(f"Public API paths: {len(load_yaml(CONTRACTS / 'public-api.openapi.yaml')['paths'])}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        print(f"Contract validation FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
