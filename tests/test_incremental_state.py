"""A fast tick must never advance past data it failed to collect."""
import json
from pathlib import Path
import pytest
from codex_usage_tracker.incremental import collect_incrementally


def setup_paths(tmp_path):
    source=tmp_path/'usage_events.db'
    source.write_bytes(b'fixture')
    projects=tmp_path/'claude'
    projects.mkdir()
    return dict(scope='usage',state_db=source,claude_root=projects,
                opencode_dbs=[],usage_ledger=tmp_path/'ledger.sqlite3',dry_run=False)


def healthy(**kwargs):
    Path(kwargs['usage_ledger']).touch()
    return {'scope':'usage','usage':{},'counts':{'conflicts':0},'lanes':{
        'hermes':{'succeeded':True,'ceded':False,'quarantined_count':0,'skipped_files':0}}}


def test_bootstrap_overlap_and_full_sweep(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path); calls=[]
    def collect(**kw):
        calls.append(kw['usage_since']); return healthy(**kw)
    collect_incrementally(collect, state, now=10000, **kwargs)
    collect_incrementally(collect, state, now=10900, **kwargs)
    collect_incrementally(collect, state, now=11800, full_sweep=True, **kwargs)
    assert calls==[None,9400,None]
    record=json.loads(state.read_text())
    assert record['last_success_started_at']==11800
    assert record['last_full_success_started_at']==11800


@pytest.mark.parametrize('bad', [
    {'succeeded':False}, {'ceded':True}, {'quarantined_count':1}, {'skipped_files':1},
])
def test_degraded_lane_does_not_move_checkpoint(tmp_path,bad):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path)
    collect_incrementally(healthy,state,now=10000,**kwargs)
    previous=state.read_bytes()
    def collect(**kw):
        result=healthy(**kw); result['lanes']['hermes'].update(bad); return result
    result=collect_incrementally(collect,state,now=10900,**kwargs)
    assert not result['incremental']['checkpoint_advanced']
    assert state.read_bytes()==previous


def test_exception_and_dry_run_leave_state_alone(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path)
    collect_incrementally(healthy,state,now=10000,**kwargs); previous=state.read_bytes()
    def fail(**kw): raise RuntimeError('failed')
    with pytest.raises(RuntimeError): collect_incrementally(fail,state,now=10900,**kwargs)
    collect_incrementally(healthy,state,now=10900,**dict(kwargs,dry_run=True))
    assert state.read_bytes()==previous


@pytest.mark.parametrize('invalid',['{bad','{}','{"last_success_started_at":99999999}','[]'])
def test_bad_state_bootstraps_full(tmp_path,invalid):
    state=tmp_path/'cursor.json'; state.write_text(invalid); kwargs=setup_paths(tmp_path)
    seen=[]
    def collect(**kw): seen.append(kw['usage_since']); return healthy(**kw)
    collect_incrementally(collect,state,now=10000,**kwargs)
    assert seen==[None]


def test_changed_source_and_destination_reset_cutoff(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path); seen=[]
    def collect(**kw): seen.append(kw['usage_since']); return healthy(**kw)
    collect_incrementally(collect,state,now=10000,**kwargs)
    source=kwargs['state_db']; source.rename(tmp_path/'old-source'); source.touch()
    collect_incrementally(collect,state,now=10900,**kwargs)
    ledger=kwargs['usage_ledger']; ledger.rename(tmp_path/'old-ledger'); ledger.touch()
    collect_incrementally(collect,state,now=11800,**kwargs)
    assert seen==[None,None,None]


def test_missing_source_does_not_advance(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path)
    kwargs['state_db'].unlink()
    result=collect_incrementally(healthy,state,now=10000,**kwargs)
    assert not state.exists()
    assert not result['incremental']['checkpoint_advanced']


def test_no_lanes_or_conflicts_do_not_advance(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path)
    collect_incrementally(lambda **kw: {'lanes':{},'counts':{}},state,now=10000,**kwargs)
    assert not state.exists()


