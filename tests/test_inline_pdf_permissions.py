"""Real PTB handlers + SQLite + existing poll engines/real PDF renderer, offline API.

No test here claims to have opened a Telegram client or contacted production.
Harness(pdf_choice=None) never auto-clicks consent, allowing exact UI flow tests.
"""
import ast
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import fitz
import pytest

import backup
import bot
import config
import database as db
import group_runner
import quiz_modes
import quiz_runner
import review_pdf
from test_bot_flow import Harness, buttons, make_quiz, run

ADMIN = 940001
GROUP = -100940001


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    old = db.get_db_path()
    db.set_db_path(tmp_path / 'consent.db')
    db.init_db()
    monkeypatch.setattr(config, 'ADMIN_IDS', (ADMIN,))
    monkeypatch.setattr(config, 'REVIEW_PDF_ENABLED', True)
    monkeypatch.setattr(config, 'REVIEW_PDF_PRIVATE_AUTO', True)
    monkeypatch.setattr(config, 'REVIEW_PDF_GROUP_AUTO', True)
    monkeypatch.setattr(config, 'AUTO_HTML_REPORT', False)
    monkeypatch.setattr(config, 'NEXT_QUESTION_DELAY', 0.03)
    monkeypatch.setattr(config, 'GROUP_NEXT_DELAY', 0.03)
    monkeypatch.setattr(config, 'TIMER_GRACE', 0.2)
    db.ensure_user_row(ADMIN)
    yield
    db.set_db_path(old)


def permission_actions(h, uid):
    return buttons(h.api.sent('sendMessage', uid)[-1].get('reply_markup'))


async def choose_private(h, u, qid, allow=True, message_id=456):
    await h.text(u, f'/start quiz_{qid}')
    actions = permission_actions(h, u['id'])
    assert [a['text'] for a in actions] == ['📄 Allow PDF Review', '🚫 Do Not Allow PDF']
    assert not h.polls_for(u['id']) or db.get_active_attempt(u['id'])
    await h.cb(u, actions[0 if allow else 1]['callback_data'], message_id=message_id)
    att = db.get_active_attempt(u['id'])
    assert att and att['pdf_allowed'] == int(allow)
    return att, actions


async def complete_private(h, u, n, correct=True, offset=0):
    for i in range(n):
        await h.wait(lambda: len(h.polls_for(u['id'])) == offset+i+1)
        await h.answer_current(u, correct)
    await h.wait(lambda: db.get_active_attempt(u['id']) is None, msg='completed attempt')
    # Finish includes rendering in the answer handler/job, wait for its result hook.
    await h.wait(lambda: '🏁' in h.all_text(u['id']))


async def lobby(h, u, qid, group=GROUP):
    await h.text(u, f'/start@TestQuizBot quiz_{qid}', chat_id=group)
    await h.cb(u, f'g:go:{qid}', chat_id=group)
    s = db.get_active_group_session(group)
    assert s['status'] == 'pending'
    assert not h.polls_for(group)
    return s


async def finish_group(h, users, group=GROUP, n=1):
    for i in range(n):
        await h.wait(lambda: len(h.polls_for(group)) == i+1)
        pid, p = h.polls_for(group)[-1]
        for user in users:
            await h.send(h.f.poll_answer(user, pid, p['correct_option_id']))
        # Close through the REAL timeout/next jobs without waiting 45 seconds.
        s = db.get_active_group_session(group)
        async with group_runner.chat_lock(group):
            await group_runner._close_current_locked(h.app, s)
    await h.wait(lambda: db.get_active_group_session(group) is None, msg='group completion')
    await h.wait(lambda: '🏆' in h.all_text(group))


@pytest.mark.parametrize('query', ['', ' ', 'quiz_', 'not-an-id', 'some title', 'quiz_abcd extra', 'quiz_doesnotexist'])
def test_empty_invalid_and_search_queries_never_list_quizzes(query):
    async def t():
        async with Harness(pdf_choice=None) as h:
            u = h.f.user(940101)
            make_quiz(owner=u['id'], n=1, title='some title')
            make_quiz(owner=u['id'], n=1, title='another title')
            await h.send(h.f.inline_query(u, query))
            assert h.api.inline_answers[-1]['results'] == []
    run(t())


