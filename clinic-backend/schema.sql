-- Clinic AI Analyst: consolidated database schema (healthcare vertical).
--
-- Apply once to a fresh database as the owner role:
--     createdb clinic_demo
--     psql "postgresql://<owner>@localhost:5432/clinic_demo" -f schema.sql
--
-- Creates: core tables (sessions, events), tenant tables (doctors, patients,
-- appointments, prescriptions, lab_results) protected by row-level security,
-- operator tables (ip_quota, leads), and the non-privileged `demo_app` role
-- the API connects as. Row-level security plus a NOSUPERUSER/NOBYPASSRLS role
-- is the isolation spine: every tenant row is filtered by the app.session_id GUC.

BEGIN;

-- ============================================================
-- Core: sessions + events (operator tables, no RLS).
-- ============================================================

-- One row per demo visit.
CREATE TABLE IF NOT EXISTS sessions (
    id            UUID        PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vertical      TEXT        NOT NULL,
    referrer_tag  TEXT,
    country       TEXT,
    expires_at    TIMESTAMPTZ NOT NULL,
    ip            INET
);
CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions (expires_at);
CREATE INDEX IF NOT EXISTS sessions_ip_idx         ON sessions (ip);

-- Every question asked, SQL executed, error, and token-usage record.
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  UUID        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type        TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS events_session_id_ts_idx   ON events (session_id, ts);
CREATE INDEX IF NOT EXISTS events_session_type_ts_idx ON events (session_id, type, ts);

-- ============================================================
-- Tenant data (healthcare vertical). A clinic's doctors, patients,
-- appointments, prescriptions, and lab results. Every table is
-- session-scoped and RLS-isolated below.
-- ============================================================

CREATE TABLE IF NOT EXISTS doctors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    name            TEXT NOT NULL,
    specialty       TEXT NOT NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS doctors_session_idx            ON doctors (session_id);
CREATE INDEX IF NOT EXISTS doctors_session_specialty_idx  ON doctors (session_id, specialty);

CREATE TABLE IF NOT EXISTS patients (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id            UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    first_name            TEXT NOT NULL,
    last_name             TEXT NOT NULL,
    dob                   DATE,
    email                 TEXT,
    phone                 TEXT,

    insurance_provider    TEXT,
    insurance_member_id   TEXT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS patients_session_idx        ON patients (session_id);
CREATE INDEX IF NOT EXISTS patients_session_name_idx   ON patients (session_id, last_name);

CREATE TABLE IF NOT EXISTS appointments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    patient_id      UUID NOT NULL REFERENCES patients(id),
    doctor_id       UUID NOT NULL REFERENCES doctors(id),

    scheduled_at    TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('scheduled', 'completed', 'cancelled', 'no_show')),
    reason          TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS appointments_session_idx            ON appointments (session_id);
CREATE INDEX IF NOT EXISTS appointments_session_patient_idx    ON appointments (session_id, patient_id);
CREATE INDEX IF NOT EXISTS appointments_session_scheduled_idx  ON appointments (session_id, scheduled_at);
CREATE INDEX IF NOT EXISTS appointments_session_status_idx     ON appointments (session_id, status);

CREATE TABLE IF NOT EXISTS prescriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    patient_id          UUID NOT NULL REFERENCES patients(id),

    medication          TEXT NOT NULL,
    dosage              TEXT,
    refills_remaining   INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL CHECK (status IN ('active', 'expired', 'cancelled')),
    last_filled_on      DATE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS prescriptions_session_idx          ON prescriptions (session_id);
CREATE INDEX IF NOT EXISTS prescriptions_session_patient_idx  ON prescriptions (session_id, patient_id);
CREATE INDEX IF NOT EXISTS prescriptions_session_status_idx   ON prescriptions (session_id, status);

