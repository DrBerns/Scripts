"""
note_processor.py
Documentation Optimization — Suncoast SpineMed
Main processing engine. Monitors ChiroTouch for new open notes,
determines visit type, and applies appropriate section rebuild logic.
"""

import pyodbc
import anthropic
import random
import re
import time
import sys
import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# Cross-project env (shared with C:\CTSync\opqrst_trigger.py).
load_dotenv(r"C:\CTSync\.env")
AIRTABLE_TOKEN          = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID        = os.getenv("AIRTABLE_BASE_ID", "app8GGPbIMTkNSGnl")
AIRTABLE_PATIENTS_TABLE = os.getenv("AIRTABLE_PATIENTS_TABLE", "tblgSOi6KcS0lKn7W")

# Set from CLI in __main__. When True, all ChartText writes are suppressed
# and the script exits after one polling cycle.
DRY_RUN = False
from rtf_parser import (
    extract_variable_links,
    replace_links_with_placeholders,
    restore_links_from_placeholders,
    resolve_placeholders_to_values,
    rtf_to_plain_text,
    plain_text_to_rtf,
    extract_opqrst,
    extract_tagged_carry_forward,
    strip_tagged_carry_forward,
    extract_new_carry_forward,
    CF_TAG_START,
    CF_TAG_END,
    CT_RTF_EMPTY,
    CT_RTF_HEADER,
    CT_RTF_FOOTER,
)


# ─────────────────────────────────────────────────────────────
# DATABASE CONNECTION
# ─────────────────────────────────────────────────────────────

def get_db_connection():
    conn_str = (
        f"DRIVER={{SQL Server}};"
        f"SERVER={config.CT_SERVER};"
        f"DATABASE={config.CT_DATABASE};"
        f"UID={config.CT_USERNAME};"
        f"PWD={config.CT_PASSWORD};"
    )
    return pyodbc.connect(conn_str)


# ─────────────────────────────────────────────────────────────
# ANTHROPIC CLIENT
# ─────────────────────────────────────────────────────────────

anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


# ─────────────────────────────────────────────────────────────
# TEMPLATE LIBRARIES
# ─────────────────────────────────────────────────────────────

OBJECTIVE_TEMPLATES = [
    "Objective findings remain consistent with those documented at the most recent evaluation. No interval changes were identified to warrant re-examination at this time.",
    "Physical examination today revealed no new or interval findings. Objective findings are unchanged from the prior evaluation.",
    "No changes to the patient's objective clinical findings were identified today. Re-examination criteria have not been met at this time.",
    "Today's examination is consistent with previously documented objective findings. No clinical changes warranting formal re-evaluation were observed.",
    "Objective findings on examination today are unchanged from those recorded at the last formal evaluation. No re-exam is indicated at this time.",
]

ASSESSMENT_STATUS_TEMPLATES = [
    "The diagnostic picture remains unchanged at this time. The patient's functional status since the last visit is",
    "No modifications to the current diagnoses are indicated. The patient's functional status is noted as",
    "Current diagnoses continue to reflect the patient's clinical presentation. No diagnostic changes are warranted. Functional status since the last visit:",
    "The existing diagnoses remain appropriate and unchanged. The patient's reported functional status since the last visit is",
    "Diagnostic findings are consistent with the established working diagnoses. No revisions are indicated at this time. The patient's functional status is",
]

_last_obj_idx = -1
_last_assess_idx = -1


def get_rotated_template(templates, last_attr):
    global _last_obj_idx, _last_assess_idx
    last = globals()[last_attr]
    available = [i for i in range(len(templates)) if i != last]
    idx = random.choice(available)
    globals()[last_attr] = idx
    return templates[idx]


# ─────────────────────────────────────────────────────────────
# VISIT TYPE DETECTION
# ─────────────────────────────────────────────────────────────

def get_previous_visit_cpt(conn, patient_id, current_note_id):
    cursor = conn.cursor()
    query = (
        "SELECT TOP 1 t.Code "
        "FROM Transactions t "
        "JOIN ChartNotes cn ON cn.PatientID = t.PatID "
        "AND CAST(cn.NoteDate AS DATE) = CAST(t.TranDate AS DATE) "
        "WHERE t.PatID = ? "
        "AND cn.ID != ? "
        "AND cn.Status = 1 "
        "AND t.TranType = 'C' "
        "AND t.Code IN ('99202','99203','99204','99205',"
        "'99211','99212','99213','99214','99215',"
        "'98940','98941','98942') "
        "ORDER BY t.TranDate DESC"
    )
    cursor.execute(query, patient_id, current_note_id)
    row = cursor.fetchone()
    return row[0] if row else None


def determine_visit_type(prev_cpt):
    if prev_cpt is None:
        return 'ROUTINE'
    if prev_cpt in config.INITIAL_EVAL_CODES:
        return 'POST_INITIAL_EVAL'
    if prev_cpt in config.REEVAL_CODES:
        return 'POST_REEVAL'
    return 'ROUTINE'


# ─────────────────────────────────────────────────────────────
# AI SUBJECTIVE GENERATION
# ─────────────────────────────────────────────────────────────

# Default anatomic radiation suffix per region. The RTF link itself always
# shows the raw ChiroTouch value ("does not radiate"), so the AI writes prose
# like "The pain {{RADIATION_1}} into the shoulders, arms, or hands." — the
# link renders "does not radiate" and the suffix completes the anatomy.
RADIATION_SUFFIX = {
    'cervical':    'into the shoulders, arms, or hands',
    'neck':        'into the shoulders, arms, or hands',
    'thoracic':    'around the chest wall or into the abdomen',
    'mid thoracic':'around the chest wall or into the abdomen',
    'lumbar':      'into the buttocks, legs, or feet',
    'low back':    'into the buttocks, legs, or feet',
    'shoulder':    'proximally or distally',
    'elbow':       'proximally or distally',
    'wrist':       'proximally or distally',
    'hand':        'proximally or distally',
    'hip':         'proximally or distally',
    'knee':        'proximally or distally',
    'ankle':       'proximally or distally',
    'foot':        'proximally or distally',
    'headache':    '',
    'head':        '',
}


def _radiation_suffix_for(region):
    low = (region or '').lower()
    for key, suffix in RADIATION_SUFFIX.items():
        if key in low:
            return suffix
    return ''


