"""Group result -> private HTML deep link integration; temporary SQLite, no live Telegram.
Run: python -m unittest discover -s tests -p test_group_html_delivery.py -v
"""
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, parse_qs

from telegram import User
from telegram.ext import ApplicationHandlerStop
import bot
import database as db
import group_runner as group
import html_report
import keyboards as kb
import membership
import review_pdf


class GroupHTMLDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'reports.sqlite');db.init_db()
        self.users=[User(501,'Alice',False),User(502,'Bob',False),User(503,'No answers',False)]
        for u in self.users:db.upsert_user(u)
        self.qid=db.new_quiz(501,'Saved group <title>')
        self.questions=[db.add_question(self.qid,'Saved **question** '+str(i),['A','B','C','D'],0,'Stored explanation') for i in range(2)]
        self.sid=self.session()
        self.client=NS(get_me=AsyncMock(return_value=NS(username='fixture_quiz_bot')),
                       send_message=AsyncMock(),send_document=AsyncMock(),send_poll=AsyncMock())
        self.app=NS(bot=self.client,job_queue=NS(get_jobs_by_name=lambda name:[]))
    def tearDown(self):
        db.set_db_path(self.old);self.tmp.cleanup()
    def session(self):
        sid=db.create_group_session(self.qid,-10055,501,self.questions,45,False,time.time(),negative_mark=.25)
        for idx,qid in enumerate(self.questions):
            poll=f'poll-{sid}-{idx}'
            db.set_group_current(sid,poll,42+idx,qid,idx,[0,1,2,3],0,time.time(),'')
            self.assertTrue(db.record_group_answer(sid,self.qid,-10055,poll,qid,idx,501,0,0,True,2,time.time()))
            self.assertTrue(db.record_group_answer(sid,self.qid,-10055,poll,qid,idx,502,1,1,False,3,time.time()))
            self.assertTrue(db.close_group_question(sid,poll))
        return sid
    def update(self,uid=501,private=True,payload=None):
        user=next(u for u in self.users if u.id==uid)
        chat=NS(id=uid if private else -10055,type='private' if private else 'supergroup',
                send_document=AsyncMock(),send_message=AsyncMock())
        text='/start '+(payload or f'htmlgroup_{self.sid}')
        msg=NS(text=text)
        update=NS(effective_user=user,effective_chat=chat,effective_message=msg,
                  callback_query=None,poll_answer=None,chat_member=None,my_chat_member=None,poll=None,inline_query=None)
        ctx=NS(args=[payload or f'htmlgroup_{self.sid}'],bot=self.client,application=self.app)
        return update,ctx
    def saved(self):
        # /start's existing upsert_user updates last_seen; quiz/result tables must not change.
        with db.connect() as con:
            return '\n'.join(x for x in con.iterdump() if not x.startswith('INSERT INTO "users"'))
    async def finish(self,pdf=False,status='finished'):
        with patch.object(db,'pdf_review_enabled',return_value=pdf),patch.object(review_pdf,'auto_group_after_finish',new=AsyncMock()):
            return await group._finish_locked(self.app,db.get_group_session(self.sid),status)
    async def follow(self,uid=501,private=True,payload=None):
        update,ctx=self.update(uid,private,payload)
        # Existing first-start admin notifications are unrelated to report integration.
        with patch.object(bot,'notify_first_start',new=AsyncMock()):
            await bot.start_cmd(update,ctx)
        return update,ctx
    async def test_complete_group_finish_to_private_document_uses_real_enhanced_renderer(self):
        done=await self.finish();expected=group.result_text(done,'Saved group <title>',False)
        message=self.client.send_message.await_args
        self.assertEqual(message.args,(-10055,expected))
        rows=message.kwargs['reply_markup'].inline_keyboard
        url=rows[-1][0].url
        self.assertEqual(url,f'https://t.me/fixture_quiz_bot?start=htmlgroup_{self.sid}')
        payload=parse_qs(urlparse(url).query)['start'][0]
        before=self.saved();board=db.group_leaderboard(self.sid)
        with patch.object(html_report,'_enhance_reference_report',wraps=html_report._enhance_reference_report) as enhanced,patch.object(bot.runner,'send_ready',new=AsyncMock()) as run:
            update,_=await self.follow(payload=payload)
        enhanced.assert_called_once();run.assert_not_awaited();self.client.send_poll.assert_not_awaited()
        self.assertIs(bot.html_report,html_report)
        doc=update.effective_chat.send_document.await_args.kwargs
        self.assertTrue(doc['filename'].endswith(f'_GroupReport_{self.sid}_501.html'))
        text=doc['document'].decode();self.assertIn('Saved **question** 0',text)
        self.assertIn('Stored explanation',text);self.assertIn('katex.min.js',text)
        self.assertIn('marked.min.js',text);self.assertIn('report-rich',text)
        self.assertEqual(self.saved(),before);self.assertEqual(db.group_leaderboard(self.sid),board)
        self.client.send_document.assert_not_awaited()  # no participant HTML posted to group
    async def test_delivered_attachment_opens_in_real_browser_with_working_navigation(self):
        # Test-only dependency: pip install playwright; python -m playwright install chromium
        from playwright.async_api import async_playwright
        await self.finish();update,_=await self.follow()
        document=update.effective_chat.send_document.await_args.kwargs['document']
        attachment=Path(self.tmp.name)/'delivered.html';attachment.write_bytes(document)
        async with async_playwright() as play:
            browser=await play.chromium.launch(args=['--no-sandbox'])
            try:
                page=await browser.new_page(viewport={'width':390,'height':844})
                # Downloaded HTML must remain usable with unavailable CDN assets.
                await page.route('https://**/*',lambda route:route.abort())
                await page.goto(attachment.as_uri(),wait_until='load')
                self.assertEqual(await page.locator('.nav-btn').count(),2)
                score=await page.locator('.score-grid').inner_text()
                await page.locator('.nav-btn').nth(1).click()
                self.assertTrue(await page.locator('#qcard-2').is_visible())
                self.assertEqual(await page.locator('.score-grid').inner_text(),score)
                self.assertEqual(await page.locator('.q-options button,input[type=radio]').count(),0)
            finally:
                await browser.close()

    async def test_result_message_text_snapshot_unchanged(self):
        done=await self.finish()
        expected=('🏆 <b>Quiz Result</b>\n\n📚 Saved group &lt;title&gt;\n'
                  '📝 प्रश्न: 2/2 · 👥 Participants: 2\n➖ Negative Marking: -1/4 प्रति गलत उत्तर\n\n'
                  '🥇 Alice — Net 2/2\n      ✅ Correct 2 · ❌ Wrong 0 · ⌛ Skipped 0 · Score 2/2 · Net 2/2 · Percentage 100.00%\n'
                  '🥈 Bob — Net -0.50/2\n      ✅ Correct 0 · ❌ Wrong 2 · ⌛ Skipped 0 · Score 0/2 · Net -0.50/2 · Percentage 0.00%')
        self.assertEqual(self.client.send_message.await_args.args[1],expected)
    async def test_pdf_button_preserved_html_also_available(self):
        await self.finish(pdf=True)
        rows=self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(rows[:-1],kb.group_review_markup(self.sid).inline_keyboard)
        self.assertIn('htmlgroup_',rows[-1][0].url)
    async def test_html_button_independent_of_pdf_toggle(self):
        await self.finish(pdf=False)
        rows=self.client.send_message.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(len(rows),1);self.assertIsNotNone(rows[0][0].url)
    async def test_multiple_chunks_only_last_gets_markup(self):
        with patch.object(group.engine,'split_message',return_value=['unchanged first','unchanged last']):await self.finish()
        calls=self.client.send_message.await_args_list
        self.assertEqual([c.args[1] for c in calls],['unchanged first','unchanged last'])
        self.assertIsNone(calls[0].kwargs['reply_markup']);self.assertIsNotNone(calls[1].kwargs['reply_markup'])
    async def test_username_failure_preserves_result_and_pdf(self):
        self.client.get_me.side_effect=RuntimeError('unavailable')
        done=await self.finish(pdf=True)
        self.assertEqual(self.client.send_message.await_args.args[1],group.result_text(done,'Saved group <title>',False))
        self.assertEqual(self.client.send_message.await_args.kwargs['reply_markup'],kb.group_review_markup(self.sid))
    async def test_each_user_receives_only_own_answers(self):
        await self.finish()
        with patch.object(html_report,'build_html_report_for_group',wraps=html_report.build_html_report_for_group) as render:
            alice,_=await self.follow(501);bob_update,_=await self.follow(502)
        self.assertEqual([c.args[1] for c in render.call_args_list],[501,502])
        self.assertNotEqual(alice.effective_chat.send_document.await_args.kwargs['document'],bob_update.effective_chat.send_document.await_args.kwargs['document'])
        self.assertTrue(bob_update.effective_chat.send_document.await_args.kwargs['filename'].endswith('_502.html'))
    async def test_nonparticipant_denied_without_renderer_or_latest_fallback(self):
        await self.finish()
        with patch.object(html_report,'build_html_report_for_group') as render,patch.object(html_report,'build_latest_html_report') as latest:
            update,_=await self.follow(503)
        render.assert_not_called();latest.assert_not_called();update.effective_chat.send_document.assert_not_awaited()
    async def test_active_session_denied(self):
        with patch.object(html_report,'build_html_report_for_group') as render:
            update,_=await self.follow()
        render.assert_not_called();update.effective_chat.send_document.assert_not_awaited()
    async def test_invalid_and_missing_sessions_denied(self):
        for suffix in ('','abc','-1','0','９','9223372036854775808','9'*80,'999999'):
            with self.subTest(suffix=suffix):
                update,_=await self.follow(payload='htmlgroup_'+suffix)
                update.effective_chat.send_document.assert_not_awaited();update.effective_chat.send_message.assert_awaited_once()
    async def test_group_use_never_delivers_report(self):
        await self.finish()
        with patch.object(html_report,'build_html_report_for_group') as render:
            update,_=await self.follow(private=False)
        render.assert_not_called();update.effective_chat.send_document.assert_not_awaited()
    async def test_exact_session_used_even_if_newer_session_exists(self):
        await self.finish();other=self.session();db.finish_group_session(other,'finished',time.time())
        with patch.object(html_report,'build_html_report_for_group',wraps=html_report.build_html_report_for_group) as render:
            await self.follow()
        self.assertEqual(render.call_args.args[0]['id'],self.sid)
    async def test_report_failure_does_not_change_saved_result(self):
        await self.finish();before=self.saved()
        with patch.object(html_report,'build_html_report_for_group',side_effect=RuntimeError('failure')):
            update,_=await self.follow()
        self.assertEqual(before,self.saved());update.effective_chat.send_document.assert_not_awaited()
        update.effective_chat.send_message.assert_awaited_once()
    async def test_delivery_failure_safe_to_retry(self):
        await self.finish();update,ctx=self.update();update.effective_chat.send_document.side_effect=RuntimeError('failed')
        before=self.saved()
        with patch.object(bot,'notify_first_start',new=AsyncMock()):await bot.start_cmd(update,ctx)
        self.assertEqual(before,self.saved());update.effective_chat.send_document.side_effect=None
        with patch.object(bot,'notify_first_start',new=AsyncMock()):await bot.start_cmd(update,ctx)
        self.assertEqual(update.effective_chat.send_document.await_count,2);self.assertEqual(before,self.saved())
    async def test_stopped_saved_session_report_available(self):
        await self.finish(status='stopped');update,_=await self.follow()
        update.effective_chat.send_document.assert_awaited_once()
    async def test_existing_membership_gate_still_blocks_private_link(self):
        await self.finish();update,ctx=self.update()
        with patch.object(membership,'enabled',return_value=True),patch.object(membership,'has_access',new=AsyncMock(return_value=False)),patch.object(bot,'notify_first_start',new=AsyncMock()):
            with self.assertRaises(ApplicationHandlerStop):await bot.membership_gate(update,ctx)
        update.effective_chat.send_document.assert_not_awaited()
    async def test_existing_private_htmlreport_command_still_delivers_group_report(self):
        await self.finish();update,ctx=self.update();ctx.args=[]
        before=self.saved();await bot.htmlreport_cmd(update,ctx)
        update.effective_chat.send_document.assert_awaited_once();self.assertEqual(before,self.saved())
    async def test_normal_start_still_opens_menu(self):
        update,ctx=self.update();ctx.args=[]
        with patch.object(bot,'notify_first_start',new=AsyncMock()),patch.object(bot,'show_main_menu',new=AsyncMock()) as menu:
            await bot.start_cmd(update,ctx)
        menu.assert_awaited_once();update.effective_chat.send_document.assert_not_awaited()


if __name__=='__main__':unittest.main()
