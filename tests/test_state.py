import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from b2b_agent.config import OutreachConfig
from b2b_agent.outbox import DispatchBlocked, Outbox
from b2b_agent.state import WorkflowStore


def action(store, **overrides):
    conv = store.get_conversation('c')
    if not conv:
        store.put_conversation({'id':'c','account_id':'acme','mailbox_id':'owner','participants':['buyer@example.com']})
    payload = dict(id='a',conversation_id='c',account_id='acme',recipient='buyer@example.com',
                   sender='owner@example.org',subject='Task',body='Exact reviewed text',
                   source_url='https://platform.example/ad/1',message_key='m1',
                   dependency_version='catalog1',contact_permission={'status':'allowed'},
                   assessment={'allowed':True,'decision':'propose_message'})
    payload.update(overrides)
    return store.queue_action(payload)


def settings():
    return OutreachConfig(enabled=True,from_email='owner@example.org',signature='Owner')


def test_exact_individual_approval_and_single_use(tmp_path):
    out = Outbox(str(tmp_path/'db'))
    a=action(out.workflow)
    with pytest.raises(DispatchBlocked,match='pending_approval'):
        out.authorize_delivery(a['id'],settings(),current_dependencies='catalog1')
    out.workflow.approve_action(a['id'],'operator')
    permit=out.authorize_delivery(a['id'],settings(),current_dependencies='catalog1')
    with pytest.raises(DispatchBlocked,match='content_changed'):
        permit.assert_valid(a['recipient'],a['subject'],'tampered',a['message_key'])
    permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'])
    with pytest.raises(DispatchBlocked,match='already_consumed'):
        permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'])
    out.finish(a['message_key'],'sent','provider')
    assert out.workflow.get_action('a')['status']=='sent'
    with pytest.raises(DispatchBlocked):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')


