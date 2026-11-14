import asyncio,json
from pathlib import Path
import pytest
from backend.history import HistoryStore
from backend.queue_store import QueueStore
import backend.queue_api as qapi
from backend.test_main import make_client

def save(h,**extra):
    return h.save(filename='same.wav',text='梧桐计划',segments=[{'start':2,'end':3,'text':'梧桐计划'}],stats={'audio_duration':4,'chunk_failures':0},**extra)

def test_search_past_200_records(tmp_path):
    h=HistoryStore(tmp_path/'history')
    first=save(h)
    for i in range(205): h.save(filename=f'{i}.wav',text='其他内容',segments=[],stats={})
    assert h.search(query='梧桐计划')['items'][0]['id']==first['id']

def test_versions_and_single_task_in_batch(tmp_path):
    h=HistoryStore(tmp_path/'history')
    a=save(h,task_id='111111111111',batch_id='aabbccddeeff')
    b=save(h,task_id='111111111111',batch_id='aabbccddeeff')
    assert b['result_version']==2
    batch=h.batches()[0]
    assert len(batch['items'])==1
    assert batch['items'][0]['history_id']==b['id']

def test_view_is_persistent(tmp_path):
    h=HistoryStore(tmp_path/'history');r=save(h)
    marked=h.update_view(r['id'],2.5)
    assert HistoryStore(tmp_path/'history').get(r['id'])['read_position']==2.5
    assert marked['first_viewed_at']

def test_legacy_cancelled_is_partial(tmp_path):
    h=HistoryStore(tmp_path/'history');r=save(h)
    r.pop('integrity');r['stats']['cancelled']=True
    h._path(r['id']).write_text(json.dumps(r),encoding='utf8')
    assert h.get(r['id'])['integrity']=='partial'
    assert h.search(filter_by='partial')['total']==1

def test_completed_cleanup_preserves_errors_and_cancel(tmp_path):
    s=QueueStore(tmp_path/'queue')
    for state in ['done','error','cancelled']:
        item=s.add(qid=s.new_id(),filename='a.wav',staged_name=state+'.wav',size=1,align=False)
        (s.uploads_dir/(state+'.wav')).write_bytes(b'a')
        s.set_status(item['id'],state)
    assert s.clear_finished()==1
    assert {i['status'] for i in s.snapshot()['items']}=={'error','cancelled'}
    assert (s.uploads_dir/'cancelled.wav').exists()

def test_partial_upload_receipt(tmp_path,monkeypatch):
    c=make_client(tmp_path=tmp_path);qapi.store=QueueStore(tmp_path/'queue')
    monkeypatch.setitem(qapi._deps,'max_upload_bytes',5)
    r=c.post('/api/queue/items',files=[('files',('same.wav',b'1234','audio/wav')),('files',('same.wav',b'123456','audio/wav'))],
        data={'batch_id':'aabbccddeeff','batch_name':'test','source_paths':'["A/same.wav","B/same.wav"]'})
    assert r.status_code==413
    assert len(r.json()['items'])==1
    assert r.json()['items'][0]['source_path']=='A/same.wav'
    assert len(qapi.store.snapshot()['items'])==1

def test_save_failure_cannot_reuse_old_result(tmp_path,monkeypatch):
    make_client(tmp_path=tmp_path);s=QueueStore(tmp_path/'queue');qapi.store=s
    item=s.add(qid=s.new_id(),filename='x.wav',staged_name='x.wav',size=1,align=False)
    (s.uploads_dir/'x.wav').write_bytes(b'x')
    s.set_status(item['id'],'cancelled',history_id='111111111111')
    s.set_status(item['id'],'queued')
    async def transcribe(**kwargs): return {'history_id':None,'text':'available text','segments':[],'stats':{}}
    monkeypatch.setitem(qapi._deps,'transcribe_fn',transcribe)
    assert asyncio.run(qapi.process_next_once())
    row=s.get(item['id'])
    assert row['history_id'] is None
    assert row['error']=='history_save_failed'

def test_save_failure_keeps_source_recoverable(tmp_path,monkeypatch):
    make_client(tmp_path=tmp_path);s=QueueStore(tmp_path/'queue');qapi.store=s
    item=s.add(qid=s.new_id(),filename='x.wav',staged_name='x.wav',size=1,align=False)
    staged=s.uploads_dir/'x.wav';staged.write_bytes(b'x')
    async def transcribe(**kwargs): return {'history_id':None,'text':'available text','segments':[],'stats':{}}
    monkeypatch.setitem(qapi._deps,'transcribe_fn',transcribe)
    asyncio.run(qapi.process_next_once())
    assert staged.exists(), 'save failed but source deleted, no reader record and no retry'

def test_viewing_older_version_does_not_make_it_latest(tmp_path,monkeypatch):
    import backend.history as mod
    import time
    monkeypatch.setattr(mod,'_now_iso',lambda:'2026-10-30T00:00:00+00:00')
    h=HistoryStore(tmp_path/'history')
    a=save(h,task_id='111111111111',batch_id='aabbccddeeff')
    b=save(h,task_id='111111111111',batch_id='aabbccddeeff')
    time.sleep(.05)
    h.update_view(a['id'],1)
    assert h.batches()[0]['items'][0]['history_id']==b['id'], 'viewing version 1 displaced version 2'
