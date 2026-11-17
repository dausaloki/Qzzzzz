"""Read-only reference HTML adaptation tests.
Production dependency: none added. Browser tests require test-only Playwright + Chromium.
The actual CDN versions used by the reference are fetched once to a test cache,
then fulfilled locally into Chromium; tests do not contact Telegram.
Run from project root: python -m unittest discover -s tests -p test_reference_html_report.py -v
"""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
import urllib.request

import config
import database as db
import html_report as report
import quiz_runner
from playwright.sync_api import sync_playwright


def sample_report():
    return {'quiz_id':'','quiz_title':'Saved result हिंदी','participant_name':'Existing participant',
            'mode_label':'Private Quiz','total':4,'correct':1,'wrong':1,'skipped':1,'unattempted':1,
            'accuracy':25,'time_taken_str':'42s','completed_at_str':'Saved date', 'negative_mark':0.25,
            'items':[
                {'number':1,'question':'**Bold** and $\\frac{1}{2}$\n\n| A | B |\n|---|---|\n| 1 | 2 |',
                 'options':['**one**','two','three','four'],'chosen_index':0,'correct_index':0,
                 'status':'correct','explanation':'*Explanation* $$x^2$$','section':''},
                {'number':2,'question':'Second question','options':['A','B','C','D'],
                 'chosen_index':1,'correct_index':0,'status':'wrong','explanation':'Saved answer','section':''},
                {'number':3,'question':'Timeout question','options':['A','B'],
                 'chosen_index':None,'correct_index':0,'status':'timeout','section':''},
                {'number':4,'question':'Unattempted question','options':['A','B'],
                 'chosen_index':None,'correct_index':0,'status':'unattempted','section':''}]}


class SavedDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_path=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'saved.sqlite3');db.init_db()
        self.qid=db.new_quiz(501,'Existing quiz **title**')
        self.questions=[db.add_question(self.qid,'Saved **question** '+str(i),['A','B','C','D'],0,'Saved explanation') for i in range(4)]
        self.aid=db.create_attempt(self.qid,501,501,self.questions,45,False,time.time(),snapshot_rules=True)
        db.record_answer(self.aid,self.questions[0],0,0,True,'correct',2)
        db.record_answer(self.aid,self.questions[1],1,1,False,'wrong',3)
        db.record_answer(self.aid,self.questions[2],2,None,False,'timeout',4)
        db.finish_attempt(self.aid,'finished',9)
    def tearDown(self):
        db.set_db_path(self.old_path);self.tmp.cleanup()
    def dump(self):
        with db.connect() as con:return '\n'.join(con.iterdump())
    def test_private_report_reads_existing_history_without_any_db_write(self):
        before=self.dump();answers=db.get_answers(self.aid)
        content,name,data=report.build_html_report_for_attempt(self.aid)
        self.assertEqual(before,self.dump());self.assertEqual(answers,db.get_answers(self.aid))
        self.assertEqual(data['correct'],1);self.assertEqual(data['wrong'],1)
        self.assertEqual(data['skipped'],1);self.assertEqual(data['unattempted'],1)
        self.assertTrue(name.endswith('.html'));self.assertIn(b'data-report-source',content)
    def test_group_report_preserves_votes_scoreboard_and_history(self):
        sid=db.create_group_session(self.qid,-10055,501,self.questions,45,False,time.time(),negative_mark=.25)
        db.set_group_current(sid,'saved-group-poll',42,self.questions[0],0,[0,1,2,3],0,time.time(),'')
        db.record_group_answer(sid,self.qid,-10055,'saved-group-poll',self.questions[0],0,501,0,0,True,2,time.time())
        db.finish_group_session(sid,'finished',time.time())
        before=self.dump();board=db.group_leaderboard(sid)
        content,name,data=report.build_html_report_for_group(sid,501)
        self.assertEqual(before,self.dump());self.assertEqual(board,db.group_leaderboard(sid))
        self.assertEqual(data['correct'],1);self.assertIn('_GroupReport_',name)
    def test_latest_report_api_preserves_existing_delivery_tuple(self):
        before=self.dump();built=report.build_latest_html_report(501,self.qid)
        self.assertEqual(len(built),3);self.assertIsInstance(built[0],bytes)
        self.assertEqual(self.dump(),before)
    def test_optional_enhancement_failure_falls_back_to_existing_report(self):
        with patch.object(report,'_enhance_reference_report',side_effect=ValueError('injected enhancement failure')):
            content=report.build_html_from_report_data(sample_report()).decode()
        self.assertIn('score-grid',content);self.assertIn('qcard-4',content)
        self.assertIn('0.75 / 4',content)
    def test_generation_is_read_only_and_does_not_shuffle_or_recalculate_in_browser(self):
        data=sample_report();before=copy.deepcopy(data)
        content=report.build_html_from_report_data(data).decode()
        self.assertEqual(data,before);self.assertIn('0.75 / 4',content)
        self.assertNotIn('function sbq(',content);self.assertNotIn('setInterval(',content)
        self.assertNotIn('modeSelection',content);self.assertNotIn('location.reload',content)
        self.assertIn('marked/9.1.6',content);self.assertIn('KaTeX/0.16.9',content)
    def test_existing_text_is_escaped_in_attribute_and_fallback(self):
        raw='" onclick="alert(1)"><script>alert(2)</script>'
        rendered=report.render_math_and_text(raw)
        self.assertNotIn('<script>',rendered)
        self.assertIn('&lt;script&gt;',rendered)
    def test_existing_htmlreport_command_delivers_through_unchanged_handler(self):
        import bot
        chat=NS(id=501,type='private',send_document=AsyncMock(),send_message=AsyncMock())
        update=NS(effective_chat=chat,effective_user=NS(id=501))
        before=self.dump()
        asyncio.run(bot.htmlreport_cmd(update,NS(args=[])))
        chat.send_document.assert_awaited_once()
        sent=chat.send_document.await_args.kwargs
        self.assertTrue(sent['filename'].endswith('.html'))
        self.assertIn(b'data-report-source',sent['document'])
        self.assertIn('Interactive HTML Result Report',sent['caption'])
        self.assertEqual(self.dump(),before)

    def test_auto_report_failure_cannot_prevent_normal_result(self):
        db.finish_attempt(self.aid,'stopped',9)
        aid=db.create_attempt(self.qid,502,502,self.questions,45,False,time.time(),snapshot_rules=True)
        app=NS(bot=NS(send_message=AsyncMock(),send_document=AsyncMock()),
               job_queue=NS(get_jobs_by_name=lambda name:[]))
        async def execute():
            with patch.object(config,'AUTO_HTML_REPORT',True),patch.object(quiz_runner.review_pdf,'auto_after_finish',new=AsyncMock()),patch.object(report,'build_html_report_for_attempt',side_effect=ValueError('injected report failure')):
                return await quiz_runner._finish_locked(app,db.get_attempt(aid),'finished')
        result=asyncio.run(execute())
        self.assertEqual(result['status'],'finished');app.bot.send_message.assert_awaited()
        app.bot.send_document.assert_not_awaited()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets=Path(os.environ.get('QZZZZZ_HTML_TEST_ASSETS','/home/user/html_reference_assets'))
        cls.assets.mkdir(parents=True,exist_ok=True)
        urls={'marked.min.js':'https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js',
              'katex.min.js':'https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js',
              'katex.min.css':'https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css'}
        for name,url in urls.items():
            if not (cls.assets/name).exists():
                (cls.assets/name).write_bytes(urllib.request.urlopen(url,timeout=30).read())
        cls.play=sync_playwright().start();cls.browser=cls.play.chromium.launch(args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.play.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844})
        self.page=self.context.new_page();self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
    def tearDown(self):
        self.context.close()
    def load(self,data=None,blocked=False):
        def serve(route):
            filename=route.request.url.split('/')[-1]
            f=self.assets/filename
            if not blocked and f.is_file():route.fulfill(body=f.read_bytes(),content_type='text/css' if filename.endswith('css') else 'application/javascript')
            else:route.abort()
        self.page.route('**/*',serve)
        content=report.build_html_from_report_data(data or sample_report()).decode()
        self.page.set_content(content,wait_until='load');return content
    def test_real_marked_and_katex_render_question_options_and_explanations(self):
        self.load()
        self.assertEqual(self.page.locator('#qcard-1 .q-stem strong').inner_text(),'Bold')
        self.assertEqual(self.page.locator('#qcard-1 .q-stem table tbody tr').count(),1)
        self.assertEqual(self.page.locator('#qcard-1 .opt-text strong').inner_text(),'one')
        self.assertEqual(self.page.locator('#qcard-1 .q-expl em').inner_text(),'Explanation')
        self.assertGreater(self.page.locator('#qcard-1 .katex').count(),1)
        self.assertEqual(self.errors,[])
    def test_navigator_keeps_saved_scores_and_answers_read_only(self):
        self.load();before=self.page.locator('.score-grid').inner_text()
        self.page.locator('.nav-btn').nth(3).click()
        self.assertTrue(self.page.locator('#qcard-4').is_visible())
        self.assertEqual(self.page.locator('.nav-btn').count(),4)
        self.assertEqual(self.page.locator('.q-options button,input[type=radio]').count(),0)
        self.assertEqual(self.page.locator('.score-grid').inner_text(),before)
        self.assertEqual(self.page.locator('#qcard-1 .tag-ok').count(),1)
        self.assertEqual(self.page.locator('#qcard-2 .tag-bad').count(),1)
        self.assertEqual(self.errors,[])
    def test_theme_toggle_and_reference_persistence(self):
        # A real origin is needed for localStorage; downloaded file support varies by browser.
        content=report.build_html_from_report_data(sample_report()).decode()
        def serve(route):
            if route.request.url=='https://report.test/':route.fulfill(body=content,content_type='text/html')
            else:
                f=self.assets/route.request.url.split('/')[-1]
                if f.is_file():route.fulfill(body=f.read_bytes(),content_type='text/css' if f.suffix=='.css' else 'application/javascript')
                else:route.abort()
        self.page.route('**/*',serve);self.page.goto('https://report.test/',wait_until='load')
        self.page.locator('[onclick="toggleTheme()"]').click()
        self.assertEqual(self.page.locator('html').get_attribute('data-theme'),'dark')
        self.assertEqual(self.page.evaluate('localStorage.getItem("qt")'),'dark')
        self.page.reload(wait_until='load')
        self.assertEqual(self.page.locator('html').get_attribute('data-theme'),'dark')
        self.page.locator('[onclick="toggleTheme()"]').click()
        self.assertEqual(self.page.locator('html').get_attribute('data-theme'),'light')
        self.assertEqual(self.errors,[])
    def test_blocked_cdn_keeps_original_readable_math_navigator_and_theme(self):
        self.load(blocked=True)
        self.assertEqual(self.page.locator('[data-reference-rendered]').count(),0)
        self.assertEqual(self.page.locator('.math-frac').count(),1)
        self.page.locator('.nav-btn').nth(2).click();self.assertTrue(self.page.locator('#qcard-3').is_visible())
        self.page.locator('[onclick="toggleTheme()"]').click()
        self.assertEqual(self.page.locator('html').get_attribute('data-theme'),'dark')
        self.assertEqual(self.errors,[])
    def test_imported_markdown_cannot_inject_scripts_or_event_handlers(self):
        data=sample_report();data['items'][0]['question']='**Safe** <img src=x onerror="window.pwned=1"><script>window.pwned=2</script> [bad](javascript:alert(1))'
        self.load(data)
        self.assertIsNone(self.page.evaluate('window.pwned'))
        self.assertEqual(self.page.locator('.q-stem script,.q-stem img,.q-stem [onerror],.q-stem [onclick]').count(),0)
        self.assertEqual(self.page.locator('.q-stem a[href^="javascript:"]').count(),0)
        self.assertEqual(self.page.locator('#qcard-1 .q-stem strong').inner_text(),'Safe')
        self.assertEqual(self.errors,[])
    def test_original_filter_and_search_interfaces_remain_functional(self):
        self.load()
        self.page.locator('.filter-btn').filter(has_text='Wrong').click()
        self.assertTrue(self.page.locator('#qcard-2').is_visible())
        self.assertFalse(self.page.locator('#qcard-1').is_visible())
        self.page.locator('.filter-btn').filter(has_text='All').click()
        self.page.locator('#q-search').fill('Timeout')
        self.assertTrue(self.page.locator('#qcard-3').is_visible())
        self.assertFalse(self.page.locator('#qcard-2').is_visible())
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