def test_inline_exactly_one_preview_three_functional_actions_and_same_id():
    async def t():
        async with Harness(pdf_choice=None) as h:
            owner, player = h.f.user(940102, 'Creator', 'creator_name'), h.f.user(940103)
            db.upsert_user(SimpleNamespace(**owner))
            qid = make_quiz(owner=owner['id'], n=2, timer=45, title='Hindi <Quiz> & One')
            db.update_quiz(qid, description='Only this requested quiz & its full description')
            make_quiz(owner=owner['id'], n=3, title='Must not appear')
            await h.send(h.f.inline_query(player, f'quiz_{qid}'))
            result, = h.api.inline_answers[-1]['results']
            assert result['id'] == qid
            text = result['input_message_content']['message_text']
            for expected in ('Hindi &lt;Quiz&gt; &amp; One', 'Only this requested quiz &amp; its full description',
                             'Questions: 2', '45 sec', 'Creator', '@creator_name', 'via @TestQuizBot'):
                assert expected in text
            actions = buttons(result['reply_markup'])
            assert [a['text'] for a in actions] == ['▶️ Start this quiz', '👥 Start quiz in group', '↗️ Share quiz']
            assert parse_qs(urlparse(actions[0]['url']).query) == {'start': [f'quiz_{qid}']}
            assert parse_qs(urlparse(actions[1]['url']).query) == {'startgroup': [f'quiz_{qid}']}
            assert actions[2]['switch_inline_query'] == f'quiz_{qid}'
            # Follow private URL payload into the actual /start handler; still no poll before consent.
            await h.text(player, '/start ' + parse_qs(urlparse(actions[0]['url']).query)['start'][0])
            assert not h.polls_for(player['id'])
            private_actions = permission_actions(h, player['id'])
            await h.cb(player, private_actions[1]['callback_data'])
            assert db.get_active_attempt(player['id'])['quiz_id'] == qid
            # Follow group picker URL's payload through actual group start flow.
            await h.text(owner, '/start@TestQuizBot ' + parse_qs(urlparse(actions[1]['url']).query)['startgroup'][0], chat_id=GROUP)
            await h.cb(owner, f'g:go:{qid}', chat_id=GROUP)
            s = db.get_active_group_session(GROUP)
            await h.cb(owner, f'pd:g:0:{s["id"]}', chat_id=GROUP)
            await h.cb(owner, f'pd:begin:{s["id"]}', chat_id=GROUP)
            assert db.get_active_group_session(GROUP)['quiz_id'] == qid
            assert h.polls_for(GROUP)
            # Simulate the native share button reissuing its unchanged query.
            await h.send(h.f.inline_query(player, actions[2]['switch_inline_query']))
            reshared, = h.api.inline_answers[-1]['results']
            assert reshared['input_message_content'] == result['input_message_content']
            assert reshared['reply_markup'] == result['reply_markup']
            assert not h.api.rejections
    run(t())


def test_private_quiz_not_disclosed_to_stranger_and_draft_empty_hidden():
    async def t():
        async with Harness(pdf_choice=None) as h:
            owner, stranger = h.f.user(940104), h.f.user(940105)
            qid = make_quiz(owner=owner['id'], n=1)
            db.update_quiz(qid, access_tier='private')
            await h.send(h.f.inline_query(stranger, f'quiz_{qid}'))
            assert h.api.inline_answers[-1]['results'] == []
            await h.send(h.f.inline_query(owner, f'quiz_{qid}'))
            assert len(h.api.inline_answers[-1]['results']) == 1
            db.update_quiz(qid, status='draft')
            await h.send(h.f.inline_query(owner, f'quiz_{qid}'))
            assert h.api.inline_answers[-1]['results'] == []
            empty = db.new_quiz(owner['id'], 'Empty')
            await h.send(h.f.inline_query(owner, f'quiz_{empty}'))
            assert h.api.inline_answers[-1]['results'] == []
    run(t())


def test_oversized_legacy_description_not_silently_truncated_or_sent_invalid():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u = h.f.user(940106)
            qid = make_quiz(owner=u['id'], n=1)
            original = 'हिंदी विवरण ' * 1000
            db.update_quiz(qid, description=original)
            await h.send(h.f.inline_query(u, f'quiz_{qid}'))
            assert h.api.inline_answers[-1]['results'] == []
            assert db.get_quiz(qid)['description'] == original
    run(t())