def _clean_region_text(location_text):
    """Pull the anatomic region out of a full location-field string."""
    if not location_text:
        return ''
    # e.g. "Patient presents with complaints in the following location(s): cervical"
    #  or  "She also presents with additional complaints in the following location(s): lumbar"
    if ':' in location_text:
        return location_text.rsplit(':', 1)[-1].strip()
    return location_text.strip()


def _resolve_tokens_in_text(text, link_map):
    """Replace FIELD_PLACEHOLDER_* tokens in a string with their display values."""
    if not text:
        return ''
    for ph, link in link_map.items():
        if ph in text:
            text = text.replace(ph, link['display_value'])
    return text


def _attach_links_to_complaints(complaints, link_map):
    """Walk complaint dicts (with placeholder tokens as field values) and
    attach both the link objects (for RTF substitution) and the display
    values (for prompt construction)."""
    for c in complaints:
        # Quality — list of tokens
        q_toks = c.get('quality', []) or []
        c['quality_links'] = [link_map.get(t) for t in q_toks]
        c['quality_values'] = [
            link_map[t]['display_value'] if t in link_map else t for t in q_toks
        ]
        # Scalars
        for field in ('radiation', 'timing', 'change'):
            token = c.get(field, '') or ''
            lk = link_map.get(token.strip())
            c[f'{field}_link'] = lk
            c[f'{field}_value'] = (
                lk['display_value'].strip() if lk else _resolve_tokens_in_text(token, link_map).strip()
            )
        # Region (anatomic)
        c['location_value'] = _resolve_tokens_in_text(c.get('location', ''), link_map)
        c['region'] = _clean_region_text(c['location_value'])


# ─────────────────────────────────────────────────────────────
# PATIENT / CASE CONTEXT LOOKUPS
# ─────────────────────────────────────────────────────────────

# Values that appear in FirstName for placeholder / test patients. If the
# stored FirstName is one of these, we don't try to use it in the narrative —
# instead the AI falls back to "the patient" / "she" / "he".
_FIRST_NAME_BLOCKLIST = {
    '', 'female', 'male', 'patient', 'test', 'unknown', 'n/a', 'na',
    'tbd', 'pending', 'new patient',
}


def _get_patient_first_name(conn, patient_id):
    """Patients.FirstName is the only first-name column ChiroTouch stores. If
    the value is a placeholder / test string, return '' so the narrative
    omits the first name instead of calling the patient 'Female'."""
    cur = conn.cursor()
    cur.execute("SELECT FirstName FROM Patients WHERE ID = ?", patient_id)
    row = cur.fetchone()
    name = (row[0] or '').strip() if row else ''
    if name.lower() in _FIRST_NAME_BLOCKLIST:
        return ''
    return name


def _get_initial_eval_sptr(conn, patient_id, exclude_note_id):
    """Return SPtr of the patient's most recent initial eval (CPT 99203/99204)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT TOP 1 cn.SPtr "
        "FROM Transactions t "
        "JOIN ChartNotes cn ON cn.PatientID = t.PatID "
        " AND CAST(cn.NoteDate AS DATE) = CAST(t.TranDate AS DATE) "
        "WHERE t.PatID = ? AND cn.ID != ? "
        " AND t.TranType = 'C' AND t.Code IN ('99203','99204') "
        " AND cn.SPtr IS NOT NULL "
        "ORDER BY cn.NoteDate DESC",
        patient_id, exclude_note_id,
    )
    row = cur.fetchone()
    return row[0] if row else None


def _find_link_after_label(rtf_text, links, label):
    """Return the link whose raw block appears first AFTER the label string
    in the raw RTF. None if no label or no link."""
    idx = rtf_text.find(label)
    if idx == -1:
        return None
    best = None
    best_pos = None
    for link in links:
        pos = rtf_text.find(link['full_block'])
        if pos >= idx and (best_pos is None or pos < best_pos):
            best = link
            best_pos = pos
    return best


_MVC_TERMS = (
    'motor vehicle crash', 'motor vehicle', 'mvc', 'vehicle crash',
    'rear ended', 'rear-ended', 'collision', 'driver', 'passenger',
    'car crash', 'car accident',
)
_FALL_TERMS = ('slip and fall', 'slip-and-fall', 'slipped', 'fell', 'fall from', 'tripped')


def _infer_mechanism_from_rtf(rtf_text, links):
    rtf_np = replace_links_with_placeholders(rtf_text, links)
    plain = rtf_to_plain_text(rtf_np).lower()
    if any(t in plain for t in _MVC_TERMS):
        return 'motor vehicle crash'
    if any(t in plain for t in _FALL_TERMS):
        return 'slip and fall'
    return None


def _resolve_doi_and_mechanism(conn, patient_id, note_id, current_rtf, current_links):
    """Returns (doi_link_or_None, mech_link_or_None, mech_text_or_None).

    Checks the current note first, then falls back to the most recent initial
    eval (99203/99204). If no mechanism link exists anywhere, infers from
    prose ("motor vehicle crash" or "slip and fall").
    """
    doi_link = _find_link_after_label(current_rtf, current_links, 'Date of Injury:')
    mech_link = _find_link_after_label(current_rtf, current_links, 'Mechanism of Injury:')
    mech_text = None

    init_sptr = _get_initial_eval_sptr(conn, patient_id, note_id)
    if init_sptr:
        init_rtf = fetch_chart_text(conn, init_sptr)
        if init_rtf:
            init_links = extract_variable_links(init_rtf)
            if doi_link is None:
                doi_link = _find_link_after_label(init_rtf, init_links, 'Date of Injury:')
            if mech_link is None:
                mech_link = _find_link_after_label(init_rtf, init_links, 'Mechanism of Injury:')
            if mech_link is None:
                mech_text = _infer_mechanism_from_rtf(init_rtf, init_links)

    if mech_link is None and mech_text is None:
        # Last resort: scan the current note
        mech_text = _infer_mechanism_from_rtf(current_rtf, current_links)

    return doi_link, mech_link, mech_text


# ─────────────────────────────────────────────────────────────
# AI NARRATIVE (TOKEN-BASED)
# ─────────────────────────────────────────────────────────────

def _onset_causation_hint(onset_type):
    """Map the parsed onset_type into a causation-phrasing hint for the LLM."""
    low = (onset_type or '').lower()
    if 'caused by' in low:
        return ('caused by', [
            'caused by the motor vehicle crash',
            'resulting from the motor vehicle crash',
            'stemming from the same motor vehicle crash',
            'arising from the motor vehicle crash',
        ])
    if 'aggravated' in low:
        return ('aggravated by', [
            'aggravated by the motor vehicle crash',
            'made worse by the motor vehicle crash',
        ])
    return ('exacerbated by', [
        'exacerbated by the motor vehicle crash',
        'worsened by the motor vehicle crash',
        'made worse by the motor vehicle crash',
    ])


def generate_subjective_narrative(complaints, first_name, onset_type, has_onset_token=False):
    """AI narrative using {{QUALITY_N}}, {{RADIATION_N}}, {{TIMING_N}},
    {{CHANGE_N}} tokens — plus {{ONSET_TYPE}} when an onset-type RTF link
    exists to substitute in. Returns raw AI output (plain text with tokens)."""
    if not complaints:
        return ''

    cause_label, cause_phrasings = _onset_causation_hint(onset_type)

    complaint_blocks = []
    for i, c in enumerate(complaints, start=1):
        region = c.get('region') or c.get('location_value', '') or 'unspecified'
        q_vals = c.get('quality_values') or []
        q_display = ' and '.join(q_vals) if q_vals else 'aching'
        rad_display = c.get('radiation_value') or 'does not radiate'
        timing_display = c.get('timing_value') or 'intermittent'
        change_display = c.get('change_value') or ''
        has_change = bool(change_display)
        rad_suffix = _radiation_suffix_for(region)

        tokens = (
            f'{{{{QUALITY_{i}}}}}, {{{{RADIATION_{i}}}}}, {{{{TIMING_{i}}}}}'
            + (f', {{{{CHANGE_{i}}}}}' if has_change else ' (no CHANGE token — skip change clause)')
        )
        complaint_blocks.append(
            f'COMPLAINT {i}:\n'
            f'  Anatomic region      : {region}\n'
            f'  Quality value        : {q_display}\n'
            f'  Radiation value      : {rad_display}\n'
            f"  Radiation suffix hint: '{rad_suffix}' (append only when the value reads 'does not radiate')\n"
            f'  Timing value         : {timing_display}\n'
            f"  Change value         : {change_display or '(none — omit this clause)'}\n"
            f'  Tokens to use        : {tokens}'
        )

    count = len(complaints)
    if count == 1:
        opening = 'Write ONE opening sentence introducing the single complaint region by name.'
    elif count <= 3:
        opening = f'Write ONE opening sentence listing all {count} complaint regions together naturally.'
    else:
        opening = f'Write ONE opening sentence summarizing {count} ongoing complaint regions.'

    first_name_clause = (
        f'Refer to the patient by her/his first name "{first_name}" ONE or TWO times across the paragraphs (not every sentence, not in the opening line).'
        if first_name else
        'Refer to the patient in the third person (the patient / she / he / they).'
    )

    prompt = f"""You are a clinical documentation assistant writing the History of Present Illness (HPI) narrative for a chiropractic SOAP note, in the voice of a well-trained American physician dictating naturally.

