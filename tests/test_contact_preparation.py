import pytest

from b2b_agent.analysis import assess_research, qualify
from b2b_agent.config import Config
from b2b_agent.contact_preparation import prepare_company_contact
from b2b_agent.models import Contact, Fact, Research
from b2b_agent.outbox import DispatchBlocked, Outbox
from b2b_agent.storage import Store
from b2b_agent.workflow import current_catalog, dispatch_action


@pytest.fixture
def setup(tmp_path):
    config=Config(input_path=str(tmp_path/'absent'),
        outreach={'from_email':'owner@example.org','signature':'Оператор'})
    path=str(tmp_path/'db')
    report=Research(domain='acme.test',website='https://acme.test',facts=[
        Fact(id='f1',kind='order',quote='Оставьте заявку менеджеру',source_url='https://acme.test/order')],
        contacts=[Contact(kind='email',value='sales@acme.test',source_url='https://acme.test/contacts')])
    qualify(report)
    storage=Store(path)
    storage.save(report)
    storage.close()
    box=Outbox(path)
    opportunity=assess_research(report,current_catalog(config)).model_dump(mode='json')
    box.workflow.put_opportunity(opportunity)
    yield box,config,opportunity
    box.close()


def authorize_policy(config):
    config.policy.cold_contact_allowed=True
    config.policy.market='owner-reviewed-market-policy'
    config.policy.provider_policy='owner-reviewed-provider-policy'


def test_unknown_policy_still_prepares_draft_but_cannot_send(setup):
    box,config,opp=setup
    action=prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    assert action['status']=='pending_approval'
    assert action['contact_verified']
    assert action['contact_permission']['status']=='unknown'
    assert action['interest']=='unconfirmed'
    assert action['evidence_snapshot']
    assert not action['assessment']['allowed']
    assert action['preparation_assessment']['allowed']
    box.workflow.approve_action(action['id'],'owner')
    with pytest.raises(DispatchBlocked,match='transport_disabled|contact_policy'):
        box.authorize_delivery(action['id'],config.outreach,current_dependencies=action['dependency_version'])
    assert box.db.execute('SELECT COUNT(*) FROM outbound_messages').fetchone()[0]==0


async def test_verified_published_contact_strict_policy_approval_and_simulated_dispatch(setup):
    box,config,opp=setup
    authorize_policy(config)
    action=prepare_company_contact(box,opp['id'],'sales@acme.test','email',config,
        basis='Owner verified an applicable permission for this purpose')
    assert action['contact_permission']['status']=='allowed'
    assert action['validation_results']==[]
    assert action['assessment']['allowed']
    assert action['status']=='pending_approval'
    box.workflow.approve_action(action['id'],'owner')
    sent=await dispatch_action(box,action['id'],config,simulated=True)
    assert sent['status']=='simulated_sent'
    again=prepare_company_contact(box,opp['id'],'sales@acme.test','email',config,
        basis='Owner verified an applicable permission for this purpose')
    assert again['id']==action['id'] and again['status']=='simulated_sent'
    assert len(box.workflow.list_actions())==1


def test_operator_source_verification_and_idempotent_ticks_preserve_approval(setup):
    box,config,opp=setup
    authorize_policy(config)
    unknown=prepare_company_contact(box,opp['id'],'other@acme.test','email',config,basis='Owner applicable basis',source='operator:record-17')
    assert unknown['contact_permission']['status']=='unknown'
    verified=prepare_company_contact(box,opp['id'],'other@acme.test','email',config,
        verified_contact=True,basis='Owner applicable basis',source='operator:record-17')
    assert verified['id']==unknown['id'] and verified['contact_permission']['status']=='allowed'
    box.workflow.approve_action(verified['id'],'owner')
    repeated=prepare_company_contact(box,opp['id'],'other@acme.test','email',config,
        verified_contact=True,basis='Owner applicable basis',source='operator:record-17')
    assert repeated['status']=='approved'
    assert len(box.workflow.list_actions())==1 and len(box.workflow.list_conversations())==1


