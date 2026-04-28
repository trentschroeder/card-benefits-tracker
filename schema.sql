CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    last_four   TEXT,
    annual_fee  REAL,
    owner_email TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS benefits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id         INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    description     TEXT,
    credit_amount   REAL,
    period_type     TEXT    NOT NULL CHECK(period_type IN ('monthly','quarterly','semi-annual','annual')),
    is_subscription INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    benefit_id  INTEGER NOT NULL,
    days_before INTEGER NOT NULL,
    UNIQUE(benefit_id, days_before),
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS redemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    benefit_id  INTEGER NOT NULL,
    period_start DATE    NOT NULL,
    amount      REAL,
    notes       TEXT,
    redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    benefit_id  INTEGER NOT NULL,
    period_start DATE    NOT NULL,
    days_before INTEGER NOT NULL,
    sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(benefit_id, period_start, days_before),
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE INDEX IF NOT EXISTS idx_benefits_card        ON benefits(card_id);
CREATE INDEX IF NOT EXISTS idx_redemptions_benefit  ON redemptions(benefit_id, period_start);
CREATE INDEX IF NOT EXISTS idx_reminders_benefit    ON reminders(benefit_id);
CREATE INDEX IF NOT EXISTS idx_sent_reminders       ON sent_reminders(benefit_id, period_start);
