CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS practitioners (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    practitioner_key text NOT NULL,
    source text NOT NULL,
    listing_type text NOT NULL,
    full_name text NOT NULL,
    profession text NOT NULL,
    licence_status text,
    specialty text,
    practice_name text,
    practice_address text,
    city text,
    postal_code text,
    phone text,
    fax text,
    source_reference text,
    collection_method text NOT NULL DEFAULT 'bulk_pdf',
    collected_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, listing_type, practitioner_key)
);

CREATE TABLE IF NOT EXISTS organizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_key text NOT NULL UNIQUE,
    name text NOT NULL,
    industry text,
    sector text,
    address_line1 text,
    city text,
    province text,
    postal_code text,
    country text,
    employee_count_estimate integer,
    licensee_count integer,
    practice_type text,
    founded_year integer,
    source_system text NOT NULL,
    source_org_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_key text NOT NULL UNIQUE,
    organization_id uuid REFERENCES organizations (id),
    first_name text,
    last_name text,
    full_name text NOT NULL,
    title text,
    credentials text,
    specialty text,
    license_number text,
    license_college text NOT NULL,
    email text,
    phone text,
    fax text,
    enriched_at timestamptz,
    enrichment_status text NOT NULL DEFAULT 'blocked',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS segments (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    province text NOT NULL,
    profession text NOT NULL,
    config_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS etl_runs (
    id uuid PRIMARY KEY,
    segment_id text NOT NULL,
    phase text NOT NULL,
    status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    row_counts jsonb,
    git_sha text,
    error text
);

CREATE TABLE IF NOT EXISTS segment_members (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    segment_id text NOT NULL REFERENCES segments (id),
    organization_id uuid NOT NULL REFERENCES organizations (id),
    contact_id uuid NOT NULL REFERENCES contacts (id),
    is_primary_contact boolean NOT NULL DEFAULT false,
    fit_score numeric NOT NULL,
    score_version integer NOT NULL,
    score_breakdown jsonb,
    run_id uuid NOT NULL REFERENCES etl_runs (id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (segment_id, organization_id, contact_id)
);

ALTER TABLE practitioners ADD COLUMN IF NOT EXISTS specialty text;
ALTER TABLE practitioners ADD COLUMN IF NOT EXISTS city text;
ALTER TABLE practitioners ADD COLUMN IF NOT EXISTS postal_code text;
ALTER TABLE practitioners ADD COLUMN IF NOT EXISTS phone text;
ALTER TABLE practitioners ADD COLUMN IF NOT EXISTS fax text;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS fax text;

CREATE INDEX IF NOT EXISTS organizations_city_idx ON organizations (city);
CREATE INDEX IF NOT EXISTS organizations_sector_idx ON organizations (sector);
CREATE INDEX IF NOT EXISTS segment_members_score_idx ON segment_members (segment_id, fit_score DESC);
CREATE INDEX IF NOT EXISTS segment_members_primary_idx ON segment_members (segment_id, is_primary_contact);

-- Advisor marks. Keyed by deterministic org_key and contact_key because each CPSA
-- load deletes and reinserts organizations, contacts, and segment_members.
-- replace_source_rows must not delete this table.
CREATE TABLE IF NOT EXISTS lead_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    segment_id text NOT NULL REFERENCES segments (id),
    org_key text NOT NULL,
    contact_key text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN (
        'worth_call',
        'hospital_employee',
        'wrong_size',
        'not_buyer'
    )),
    note text,
    reviewed_by text NOT NULL DEFAULT 'advisor',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (segment_id, org_key, contact_key)
);

-- Column list changed after the first load, so replace cannot rename in place.
DROP VIEW IF EXISTS advisor_shortlist;
DROP VIEW IF EXISTS advisor_review_queue;
DROP VIEW IF EXISTS advisor_leads;

-- Advisor call list from the 2026-10-24 CPSA load.
-- Group practices at fit_score >= 80 are the 2–15 licensee band (scores 80, 88, 92).
-- Score 66 is the 16+ band and stays out of this view.
-- A matched professional corporation is usually practice_type solo, because only the
-- unique owner is attached. Those rows are the buyer list and are included on their own.
CREATE OR REPLACE VIEW advisor_leads AS
SELECT
    m.segment_id,
    o.org_key,
    c.contact_key,
    c.full_name,
    c.credentials,
    c.title,
    c.specialty,
    c.phone,
    c.fax,
    phones.phone_shared_count,
    CASE
        WHEN c.phone IS NULL OR btrim(c.phone) = '' THEN 'missing'
        WHEN phones.phone_shared_count > 1 THEN 'practice'
        ELSE 'unshared'
    END AS phone_kind,
    o.name AS practice,
    o.address_line1,
    COALESCE(
        NULLIF(btrim(o.city), ''),
        NULLIF(initcap(split_part(c.contact_key, '|', 3)), '')
    ) AS city,
    o.licensee_count,
    o.practice_type,
    m.fit_score,
    CASE
        WHEN o.name ILIKE '%professional corporation%' THEN 'professional_corporation'
        ELSE 'group_practice'
    END AS lead_kind
FROM segment_members m
JOIN organizations o ON o.id = m.organization_id
JOIN contacts c ON c.id = m.contact_id
LEFT JOIN LATERAL (
    SELECT count(*)::int AS phone_shared_count
    FROM contacts others
    WHERE c.phone IS NOT NULL
      AND btrim(c.phone) <> ''
      AND others.phone = c.phone
) phones ON true
WHERE m.is_primary_contact
  AND (
        (o.practice_type = 'group' AND m.fit_score >= 80)
        OR (
            o.practice_type = 'solo'
            AND c.title = 'Owner'
            AND o.name ILIKE '%professional corporation%'
        )
  );

-- Same rows as advisor_leads, with any stored mark. Unreviewed rows have a null verdict.
CREATE OR REPLACE VIEW advisor_review_queue AS
SELECT
    l.*,
    r.verdict,
    r.note,
    r.reviewed_by,
    r.updated_at AS reviewed_at
FROM advisor_leads l
LEFT JOIN lead_reviews r
    ON r.segment_id = l.segment_id
   AND r.org_key = l.org_key
   AND r.contact_key = l.contact_key;

-- Advisor-facing list for the current pilot: Edmonton only, 20 highest scores.
-- Score v1 ranks an owner-matched practice above a larger clinic. Rejected rows
-- drop out and the next Edmonton score fills the slot. worth_call stays.
CREATE OR REPLACE VIEW advisor_shortlist AS
SELECT *
FROM advisor_review_queue
WHERE city = 'Edmonton'
  AND (verdict IS NULL OR verdict = 'worth_call')
ORDER BY fit_score DESC, licensee_count DESC, full_name
LIMIT 20;
