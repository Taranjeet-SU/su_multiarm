"""Synthetic process/rate/scheduling checks; no private source or provider calls."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

location = os.environ.get('SU_PARALLEL_COLLECTOR_DIR')
if not location:
    pytest.skip('private parallel collector not supplied', allow_module_level=True)
HERE = Path(location).resolve()
sys.path.insert(0, str(HERE))
from collector_budget import CaptureStopped, SharedBudget  # noqa: E402


def _request_worker(budget, sender, requests, seconds):
    rows = []
    for _ in range(requests):
        started = budget.acquire()
        time.sleep(seconds)
        rows.append((started, time.monotonic()))
        budget.finish(seconds * 1000)
    sender.send(rows)
    sender.close()


def test_two_processes_share_rolling_rate_and_inflight_cap():
    ctx = multiprocessing.get_context('fork')
    budget = SharedBudget(ctx, initial_rps=4, max_rps=4)
    children, receivers = [], []
    for _ in range(2):
        recv, send = ctx.Pipe(False)
        process = ctx.Process(target=_request_worker, args=(budget, send, 5, 0.32))
        process.start()
        send.close()
        children.append(process)
        receivers.append(recv)
    events = [event for recv in receivers for event in recv.recv()]
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    starts = sorted(start for start, _ in events)
    assert all(sum(t <= other < t + 1 - 1e-6 for other in starts) <= 4 for t in starts)
    changes = sorted([(start, 1) for start, _ in events] + [(end, -1) for _, end in events])
    active = peak = 0
    for _, change in changes:
        active += change
        peak = max(peak, active)
    assert peak == 2 and active == 0
    assert budget.snapshot()['peak_active'] == 2
    assert budget.snapshot()['started'] == budget.snapshot()['completed'] == 10


def test_initial_two_rps_then_healthy_ramp_never_exceeds_four():
    ctx = multiprocessing.get_context('fork')
    budget = SharedBudget(ctx, initial_rps=2, ramp_seconds=0, min_samples=2)
    rates = []
    for _ in range(12):
        budget.acquire()
        budget.finish(10)
        rates.append(budget.snapshot()['rps'])
    assert rates[0] == 2
    assert rates[-1] == 4 and max(rates) == 4


def _backoff_worker(budget, signal, result):
    signal.recv()
    result.send(budget.snapshot())
    started = budget.acquire()
    budget.finish(10)
    result.send(started)


@pytest.mark.parametrize('failure', ['429', 'slow'])
def test_errors_and_latency_backoff_apply_to_other_process(failure):
    ctx = multiprocessing.get_context('fork')
    budget = SharedBudget(ctx, initial_rps=4, max_rps=4)
    budget.last_adjust.value = time.monotonic() - 3
    recv_signal, send_signal = ctx.Pipe(False)
    recv_result, send_result = ctx.Pipe(False)
    child = ctx.Process(target=_backoff_worker, args=(budget, recv_signal, send_result))
    child.start()
    budget.acquire()
    before = time.monotonic()
    update = budget.finish(4000 if failure == 'slow' else 100, failed=failure == '429')
    assert update['rps'] == 2
    send_signal.send(True)
    assert recv_result.recv()['rps'] == 2
    assert recv_result.recv() - before >= 1.95
    child.join(5)
    assert child.exitcode == 0


def test_stopping_blocks_new_work_but_owned_cleanup_remains_budgeted():
    ctx = multiprocessing.get_context('fork')
    budget = SharedBudget(ctx)
    budget.stop()
    with pytest.raises(CaptureStopped):
        budget.acquire()
    budget.acquire(cleanup=True)
    budget.finish(10)
    assert budget.snapshot()['completed'] == 1


@pytest.fixture
def parallel(monkeypatch, tmp_path):
    module = ModuleType('grouped_pull')
    module.ROOT = tmp_path
    module.stamp = lambda: 'synthetic-time'
    module.esro = SimpleNamespace(ReadOnlyES=None)
    monkeypatch.setitem(sys.modules, 'grouped_pull', module)
    spec = importlib.util.spec_from_file_location('synthetic_parallel', HERE / 'parallel_grouped_pull.py')
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


class FakeClient:
    def __init__(self, name, min_interval):
        self.received = sys.stdin.readline()
        self.interval = min_interval
        self.calls = []
    def request(self, method, path, body=None, *, note=''):
        self.calls.append((method, path))
        if path == '/fail':
            raise RuntimeError('synthetic 429')
        return {'_shards': {'total': 1, 'successful': 1, 'failed': 0}}
    def scroll(self):
        return self.request('POST', '/_search/scroll', note='scroll')
    def close(self):
        return self.request('DELETE', '/_search/scroll', note='clear_scroll')


def test_transport_routes_helpers_and_errors_through_shared_budget(parallel):
    ctx = multiprocessing.get_context('fork')
    budget = SharedBudget(ctx, initial_rps=4)
    logs = []
    secret = 'user = "synthetic-user:synthetic-secret"\n'
    old_stdin = sys.stdin
    client = parallel.paced_client(secret, budget, logs.append, name='synthetic', factory=FakeClient)
    assert sys.stdin is old_stdin and client.received == secret and client.interval == 0
    client.scroll()
    with pytest.raises(RuntimeError, match='429'):
        client.request('GET', '/fail')
    budget.stop()
    client.close()
    assert budget.snapshot()['started'] == budget.snapshot()['completed'] == 3
    assert budget.snapshot()['failures'] == 1
    assert 'synthetic-secret' not in json.dumps(logs)


def _jobs_worker(number, secret, cfg, options, jobs, budget, output, log_lock):
    while True:
        try:
            budget.ensure_running()
        except CaptureStopped:
            break
        claimed = jobs.claim()
        if claimed is None:
            break
        job_id, job = claimed
        output.send({'event': 'job_started', 'job': job_id, 'worker': number})
        done = Path(options['work']) / f'{job_id}.done'
        if options.get('crash') and job_id == 1:
            os._exit(7)
        if not done.exists():
            done.write_text(str(number))
        output.send({'event': 'job_done', 'job': job_id, 'worker': number})
        time.sleep(0.02)
    output.send({'event': 'worker_done', 'worker': number})
    output.close()


def test_unique_whole_service_ownership_and_crash_restart_skips_no_jobs(parallel, tmp_path):
    ctx = multiprocessing.get_context('fork')
    jobs = [{'service': f'synthetic-{i}', 'partitions': [('d1', []), ('d2', [])]} for i in range(6)]
    options = {'workers': 2, 'work': str(tmp_path), 'crash': True}
    events = []
    with pytest.raises(RuntimeError, match='incomplete'):
        parallel.run_workers(ctx, jobs, SharedBudget(ctx), 'SYNTHETIC', None, options,
                             events.append, worker_target=_jobs_worker, stop_grace=0.1)
    already = {p.name: p.read_text() for p in tmp_path.glob('*.done')}
    options['crash'] = False
    result = parallel.run_workers(ctx, jobs, SharedBudget(ctx), 'SYNTHETIC', None, options,
                                  events.append, worker_target=_jobs_worker, stop_grace=0.1)
    assert result['jobs_done'] == result['jobs_total'] == 6
    assert len(list(tmp_path.glob('*.done'))) == 6
    assert all((tmp_path / name).read_text() == value for name, value in already.items())


def test_count_hints_order_small_pending_services_without_filtering_scope(parallel, tmp_path):
    counts = tmp_path / 'data/counts'
    counts.mkdir(parents=True)
    (counts / '2026-01-01.jsonl').write_text('\n'.join(json.dumps({'index': name, 'total': n}) for name, n in [('large', 10000), ('small', 4)]))
    jobs = parallel.ordered_jobs({name: [('2026-01-01', [{'index': name}])] for name in ('large', 'small', 'unknown')})
    assert [j['service'] for j in jobs] == ['small', 'large', 'unknown']
    assert sum(len(partition[1]) for job in jobs for partition in job['partitions']) == 3


def test_budget_rejects_source_policy_excess():
    ctx = multiprocessing.get_context('fork')
    for kwargs in ({'max_rps': 5}, {'concurrency': 3}, {'initial_rps': 4, 'max_rps': 2},
                   {'p95_ms': float('nan')}, {'p95_ms': 3001}):
        with pytest.raises(ValueError):
            SharedBudget(ctx, **kwargs)


def _crash_locked_worker(number, secret, cfg, options, jobs, budget, output, log_lock):
    if number == 0:
        budget.condition.acquire()
        os._exit(9)
    time.sleep(20)


def test_hard_crash_while_holding_shared_controller_cannot_deadlock_parent(parallel, tmp_path):
    ctx = multiprocessing.get_context('fork')
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        parallel.run_workers(ctx, [{'service': 'synthetic'}], SharedBudget(ctx), 'SYNTHETIC', None,
            {'workers': 2}, lambda _: None, worker_target=_crash_locked_worker, stop_grace=0.1)
    assert time.monotonic() - started < 7


class FakePuller:
    def __init__(self, client, info, page, safe, log, **kwargs):
        self.client, self.info = client, info
    def run(self, targets, boundary):
        boundary.boundary()
        base = Path(self.info['work'])
        owner = base / (self.info['service'] + '.owner')
        if owner.exists():
            assert owner.read_text() == str(os.getpid())
        else:
            owner.write_text(str(os.getpid()))
        self.client.request('POST', '/synthetic/_search', {'size': 0}, note='synthetic_capture')
        (base / (self.info['index'] + '.done')).write_text('exact-synthetic-capture')


def test_real_worker_loop_has_disjoint_services_across_dates(parallel, tmp_path, monkeypatch):
    monkeypatch.setattr(parallel.collector.esro, 'ReadOnlyES', FakeClient)
    parallel.collector.safe_redactor = lambda _: SimpleNamespace(stamp='synthetic')
    parallel.collector.make_head_policy = lambda cfg, safe: (lambda text, info: True, 'synthetic')
    parallel.collector.GroupedPuller = FakePuller
    jobs = [{'service': name, 'partitions': [(day, [{'index': f'{name}-{day}', 'service': name,
            'date': day, 'work': str(tmp_path)}]) for day in ('day1', 'day2')]}
            for name in ('alpha', 'beta', 'gamma')]
    options = {'workers': 2, 'page': 100, 'raw_page': 1000, 'hybrid_candidates': 16,
               'snapshot_protocol': 'adaptive', 'reuse_raw_recipe': []}
    ctx = multiprocessing.get_context('fork')
    events = []
    result = parallel.run_workers(ctx, jobs, SharedBudget(ctx, initial_rps=4),
        'user = "synthetic:never-logged"\n', SimpleNamespace(privacy=None), options, events.append)
    assert result['jobs_done'] == 3
    assert len(list(tmp_path.glob('*.done'))) == 6
    assert len({p.read_text() for p in tmp_path.glob('*.owner')}) == 2
    assert 'never-logged' not in json.dumps(events)
