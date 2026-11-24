"""Rollback regressions: no aggregate HTML leaderboard, original result/PDF preserved.
Replaces the previous feature-specific leaderboard tests. No live Telegram.
Run: python -m unittest discover -s tests -p test_html_leaderboard.py -v
"""
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from telegram import User
import bot
import config
import database as db
import group_runner as group
import html_report
import review_pdf


class LeaderboardRollbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'rollback.sqlite');db.init_db()
        self.user=User(701,'Rollback हिंदी',False);db.upsert_user(self.user)
        self.qid=db.new_quiz(701,'Existing quiz')
        self.question=db.add_question(self.qid,'Existing question',['Correct','Wrong'],0,'Existing explanation')
        self.client=NS(get_me=AsyncMock(return_value=NS(username='offline_quiz_bot')),
                       send_message=AsyncMock(),send_document=AsyncMock(return_value=NS(message_id=99)))
        self.app=NS(bot=self.client,job_queue=NS(get_jobs_by_name=lambda name:[]))
    def tearDown(self):db.set_db_path(self.previous);self.tmp.cleanup()
    def session(self,consent=False):
        sid=db.create_group_session(self.qid,-10055,701,[self.question],45,False,time.time(),negative_mark=.25,pending=True)
        if consent:self.assertTrue(db.set_group_pdf_permission(sid,701,-10055,True))
        self.assertTrue(db.activate_group_session(sid,True))
        db.set_group_current(sid,'rollback-poll',10,self.question,0,[0,1],0,time.time(),'')
        self.assertTrue(db.record_group_answer(sid,self.qid,-10055,'rollback-poll',self.question,0,701,0,0,True,1.25,time.time()))
        self.assertTrue(db.close_group_question(sid,'rollback-poll'))
        return db.get_group_session(sid)
    def test_new_renderer_and_aggregate_adapter_are_absent_but_earlier_reports_remain(self):
        self.assertFalse(hasattr(group,'_group_leaderboard_attachment'))
        self.assertFalse(hasattr(html_report,'build_group_leaderboard_html'))
        self.assertTrue(callable(html_report.build_html_report_for_group))
        self.assertTrue(callable(bot.htmlreport_cmd));self.assertTrue(callable(bot.group_html_report_link))
    async def test_finished_and_stopped_results_have_no_automatic_html_attachment(self):
        for status in ('finished','stopped'):
            session=self.session();before_rows=db.get_questions(self.qid)
            self.client.send_message.reset_mock();self.client.send_document.reset_mock()
            with patch.object(db,'pdf_review_enabled',return_value=False),patch.object(review_pdf,'auto_group_after_finish',new=AsyncMock()) as pdf:
                done=await group._finish_locked(self.app,session,status)
            self.assertEqual(self.client.send_message.await_args.args[1],group.result_text(done,'Existing quiz',status=='stopped'))
            self.client.send_document.assert_not_awaited();pdf.assert_awaited_once()
            self.assertEqual(db.get_questions(self.qid),before_rows)
            self.assertEqual(db.group_leaderboard(done['id'])[0]['correct'],1)
    async def test_pdf_button_and_prior_private_html_link_are_preserved_without_new_button(self):
        session=self.session()
        with patch.object(db,'pdf_review_enabled',return_value=True),patch.object(review_pdf,'auto_group_after_finish',new=AsyncMock()):
            await group._finish_locked(self.app,session,'finished')
        markup=self.client.send_message.await_args.kwargs['reply_markup']
        self.assertEqual(len(markup.inline_keyboard),2)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,f"p:g:{session['id']}")
        self.assertIn('htmlgroup_',markup.inline_keyboard[1][0].url)
        self.client.send_document.assert_not_awaited()
    async def test_existing_real_review_pdf_render_and_automatic_private_delivery_still_work(self):
        session=self.session(consent=True)
        with patch.object(db,'pdf_review_enabled',return_value=True),patch.object(config,'REVIEW_PDF_GROUP_AUTO',True):
            done=await group._finish_locked(self.app,session,'finished')
        self.client.send_document.assert_awaited_once()
        doc=self.client.send_document.await_args.kwargs
        self.assertEqual(doc['chat_id'],701)  # personalized PDF is private, not posted to group
        content=doc['document'].input_file_content
        self.assertTrue(content.startswith(b'%PDF-'))
        self.assertTrue(doc['document'].filename.endswith('.pdf'))
        record=db.review_pdf_get(session_id=done['id'],user_id=701)
        self.assertEqual(record['status'],'sent');self.assertGreater(record['pages'],0)
        self.assertEqual(db.get_questions(self.qid)[0]['question'],'Existing question')
    async def test_pdf_consent_restrictions_remain_enforced(self):
        session=self.session(consent=False)
        with patch.object(db,'pdf_review_enabled',return_value=True),patch.object(config,'REVIEW_PDF_GROUP_AUTO',True):
            done=await group._finish_locked(self.app,session,'finished')
        self.client.send_document.assert_not_awaited()
        with patch.object(db,'pdf_review_enabled',return_value=True):
            response=await review_pdf.deliver_group_member(self.client,done['id'],701,force=True,reason='button')
        self.assertEqual(response['skipped'],'consent-required');self.client.send_document.assert_not_awaited()
    async def test_existing_pdf_button_callback_can_still_deliver_review_pdf(self):
        session=self.session(consent=True)
        with patch.object(db,'pdf_review_enabled',return_value=True),patch.object(config,'REVIEW_PDF_GROUP_AUTO',False):
            done=await group._finish_locked(self.app,session,'finished')
        self.client.send_document.assert_not_awaited()
        update=NS(effective_user=self.user,effective_chat=NS(id=-10055,type='supergroup'),
                  callback_query=NS(data=f"p:g:{done['id']}",answer=AsyncMock()))
        with patch.object(db,'pdf_review_enabled',return_value=True):
            await bot.callback_router(update,NS(bot=self.client,application=self.app))
        self.client.send_document.assert_awaited_once()
        doc=self.client.send_document.await_args.kwargs
        self.assertEqual(doc['chat_id'],701);self.assertTrue(doc['document'].filename.endswith('.pdf'))
    async def test_repeat_finish_does_not_duplicate_result_or_pdf_hook(self):
        session=self.session()
        with patch.object(db,'pdf_review_enabled',return_value=False),patch.object(review_pdf,'auto_group_after_finish',new=AsyncMock()) as pdf:
            done=await group._finish_locked(self.app,session,'finished')
            self.assertIsNone(await group._finish_locked(self.app,done,'finished'))
        self.client.send_message.assert_awaited_once();self.client.send_document.assert_not_awaited();pdf.assert_awaited_once()


if __name__=='__main__':unittest.main()
