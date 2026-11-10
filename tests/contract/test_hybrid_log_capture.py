"""Global candidate groups and complement scroll must share one exact snapshot."""
from copy import deepcopy

import pytest

from scripts.grouped_log_capture import _decode_hybrid_key
from tests.contract.test_compatible_log_capture import Transport, capture, scroll_response
from tests.contract.test_grouped_capture_validation import HOUR, bucket, hit, response


def selection(count=999999):
    # Deliberately differs from final snapshot; used only to select candidate keys.
    return {**response(count), 'aggregations': {'candidates': {'buckets': [
        {'key': 'synthetic timeout', 'doc_count': count}]}}}


def encoded_key(message='synthetic timeout'):
    hour = str(HOUR)
    return f'{len(message.encode("utf-16-le")) // 2}:{message}{len(hour)}:{hour}'


def hybrid(*, raw=2, grouped=8):
    b = bucket(grouped)
    b.update(key=encoded_key(), doc_count_error_upper_bound=0)
    return {**scroll_response(raw, [hit('raw-one')]), 'aggregations': {'all_source': {
        'doc_count': raw + grouped, 'eligible': {'doc_count': grouped, 'groups': {
            'buckets': [b], 'sum_other_doc_count': 0, 'doc_count_error_upper_bound': 0}}}}}


def test_hybrid_uses_same_snapshot_for_groups_and_exact_complement(tmp_path):
    second = {**scroll_response(2, [hit('raw-two')]), 'terminated_early': True}
    es = Transport([selection(), hybrid()], [second])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16)
    assert stats['capture_mode'] == 'hybrid_groups_scroll'
    assert stats['source_total'] == 10
    assert stats['grouped_occurrences'] == 8 and stats['raw_occurrences'] == 2
    assert stats['downloaded_documents'] == len(rows) == 3
    assert stats['scroll_terminated_early_pages'] == 1
    assert stats['selection_counts_used_for_evidence'] is False
    assert es.closed == ['owned-scroll'] and len(es.requests) == 3
    query = es.requests[1][2]
    candidate = query['aggs']['all_source']['aggs']['eligible']['filter']
    assert query['query'] == {'bool': {'must_not': [candidate]}}
    assert query['aggs']['all_source']['global'] == {}
    assert all('MASKED' in str(row) for row in rows)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure', [
    'unsafe', 'truncated', 'bucket_error', 'missing_bucket', 'overcount', 'population',
    'initial_early', 'initial_shards', 'raw_duplicate', 'raw_group_overlap', 'raw_short',
    'raw_changed_total', 'representative_mismatch', 'strata',
])
def test_hybrid_discards_entire_attempt_before_fresh_full_scroll(failure, tmp_path):
    first, next_page = hybrid(), scroll_response(2, [hit('raw-two')])
    whole = first['aggregations']['all_source']
    groups = whole['eligible']['groups']
    b = groups['buckets'][0]
    kwargs = {}
    if failure == 'unsafe':
        b['unsafe']['value'] = 1
    elif failure == 'truncated':
        groups['sum_other_doc_count'] = 9
    elif failure == 'bucket_error':
        b['doc_count_error_upper_bound'] = 1
    elif failure == 'missing_bucket':
        b['doc_count'] -= 1
    elif failure == 'overcount':
        b['doc_count'] += 1
    elif failure == 'population':
        whole['doc_count'] += 1
    elif failure == 'initial_early':
        first['terminated_early'] = True
    elif failure == 'initial_shards':
        first['_shards']['failed'] = 1
    elif failure == 'raw_duplicate':
        next_page['hits']['hits'] = [hit('raw-one')]
    elif failure == 'raw_group_overlap':
        next_page['hits']['hits'] = [hit('example')]
    elif failure == 'raw_short':
        next_page['hits']['hits'] = []
    elif failure == 'raw_changed_total':
        next_page['hits']['total']['value'] = 3
    elif failure == 'representative_mismatch':
        b['example']['hits']['hits'][0]['_source']['message'] = 'different'
    else:
        kwargs['strata_queries'] = {'all': {'match_all': {}}}
        whole['strata'] = {'buckets': {'all': {'doc_count': 9}}}
    fresh = scroll_response(1, [hit('fresh')], sid='fresh-scroll')
    if failure == 'strata':
        fresh['aggregations'] = {'strata': {'buckets': {'all': {'doc_count': 1}}}}
    es = Transport([selection(), first, fresh], [next_page])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16, **kwargs)
    assert stats['capture_mode'] == 'raw_scroll' and stats['source_total'] == 1
    assert [r['record']['id'] for r in rows] == ['fresh']
    assert es.closed == ['owned-scroll', 'fresh-scroll']
    assert not list(tmp_path.iterdir())


