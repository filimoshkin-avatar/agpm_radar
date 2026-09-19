"""Restricted installation of sidecar reports, independent of issue publication."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from packages.contracts.event_observation import encode, issue_hash, validate_report
from packages.storage.content_pointer import read_content_pointer
from packages.storage.mutation_lock import acquire_mutation_lock, release_mutation_lock
from packages.storage.safe_files import (
    atomic_write_new,
    ensure_private_directory,
    read_regular_file,
)
from packages.validation.public_issue import build_public_issue


def install_observation(
    report: Any,
    *,
    content_root: Path,
    root: Path,
    api_uid: int,
    api_gid: int,
) -> dict[str, str]:
    report = validate_report(report)
    root.mkdir(mode=0o700, exist_ok=True)
    os.chown(root, api_uid, api_gid)
    lock = acquire_mutation_lock(root)
    try:
        pointer = read_content_pointer(content_root)
        with sqlite3.connect(f"file:{pointer.database_path}?mode=ro", uri=True) as connection:
            issue = build_public_issue(connection, issue_date=report["issueDate"])
        if issue_hash(issue) != report["issueHash"]:
            raise ValueError("observation targets a different published issue version")
        # Root is fixed by the activator, never supplied by the deploy request.
        root.mkdir(mode=0o700, exist_ok=True)
        os.chown(root, api_uid, api_gid)
        ensure_private_directory(root)
        archive = root / f"{report['reportId']}.report.json"
        content = encode(report)
        if archive.exists():
            if read_regular_file(archive, expected_mode=0o600) != content:
                raise ValueError("immutable observation differs")
        else:
            atomic_write_new(archive, content, mode=0o600)
            os.chown(archive, api_uid, api_gid)
        target = root / f"{report['issueHash']}.json"
        if target.exists():
            previous = validate_report(json.loads(read_regular_file(target, expected_mode=0o600)))
            if previous["generatedAt"] > report["generatedAt"]:
                raise ValueError("older observation cannot replace a newer report")
        temporary = root / f"{report['issueHash']}.pending"
        if temporary.exists():
            temporary.unlink()
        atomic_write_new(temporary, content, mode=0o600)
        os.chown(temporary, api_uid, api_gid)
        os.replace(temporary, target)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {"status": "stored", "reportId": report["reportId"]}
    finally:
        release_mutation_lock(lock)
