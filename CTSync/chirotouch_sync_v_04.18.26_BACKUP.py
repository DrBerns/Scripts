# -*- coding: utf-8 -*-
# ChiroTouch -> Airtable Sync Bot - Final Version
# Uses pyairtable library for reliable upsert

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

# Airtable setup via pyairtable
api      = Api(config.AIRTABLE_API_KEY)
base     = api.base(config.AIRTABLE_BASE_ID)
pat_tbl  = base.table(config.AIRTABLE_PATIENTS_TABLE_ID)
notes_tbl = base.table("tbl2D9NTGjXcWeiHI")

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

def safe_float(v):
    try: return float(v)
    except: return None

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


def sync_patients():
    log.info("[RUNNING] Patient sync...")
    start = time.time()
    try:
        conn = get_db()
        rows = conn.cursor().execute(PATIENT_QUERY).fetchall()
        conn.close()
        log.info(f"[DATA] {len(rows)} patients from ChiroTouch")

        records = [{"fields": patient_fields(r)} for r in rows]

        # batch_upsert handles all deduplication - pyairtable's proven implementation
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


def sync_all():
    log.info("=" * 55)
    log.info("[SYNC] Starting ChiroTouch -> Airtable sync")
    log.info("=" * 55)
    sync_patients()
    sync_notes()
    log.info("[DONE] Sync complete\n")


if __name__ == "__main__":
    log.info("[START] ChiroTouch -> Airtable Sync Bot")
    log.info(f"   Server: {config.CT_SERVER} | DB: {config.CT_DATABASE}")
    log.info(f"   Schedule: Every 30 seconds")

    sync_all()
    schedule.every(30).seconds.do(sync_all)

    log.info("[TIMER] Running - Ctrl+C to stop")
    while True:
        schedule.run_pending()
        time.sleep(15)