def test_hybrid_key_encoding_is_unambiguous_including_unicode_colon_and_null():
    value = 'synthetic : 🧪 -1:'
    assert _decode_hybrid_key(encoded_key(value) + '-1:', ['message', 'hour', 'level']) == {
        'message': value, 'hour': HOUR, 'level': None}


def test_zero_raw_complement_and_exact_strata(tmp_path):
    first = hybrid(raw=0)
    first['hits']['hits'] = []
    first['aggregations']['all_source']['strata'] = {'buckets': {'all': {'doc_count': 8}}}
    es = Transport([selection(), first])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16, strata_queries={'all': {'match_all': {}}})
    assert stats['es_counts'] == {'total': 8, 'all': 8}
    assert len(rows) == 1 and stats['raw_occurrences'] == 0
    assert es.closed == ['owned-scroll']


def test_no_repeated_selection_skips_hybrid_and_downloads_full_snapshot(tmp_path):
    es = Transport([selection(1), scroll_response(1, [hit('fresh')])])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16)
    assert stats['fallback_reason'] == 'hybrid_no_repeated_candidates'
    assert len(es.requests) == 2 and len(rows) == 1


def test_group_validation_failure_never_leaks_unmasked_or_partial_output(tmp_path):
    first = deepcopy(hybrid())
    first['aggregations']['all_source']['eligible']['groups']['buckets'][0]['last_seen']['value'] = HOUR - 1
    es = Transport([selection(), first, scroll_response(1, [hit('fresh')])])
    _, rows = capture(es, tmp_path, hybrid_candidates=16)
    assert [r['record']['id'] for r in rows] == ['fresh']


def test_unsafe_key_is_pruned_once_and_all_counts_come_from_new_snapshot(tmp_path):
    picked = selection()
    picked['aggregations']['candidates']['buckets'].append({'key': 'other timeout', 'doc_count': 99})
    first = hybrid()
    first['_scroll_id'] = 'discarded-scroll'
    good = bucket(4)
    good.update(key=encoded_key(), doc_count_error_upper_bound=0)
    bad = bucket(4, identity='bad', message='other timeout', unsafe=4)
    bad.update(key=encoded_key('other timeout'), doc_count_error_upper_bound=0)
    first['aggregations']['all_source']['eligible']['groups']['buckets'] = [good, bad]
    second = hybrid(grouped=10)
    second['_scroll_id'] = 'fresh-hybrid-scroll'
    es = Transport([picked, first, second], [scroll_response(2, [hit('raw-two')], sid='fresh-hybrid-scroll')])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16)
    assert stats['source_total'] == 12 and stats['grouped_occurrences'] == 10
    assert stats['discarded_candidate_snapshots'] == 1
    assert rows[0]['occurrence_count'] == 10
    assert es.closed == ['discarded-scroll', 'fresh-hybrid-scroll']
    selected = es.requests[2][2]['aggs']['all_source']['aggs']['eligible']['filter']
    assert selected['bool']['filter'][1] == {'terms': {'message.keyword': ['synthetic timeout']}}
    assert len([r for r in es.requests if r[3] == 'compatible_candidate_selection']) == 1


