"""Durable suppression, separate from editable-copy overrides. No automatic cleanup."""
import json
from pathlib import Path


def decisions():
    return json.loads((Path(__file__).resolve().parents[1] / 'docs' / 'QA_CLEAN2_DECISIONS.json')
                      .read_text(encoding='utf-8'))['records']


def records(database=None):
    if database is None:
        from camellia.database import db
        database = db
    return database.list_response_template_retirements()


def customer_templates(templates, database=None):
    retired = {key for key, row in records(database).items() if row['retired']}
    return [template for template in templates if template.get('id') not in retired]


def retire(template_id, actor, database=None):
    """Only an individually audited RETIRE_SAFE decision permits a durable write.

    This release approves ZERO IDs. Dependency-bearing candidates fail closed.
    Future retirement requires a new reviewed decision, not an inactive override.
    """
    from camellia import client_qa
    from camellia.database import db
    database = database or db
    row = next((item for item in decisions() if item['legacy_id'] == template_id), None)
    if (not row or row['decision'] != 'RETIRE_SAFE' or not row['retirement_safe']
            or row['runtime_dependencies'] or row['source_mapping_references']
            or row['special_actions_assets']):
        raise ValueError('Template has not passed individual dependency-safe retirement review')
    if len(row['matching_approved_qa_ids']) != 1 or not actor:
        raise ValueError('Retirement requires one canonical replacement and an actor')
    replacement = row['matching_approved_qa_ids'][0]
    if not client_qa.effective_entry(replacement):
        raise ValueError('Canonical replacement is not active/effective')
    # Recheck current code and dynamic production route/topic records. An old
    # audit must never authorize retirement after a new dependency is added.
    from camellia.knowledge import CATALOGUE
    code_refs = any(template_id in source.read_text(encoding='utf-8')
                    for source in Path(__file__).parent.glob('*.py'))
    dynamic_refs = any(record.template_id == template_id for record in CATALOGUE)
    source_refs = template_id in json.dumps([
        database.list_instagram_source_routes(), database.list_instagram_topics(),
        database.list_ad_product_routes()])
    if code_refs or dynamic_refs or source_refs:
        raise ValueError('Current runtime/source dependencies block retirement')
    database._set_response_template_retirement(template_id, replacement, row['reason'], actor, True)


def restore(template_id, actor, database=None):
    from camellia.database import db
    database = database or db
    row = records(database).get(template_id)
    if not row or not actor:
        raise ValueError('Restore requires an existing retirement and an actor')
    database._set_response_template_retirement(template_id, row['replacement_qa_id'],
                                               'Explicitly restored', actor, False)