TASK
{opening}
Then write ONE paragraph per complaint (in order), separated by blank lines. Each paragraph must incorporate the Quality, Radiation, Timing, and — if provided — Change-since-last-visit data by inserting the literal token markers shown for that complaint. Do NOT write out the field values directly; only use the tokens. They will be substituted with clickable clinical fields after you finish.

LANGUAGE RULES
- ALWAYS say "motor vehicle crash" or "MVC." NEVER write "motor vehicle accident," "MVA," or "accident."
- Causation phrasing uses "{cause_label}" — vary naturally across paragraphs using forms like: {', '.join(f'"{p}"' for p in cause_phrasings)}.
- {'Use the literal token {{ONSET_TYPE}} ONCE, in the opening sentence, in place of the causation verb phrase. Example: "all {{ONSET_TYPE}} the motor vehicle crash." The remaining complaint paragraphs must use hardcoded, varied causation phrasing (no {{ONSET_TYPE}} token in them).' if has_onset_token else 'Do NOT use {{ONSET_TYPE}}.'}
- {first_name_clause}
- Third person throughout (she / he / they).
- Each complaint paragraph is 2–4 sentences of natural clinical prose. No labels like "Quality:" or "Timing:".
- Do NOT add clinical content not present in the data.

TOKEN USAGE
- Quality: insert {{{{QUALITY_N}}}} where the adjective goes. Example: "a {{{{QUALITY_1}}}} pain".
- Radiation: insert {{{{RADIATION_N}}}} where the radiation phrase goes. If the radiation value is "does not radiate", follow {{{{RADIATION_N}}}} with the anatomic suffix hint for that region so the full phrase reads naturally. If the radiation value is anything else, the token replaces the whole phrase and no suffix is needed.
- Timing: insert {{{{TIMING_N}}}} where the timing phrase goes.
- Change: insert {{{{CHANGE_N}}}} only if a Change value is provided for that complaint; otherwise omit the change clause entirely.
- Emit tokens exactly as shown — double curly braces, uppercase, with the complaint number suffix.

COMPLAINT DATA
{chr(10).join(complaint_blocks)}

OUTPUT: Plain text only. The opening sentence, a blank line, then the numbered complaint paragraphs separated by blank lines. No headers, no bullets, no preamble, no trailing commentary.
"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ─────────────────────────────────────────────────────────────
# DIAGNOSIS LIST EXTRACTION
# Pulls ICD-10 diagnosis block from assessment plain text
# ─────────────────────────────────────────────────────────────

def extract_diagnosis_block(plain_text):
    """
    Finds the diagnosis list in the assessment plain text.
    ICD-10 codes start with a capital letter followed by digits.
    Returns the full diagnosis block as a string, or empty string if not found.
    """
    # Find first ICD-10 code pattern (e.g. G89.11, M54.2, S13.9XXA)
    match = re.search(r'([A-Z]\d{2}[^\n]*(?:\n[A-Z]\d{2}[^\n]*)*)', plain_text)
    if match:
        return match.group(1).strip()
    return ''


# ─────────────────────────────────────────────────────────────
# SECTION PROCESSORS
# ─────────────────────────────────────────────────────────────

