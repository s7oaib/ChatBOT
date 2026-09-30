"""QA-S1: flexible wording must select unchanged approved copy and assets."""
import pytest

from camellia import client_qa

PARAPHRASES = [(qa_id, phrase) for qa_id, aliases in client_qa._PARAPHRASE_ALIASES.items() for phrase in aliases]
PARSED = {entry.qa_id: entry for entry in client_qa.parse_source()}


def assert_approved(message, expected_id, facts=None):
    matched = client_qa.match(message, facts)
    assert matched is not None, message
    assert matched.entry is client_qa.REGISTRY_BY_ID[expected_id], message
    assert matched.entry == PARSED[expected_id]
    assert matched.entry.answer == PARSED[expected_id].answer
    assert matched.asset_keys == PARSED[expected_id].asset_keys
    assert matched.entry.blocks == PARSED[expected_id].blocks
    return matched


@pytest.mark.parametrize('entry', client_qa.REGISTRY, ids=lambda entry: entry.qa_id)
def test_all_23_canonical_entries_keep_exact_copy_and_assets(entry):
    facts = {'home_table_context': True} if entry.context == 'home_table' else {}
    if entry.qa_id == 'order_prebook':
        facts['order_flow_context'] = True
    assert_approved(entry.question, entry.qa_id, facts)


@pytest.mark.parametrize('qa_id,message', PARAPHRASES)
def test_aliases_select_source_entry_without_rewriting_or_changing_assets(qa_id, message):
    assert_approved(message, qa_id)


def test_alias_coverage_is_bounded_and_contains_no_answers():
    assert set(client_qa._PARAPHRASE_ALIASES) == set(client_qa.REGISTRY_BY_ID)
    assert len(PARAPHRASES) == 92
    assert all(4 <= len(aliases) <= 8 for aliases in client_qa._PARAPHRASE_ALIASES.values())


@pytest.mark.parametrize('message,expected_id', [
    ('what is pirce of BENGALI BENTO', 'bento_price_client'),
    ('how much is the Bengali bento?', 'bento_price_client'),
    ('bento price please', 'bento_price_client'),
    ('rate for bento', 'bento_price_client'),
    ('how much for veg bento?', 'bento_price_client'),
    ('bento prce pls', 'bento_price_client'),
    ('BENTO PRISE PLZ!!!', 'bento_price_client'),
    ('hw mch for a meal box?', 'bento_price_client'),
    ('Whats the price of sondesh?', 'sondesh_price_client'),
    ('sandesh price', 'sondesh_price_client'),
    ('how much for sondesh', 'sondesh_price_client'),
    ('mishti price please', 'sondesh_price_client'),
    ('What’s the prce of sandesh?', 'sondesh_price_client'),
    ('How much time do you take to deliver sondesh', 'sondesh_delivery_time'),
    ('how long for sandesh delivery?', 'sondesh_delivery_time'),
    ('when can sondesh reach?', 'sondesh_delivery_time'),
    ('delivery time for mishti', 'sondesh_delivery_time'),
    ('how long for sondesh?', 'sondesh_delivery_time'),
    ('WHATS THE PRICE OF CHICKEN KOSHA', 'chicken_kosha_price_client'),
    ('how much is chicken kosha', 'chicken_kosha_price_client'),
    ('chicken kosha rate', 'chicken_kosha_price_client'),
    ('why do I need 9 pieces?', 'minimum_sondesh_quantity_client'),
    ('why minimum 250g?', 'minimum_sondesh_quantity_client'),
    ('why multiples of nine for sondesh?', 'minimum_sondesh_quantity_client'),
    ('subscription price', 'subscription_price'),
    ('how much is monthly meal subscription?', 'subscription_price'),
    ('tell me about meal plan', 'subscription_details_client'),
    ('home food away from home price', 'subscription_price'),
    ('how do I prebook?', 'order_prebook'),
    ('how can I order?', 'order_prebook'),
    ('what details do you need to place my order?', 'order_prebook'),
    ('delivery to Mumbai?', 'delivery_out_of_area_client'),
    ('can you send to chennai?', 'delivery_out_of_area_client'),
    ('how can I pay by card?', 'general_payment_client'),
    ('how can i pay by upi?', 'general_payment_client'),
    ('payment options please', 'general_payment_client'),
    ('HOME   TABLE   PRCE!!!', 'home_table_price'),
    ('tell me about sandesh', 'sondesh_details_client'),
])
def test_intent_subject_typos_and_shorthand(message, expected_id):
    assert_approved(message, expected_id)


