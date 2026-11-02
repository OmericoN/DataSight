import json

import pytest

from datasight.application.bounded_extraction import BoundedExtractionEngine, OccurrenceAnswers, SparseFeatures, CompactUnits, decode
from datasight.domain import extraction as c
from datasight.infrastructure.llm.client import ExtractionBudgetExceeded
from test_llm_extraction import MemoryRequests


class Client:
    def __init__(self): self.calls = []
    def complete(self, payload, model, record_attempt, validate):
        self.before_attempt()
        self.calls.append(SparseFeatures if issubclass(model, SparseFeatures) else OccurrenceAnswers if issubclass(model, OccurrenceAnswers) else model)
        data = json.loads(payload['messages'][1]['content'])
        if model is CompactUnits:
            result = CompactUnits(selections=[(i, 'Alpha', 'r')
                                        for i, line in enumerate(data['lines']) if 'Alpha' in line['text']])
        elif issubclass(model, SparseFeatures):
            result = SparseFeatures(observations=[])
        else:
            schema = model.model_json_schema()
            assert 'use.roles' in data['fields'].values() and 'identity.name' not in data['fields'].values()
            assert set(schema['$defs']['CompactOccurrenceFields']['properties']) == set(data['fields'])
            assert schema['properties']['occurrences']['minItems'] == len(data['occurrences'])
            result = model.model_validate({'occurrences': [
                {'occurrence_id': item['occurrence_id'], 'fields': {}} for item in data['occurrences']]})
        validate(result)
        record_attempt({'elapsed_seconds': .1, 'http_status': 200, 'response': {}, 'error': None})
        return result


def test_repeated_references_share_features_and_batch_occurrences():
    client = Client()
    repo = MemoryRequests()
    text = 'We used Alpha.\nWe compared Alpha.'
    result = BoundedExtractionEngine(repo, client, 1, text).run('W1', 'hash')
    assert len(result.datasets) == 1
    assert len(result.datasets[0].occurrences) == 2
    assert client.calls == [CompactUnits, SparseFeatures, OccurrenceAnswers]
    assert result.coverage['covered_characters'] == len(text)
    for evidence in result.evidence:
        assert text[evidence.char_start:evidence.char_end] == evidence.quote
    cached = Client()
    assert BoundedExtractionEngine(repo, cached, 1, text).run('W1', 'hash') == result
    assert cached.calls == []


def test_attempt_and_time_budgets_stop_before_next_http(monkeypatch):
    engine = BoundedExtractionEngine(MemoryRequests(), Client(), 1, 'text')
    engine.attempts = 50
    with pytest.raises(ExtractionBudgetExceeded, match='HTTP attempt'):
        engine.before_attempt()
    engine.attempts = 0
    monkeypatch.setattr('datasight.application.bounded_extraction.time.monotonic', lambda: engine.started+301)
    with pytest.raises(ExtractionBudgetExceeded, match='elapsed time'):
        engine.before_attempt()


def test_long_source_allows_one_hundred_attempts_only():
    engine = BoundedExtractionEngine(MemoryRequests(), Client(), 1, 'x' * 60001)
    for _ in range(100):
        engine.before_attempt()
    with pytest.raises(ExtractionBudgetExceeded, match='HTTP attempt'):
        engine.before_attempt()
    assert engine.attempts == 100


def test_sparse_rejects_unknown_fields_and_unsupported_literal():
    evidence = c.Evidence(evidence_id='e1', block_id='b', quote='Alpha', char_start=0, char_end=5,
                          section_heading=None, standardized_section='other', placement='body')
    for field, value in [('identity.invented', 'Alpha'), ('identity.name', 'Beta')]:
        from datasight.application.bounded_extraction import SparseField
        with pytest.raises(ValueError):
            decode([SparseField(field=field, state='reported', values_json=json.dumps([value]), evidence_ids=['e1'])],
                   {'identity': c.IdentityFeatures}, [evidence])


def test_larger_blocks_cover_source_without_gaps():
    from datasight.application.bounded_extraction import indexed_blocks
    text = ('## Section\n\nParagraph.\n\n' * 1500)
    blocks = list(indexed_blocks(text))
    assert blocks[0].start == 0 and blocks[-1].end == len(text)
    assert all(right.start <= left.end for left, right in zip(blocks, blocks[1:]))


