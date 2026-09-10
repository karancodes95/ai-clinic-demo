"""Fixed deterministic seed for the healthcare vertical.

Patient-facing: each session is ONE signed-in patient — **James Anderson**, a
65-year-old Medicare patient at Cedarwood Family Health with a cardiac history.
Only his own records are seeded, so session RLS already limits every query to
this one patient (no cross-patient access is possible — there is no other
patient in the session). Mirrors ask_clinic's single-patient scoping.

Design rules:
- Every "story fact" (upcoming cardiology visit, the A1C slightly high, the
  preliminary triglycerides) is deterministic and traceable.
- Visit / fill / result dates are *anchored to NOW()* at seed time so
  "next appointment" / "recent results" stay meaningful. Birthdate is fixed.
- Rows hand-authored. No Faker, no randomness. The patient is fictional.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid5, NAMESPACE_DNS

import asyncpg  # noqa: F401  (type hint in signature)


# ============================================================
# Time anchors — "today" when the seed runs.
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(n: int) -> datetime:
    """Negative n -> future (upcoming appointments)."""
    return _now() - timedelta(days=n)


def _date_days_ago(n: int) -> date:
    return (_now() - timedelta(days=n)).date()


def _uuid_for(label: str) -> UUID:
    return uuid5(NAMESPACE_DNS, f"cedarwood.{label}")


# ============================================================
# Doctors (2) — the ones this patient sees.
# ============================================================

DOCTORS = [
    # (label, name, specialty)
    ("whitman", "Dr. Sarah Whitman", "Family Medicine"),
    ("torres",  "Dr. Miguel Torres", "Cardiology"),
]


# ============================================================
# Patient (1) — the signed-in user.
# ============================================================

PATIENTS = [
    # (label, first, last, dob, email, phone, insurance_provider, insurance_member_id)
    ("anderson", "James", "Anderson", date(1968, 4, 12), "james.anderson@example.com",
     "312-555-0142", "Medicare", "1EG4-TE5-MK72"),
]


# ============================================================
# Appointments (5) — his. Narrative facts:
#  - Next up: cardiology follow-up with Dr. Torres (in ~5 days)
#  - Also upcoming: medication review with Dr. Whitman (in ~12 days)
#  - One past no-show (flu shot)
# ============================================================

APPOINTMENTS = [
    # (patient_label, doctor_label, days_ago (negative = future), status, reason)
    ("anderson", "torres",   -5, "scheduled", "Cardiology follow-up"),
    ("anderson", "whitman", -12, "scheduled", "Medication review"),
    ("anderson", "whitman",  20, "completed", "Blood pressure check"),
    ("anderson", "torres",   95, "completed", "Chest pain workup"),
    ("anderson", "whitman", 200, "no_show",   "Flu shot"),
]


# ============================================================
# Prescriptions (3) — his.
# ============================================================

PRESCRIPTIONS = [
    # (patient_label, medication, dosage, refills_remaining, status, last_filled_days_ago)
    ("anderson", "Lisinopril 10mg",   "once daily",           2, "active", 23),
    ("anderson", "Atorvastatin 20mg", "once daily at night",  3, "active", 23),
    ("anderson", "Metformin 500mg",   "twice daily",          1, "active", 15),
]


# ============================================================
# Lab results (4) — his. Narrative facts:
#  - A1C 6.8 (slightly above range), LDL 138 (above <100)
#  - Triglycerides still preliminary
# ============================================================

LAB_RESULTS = [
    # (patient_label, test_name, value, unit, reference_range, status, resulted_days_ago)
    ("anderson", "Hemoglobin A1C",  "6.8", "%",     "4.0-5.6", "final",       22),
    ("anderson", "LDL Cholesterol", "138", "mg/dL", "<100",    "final",       22),
    ("anderson", "Creatinine",      "1.1", "mg/dL", "0.7-1.3", "final",       22),
    ("anderson", "Triglycerides",   "180", "mg/dL", "<150",    "preliminary",  4),
]


# ============================================================
# Loader.
# ============================================================

async def seed(conn, session_id: UUID) -> int:
    """Insert this patient's full record into the session's scope.

    Returns the total number of rows inserted across all tables. Assumes the
    caller has already set the `app.session_id` GUC so RLS WITH CHECK passes.
    """
    sid_str = str(session_id)
    doctor_ids: dict[str, UUID] = {}
    patient_ids: dict[str, UUID] = {}

    # doctors
    doctor_rows = []
    for (label, name, specialty) in DOCTORS:
        did = _uuid_for(f"{sid_str}.doctor.{label}")
        doctor_ids[label] = did
        doctor_rows.append((did, session_id, name, specialty))
    await conn.executemany(
        """INSERT INTO doctors (id, session_id, name, specialty)
           VALUES ($1,$2,$3,$4)""",
        doctor_rows,
    )

    # patients
    patient_rows = []
    for (label, first, last, dob, email, phone, provider, member_id) in PATIENTS:
        pid = _uuid_for(f"{sid_str}.patient.{label}")
        patient_ids[label] = pid
        patient_rows.append((
            pid, session_id, first, last, dob, email, phone, provider, member_id,
        ))
    await conn.executemany(
        """INSERT INTO patients
           (id, session_id, first_name, last_name, dob, email, phone,
            insurance_provider, insurance_member_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        patient_rows,
    )

    # appointments
    appt_rows = []
    for idx, (p_label, d_label, days_ago, status, reason) in enumerate(APPOINTMENTS):
        aid = _uuid_for(f"{sid_str}.appt.{idx}")
        appt_rows.append((
            aid, session_id, patient_ids[p_label], doctor_ids[d_label],
            _days_ago(days_ago), status, reason,
        ))
    await conn.executemany(
        """INSERT INTO appointments
           (id, session_id, patient_id, doctor_id, scheduled_at, status, reason)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        appt_rows,
    )

    # prescriptions
    rx_rows = []
    for idx, (p_label, medication, dosage, refills, status, filled_days_ago) in enumerate(PRESCRIPTIONS):
        rid = _uuid_for(f"{sid_str}.rx.{idx}")
        rx_rows.append((
            rid, session_id, patient_ids[p_label], medication, dosage,
            refills, status, _date_days_ago(filled_days_ago),
        ))
    await conn.executemany(
        """INSERT INTO prescriptions
           (id, session_id, patient_id, medication, dosage, refills_remaining,
            status, last_filled_on)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        rx_rows,
    )

    # lab_results
    lab_rows = []
    for idx, (p_label, test_name, value, unit, ref_range, status, resulted_days_ago) in enumerate(LAB_RESULTS):
        lid = _uuid_for(f"{sid_str}.lab.{idx}")
        lab_rows.append((
            lid, session_id, patient_ids[p_label], test_name, value, unit,
            ref_range, status, _date_days_ago(resulted_days_ago),
        ))
    await conn.executemany(
        """INSERT INTO lab_results
           (id, session_id, patient_id, test_name, value, unit, reference_range,
            status, resulted_on)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        lab_rows,
    )

    return (len(doctor_rows) + len(patient_rows) + len(appt_rows)
            + len(rx_rows) + len(lab_rows))
