"""Deterministic management validation and reversible Custom Q&A CSV format."""
import csv
import io
import json
import re

from camellia import approved_qa, client_qa
from camellia.candidate_safety import validate_candidate_answer, detect_business_commitment_warnings

COLUMNS = ['id', 'topic', 'primary_question', 'aliases', 'approved_answer',
           'priority', 'status', 'source', 'created_at', 'updated_at',
           'usage_count', 'last_used_at']
EDITABLE = ['topic', 'primary_question', 'aliases', 'approved_answer', 'priority', 'status']
AUDIT_COLUMNS = ['record_type', 'id', 'template_id', 'topic', 'display_name',
                 'primary_question', 'aliases', 'response', 'priority', 'status',
                 'source', 'protected_runtime', 'editable', 'importable',
                 'runtime_action_or_behavior', 'created_at', 'updated_at',
                 'usage_count', 'last_used_at']


def alias_texts(values):
    return [v['alias_text'] if isinstance(v, dict) else v for v in values or []]


def keys(question, aliases):
    return {client_qa._normalise_match_text(v) for v in [question, *aliases] if v.strip()}


def conflicts(database, templates, question, aliases, exclude_id=None):
    candidates = keys(question, aliases)
    found = []
    for item in database.list_custom_qa(status='ALL', limit=2147483647):
        if item['id'] != exclude_id and candidates & keys(item['question_pattern'], alias_texts(item['aliases'])):
            found.append({'id': item['id'], 'topic': item['topic'],
                          'primary_question': item['question_pattern'], 'source': 'CUSTOM'})
    for item in templates:
        if candidates & keys('', item.get('triggers') or []):
            found.append({'template_id': item['id'], 'primary_question': (item.get('triggers') or [''])[0],
                          'source': 'SYSTEM', 'protected_runtime': True})
    # Strict current effective registry: an unavailable authority must fail closed.
    for item in approved_qa.listing()['records']:
        if item['status'] == 'ACTIVE' and candidates & keys(item['question'],
                [*item.get('aliases', []), *item.get('source_aliases', [])]):
            found.append({'qa_id': item['qa_id'], 'primary_question': item['question'],
                          'source': 'APPROVED_QA'})
    return found


def safe_cell(value):
    """Quote formula prefixes for Excel; double initial apostrophes for lossless replay."""
    if isinstance(value, str) and (value.startswith("'") or value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r'))):
        return "'" + value
    return value


def csv_text(columns, rows):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: safe_cell(row.get(k, '')) for k in columns})
    return '\ufeff' + out.getvalue()


def parse_csv(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('CSV must have unique Custom Q&A headers')
    if not set(['topic', 'primary_question', 'approved_answer']).issubset(reader.fieldnames):
        raise ValueError('Missing required Custom Q&A headers')
    if set(reader.fieldnames) - set(COLUMNS):
        raise ValueError('Only Custom Q&A round-trip columns are accepted; full library audit is not importable')
    rows = []
    for number, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Malformed CSV row {number}')
        # Only undo the unambiguous escaping produced by safe_cell.
        row = {k: v[1:] if v.startswith("'") and safe_cell(v[1:]) == v else v for k, v in row.items()}
        rows.append({'row_number': number, **row})
    return rows


def editable(item):
    return {'topic': item['topic'], 'primary_question': item['question_pattern'],
            'aliases': alias_texts(item.get('aliases')), 'approved_answer': item['verified_answer'],
            'priority': item['priority'], 'status': item['status']}


def plan_import(database, templates, rows):
    results, seen_ids, seen_keys = [], set(), set()
    for row in rows:
        result = {'row_number': row['row_number'], 'operation': 'ERROR', 'target_id': None,
                  'reason': '', 'errors': [], 'conflicts': [], 'warnings': [], 'data': {}}
        try:
            aliases = json.loads(row.get('aliases') or '[]')
            if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                raise ValueError('aliases must be a JSON array of strings')
            data = {k: (row.get(k) or '').strip() for k in EDITABLE if k != 'aliases'}
            data.update(aliases=[a.strip() for a in aliases if a.strip()],
                        priority=(row.get('priority') or 'NORMAL').strip().upper(),
                        status=(row.get('status') or 'ACTIVE').strip().upper())
            result['data'] = data
            if not data['topic'] or not data['primary_question']:
                raise ValueError('topic and primary_question are required')
            valid, reason = validate_candidate_answer(data['approved_answer'])
            if not valid:
                raise ValueError(reason)
            if data['priority'] not in {'NORMAL', 'HIGH'} or data['status'] not in {'ACTIVE', 'DISABLED', 'ARCHIVED'}:
                raise ValueError('Invalid priority or status')
            supplied_id = row.get('id', '').strip()
            current = None
            if supplied_id:
                if not re.fullmatch(r'[1-9][0-9]*', supplied_id):
                    raise ValueError('id must be an existing numeric Custom Q&A ID, or blank for NEW')
                target = int(supplied_id)
                current = database.get_custom_qa(target)
                if not current:
                    raise ValueError('Custom Q&A ID does not exist')
                result['target_id'] = target
                if target in seen_ids:
                    raise ValueError('Repeated target ID in this file')
                seen_ids.add(target)
            matched = conflicts(database, templates, data['primary_question'], data['aliases'], result['target_id'])
            row_keys = keys(data['primary_question'], data['aliases'])
            if row_keys & seen_keys:
                matched.append({'source': 'CSV', 'primary_question': data['primary_question']})
            seen_keys.update(row_keys)
            if not supplied_id and len(matched) == 1 and matched[0]['source'] == 'CUSTOM':
                same = database.get_custom_qa(matched[0]['id'])
                if editable(same) == {**data, 'primary_question': data['primary_question'].lower()}:
                    result.update(operation='UNCHANGED', target_id=same['id'],
                                  reason='Identical existing row skipped; blank ID never updates a record')
                    result['row_status'] = result['operation']
                    results.append(result)
                    continue
            if matched:
                result.update(operation='CONFLICT', conflicts=matched, reason='Question/alias conflicts with protected or existing knowledge')
            else:
                # The existing store lowercases primary questions. Compare its exact representation.
                compare = {**data, 'primary_question': data['primary_question'].lower()}
                result.update(operation=('UNCHANGED' if current and editable(current) == compare else 'UPDATE' if current else 'NEW'),
                              reason='Server-maintained metadata columns are ignored; provenance and usage are preserved')
                if result['operation'] in {'NEW', 'UPDATE'}:
                    result['warnings'] = detect_business_commitment_warnings(data['approved_answer'])
                    if result['warnings']:
                        result['reason'] += '; review business commitments: ' + ', '.join(result['warnings'])
        except (ValueError, TypeError, KeyError) as exc:
            result['reason'] = str(exc)
            result['errors'] = [str(exc)]
        result['row_status'] = result['operation']
        results.append(result)
    counts = {name.lower(): sum(r['operation'] == name for r in results)
              for name in ('NEW', 'UPDATE', 'UNCHANGED', 'CONFLICT', 'ERROR')}
    return {'status': 'success', 'summary': {'rows': len(results), **counts}, 'rows': results,
            'warnings': sorted({w for row in results for w in row['warnings']}),
            'can_confirm': not counts['conflict'] and not counts['error']}
