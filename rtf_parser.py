"""
rtf_parser.py
Documentation Optimization — Suncoast SpineMed
"""

import re


CT_RTF_HEADER = (
    r'{\rtf1\ansi\ansicpg1252\uc1\deff0'
    r'{\fonttbl'
    r'{\f0\fnil\fcharset0\fprq2 Arial;}'
    r'{\f1\fswiss\fcharset0\fprq2 Arial;}'
    r'{\f2\froman\fcharset2\fprq2 Symbol;}}'
    r'{\colortbl;\red0\green0\blue0;\red255\green255\blue255;'
    r'\red0\green0\blue255;\red0\green0\blue0;}'
    r'{\stylesheet{\s0\itap0\nowidctlpar\f0\fs24 [Normal];}'
    r'{\*\cs10\additive Default Paragraph Font;}}'
    r'{\*\generator TX_RTF32 16.0.534.502;}'
    r'\deftab1134\paperw12240\paperh15840'
    r'\margl1440\margt1440\margr1440\margb1440'
    r'\widowctrl\formshade\sectd'
    r'\headery720\footery720'
    r'\pgwsxn12240\pghsxn15840'
    r'\marglsxn1440\margtsxn1440\margrsxn1440\margbsxn1440'
    r'\pard\itap0\nowidctlpar\plain\f1\fs24 '
)

CT_RTF_FOOTER = r'\par}'
CT_RTF_EMPTY = CT_RTF_HEADER + CT_RTF_FOOTER


def plain_text_to_rtf(plain_text):
    if not plain_text or not plain_text.strip():
        return CT_RTF_EMPTY
    content = plain_text
    content = content.replace('\\', '\\\\')
    content = content.replace('{', '\\{')
    content = content.replace('}', '\\}')
    content = content.replace('\r\n', '\n')
    content = content.replace('\n\n', '\\par\\par ')
    content = content.replace('\n', '\\par ')
    return CT_RTF_HEADER + content + CT_RTF_FOOTER


