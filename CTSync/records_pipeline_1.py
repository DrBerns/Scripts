# -*- coding: utf-8 -*-
# records_pipeline.py
# Handles the full records request workflow:
#   - Crawls fax inbox and info inbox for inbound requests
#   - Creates Records Request records in Airtable
#   - Sends records via email with proper template
#   - Saves sent emails to patient CT folder
#   - Writes timestamped notes to Patient Notes
#   - Tracks 48-hour SLA
#   - Crawls sent folder and saves copies to patient CT folder

import imaplib
import smtplib
import email
import email.mime.multipart
import email.mime.text
import email.mime.base
import email.mime.application
import email.encoders
import os
import time
import logging
import re
import hashlib
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

import requests
from pyairtable import Api

try:
    import config
except ImportError:
    raise ImportError("config.py not found in C:\\CTSync\\")

log = logging.getLogger(__name__)

# Airtable setup
at_api      = Api(config.AIRTABLE_API_KEY)
at_base     = at_api.base(config.AIRTABLE_BASE_ID)
pat_tbl     = at_base.table(config.AIRTABLE_PATIENTS_TABLE_ID)
notes_tbl   = at_base.table("tbl2D9NTGjXcWeiHI")
rr_tbl      = at_base.table("tblDegyhbPW9wtr2h")
comms_tbl   = at_base.table("tblX4gm6sjQMRYyDf")

AT_HEADERS = {
    "Authorization": f"Bearer {config.AIRTABLE_API_KEY}",
    "Content-Type": "application/json"
}
BASE_ID = config.AIRTABLE_BASE_ID

# =============================================================================
# IMAP HELPERS
# =============================================================================

def get_imap_connection(server, port, email_addr, password):
    conn = imaplib.IMAP4_SSL(server, port)
    conn.login(email_addr, password)
    return conn

def decode_mime_words(s):
    if not s:
        return ""
    decoded = decode_header(s)
    result = []
    for part, enc in decoded:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)

def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return body.strip()

def get_attachments(msg):
    attachments = []
    for part in msg.walk():
        cd = str(part.get("Content-Disposition", ""))
        if "attachment" in cd:
            filename = decode_mime_words(part.get_filename() or "attachment.bin")
            data = part.get_payload(decode=True)
            if data:
                attachments.append({"filename": filename, "data": data})
    return attachments

# =============================================================================
# PATIENT MATCHING
# =============================================================================

def normalize_name(name):
    return re.sub(r"[^a-z]", "", name.lower())

def find_patient_by_name_dob(name, dob_str=None):
    all_patients = pat_tbl.all()
    name_clean = normalize_name(name)

    candidates = []
    for rec in all_patients:
        f = rec.get("fields", {})
        pat_name = normalize_name(f.get("fld0wmWONVDMBzgAQ", ""))
        if name_clean in pat_name or pat_name in name_clean:
            candidates.append(rec)

    if len(candidates) == 1:
        return candidates[0]

    # If DOB provided, narrow down
    if dob_str and len(candidates) > 1:
        for rec in candidates:
            f = rec.get("fields", {})
            if dob_str in str(f.get("fldlhB2MCArVOn4H0", "")):
                return rec

    return candidates[0] if len(candidates) == 1 else None

