CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash      TEXT    NOT NULL,
    is_admin           INTEGER NOT NULL DEFAULT 0,
    notification_email TEXT,
    reminders_enabled  INTEGER NOT NULL DEFAULT 1,
    summary_enabled    INTEGER NOT NULL DEFAULT 1,
    -- Account linking: two accounts that link share one wallet (cards, benefits,
    -- redemptions, offers). NULL = solo. Both linked users carry the same group id.
    link_group_id      INTEGER REFERENCES account_link_groups(id) ON DELETE SET NULL,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- A pair of linked accounts. Membership lives on users.link_group_id; this table
-- just allocates the shared id (mirrors the card_share_groups idiom one level up).
CREATE TABLE IF NOT EXISTS account_link_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    inviter_user_id INTEGER,
    card_id         INTEGER,
    token_hash      TEXT    NOT NULL UNIQUE,
    purpose         TEXT    NOT NULL DEFAULT 'invite',
    expires_at      TIMESTAMP NOT NULL,
    used_at         TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id)         REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (inviter_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (card_id)         REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS card_share_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    card_id         INTEGER NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    share_group_id  INTEGER,
    nickname        TEXT,
    assigned_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- No UNIQUE(user_id, card_id): a user may hold multiple instances of the
    -- same catalog card with separate redemption tracking.
    FOREIGN KEY (user_id)        REFERENCES users(id)              ON DELETE CASCADE,
    FOREIGN KEY (card_id)        REFERENCES cards(id)              ON DELETE CASCADE,
    FOREIGN KEY (share_group_id) REFERENCES card_share_groups(id)  ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS card_share_members (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    user_card_id INTEGER NOT NULL,
    joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, user_card_id),
    FOREIGN KEY (group_id)     REFERENCES card_share_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (user_card_id) REFERENCES user_cards(id)        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_benefits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_card_id INTEGER NOT NULL,
    benefit_id   INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_card_id, benefit_id),
    FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_card_id INTEGER NOT NULL,
    benefit_id   INTEGER NOT NULL,
    days_before  INTEGER NOT NULL,
    UNIQUE(user_card_id, benefit_id, days_before),
    FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
);

-- Template-level reminder defaults: when a user adds a card, each active
-- benefit's default reminders are copied into the per-instance reminders table
-- so the user starts with a sensible schedule they can then tweak.
CREATE TABLE IF NOT EXISTS benefit_default_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    benefit_id   INTEGER NOT NULL,
    days_before  INTEGER NOT NULL,
    UNIQUE(benefit_id, days_before),
    FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS redemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_card_id INTEGER NOT NULL,
    benefit_id   INTEGER NOT NULL,
    period_start DATE    NOT NULL,
    amount       REAL,
    notes        TEXT,
    redeemed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_card_id INTEGER NOT NULL,
    benefit_id   INTEGER NOT NULL,
    period_start DATE    NOT NULL,
    days_before  INTEGER NOT NULL,
    sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_card_id, benefit_id, period_start, days_before),
    FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Users can request a card that isn't in the catalog yet; admins see open
-- requests on the Card Templates page and mark them handled/dismissed.
CREATE TABLE IF NOT EXISTS card_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    card_name   TEXT    NOT NULL,
    notes       TEXT,
    status      TEXT    NOT NULL DEFAULT 'open',  -- open | done | dismissed
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Offers: gift cards / coupons / promotions a user wants to remember to use.
-- Unlike benefits these are personal (owned directly by a user, never shared)
-- and one-shot rather than recurring: an optional fixed expiration date drives
-- reminders, and once fully used an offer is archived (hidden) rather than reset.
CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    description     TEXT,
    amount          REAL,            -- optional starting value; NULL = track as used/not-used
    amount_used     REAL    NOT NULL DEFAULT 0,  -- cumulative redeemed against amount
    expiration_date DATE,            -- optional; required for date-based reminders
    archived        INTEGER NOT NULL DEFAULT 0,  -- 1 once fully used (hidden from UI)
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Per-offer reminder schedule: send a nudge this many days before expiration.
CREATE TABLE IF NOT EXISTS offer_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     INTEGER NOT NULL,
    days_before  INTEGER NOT NULL,
    UNIQUE(offer_id, days_before),
    FOREIGN KEY (offer_id) REFERENCES offers(id) ON DELETE CASCADE
);

-- Dedup log for offer reminders. An offer has a single fixed expiration (no
-- recurring period), so one row per (offer, days_before) threshold suffices.
CREATE TABLE IF NOT EXISTS offer_sent_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     INTEGER NOT NULL,
    days_before  INTEGER NOT NULL,
    sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(offer_id, days_before),
    FOREIGN KEY (offer_id) REFERENCES offers(id) ON DELETE CASCADE
);

-- Per-recipient reminder dedup. With account linking, BOTH linked users are
-- emailed about the same shared card/offer, so dedup must be keyed by recipient
-- (the older sent_reminders/offer_sent_reminders tables keyed only by the item
-- and so could not represent "sent to A but not yet to B"). These supersede them.
CREATE TABLE IF NOT EXISTS reminder_sends (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_user_id INTEGER NOT NULL,
    user_card_id      INTEGER NOT NULL,
    benefit_id        INTEGER NOT NULL,
    period_start      DATE    NOT NULL,
    days_before       INTEGER NOT NULL,
    sent_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recipient_user_id, user_card_id, benefit_id, period_start, days_before),
    FOREIGN KEY (recipient_user_id) REFERENCES users(id)      ON DELETE CASCADE,
    FOREIGN KEY (user_card_id)      REFERENCES user_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (benefit_id)        REFERENCES benefits(id)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS offer_reminder_sends (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_user_id INTEGER NOT NULL,
    offer_id          INTEGER NOT NULL,
    days_before       INTEGER NOT NULL,
    sent_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recipient_user_id, offer_id, days_before),
    FOREIGN KEY (recipient_user_id) REFERENCES users(id)   ON DELETE CASCADE,
    FOREIGN KEY (offer_id)          REFERENCES offers(id)  ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reminder_sends ON reminder_sends(recipient_user_id, user_card_id, benefit_id, period_start);
CREATE INDEX IF NOT EXISTS idx_offer_reminder_sends ON offer_reminder_sends(recipient_user_id, offer_id);
CREATE INDEX IF NOT EXISTS idx_offers_user   ON offers(user_id, archived);
CREATE INDEX IF NOT EXISTS idx_offer_reminders ON offer_reminders(offer_id);
CREATE INDEX IF NOT EXISTS idx_benefits_card   ON benefits(card_id);
CREATE INDEX IF NOT EXISTS idx_benefit_default_reminders ON benefit_default_reminders(benefit_id);
CREATE INDEX IF NOT EXISTS idx_user_cards_user ON user_cards(user_id, active);
-- Indexes on the per-instance tables (redemptions/reminders/sent_reminders)
-- are created by _ensure_user_scoped_indexes after the migration finishes,
-- because on existing dbs the user_card_id column is added mid-migration.
