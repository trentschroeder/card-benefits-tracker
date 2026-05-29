CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash      TEXT    NOT NULL,
    is_admin           INTEGER NOT NULL DEFAULT 0,
    notification_email TEXT,
    reminders_enabled  INTEGER NOT NULL DEFAULT 1,
    summary_enabled    INTEGER NOT NULL DEFAULT 1,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    annual_fee  REAL,
    active      INTEGER NOT NULL DEFAULT 1,
    published   INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS benefits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id             INTEGER NOT NULL,
    name                TEXT    NOT NULL,
    description         TEXT,
    credit_amount       REAL,
    period_type         TEXT    NOT NULL CHECK(period_type IN ('monthly','quarterly','semi-annual','annual')),
    is_subscription     INTEGER NOT NULL DEFAULT 0,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS impersonation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        INTEGER NOT NULL,
    impersonated_id INTEGER NOT NULL,
    started_at      TIMESTAMP NOT NULL,
    stopped_at      TIMESTAMP,
    FOREIGN KEY (admin_id)        REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (impersonated_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invitations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    token_hash  TEXT    NOT NULL UNIQUE,
    purpose     TEXT    NOT NULL DEFAULT 'invite',
    expires_at  TIMESTAMP NOT NULL,
    used_at     TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_cards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    card_id      INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, card_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_benefits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    benefit_id  INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, benefit_id),
    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    benefit_id  INTEGER NOT NULL,
    days_before INTEGER NOT NULL,
    UNIQUE(user_id, benefit_id, days_before),
    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS redemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    benefit_id   INTEGER NOT NULL,
    period_start DATE    NOT NULL,
    amount       REAL,
    notes        TEXT,
    redeemed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    benefit_id   INTEGER NOT NULL,
    period_start DATE    NOT NULL,
    days_before  INTEGER NOT NULL,
    sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, benefit_id, period_start, days_before),
    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE INDEX IF NOT EXISTS idx_benefits_card   ON benefits(card_id);
CREATE INDEX IF NOT EXISTS idx_user_cards_user ON user_cards(user_id, active);
-- Indexes on redemptions/reminders/sent_reminders are created in init_db AFTER
-- the Phase 2 migration has added user_id to those tables on existing dbs.