def test_real_cli_threads_checkpoint(tmp_path,monkeypatch,capsys):
    import sqlite3
    from codex_usage_tracker.cli import main
    from test_fast_incremental_collection import _sidecar
    state=tmp_path/'cursor.json'; sidecar=tmp_path/'usage_events.db'
    _sidecar(sidecar, [])
    projects=tmp_path/'projects'; projects.mkdir()
    opencode=tmp_path/'opencode.db'
    with sqlite3.connect(opencode) as c: c.execute('CREATE TABLE session(id TEXT)')
    argv=['tracker','collect-all','--scope','usage','--no-live-quota',
          '--state-db',str(sidecar),'--claude-root',str(projects),
          '--opencode-db',str(opencode),'--openclaw-home','',
          '--usage-ledger',str(tmp_path/'ledger.sqlite3'),
          '--incremental-state',str(state)]
    monkeypatch.setattr('sys.argv',argv)
    assert main()==0
    first=json.loads(capsys.readouterr().out)
    assert first['incremental']['checkpoint_advanced'], first
    assert first['incremental']['mode']=='full'
    assert main()==0
    second=json.loads(capsys.readouterr().out)
    assert second['incremental']['mode']=='incremental'
    assert second['incremental']['checkpoint_advanced']
    import os
    broken=projects/'old-broken.jsonl'
    broken.write_text('{}\n{broken\n{}\n')
    os.utime(broken,(100,100))
    monkeypatch.setattr('sys.argv',argv+['--full-sweep'])
    assert main()==0
    full=json.loads(capsys.readouterr().out)
    assert full['incremental']['mode']=='full'
    assert full['incremental']['checkpoint_advanced']
    assert full['incremental']['pending_transcripts']==1
    assert json.loads(state.read_text())['retry_files']==[str(broken)]
    monkeypatch.setattr('sys.argv',argv)
    assert main()==0
    retry=json.loads(capsys.readouterr().out)
    assert retry['incremental']['mode']=='incremental'
    assert retry['lanes']['claude_code']['skipped_files']==1
    assert retry['incremental']['pending_transcripts']==1
    # Repair while preserving old mtime: the pending list still forces a read.
    broken.write_text('{}\n'); os.utime(broken,(100,100))
    assert main()==0
    assert json.loads(capsys.readouterr().out)['incremental']['pending_transcripts']==0


def test_captured_malformed_files_retry_without_pinning_full_scan(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path)
    bad=str(tmp_path/'claude'/'old.jsonl'); seen=[]
    def collect(**kw):
        seen.append((kw['usage_since'],kw.get('claude_retry_files')))
        result=healthy(**kw)
        if len(seen)==1:
            result['lanes']['hermes']['skipped_files']=1
            kw['retry_files_out'].append(bad)
        return result
    one=collect_incrementally(collect,state,now=10000,**kwargs)
    assert one['incremental']['checkpoint_advanced']
    assert json.loads(state.read_text())['retry_files']==[bad]
    collect_incrementally(collect,state,now=10900,**kwargs)
    assert seen[1]==(9400,[bad])
    assert json.loads(state.read_text())['retry_files']==[]


def test_dry_run_does_not_create_checkpoint_directory(tmp_path):
    kwargs=setup_paths(tmp_path); directory=tmp_path/'state'
    collect_incrementally(healthy,directory/'cursor.json',now=10000,**dict(kwargs,dry_run=True))
    assert not directory.exists()


def test_ledger_replaced_during_run_does_not_advance(tmp_path):
    kwargs=setup_paths(tmp_path); state=tmp_path/'cursor.json'
    collect_incrementally(healthy,state,now=10000,**kwargs)
    before=state.read_bytes()
    def collect(**kw):
        ledger=Path(kw['usage_ledger']); ledger.rename(tmp_path/'old-ledger')
        return healthy(**kw)
    result=collect_incrementally(collect,state,now=10900,**kwargs)
    assert not result['incremental']['checkpoint_advanced']
    assert state.read_bytes()==before


def test_state_cannot_alias_input_or_ledger(tmp_path):
    kwargs=setup_paths(tmp_path)
    for target in (kwargs['state_db'],kwargs['usage_ledger']):
        with pytest.raises(ValueError): collect_incrementally(healthy,target,**kwargs)


def test_shared_lock_serializes_fast_and_deep(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    kwargs=setup_paths(tmp_path); state=tmp_path/'cursor.json'
    entered=Event(); release=Event(); second_entered=Event(); seen=[]
    def first(**kw):
        entered.set(); assert release.wait(3); return healthy(**kw)
    def second(**kw):
        second_entered.set(); seen.append(kw['usage_since']); return healthy(**kw)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(collect_incrementally,first,state,now=10000,**kwargs)
        assert entered.wait(3)
        two=pool.submit(collect_incrementally,second,state,now=10900,**kwargs)
        assert not second_entered.wait(0.05)
        release.set(); one.result(3); two.result(3)
    assert seen==[9400]


def test_rejects_wrong_scope_or_explicit_cutoff(tmp_path):
    state=tmp_path/'cursor.json'; kwargs=setup_paths(tmp_path)
    with pytest.raises(ValueError):
        collect_incrementally(healthy,state,**dict(kwargs,scope='all'))
    with pytest.raises(ValueError):
        collect_incrementally(healthy,state,usage_since=100,**kwargs)