def remove_rtf_group(s, start_pos):
    """
    Given a string and the position of an opening brace,
    returns the position of the matching closing brace.
    """
    depth = 0
    i = start_pos
    while i < len(s):
        if s[i] == '{':
            depth += 1
        elif s[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(s) - 1


def rtf_to_plain_text(rtf_text):
    if not rtf_text:
        return ''

    text = rtf_text

    # Named top-level groups whose contents are pure metadata and must be
    # stripped whole. Any {\*\...} ignorable destinations are handled below
    # by the generic stripper.
    header_keywords = [
        '\\fonttbl',
        '\\colortbl',
        '\\stylesheet',
        '\\listtable',
        '\\listoverridetable',
        '\\txfielddef',
    ]

    # Before stripping txfielddef groups, extract the \fldrslt display value
    # from any {\field ...} constructs so we keep the user-visible content.
    text = _inline_field_results(text)

    # Repeatedly scan and remove metadata groups (named headers + any
    # {\*\xxx ...} ignorable destination). Loop until nothing changes so
    # nested groups get cleaned up.
    changed = True
    while changed:
        changed = False
        i = 0
        result = []
        while i < len(text):
            if text[i] == '{':
                after = text[i+1:]
                after_stripped = after.lstrip(' ')
                is_ignorable_dest = after_stripped.startswith('\\*')
                is_named_header = any(
                    after_stripped.startswith(kw) for kw in header_keywords
                )
                if is_ignorable_dest or is_named_header:
                    end = remove_rtf_group(text, i)
                    i = end + 1
                    changed = True
                else:
                    result.append(text[i])
                    i += 1
            else:
                result.append(text[i])
                i += 1
        text = ''.join(result)

    # Convert \par to newlines
    text = re.sub(r'\\par\b\s*', '\n', text)

    # Convert \tab to space
    text = re.sub(r'\\tab\b\s*', ' ', text)

    # \uN ? — Unicode escape followed by fallback char. Decode to the
    # unicode codepoint and drop the single fallback char.
    def _uni_sub(m):
        code = int(m.group(1))
        if code < 0:
            code += 65536
        try:
            return chr(code)
        except ValueError:
            return ''
    text = re.sub(r"\\u(-?\d+)\s?\??", _uni_sub, text)

    # Remove \'hh hex escapes
    text = re.sub(r"\\'[0-9a-fA-F]{2}", '', text)

    # Remove remaining RTF control words (\word, optionally with numeric arg)
    text = re.sub(r'\\[a-zA-Z]+\-?\d*\s?', '', text)

    # Remove escaped braces / backslashes artifacts and stray braces
    text = text.replace('\\{', '{').replace('\\}', '}').replace('\\\\', '\\')
    text = re.sub(r'[{}]', '', text)

    # Clean whitespace
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def _inline_field_results(text):
    """Replace {\\field ... {\\fldrslt{VALUE}} ...} groups with VALUE.

    This keeps user-visible hyperlinked content when the caller did not
    first substitute placeholders via extract_variable_links.
    """
    result = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '{' and text[i+1:i+7] == '\\field':
            end = remove_rtf_group(text, i)
            group = text[i:end+1]
            m = re.search(r'\\fldrslt\s*\{([^{}]*)\}', group)
            if m:
                result.append(m.group(1))
            i = end + 1
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def extract_variable_links(rtf_text):
    links = []
    pattern = re.compile(
        r'\{\\txfielddef'
        r'(?:(?!\{\\txfielddef).)*?'
        r'HYPERLINK\s+"(\d+)"'
        r'(?:(?!\{\\txfielddef).)*?'
        r'\{\\fldrslt\{([^}]*)\}'
        r'(?:(?!\{\\txfielddef).)*?'
        r'\}\}\}',
        re.DOTALL
    )
    occurrence_counts = {}
    for match in pattern.finditer(rtf_text):
        field_id = match.group(1)
        display_value = match.group(2).strip()
        full_block = match.group(0)
        occ = occurrence_counts.get(field_id, 0)
        occurrence_counts[field_id] = occ + 1
        placeholder = f'FIELD_PLACEHOLDER_{field_id}_{occ}'
        links.append({
            'field_id': field_id,
            'display_value': display_value,
            'full_block': full_block,
            'placeholder': placeholder
        })
    return links


def replace_links_with_placeholders(rtf_text, links):
    result = rtf_text
    for link in links:
        result = result.replace(link['full_block'], link['placeholder'], 1)
    return result


def restore_links_from_placeholders(text, links):
    result = text
    for link in links:
        result = result.replace(link['placeholder'], link['full_block'], 1)
    return result


def resolve_placeholders_to_values(text, links):
    """Replace FIELD_PLACEHOLDER_* tokens with their display values.

    Used when feeding plain text to the LLM — the model needs the actual
    clinical content ("cervical", "7/28/2025") rather than opaque tokens.
    """
    result = text
    for link in links:
        result = result.replace(link['placeholder'], link['display_value'])
    return result


# Field labels that can appear in the OPQRST block. Used as stop-boundaries
# when parsing one field — ChiroTouch concatenates several labeled fields
# on a single line without \par between them, so a plain-text regex has to
# stop at the NEXT label, not just at the end of line.
_OPQRST_FIELD_LABELS = (
    'Location:', 'Onset:', 'Quality:', 'Radiation:', 'Radiating:',
    'Severity:', 'Timing:', 'Provoking Actions:', 'Palliative Actions:',
    'Change Since Last Visit:', 'Change:',
)
_OPQRST_BOUNDARY = (
    r'(?=\s*(?:'
    + '|'.join(re.escape(L) for L in _OPQRST_FIELD_LABELS)
    + r'|\n|$))'
)


def _get_opqrst_field(label, block):
    """Return 'LABEL: value' where value stops at the next known label,
    a newline, or end of block. Empty string if no match."""
    pattern = re.escape(label) + r'\s*(.+?)' + _OPQRST_BOUNDARY
    m = re.search(pattern, block, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ''


def extract_opqrst(plain_text):
    complaints = []
    # Split on the section headers only. The trailing (?!s) keeps us from
    # matching the plural "complaints" that appears in body prose like
    # "presents with additional complaints in the following location(s)".
    blocks = re.split(
        r'(?:Chief Complaint|Additional Complaint)(?!s)',
        plain_text,
        flags=re.IGNORECASE
    )
    complaint_blocks = [b.strip() for b in blocks[1:] if b.strip()]

    for i, block in enumerate(complaint_blocks):
        complaint = {'complaint_number': i + 1}

        complaint['location'] = _get_opqrst_field('Location:', block)

        # Onset type. The text reads e.g. "This complaint was caused by the
        # mechanism of injury" or "...exacerbated by..." or "...aggravated
        # by...". Search the whole block, not just the Onset field, because
        # ChiroTouch slots a date token between "Onset:" and "This".
        onset_match = re.search(
            r'This\s+(complaint\s+was\s+(?:caused|exacerbated|aggravated)\s+by)'
            r'\s+the\s+mechanism',
            block, re.IGNORECASE
        )
        if onset_match:
            complaint['onset_type'] = onset_match.group(1).lower()
        else:
            low = block.lower()
            if 'caused by' in low:
                complaint['onset_type'] = 'complaint was caused by'
            elif 'aggravated by' in low:
                complaint['onset_type'] = 'complaint was aggravated by'
            else:
                complaint['onset_type'] = 'complaint was exacerbated by'

        # Quality. Strip any "X describes the complaint as:" filler so the
        # list only contains adjectives.
        quality_raw = _get_opqrst_field('Quality:', block)
        quality_raw = re.sub(
            r'^.*?describes\s+the\s+complaint\s+as:\s*', '',
            quality_raw, flags=re.IGNORECASE
        )
        if quality_raw:
            quality_list = re.split(r'\s*(?:\band\b|\bor\b|,)\s*', quality_raw)
            complaint['quality'] = [q.strip() for q in quality_list if q.strip()]
        else:
            complaint['quality'] = ['aching']

        # Radiation. Two templates in the wild: "Radiation: The pain was
        # reported to: X" and "Radiating: the complaint radiates into: X".
        rad_raw = _get_opqrst_field('Radiation:', block) or _get_opqrst_field('Radiating:', block)
        rad_raw = re.sub(r'^\s*The pain was reported to:?\s*', '', rad_raw, flags=re.IGNORECASE)
        rad_raw = re.sub(r'^\s*the complaint radiates into:?\s*', '', rad_raw, flags=re.IGNORECASE)
        complaint['radiation'] = rad_raw.strip() if rad_raw.strip() else 'does not radiate'

        complaint['severity'] = _get_opqrst_field('Severity:', block)

        # Timing. Strip the prose leader so "constant (76-100%) of the time"
        # survives cleanly.
        timing_raw = _get_opqrst_field('Timing:', block)
        timing_raw = re.sub(
            r'^\s*Patient reports this complaint to be\s+', '',
            timing_raw, flags=re.IGNORECASE
        )
        timing_raw = re.sub(
            r'^\s*the complaint is present\s+', '',
            timing_raw, flags=re.IGNORECASE
        )
        complaint['timing'] = timing_raw.strip()

        complaint['change'] = (
            _get_opqrst_field('Change Since Last Visit:', block)
            or _get_opqrst_field('Change:', block)
        )

        complaints.append(complaint)

    return complaints


CARRY_FORWARD_MARKER = '**'
CF_TAG_START = 'CFSTART'
CF_TAG_END = 'CFEND'


def extract_tagged_carry_forward(plain_text):
    pattern = re.compile(
        re.escape(CF_TAG_START) + r'(.*?)' + re.escape(CF_TAG_END),
        re.DOTALL
    )
    return [m.group(1).strip() for m in pattern.finditer(plain_text)]


def strip_tagged_carry_forward(plain_text):
    pattern = re.compile(
        re.escape(CF_TAG_START) + r'.*?' + re.escape(CF_TAG_END),
        re.DOTALL
    )
    return pattern.sub('', plain_text).strip()


def extract_new_carry_forward(plain_text):
    lines = plain_text.split('\n')
    carry_forward = []
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(CARRY_FORWARD_MARKER):
            carry_forward.append(stripped[len(CARRY_FORWARD_MARKER):].strip())
        else:
            cleaned.append(line)
    return carry_forward, '\n'.join(cleaned)