"""One-response grouping needs no multi-request snapshot API; raw fallback is atomic."""
from copy import deepcopy

import pytest

from scripts.grouped_log_capture import CaptureError, capture_index_compatible
from tests.contract.test_grouped_capture_validation import INFO, PROPS, bucket, hit, response


class Transport:
    def __init__(self, replies, scrolls=()):
        self.replies, self.scrolls = deepcopy(replies), deepcopy(scrolls)
        self.requests, self.closed = [], []

    def request(self, method, path, body, *, note):
        self.requests.append((method, path, body, note))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def scroll(self, sid, keepalive):
        self.requests.append(('SCROLL', sid, keepalive, 'scroll'))
        return self.scrolls.pop(0)

    def clear_scroll(self, sid):
        self.closed.append(sid)


def capture(transport, tmp_path, *, props=PROPS, **kwargs):
    output = []
    result = capture_index_compatible(transport, INFO, props, output.append,
        lambda h: {'id': h['_id'], 'ts': h['_source']['@timestamp'], 'message': '[MASKED]'},
        page_size=2, work_dir=tmp_path, **kwargs)
    return result, output


def scroll_response(total, hits, sid='owned-scroll'):
    return {**response(total, hits=hits), '_scroll_id': sid}


def test_complete_single_response_groups_and_raw_examples_need_no_pit_or_more_searches(tmp_path):
    missing = bucket(1, identity='long', message=None, unsafe=1)
    missing['example']['hits']['hits'] = [hit('long', 'long synthetic message')]
    # after_key can be present on a final composite page; exact complete accounting proves coverage.
    transport = Transport([response(4, buckets=[bucket(), missing], after=missing['key'])])
    result, output = capture(transport, tmp_path)
    assert result['capture_mode'] == 'single_response_groups'
    assert result['source_total'] == result['grouped_occurrences'] + result['raw_occurrences'] == 4
    assert result['snapshot']['protocol'] == 'single_search'
    assert len(transport.requests) == 1 and not transport.closed
    assert [row['kind'] for row in output] == ['group', 'raw']
    assert output[0]['examples'][0]['message'] == output[1]['record']['message'] == '[MASKED]'
    assert not list(tmp_path.iterdir())


def test_incomplete_aggregation_is_discarded_before_fresh_scroll_even_if_source_total_changes(tmp_path):
    # Later scroll captures a different population. None of the earlier grouped output survives.
    transport = Transport([response(100, buckets=[bucket(10)]), scroll_response(2, [hit('new-1')])],
                          [scroll_response(2, [hit('new-2')], sid='owned-next')])
    result, output = capture(transport, tmp_path)
    assert result['capture_mode'] == 'raw_scroll' and result['source_total'] == 2
    assert result['fallback_reason'] == 'aggregation_requires_pagination'
    assert [row['record']['id'] for row in output] == ['new-1', 'new-2']
    assert all(row['kind'] == 'raw' for row in output)
    assert transport.closed == ['owned-next']
    assert not list(tmp_path.iterdir())


def test_unsafe_bucket_without_complete_raw_hits_uses_full_scroll(tmp_path):
    transport = Transport([response(3, buckets=[bucket(3, unsafe=3)]),
                           scroll_response(3, [hit(str(i)) for i in range(3)])])
    result, output = capture(transport, tmp_path)
    assert result['fallback_reason'] == 'unsafe_bucket_requires_pagination'
    assert len(output) == 3 and result['groups'] == 0


def test_duplicate_raw_memberships_are_deduplicated_within_one_response(tmp_path):
    left, right = bucket(1, message='a', unsafe=1), bucket(1, message='b', unsafe=1)
    transport = Transport([response(1, buckets=[left, right])])
    result, output = capture(transport, tmp_path, min_first_page_reduction=1)
    assert result['raw_duplicate_hits'] == 1 and len(output) == 1


def test_poor_compression_gate_uses_lossless_scroll(tmp_path):
    transport = Transport([response(1, buckets=[bucket(1)]), scroll_response(1, [hit()])])
    result, output = capture(transport, tmp_path)
    assert result['compression_gate_raw'] is True
    assert result['groups'] == 0 and len(output) == 1


def test_unsupported_aggregation_is_explicit_and_raw_scroll_can_still_work(tmp_path):
    transport = Transport([RuntimeError('synthetic unsupported aggregation'), scroll_response(1, [hit()])])
    result, _ = capture(transport, tmp_path)
    assert result['fallback_reason'] == 'aggregation_unavailable:RuntimeError'


def test_source_permission_failure_is_not_hidden_by_fallback(tmp_path):
    transport = Transport([PermissionError('synthetic denied')])
    with pytest.raises(PermissionError):
        capture(transport, tmp_path)
    assert len(transport.requests) == 1


def test_older_doc_sorted_scroll_page_early_flag_requires_complete_unique_reconciliation(tmp_path):
    next_page = scroll_response(2, [hit('two')])
    next_page['terminated_early'] = True
    transport = Transport([scroll_response(2, [hit('one')])], [next_page])
    result, output = capture(transport, tmp_path, props={})
    assert result['scroll_terminated_early_pages'] == 1
    assert result['source_total'] == len(output) == 2
    assert result['snapshot_consistent'] is True
    incomplete = {**next_page, 'hits': {'total': {'value': 2, 'relation': 'eq'}, 'hits': []}}
    transport = Transport([scroll_response(2, [hit('one')])], [incomplete])
    with pytest.raises(CaptureError, match='ended before'):
        capture(transport, tmp_path, props={})


def test_initial_scroll_early_termination_is_never_accepted(tmp_path):
    initial = {**scroll_response(1, [hit()]), 'terminated_early': True}
    transport = Transport([initial])
    with pytest.raises(CaptureError, match='incomplete'):
        capture(transport, tmp_path, props={})
    assert transport.closed == ['owned-scroll']


@pytest.mark.parametrize('failure', ['short', 'duplicate', 'partial', 'changed_total'])
def test_raw_fallback_rejects_incomplete_or_inconsistent_scroll_and_closes(failure, tmp_path):
    initial = scroll_response(2, [hit('one')])
    second = scroll_response(2, [hit('two')])
    if failure == 'short':
        second['hits']['hits'] = []
    elif failure == 'duplicate':
        second['hits']['hits'] = [hit('one')]
    elif failure == 'partial':
        second['_shards']['failed'] = 1
    else:
        second['hits']['total']['value'] = 3
    transport = Transport([initial], [second])
    with pytest.raises(CaptureError):
        capture(transport, tmp_path, props={})
    assert transport.closed == ['owned-scroll']
    assert not list(tmp_path.iterdir())


def test_single_response_groups_also_respect_configured_head_policy(tmp_path):
    transport = Transport([response(3, buckets=[bucket(3)]), scroll_response(1, [hit('fresh')])])
    result, rows = capture(transport, tmp_path, candidate_is_head=lambda _: False)
    assert result['fallback_reason'] == 'group_is_not_standalone_head'
    assert [r['record']['id'] for r in rows] == ['fresh']