def test_raw_and_group_environment_use_index_projection_despite_unretained_source_value(tmp_path):
    from scripts.grouped_log_capture import capture_index_compatible
    from tests.contract.test_grouped_capture_validation import INFO, PROPS
    first = hybrid(raw=1)
    representative = first['aggregations']['all_source']['eligible']['groups']['buckets'][0]['example']['hits']['hits'][0]
    representative['_source']['fields'] = {'env': 'different-source-label'}
    first['hits']['hits'][0]['_source']['fields'] = {'env': 'another-source-label'}
    es, rows = Transport([selection(), first]), []
    capture_index_compatible(es, INFO, PROPS, rows.append,
        lambda h: {'id': h['_id'], 'env': INFO['env'], 'index': INFO['index'], 'message': 'MASKED'},
        work_dir=tmp_path, hybrid_candidates=16)
    assert rows[0]['env'] == rows[0]['examples'][0]['env'] == rows[1]['record']['env'] == INFO['env']
    assert rows[0]['index'] == rows[0]['examples'][0]['index'] == rows[1]['record']['index'] == INFO['index']
    # Even identical message/environment values cannot admit another source index.
    representative['_index'] = 'other-prod-query-gateway-2026.01.05'
    es = Transport([selection(), first, scroll_response(1, [hit('fresh')])])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16)
    assert stats['capture_mode'] == 'raw_scroll'
    assert [r['record']['id'] for r in rows] == ['fresh']


def test_preselection_skips_multiline_or_indented_keys_without_using_their_counts(tmp_path):
    picked = selection()
    picked['aggregations']['candidates']['buckets'] += [
        {'key': ' stack line', 'doc_count': 999}, {'key': 'multi\nline', 'doc_count': 999}]
    first = hybrid(raw=1)
    es = Transport([picked, first])
    stats, _ = capture(es, tmp_path, hybrid_candidates=16)
    assert stats['candidate_keys'] == 1
    assert stats['source_total'] == 9


def test_staging_disk_reserve_failure_closes_snapshot_without_publishing_or_redownloading(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import grouped_log_capture
    monkeypatch.setattr(grouped_log_capture.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100))
    es = Transport([selection(), hybrid()])
    with pytest.raises(OSError, match='disk reserve'):
        capture(es, tmp_path, hybrid_candidates=16, min_staging_free_bytes=101)
    assert es.closed == ['owned-scroll']
    assert len(es.requests) == 2
    assert not list(tmp_path.iterdir())


def test_candidate_pruning_retries_only_once_then_starts_fresh_full_scroll(tmp_path):
    picked = selection()
    picked['aggregations']['candidates']['buckets'].append({'key': 'other timeout', 'doc_count': 99})
    first = hybrid()
    first['_scroll_id'] = 'discarded-one'
    bad = bucket(4, identity='bad', message='other timeout', unsafe=4)
    bad.update(key=encoded_key('other timeout'), doc_count_error_upper_bound=0)
    good = bucket(4)
    good.update(key=encoded_key(), doc_count_error_upper_bound=0)
    first['aggregations']['all_source']['eligible']['groups']['buckets'] = [good, bad]
    second = hybrid()
    second['_scroll_id'] = 'discarded-two'
    second['aggregations']['all_source']['eligible']['groups']['buckets'][0]['unsafe']['value'] = 1
    es = Transport([picked, first, second, scroll_response(1, [hit('fresh')], sid='fresh-full')])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16)
    assert stats['capture_mode'] == 'raw_scroll'
    assert [r['record']['id'] for r in rows] == ['fresh']
    assert es.closed == ['discarded-one', 'discarded-two', 'fresh-full']
    assert len(es.requests) == 4


def test_masked_head_policy_excludes_continuation_key_from_final_candidate_query(tmp_path):
    picked = selection()
    picked['aggregations']['candidates']['buckets'].insert(0, {'key': '}', 'doc_count': 999999})
    es = Transport([picked, hybrid(raw=1)])
    checked = []
    def eligible(text):
        checked.append(text)
        return text != '}'
    stats, _ = capture(es, tmp_path, hybrid_candidates=16, candidate_is_head=eligible)
    assert checked == ['}', 'synthetic timeout']
    assert stats['source_total'] == 9 and stats['candidate_keys'] == 1
    assert es.requests[1][2]['aggs']['all_source']['aggs']['eligible']['filter']['bool']['filter'][1] == {
        'terms': {'message.keyword': ['synthetic timeout']}}


