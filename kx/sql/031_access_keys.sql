BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- The agent mode grows a subscription, and a subscription needs a key
--
-- Decision (owner, 2026-08-24): the conversation stays free, and browsing the
-- base - the tabs, the walking from node to node - is for subscribers. No
-- accounts, on the owner's standing decision: a subscriber holds a key, the key
-- is a revocable capability, and the base stores only its SHA-256. The full key
-- exists once, at issuance, in the owner's editor and the subscriber's letter.
--
-- `key_prefix` is the first characters of the key, kept so the owner can tell
-- two keys apart in a list without ever seeing the keys again. `plan` is text,
-- not an enum: v1 has one plan, and a CHECK would freeze a pricing idea into
-- the schema before the pricing exists.
-- ---------------------------------------------------------------------------

CREATE TABLE kx.access_keys (
    key_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_prefix text NOT NULL,
    key_hash text NOT NULL UNIQUE,
    plan text NOT NULL DEFAULT 'full',
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    expires_at timestamptz NOT NULL,
    note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    last_used_at timestamptz
);

COMMENT ON TABLE kx.access_keys IS
    'Subscription access keys. Only the SHA-256 hash is stored; the prefix is '
    'for the owner to recognise a key in a list. A key is a capability, not an '
    'identity: revocation is a status flip, and nothing about a subscriber is '
    'recorded beyond the note the owner writes.';

CREATE INDEX access_keys_status_idx ON kx.access_keys (status, expires_at);

-- The serving role reads the table to answer "is this key live" - nothing else
-- of the key exists to read. Writes stay with the editor's role.
GRANT SELECT ON kx.access_keys TO radar_kb_public;

COMMIT;