def _build_token_to_placeholder_map(complaints, onset_placeholder):
    """Build a mapping from AI narrative tokens to ChiroTouch FIELD_PLACEHOLDER_*
    strings (NOT display values). The placeholders are substituted back to
    raw RTF hyperlink blocks later via restore_links_from_placeholders.
    """
    mapping = {}
    for i, c in enumerate(complaints, start=1):
        # Quality: list of placeholder tokens (one per quality adjective)
        q_placeholders = [t for t in (c.get('quality') or []) if t and t.startswith('FIELD_PLACEHOLDER_')]
        if q_placeholders:
            # Multiple quality links — ChiroTouch can render two inline, joined
            # with " and ". Both stay clickable.
            mapping[f'{{{{QUALITY_{i}}}}}'] = ' and '.join(q_placeholders)

        for field, token_prefix in (
            ('radiation', 'RADIATION'),
            ('timing',    'TIMING'),
            ('change',    'CHANGE'),
        ):
            tok = (c.get(field) or '').strip()
            if tok.startswith('FIELD_PLACEHOLDER_'):
                mapping[f'{{{{{token_prefix}_{i}}}}}'] = tok

    if onset_placeholder:
        mapping['{{ONSET_TYPE}}'] = onset_placeholder
    return mapping


def _encode_narrative_with_link_tokens(narrative, complaints, links, onset_placeholder):
    """Correct token → link flow, per user spec:
      1. Build {{TOKEN}} → FIELD_PLACEHOLDER_* mapping.
      2. Replace tokens in the AI narrative with their FIELD_PLACEHOLDER_*
         string (plain ASCII — not the display value, not the RTF block).
      3. RTF-escape the prose; placeholders survive the escape unchanged.
      4. restore_links_from_placeholders swaps every placeholder for its
         full clickable RTF hyperlink block.
    """
    token_map = _build_token_to_placeholder_map(complaints, onset_placeholder)

    # Step 2: tokens → placeholders
    text = narrative
    for token, placeholder in token_map.items():
        text = text.replace(token, placeholder)

    # If the AI emitted any tokens we don't have a link for, drop them so
    # they don't show up as {{QUALITY_1}} in the rendered note.
    text = re.sub(r'\{\{(?:QUALITY|RADIATION|TIMING|CHANGE|ONSET_TYPE)_?\d*\}\}', '', text)

    # Step 3: RTF-escape the prose
    text = _rtf_escape_content(text)

    # Step 4: placeholders → raw RTF hyperlink blocks
    text = restore_links_from_placeholders(text, links)
    return text


def _assemble_subjective_rtf(narrative_with_links_rtf, doi_link, mech_link,
                              mech_text, carry_forward_blocks):
    """Build the final Subjective RTF: header + bold section header + bold
    Mechanism/DOI lines + narrative + optional carry-forward + closer."""
    parts = [CT_RTF_HEADER]

    # Section header — bold, on its own paragraph
    parts.append(r'{\b History of Present Illness}\par\par ')

    # Mechanism line: bold label + value (link or bold text)
    parts.append(r'{\b Mechanism of Injury:} ')
    if mech_link:
        parts.append(mech_link['full_block'])
    elif mech_text:
        parts.append('{\\b ' + _rtf_escape_content(mech_text) + '}')
    parts.append(r'\par ')

    # DOI line: bold label + link
    parts.append(r'{\b Date of Injury:} ')
    if doi_link:
        parts.append(doi_link['full_block'])
    parts.append(r'\par\par ')

    # Narrative
    parts.append(narrative_with_links_rtf)

    # Carry-forward blocks (plain text, tagged)
    if carry_forward_blocks:
        cf_plain = '\n\n'.join(
            f'{CF_TAG_START}{b}{CF_TAG_END}' for b in carry_forward_blocks
        )
        parts.append(r'\par\par ')
        parts.append(_rtf_escape_content(cf_plain))

    parts.append(CT_RTF_FOOTER)  # \par}
    return ''.join(parts)