def test_edit_and_relevant_catalog_invalidate(tmp_path):
    out=Outbox(str(tmp_path/'db'))
    action(out.workflow)
    out.workflow.approve_action('a','operator')
    with pytest.raises(DispatchBlocked,match='catalog_stale'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog2')
    out.workflow.update_action('a',body='Changed')
    with pytest.raises(DispatchBlocked,match='pending_approval'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')


def test_suppression_other_connection_before_dispatch(tmp_path):
    path=str(tmp_path/'db')
    out=Outbox(path)
    a=action(out.workflow)
    out.workflow.approve_action('a','operator')
    other=WorkflowStore(path)
    other.suppress('contact',a['recipient'],'do not write')
    with pytest.raises(DispatchBlocked,match='suppressed'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    assert out.db.execute('SELECT COUNT(*) FROM outbound_messages').fetchone()[0]==0


def test_suppression_after_reservation_before_transport(tmp_path):
    path=str(tmp_path/'db')
    out=Outbox(path)
    a=action(out.workflow)
    out.workflow.approve_action('a','operator')
    permit=out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    other=WorkflowStore(path)
    other.suppress('contact',a['recipient'],'do not write')
    with pytest.raises(DispatchBlocked,match='suppressed'):
        permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'])


def test_inbound_idempotent_cancels_approved_action(tmp_path):
    store=WorkflowStore(tmp_path/'db')
    action(store)
    store.approve_action('a','operator')
    event={'id':'i','mailbox_id':'owner','provider_message_id':'gmail1','sender':'buyer@example.com','body':'interesting, but do not email me'}
    assert store.apply_inbound(event,'c',labels=['interest','opt_out'],stop_scope='contact')
    assert not store.apply_inbound(event,'c',labels=['interest','opt_out'],stop_scope='contact')
    assert store.get_conversation('c')['context_version']==2
    assert store.get_action('a')['status']=='cancelled'
    assert len(store.list_inbound())==1
    assert len(store.list_events())==1
    assert store.is_suppressed({'recipient':'buyer@example.com','channel':'telegram'})


def test_pause_persistent_and_expired_approval_not_restored(tmp_path):
    path=str(tmp_path/'db')
    out=Outbox(path)
    action(out.workflow)
    now=datetime.now(UTC)
    out.workflow.approve_action('a','operator',ttl_seconds=1,now=now-timedelta(seconds=5))
    out.workflow.pause('global','*')
    out.close()
    out=Outbox(path)
    with pytest.raises(DispatchBlocked,match='paused'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    out.workflow.resume('global','*')
    with pytest.raises(DispatchBlocked,match='expired'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')


def test_reply_requires_fresh_sync_and_skips_initial_cooldown(tmp_path):
    out=Outbox(str(tmp_path/'db'))
    action(out.workflow)
    out.workflow.approve_action('a','operator')
    out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    out.finish('m1','sent','provider')
    reply=action(out.workflow,id='reply',kind='reply',message_key='m2',mailbox_id='owner')
    out.workflow.approve_action('reply','operator')
    with pytest.raises(DispatchBlocked,match='stale_sync'):
        out.authorize_delivery('reply',settings(),current_dependencies='catalog1')
    out.workflow.mark_sync('owner')
    permit=out.authorize_delivery('reply',settings(),current_dependencies='catalog1')
    permit.assert_valid(reply['recipient'],reply['subject'],reply['body'],reply['message_key'])


def test_migration_real_legacy_schema_preserves_uncertain_and_suppression(tmp_path):
    path=str(tmp_path/'db')
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE outbound_messages(message_key TEXT PRIMARY KEY,source_url TEXT NOT NULL UNIQUE,recipient TEXT NOT NULL,created_at TEXT NOT NULL,local_day TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,provider_id TEXT,error TEXT);CREATE TABLE suppressed_emails(recipient TEXT PRIMARY KEY,reason TEXT NOT NULL);''')
    now=datetime.now(UTC).isoformat()
    db.execute('INSERT INTO outbound_messages VALUES (?,?,?,?,?,?,?,?,?)',('old','https://platform/ad/1','buyer@example.com',now,now[:10],'uncertain','{}',None,'timeout'))
    db.execute('INSERT INTO suppressed_emails VALUES (?,?)',('blocked@example.com','old optout'))
    db.commit()
    db.close()
    out=Outbox(path)
    row=out.db.execute('SELECT * FROM outbound_messages').fetchone()
    assert row['status']=='uncertain' and row['message_key']=='old'
    assert out.reserve({'message_key':'old','recipient':'buyer@example.com','source_url':'https://platform/ad/1'},settings())=='duplicate'
    assert out.reserve({'message_key':'new','recipient':'buyer@example.com','source_url':'https://platform/ad/1','kind':'reply'},settings())=='reserved'
    assert out.reserve({'message_key':'blocked','recipient':'blocked@example.com','source_url':'https://platform/ad/2'},settings())=='suppressed'
    assert out.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_operator_rejection_not_customer_rejection(tmp_path):
    store=WorkflowStore(tmp_path/'db')
    action(store)
    store.reject_action('a','operator')
    assert not store.get_conversation('c').get('is_terminal',False)
    assert store.list_events()[0]['kind']=='operator_rejected'
    assert not store.is_suppressed({'recipient':'buyer@example.com'})


def test_unknown_permission_blocks_even_approved(tmp_path):
    out=Outbox(str(tmp_path/'db'))
    action(out.workflow,contact_permission={'status':'unknown'})
    out.workflow.approve_action('a','operator')
    with pytest.raises(DispatchBlocked,match='contact_policy'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')


def test_final_catalog_reload_and_metadata_bound(tmp_path):
    out=Outbox(str(tmp_path/'db'))
    a=action(out.workflow, references=['<old@example.org>'],in_reply_to='<old@example.org>',original_subject='Task')
    out.workflow.approve_action('a','operator')
    current=['catalog1']
    permit=out.authorize_delivery('a',settings(),current_dependencies='catalog1',dependency_loader=lambda:current[0])
    metadata={'sender':'owner@example.org','reply_to':'','provider':'gmail','thread_id':None,'in_reply_to':'<old@example.org>','references':['<old@example.org>'],'original_subject':'Task','unsubscribe_headers':{}}
    with pytest.raises(DispatchBlocked,match='metadata_changed'):
        permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'],metadata={**metadata,'thread_id':'foreign-thread'})
    current[0]='catalog2'
    with pytest.raises(DispatchBlocked,match='catalog_stale'):
        permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'],metadata=metadata)


async def test_smtp_thread_uses_own_connection_and_final_pause(tmp_path):
    import asyncio
    out=Outbox(str(tmp_path/'db'))
    a=action(out.workflow)
    out.workflow.approve_action('a','operator')
    permit=out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    await asyncio.to_thread(permit.assert_valid,a['recipient'],a['subject'],a['body'],a['message_key'],consume=False)
    other=WorkflowStore(tmp_path/'db')
    other.pause('conversation','c')
    with pytest.raises(DispatchBlocked,match='paused'):
        await asyncio.to_thread(permit.assert_valid,a['recipient'],a['subject'],a['body'],a['message_key'])


def test_action_cost_denied_and_sender_change_block(tmp_path):
    out=Outbox(str(tmp_path/'db'))
    action(out.workflow,assessment={'allowed':False,'decision':'cheaper_alternative'})
    out.workflow.approve_action('a','operator')
    with pytest.raises(DispatchBlocked,match='action_policy'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    out.workflow.update_action('a',assessment={'allowed':True,'decision':'propose_message'},sender_account='other@example.org')
    out.workflow.approve_action('a','operator')
    with pytest.raises(DispatchBlocked,match='sender_changed'):
        out.authorize_delivery('a',settings(),current_dependencies='catalog1')


def test_opportunity_and_inbound_update_commit_together(tmp_path):
    store=WorkflowStore(tmp_path/'db')
    store.put_opportunity({'id':'opp','need':{'status':'hypothesized'}})
    store.put_conversation({'id':'c','opportunity_id':'opp','account_id':'acme'})
    event={'id':'e','provider_message_id':'p','mailbox_id':'m','sender':'buyer@example.com'}
    with pytest.raises(ValueError,match='identity'):
        store.apply_inbound(event,'c',opportunity_update={'id':'wrong','need':{'status':'disproved'}})
    assert store.list_inbound()==[]
    assert store.get_conversation('c')['context_version']==1
    assert store.apply_inbound(event,'c',opportunity_update={'need':{'status':'disproved','source_ref':'e'}})
    assert store.get_opportunity('opp')['need']=={'status':'disproved','source_ref':'e'}


def test_personal_optout_pauses_company_bounce_only_blocks_address(tmp_path):
    store=WorkflowStore(tmp_path/'db')
    store.put_conversation({'id':'c','account_id':'acme'})
    store.apply_inbound({'id':'e','provider_message_id':'p','mailbox_id':'m','sender':'buyer@example.com'},'c',labels=['opt_out'],stop_scope='contact')
    assert store.pause_reason({'account_id':'acme'})
    store.put_conversation({'id':'b','account_id':'another'})
    store.apply_inbound({'id':'bounce','provider_message_id':'p2','mailbox_id':'m','sender':'mailer-daemon@example.org','suppression_target':'invalid@another.test'},'b',labels=['hard_bounce'],stop_scope='contact')
    assert not store.pause_reason({'account_id':'another'})
    assert store.is_suppressed({'recipient':'invalid@another.test'})
    assert not store.is_suppressed({'recipient':'mailer-daemon@example.org'})


def test_issued_unsubscribe_headers_are_pinned_before_transport(tmp_path):
    out=Outbox(str(tmp_path/'db'))
    a=action(out.workflow)
    out.workflow.approve_action('a','operator')
    permit=out.authorize_delivery('a',settings(),current_dependencies='catalog1')
    headers={'List-Unsubscribe':'<https://unsubscribe.example/token>','List-Unsubscribe-Post':'List-Unsubscribe=One-Click'}
    permit.bind_unsubscribe_headers(headers)
    metadata={'sender':'owner@example.org','reply_to':'','provider':'gmail','thread_id':None,'in_reply_to':None,'references':[],'original_subject':None,'unsubscribe_headers':{'List-Unsubscribe':'<https://unrelated.example/token>'}}
    with pytest.raises(DispatchBlocked,match='issued_unsubscribe_headers_changed'):
        permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'],metadata=metadata)
    metadata['unsubscribe_headers']=headers
    permit.assert_valid(a['recipient'],a['subject'],a['body'],a['message_key'],metadata=metadata)
    with pytest.raises(DispatchBlocked,match='already_bound'):
        permit.bind_unsubscribe_headers(headers)