def test_missing_occurrence_ids_trigger_repair_then_split():
    class Missing(Client):
        def complete(self, payload, model, record_attempt, validate):
            data = json.loads(payload['messages'][1]['content'])
            if issubclass(model, OccurrenceAnswers) and len(data['occurrences']) > 1:
                from datasight.infrastructure.llm.client import InvalidCompletion
                with pytest.raises(ValueError, match='occurrence ID'):
                    validate(OccurrenceAnswers(occurrences=[]))
                raise InvalidCompletion('Missing occurrence')
            return super().complete(payload, model, record_attempt, validate)
    result = BoundedExtractionEngine(MemoryRequests(), Missing(), 1, 'We used Alpha.\nWe compared Alpha.').run('W1','hash')
    assert len(result.datasets[0].occurrences) == 2


def test_field_split_keeps_omitted_fields_not_reported():
    class Small(Client):
        def complete(self, payload, model, record_attempt, validate):
            from datasight.infrastructure.llm.client import RequestTooLarge
            data = json.loads(payload['messages'][1]['content'])
            if len(data.get('fields', {})) > 2:
                raise RequestTooLarge('split fields')
            return super().complete(payload, model, record_attempt, validate)
    evidence = c.Evidence(evidence_id='e1', block_id='b', quote='Alpha', char_start=0, char_end=5,
                          section_heading=None, standardized_section='other', placement='body')
    engine = BoundedExtractionEngine(MemoryRequests(), Small(), 1, 'Alpha')
    result = engine.sparse('Alpha', [evidence], {'identity': c.IdentityFeatures, 'use': c.UseFeatures})
    assert result['identity'] == c.IdentityFeatures()
    assert result['use'] == c.UseFeatures()


def test_unsupported_optional_name_retains_reference_as_unnamed():
    class Unsupported(Client):
        def complete(self, payload, model, record_attempt, validate):
            if model is CompactUnits:
                result = CompactUnits(selections=[(0, 'Invented', 'r')])
                validate(result)
                return result
            return super().complete(payload, model, record_attempt, validate)
    text = 'We used Alpha.'
    result = BoundedExtractionEngine(MemoryRequests(), Unsupported(), 1, text).run('W1','hash')
    assert result.datasets[0].display_name == text
    assert result.datasets[0].review_reasons
    assert result.evidence[0].quote == text


def test_retry_wait_cannot_exceed_remaining_budget(monkeypatch):
    from types import SimpleNamespace
    from datasight import config
    from datasight.infrastructure.llm.client import GatewayClient
    monkeypatch.setattr(config, 'LLM_BASE_URL', 'https://example.test')
    monkeypatch.setattr(config, 'LLM_API_KEY', 'test-key')
    client = GatewayClient()
    client.before_attempt = lambda: 1
    client.remaining_time = lambda: 1
    monkeypatch.setattr(client.session, 'post', lambda *args, **kwargs: SimpleNamespace(status_code=429, headers={'Retry-After':'10'}, json=lambda: {}))
    attempts = []
    try:
        with pytest.raises(ExtractionBudgetExceeded, match='retry wait'):
            client.complete({}, SparseFeatures, attempts.append, lambda value: None)
        assert len(attempts) == 1
    finally:
        client.close()


def test_budget_limited_read_timeout_does_not_send_a_futile_retry(monkeypatch):
    import requests
    from datasight import config
    from datasight.infrastructure.llm.client import GatewayClient
    monkeypatch.setattr(config, 'LLM_BASE_URL', 'https://example.test')
    monkeypatch.setattr(config, 'LLM_API_KEY', 'test-key')
    client = GatewayClient()
    client.before_attempt = lambda: 10
    client.remaining_time = lambda: 1
    def timeout(*args, **kwargs):
        raise requests.ReadTimeout('read deadline')
    monkeypatch.setattr(client.session, 'post', timeout)
    monkeypatch.setattr('time.sleep', lambda seconds: pytest.fail('Deadline timeout must not retry'))
    attempts = []
    try:
        with pytest.raises(ExtractionBudgetExceeded, match='request read deadline'):
            client.complete({}, SparseFeatures, attempts.append, lambda value: None)
        assert len(attempts) == 1
    finally:
        client.close()