def test_no_start_before_decision_including_legacy_and_direct_start_paths():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u = h.f.user(940107)
            qid = make_quiz(owner=u['id'], n=1)
            await h.text(u, f'/quiz {qid}')
            assert not h.polls_for(u['id']) and db.get_active_attempt(u['id']) is None
            await h.cb(u, f'r:go:{qid}')
            assert not h.polls_for(u['id']) and db.get_active_attempt(u['id']) is None
            aid = await quiz_runner.start_quiz(h.app, SimpleNamespace(**u), u['id'], qid)
            assert aid is None and not h.polls_for(u['id'])
    run(t())


@pytest.mark.parametrize('allow', [True, False])
def test_private_choice_completion_real_pdf_or_no_generation(allow, monkeypatch):
    calls = []
    real_render = review_pdf.render_report
    def track(rep):
        calls.append(rep)
        return real_render(rep)
    monkeypatch.setattr(review_pdf, 'render_report', track)
    async def t():
        async with Harness(pdf_choice=None) as h:
            u = h.f.user(940108)
            qid = make_quiz(owner=u['id'], n=2, timer=45)
            att, _ = await choose_private(h, u, qid, allow)
            await complete_private(h, u, 2)
            if allow:
                await h.wait(lambda: len(h.api.sent('sendDocument',u['id'])) == 1)
                pdf = [files['document'][1] for api,p,files in h.api.uploads if api=='sendDocument'][-1]
                assert pdf.startswith(b'%PDF')
                with fitz.open(stream=pdf, filetype='pdf') as doc:
                    text = '\n'.join(page.get_text() for page in doc)
                    assert 'LOKESH DAUSA' in text and 'प्रश्न 1' in text and 'प्रश्न 2' in text
                    assert all(page.get_links() for page in doc)
                assert len(calls) == 1
                await h.text(u, '/review')
                assert len(h.api.sent('sendDocument',u['id'])) == 2
            else:
                await h.text(u, '/review')
                assert 'pre-start' in h.api.texts(u['id'])[-1]
                res = await review_pdf.deliver_attempt(h.app.bot, db.get_attempt(att['id']), force=True, reason='command')
                assert res['skipped'] == 'consent-required'
                assert not h.api.sent('sendDocument',u['id']) and calls == []
            assert db.get_attempt(att['id'])['status'] == 'finished'
            assert db.get_attempt(att['id'])['score'] == 2
    run(t())


def test_global_off_no_prompt_no_pdf_even_after_on_again(monkeypatch):
    def forbidden_render(rep):
        raise AssertionError('PDF renderer must not run')
    monkeypatch.setattr(review_pdf, 'render_report', forbidden_render)
    async def t():
        async with Harness(pdf_choice=None) as h:
            admin, u = h.f.user(ADMIN), h.f.user(940109)
            await h.text(admin, '/admin')
            assert 'PDF Review System: ON' in h.api.texts(ADMIN)[-1]
            await h.cb(admin, 'pd:global:0')
            assert not db.pdf_review_enabled()
            qid = make_quiz(owner=u['id'], n=1)
            await h.text(u, f'/start quiz_{qid}')
            actions = permission_actions(h,u['id'])
            assert len(actions)==1 and actions[0]['callback_data']==f'r:go:{qid}'
            await h.cb(u, actions[0]['callback_data'])
            att = db.get_active_attempt(u['id'])
            assert att['pdf_allowed'] == 0
            await h.cb(admin, 'pd:global:1')
            await complete_private(h,u,1)
            assert not h.api.sent('sendDocument',u['id'])
            assert (await review_pdf.deliver_attempt(h.app.bot,db.get_attempt(att['id']),force=True,reason='command'))['skipped']=='consent-required'
    run(t())


def test_global_off_mid_attempt_overrides_allow_and_retry(monkeypatch):
    calls=[]
    monkeypatch.setattr(review_pdf,'render_report',lambda rep: calls.append(rep))
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940110)
            qid=make_quiz(owner=u['id'],n=1)
            att,_=await choose_private(h,u,qid,True)
            db.set_pdf_review_enabled(False,ADMIN)
            await complete_private(h,u,1)
            ctx=SimpleNamespace(job=SimpleNamespace(data={'kind':'attempt','attempt_id':att['id']}),bot=h.app.bot)
            await review_pdf._retry_job(ctx)
            assert calls==[] and not h.api.sent('sendDocument',u['id'])
            await h.text(u,'/review')
            assert 'globally OFF' in h.api.texts(u['id'])[-1]
    run(t())