def test_net_request_gate_counts_selection_overhead_not_only_row_compression(tmp_path):
    first = hybrid(raw=0)
    first['hits']['hits'] = []
    es = Transport([selection(), first, scroll_response(1, [hit('fresh')])])
    stats, rows = capture(es, tmp_path, hybrid_candidates=16, require_request_reduction=True)
    assert stats['fallback_reason'] == 'hybrid_no_request_reduction'
    assert [r['record']['id'] for r in rows] == ['fresh']
    first = hybrid(raw=0, grouped=10000)
    first['hits']['hits'] = []
    es = Transport([selection(), first])
    stats, _ = capture(es, tmp_path, hybrid_candidates=16, require_request_reduction=True)
    assert stats['estimated_request_saving'] == 8


def test_traceback_context_falls_back_without_partial_groups_and_preserves_signature_counts(tmp_path):
    from datetime import datetime, timezone
    from scripts.grouped_log_capture import capture_index_compatible
    from su_multi_arm.core.config import PatternLibraryRef
    from su_multi_arm.core.config_sources import MultilineConfig
    from su_multi_arm.extraction.patterns.extractor import PatternExtractor
    from su_multi_arm.ingestion.logs_parse import parse_units
    from su_multi_arm.sources.logs import LogRecord
    from su_multi_arm.sources.multiline import BUILTIN_CONTINUATION, assemble
    from tests.contract.test_grouped_capture_validation import INFO, PROPS
    from tests.logs_support import ENGINE, write_library
    from tests.unit.test_logs_parse import LINES, artifacts
    head = LINES[0][1]
    texts = [head, 'Traceback (most recent call last):', 'ValueError: synthetic failure']
    hits = [hit(str(i), text) for i, text in enumerate(texts)]
    initial = hybrid(raw=3)
    initial['hits']['hits'] = [hits[0]]
    es = Transport([selection(), initial, scroll_response(3, hits, sid='fresh-full')],
                   [scroll_response(3, [hits[1]])])
    output = []
    stats = capture_index_compatible(es, INFO, PROPS, output.append,
        lambda h: {'id': h['_id'], 'message': h['_source']['message']}, work_dir=tmp_path,
        hybrid_candidates=16,
        raw_record_requires_context=lambda rec: bool(BUILTIN_CONTINUATION['traceback_header'].match(rec['message'])))
    assert stats['fallback_reason'] == 'hybrid_raw_requires_full_context'
    assert [r['record']['message'] for r in output] == texts
    assert all(r['kind'] == 'raw' for r in output)
    assert es.closed == ['owned-scroll', 'fresh-full']
    write_library(tmp_path / 'library', [{'id': 'msg.contextual_exception', 'kind': 'message',
        'status': 'approved', 'regex': '^ValueError: synthetic failure$',
        'signature': {'id': 'python.contextual_failure'},
        'examples': {'positive': ['ValueError: synthetic failure']}, 'provenance': {'author': 'user'}}])
    library = PatternExtractor(PatternLibraryRef(path=tmp_path / 'library', engine=ENGINE), None).load()
    records = [LogRecord(id=str(i), ts=datetime(2026, 1, 5, 10, 30, tzinfo=timezone.utc),
        partition_key='2026-01-05', attributes={'tenant': 't001', 'env': 'prod', 'service': 'query-gateway'},
        message=row['record']['message'], offset=i, source_id=str(i), index=INFO['index']) for i, row in enumerate(output)]
    heads, multiline = assemble(records, MultilineConfig(enabled=True,
        continuation=['traceback_header', 'python_traceback']), library=library)
    unit = artifacts(total=3)
    unit.records, unit.docs_pulled, unit.multiline = heads, 3, multiline.as_dict()
    unit.sample.strata[0].quota = unit.sample.strata[0].fetched = 3
    parsed = parse_units([unit], library)
    signature_counts = {row['signature_id']: row['lines'] for row in parsed.aggregates['signatures']}
    assert signature_counts == {'dependency.connect_failed': 1}
    assert multiline.joined == 2 and 'ValueError: synthetic failure' in heads[0].stack
    # Singleton interpretation would incorrectly turn the final traceback line into
    # an extra exception signature. The fallback preserves full-context semantics.
    independent = parse_units([unit.model_copy(update={'records': records, 'multiline': {}, 'docs_pulled': 3})], library)
    assert any(row['signature_id'] == 'python.contextual_failure' for row in independent.aggregates['signatures'])