@pytest.mark.parametrize('recipient',['sales','Company <sales@acme.test>','sales@acme.test\nBcc: victim@example.org'])
def test_no_address_generation_or_header_injection(setup,recipient):
    box,config,opp=setup
    with pytest.raises(ValueError,match='literal|recipient'):
        prepare_company_contact(box,opp['id'],recipient,'email',config)
    assert box.workflow.list_actions()==[]


def test_platform_contact_never_treated_as_company_published_contact(setup):
    box,config,opp=setup
    authorize_policy(config)
    import json
    row=box.db.execute("SELECT payload FROM companies WHERE domain='acme.test'").fetchone()
    report=json.loads(row[0])
    report['contacts'][0]['source_url']='https://marketplace.test/requests/1'
    with box.db:
        box.db.execute("UPDATE companies SET payload=? WHERE domain='acme.test'",(json.dumps(report),))
    action=prepare_company_contact(box,opp['id'],'sales@acme.test','email',config,basis='Owner basis')
    assert not action['contact_verified'] and action['contact_permission']['status']=='unknown'


def test_parallel_public_request_or_other_recipient_prevents_new_company_attempt(setup):
    box,config,opp=setup
    box.workflow.put_conversation({'id':'request-conv','opportunity_id':'another-request',
        'account_id':None,'recipient':'sales@acme.test','participants':['sales@acme.test']})
    with pytest.raises(ValueError,match='active'):
        prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    assert box.workflow.list_actions()==[]
    assert len(box.workflow.list_conversations())==1


def test_same_company_different_recipient_requires_review(setup):
    box,config,opp=setup
    prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    with pytest.raises(ValueError,match='active'):
        prepare_company_contact(box,opp['id'],'ceo@acme.test','email',config,
            verified_contact=True,source='operator:ceo',basis='Owner basis')
    assert len(box.workflow.list_conversations())==1
    assert len(box.workflow.list_actions())==1


def test_closed_attempt_is_not_overwritten_by_preparation(setup):
    box,config,opp=setup
    action=prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    conv=box.workflow.get_conversation(action['conversation_id'])
    conv.update(is_terminal=True,stage='closed',closed_reason='customer declined')
    box.workflow.put_conversation(conv)
    with pytest.raises(ValueError,match='closed'):
        prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    assert box.workflow.get_conversation(conv['id'])['is_terminal']


@pytest.mark.parametrize('field',['market','provider_policy'])
def test_unknown_market_or_provider_never_grants_cold_permission(setup,field):
    box,config,opp=setup
    authorize_policy(config)
    setattr(config.policy,field,'unknown')
    action=prepare_company_contact(box,opp['id'],'sales@acme.test','email',config,basis='Owner basis')
    assert action['contact_permission']['status']=='unknown'
    assert not action['assessment']['allowed']


def test_pause_and_suppression_stop_new_company_proposals(setup):
    box,config,opp=setup
    box.workflow.pause('account','acme.test','operator pause')
    with pytest.raises(ValueError,match='pause'):
        prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    box.workflow.resume('account','acme.test')
    box.workflow.suppress('contact','sales@acme.test')
    with pytest.raises(ValueError,match='suppression'):
        prepare_company_contact(box,opp['id'],'sales@acme.test','email',config)
    assert box.workflow.list_actions()==[]


def test_concurrent_company_contact_preparation_keeps_one_active_attempt(setup):
    from concurrent.futures import ThreadPoolExecutor
    box,config,opp=setup
    path=box.path

    def prepare(recipient):
        worker=Outbox(path)
        try:
            try:
                return prepare_company_contact(worker,opp['id'],recipient,'email',config,
                    verified_contact=True,source='operator:verified-person',basis='Owner basis')['id']
            except ValueError as error:
                assert 'active' in str(error)
                return None
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(prepare,['sales@acme.test','other@acme.test']))
    assert sum(value is not None for value in results)==1
    assert len(box.workflow.list_conversations())==1
    assert len(box.workflow.list_actions())==1