@pytest.mark.parametrize('message,facts,expected_id', [
    ('how much?', {'last_topic': 'bento'}, 'bento_price_client'),
    ('how much?', {'last_topic': 'bengali_home_table'}, 'home_table_price'),
    ('how long will delivery take?', {'last_topic': 'sondesh'}, 'sondesh_delivery_time'),
    ('how do I pay?', {'last_topic': 'bengali_home_table'}, 'home_table_payment'),
    ('how much is Bengali Bento?', {'home_table_context': True}, 'bento_price_client'),
    ('sandesh prce?', {'home_table_context': True}, 'sondesh_price_client'),
    ('meal box price?', {'home_table_context': True}, 'bento_price_client'),
    ('how much?', {'last_topic': 'subscription'}, 'subscription_price'),
    ('tell me more', {'last_topic': 'bento'}, 'bento_details_client'),
    ('tell me more', {'last_topic': 'sondesh'}, 'sondesh_details_client'),
    ('price pls', {'last_topic': 'bento'}, 'bento_price_client'),
    ('how much?', {'last_topic': 'bento', 'home_table_context': True, 'source_context_superseded': True}, 'bento_price_client'),
])
def test_followups_use_existing_topic_facts_but_explicit_subject_wins(message, facts, expected_id):
    assert_approved(message, expected_id, facts)


@pytest.mark.parametrize('message', [
    'how much is chicken?', 'how much?', 'tell me more', 'do you have fish?',
    'what is paneer price?', 'what is the weather?', 'how much is mutton?',
    'how much is chicken curry?', 'how much is fish?', 'tell me more about paneer',
    'bento and sondesh price?', 'bento and chicken kosha price?',
    'how much for vegan bento?', 'can i replace the chicken in bento?',
    'how much is the weather?', 'when was sondesh invented?',
    'when was sondesh first created?', 'when is sondesh available?', 'when did sondesh become popular?', 'bento with paneer price?',
    'fish bento price?', 'how much for mutton bento?',
])
def test_unknown_or_ambiguous_questions_do_not_choose_approved_business_facts(message):
    assert client_qa.match(message) is None


@pytest.mark.parametrize('facts', [{}, {'home_table_context': True}, {'last_topic': 'bento'}])
def test_unsupported_product_price_cannot_inherit_context(facts):
    assert client_qa.match('what is paneer price?', facts) is None
    assert client_qa.match('how much is chicken?', facts) is None


@pytest.mark.parametrize('first,followup,expected_id', [
    ('tell me about Bengali Bento', 'how much?', 'bento_price_client'),
    ('tell me about the Bengali Home Table', 'how much?', 'home_table_price'),
    ('tell me about Sondesh', 'how long will delivery take?', 'sondesh_delivery_time'),
    ('tell me about the Home Table', 'how do I pay?', 'home_table_payment'),
])
def test_actual_conversations_resolve_followups_without_llm(monkeypatch, first, followup, expected_id, eligible_weekly_asset):
    from unittest.mock import patch
    from camellia.config import config
    from camellia.responder import responder
    from camellia.session_memory import session_memory

    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    user_id = 'qa-s1-context-' + expected_id
    session_memory.clear_session(user_id, 'instagram')
    with patch('camellia.ai_responder.ai_responder._query_groq', side_effect=AssertionError('LLM called')):
        responder.match_and_respond(first, user_id=user_id, channel='instagram')
        response, matched, _, _ = responder.match_and_respond(followup, user_id=user_id, channel='instagram')
    facts = session_memory.get_facts(user_id, 'instagram')
    assert matched
    assert facts['client_qa_id'] == expected_id
    assert response


@pytest.mark.parametrize('qa_id,message', [
    ('bento_price_client', 'hw mch for a meal box?'),
    ('sondesh_price_client', 'mishti price please'),
    ('subscription_price', 'home food away from home price'),
    ('chicken_kosha_price_client', 'chicken kosha rate'),
    ('minimum_sondesh_quantity_client', 'why minimum 9 pieces?'),
])
def test_responder_paraphrases_preserve_canonical_assets_and_copy(monkeypatch, qa_id, message, eligible_weekly_asset):
    from unittest.mock import patch
    from camellia.config import config
    from camellia.responder import responder
    from camellia.session_memory import session_memory

    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    outputs = []
    for index, wording in enumerate([client_qa.REGISTRY_BY_ID[qa_id].question, message]):
        user_id = f'qa-s1-assets-{qa_id}-{index}'
        session_memory.clear_session(user_id, 'instagram')
        with patch('camellia.ai_responder.ai_responder._query_groq', side_effect=AssertionError('LLM called')):
            response, matched, _, images = responder.match_and_respond(wording, user_id=user_id, channel='instagram')
        assert matched
        facts = session_memory.get_facts(user_id, 'instagram')
        assert facts['client_qa_id'] == qa_id
        outputs.append((response, images, facts.get('ordered_response_blocks')))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('message', ['how much is chicken?', 'what is paneer price?', 'what is the weather?'])
def test_responder_ambiguous_or_unknown_questions_stay_on_safe_path(monkeypatch, message):
    from unittest.mock import patch
    from camellia.config import config
    from camellia.responder import responder
    from camellia.session_memory import session_memory

    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    user_id = 'qa-s1-negative-' + message
    session_memory.clear_session(user_id, 'instagram')
    with patch('camellia.ai_responder.ai_responder._query_groq', side_effect=AssertionError('LLM called')):
        response, matched, template_id, _ = responder.match_and_respond(message, user_id=user_id, channel='instagram')
    assert not matched
    assert template_id != 'chicken_kosha_price'
    assert response
    assert session_memory.get_facts(user_id, 'instagram').get('client_qa_id') is None