def test_off_during_render_is_rechecked_before_upload(monkeypatch):
    real=review_pdf.render_report
    def toggle(rep):
        result=real(rep)
        db.set_pdf_review_enabled(False,ADMIN)
        return result
    monkeypatch.setattr(review_pdf,'render_report',toggle)
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940111)
            qid=make_quiz(owner=u['id'],n=1)
            att,_=await choose_private(h,u,qid,True)
            await complete_private(h,u,1)
            # Wait for any thread started by the finish hook to return.
            await h.wait(lambda:not db.pdf_review_enabled())
            assert not h.api.sent('sendDocument',u['id'])
            assert db.get_attempt(att['id'])['status']=='finished'
    run(t())


def test_global_switch_admin_only_and_environment_kill_switch(monkeypatch):
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940112)
            await h.cb(u,'pd:global:0')
            assert db.pdf_review_enabled()
            with pytest.raises(PermissionError): db.set_pdf_review_enabled(False,u['id'])
            await h.cb(h.f.user(ADMIN),'pd:global:0',chat_id=GROUP)
            assert db.pdf_review_enabled()
            db.set_pdf_review_enabled(False,ADMIN)
            db.init_db()
            assert not db.pdf_review_enabled()  # survives init/restart
            db.set_pdf_review_enabled(True,ADMIN)
            monkeypatch.setattr(config,'REVIEW_PDF_ENABLED',False)
            assert not db.pdf_review_enabled()
    run(t())


def test_two_users_independent_choices_real_completion():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a,b=h.f.user(940113),h.f.user(940114)
            qid=make_quiz(owner=a['id'],n=1)
            aa,_=await choose_private(h,a,qid,True)
            bb,_=await choose_private(h,b,qid,False)
            await complete_private(h,a,1)
            await complete_private(h,b,1)
            await h.wait(lambda:h.api.sent('sendDocument',a['id']))
            assert not h.api.sent('sendDocument',b['id'])
            assert db.get_attempt(aa['id'])['pdf_allowed']==1
            assert db.get_attempt(bb['id'])['pdf_allowed']==0
    run(t())


def test_private_card_actor_binding_single_use_and_new_attempt_needs_new_choice():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a,b=h.f.user(940115),h.f.user(940116)
            qid=make_quiz(owner=a['id'],n=1)
            await h.text(a,f'/start quiz_{qid}')
            action=permission_actions(h,a['id'])[0]['callback_data']
            await h.cb(b,action)
            await h.cb(a,action,chat_id=GROUP)
            assert not h.polls_for(a['id']) and not h.polls_for(b['id'])
            await h.cb(a,action,message_id=900)
            aid=db.get_active_attempt(a['id'])['id']
            await h.cb(a,action,message_id=900)
            assert len(h.polls_for(a['id']))==1
            await h.text(a,'/stop')
            await h.cb(a,action,message_id=900)
            assert db.get_active_attempt(a['id']) is None
            await h.cb(a,f'r:go:{qid}',message_id=901)
            assert db.get_active_attempt(a['id']) is None
            actions=permission_actions(h,a['id'])
            await h.cb(a,actions[1]['callback_data'],message_id=901)
            new=db.get_active_attempt(a['id'])
            assert new['id']!=aid and new['pdf_allowed']==0
    run(t())


@pytest.mark.parametrize('command',['/cancel','/stop'])
def test_cancel_invalidates_pending_card(command):
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940117)
            qid=make_quiz(owner=u['id'],n=1)
            await h.text(u,f'/start quiz_{qid}')
            action=permission_actions(h,u['id'])[0]['callback_data']
            await h.text(u,command)
            await h.cb(u,action)
            assert not h.polls_for(u['id'])
    run(t())


def test_old_or_expired_card_cannot_restart_quiz():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940118)
            qid=make_quiz(owner=u['id'],n=1)
            await h.text(u,f'/start quiz_{qid}')
            old=permission_actions(h,u['id'])[0]['callback_data']
            await h.text(u,f'/start quiz_{qid}')
            new=permission_actions(h,u['id'])[0]['callback_data']
            await h.cb(u,old)
            assert not h.polls_for(u['id'])
            with db.connect() as con: con.execute('UPDATE pdf_start_requests SET created_ts=?',(time.time()-1801,))
            await h.cb(u,new)
            assert not h.polls_for(u['id'])
    run(t())