CREATE TABLE IF NOT EXISTS lab_results (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    patient_id        UUID NOT NULL REFERENCES patients(id),

    test_name         TEXT NOT NULL,
    value             TEXT,
    unit              TEXT,
    reference_range   TEXT,
    status            TEXT NOT NULL CHECK (status IN ('final', 'preliminary', 'amended')),
    resulted_on       DATE,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS lab_results_session_idx          ON lab_results (session_id);
CREATE INDEX IF NOT EXISTS lab_results_session_patient_idx  ON lab_results (session_id, patient_id);
CREATE INDEX IF NOT EXISTS lab_results_session_test_idx     ON lab_results (session_id, test_name);

-- ============================================================
-- Row-level security. LLM-generated SQL runs inside a read-only transaction
-- with the app.session_id GUC set; the policy filters every row so one
-- session can never read another session's data. FORCE applies RLS even to
-- the table owner.
-- ============================================================

ALTER TABLE doctors ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctors FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS doctors_session_isolation ON doctors;
CREATE POLICY doctors_session_isolation ON doctors
    USING      (session_id::text = current_setting('app.session_id', true))
    WITH CHECK (session_id::text = current_setting('app.session_id', true));

ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE patients FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS patients_session_isolation ON patients;
CREATE POLICY patients_session_isolation ON patients
    USING      (session_id::text = current_setting('app.session_id', true))
    WITH CHECK (session_id::text = current_setting('app.session_id', true));

ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS appointments_session_isolation ON appointments;
CREATE POLICY appointments_session_isolation ON appointments
    USING      (session_id::text = current_setting('app.session_id', true))
    WITH CHECK (session_id::text = current_setting('app.session_id', true));

ALTER TABLE prescriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE prescriptions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS prescriptions_session_isolation ON prescriptions;
CREATE POLICY prescriptions_session_isolation ON prescriptions
    USING      (session_id::text = current_setting('app.session_id', true))
    WITH CHECK (session_id::text = current_setting('app.session_id', true));

ALTER TABLE lab_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE lab_results FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS lab_results_session_isolation ON lab_results;
CREATE POLICY lab_results_session_isolation ON lab_results
    USING      (session_id::text = current_setting('app.session_id', true))
    WITH CHECK (session_id::text = current_setting('app.session_id', true));

-- ============================================================
-- Operator tables: per-IP rate limit + lead capture (no RLS, backend-only).
-- ============================================================

CREATE TABLE IF NOT EXISTS ip_quota (
    ip                INET        PRIMARY KEY,
    day_count         INT         NOT NULL DEFAULT 0,
    day_window_start  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS leads (
    id            BIGSERIAL   PRIMARY KEY,
    email         TEXT        NOT NULL,
    name          TEXT,
    ip            INET,
    source        TEXT,                         -- referrer_tag from the URL
    slack_status  TEXT,                         -- pending | sent | failed | NULL
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS leads_email_idx       ON leads (email);
CREATE INDEX IF NOT EXISTS leads_captured_at_idx ON leads (captured_at DESC);
-- Idempotency: same email + same source collapses into one row.
CREATE UNIQUE INDEX IF NOT EXISTS leads_email_source_uniq ON leads (email, COALESCE(source, ''));
CREATE INDEX IF NOT EXISTS leads_slack_status_idx ON leads (slack_status) WHERE slack_status IS NOT NULL;

-- ============================================================
-- Application role: demo_app (NOSUPERUSER, NOBYPASSRLS).
-- Postgres bypasses RLS for superusers and for any BYPASSRLS role, so the
-- API must run as a vanilla role for session isolation to hold. The backend
-- boots with a startup assertion that this role cannot bypass RLS.
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo_app') THEN
        CREATE ROLE demo_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO demo_app;

-- Sessions / events: backend needs read + write (cleanup, logging).
GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO demo_app;
GRANT SELECT, INSERT                 ON events   TO demo_app;

-- Tenant tables: backend seeds with INSERT/UPDATE/DELETE; LLM queries are
-- SELECT inside a READ ONLY transaction (writes rejected by the transaction).
GRANT SELECT, INSERT, UPDATE, DELETE ON doctors       TO demo_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON patients      TO demo_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON appointments  TO demo_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON prescriptions TO demo_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON lab_results   TO demo_app;

-- Operator tables.
GRANT SELECT, INSERT, UPDATE ON ip_quota TO demo_app;
GRANT SELECT, INSERT, UPDATE ON leads    TO demo_app;

-- Sequences for BIGSERIAL inserts (events, leads), now and in the future.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO demo_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO demo_app;

COMMIT;