def process_subjective(rtf_text, visit_type, conn=None, patient_id=None, note_id=None):
    """Rebuild the Subjective section:
      • Bold "History of Present Illness" header
      • Bold "Mechanism of Injury: ..." line (link from initial eval, or
        inferred bold-text phrase)
      • Bold "Date of Injury: ..." line (link from initial eval)
      • Opening sentence naming every complaint region
      • One AI-written paragraph per complaint, with Quality / Radiation /
        Timing / Change preserved as clickable RTF fields
    """
    # 1. Extract all links + build plain-text view (placeholders intact so
    #    extract_opqrst can tie each field to a specific link).
    links = extract_variable_links(rtf_text)
    link_map = {link['placeholder']: link for link in links}
    rtf_no_links = replace_links_with_placeholders(rtf_text, links)
    plain_with_placeholders = rtf_to_plain_text(rtf_no_links)
    plain_debug = resolve_placeholders_to_values(plain_with_placeholders, links)
    print(f"    DEBUG subjective plain text:\n{plain_debug[:500]}")

    # 2. Carry-forward handling.
    plain_cleaned = plain_with_placeholders
    if visit_type in ('POST_INITIAL_EVAL', 'POST_REEVAL'):
        plain_cleaned = strip_tagged_carry_forward(plain_cleaned)
        carry_forward_blocks = []
    else:
        carry_forward_blocks = extract_tagged_carry_forward(plain_cleaned)
        plain_cleaned = strip_tagged_carry_forward(plain_cleaned)
        new_cf, plain_cleaned = extract_new_carry_forward(plain_cleaned)
        carry_forward_blocks.extend(new_cf)

    # 3. OPQRST — complaint dicts still contain placeholder tokens.
    complaints = extract_opqrst(plain_cleaned)
    print(f"    DEBUG complaints found: {len(complaints)}")

    if not complaints:
        print("    DEBUG: No complaints found, returning original RTF")
        return rtf_text

    # 3b. Fix onset_type: the onset phrase itself is a variable link, so the
    # plain-with-placeholders view hides "caused by" / "exacerbated by" from
    # the regex. Re-parse onset from the resolved view.
    plain_resolved = resolve_placeholders_to_values(plain_cleaned, links)
    resolved_blocks = [
        b.strip() for b in re.split(
            r'(?:Chief Complaint|Additional Complaint)(?!s)',
            plain_resolved, flags=re.IGNORECASE,
        )[1:]
        if b.strip()
    ]
    for c, rb in zip(complaints, resolved_blocks):
        m = re.search(
            r'This\s+(complaint\s+was\s+(?:caused|exacerbated|aggravated)\s+by)'
            r'\s+the\s+mechanism',
            rb, re.IGNORECASE,
        )
        if m:
            c['onset_type'] = m.group(1).lower()
        else:
            low = rb.lower()
            if 'caused by' in low:
                c['onset_type'] = 'complaint was caused by'
            elif 'aggravated' in low:
                c['onset_type'] = 'complaint was aggravated by'

    # 4. Map placeholders → link objects + display values per complaint.
    _attach_links_to_complaints(complaints, link_map)
    for c in complaints:
        print(
            f"      [{c['complaint_number']}] region={c.get('region','')!r} "
            f"quality={c.get('quality_values','')} "
            f"radiation={c.get('radiation_value','')!r} "
            f"timing={c.get('timing_value','')!r} "
            f"change={c.get('change_value','')!r} "
            f"onset={c.get('onset_type','')!r}"
        )

    # 5. Case-level context: DOI + mechanism from initial eval, patient name.
    if conn is not None and patient_id is not None:
        doi_link, mech_link, mech_text = _resolve_doi_and_mechanism(
            conn, patient_id, note_id, rtf_text, links
        )
        first_name = _get_patient_first_name(conn, patient_id)
    else:
        doi_link, mech_link, mech_text, first_name = None, None, None, ''
    print(
        f"    DEBUG doi_link={'yes' if doi_link else 'no'} "
        f"mech_link={'yes' if mech_link else 'no'} "
        f"mech_text={mech_text!r} first_name={first_name!r}"
    )

    # 6. Causation phrasing — onset_type is case-level, use the first
    #    complaint's parsed value. Also find the onset link placeholder so
    #    the AI can reference it via {{ONSET_TYPE}} and it stays clickable.
    onset_type = complaints[0].get('onset_type') or 'complaint was exacerbated by'
    onset_placeholder = None
    m = re.search(
        r'This\s+(FIELD_PLACEHOLDER_\d+_\d+)\s+the\s+mechanism',
        plain_cleaned,
    )
    if m:
        onset_placeholder = m.group(1)

    # 7. AI narrative (plain text with {{TOKEN_N}} markers).
    narrative = generate_subjective_narrative(
        complaints, first_name, onset_type, has_onset_token=bool(onset_placeholder)
    )
    print(f"    DEBUG narrative (first 400):\n{narrative[:400]}")

    # 8. Encode narrative as RTF content with link blocks substituted.
    narrative_rtf = _encode_narrative_with_link_tokens(
        narrative, complaints, links, onset_placeholder
    )

    # 9. Wrap in full RTF with bold header, Mechanism, DOI, carry-forward.
    return _assemble_subjective_rtf(
        narrative_rtf, doi_link, mech_link, mech_text, carry_forward_blocks
    )


def process_objective(visit_type):
    """
    Returns RTF for Objective section.
    POST_INITIAL_EVAL / POST_REEVAL: blank (provider enters fresh findings)
    ROUTINE: rotated no-change template
    """
    if visit_type in ('POST_INITIAL_EVAL', 'POST_REEVAL'):
        return CT_RTF_EMPTY
    template = get_rotated_template(OBJECTIVE_TEMPLATES, '_last_obj_idx')
    return plain_text_to_rtf(template)


CONTENT_START_MARKER = r'\pard\itap0\nowidctlpar\plain\f1\fs24'

# \b is the RTF bold-on toggle. It's delimited by any non-alphanumeric char,
# so \b is followed by \par, \cf, space, etc. — NOT necessarily whitespace.
_B_TOGGLE_RE = re.compile(r'\\b(?![a-zA-Z0-9])')


def _rtf_escape_content(text):
    """Encode plain text as RTF content. Placeholders pass through unchanged."""
    content = text.replace('\\', '\\\\')
    content = content.replace('{', '\\{').replace('}', '\\}')
    content = content.replace('\r\n', '\n')
    content = content.replace('\n\n', '\\par\\par ')
    content = content.replace('\n', '\\par ')
    return content


def _restore_links_in_fragment(fragment, links):
    """Swap placeholder tokens for their raw RTF blocks.
    Placeholders are plain ASCII (FIELD_PLACEHOLDER_*), so they pass through
    _rtf_escape_content unchanged and we can substitute raw RTF here.
    """
    for link in links:
        fragment = fragment.replace(link['placeholder'], link['full_block'], 1)
    return fragment


def _build_status_sentence_text(links):
    """Return the rotated status sentence as plain text with placeholder tokens."""
    template = get_rotated_template(ASSESSMENT_STATUS_TEMPLATES, '_last_assess_idx')
    functional_link = ''
    for link in links:
        if link['field_id'] == '86655':
            functional_link = link['placeholder']
            break
    if functional_link:
        return f"{template} {functional_link}."
    return f"{template} unchanged."


def _process_assessment_flat(rtf_text, visit_type):
    """Legacy rebuild path when the RTF has no 'Primary Diagnoses' anchor
    (typically a note that was flattened by a prior run)."""
    links = extract_variable_links(rtf_text)
    rtf_no_links = replace_links_with_placeholders(rtf_text, links)
    plain = rtf_to_plain_text(rtf_no_links)

    if visit_type in ('POST_INITIAL_EVAL', 'POST_REEVAL'):
        plain = strip_tagged_carry_forward(plain)
        carry_forward_blocks = []
    else:
        carry_forward_blocks = extract_tagged_carry_forward(plain)
        plain = strip_tagged_carry_forward(plain)
        new_cf, plain = extract_new_carry_forward(plain)
        carry_forward_blocks.extend(new_cf)

    diagnosis_block = extract_diagnosis_block(plain)
    status_sentence = _build_status_sentence_text(links)

    parts = [status_sentence]
    if diagnosis_block:
        parts.append(diagnosis_block)
    if carry_forward_blocks:
        parts.append('\n\n'.join(
            f'{CF_TAG_START}{b}{CF_TAG_END}' for b in carry_forward_blocks
        ))
    full_assessment = '\n\n'.join(parts)
    full_assessment_with_links = restore_links_from_placeholders(full_assessment, links)
    return plain_text_to_rtf(full_assessment_with_links)