def test_restart_preserves_pending_private_choice_and_allowed_attempt():
    async def t():
        u={'id':940119,'is_bot':False,'first_name':'Restart','username':'restart'}
        qid=make_quiz(owner=u['id'],n=1)
        async with Harness(pdf_choice=None) as h:
            await h.text(u,f'/start quiz_{qid}')
            action=permission_actions(h,u['id'])[0]['callback_data']
        async with Harness(pdf_choice=None) as h:
            await h.cb(u,action)
            att=db.get_active_attempt(u['id'])
            assert att['pdf_allowed']==1
        async with Harness(pdf_choice=None):
            assert db.get_active_attempt(u['id'])['pdf_allowed']==1
    run(t())


def test_group_individual_choices_late_voter_denied_and_same_engine():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a,b,c=h.f.user(940120),h.f.user(940121),h.f.user(940122)
            qid=make_quiz(owner=a['id'],n=1)
            s=await lobby(h,a,qid)
            await h.cb(a,f'pd:begin:{s["id"]}',chat_id=GROUP)
            assert not h.polls_for(GROUP)
            await h.cb(a,f'pd:g:1:{s["id"]}',chat_id=GROUP)
            await h.cb(b,f'pd:g:0:{s["id"]}',chat_id=GROUP)
            assert db.group_pdf_permission(s['id'],a['id'])['allowed']==1
            assert db.group_pdf_permission(s['id'],b['id'])['allowed']==0
            await h.cb(a,f'pd:begin:{s["id"]}',chat_id=GROUP)
            await h.cb(c,f'pd:g:1:{s["id"]}',chat_id=GROUP)
            assert db.group_pdf_permission(s['id'],c['id']) is None
            await finish_group(h,[a,b,c])
            await h.wait(lambda:h.api.sent('sendDocument',a['id']))
            assert not h.api.sent('sendDocument',b['id'])
            assert not h.api.sent('sendDocument',c['id'])
            assert not h.api.sent('sendDocument',GROUP)
            assert len(db.group_leaderboard(s['id']))==3
            for user in (b,c):
                await h.cb(user,f'p:g:{s["id"]}',chat_id=GROUP)
                assert not h.api.sent('sendDocument',user['id'])
            await h.cb(a,f'p:g:{s["id"]}',chat_id=GROUP)
            assert len(h.api.sent('sendDocument',a['id']))==2
    run(t())


def test_group_starter_admin_checks_cross_chat_and_duplicate_begin():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a,b=h.f.user(940123),h.f.user(940124)
            qid=make_quiz(owner=a['id'],n=1)
            s=await lobby(h,a,qid)
            await h.cb(a,f'pd:g:1:{s["id"]}',chat_id=GROUP-1)
            assert db.group_pdf_permission(s['id'],a['id']) is None
            await h.cb(b,f'pd:g:0:{s["id"]}',chat_id=GROUP)
            await h.cb(b,f'pd:begin:{s["id"]}',chat_id=GROUP)
            assert not h.polls_for(GROUP)
            h.api.members[(str(GROUP),b['id'])]='administrator'
            await h.cb(b,f'pd:begin:{s["id"]}',chat_id=GROUP)
            assert len(h.polls_for(GROUP))==1
            await h.cb(b,f'pd:begin:{s["id"]}',chat_id=GROUP)
            await h.cb(a,f'g:go:{qid}',chat_id=GROUP)
            assert len(h.polls_for(GROUP))==1
    run(t())


def test_global_off_group_skips_lobby_and_never_inherits_permissions():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a=h.f.user(940125)
            qid=make_quiz(owner=a['id'],n=1)
            db.set_pdf_review_enabled(False,ADMIN)
            await h.cb(a,f'g:go:{qid}',chat_id=GROUP)
            s=db.get_active_group_session(GROUP)
            assert s['status']=='active' and s['pdf_enabled_at_start']==0
            assert not any('pd:g:' in str(m.get('reply_markup')) for m in h.api.sent('sendMessage',GROUP))
            db.set_pdf_review_enabled(True,ADMIN)
            await finish_group(h,[a])
            assert not h.api.sent('sendDocument',a['id'])
            assert (await review_pdf.deliver_group_member(h.app.bot,s['id'],a['id']))['skipped']=='consent-required'
    run(t())


