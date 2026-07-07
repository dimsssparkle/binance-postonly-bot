CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    desired_side    TEXT NOT NULL,
    qty             TEXT NOT NULL,
    state           TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL DEFAULT 0,
    entry_price     TEXT,
    failure_reason  TEXT,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_intents_active_symbol
    ON intents(symbol) WHERE state NOT IN ('flat', 'failed');

CREATE TABLE IF NOT EXISTS intent_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id           INTEGER NOT NULL REFERENCES intents(id),
    role                TEXT NOT NULL,
    client_order_id     TEXT NOT NULL UNIQUE,
    exchange_order_id   INTEGER,
    side                TEXT NOT NULL,
    order_type          TEXT NOT NULL,
    requested_qty       TEXT,
    requested_price     TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    filled_qty          TEXT NOT NULL DEFAULT '0',
    commission          TEXT NOT NULL DEFAULT '0',
    commission_asset    TEXT,
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_intent_orders_intent ON intent_orders(intent_id);

CREATE TABLE IF NOT EXISTS events_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    intent_id       INTEGER,
    payload_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_events_log_intent ON events_log(intent_id);
CREATE INDEX IF NOT EXISTS ix_events_log_ts ON events_log(ts_ms);

CREATE TABLE IF NOT EXISTS listen_key_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    listen_key      TEXT,
    created_at_ms   INTEGER,
    last_renewed_ms INTEGER
);
