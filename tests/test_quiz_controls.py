"""Targeted control workflow tests: actual runners/callbacks/SQLite; fake Telegram and job clock.
Run: python -m unittest discover -s tests -p test_quiz_controls.py -v
"""
import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from telegram import User
from telegram.error import BadRequest, NetworkError
import backup
import bot
import config
import database as db
import group_runner as group
import keyboards as kb
import quiz_engine as engine
import quiz_modes
import quiz_runner as private

OWNER=101
OTHER=202
CHAT=-100111


class Job:
    def __init__(self,callback,data,name):
        self.callback,self.data,self.name=callback,data,name
        self.removed=False
    def schedule_removal(self):
        self.removed=True


class Jobs:
    def __init__(self):self.items=[]
    def get_jobs_by_name(self,name):return [j for j in self.items if j.name==name and not j.removed]
    def run_once(self,callback,when,data,name):
        job=Job(callback,data,name);self.items.append(job);return job


class Controls(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'quiz.sqlite3');db.init_db()
        self.admin=patch.object(config,'is_admin',lambda uid:uid==OWNER);self.admin.start()
        self.pdf=patch.object(db,'pdf_review_enabled',return_value=False);self.pdf.start()
        self.user=User(OWNER,'Owner',False,username='fixture_owner')
        self.other=User(OTHER,'Other',False,username='fixture_other')
        db.upsert_user(self.user);db.upsert_user(self.other)
        self.qid=db.new_quiz(OWNER,'Synthetic control fixture',timer=45,shuffle_options=True)
        self.questions=[db.add_question(self.qid,f'Synthetic question {i+1}',[f'A{i}',f'B{i}',f'C{i}',f'D{i}'],i%4,'Why') for i in range(12)]
        with db.connect() as con:con.execute('UPDATE quizzes SET negative_mark=0.25 WHERE id=?',(self.qid,))
        self.client=NS(send_message=AsyncMock(return_value=NS(message_id=999)),
                       send_poll=AsyncMock(side_effect=self.poll),stop_poll=AsyncMock(),send_document=AsyncMock(),
                       get_chat_member=AsyncMock(return_value=NS(status='member')))
        self.count=0;self.app=NS(bot=self.client,job_queue=Jobs())
        self.context=NS(application=self.app,bot=self.client)
        self.private_id=db.create_attempt(self.qid,OWNER,OWNER,self.questions,45,True,time.time(),start_msg_id=777,snapshot_rules=True,pdf_allowed=False)
        self.group_id=db.create_group_session(self.qid,CHAT,OWNER,self.questions,45,True,time.time(),negative_mark=0.25,pdf_enabled_at_start=False)

    def tearDown(self):
        self.admin.stop();self.pdf.stop();db.set_db_path(self.previous);self.tmp.cleanup()

    async def poll(self,**kw):
        self.count+=1
        return NS(poll=NS(id=f'poll_{self.count}'),message_id=self.count)

    def record(self,kind='p',sid=None):
        return db.get_attempt(sid or self.private_id) if kind=='p' else db.get_group_session(sid or self.group_id)

    async def send(self,kind='p',sid=None):
        if kind=='p':await private.send_current(self.app,sid or self.private_id)
        else:await group._send_current(self.app,sid or self.group_id)

    async def action(self,action,kind='p',sid=None,actor=OWNER,epoch=None,chat=None,admin=False):
        r=self.record(kind,sid)
        return await private.control_action(self.app,kind,r['id'],actor,
            r['chat_id'] if chat is None else chat,r['control_epoch'] if epoch is None else epoch,action,group_admin=admin)

    async def job(self,kind,session='p',sid=None):
        rid=sid or (self.private_id if session=='p' else self.group_id)
        name=(f'{kind}:{rid}' if session=='p' else f'g{kind}:{rid}')
        jobs=self.app.job_queue.get_jobs_by_name(name)
        self.assertEqual(len(jobs),1,name)
        job=jobs[0];job.removed=True
        await job.callback(NS(application=self.app,bot=self.client,job=job))

    async def vote(self,kind='p',user=None,chosen=None,poll=None):
        r=self.record(kind)
        pid=poll or r['cur_poll_id']
        if chosen is None:
            chosen=r['cur_correct'] if kind=='p' else db.get_group_poll(pid)['correct_display']
        update=NS(poll_answer=NS(poll_id=pid,user=user or self.user,option_ids=[chosen]))
        if kind=='p':await private.handle_poll_answer(update,self.context)
        else:await group.handle_group_answer(update,self.context)

    async def miss(self,kind='p',n=1):
        for i in range(n):
            if not self.record(kind)['cur_poll_id']:await self.send(kind)
            await self.job('timeout',kind)
            if i<n-1:
                self.assertFalse(self.record(kind)['is_paused'])
                await self.job('next',kind)

    async def test_01_normal_answers_continue_and_reset_idle_counter(self):
        await self.miss(n=3)
        self.assertEqual(self.record()['unanswered_streak'],3)
        await self.job('next');await self.vote()
        r=self.record();self.assertEqual(r['correct'],1);self.assertEqual(r['unanswered_streak'],0)
        await self.job('next');self.assertEqual(self.record()['current_index'],4)
        self.assertFalse(self.record()['is_paused'])

    async def test_02_five_timeouts_pause_before_sixth_and_resume_next_position(self):
        await self.miss(n=5)
        r=self.record();self.assertTrue(r['is_paused']);self.assertEqual(r['current_index'],5)
        self.assertEqual(r['unanswered_streak'],5);self.assertEqual(self.count,5)
        self.assertFalse(self.app.job_queue.get_jobs_by_name(f'next:{self.private_id}'))
        await self.send();self.assertEqual(self.count,5)
        self.assertEqual(await self.action('resume'),'ok')
        r=self.record();self.assertFalse(r['is_paused']);self.assertEqual(r['current_index'],5)
        self.assertEqual(r['cur_question_id'],self.questions[5]);self.assertEqual(self.count,6)
        self.assertEqual(r['skipped'],5);self.assertEqual(r['score'],0)
        self.assertEqual(r['unanswered_streak'],0)

    async def test_03_manual_pause_reopens_current_question_same_permutation_timer_score(self):
        await self.send();await self.vote();await self.job('next')
        before=self.record();old_poll=before['cur_poll_id'];old_perm=before['cur_perm']
        await self.action('pause');paused=self.record()
        self.assertTrue(paused['is_paused']);self.assertIsNone(paused['cur_poll_id'])
        self.assertEqual(paused['current_index'],1);self.assertEqual(paused['score'],1)
        self.assertGreater(paused['control_remaining'],0);self.assertLessEqual(paused['control_remaining'],45)
        await self.vote(poll=old_poll);self.assertEqual(self.record()['correct'],1)
        await self.action('resume');resumed=self.record()
        self.assertEqual(resumed['cur_question_id'],before['cur_question_id'])
        self.assertEqual(resumed['cur_perm'],old_perm);self.assertEqual(resumed['score'],1)
        self.assertNotEqual(resumed['cur_poll_id'],old_poll)

    async def test_04_restart_requires_confirmation_archives_answers_and_resets_new_run_only(self):
        await self.send();await self.vote();await self.job('next')
        self.assertNotEqual(await self.action('confirm'),'ok')
        await self.action('pause');await self.action('restart')
        self.assertEqual(self.record()['score'],1)
        old_epoch=self.record()['control_epoch'];old_answers=db.get_answers(self.private_id)
        self.assertEqual(await self.action('confirm'),'ok')
        old=self.record();new=db.get_active_attempt(OWNER)
        self.assertEqual(old['status'],'stopped');self.assertEqual(old['score'],1)
        self.assertEqual(db.get_answers(old['id']),old_answers)
        self.assertEqual(new['restart_parent'],old['id']);self.assertNotEqual(new['id'],old['id'])
        self.assertEqual(new['current_index'],0);self.assertEqual(new['cur_question_id'],self.questions[0])
        self.assertEqual(new['score'],0);self.assertEqual(new['wrong'],0);self.assertEqual(new['unanswered_streak'],0)
        for key in ('order','timer','shuffle_options','negative_mark','presentation_json','quiz_mode','start_msg_id'):
            self.assertEqual(new[key],old[key])
        self.assertEqual(await self.action('confirm',epoch=old_epoch), 'This quiz control has expired; use the newest buttons.')
        with db.connect() as con:self.assertEqual(con.execute("SELECT count(*) FROM attempts WHERE status='active'").fetchone()[0],1)

    async def test_05_concurrent_resume_and_stale_pause_buttons_do_not_duplicate_delivery(self):
        await self.send();old_epoch=self.record()['control_epoch'];await self.action('pause')
        epoch=self.record()['control_epoch']
        outcomes=await asyncio.gather(*[self.action('resume',epoch=epoch) for _ in range(15)])
        self.assertEqual(outcomes.count('ok'),1);self.assertEqual(self.count,2)
        self.assertNotEqual(await self.action('pause',epoch=old_epoch),'ok')
        self.assertFalse(self.record()['is_paused'])

    async def test_06_paused_private_and_group_survive_recovery_without_delivery_or_expiry(self):
        await self.send();await self.send('g');await self.action('pause');await self.action('pause','g')
        with db.connect() as con:
            con.execute('UPDATE attempts SET started_ts=1');con.execute('UPDATE group_sessions SET started_ts=1')
        before=self.count
        await private.recover_sessions(self.app);await group.recover_group_sessions(self.app)
        self.assertEqual(self.count,before);self.assertTrue(self.record()['is_paused']);self.assertTrue(self.record('g')['is_paused'])
        db.init_db()
        self.assertEqual(await self.action('resume'),'ok');self.assertEqual(await self.action('resume','g'),'ok')
        self.assertEqual(self.count,before+2)

    async def test_07_private_ownership_chat_and_group_sessions_are_independent(self):
        await self.send();await self.send('g')
        self.assertNotEqual(await self.action('pause',actor=OTHER),'ok')
        self.assertNotEqual(await self.action('pause',chat=OTHER),'ok')
        await self.action('pause');self.assertFalse(self.record('g')['is_paused'])
        self.assertEqual(await self.action('pause','g'),'ok')
        await self.action('resume');self.assertTrue(self.record('g')['is_paused'])

    async def test_08_group_any_response_resets_idle_and_no_votes_five_times_pauses(self):
        await self.miss('g',n=3);self.assertEqual(self.record('g')['unanswered_streak'],3)
        await self.job('next','g');await self.vote('g',self.other)
        self.assertEqual(self.record('g')['unanswered_streak'],0)
        await self.job('timeout','g');self.assertEqual(self.record('g')['unanswered_streak'],0)
        await self.job('next','g');await self.miss('g',n=5)
        r=self.record('g');self.assertTrue(r['is_paused']);self.assertEqual(r['current_index'],9)
        board=db.group_leaderboard(self.group_id);self.assertEqual(board[0]['correct'],1)
        await self.action('resume','g');self.assertEqual(db.group_leaderboard(self.group_id),board)
        self.assertEqual(self.record('g')['cur_question_id'],self.questions[9])

    async def test_09_group_pause_preserves_votes_and_resend_cannot_score_same_participant_twice(self):
        await self.send('g');await self.vote('g',self.other)
        original=db.get_group_answers(self.group_id)
        old_poll=self.record('g')['cur_poll_id']
        await self.action('pause','g');await self.action('resume','g')
        await self.vote('g',self.other)
        self.assertEqual(db.get_group_answers(self.group_id),original)
        await self.vote('g',self.user,poll=old_poll)
        self.assertEqual(db.get_group_answers(self.group_id),original)
        await self.vote('g',self.user)
        self.assertEqual(len(db.get_group_answers(self.group_id)),2)

    async def test_10_group_controller_admin_callback_auth_and_malformed_callbacks(self):
        await self.send('g')
        def update(data,user=self.other,chat=CHAT):
            return NS(callback_query=NS(data=data,answer=AsyncMock()),effective_chat=NS(id=chat,type='supergroup'),effective_user=user)
        token=f'qc:g:{self.group_id}:0:pause'
        u=update(token);await bot.callback_router(u,self.context)
        self.assertFalse(self.record('g')['is_paused'])
        self.client.get_chat_member.return_value=NS(status='administrator')
        await bot.callback_router(update(token),self.context)
        self.assertTrue(self.record('g')['is_paused'])
        await bot.callback_router(update('qc:bad'),self.context)
        self.assertTrue(self.record('g')['is_paused'])

    async def test_11_private_real_callback_pipeline_and_keyboard_buttons(self):
        await self.send()
        self.assertIsNone(self.client.send_poll.await_args.kwargs['reply_markup'])
        update=NS(effective_chat=NS(id=OWNER,type='private'),effective_user=self.user)
        await bot.controls_cmd(update,self.context)
        buttons=[b for row in self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        pause=next(b for b in buttons if b.text=='⏸ Pause Quiz')
        update=NS(callback_query=NS(data=pause.callback_data,answer=AsyncMock()),effective_chat=NS(id=OWNER,type='private'),effective_user=self.user)
        await bot.callback_router(update,self.context)
        self.assertTrue(self.record()['is_paused'])
        rows=self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.text for row in rows for b in row],['▶️ Resume Quiz','⏹ Stop Quiz'])
        update.callback_query.data=rows[0][0].callback_data
        await bot.callback_router(update,self.context)
        self.assertFalse(self.record()['is_paused']);self.assertEqual(self.count,2)

    async def test_12_stale_timeouts_next_jobs_and_poll_votes_are_ignored_after_pause(self):
        await self.send();timeout=self.app.job_queue.get_jobs_by_name(f'timeout:{self.private_id}')[0]
        poll=self.record()['cur_poll_id'];await self.action('pause')
        await timeout.callback(NS(application=self.app,job=timeout))
        await private._next_job(NS(application=self.app,job=NS(data={'attempt_id':self.private_id,'index':0,'user_id':OWNER})))
        await self.vote(poll=poll)
        self.assertEqual(self.record()['current_index'],0);self.assertEqual(self.count,1)
        self.assertEqual(await private.skip_current(self.app,OWNER),'busy')

    async def test_13_failed_stop_poll_keeps_paused_until_closure_acknowledged(self):
        await self.send();self.client.stop_poll.side_effect=NetworkError('uncertain closure')
        await self.action('pause');self.assertEqual(self.record()['pause_stop_pending'],1)
        self.assertNotEqual(await self.action('resume'),'ok');self.assertEqual(self.count,1)
        self.client.stop_poll.side_effect=None
        self.assertEqual(await self.action('resume'),'ok');self.assertEqual(self.count,2)

    async def test_14_uncertain_send_never_retries_automatically_even_after_recovery(self):
        self.client.send_poll.side_effect=NetworkError('unknown delivery')
        await self.send();r=self.record()
        self.assertTrue(r['is_paused']);self.assertEqual(r['delivery_pending'],1)
        calls=self.client.send_poll.await_count
        await private.recover_sessions(self.app)
        self.assertNotEqual(await self.action('resume'),'ok')
        self.assertEqual(self.client.send_poll.await_count,calls)

    async def test_15_crash_after_delivery_claim_is_safely_paused_on_startup(self):
        self.assertTrue(db.claim_quiz_delivery('p',self.private_id))
        self.assertTrue(db.claim_quiz_delivery('g',self.group_id))
        await private.recover_sessions(self.app);await group.recover_group_sessions(self.app)
        self.assertTrue(self.record()['is_paused']);self.assertTrue(self.record('g')['is_paused'])
        self.assertEqual(self.count,0)

    async def test_16_atomic_delivery_claim_and_duplicate_send_current(self):
        await asyncio.gather(*[self.send() for _ in range(10)])
        self.assertEqual(self.count,1)
        await asyncio.gather(*[self.send('g') for _ in range(10)])
        self.assertEqual(self.count,2)

    async def test_17_group_restart_archives_votes_and_preserves_order_and_negative_rules(self):
        await self.send('g');await self.vote('g',self.other)
        answers=db.get_group_answers(self.group_id)
        await self.action('pause','g');await self.action('restart','g');await self.action('confirm','g')
        old=self.record('g');new=db.get_active_group_session(CHAT)
        self.assertEqual(old['status'],'stopped');self.assertEqual(db.get_group_answers(old['id']),answers)
        self.assertEqual(new['current_index'],0);self.assertEqual(new['cur_question_id'],self.questions[0])
        self.assertFalse(db.get_group_answers(new['id']));self.assertEqual(new['restart_parent'],old['id'])
        for key in ('order','timer','negative_mark','shuffle_options','presentation_json'):
            self.assertEqual(new[key],old[key])
        with db.connect() as con:self.assertEqual(con.execute("SELECT count(*) FROM group_sessions WHERE status='active'").fetchone()[0],1)

    async def test_18_restart_cancel_does_not_reset_scores_or_create_rows(self):
        await self.send();await self.vote();await self.job('next');await self.action('pause')
        await self.action('restart');epoch=self.record()['control_epoch'];await self.action('cancel')
        self.assertEqual(self.record()['score'],1)
        self.assertNotEqual(await self.action('confirm',epoch=epoch),'ok')
        with db.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM attempts').fetchone()[0],1)

    async def test_19_full_backup_restore_preserves_paused_state_and_resume_revision(self):
        await self.send();await self.action('pause');before=self.record()
        payload=backup.build_payload();self.assertFalse(backup.validate_payload(payload))
        dest=Path(self.tmp.name)/'restored.sqlite3'
        result=backup.restore_payload(payload,db_file=dest,apply=True);self.assertTrue(result['ok'],result)
        db.set_db_path(dest);after=self.record()
        for key in ('is_paused','control_epoch','control_remaining','pause_perm_json','score','current_index'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(await self.action('resume'),'ok')

    async def test_20_restore_refuses_schema_that_would_lose_pause_flag(self):
        await self.action('pause');payload=backup.build_payload()
        with db.connect() as con:con.execute('ALTER TABLE attempts DROP COLUMN is_paused')
        result=backup.restore_payload(payload,apply=True)
        self.assertFalse(result['ok']);self.assertIn('attempts.is_paused',result['schema_outdated'])

    async def test_21_additive_migration_preserves_all_old_session_data(self):
        # New flags absent on an older DB: adding them never changes progress or answers.
        await self.send();await self.vote();before=self.record();answers=db.get_answers(self.private_id)
        control_cols=('is_paused','unanswered_streak','control_epoch','pause_reason','pause_poll_id','pause_message_id',
                      'pause_stop_pending','pause_perm_json','control_remaining','pause_elapsed','delivery_pending','restart_confirm','restart_parent','paused_at','paused_total')
        with db.connect() as con:
            for table in ('attempts','group_sessions'):
                for col in control_cols:con.execute(f'ALTER TABLE {table} DROP COLUMN {col}')
        db.init_db();db.init_db();after=self.record()
        for key in ('correct','wrong','score','current_index','order','timer','negative_mark'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(db.get_answers(self.private_id),answers)

    async def test_22_question_active_elapsed_is_not_lost_across_pause(self):
        await self.send('g')
        with db.connect() as con:con.execute('UPDATE group_sessions SET cur_sent_ts=? WHERE id=?',(time.time()-12,self.group_id))
        await self.action('pause','g');self.assertGreaterEqual(self.record('g')['pause_elapsed'],12)
        await self.action('resume','g');await self.vote('g',self.other)
        self.assertGreaterEqual(db.get_group_answers(self.group_id)[0]['elapsed'],12)

    async def test_23_defaults_quiz_questions_and_rules_never_changed_by_controls(self):
        original=db.get_quiz(self.qid);questions=db.get_questions(self.qid)
        await self.send();await self.action('pause');await self.action('resume');await self.action('pause')
        await self.action('restart');await self.action('confirm')
        self.assertEqual(db.get_quiz(self.qid),original);self.assertEqual(db.get_questions(self.qid),questions)

    async def test_24_already_closed_poll_allows_resume_but_other_bad_requests_do_not(self):
        await self.send();self.client.stop_poll.side_effect=BadRequest('Poll has already been closed')
        await self.action('pause');self.assertEqual(await self.action('resume'),'ok')
        self.client.stop_poll.side_effect=BadRequest('not enough rights')
        await self.action('pause');self.assertNotEqual(await self.action('resume'),'ok')

    async def test_25_old_stop_message_cannot_stop_new_restarted_run(self):
        await self.send();old_message=self.record()['cur_message_id']
        await self.action('pause');await self.action('restart');await self.action('confirm')
        active=db.get_active_attempt(OWNER)
        self.assertIsNone(await private.stop_quiz(self.app,OWNER,old_message))
        self.assertEqual(db.get_active_attempt(OWNER)['id'],active['id'])

    async def test_26_wrong_answer_resets_streak_and_negative_marking_survives_resume(self):
        await self.miss(n=2);await self.job('next')
        await self.vote(chosen=(self.record()['cur_correct']+1)%4)
        r=self.record();self.assertEqual(r['unanswered_streak'],0);self.assertEqual(r['wrong'],1)
        expected=quiz_modes.compute_net_score(r['correct'],r['wrong'],r['negative_mark'])
        self.assertEqual(expected,-0.25)
        await self.job('next');await self.action('pause');await self.action('resume')
        r=self.record();self.assertEqual(quiz_modes.compute_net_score(r['correct'],r['wrong'],r['negative_mark']),expected)
        await self.action('pause');await self.action('restart');await self.action('confirm')
        self.assertEqual(db.get_active_attempt(OWNER)['wrong'],0)
        self.assertEqual(self.record()['wrong'],1)  # historical run retained

    async def test_27_group_simultaneous_resumes_and_restart_confirmations_are_single_use(self):
        await self.send('g');await self.action('pause','g');epoch=self.record('g')['control_epoch']
        outcomes=await asyncio.gather(*[self.action('resume','g',epoch=epoch) for _ in range(12)])
        self.assertEqual(outcomes.count('ok'),1);self.assertEqual(self.count,2)
        await self.action('pause','g');await self.action('restart','g');epoch=self.record('g')['control_epoch']
        outcomes=await asyncio.gather(*[self.action('confirm','g',epoch=epoch) for _ in range(12)])
        self.assertEqual(outcomes.count('ok'),1);self.assertEqual(self.count,3)
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM group_sessions").fetchone()[0],2)
            self.assertEqual(con.execute("SELECT count(*) FROM group_sessions WHERE status='active'").fetchone()[0],1)

    async def test_28_restart_buttons_execute_real_callback_confirmation_pipeline(self):
        await self.send();await self.action('pause')
        epoch=self.record()['control_epoch']
        update=NS(callback_query=NS(data=f'qc:p:{self.private_id}:{epoch}:restart',answer=AsyncMock()),
                  effective_chat=NS(id=OWNER,type='private'),effective_user=self.user)
        await bot.callback_router(update,self.context)
        self.assertEqual(db.get_active_attempt(OWNER)['id'],self.private_id)
        buttons=[b for row in self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        confirm=next(b for b in buttons if b.text=='✅ Confirm Restart')
        update.callback_query.data=confirm.callback_data
        await bot.callback_router(update,self.context)
        self.assertNotEqual(db.get_active_attempt(OWNER)['id'],self.private_id)
        self.assertEqual(db.get_active_attempt(OWNER)['current_index'],0)

    async def test_29_paused_progress_reopens_from_sqlite_in_a_fresh_python_process(self):
        import subprocess,sys
        await self.send();await self.vote();await self.job('next');await self.action('pause')
        before=self.record()
        code=("import sys,json,database as d;d.set_db_path(sys.argv[1]);d.init_db();"
              "r=d.get_attempt(int(sys.argv[2]));print(json.dumps({k:r[k] for k in "
              "('is_paused','current_index','score','control_epoch','pause_perm_json')}))")
        result=subprocess.run([sys.executable,'-B','-c',code,str(db.get_db_path()),str(self.private_id)],
                              cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        for key,value in json.loads(result.stdout).items():self.assertEqual(value,before[key])
        await private.recover_sessions(self.app);self.assertTrue(self.record()['is_paused'])
        self.assertEqual(await self.action('resume'),'ok');self.assertEqual(self.record()['current_index'],1)

    async def test_30_multi_day_pause_does_not_expire_immediately_after_resume_and_recovery(self):
        await self.send();await self.send('g');await self.action('pause');await self.action('pause','g')
        now=time.time()
        with db.connect() as con:
            for table in ('attempts','group_sessions'):
                con.execute(f'UPDATE {table} SET started_ts=?,paused_at=?',(now-5*86400,now-5*86400+30))
        await self.action('resume');await self.action('resume','g')
        await private.recover_sessions(self.app);await group.recover_group_sessions(self.app)
        self.assertEqual(self.record()['status'],'active');self.assertEqual(self.record('g')['status'],'active')
        self.assertGreater(self.record()['paused_total'],4*86400)

    async def test_31_group_uncertain_delivery_stays_paused_and_never_retries(self):
        self.client.send_poll.side_effect=NetworkError('unknown group send')
        await self.send('g')
        self.assertTrue(self.record('g')['is_paused']);self.assertEqual(self.record('g')['delivery_pending'],1)
        calls=self.client.send_poll.await_count
        await group.recover_group_sessions(self.app)
        self.assertNotEqual(await self.action('resume','g'),'ok')
        self.assertEqual(self.client.send_poll.await_count,calls)

    async def test_32_group_wrong_answer_is_still_an_attempt_and_prevents_idle_pause(self):
        await self.miss('g',n=4);await self.job('next','g')
        info=db.get_group_poll(self.record('g')['cur_poll_id'])
        await self.vote('g',self.other,chosen=(info['correct_display']+1)%4)
        self.assertEqual(self.record('g')['unanswered_streak'],0)
        await self.job('timeout','g');self.assertFalse(self.record('g')['is_paused'])
        self.assertEqual(db.group_leaderboard(self.group_id)[0]['wrong'],1)

    async def test_33_group_and_private_ids_cannot_be_used_from_wrong_chat(self):
        await self.send();await self.send('g')
        for data,chat_type,chat_id in ((f'qc:p:{self.private_id}:0:pause','supergroup',CHAT),
                                      (f'qc:g:{self.group_id}:0:pause','private',OWNER),
                                      ('qc:p:99999999999999999999999999999:0:pause','private',OWNER)):
            u=NS(callback_query=NS(data=data,answer=AsyncMock()),effective_chat=NS(id=chat_id,type=chat_type),effective_user=self.user)
            await bot.callback_router(u,self.context)
        self.assertFalse(self.record()['is_paused']);self.assertFalse(self.record('g')['is_paused'])

    async def test_34_manual_skip_counts_unanswered_but_not_as_a_wrong_answer(self):
        for i in range(5):
            await self.send();self.assertEqual(await private.skip_current(self.app,OWNER),'ok')
        r=self.record();self.assertTrue(r['is_paused']);self.assertEqual(r['skipped'],5)
        self.assertEqual(r['wrong'],0);self.assertEqual(r['correct'],0)

    async def test_35_atomic_delivery_claim_across_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:db.claim_quiz_delivery('p',self.private_id),range(20)))
        self.assertEqual(sum(results),1)


    async def test_36_restart_does_not_reuse_historical_pdf_consent(self):
        with db.connect() as con:
            con.execute('UPDATE attempts SET pdf_allowed=1 WHERE id=?',(self.private_id,))
            con.execute('UPDATE group_sessions SET pdf_enabled_at_start=1 WHERE id=?',(self.group_id,))
        for kind in ('p','g'):
            await self.send(kind);await self.action('pause',kind);await self.action('restart',kind);await self.action('confirm',kind)
        self.assertEqual(self.record()['pdf_allowed'],1)
        self.assertEqual(db.get_active_attempt(OWNER)['pdf_allowed'],0)
        self.assertEqual(self.record('g')['pdf_enabled_at_start'],1)
        self.assertEqual(db.get_active_group_session(CHAT)['pdf_enabled_at_start'],0)

    async def test_37_restart_uses_saved_order_and_nonempty_presentation_not_edited_defaults(self):
        order=list(reversed(self.questions))
        presentation=json.dumps(engine.validate_format_settings('presentation',{'header':'Saved brand'}))
        with db.connect() as con:
            for table,sid in (('attempts',self.private_id),('group_sessions',self.group_id)):
                con.execute(f'UPDATE {table} SET order_json=?,presentation_json=? WHERE id=?',(json.dumps(order),presentation,sid))
        for kind in ('p','g'):
            await self.send(kind);await self.action('pause',kind);await self.action('restart',kind);await self.action('confirm',kind)
        for r in (db.get_active_attempt(OWNER),db.get_active_group_session(CHAT)):
            self.assertEqual(r['order'],order);self.assertEqual(r['cur_question_id'],order[0])
            self.assertEqual(r['current_index'],0);self.assertEqual(r['presentation_json'],presentation)


    async def test_38_old_start_button_does_not_create_another_run_after_confirmed_restart(self):
        await self.send();await self.action('pause');await self.action('restart');await self.action('confirm')
        new=db.get_active_attempt(OWNER);before=self.count
        self.assertEqual(new['start_msg_id'],777)
        self.assertIsNone(await private.start_quiz(self.app,self.user,OWNER,self.qid,start_msg_id=777))
        self.assertEqual(self.count,before)
        with db.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM attempts').fetchone()[0],2)
        await self.action('pause',sid=new['id'])
        self.assertIsNone(await private.start_quiz(self.app,self.user,OWNER,self.qid,start_msg_id=777))
        self.assertTrue(db.get_active_attempt(OWNER)['is_paused'])

    async def test_39_failed_pause_notice_is_recoverable_with_real_resume_buttons(self):
        await self.send();self.client.send_message.side_effect=NetworkError('notice unavailable')
        await self.action('pause');self.assertTrue(self.record()['is_paused'])
        self.client.send_message.side_effect=None
        await private.recover_sessions(self.app)
        buttons=[b for row in self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertEqual([b.text for b in buttons],['▶️ Resume Quiz','⏹ Stop Quiz'])
        update=NS(callback_query=NS(data=buttons[0].callback_data,answer=AsyncMock()),
                  effective_chat=NS(id=OWNER,type='private'),effective_user=self.user)
        await bot.callback_router(update,self.context)
        self.assertFalse(self.record()['is_paused']);self.assertEqual(self.count,2)

    async def test_40_fifth_unanswered_final_question_still_saves_a_paused_position(self):
        with db.connect() as con:
            for table in ('attempts','group_sessions'):
                con.execute(f'UPDATE {table} SET order_json=?,total=5',(json.dumps(self.questions[:5]),))
        await self.miss(n=5);await self.miss('g',n=5)
        for r in (self.record(),self.record('g')):
            self.assertTrue(r['is_paused']);self.assertEqual(r['current_index'],5)
            self.assertEqual(r['unanswered_streak'],5);self.assertEqual(r['status'],'active')
        self.assertEqual(self.count,10)


    async def test_41_resume_and_restart_do_not_bypass_existing_quiz_access_rules(self):
        await self.send();await self.action('pause')
        with patch.object(private.access_control,'check_quiz_access',return_value=(False,'denied')):
            self.assertNotEqual(await self.action('resume'),'ok')
            await self.action('restart')
            self.assertNotEqual(await self.action('confirm'),'ok')
        self.assertTrue(self.record()['is_paused']);self.assertEqual(self.count,1)
        with db.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM attempts').fetchone()[0],1)




    async def test_42_private_24_question_completion_through_real_jobs(self):
        extra=[db.add_question(self.qid,f'Long run {i}', ['A','B','C','D'],0,'Why') for i in range(12)]
        self.questions += extra
        with db.connect() as con:
            con.execute('UPDATE attempts SET order_json=?,total=24 WHERE id=?',(json.dumps(self.questions),self.private_id))
        with patch.object(private.review_pdf,'auto_after_finish',new=AsyncMock()):
            await self.send()
            for i in range(24):
                self.assertEqual(self.record()['current_index'],i)
                await self.vote();await self.job('next')
        r=self.record();self.assertEqual(r['status'],'finished');self.assertEqual(r['correct'],24)
        self.assertEqual(len(db.get_answers(self.private_id)),24);self.assertEqual(self.count,24)
        self.assertFalse(self.app.job_queue.get_jobs_by_name(f'next:{self.private_id}'))

    async def test_43_group_24_question_completion_through_real_jobs(self):
        self.questions += [db.add_question(self.qid,f'Group long run {i}',['A','B','C','D'],0,'Why') for i in range(12)]
        with db.connect() as con:
            con.execute('UPDATE group_sessions SET order_json=?,total=24 WHERE id=?',(json.dumps(self.questions),self.group_id))
        with patch.object(group.review_pdf,'auto_group_after_finish',new=AsyncMock()):
            await self.send('g')
            for i in range(24):
                self.assertEqual(self.record('g')['current_index'],i)
                await self.vote('g',self.other);await self.job('timeout','g');await self.job('next','g')
        r=self.record('g');self.assertEqual(r['status'],'finished');self.assertEqual(self.count,24)
        self.assertEqual(db.group_leaderboard(self.group_id)[0]['correct'],24)
        self.assertEqual(len(db.get_group_answers(self.group_id)),24)

    async def test_44_starter_is_not_admin_and_cannot_pause_stop_restart(self):
        await self.send();await self.send('g')
        with patch.object(config,'is_admin',return_value=False):
            for kind in ('p','g'):
                for action in ('pause','stop','restart','confirm','confirm_stop'):
                    self.assertNotEqual(await self.action(action,kind),'ok',(kind,action))
                self.assertFalse(self.record(kind)['is_paused'])
        self.client.get_chat_member.return_value=NS(status='administrator')
        self.assertEqual(await self.action('pause','g',actor=OTHER),'ok')
        # Group admin authority does not extend to a different private session.
        self.assertNotEqual(await self.action('pause',actor=OTHER,admin=True),'ok')

    async def test_45_any_current_group_participant_resumes_but_not_left_member(self):
        await self.send('g');await self.action('pause','g')
        epoch=self.record('g')['control_epoch']
        self.client.get_chat_member.return_value=NS(status='left')
        self.assertNotEqual(await self.action('resume','g',actor=OTHER),'ok')
        self.assertEqual(self.record('g')['control_epoch'],epoch)
        self.client.get_chat_member.return_value=NS(status='member')
        self.assertEqual(await self.action('resume','g',actor=OTHER),'ok')
        self.assertFalse(self.record('g')['is_paused']);self.assertEqual(self.count,2)

    async def test_46_private_resume_owner_only_even_for_configured_admin(self):
        await self.send();await self.action('pause')
        with patch.object(config,'is_admin',return_value=True):
            self.assertNotEqual(await self.action('resume',actor=OTHER),'ok')
        self.assertEqual(await self.action('resume'),'ok')

    async def test_47_stop_requires_confirmation_preserves_private_and_group_answers(self):
        for kind in ('p','g'):
            await self.send(kind);await self.vote(kind)
            if kind=='p':await self.job('next')
            r=self.record(kind)
            self.assertNotEqual(await self.action('confirm_stop',kind),'ok')
            self.assertEqual(await self.action('stop',kind),'ok')
            self.assertEqual(self.record(kind)['status'],'active');self.assertTrue(self.record(kind)['is_paused'])
            old=self.record(kind);epoch=old['control_epoch']
            with patch.object(private.review_pdf,'auto_after_finish',new=AsyncMock()),patch.object(group.review_pdf,'auto_group_after_finish',new=AsyncMock()):
                results=await asyncio.gather(*[self.action('confirm_stop',kind,epoch=epoch) for _ in range(12)])
            self.assertEqual(results.count('ok'),1)
            self.assertEqual(self.record(kind)['status'],'stopped')
            answers=db.get_answers(self.private_id) if kind=='p' else db.get_group_answers(self.group_id)
            self.assertEqual(len(answers),1)

    async def test_48_stop_confirmation_cannot_be_used_as_restart_confirmation(self):
        await self.send();await self.action('stop')
        self.assertNotEqual(await self.action('confirm'),'ok')
        self.assertEqual(self.record()['status'],'active')
        with db.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
        await self.action('cancel');self.assertEqual(self.record()['restart_confirm'],0)
        self.assertNotEqual(await self.action('confirm_stop'),'ok')

    async def test_49_nonadmin_callbacks_and_legacy_stop_command_cannot_stop(self):
        await self.send('g')
        update=NS(callback_query=NS(data=f'qc:g:{self.group_id}:0:stop',answer=AsyncMock()),
                  effective_chat=NS(id=CHAT,type='supergroup'),effective_user=self.user)
        with patch.object(config,'is_admin',return_value=False):
            await bot.callback_router(update,self.context)
            await bot.stop_cmd(update,self.context)
        self.assertEqual(self.record('g')['status'],'active');self.assertFalse(self.record('g')['is_paused'])
        self.assertIsNone(await private.stop_quiz(self.app,OWNER))
        self.assertIsNone(await group.stop_group_quiz(self.app,CHAT))

    async def test_50_racing_resume_restart_and_stop_single_revision(self):
        await self.send('g');await self.action('pause','g')
        epoch=self.record('g')['control_epoch']
        results=await asyncio.gather(self.action('resume','g',actor=OTHER,epoch=epoch),
            self.action('restart','g',epoch=epoch),self.action('stop','g',epoch=epoch))
        self.assertEqual(results.count('ok'),1)
        with db.connect() as con:self.assertEqual(con.execute("SELECT count(*) FROM group_sessions WHERE status='active'").fetchone()[0],1)
        self.assertLessEqual(self.count,2)

    async def test_51_timeout_and_network_error_in_compact_fallback_pause_both_runners(self):
        from telegram.error import TimedOut
        for kind,error in (('p',TimedOut('uncertain fallback')),('g',NetworkError('uncertain fallback'))):
            self.client.send_poll.side_effect=[BadRequest('poll too long'),error]
            await self.send(kind)
            r=self.record(kind);self.assertTrue(r['is_paused']);self.assertEqual(r['delivery_pending'],1)
            attempts=self.client.send_poll.await_count
            await self.action('resume',kind)
            await private.recover_sessions(self.app);await group.recover_group_sessions(self.app)
            self.assertEqual(self.client.send_poll.await_count,attempts)

    async def test_52_start_intro_network_error_does_not_strand_new_session(self):
        # Stop the setUp fixture without touching its history, then exercise real start.
        db.finish_attempt(self.private_id,'stopped',0)
        with db.connect() as con:con.execute("UPDATE quizzes SET pre_text='Introduction' WHERE id=?",(self.qid,))
        self.client.send_message.side_effect=NetworkError('intro unavailable')
        aid=await private.start_quiz(self.app,self.user,OWNER,self.qid,start_msg_id=909)
        self.assertIsNotNone(aid);self.assertIsNotNone(db.get_attempt(aid)['cur_poll_id'])
        db.finish_group_session(self.group_id,'stopped',time.time())
        self.assertEqual(await group.start_group_quiz(self.app,CHAT,self.qid,self.user),'ok')
        self.assertIsNotNone(db.get_active_group_session(CHAT)['cur_poll_id'])

    async def test_53_missing_parent_backup_preserves_history_without_fabricated_quiz(self):
        await self.send();await self.vote();await self.send('g');await self.vote('g',self.other)
        before_answers=db.get_answers(self.private_id);before_votes=db.get_group_answers(self.group_id)
        # Reproduce the old deployed hard-delete defect on disposable SQLite only.
        with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
        payload=backup.build_payload();self.assertEqual(payload['quizzes'],[])
        self.assertEqual(backup.validate_payload(payload),[])
        evidence=payload['history_integrity']['missing_quizzes'][0]
        self.assertEqual(evidence['quiz_id'],self.qid)
        self.assertEqual(evidence['attempts'],[self.private_id]);self.assertEqual(evidence['group_sessions'],[self.group_id])
        self.assertEqual(payload['attempts'][0]['answers'],before_answers)
        self.assertEqual(payload['group_sessions'][0]['answers'],before_votes)
        dest=Path(self.tmp.name)/'orphan-restored.sqlite3'
        restored=backup.restore_payload(payload,db_file=dest,apply=True)
        self.assertTrue(restored['ok'],restored);self.assertIn('history_integrity',restored)
        db.set_db_path(dest)
        self.assertIsNone(db.get_quiz(self.qid));self.assertEqual(db.get_answers(self.private_id),before_answers)
        self.assertEqual(db.get_group_answers(self.group_id),before_votes)
        self.assertEqual(backup.validate_payload(backup.build_payload()),[])

    async def test_54_missing_parent_inventory_cannot_silently_allow_unlisted_rows(self):
        with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
        data=backup.build_payload();self.assertFalse(backup.validate_payload(data))
        missing=copy.deepcopy(data);missing.pop('history_integrity')
        self.assertTrue(any('unknown quiz_id' in e for e in backup.validate_payload(missing)))
        wrong=copy.deepcopy(data);wrong['history_integrity']['missing_quizzes'][0]['attempts']=[]
        self.assertTrue(any('history_integrity' in e for e in backup.validate_payload(wrong)))
        mismatch=copy.deepcopy(data);mismatch['attempts'][0]['quiz_id']='different-missing-id'
        self.assertTrue(backup.validate_payload(mismatch))
        self.assertFalse(backup.restore_payload(mismatch,apply=True)['ok'])

    async def test_55_soft_delete_keeps_real_content_scores_and_active_run(self):
        await self.send();await self.vote();await self.job('next');before=self.record()
        questions=db.get_questions(self.qid);answers=db.get_answers(self.private_id)
        self.assertFalse(db.delete_quiz(self.qid,owner_id=OTHER))
        self.assertTrue(db.delete_quiz(self.qid,owner_id=OWNER))
        self.assertEqual(db.get_quiz(self.qid)['status'],'archived')
        self.assertEqual(db.get_questions(self.qid),questions);self.assertEqual(db.get_answers(self.private_id),answers)
        self.assertEqual(self.record(),before);self.assertEqual(db.get_owner_quizzes(OWNER),[])
        self.assertFalse(await private.send_ready(self.app,self.user,OWNER,self.qid))
        self.assertEqual(await group.start_group_quiz(self.app,CHAT,self.qid,self.user),'missing')
        self.assertFalse(backup.validate_payload(backup.build_payload()))
        # Archiving doesn't interrupt an existing active run.
        await self.vote();await self.job('next');self.assertEqual(self.record()['current_index'],2)

    async def test_56_database_creation_rejects_missing_or_archived_parent(self):
        for qid in ('not-a-real-quiz',self.qid):
            if qid==self.qid:db.delete_quiz(qid)
            with self.assertRaises(ValueError):db.create_attempt(qid,OTHER,OTHER,[],45,False,time.time())
            with self.assertRaises(ValueError):db.create_group_session(qid,CHAT-1,OTHER,[],45,False,time.time())

    async def test_57_orphan_export_writes_valid_full_backup_and_keeps_previous_backup(self):
        path=Path(self.tmp.name)/'quiz_backup.json'
        with patch.object(backup,'backup_path',return_value=path):
            backup.reset_state()
            good=backup.export_now(force=True);self.assertTrue(good['ok'],good)
            previous=path.read_bytes()
            with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
            result=backup.export_now(force=True)
            self.assertTrue(result['ok'],result);self.assertIn('history_integrity',result)
            data=json.loads(path.read_text());self.assertEqual(data['attempts'][0]['id'],self.private_id)
            self.assertFalse(backup.validate_payload(data))
            # Existing backup rotation remains enabled and non-empty.
            self.assertTrue(backup.prev_path().exists())
        backup.reset_state()

    async def test_58_invalid_snapshot_has_backoff_and_does_not_overwrite_good_backup(self):
        path=Path(self.tmp.name)/'quiz_backup.json'
        with patch.object(backup,'backup_path',return_value=path):
            backup.reset_state();self.assertTrue(backup.export_now(force=True)['ok']);good=path.read_bytes()
            data=backup.build_payload();data['attempts'][0]['quiz_id']='undeclared'
            with patch.object(backup,'build_payload',return_value=data) as build:
                result=backup.export_now(force=True)
                self.assertFalse(result['ok']);self.assertEqual(build.call_count,1)
                self.assertEqual(path.read_bytes(),good)
                tick=backup.run_once(now=time.time());self.assertEqual(tick['reason'],'validation-backoff')
                self.assertEqual(build.call_count,1)
        backup.reset_state()

    async def test_59_backup_reader_does_not_take_runner_mutex_and_snapshot_is_consistent(self):
        import threading
        began=threading.Event();finished=threading.Event();errors=[]
        def export():
            began.set()
            try:self.assertFalse(backup.validate_payload(backup.build_payload()))
            except Exception as e:errors.append(e)
            finally:finished.set()
        with db._lock:
            thread=threading.Thread(target=export);thread.start();self.assertTrue(began.wait(2))
            # An old db.connect()-based exporter could not finish while this lock was held.
            self.assertTrue(finished.wait(3),'Backup blocked on the runner mutex')
        thread.join(3);self.assertEqual(errors,[])
        # A writer committing between table reads cannot leak into the pinned snapshot.
        original=backup._select;changed=[]
        def select(con,table,order):
            rows=original(con,table,order)
            if table=='quizzes' and not changed:
                changed.append(True);db.new_quiz(OWNER,'Committed after snapshot')
            return rows
        with patch.object(backup,'_select',side_effect=select):data=backup.build_payload()
        self.assertEqual(len(data['quizzes']),1);self.assertEqual(len(db.get_owner_quizzes(OWNER)),2)
        self.assertFalse(backup.validate_payload(data))

    async def test_60_dirty_write_after_snapshot_is_not_lost(self):
        path=Path(self.tmp.name)/'quiz_backup.json';original=backup.build_payload
        def build():
            data=original();backup.mark_dirty('write-after-snapshot');return data
        with patch.object(backup,'backup_path',return_value=path),patch.object(backup,'build_payload',side_effect=build):
            backup.reset_state();backup.mark_dirty('before');result=backup.export_now(force=True)
            self.assertTrue(result['ok'],result);self.assertTrue(backup.pending())
        backup.reset_state()

    async def test_61_missing_original_content_pauses_without_consuming_questions(self):
        with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
        await self.send();await self.send('g')
        for kind in ('p','g'):
            self.assertTrue(self.record(kind)['is_paused']);self.assertEqual(self.record(kind)['current_index'],0)
            self.assertNotEqual(await self.action('resume',kind),'ok')
        self.assertEqual(self.count,0)

    async def test_62_legacy_valid_payload_still_restores_without_manifest(self):
        payload=backup.build_payload();self.assertNotIn('history_integrity',payload)
        dest=Path(self.tmp.name)/'legacy.sqlite3'
        result=backup.restore_payload(payload,db_file=dest,apply=True)
        self.assertTrue(result['ok'],result)
        again=backup.restore_payload(payload,db_file=dest,apply=True);self.assertTrue(again['ok'],again)
        with sqlite3.connect(dest) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT count(*) FROM questions').fetchone()[0],12)

    async def test_63_group_membership_verification_failure_denies_privilege_not_starter_exception(self):
        with patch.object(config,'is_admin',return_value=False):
            self.client.get_chat_member.side_effect=NetworkError('cannot verify')
            self.assertNotEqual(await self.action('pause','g',admin=True),'ok')
            self.assertFalse(self.record('g')['is_paused'])
        self.client.get_chat_member.side_effect=None

    async def test_64_group_pdf_lobby_can_be_stopped_with_admin_confirmation(self):
        db.finish_group_session(self.group_id,'stopped',time.time())
        sid=db.create_group_session(self.qid,CHAT,OWNER,self.questions,45,True,time.time(),pending=True)
        self.assertEqual(await self.action('stop','g',sid=sid),'ok')
        self.assertEqual(db.get_group_session(sid)['status'],'pending')
        self.assertEqual(await self.action('confirm_stop','g',sid=sid),'ok')
        self.assertEqual(db.get_group_session(sid)['status'],'stopped')

    async def test_65_separate_menu_restart_is_available_while_running_and_confirmation_is_required(self):
        await self.send()
        self.assertIsNone(self.client.send_poll.await_args.kwargs['reply_markup'])
        await self.send('g')
        self.assertIsNone(self.client.send_poll.await_args.kwargs['reply_markup'])
        update=NS(effective_chat=NS(id=OWNER,type='private'),effective_user=self.user)
        await bot.controls_cmd(update,self.context)
        buttons=[b for row in self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        restart=next(b for b in buttons if b.text=='🔄 Restart Quiz')
        update.callback_query=NS(data=restart.callback_data,answer=AsyncMock())
        await bot.callback_router(update,self.context)
        self.assertEqual(db.get_active_attempt(OWNER)['id'],self.private_id)
        self.assertTrue(self.record()['is_paused']);self.assertEqual(self.record()['restart_confirm'],1)
        self.assertEqual(self.count,2)

    async def test_66_old_start_card_cannot_silently_stop_and_replace_active_quiz(self):
        await self.send()
        second=db.new_quiz(OWNER,'Second quiz');db.add_question(second,'Q',['A','B'],0,'')
        self.assertIsNone(await private.start_quiz(self.app,self.user,OWNER,second,start_msg_id=12345))
        self.assertEqual(self.record()['status'],'active');self.assertEqual(db.get_active_attempt(OWNER)['id'],self.private_id)
        self.assertEqual(self.count,1)

    async def test_67_pdf_request_orphans_inventory_and_redacted_backup_do_not_leak_request_tokens(self):
        surviving=db.new_quiz(OWNER,'Surviving quiz')
        token=db.new_pdf_start_request(surviving,OWNER,OWNER,777)
        with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
        data=backup.build_payload();self.assertFalse(backup.validate_payload(data))
        self.assertIn('history_integrity',data)
        redacted=backup.github_payload(data)
        self.assertNotIn(token,json.dumps(redacted))
        self.assertFalse(backup.restore_payload(redacted,apply=True)['ok'])

    async def test_68_callback_is_acknowledged_before_admin_network_lookup(self):
        await self.send('g');events=[]
        async def ack(*args,**kwargs):events.append('ack')
        async def member(*args,**kwargs):events.append('lookup');return NS(status='administrator')
        self.client.get_chat_member.side_effect=member
        update=NS(callback_query=NS(data=f'qc:g:{self.group_id}:0:pause',answer=AsyncMock(side_effect=ack)),
                  effective_chat=NS(id=CHAT,type='supergroup'),effective_user=self.other)
        await bot.callback_router(update,self.context)
        self.assertEqual(events[0],'ack');self.assertIn('lookup',events);self.assertTrue(self.record('g')['is_paused'])



    async def test_69_missing_parent_recovery_pauses_existing_poll_without_advancing(self):
        await self.send();await self.send('g')
        with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
        await private.recover_sessions(self.app);await group.recover_group_sessions(self.app)
        for kind in ('p','g'):
            self.assertTrue(self.record(kind)['is_paused']);self.assertEqual(self.record(kind)['current_index'],0)
        self.assertEqual(self.count,2)

    async def test_70_recovery_uses_effective_question_timer_not_wrong_default(self):
        await self.send()
        with patch.object(private.quiz_modes,'resolve_question_timer',return_value=91) as timer:
            await private.recover_sessions(self.app)
        timer.assert_called_once()
        self.assertEqual(timer.call_args.kwargs['default_timer'],45)
        self.assertEqual(timer.call_args.args[1]['id'],self.questions[0])
        self.assertEqual(len(self.app.job_queue.get_jobs_by_name(f'timeout:{self.private_id}')),1)

    async def test_71_archived_active_attempt_can_resume_but_cannot_be_restarted_as_fresh_run(self):
        await self.send();db.delete_quiz(self.qid);await self.action('pause')
        self.assertEqual(await self.action('resume'),'ok')
        await self.action('restart');self.assertNotEqual(await self.action('confirm'),'ok')
        with db.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM attempts').fetchone()[0],1)

    async def test_72_new_quiz_button_racing_restart_cannot_create_multiple_private_sessions(self):
        await self.send();await self.action('restart');epoch=self.record()['control_epoch']
        results=await asyncio.gather(self.action('confirm',epoch=epoch),
            private.start_quiz(self.app,self.user,OWNER,self.qid,start_msg_id=9876))
        self.assertEqual(results[0],'ok');self.assertIsNone(results[1])
        with db.connect() as con:self.assertEqual(con.execute("SELECT count(*) FROM attempts WHERE status='active'").fetchone()[0],1)

    async def test_73_bootstrap_migration_remains_idempotent_with_history_integrity_payload(self):
        await self.send();await self.vote();answers=db.get_answers(self.private_id)
        db.init_db();db.init_db()
        self.assertEqual(db.get_answers(self.private_id),answers);self.assertEqual(self.record()['correct'],1)
        payload=backup.build_payload();self.assertNotIn('history_integrity',payload)
        self.assertFalse(backup.validate_payload(payload))

    async def test_74_stop_confirmation_from_other_group_is_rejected(self):
        await self.send('g');await self.action('stop','g');epoch=self.record('g')['control_epoch']
        self.assertNotEqual(await self.action('confirm_stop','g',epoch=epoch,chat=CHAT-1),'ok')
        self.assertEqual(self.record('g')['status'],'active')
        self.assertEqual(await self.action('confirm_stop','g',epoch=epoch),'ok')

    async def test_75_backup_refuses_unattached_answer_rows_instead_of_silently_dropping_them(self):
        await self.send();await self.vote()
        # Model an old externally corrupted DB, exclusively inside this disposable fixture.
        con=sqlite3.connect(db.get_db_path())
        try:
            con.execute('PRAGMA foreign_keys=OFF')
            con.execute('DELETE FROM attempts WHERE id=?',(self.private_id,));con.commit()
        finally:con.close()
        data=backup.build_payload();self.assertEqual(data['counts']['orphans'],1)
        self.assertTrue(any('unattached' in e for e in backup.validate_payload(data)))

    async def test_76_empty_or_numeric_missing_quiz_ids_are_not_reclassified_as_valid_history(self):
        with db.connect() as con:con.execute('DELETE FROM quizzes WHERE id=?',(self.qid,))
        for value in ('',123,None):
            with db.connect() as con:
                # SQLite NOT NULL disallows None at rest; exercise the JSON validator for it.
                con.execute('UPDATE attempts SET quiz_id=?',(str(value),))
            data=backup.build_payload();data['attempts'][0]['quiz_id']=value
            data['history_integrity']['missing_quizzes']=backup._missing_quiz_inventory(data)
            self.assertTrue(backup.validate_payload(data),value)



    async def test_77_legacy_unbound_stop_button_cannot_pause_or_stop_current_session(self):
        await self.send()
        update=NS(callback_query=NS(data='r:stop',answer=AsyncMock()),
                  effective_chat=NS(id=OWNER,type='private'),effective_user=self.user)
        await bot.callback_router(update,self.context)
        self.assertEqual(self.record()['status'],'active');self.assertFalse(self.record()['is_paused'])
        update.callback_query.answer.assert_awaited_once()

    async def test_78_application_registers_controls_pause_stop_and_qc_router_without_network(self):
        from telegram.ext import CommandHandler,CallbackQueryHandler
        app=bot.build_application('123456:offline-test-placeholder-not-a-real-token')
        commands={name for handlers in app.handlers.values() for handler in handlers if isinstance(handler,CommandHandler) for name in handler.commands}
        self.assertTrue({'controls','pause','stop'} <= commands)
        callbacks=[handler.callback for handlers in app.handlers.values() for handler in handlers if isinstance(handler,CallbackQueryHandler)]
        self.assertIn(bot.callback_router,callbacks)

if __name__=='__main__':unittest.main()