def test_off_while_group_lobby_pending_can_begin_without_pdf_choice():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a=h.f.user(940126)
            qid=make_quiz(owner=a['id'],n=1)
            s=await lobby(h,a,qid)
            await h.cb(a,f'pd:g:1:{s["id"]}',chat_id=GROUP)
            db.set_pdf_review_enabled(False,ADMIN)
            await h.cb(a,f'pd:begin:{s["id"]}',chat_id=GROUP)
            assert db.get_group_session(s['id'])['pdf_enabled_at_start']==0
            await finish_group(h,[a])
            assert not h.api.sent('sendDocument',a['id'])
            marked=buttons(h.api.sent('sendMessage',GROUP)[-1].get('reply_markup'))
            assert not marked
    run(t())


def test_group_restart_stop_expiry_and_new_session_do_not_reuse_consent():
    async def t():
        a={'id':940127,'is_bot':False,'first_name':'Group Restart'}
        qid=make_quiz(owner=a['id'],n=1)
        async with Harness(pdf_choice=None) as h:
            s=await lobby(h,a,qid)
            await h.cb(a,f'pd:g:1:{s["id"]}',chat_id=GROUP)
        async with Harness(pdf_choice=None) as h:
            assert db.get_active_group_session(GROUP)['status']=='pending'
            assert db.group_pdf_permission(s['id'],a['id'])['allowed']==1
            await h.text(a,'/stop',chat_id=GROUP)
            assert db.get_group_session(s['id'])['status']=='stopped'
            await h.cb(a,f'g:go:{qid}',chat_id=GROUP)
            new=db.get_active_group_session(GROUP)
            assert new['id']!=s['id'] and db.group_pdf_permission(new['id'],a['id']) is None
            with db.connect() as con:con.execute('UPDATE group_sessions SET started_ts=? WHERE id=?',(time.time()-1801,new['id']))
            await group_runner.recover_group_sessions(h.app)
            assert db.get_group_session(new['id'])['status']=='expired'
    run(t())


def test_negative_one_third_and_htmlreport_unaffected_when_pdf_declined():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940128)
            qid=make_quiz(owner=u['id'],n=3)
            db.update_quiz(qid,negative_mark=1/3)
            att,_=await choose_private(h,u,qid,False)
            for i,correct in enumerate((True,False,False)):
                await h.wait(lambda:len(h.polls_for(u['id']))==i+1)
                await h.answer_current(u,correct)
            await h.wait(lambda:db.get_active_attempt(u['id']) is None)
            done=db.get_attempt(att['id'])
            assert done['correct']==1 and done['wrong']==2
            assert done['negative_mark']==pytest.approx(1/3)
            assert done['net_score']==pytest.approx(quiz_modes.compute_net_score(1,2,1/3))
            assert not h.api.sent('sendDocument',u['id'])
            db.set_pdf_review_enabled(False,ADMIN)
            await h.text(u,'/htmlreport')
            assert h.api.sent('sendDocument',u['id'])
            files=[files for api,p,files in h.api.uploads if api=='sendDocument'][-1]
            name,raw=files['document']
            assert name.endswith('.html') and b'Question' in raw
    run(t())


def test_automatic_html_report_independent_of_pdf_permission(monkeypatch):
    monkeypatch.setattr(config,'AUTO_HTML_REPORT',True)
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940129)
            qid=make_quiz(owner=u['id'],n=1)
            await choose_private(h,u,qid,False)
            await complete_private(h,u,1)
            await h.wait(lambda:h.api.sent('sendDocument',u['id']))
            docs=[files['document'][0] for api,p,files in h.api.uploads if api=='sendDocument']
            assert len(docs)==1 and docs[0].endswith('.html')
    run(t())


def test_additive_migration_preserves_legacy_rows_idempotently(tmp_path):
    legacy=tmp_path/'legacy.db'
    # Reconstruct pre-change schema by omitting ONLY the two new nullable columns.
    old_schema=db.SCHEMA.replace('    start_msg_id INTEGER,\n    pdf_allowed INTEGER','    start_msg_id INTEGER').replace('    negative_mark REAL DEFAULT 0,\n    pdf_enabled_at_start INTEGER','    negative_mark REAL DEFAULT 0')
    with sqlite3.connect(legacy) as con:
        con.executescript(old_schema)
        for table in ('group_pdf_permissions','pdf_start_requests','bot_settings'):con.execute(f'DROP TABLE {table}')
        con.execute("INSERT INTO users(user_id,first_name) VALUES(33,'Original')")
        con.execute("INSERT INTO quizzes(id,owner_id,title) VALUES('abcd',33,'Legacy')")
        con.execute("INSERT INTO attempts(id,quiz_id,user_id,chat_id,status,order_json,total) VALUES(41,'abcd',33,33,'finished','[5]',1)")
        con.execute("INSERT INTO answers(attempt_id,question_id,correct,status) VALUES(41,5,1,'correct')")
    db.set_db_path(legacy)
    db.init_db(); db.init_db()
    assert db.get_user(33)['first_name']=='Original'
    att=db.get_attempt(41)
    assert att['status']=='finished' and att['pdf_allowed'] is None
    assert len(db.get_answers(41))==1 and db.get_quiz('abcd')['title']=='Legacy'


