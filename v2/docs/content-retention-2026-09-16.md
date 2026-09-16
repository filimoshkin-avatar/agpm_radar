# Radar V2 content retention

## Decision

Full immutable SQLite copies are retained for seven days, with at least seven newest
releases on each content endpoint. Small audit, request, delta and manifest evidence is
not rotated by this policy.

The source and production jobs run after the daily Stage 15 publication window. They
share the existing mutation lock with the publisher/activator and therefore fail closed
instead of racing a publication.

## Protected state

The rotator never removes:

- the database selected by \`active.json\`;
- the newest seven release databases;
- releases younger than seven days;
- source databases referenced by unresolved publisher requests;
- production base/target databases referenced by unresolved remote requests;
- incomplete or failed staging databases without a successful audit result.

If \`NEEDS_RECONCILIATION\` exists on production, no production file is removed.
Every deletion revalidates device, inode, size, mtime, regular-file type and single-link
status under a pinned parent directory. The active pointer and SQLite identity are
verified before and after an applying pass.

## Operations

Dry-run is the default. \`--apply\` is present only in the installed systemd services.
The JSON result records the exact candidates and byte count in journald.

Source host:

\`\`\`text
radar-v2-source-retention.timer -> 06:30 UTC daily
\`\`\`

Production host:

\`\`\`text
radar-v2-production-retention.timer -> 06:40 UTC daily
\`\`\`

Rollback is disabling the timer. Deleted historical full copies are not recreated by
rollback; recovery uses the retained active/recent releases and the current source/OpenClaw
backup mirror. This policy intentionally limits point-in-time release recovery to the
retention window; Radar V2 does not currently have a separate recurring production backup timer.
