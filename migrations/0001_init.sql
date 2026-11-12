-- Tracking cell schema v1 (SPEC-003). Applied by `tracking-db migrate` with the migrator URL,
-- never at app start. Column names follow the web's former Prisma models so the import is 1:1.
CREATE SCHEMA IF NOT EXISTS tracking;

CREATE TABLE IF NOT EXISTS tracking.items (
    id              text PRIMARY KEY,
    title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    description     text CHECK (description IS NULL OR length(description) <= 10000),
    image_url       text CHECK (image_url IS NULL OR length(image_url) <= 2000),
    external_url    text CHECK (external_url IS NULL OR length(external_url) <= 2000),
    category        text NOT NULL CHECK (category IN
                    ('ANIME','MANGA','MOVIE','TV_SHOW','BOOK','WEBSITE','PODCAST','GAME','OTHER')),
    author          text CHECK (author IS NULL OR length(author) <= 500),
    year            integer,
    genres          text[] NOT NULL DEFAULT '{}',
    total_episodes  integer,
    total_chapters  integer,
    total_books     integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT title_category UNIQUE (title, category)
);
CREATE INDEX IF NOT EXISTS items_category ON tracking.items (category);
CREATE INDEX IF NOT EXISTS items_title_lower ON tracking.items (lower(title), category);

CREATE TABLE IF NOT EXISTS tracking.user_tracking (
    id           text PRIMARY KEY,
    user_id      text NOT NULL,
    item_id      text NOT NULL REFERENCES tracking.items (id) ON DELETE CASCADE,
    status       text NOT NULL DEFAULT 'PLAN_TO_WATCH' CHECK (status IN
                 ('PLAN_TO_WATCH','WATCHING','COMPLETED','ON_HOLD','DROPPED')),
    rating       text CHECK (rating IS NULL OR rating IN
                 ('ONE','TWO','THREE','FOUR','FIVE','SIX','SEVEN','EIGHT','NINE','TEN')),
    progress     integer,
    start_date   timestamptz,
    finish_date  timestamptz,
    priority     integer,
    is_favorite  boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_item UNIQUE (user_id, item_id)
);
CREATE INDEX IF NOT EXISTS user_tracking_user ON tracking.user_tracking (user_id);
CREATE INDEX IF NOT EXISTS user_tracking_item ON tracking.user_tracking (item_id);

CREATE TABLE IF NOT EXISTS tracking.notes (
    id          text PRIMARY KEY,
    user_id     text NOT NULL,
    item_id     text REFERENCES tracking.items (id) ON DELETE CASCADE,
    content     text NOT NULL,
    is_private  boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notes_user ON tracking.notes (user_id);

CREATE TABLE IF NOT EXISTS tracking.activity_log (
    id          text PRIMARY KEY,
    user_id     text NOT NULL,
    action      text NOT NULL,
    item_title  text NOT NULL,
    category    text NOT NULL,
    details     text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activity_user ON tracking.activity_log (user_id, created_at);

-- AC-03: a replayed Idempotency-Key returns the first answer and writes nothing new.
CREATE TABLE IF NOT EXISTS tracking.idempotency (
    user_id     text NOT NULL,
    key         text NOT NULL,
    method      text NOT NULL,
    status      integer NOT NULL,
    response    jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