def test_legacy_unknown_never_auto_sends_but_explicit_review_is_supported(monkeypatch):
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940130)
            qid=make_quiz(owner=u['id'],n=1)
            q=db.get_questions(qid)[0]
            aid=db.create_attempt(qid,u['id'],u['id'],[q['id']],45,False,time.time())
            db.record_answer(aid,q['id'],0,0,True,'correct',1)
            att=db.finish_attempt(aid,'finished',1)
            res=await review_pdf.auto_after_finish(h.app,att)
            assert res['skipped']=='consent-required' and not h.api.sent('sendDocument',u['id'])
            await h.text(u,'/review')
            assert h.api.sent('sendDocument',u['id'])
    run(t())


def test_consent_backup_roundtrip_redaction_and_old_backup_compatibility(tmp_path):
    qid=make_quiz(owner=940131,n=1)
    q=db.get_questions(qid)[0]
    aid=db.create_attempt(qid,940131,940131,[q['id']],45,False,time.time(),pdf_allowed=False)
    db.finish_attempt(aid,'stopped',0)
    sid=db.create_group_session(qid,GROUP,940131,[q['id']],45,False,time.time(),pending=True)
    assert db.set_group_pdf_permission(sid,940132,GROUP,True)
    request_id=db.new_pdf_start_request(qid,940131,940131)
    db.set_pdf_review_enabled(False,ADMIN)
    data=backup.build_payload()
    assert backup.validate_payload(data)==[]
    assert data['pdf_start_requests'][0]['request_id']==request_id
    target=tmp_path/'restored.db'
    result=backup.restore_payload(data,db_file=target,apply=True)
    assert result['ok'] and result['applied']
    result2=backup.restore_payload(data,db_file=target,apply=True)
    assert result2['ok']
    db.set_db_path(target)
    assert db.get_attempt(aid)['pdf_allowed']==0 and not db.pdf_review_enabled()
    assert db.group_pdf_permission(sid,940132)['allowed']==1
    assert db.get_pdf_start_request(request_id,940131,940131) is None  # Restore invalidates old cards.
    redacted=backup.github_payload(data)
    assert redacted['pdf_start_requests'][0]['request_id'] is None
    assert redacted['pdf_start_requests'][0]['user_id'] is None
    assert redacted['group_pdf_permissions'][0]['user_id'] is None
    assert redacted['bot_settings'][0]['updated_by'] is None
    assert not backup.restore_payload(redacted,db_file=target,apply=True)['ok']
    # Older JSON has no new sections or fields; absence never means permission.
    old=json.loads(json.dumps(data))
    for section in ('bot_settings','pdf_start_requests','group_pdf_permissions'):
        old.pop(section);old['counts'].pop(section)
    for att in old['attempts']:att.pop('pdf_allowed',None)
    for session in old['group_sessions']:session.pop('pdf_enabled_at_start',None)
    legacy_target=tmp_path/'old-restored.db'
    assert backup.restore_payload(old,db_file=legacy_target,apply=True)['ok']
    db.set_db_path(legacy_target)
    assert db.get_attempt(aid)['pdf_allowed'] is None


def test_cross_user_explicit_review_target_is_rejected():
    async def t():
        async with Harness(pdf_choice=None) as h:
            qid=make_quiz(owner=940133,n=1)
            aid=db.create_attempt(qid,940133,940133,[db.get_questions(qid)[0]['id']],45,False,time.time(),pdf_allowed=True)
            att=db.finish_attempt(aid,'finished',1)
            res=await review_pdf.deliver_latest_review(h.app.bot,940134,target={'kind':'attempt','attempt':att})
            assert res['error']=='wrong-user' and not h.api.sent('sendDocument')
    run(t())