def process_assessment(rtf_text, visit_type):
    """
    Processes Assessment section.
    Finds the "Primary Diagnoses" header in the raw RTF, rewrites only the
    status sentence slot before it with the rotated template, and keeps
    everything from the bold diagnosis header through the end of the RTF
    byte-for-byte so bold headers (Primary / Additional / Complicating
    Diagnoses), bullet points, and closing \\par} all survive.
    """
    links = extract_variable_links(rtf_text)

    # Debug view of the pre-split raw plain text — tells us whether the
    # anchor is in the text and what shape the pre-anchor slot has.
    rtf_no_links = replace_links_with_placeholders(rtf_text, links)
    plain_debug = resolve_placeholders_to_values(rtf_to_plain_text(rtf_no_links), links)
    print(f"    DEBUG assessment plain text (first 300):\n      {plain_debug[:300]!r}")

    # Anchor: split at the earliest of "Prognosis" or "Primary Diagnoses",
    # whichever comes first in the raw RTF. Preserving both (when Prognosis
    # leads) keeps the prognosis paragraph + causation + diagnosis list all
    # byte-for-byte so bold headers and bullet formatting survive. Walk
    # backward from the anchor to the nearest \b toggle so the bold state
    # that activates the header stays in the tail.
    prog_idx = rtf_text.find('Prognosis')
    pd_idx   = rtf_text.find('Primary Diagnoses')
    anchor_candidates = [p for p in (prog_idx, pd_idx) if p != -1]
    if not anchor_candidates:
        print("    DEBUG assessment: no Prognosis/Primary-Diagnoses anchor — flat rebuild")
        return _process_assessment_flat(rtf_text, visit_type)
    anchor_idx = min(anchor_candidates)
    anchor_label = 'Prognosis' if anchor_idx == prog_idx else 'Primary Diagnoses'

    b_matches = list(_B_TOGGLE_RE.finditer(rtf_text, 0, anchor_idx))
    diag_start = b_matches[-1].start() if b_matches else anchor_idx
    print(
        f"    DEBUG assessment anchor: {anchor_label!r} at {anchor_idx} "
        f"split_at={diag_start}"
    )

    # Carry-forward lives in the status-sentence slot; extract from the plain
    # text view of that slot only (not from the diagnosis block).
    if visit_type in ('POST_INITIAL_EVAL', 'POST_REEVAL'):
        carry_forward_blocks = []
    else:
        slot_rtf = rtf_text[:diag_start]
        slot_rtf_no_links = replace_links_with_placeholders(slot_rtf, links)
        slot_plain = rtf_to_plain_text(slot_rtf_no_links)
        carry_forward_blocks = extract_tagged_carry_forward(slot_plain)
        slot_plain = strip_tagged_carry_forward(slot_plain)
        new_cf, _ = extract_new_carry_forward(slot_plain)
        carry_forward_blocks.extend(new_cf)

    # Preserve the original RTF header (incl. listtable / listoverridetable
    # that drive bullet rendering). Split at the last content-start marker
    # before the diagnosis block — everything up through it is the header,
    # everything between it and the diagnosis block is the status-sentence
    # slot to be rewritten.
    content_start = rtf_text.rfind(CONTENT_START_MARKER, 0, diag_start)
    if content_start == -1:
        header_rtf = CT_RTF_HEADER.rstrip()
    else:
        header_rtf = rtf_text[:content_start + len(CONTENT_START_MARKER)]

    diagnosis_rtf = rtf_text[diag_start:]  # bold header + bullets + closing \par}

    # Strip ONLY the causation attestation paragraph from the tail, leaving
    # any content that follows (Primary Diagnoses list, etc.) untouched. The
    # causation paragraph runs from the opening phrase through its
    # end-of-paragraph marker (\par\par or a single \par).
    diagnosis_rtf = re.sub(
        r'(?:\\par\\par\s*|\\par\s*)?'
        r'It is my opinion based on a reasonable degree of medical certainty'
        r'.*?'
        r'(?:\\par\\par|\\par)',
        '',
        diagnosis_rtf,
        count=1,
        flags=re.DOTALL,
    )

    # Force a paragraph break right before the "Prognosis" label so it
    # visually separates from the rotation sentence. The ChiroTouch template
    # writes this as `\b\par\par Prognosis` where \b toggles bold and then
    # two paragraph breaks follow — we prepend one more \par so there's a
    # guaranteed blank line in renderers that collapse adjacent \par runs.
    diagnosis_rtf = re.sub(
        r'(\\b\\par\\par\s*Prognosis)',
        r'\\par \1',
        diagnosis_rtf,
        count=1,
    )

    print(f"    DEBUG diagnosis block preserved: {len(diagnosis_rtf)} bytes")

    # Build new status sentence (plain text with placeholder tokens), encode
    # to an RTF content fragment, then swap placeholders for raw link blocks.
    status_plain = _build_status_sentence_text(links)
    status_rtf = _restore_links_in_fragment(_rtf_escape_content(status_plain), links)

    pieces = [header_rtf, ' ', status_rtf]
    if carry_forward_blocks:
        cf_plain = '\n\n'.join(
            f'{CF_TAG_START}{b}{CF_TAG_END}' for b in carry_forward_blocks
        )
        pieces.append('\\par\\par ')
        pieces.append(_restore_links_in_fragment(_rtf_escape_content(cf_plain), links))
    pieces.append('\\par\\par ')
    pieces.append(diagnosis_rtf)

    return ''.join(pieces)


# ─────────────────────────────────────────────────────────────
# OPQRST INTEGRATION (Airtable → plain narrative → RTF envelope)
# ─────────────────────────────────────────────────────────────
#
# When a patient checks in via opqrst_trigger.py and submits the OPQRST form,
# Airtable's `OPQRST Last Response` field holds the JSON payload. This block
# pulls that payload, generates a plain-prose Subjective narrative, and wraps
# it in a minimal RTF envelope that update_chart_text can write through the
# patient's SPtr → ChartText chain. Replaces the AI-rebuilt Subjective for
# that visit (loses clickable RTF link fields — the OPQRST data path doesn't
# carry placeholder tokens).