def extract_patient_info_from_text(text):
    info = {}

    # Patient name patterns
    name_patterns = [
        r"patient[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        r"re[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        r"for[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        r"client[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["patient_name"] = match.group(1).strip()
            break

    # DOB patterns
    dob_patterns = [
        r"d\.?o\.?b\.?[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"date of birth[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"born[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    ]
    for pattern in dob_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["dob"] = match.group(1).strip()
            break

    # Date range patterns
    dos_patterns = [
        r"dates?\s+of\s+service[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*(?:to|-|through)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"from[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*(?:to|-|through)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    ]
    for pattern in dos_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["dos_start"] = match.group(1).strip()
            info["dos_end"] = match.group(2).strip()
            break

    # Date of accident
    doa_patterns = [
        r"date\s+of\s+(?:accident|loss|injury)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"d\.?o\.?a\.?[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"d\.?o\.?l\.?[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    ]
    for pattern in doa_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["date_of_accident"] = match.group(1).strip()
            break

    # What's being requested
    request_types = []
    if re.search(r"billing|ledger|charges|itemized", text, re.IGNORECASE):
        request_types.append("Billing Ledger")
    if re.search(r"notes?|records?|chart|office notes", text, re.IGNORECASE):
        request_types.append("Office Notes")
    if re.search(r"mri|imaging|x.ray|radiology", text, re.IGNORECASE):
        request_types.append("Imaging")
    if re.search(r"discharge", text, re.IGNORECASE):
        request_types.append("Discharge Summary")
    if request_types:
        info["records_requested"] = request_types

    return info

# =============================================================================
# AIRTABLE HELPERS
# =============================================================================

def create_records_request(patient_rec, sender_name, sender_email, sender_fax,
                            raw_text, source_id, direction, info, received_via):
    now = datetime.now()
    sla_deadline = now + timedelta(hours=48)

    needs_ledger = "Billing Ledger" in info.get("records_requested", [])

    fields = {
        "flduZQsoENFDjMIkj": f"RR-{now.strftime('%Y%m%d%H%M%S')}",
        "fldPUoFww6zfxWoda": "Inbound",
        "fldwQ42oTmKQFlVRh": info.get("patient_name", ""),
        "fldb8rJ7GBmxAXNs9": sender_name,
        "fldyg3YYWt2wCOCLN": "Attorney" if "law" in sender_name.lower() or "esq" in sender_name.lower() or "atty" in sender_name.lower() else "Other",
        "fldImJzfAok4go81l": now.strftime("%Y-%m-%d"),
        "fldEJVfgkAdVk6lXx": "Received",
        "fldmqrO6eJXqBBWLn": received_via,
        "fld320FIoKuTcofXg": received_via,
        "fld4F1eO3wZTtUrSU": sender_fax or sender_email or "",
        "fldcTuVabHvdyU1nx": sla_deadline.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fldayhhNzT6MTwTzn": needs_ledger,
        "fldWDDsl9tmNICsvw": raw_text[:5000] if raw_text else "",
        "flddL1WhrpFd8E5if": source_id,
        "fldktTuq0xq6qnQjV": False,
    }

    if info.get("dos_start"):
        fields["fldImJzfAok4go81l"] = now.strftime("%Y-%m-%d")
    if info.get("records_requested"):
        fields["fldB8qshVe4ButIRY"] = info["records_requested"]

    if patient_rec:
        fields["fldxg6a2IPOnuzs9X"] = [{"id": patient_rec["id"]}]
        fields["fldwQ42oTmKQFlVRh"] = patient_rec.get("fields", {}).get("fld0wmWONVDMBzgAQ", fields["fldwQ42oTmKQFlVRh"])

    fields = {k: v for k, v in fields.items() if v not in (None, "", [], False)}
    result = rr_tbl.create(fields)
    log.info(f"[RR] Created Records Request: {fields.get('flduZQsoENFDjMIkj')} for {fields.get('fldwQ42oTmKQFlVRh')}")
    return result

def write_patient_note(patient_rec, category, subject, note_body, created_by="System"):
    if not patient_rec:
        return None

    fields = {
        "fld3wVfdKBa7TGVAX": subject,
        "fldRWIqN2f9FGcFIl": category,
        "fldJS3Mt7duyTIh5K": note_body,
        "fldGXIhq9NurFduEs": created_by,
        "fldgCxoYhvNwzaLQc": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fldPXP2QyHHcWOAQc": patient_rec.get("fields", {}).get("fld0wmWONVDMBzgAQ", ""),
        "fldyfgNXd5zGuecb1": [{"id": patient_rec["id"]}],
        "fldUz4g2Be2Z5flQW": True,
        "fldZcALGKyhvJTDEo": int(patient_rec.get("fields", {}).get("fld4ogKYb3HOjcm4w", 0)) or None,
    }
    fields = {k: v for k, v in fields.items() if v not in (None, "", False)}
    result = notes_tbl.create(fields)
    log.info(f"[NOTE] Written: {subject}")
    return result

def log_communication(patient_rec, direction, doc_type, sent_to, address,
                       status, confirmation, pages, subject, note_text,
                       filename, filepath, sent_by="System"):
    now = datetime.now()
    fields = {
        "fldLQv9GFdXANvWym": f"COMM-{now.strftime('%Y%m%d%H%M%S')}",
        "fld6ULkFrVeRCABMb": direction,
        "fldgHF84i2POHAB49": doc_type,
        "fldKupuM6e9vsQnFy": sent_to,
        "flddpyNjGif741Hd4": address,
        "fldvqL4iJT8xWYtsV": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fldY6cs4wKSF5Xx7M": status,
        "fldcr0OPkFfj1ZDPH": confirmation or "",
        "fld10Adk3YMNYgWWA": pages or 0,
        "fldZocSlwdXlhDi53": subject,
        "fldjkg1Th6rjOoUyR": note_text,
        "fldkNuhxwfJYwe0Wj": filename or "",
        "fldGArpJGG2vLueKd": filepath or "",
        "fldSUp8jPY5bCQEbG": sent_by,
        "fldgT0K81yqiBq31t": True,
    }
    if patient_rec:
        fields["fldcLAliEnI4d2DkA"] = [{"id": patient_rec["id"]}]
        fields["fldFvb6UhHYdd1ZEg"] = patient_rec.get("fields", {}).get("fld0wmWONVDMBzgAQ", "")

    fields = {k: v for k, v in fields.items() if v not in (None, "", 0, False)}
    result = comms_tbl.create(fields)
    log.info(f"[COMM] Logged: {direction} - {subject}")
    return result

# =============================================================================
# FILE SAVING
# =============================================================================

def save_to_patient_folder(pdf_bytes, patient_rec, subfolder, filename):
    base_path = getattr(config, "CT_FILES_PATH", "\\\\CTSERVER-USF\\CTData\\PatFiles")
    pat_files_id = patient_rec.get("fields", {}).get("fldjYm8gD2qoewPjO") if patient_rec else None

    if not pat_files_id:
        log.warning("[FILE] No PatFilesID found - cannot save to CT folder")
        return None

    pat_id_int    = int(pat_files_id)
    parent_folder = str(pat_id_int)[-2:].zfill(2)
    folder        = os.path.join(base_path, parent_folder, str(pat_id_int), subfolder)

    try:
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, filename)
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)
        log.info(f"[FILE] Saved: {filepath}")
        return filepath
    except Exception as e:
        log.error(f"[FILE] Save failed: {e}")
        return None

# =============================================================================
# EMAIL SENDING
# =============================================================================

def send_records_email(to_email, to_name, patient_name, patient_dob,
                        attachments, sent_by_name, sent_by_ext,
                        custom_body=None):
    msg = email.mime.multipart.MIMEMultipart()
    msg["From"]    = f"{config.PRACTICE_NAME} <{config.SMTP_EMAIL}>"
    msg["To"]      = to_email
    msg["Subject"] = f"RE: Records and Billing Request - {patient_name}"

    body = custom_body or f"""Good afternoon,

I hope this message finds you well.

Please find attached the medical records for {patient_name}, date of birth {patient_dob}, as requested.

If you require any additional information or have questions regarding the enclosed documents, please do not hesitate to contact me. Thank you.

Kindly confirm receipt of this email at your earliest convenience.

Best regards,
{sent_by_name}
{config.PRACTICE_NAME}
Phone: {config.PRACTICE_PHONE} EXT {sent_by_ext}

Important Note: This email contains confidential medical information intended only for the use of the individual or entity to whom it is addressed. If you have received this in error, please notify the sender and delete the message immediately.

CONFIDENTIALITY NOTICE: This email message including files, if any, is intended for the person or entity to which it is addressed and may contain confidential and/or privileged material. Any unauthorized review, use, disclosure or distribution is prohibited. If you are not the intended recipient, please contact the sender by reply email and destroy all copies immediately."""

    msg.attach(email.mime.text.MIMEText(body, "plain"))

    for att in attachments:
        part = email.mime.application.MIMEApplication(att["data"], Name=att["filename"])
        part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
        msg.attach(part)

    try:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.SMTP_EMAIL, config.SMTP_PASS)
            server.sendmail(config.SMTP_EMAIL, to_email, msg.as_bytes())
        log.info(f"[EMAIL] Sent to {to_email} - {patient_name}")
        return True, msg.as_bytes()
    except Exception as e:
        log.error(f"[EMAIL] Send failed to {to_email}: {e}")
        return False, None

# =============================================================================
# INBOX CRAWLER - INBOUND REQUESTS
# =============================================================================

def get_processed_ids():
    processed_file = os.path.join(os.path.dirname(__file__), "processed_emails.txt")
    if os.path.exists(processed_file):
        with open(processed_file, "r") as f:
            return set(line.strip() for line in f.readlines())
    return set()

def mark_processed(email_id):
    processed_file = os.path.join(os.path.dirname(__file__), "processed_emails.txt")
    with open(processed_file, "a") as f:
        f.write(f"{email_id}\n")

def crawl_inbound_inbox():
    log.info("[INBOX] Crawling inbound fax/email inbox...")
    processed = get_processed_ids()
    new_requests = 0

    inboxes = [
        {
            "server": config.FAX_IMAP_SERVER,
            "port": config.FAX_IMAP_PORT,
            "email": config.FAX_EMAIL,
            "password": config.FAX_EMAIL_PASS,
            "type": "Inbound - Fax",
            "label": "fax inbox"
        },
        {
            "server": config.INFO_IMAP_SERVER,
            "port": config.INFO_IMAP_PORT,
            "email": config.INFO_EMAIL,
            "password": config.INFO_EMAIL_PASS,
            "type": "Inbound - Email",
            "label": "info inbox"
        }
    ]

    for inbox in inboxes:
        try:
            conn = get_imap_connection(
                inbox["server"], inbox["port"],
                inbox["email"], inbox["password"]
            )
            conn.select("INBOX")

            # Search last 30 days
            since_date = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
            _, msg_nums = conn.search(None, f'(SINCE "{since_date}")')

            for num in msg_nums[0].split():
                try:
                    _, data = conn.fetch(num, "(RFC822)")
                    raw = data[0][1]
                    msg = email.message_from_bytes(raw)

                    msg_id = msg.get("Message-ID", "")
                    uid    = hashlib.md5(f"{inbox['email']}{msg_id}{num}".encode()).hexdigest()

                    if uid in processed:
                        continue

                    subject = decode_mime_words(msg.get("Subject", ""))
                    from_raw = msg.get("From", "")
                    from_name, from_addr = parseaddr(from_raw)
                    from_name = decode_mime_words(from_name)
                    body = get_email_body(msg)
                    attachments = get_attachments(msg)

                    # Only process if it looks like a records request
                    keywords = ["records", "billing", "ledger", "notes", "medical",
                                "dates of service", "date of service", "request"]
                    combined = f"{subject} {body}".lower()
                    if not any(kw in combined for kw in keywords):
                        mark_processed(uid)
                        continue

                    log.info(f"[INBOX] Records request detected: '{subject}' from {from_addr}")

                    # Extract patient info
                    info = extract_patient_info_from_text(f"{subject}\n{body}")

                    # Try to find patient in Airtable
                    patient_rec = None
                    if info.get("patient_name"):
                        patient_rec = find_patient_by_name_dob(
                            info["patient_name"],
                            info.get("dob")
                        )

                    # Save any PDF attachments to patient folder
                    for att in attachments:
                        if att["filename"].lower().endswith(".pdf") and patient_rec:
                            save_to_patient_folder(
                                att["data"],
                                patient_rec,
                                "Records Requests",
                                f"REQUEST_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{att['filename']}"
                            )

                    # Create records request in Airtable
                    received_via = "Fax" if "fax" in inbox["label"] else "Email"
                    rr_rec = create_records_request(
                        patient_rec, from_name, from_addr,
                        None, f"{subject}\n\n{body}",
                        uid, "Inbound", info, received_via
                    )

                    # Write note to patient
                    if patient_rec:
                        note = (f"Inbound records request received from {from_name} "
                                f"({from_addr}) via {received_via} on "
                                f"{datetime.now().strftime('%m/%d/%Y at %I:%M %p')}. "
                                f"Subject: {subject}. "
                                f"Records requested: {', '.join(info.get('records_requested', ['Not specified']))}. "
                                f"SLA deadline: 48 hours.")
                        write_patient_note(
                            patient_rec,
                            "Attorney Communication",
                            f"Records request received - {from_name}",
                            note,
                            "System"
                        )

                    mark_processed(uid)
                    new_requests += 1
                    time.sleep(0.5)

                except Exception as e:
                    log.warning(f"[INBOX] Error processing message {num}: {e}")

            conn.logout()

        except Exception as e:
            log.error(f"[INBOX] Error connecting to {inbox['label']}: {e}")

    log.info(f"[INBOX] Crawl complete - {new_requests} new requests found")
    return new_requests

# =============================================================================
# SENT FOLDER CRAWLER
# =============================================================================

def crawl_sent_folder():
    log.info("[SENT] Crawling sent folder for records sent...")
    processed = get_processed_ids()
    saved = 0

    try:
        conn = get_imap_connection(
            config.INFO_IMAP_SERVER, config.INFO_IMAP_PORT,
            config.INFO_EMAIL, config.INFO_EMAIL_PASS
        )

        # Try common sent folder names
        sent_folders = ["Sent", "INBOX.Sent", "Sent Items", "Sent Messages"]
        selected = False
        for folder in sent_folders:
            try:
                result, _ = conn.select(f'"{folder}"')
                if result == "OK":
                    selected = True
                    log.info(f"[SENT] Using folder: {folder}")
                    break
            except Exception:
                continue

        if not selected:
            log.warning("[SENT] Could not find sent folder")
            conn.logout()
            return 0

        since_date = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
        _, msg_nums = conn.search(None, f'(SINCE "{since_date}")')

        for num in msg_nums[0].split():
            try:
                _, data = conn.fetch(num, "(RFC822)")
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                msg_id = msg.get("Message-ID", "")
                uid    = hashlib.md5(f"SENT{config.INFO_EMAIL}{msg_id}{num}".encode()).hexdigest()

                if uid in processed:
                    continue

                subject = decode_mime_words(msg.get("Subject", ""))
                to_raw  = msg.get("To", "")
                body    = get_email_body(msg)
                attachments = get_attachments(msg)

                # Only process if it looks like records we sent
                keywords = ["records", "billing", "medical", "request"]
                if not any(kw in subject.lower() for kw in keywords):
                    mark_processed(uid)
                    continue

                # Extract patient name from subject
                # Format: "RE: Records and Billing Request - Jay Patil"
                pat_match = re.search(r"[-:]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$", subject)
                patient_rec = None
                if pat_match:
                    patient_rec = find_patient_by_name_dob(pat_match.group(1))

                # Save the raw email as .eml to patient folder
                if patient_rec:
                    eml_filename = f"SENT_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{subject[:40].replace('/', '-').replace(':', '')}.eml"
                    save_to_patient_folder(
                        raw,
                        patient_rec,
                        "Records",
                        eml_filename
                    )

                    # Also save any PDF attachments
                    for att in attachments:
                        if att["filename"].lower().endswith(".pdf"):
                            save_to_patient_folder(
                                att["data"],
                                patient_rec,
                                "Records",
                                f"SENT_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{att['filename']}"
                            )

                    # Log to communications
                    to_name, to_addr = parseaddr(to_raw)
                    log_communication(
                        patient_rec,
                        "Outbound - Email",
                        "Medical Records",
                        decode_mime_words(to_name),
                        to_addr,
                        "Sent",
                        msg_id,
                        len(attachments),
                        subject,
                        f"Records sent via email on {datetime.now().strftime('%m/%d/%Y at %I:%M %p')}",
                        None,
                        None,
                        "System"
                    )

                    # Write note
                    to_display = decode_mime_words(to_name) or to_addr
                    note = (f"Records sent to {to_display} via email on "
                            f"{datetime.now().strftime('%m/%d/%Y at %I:%M %p')}. "
                            f"Subject: {subject}. "
                            f"{len(attachments)} attachment(s). "
                            f"Sent from: {config.INFO_EMAIL}.")
                    write_patient_note(
                        patient_rec,
                        "Attorney Communication",
                        f"Records sent - {to_display}",
                        note,
                        "System"
                    )

                mark_processed(uid)
                saved += 1
                time.sleep(0.5)

            except Exception as e:
                log.warning(f"[SENT] Error processing sent message {num}: {e}")

        conn.logout()

    except Exception as e:
        log.error(f"[SENT] Error connecting to sent folder: {e}")

    log.info(f"[SENT] Crawl complete - {saved} sent records processed")
    return saved

# =============================================================================
# SLA MONITOR
# =============================================================================

def check_sla_breaches():
    log.info("[SLA] Checking for 48-hour SLA breaches...")
    now = datetime.now()
    breached = 0

    try:
        open_requests = rr_tbl.all()
        for rec in open_requests:
            f = rec.get("fields", {})
            status = f.get("fldEJVfgkAdVk6lXx", "")
            deadline = f.get("fldcTuVabHvdyU1nx")
            already_flagged = f.get("fldJ1biICh00tDuyW", False)

            if status in ("Completed", "Cancelled"):
                continue
            if already_flagged:
                continue
            if not deadline:
                continue

            try:
                deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00")).replace(tzinfo=None)
                if now > deadline_dt:
                    rr_tbl.update(rec["id"], {"fldJ1biICh00tDuyW": True})
                    breached += 1
                    log.warning(f"[SLA] BREACH: {f.get('flduZQsoENFDjMIkj')} for {f.get('fldwQ42oTmKQFlVRh')} - past 48hr deadline")
            except Exception:
                pass

    except Exception as e:
        log.error(f"[SLA] Error checking SLA: {e}")

    if breached:
        log.warning(f"[SLA] {breached} records requests past 48-hour SLA deadline")
    else:
        log.info("[SLA] All records requests within SLA")

    return breached

# =============================================================================
# MAIN PIPELINE RUNNER
# =============================================================================

def run_records_pipeline():
    log.info("[PIPELINE] Running records pipeline...")
    crawl_inbound_inbox()
    crawl_sent_folder()
    check_sla_breaches()
    log.info("[PIPELINE] Records pipeline complete")