def test_existing_handlers_commands_and_single_application_preserved():
    source=Path(bot.__file__).read_text()
    tree=ast.parse(source)
    names=[n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
    assert len(names)==len(set(names))
    assert source.count('InlineQueryHandler(inline_query_handler)')==1
    assert source.count('CallbackQueryHandler(callback_router)')==1
    assert source.count('app.run_polling(')==1
    registered=set()
    for call in ast.walk(tree):
        if isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='CommandHandler':
            value=ast.literal_eval(call.args[0])
            registered.update([value] if isinstance(value,str) else value)
    assert registered >= {'review','htmlreport','backup','admin','start','share','quiz','import',
                           'cancel','newquiz','done','undo','stop','skip','stats','settings',
                           'aiquiz','quizmode','section','access','broadcast','batch','authchat'}
    assert {c.command for c in bot.BOT_COMMANDS} >= {'review','htmlreport','backup','start','share','import'}
    assert all(Path(path).exists() for path in ('review_pdf.py','html_report.py','fonts/NotoSansDevanagari-Regular.ttf','assets/group_dp_watermark.jpg'))


def test_cancelled_new_lobby_is_not_mistaken_for_legacy_unknown():
    async def t():
        async with Harness(pdf_choice=None) as h:
            a,b=h.f.user(940135),h.f.user(940136)
            qid=make_quiz(owner=a['id'],n=1)
            s=await lobby(h,a,qid)
            await h.text(a,'/stop',chat_id=GROUP)
            for user in (a,b):
                res=await review_pdf.deliver_group_member(h.app.bot,s['id'],user['id'],force=True,reason='button')
                assert res['skipped']=='consent-required'
            assert not h.api.sent('sendDocument')
    run(t())


def test_existing_pdf_buttons_cannot_allow_when_admin_switches_off():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940137)
            qid=make_quiz(owner=u['id'],n=1)
            await h.text(u,f'/start quiz_{qid}')
            action=permission_actions(h,u['id'])[0]['callback_data']
            db.set_pdf_review_enabled(False,ADMIN)
            await h.cb(u,action)
            assert db.get_active_attempt(u['id'])['pdf_allowed']==0
            db.set_pdf_review_enabled(True,ADMIN)
            await complete_private(h,u,1)
            assert not h.api.sent('sendDocument',u['id'])
    run(t())


def test_group_same_user_parallel_sessions_have_independent_permissions():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940138)
            qid=make_quiz(owner=u['id'],n=1)
            a=await lobby(h,u,qid,GROUP)
            b=await lobby(h,u,qid,GROUP-1)
            await h.cb(u,f'pd:g:1:{a["id"]}',chat_id=GROUP)
            await h.cb(u,f'pd:g:0:{b["id"]}',chat_id=GROUP-1)
            await h.cb(u,f'pd:begin:{a["id"]}',chat_id=GROUP)
            await h.cb(u,f'pd:begin:{b["id"]}',chat_id=GROUP-1)
            await finish_group(h,[u],GROUP)
            await finish_group(h,[u],GROUP-1)
            await h.wait(lambda:h.api.sent('sendDocument',u['id']))
            assert len(h.api.sent('sendDocument',u['id']))==1
            assert db.review_pdf_get(session_id=a['id'],user_id=u['id'])['status']=='sent'
            assert db.review_pdf_get(session_id=b['id'],user_id=u['id']) is None
    run(t())


def test_malformed_consent_backup_rejected_without_writes(tmp_path):
    qid=make_quiz(owner=940139,n=1)
    q=db.get_questions(qid)[0]
    sid=db.create_group_session(qid,GROUP,940139,[q['id']],45,False,time.time(),pending=True)
    db.set_group_pdf_permission(sid,940139,GROUP,True)
    data=backup.build_payload()
    data['group_pdf_permissions'][0]['allowed']='yes'
    target=tmp_path/'must_stay_empty.db'
    result=backup.restore_payload(data,db_file=target,apply=True)
    assert not result['ok'] and not result['applied']
    assert not target.exists()


def test_escaped_description_limit_counts_rendered_text_not_html_entities():
    async def t():
        async with Harness(pdf_choice=None) as h:
            u=h.f.user(940140)
            qid=make_quiz(owner=u['id'],n=1)
            db.update_quiz(qid,description='&'*1000)
            await h.send(h.f.inline_query(u,f'quiz_{qid}'))
            result,=h.api.inline_answers[-1]['results']
            assert '&amp;'*1000 in result['input_message_content']['message_text']
    run(t())