# Maps OPQRST status enum to natural-prose phrasing.
_OPQRST_STATUS_PHRASE = {
    "worse":  "worsening",
    "same":   "unchanged",
    "better": "improving",
}


def get_opqrst_from_airtable(ct_patient_id):
    """Look up an Airtable Patients record by CT Patient ID.

    Returns the parsed `OPQRST Last Response` JSON dict iff it exists AND was
    submitted today. Returns None on any miss/skip and logs the reason.
    """
    if not AIRTABLE_TOKEN:
        print(f"  [OPQRST] AIRTABLE_TOKEN not set — skipping lookup")
        return None
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_PATIENTS_TABLE}"
    params = {
        "filterByFormula": f"({{CT Patient ID}}={int(ct_patient_id)})",
        "fields[]": ["fld4ogKYb3HOjcm4w", "fldLgvtcAFD5P18aK", "fld1a8oQRwjiJBb4N"],
        "maxRecords": 1,
    }
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
    except requests.RequestException as e:
        print(f"  [OPQRST] Airtable request failed: {e}")
        return None
    if resp.status_code != 200:
        print(f"  [OPQRST] Airtable HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    records = resp.json().get("records", [])
    if not records:
        print(f"  [OPQRST] No Airtable record for CT Patient ID {ct_patient_id}")
        return None
    fields = records[0].get("fields", {})
    blob = fields.get("fldLgvtcAFD5P18aK") or fields.get("OPQRST Last Response")
    submitted_date = (
        fields.get("fld1a8oQRwjiJBb4N") or fields.get("OPQRST Last Response Date")
    )
    today_str = datetime.now().strftime("%Y-%m-%d")
    if submitted_date != today_str:
        print(f"  [OPQRST] response date is {submitted_date!r}, not today ({today_str}) — skipping")
        return None
    if not blob:
        print(f"  [OPQRST] OPQRST Last Response is empty for CT Patient ID {ct_patient_id}")
        return None
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"  [OPQRST] Failed to parse JSON: {e}")
        return None


def build_subjective_narrative(opqrst):
    """Convert parsed OPQRST JSON into a plain-prose Subjective narrative.

    One paragraph per non-bypassed complaint, paragraphs separated by blank
    lines. Returns '' if there's nothing renderable.
    """
    if not isinstance(opqrst, dict):
        return ""
    complaints = opqrst.get("complaints") or []
    blocks = []
    for c in complaints:
        if c.get("bypassed"):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        name_l = name.lower()
        verb = "are" if name_l.endswith("s") else "is"

        quality = ", ".join(q for q in (c.get("quality") or []) if q).lower()
        timing = (c.get("timing") or "").strip().lower()
        sev = c.get("severity") or {}
        avg = sev.get("avg")
        worst = sev.get("worst")
        status_raw = (c.get("status") or "").strip().lower()
        status_phrase = _OPQRST_STATUS_PHRASE.get(status_raw, "changed")
        onset = (c.get("onset") or "").strip()
        provokes = (c.get("provokes") or "").strip()
        relieves = (c.get("relieves") or "").strip()
        radiation_list = [r for r in (c.get("radiation") or []) if r]

        sentences = [
            f"Patient reports {name_l} that {verb} {quality} in nature, "
            f"{timing}, rated {avg}/10 on average and {worst}/10 at worst.",
            f"Symptoms are {status_phrase} since last visit.",
        ]
        if onset:
            sentences.append(f"Onset: {onset}.")
        if provokes:
            sentences.append(f"Aggravating factors: {provokes}.")
        if relieves:
            sentences.append(f"Relieving factors: {relieves}.")
        if radiation_list:
            sentences.append(f"Radiation/referral to: {', '.join(radiation_list)}.")
        blocks.append(" ".join(sentences))
    return "\n\n".join(blocks)


def _build_opqrst_subjective_rtf(narrative_plain):
    """Wrap a plain-prose narrative in a minimal RTF envelope mirroring the
    bold 'History of Present Illness' header that _assemble_subjective_rtf
    emits, so the result renders consistently in ChiroTouch."""
    if not narrative_plain:
        return ""
    parts = [CT_RTF_HEADER]
    parts.append(r'{\b History of Present Illness}\par\par ')
    parts.append(_rtf_escape_content(narrative_plain))
    parts.append(CT_RTF_FOOTER)
    return ''.join(parts)


# ─────────────────────────────────────────────────────────────
# DATABASE WRITER
# ─────────────────────────────────────────────────────────────

CHART_TEXT_CHUNK_SIZE = 1024


CHART_TEXT_CHAIN_END = 0  # ChiroTouch uses NextPtr = 0 to mark end of chain


def _walk_chain(cursor, head_ptr):
    """Return ordered list of Ptr values in the linked ChartText chain."""
    chain = []
    p = head_ptr
    while p:
        cursor.execute("SELECT NextPtr FROM ChartText WHERE Ptr = ?", p)
        row = cursor.fetchone()
        if not row:
            break
        chain.append(p)
        p = row[0]
    return chain


def fetch_chart_text(conn, head_ptr):
    """Read the full RTF by walking the NextPtr chain.

    ChiroTouch stores TextBody as a linked list of up to 1024-byte chunks.
    Reading only the head row truncates at the first chunk.
    """
    if not head_ptr:
        return None
    cursor = conn.cursor()
    parts = []
    p = head_ptr
    while p:
        cursor.execute("SELECT TextBody, NextPtr FROM ChartText WHERE Ptr = ?", p)
        row = cursor.fetchone()
        if not row:
            break
        parts.append(row[0] or '')
        p = row[1]
    return ''.join(parts)