def test_only_invalid_fields_are_repaired_and_valid_facts_are_reused():
    class Partial(Client):
        def complete(self, payload, model, record_attempt, validate):
            from datasight.application.bounded_extraction import SparseField
            self.before_attempt()
            data = json.loads(payload['messages'][1]['content'])
            self.calls.append(set(data['fields']))
            schema = model.model_json_schema()
            field_schema = schema['$defs']['RequestedField']['properties']['f']
            assert field_schema.get('enum', [field_schema.get('const')]) == list(data['fields'])
            assert schema['properties']['observations']['maxItems'] == len(data['fields'])
            observations = [] if len(data['fields']) == 1 else [
                SparseField(field='identity.name', state='reported', values_json='["Alpha"]', evidence_ids=['e1']),
                SparseField(field='identity.aliases', state='reported', values_json='["Invented"]', evidence_ids=['e1'])]
            result = SparseFeatures(observations=observations)
            validate(result)
            return result
    evidence = c.Evidence(evidence_id='e1', block_id='b', quote='Alpha', char_start=0, char_end=5,
                          section_heading=None, standardized_section='other', placement='body')
    repo, client = MemoryRequests(), Partial()
    result = BoundedExtractionEngine(repo, client, 1, 'Alpha').sparse('Alpha', [evidence], {'identity': c.IdentityFeatures})
    assert result['identity'].name.values[0].value == 'Alpha'
    assert result['identity'].aliases.state == 'not_reported'
    assert client.calls[1] == {'identity.aliases'}
    cached = Partial()
    assert BoundedExtractionEngine(repo, cached, 1, 'Alpha').sparse('Alpha', [evidence], {'identity': c.IdentityFeatures}) == result
    assert not cached.calls


def test_truncation_splits_evidence_without_retrying_the_same_payload():
    from datasight.infrastructure.llm.client import OutputTruncated
    class Truncated(Client):
        def complete(self, payload, model, record_attempt, validate):
            data = json.loads(payload['messages'][1]['content'])
            if len(data['evidence']) > 1:
                self.calls.append('truncated')
                raise OutputTruncated('output limit')
            return super().complete(payload, model, record_attempt, validate)
    evidence = [c.Evidence(evidence_id=f'e{i}', block_id='b', quote='Alpha', char_start=0, char_end=5,
                          section_heading=None, standardized_section='other', placement='body') for i in range(2)]
    client = Truncated()
    BoundedExtractionEngine(MemoryRequests(), client, 1, 'Alpha').sparse('Alpha', evidence, {'identity': c.IdentityFeatures})
    assert client.calls == ['truncated', SparseFeatures, SparseFeatures]
    assert set(SparseFeatures.model_json_schema()['$defs']['SparseField']['properties']) == {'f', 's', 'v', 'e'}
    fields = SparseFeatures.model_json_schema()['$defs']['SparseField']['properties']['f']['enum']
    assert 'use.roles' in fields and 'usage.used' not in fields


def test_compact_occurrences_preserve_types_and_translate_local_evidence_indices():
    from datasight.application.bounded_extraction import compact_occurrence_fields, decode_compact
    models = {'use': c.UseFeatures, 'citation': c.CitationFeatures}
    fields, guide = compact_occurrence_fields(models, 1)
    evidence = c.Evidence(evidence_id='e1', block_id='b', quote='We used Alpha.', char_start=0, char_end=14,
                          section_heading=None, standardized_section='other', placement='body')
    result = decode_compact(fields.model_validate({'0': ['r', ['used'], [0]]}), models, [evidence])
    assert result['use'].roles.values[0].value == 'used'
    assert result['use'].roles.values[0].evidence_ids == ['e1']
    assert result['citation'] == c.CitationFeatures()
    for value in [['r', ['invented-role'], [0]], ['r', ['used'], [1]]]:
        with pytest.raises(ValueError):
            fields.model_validate({'0': value})
