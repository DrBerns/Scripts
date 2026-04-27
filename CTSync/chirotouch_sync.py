# -*- coding: utf-8 -*-
# ChiroTouch -> Airtable Sync Bot - v6 (drafted 2026-04-19 for Phase 1 of Automation A)
# Based on chirotouch_sync_v_04.18.26.py. Adds Modifier + Units pull from
# PSChiro.ClaimLines (via PaymentClaims → Claims JOIN chain) and populates
# Linked Patient on Charge Log at sync time.
# Aligned with Automation A design v_04.19.26.2.

import sys, time, logging, os
from datetime import datetime, date

try:
    from records_pipeline import run_records_pipeline
except ImportError:
    run_records_pipeline = None

try:
    import config
except ImportError:
    print("ERROR: config.py not found")
    sys.exit(1)

try:
    import pyodbc
    from pyairtable import Api
    import schedule
except ImportError as e:
    print(f"Missing library: {e}")
    print("Run: pip install pyodbc pyairtable schedule")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("chirotouch_sync.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── Airtable setup ──────────────────────────────────────────────────────────
api           = Api(config.AIRTABLE_API_KEY)
base          = api.base(config.AIRTABLE_BASE_ID)
pat_tbl       = base.table(config.AIRTABLE_PATIENTS_TABLE_ID)
notes_tbl     = base.table("tbl2D9NTGjXcWeiHI")
charge_log_tbl = base.table(config.AIRTABLE_CHARGE_LOG_TABLE_ID)

# ── Queries ──────────────────────────────────────────────────────────────────

PATIENT_QUERY = """
SELECT
    p.ID                    AS ct_patient_id,
    p.FirstName, p.MiddleName, p.LastName,
    p.BirthDate,
    p.Address, p.City, p.State, p.Zip,
    p.EmailDefault          AS email,
    p.ChartNo               AS chart_no,
    p.AccountNo,
    p.AccountBalance,
    p.PatientBalance,
    p.InActive, p.Deceased,
    p.InitialVisit          AS date_first_visit,
    p.LastVisit             AS date_last_visit,
    p.NextVisit             AS next_appointment_date,
    p.LastPmtDate, p.LastPmtAmt,
    p.ReferredBy, p.ReferByName,
    p.CondAuto              AS is_auto_accident,
    p.CurInjuryDate         AS date_of_injury,
    p.OrigInjuryDate,
    p.AttyFirm              AS law_firm,
    p.AttyName              AS attorney_name,
    p.AttyPhone             AS attorney_phone,
    p.AttyFax               AS attorney_fax,
    p.AttyEmail             AS attorney_email,
    p.AttyNotes             AS attorney_notes,
    p.PCPName               AS pcp_name,
    p.DemographicLanguage   AS preferred_language,
    p.FeeSchedule, p.PatFilesID
FROM Patients p
WHERE p.InActive = 0 AND p.Deceased = 0
ORDER BY p.LastVisit DESC;
"""

NOTES_QUERY = """
SELECT n.ID AS note_id, n.NoteTypeID AS ct_patient_id,
    n.NoteType, n.CreatedBy, n.CreatedOn,
    n.Category, n.Subject, n.Note,
    p.FirstName + ' ' + p.LastName AS patient_name
FROM Notes n
LEFT JOIN Patients p ON n.NoteTypeID = p.ID
WHERE n.NoteType = 'PT'
  AND n.CreatedOn >= DATEADD(day, -365, GETDATE())
ORDER BY n.CreatedOn DESC;
"""

# Pulls all charges (TranType = 'C') from the last 90 days.
# v_04.19.26.3 (2026-04-20): switched modifier+units source from ClaimLines
# to ChargeDetails. ClaimLines only populates AFTER a charge is billed out
# (Transactions.PaymentClaimID → PaymentClaims → Claims → ClaimLines); for
# fresh/unbilled charges that entire chain is NULL, so 0/1728 recent rows
# resolved. ChargeDetails is keyed 1:1 to Transactions.ID via ChargeTranID
# and is populated at charge-entry time, giving 100% coverage. M1/M2 are
# the two modifier slots (ChargeDetails has no M3/M4 — those only exist in
# ClaimLines); DaysUnits is stored as char so cast at use site.
# Upsert key is ct_transaction_id (Transactions.ID) so re-runs are safe.
CHARGES_QUERY = """
SELECT
    t.ID                            AS ct_transaction_id,
    t.TranDate                      AS charge_date,
    t.Code                          AS cpt_code,
    t.Description                   AS charge_description,
    t.TranType                      AS tran_type,
    t.TranSubType                   AS tran_sub_type,
    t.TranAmt                       AS charge_amount,
    t.PatID                         AS ct_patient_id,
    t.ApptID                        AS ct_appt_id,
    t.DoctorID                      AS ct_doctor_id,
    t.Notes                         AS charge_notes,
    t.CreatedDate                   AS created_date,
    p.FirstName + ' ' + p.LastName  AS patient_name,
    p.ChartNo                       AS chart_no,
    -- v_04.19.26.3: modifier + units from ChargeDetails (keyed on ChargeTranID)
    cd.M1                           AS modifier1,
    cd.M2                           AS modifier2,
    NULL                            AS modifier3,
    NULL                            AS modifier4,
    cd.DaysUnits                    AS units
FROM Transactions t
LEFT JOIN Patients      p  ON t.PatID = p.ID
LEFT JOIN ChargeDetails cd ON cd.ChargeTranID = t.ID
WHERE t.TranType = 'C'
  AND t.TranDate >= DATEADD(day, -90, GETDATE())
  AND t.Code IS NOT NULL
  AND t.Code != ''
ORDER BY t.TranDate DESC;
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_db():
    conn_str = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={config.CT_SERVER};DATABASE={config.CT_DATABASE};"
        f"UID={config.CT_USERNAME};PWD={config.CT_PASSWORD};"
        f"TrustServerCertificate=yes;Encrypt=yes;"
    )
    conn = pyodbc.connect(conn_str, timeout=10)
    log.info("[OK] Connected to PSChiro")
    return conn

def safe_str(v):
    return "" if v is None else str(v).strip()

def safe_date(v):
    if v is None: return None
    if isinstance(v, (datetime, date)): return v.strftime("%Y-%m-%d")
    return None

def safe_datetime(v):
    if v is None: return None
    if isinstance(v, (datetime, date)): return v.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return None

def safe_float(v):
    try: return float(v)
    except: return None

def _parse_units(v):
    """ChargeDetails.DaysUnits is char (e.g. '1', '2 '). Strip + int; default 1."""
    try:
        n = int(str(v).strip())
        return n if n > 0 else 1
    except (ValueError, TypeError, AttributeError):
        return 1


def concat_modifiers(row):
    """
    Concatenate non-null Modifier1..4 from ClaimLines into a comma-separated
    string. Preserves position order so '50' always lands before '59' if both
    exist and were stored that way in the source.

    Option B per Doc (2026-04-19): single Charge Log field holding the joined
    string. Automation A (§4.2 / §4.3 of design v_04.19.26.2) reads this with
    `"50" in modifier.split(",")` to detect bilateral.

    Examples:
        modifier1='50', modifier2=None, ... → '50'
        modifier1='RT', modifier2='50', ... → 'RT,50'
        all NULL (cash-pay charge)          → ''
    """
    mods = []
    for attr in ("modifier1", "modifier2", "modifier3", "modifier4"):
        v = safe_str(getattr(row, attr, ""))
        if v:
            mods.append(v)
    return ",".join(mods)


def build_patient_lookup():
    """
    One-shot fetch of all Airtable Patients records, keyed on CT Patient ID.
    Returns a dict: {ct_patient_id (int): airtable_record_id (str)}.

    Called once per charges sync run from sync_charges(). Patients sync runs
    BEFORE charges sync in sync_all() (verified: sync_patients() → sync_notes()
    → sync_charges() order preserved from v_04.18.26.py line 367-369), so this
    lookup sees the most recent Patients state.

    Field ID references:
        fld4ogKYb3HOjcm4w = CT Patient ID (int) on Patients table
    """
    log.info("[RUNNING] Building patient lookup for Charge Log...")
    lookup = {}
    # use_field_ids=True (pyairtable 3.x alias for returnFieldsByFieldId) so
    # Airtable interprets "fld..." as IDs (not names) AND keys the returned
    # dict by field ID. Without it, the API treats the string as a field NAME,
    # matches nothing, and every record comes back with an empty fields dict —
    # which is why the v_04.19.26 first run produced 0 entries against 429
    # Patients rows.
    for r in pat_tbl.all(fields=["fld4ogKYb3HOjcm4w"], use_field_ids=True):
        ct_id = r["fields"].get("fld4ogKYb3HOjcm4w")
        if ct_id is not None:
            lookup[int(ct_id)] = r["id"]
    log.info(f"[OK] Patient lookup built: {len(lookup)} entries")
    return lookup

# ── Field mappers ─────────────────────────────────────────────────────────────

def patient_fields(row):
    addr = ", ".join(p for p in [
        safe_str(row.Address),
        safe_str(row.City),
        f"{safe_str(row.State)} {safe_str(row.Zip)}".strip()
    ] if p)

    last_pmt = ""
    if row.LastPmtDate:
        last_pmt = f"{safe_date(row.LastPmtDate)} / ${safe_float(row.LastPmtAmt) or 0:.2f}"

    f = {
        "fld0wmWONVDMBzgAQ": " ".join(p for p in [safe_str(row.FirstName), safe_str(row.MiddleName), safe_str(row.LastName)] if p),
        "fldlhB2MCArVOn4H0": safe_date(row.BirthDate),
        "fldiNm1RO9OcQuwpu": safe_str(row.chart_no),
        "fldzCNZ7bi1AN2ipR": safe_str(row.email),
        "fldJhUXv8w700p1co": addr,
        "fldAKtnTlxQ7qjuUr": safe_date(row.date_first_visit),
        "fldkBSg5hNrH5641f": safe_date(row.date_last_visit),
        "fldEvwLuQEtw8xnjB": safe_date(row.next_appointment_date),
        "fldMH3a0rMCD9OItQ": safe_float(row.PatientBalance),
        "fldQ4vKBkoOS0H9Si": safe_float(row.AccountBalance),
        "fldHnZbHMTlPZQ2NP": last_pmt,
        "fldR4YVOJJwfNdkCG": safe_str(row.ReferredBy) or safe_str(row.ReferByName),
        "flduslveCNUIhCgPp": bool(row.is_auto_accident),
        "fldGd54zi1pJ8pjp0": safe_date(row.date_of_injury),
        "fldI3DNESryeSiKIP": safe_date(row.OrigInjuryDate),
        "fldpRRBnqATHFfrLn": safe_str(row.attorney_name),
        "fldDGZjrnCsuUYu6p": safe_str(row.law_firm),
        "fld0QKGpnvKIxmXwv": safe_str(row.attorney_fax),
        "fldyjuuvproW4tDAv": safe_str(row.attorney_email),
        "fldEuhazNyShuTubr": safe_str(row.attorney_notes),
        "fldFCXEljvYkyU13J": safe_str(row.pcp_name),
        "fldj9NiF6tOuDLcKt": safe_str(row.preferred_language) if safe_str(row.preferred_language) not in ("", "NULL", "None") else "English",
        "fld4ogKYb3HOjcm4w": int(row.ct_patient_id),
        "fldIZmbzvBqrCRStW": int(row.AccountNo) if row.AccountNo else None,
        "fldjYm8gD2qoewPjO": int(row.PatFilesID) if row.PatFilesID else None,
        "fldGyrm7QhcnXI2Le": safe_str(row.chart_no),
        "fldIjXMjWkkoOwmRz": safe_str(row.FeeSchedule),
        "fldn7IMJoR5cJAtio": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    return {k: v for k, v in f.items() if v not in (None, "", False)}


def charge_fields(row, patient_lookup):
    """
    Maps a Transactions row (TranType='C') to the Airtable Charge Log fields.

    Real Airtable field IDs wired in on 2026-04-18 after Charge Log table
    (tblncYo80pqzXsZNV) was created in base app8GGPbIMTkNSGnl.

    Upsert key: fldRXvmup8WSGeFmg (CT Transaction ID) — ensures re-runs don't
    duplicate records. Set this as the key_field in batch_upsert below.

    Inventory Processed (fldqipUvdqwG9i1ZY) is Airtable-owned — the sync bot
    never writes to it. Automation A flips it TRUE after inventory debit.

    Automation Errors (fld38Xm3nitYQ6dT5) is also Airtable-owned — populated
    by Automation A when a rule fails mid-run. Sync bot never writes here.

    NEW in v_04.19.26 (aligned with Automation A design v_04.19.26.2):
      - Modifier (fldl1R9Axu8oEU6P2): concatenated from ClaimLines.Modifier1..4
      - Units    (fldwILSiViRe6PdjE): from ClaimLines.Units, defaults to 1
      - Linked Patient (fldbwqMmbakZPWCBv): Airtable record ID from patient_lookup
    """
    ct_pid = int(row.ct_patient_id) if row.ct_patient_id else None

    # Linked Patient — single-element list when resolved, None when not.
    # Returning None (not []) means the existing global filter strips it cleanly.
    linked_patient = [patient_lookup[ct_pid]] if ct_pid and ct_pid in patient_lookup else None

    f = {
        # CT Transaction ID — upsert key, never changes
        "fldRXvmup8WSGeFmg":  int(row.ct_transaction_id),

        # Charge Date
        "fldcmu6gm09Jkrmjy":  safe_date(row.charge_date),

        # CPT / HCPCS Code
        "fldlz4sp250T5UoV9":  safe_str(row.cpt_code),

        # Description
        "fldAUxH4hkcvhzmiE":  safe_str(row.charge_description),

        # Charge Amount
        "fldWJZuUDmVpldVgY":  safe_float(row.charge_amount),

        # TranSubType: SV / PT / RG
        "fldYsL8cbMeL95C6e":  safe_str(row.tran_sub_type),

        # CT Patient ID — foreign key to Patients table
        "fldccrrTBALUUe9yG":  ct_pid,

        # Patient Name (denormalized for display)
        "fldlJ5xXbfxGIMqQJ":  safe_str(row.patient_name),

        # Chart No
        "fldEZnCjGS1STLAJD":  safe_str(row.chart_no),

        # CT Appointment ID
        "fldvQvmyDc7fp6dsA":  int(row.ct_appt_id) if row.ct_appt_id else None,

        # CT Doctor ID
        "fldxNr1HzMLxWiP4B":  int(row.ct_doctor_id) if row.ct_doctor_id else None,

        # Notes from ChiroTouch
        "fldZTsT7Kiyu4QnFx":  safe_str(row.charge_notes),

        # ═══ NEW IN v_04.19.26 ═══

        # Modifier (singleLineText) — concatenated non-null Modifier1..4 from ClaimLines
        "fldl1R9Axu8oEU6P2":  concat_modifiers(row),

        # Units (number, integer) — from ChargeDetails.DaysUnits; char column so
        # strip + int-cast safely, default to 1 on NULL/empty/non-numeric/<=0.
        "fldwILSiViRe6PdjE":  _parse_units(row.units),

        # Linked Patient (multipleRecordLinks → Patients) — resolved via lookup; None if unresolved
        "fldbwqMmbakZPWCBv":  linked_patient,

        # ═══════════════════════

        # Inventory Processed (fldqipUvdqwG9i1ZY) — owned by Airtable automation.
        # Do NOT sync this field — let Airtable own it after creation.
        # Automation Errors (fld38Xm3nitYQ6dT5) — owned by Automation A. Do NOT sync.

        # Last Synced timestamp
        "fldEh8TCEBb9Qejyo":  datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    return {k: v for k, v in f.items() if v not in (None, "", False)}


# ── Sync functions ────────────────────────────────────────────────────────────

def sync_patients():
    log.info("[RUNNING] Patient sync...")
    start = time.time()
    try:
        conn = get_db()
        rows = conn.cursor().execute(PATIENT_QUERY).fetchall()
        conn.close()
        log.info(f"[DATA] {len(rows)} patients from ChiroTouch")

        records = [{"fields": patient_fields(r)} for r in rows]

        result = pat_tbl.batch_upsert(
            records,
            key_fields=["fld4ogKYb3HOjcm4w"],
            typecast=True
        )
        created = len(result.get("createdRecords", []))
        updated = len(result.get("updatedRecords", []))
        elapsed = round(time.time() - start, 1)
        log.info(f"[OK] Patients -- Created: {created}, Updated: {updated} [{elapsed}s]")

    except Exception as e:
        log.error(f"[ERROR] Patient sync: {e}")


def sync_notes():
    VALID_CATS = [
        "Attorney Communication", "Patient Communication", "Billing Note",
        "Insurance Communication", "Aging", "Doctor Communication",
        "Financial Agreement", "Policy Information", "No Future Appointments",
        "EMC", "Other"
    ]
    log.info("[RUNNING] Notes sync...")
    start = time.time()
    try:
        conn = get_db()
        rows = conn.cursor().execute(NOTES_QUERY).fetchall()
        conn.close()
        log.info(f"[DATA] {len(rows)} notes from ChiroTouch")

        records = []
        for r in rows:
            cat = safe_str(r.Category) if safe_str(r.Category) in VALID_CATS else "Other"
            f = {
                "fld3wVfdKBa7TGVAX": safe_str(r.Subject),
                "fldRWIqN2f9FGcFIl": cat,
                "fldJS3Mt7duyTIh5K": safe_str(r.Note),
                "fldGXIhq9NurFduEs": safe_str(r.CreatedBy),
                "fldgCxoYhvNwzaLQc": r.CreatedOn.strftime("%Y-%m-%dT%H:%M:%S.000Z") if r.CreatedOn else None,
                "fldPXP2QyHHcWOAQc": safe_str(r.patient_name),
                "flduLvExsaLNlm1MC": int(r.note_id),
                "fldZcALGKyhvJTDEo": int(r.ct_patient_id),
            }
            records.append({"fields": {k: v for k, v in f.items() if v not in (None, "")}})

        result = notes_tbl.batch_upsert(
            records,
            key_fields=["flduLvExsaLNlm1MC"],
            typecast=True
        )
        created = len(result.get("createdRecords", []))
        updated = len(result.get("updatedRecords", []))
        elapsed = round(time.time() - start, 1)
        log.info(f"[OK] Notes -- Created: {created}, Updated: {updated} [{elapsed}s]")

    except Exception as e:
        log.error(f"[ERROR] Notes sync: {e}")


def sync_charges():
    """
    Syncs charge transactions (TranType='C') from PSChiro.Transactions
    to the Airtable Charge Log table (📋 Charge Log, tblncYo80pqzXsZNV).

    v_04.19.26: adds Modifier + Units pull from ClaimLines, and populates
    Linked Patient via a one-shot Patients lookup built at start of run.

    Call order (verified from v_04.18.26.py line 367-369):
        sync_patients() → sync_notes() → sync_charges()
    This guarantees the patient_lookup below reads a fresh Patients state.

    - Pulls last 90 days of charges on every run
    - Upserts on CT Transaction ID (fldRXvmup8WSGeFmg) so re-runs are safe and idempotent
    - Does NOT overwrite the Inventory Processed checkbox — Airtable automation owns that field
    - Does NOT overwrite Automation Errors — Automation A owns that field
    """
    log.info("[RUNNING] Charge sync...")
    start = time.time()
    try:
        # Build patient lookup ONCE per run (not per row) — one Airtable read, not N
        patient_lookup = build_patient_lookup()

        conn = get_db()
        rows = conn.cursor().execute(CHARGES_QUERY).fetchall()
        conn.close()
        log.info(f"[DATA] {len(rows)} charge transactions from ChiroTouch")

        records = [{"fields": charge_fields(r, patient_lookup)} for r in rows]

        result = charge_log_tbl.batch_upsert(
            records,
            key_fields=["fldRXvmup8WSGeFmg"],  # CT Transaction ID
            typecast=True
        )
        created = len(result.get("createdRecords", []))
        updated = len(result.get("updatedRecords", []))
        elapsed = round(time.time() - start, 1)
        log.info(f"[OK] Charges -- Created: {created}, Updated: {updated} [{elapsed}s]")

    except Exception as e:
        log.error(f"[ERROR] Charge sync: {e}")


def sync_all():
    log.info("=" * 55)
    log.info("[SYNC] Starting ChiroTouch -> Airtable sync")
    log.info("=" * 55)
    sync_patients()
    sync_notes()
    sync_charges()
    log.info("[DONE] Sync complete\n")


if __name__ == "__main__":
    log.info("[START] ChiroTouch -> Airtable Sync Bot v6 (drafted 2026-04-19)")
    log.info(f"   Server: {config.CT_SERVER} | DB: {config.CT_DATABASE}")
    log.info(f"   Schedule: Every 30 seconds")

    sync_all()
    schedule.every(30).seconds.do(sync_all)

    log.info("[TIMER] Running - Ctrl+C to stop")
    while True:
        schedule.run_pending()
        time.sleep(15)