def update_chart_text(conn, head_ptr, new_rtf):
    """Write new_rtf across the existing ChartText chain.

    Reuses existing chain slots in chunk-size pieces. If the new content
    needs more chunks than the existing chain provides, additional rows
    are inserted with freshly allocated Ptrs. Unused tail slots become
    orphans (we null out the last-used NextPtr).
    """
    cursor = conn.cursor()
    chunks = [
        new_rtf[i:i + CHART_TEXT_CHUNK_SIZE]
        for i in range(0, len(new_rtf), CHART_TEXT_CHUNK_SIZE)
    ] or ['']

    chain = _walk_chain(cursor, head_ptr)
    if not chain:
        raise ValueError(f"ChartText head Ptr {head_ptr} not found")

    # Extend chain if new content doesn't fit
    while len(chain) < len(chunks):
        cursor.execute("SELECT MAX(Ptr) + 1 FROM ChartText")
        new_ptr = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO ChartText (Ptr, TextBody, NextPtr) VALUES (?, ?, ?)",
            new_ptr, '', CHART_TEXT_CHAIN_END
        )
        chain.append(new_ptr)

    # Write chunks to chain slots, linking each to the next. Last used slot
    # gets the sentinel end-of-chain value so ChiroTouch stops reading there.
    for i, chunk in enumerate(chunks):
        slot = chain[i]
        if i + 1 < len(chunks):
            next_ptr = chain[i + 1]
        else:
            next_ptr = CHART_TEXT_CHAIN_END
        cursor.execute(
            "UPDATE ChartText SET TextBody = ?, NextPtr = ? WHERE Ptr = ?",
            chunk, next_ptr, slot
        )

    conn.commit()


# ─────────────────────────────────────────────────────────────
# MAIN PROCESSOR
# ─────────────────────────────────────────────────────────────

def process_note(conn, note_id, patient_id, s_ptr, o_ptr, a_ptr):
    mode = " [DRY RUN]" if DRY_RUN else ""
    print(f"[{datetime.now()}] Processing note ID {note_id} for patient {patient_id}{mode}")

    prev_cpt = get_previous_visit_cpt(conn, patient_id, note_id)
    visit_type = determine_visit_type(prev_cpt)
    print(f"  Previous CPT: {prev_cpt} -> Visit type: {visit_type}")

    s_text = fetch_chart_text(conn, s_ptr)
    o_text = fetch_chart_text(conn, o_ptr)
    a_text = fetch_chart_text(conn, a_ptr)

    # Process Subjective — try OPQRST (Airtable) first; fall back to AI rebuild.
    if s_text and s_ptr:
        try:
            opqrst = get_opqrst_from_airtable(patient_id)
            if opqrst:
                narrative = build_subjective_narrative(opqrst)
                new_s = _build_opqrst_subjective_rtf(narrative)
                source = "OPQRST (Airtable)"
                if DRY_RUN:
                    print(
                        f"  [DRY RUN] Would write Subjective ({source}) to "
                        f"ChartNotes.ID={note_id} via SPtr={s_ptr} "
                        f"(rtf={len(new_s)} bytes)"
                    )
                    print(f"  [DRY RUN] Narrative:\n{narrative}")
                else:
                    update_chart_text(conn, s_ptr, new_s)
                    print(f"  Subjective processed ({source})")
            else:
                if DRY_RUN:
                    print(
                        f"  [DRY RUN] No OPQRST data for PatientID={patient_id} — "
                        f"would fall back to AI rebuild (skipped in dry-run)"
                    )
                else:
                    new_s = process_subjective(
                        s_text, visit_type,
                        conn=conn, patient_id=patient_id, note_id=note_id,
                    )
                    update_chart_text(conn, s_ptr, new_s)
                    print(f"  Subjective processed (AI rebuild)")
        except Exception as e:
            import traceback
            print(f"  Subjective error: {e}")
            traceback.print_exc()

    # Process Objective
    if o_ptr:
        try:
            if DRY_RUN:
                print(f"  [DRY RUN] Would process Objective for OPtr={o_ptr} (skipped)")
            else:
                new_o = process_objective(visit_type)
                update_chart_text(conn, o_ptr, new_o)
                print(f"  Objective processed")
        except Exception as e:
            print(f"  Objective error: {e}")

    # Process Assessment
    if a_text and a_ptr:
        try:
            if DRY_RUN:
                print(f"  [DRY RUN] Would process Assessment for APtr={a_ptr} (skipped)")
            else:
                new_a = process_assessment(a_text, visit_type)
                update_chart_text(conn, a_ptr, new_a)
                print(f"  Assessment processed")
        except Exception as e:
            import traceback
            print(f"  Assessment error: {e}")
            traceback.print_exc()

    print(f"  Note {note_id} complete{mode}")


# ─────────────────────────────────────────────────────────────
# MONITOR LOOP
# ─────────────────────────────────────────────────────────────

def get_new_open_notes(conn, processed_ids):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cn.ID, cn.PatientID, cn.SPtr, cn.OPtr, cn.APtr
        FROM ChartNotes cn
        WHERE cn.Status IS NULL
          AND CAST(cn.NoteDate AS DATE) = CAST(GETDATE() AS DATE)
          AND cn.SPtr IS NOT NULL
    """)
    rows = cursor.fetchall()
    return [r for r in rows if r[0] not in processed_ids]


def run_monitor(poll_interval_seconds=30):
    print(f"[{datetime.now()}] Documentation Optimization monitor started"
          f"{' [DRY RUN]' if DRY_RUN else ''}.")
    if DRY_RUN:
        print(f"  Dry-run mode: one polling cycle, no writes, then exit.\n")
    else:
        print(f"  Polling every {poll_interval_seconds} seconds. Press Ctrl+C to stop.\n")

    processed_ids = set()

    try:
        conn = get_db_connection()
        print(f"  Database connected: {config.CT_SERVER}/{config.CT_DATABASE}\n")

        while True:
            new_notes = get_new_open_notes(conn, processed_ids)

            if new_notes:
                print(f"[{datetime.now()}] Found {len(new_notes)} new note(s) to process.")
                for note in new_notes:
                    note_id, patient_id, s_ptr, o_ptr, a_ptr = note
                    try:
                        process_note(conn, note_id, patient_id, s_ptr, o_ptr, a_ptr)
                        processed_ids.add(note_id)
                    except Exception as e:
                        import traceback
                        print(f"  Error processing note {note_id}: {e}")
                        traceback.print_exc()
            else:
                print(f"[{datetime.now()}] No new notes.")

            if DRY_RUN:
                print(f"[{datetime.now()}] Dry-run complete; exiting.")
                return

            time.sleep(poll_interval_seconds)

    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Monitor stopped.")
    except Exception as e:
        import traceback
        print(f"\n[{datetime.now()}] Fatal error: {e}")
        traceback.print_exc()


if __name__ == '__main__':
    DRY_RUN = "--dry-run" in sys.argv
    run_monitor(poll_interval_seconds=30)