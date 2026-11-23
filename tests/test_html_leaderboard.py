"""Automatic group HTML leaderboard; temporary SQLite, fake Telegram, real Chromium.
Run: python -m unittest discover -s tests -p test_html_leaderboard.py -v
Browser tests need test-only playwright + Chromium; production adds no dependency.
"""
import copy
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from telegram import User
from telegram.error import Forbidden,TimedOut
import database as db
import group_runner as group
import html_report
import review_pdf


class Table(HTMLParser):
    def __init__(self,data):
        super().__init__();self.rows=[];self.heads=[];self.row=None;self.cell=None;self.header=False;self.feed(data)
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        if tag in ('td','th'):self.cell='';self.header=tag=='th'
    def handle_data(self,data):
        if self.cell is not None:self.cell+=data
    def handle_endtag(self,tag):
        if tag in ('td','th'):
            if self.header:self.heads.append(self.cell)
            elif self.row is not None:self.row.append(self.cell)
            self.cell=None
        if tag=='tr' and self.row:
            self.rows.append(self.row);self.row=None


def fixture_rows(n):
    return [dict(rank=i,name=('Participant '+str(i)+' हिंदी 👩🏽‍⚕️'),correct=3,wrong=1,score='2.75/5',elapsed=12.345+i/100,percentage='60.00%') for i in range(1,n+1)]


class RendererTests(unittest.TestCase):
    def test_columns_medals_and_numeric_ranks(self):
        table=Table(html_report.build_group_leaderboard_html('Quiz',fixture_rows(6),asked=5,total=5).decode())
        self.assertEqual(table.heads,['#','Name','✅','❌','Score','Time','%'])
        self.assertEqual([r[0] for r in table.rows],['🥇','🥈','🥉','4','5','6'])
        self.assertEqual(table.rows[0][2:],['3','1','2.75/5','12.36s','60.00%'])
    def test_all_participants_present_without_mutating_or_sorting_input(self):
        rows=fixture_rows(1001);original=copy.deepcopy(rows)
        table=Table(html_report.build_group_leaderboard_html('Quiz',rows,asked=5,total=5).decode())
        self.assertEqual(len(table.rows),1001);self.assertEqual(rows,original)
        self.assertEqual([r[1] for r in table.rows],[r['name'] for r in rows])
        self.assertEqual(table.rows[-1][0],'1001')
    def test_long_names_emojis_and_markup_are_preserved_safely(self):
        rows=fixture_rows(1);name='Long 👩🏽‍⚕️ नाम '+('अ'*1000)+' <script>window.injected=1</script> & "quotes"'
        rows[0]['name']=name
        content=html_report.build_group_leaderboard_html('<img src=x onerror=alert(1)>',rows,asked=1,total=1).decode()
        self.assertEqual(Table(content).rows[0][1],name)
        self.assertNotIn('<script>window.injected',content);self.assertNotIn('<img src=x',content)
    def test_zero_rows_and_zero_questions(self):
        content=html_report.build_group_leaderboard_html('Empty',[],asked=0,total=0).decode()
        self.assertIn('No participants',content);self.assertIn('Page 1',content);self.assertIn('Participants: 0',content)
    def test_exact_page_boundaries_and_no_omitted_rows(self):
        for n,pages in ((0,1),(1,1),(50,1),(51,2),(100,2),(101,3)):
            with self.subTest(n=n):
                content=html_report.build_group_leaderboard_html('Quiz',fixture_rows(n),asked=5,total=5).decode()
                self.assertEqual(content.count('<section class="leaderboard-page"'),pages)
                if n:self.assertEqual(len(Table(content).rows),n)
    def test_invalid_page_size_rejected_not_silently_truncated(self):
        for size in (0,-1,True,2.5):
            with self.assertRaises(ValueError):html_report.build_group_leaderboard_html('Quiz',fixture_rows(3),asked=5,total=5,page_size=size)
    def test_rows_are_not_resorted_by_renderer(self):
        rows=list(reversed(fixture_rows(4)))
        table=Table(html_report.build_group_leaderboard_html('Quiz',rows,asked=5,total=5).decode())
        self.assertEqual([r[0] for r in table.rows],['4','🥉','🥈','🥇'])
    def test_public_report_contains_no_question_answers_or_private_fields(self):
        rows=fixture_rows(1);rows[0]['user_id']='PRIVATE_ID';rows[0]['answers']='PRIVATE_ANSWERS'
        content=html_report.build_group_leaderboard_html('Quiz',rows,asked=5,total=5).decode()
        self.assertNotIn('PRIVATE_ID',content);self.assertNotIn('PRIVATE_ANSWERS',content)
        self.assertNotIn('https://',content)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'leaderboard.sqlite');db.init_db()
        self.users=[User(i,'Name '+str(i)+' हिंदी 👩🏽‍⚕️',False) for i in range(701,707)]
        for user in self.users:db.upsert_user(user)
        self.qid=db.new_quiz(701,'Quiz <title>')
        self.questions=[db.add_question(self.qid,'Question '+str(i),['Correct','Wrong'],0) for i in range(4)]
        self.sid=None
        self.client=NS(get_me=AsyncMock(return_value=NS(username='offline_quiz_bot')),send_message=AsyncMock(),send_document=AsyncMock())
        self.app=NS(bot=self.client,job_queue=NS(get_jobs_by_name=lambda name:[]))
    def tearDown(self):
        db.set_db_path(self.previous);self.tmp.cleanup()
    def session(self,negative=0,asked=4):
        self.sid=db.create_group_session(self.qid,-10055,701,self.questions,45,False,100,negative_mark=negative)
        # Two equal-correct users differ by wrong count; a third ties net/elapsed.
        votes={701:[0,0,1,1],702:[0,0],703:[0,0],704:[0,1,1,1],705:[1],706:[1,1]}
        speeds={701:1.25,702:5.0,703:2.0,704:1.0,705:1.0,706:1.0}
        for i,qid in enumerate(self.questions[:asked]):
            poll=f'leaderboard-{self.sid}-{i}';db.set_group_current(self.sid,poll,10+i,qid,i,[0,1],0,100+i,'')
            for uid,choices in votes.items():
                if i<len(choices):
                    c=choices[i];self.assertTrue(db.record_group_answer(self.sid,self.qid,-10055,poll,qid,i,uid,c,c,c==0,speeds[uid],100+i))
            self.assertTrue(db.close_group_question(self.sid,poll))
        return db.get_group_session(self.sid)
    def dump(self):
        with db.connect() as con:return '\n'.join(con.iterdump())
    def html(self,session,stopped=False):
        return Table(group._group_leaderboard_attachment(session,'Quiz <title>',stopped).decode())
    async def finish(self,session,status='finished'):
        with patch.object(db,'pdf_review_enabled',return_value=False),patch.object(review_pdf,'auto_group_after_finish',new=AsyncMock()) as pdf:
            done=await group._finish_locked(self.app,session,status)
            pdf.assert_awaited_once()
        return done
    def test_no_negative_ranking_counts_score_percentage_elapsed_match_existing_result(self):
        session=self.session();db.finish_group_session(self.sid,'finished',110);session=db.get_group_session(self.sid)
        before=self.dump();text=group.result_text(session,'Quiz <title>',False);table=self.html(session)
        # Existing SQL: correct descending, then accumulated elapsed, then user ID.
        self.assertEqual([r[1].split()[1] for r in table.rows],['703','701','702','704','705','706'])
        self.assertEqual(table.rows[0][2:],['2','0','2/4','4.00s','50.00%'])
        self.assertEqual(table.rows[1][2:],['2','2','2/4','5.00s','50.00%'])
        for row in table.rows:self.assertIn(row[1].replace('<','&lt;'),text)
        self.assertEqual(before,self.dump())
    def test_negative_stable_net_order_and_correct_percentage_not_net_percentage(self):
        session=self.session(.25);db.finish_group_session(self.sid,'finished',110);session=db.get_group_session(self.sid)
        rows=self.html(session).rows
        self.assertEqual([r[1].split()[1] for r in rows],['703','702','701','704','705','706'])
        self.assertEqual(rows[2][2:],['2','2','1.50/4','5.00s','50.00%'])
        self.assertEqual(rows[-1][4],'-0.50/4')
        text=group.result_text(session,'Quiz <title>',False)
        self.assertIn('Net 1.50/4',text);self.assertIn('Percentage 50.00%',text)
    def test_negative_one_third_uses_existing_rounding(self):
        session=self.session(1/3);db.finish_group_session(self.sid,'finished',110);session=db.get_group_session(self.sid)
        rows=self.html(session).rows
        self.assertEqual(next(r for r in rows if r[1].startswith('Name 701 '))[4],'1.33/4')
    def test_stopped_denominator_and_zero_asked_fallback_match_old_result(self):
        session=self.session(.25,asked=2);db.finish_group_session(self.sid,'stopped',110);session=db.get_group_session(self.sid)
        rows=self.html(session,True).rows;self.assertTrue(all(r[4].endswith('/2') for r in rows))
        self.assertEqual(rows[0][-1],'100.00%')
        session['asked']=0;rows=self.html(session,True).rows
        self.assertTrue(all(r[4].endswith('/4') for r in rows))
    def test_name_fallback_uses_existing_display_logic(self):
        rows=[dict(user_id=1,first_name='',username='fallback',correct=1,wrong=0,score=1,answered=1,elapsed=2),dict(user_id=2,first_name='',username='',correct=0,wrong=1,score=0,answered=1,elapsed=3)]
        with patch.object(db,'group_leaderboard',return_value=rows):table=self.html({'id':1,'total':2})
        self.assertEqual([r[1] for r in table.rows],['@fallback','User 2'])
    async def test_real_finish_sends_unchanged_text_and_one_automatic_attachment_no_new_button(self):
        session=self.session(.25);done=await self.finish(session)
        self.client.send_message.assert_awaited_once();self.client.send_document.assert_awaited_once()
        self.assertEqual(self.client.send_message.await_args.args[1],group.result_text(done,'Quiz <title>',False))
        markup=self.client.send_message.await_args.kwargs['reply_markup']
        self.assertEqual(len(markup.inline_keyboard),1)  # pre-existing personal HTML link only
        self.assertIn('htmlgroup_',markup.inline_keyboard[0][0].url)
        doc=self.client.send_document.await_args.kwargs
        self.assertEqual(doc['chat_id'],-10055);self.assertTrue(doc['filename'].endswith('.html'))
        self.assertEqual(len(Table(doc['document'].decode()).rows),6)
        before=self.dump();await group._finish_locked(self.app,done,'finished')
        self.assertEqual(self.client.send_document.await_count,1);self.assertEqual(before,self.dump())
    async def test_stopped_finish_also_delivers_partial_table(self):
        done=await self.finish(self.session(.25,asked=2),'stopped')
        doc=self.client.send_document.await_args.kwargs
        self.assertIn('partial result',doc['document'].decode());self.assertEqual(done['status'],'stopped')
    async def test_attachment_failure_keeps_saved_result_and_pdf_hook(self):
        self.client.send_document.side_effect=TimedOut()
        done=await self.finish(self.session())
        self.assertEqual(done['status'],'finished');self.client.send_message.assert_awaited_once()
        self.assertEqual(len(db.group_leaderboard(self.sid)),6)
    async def test_renderer_failure_does_not_suppress_original_result_or_pdf(self):
        with patch.object(html_report,'build_group_leaderboard_html',side_effect=RuntimeError('fixture')):
            done=await self.finish(self.session())
        self.assertEqual(done['status'],'finished');self.client.send_message.assert_awaited_once()
        self.client.send_document.assert_not_awaited()
    async def test_existing_message_failure_does_not_prevent_attachment(self):
        self.client.send_message.side_effect=Forbidden('fixture')
        done=await self.finish(self.session());self.assertEqual(done['status'],'finished')
        self.client.send_document.assert_awaited_once()
    async def test_text_chunks_and_existing_pdf_markup_preserved(self):
        session=self.session()
        with patch.object(group.engine,'split_message',return_value=['original first','original last']),patch.object(db,'pdf_review_enabled',return_value=True),patch.object(review_pdf,'auto_group_after_finish',new=AsyncMock()):
            await group._finish_locked(self.app,session,'finished')
        calls=self.client.send_message.await_args_list
        self.assertEqual([c.args[1] for c in calls],['original first','original last']);self.assertIsNone(calls[0].kwargs['reply_markup'])
        self.assertEqual(len(calls[1].kwargs['reply_markup'].inline_keyboard),2)
    async def test_finish_empty_group_delivers_table_without_divide_by_zero(self):
        sid=db.create_group_session(self.qid,-10055,701,self.questions,45,False,100)
        await self.finish(db.get_group_session(sid))
        self.assertIn('No participants',self.client.send_document.await_args.kwargs['document'].decode())


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.play=sync_playwright().start();cls.browser=cls.play.chromium.launch(args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.play.stop()
    def test_mobile_pagination_all_123_rows_columns_and_horizontal_access(self):
        rows=fixture_rows(123);rows[0]['name']='Name 👩🏽‍⚕️ हिंदी '+('LongName'*150)
        content=html_report.build_group_leaderboard_html('Mobile quiz',rows,asked=5,total=5).decode()
        for width in (360,390,412):
            with self.subTest(width=width):
                page=self.browser.new_page(viewport={'width':width,'height':844});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                try:
                    page.set_content(content,wait_until='load')
                    self.assertEqual(page.locator('#page-status').inner_text(),'Page 1 of 3')
                    self.assertTrue(page.locator('#previous').is_disabled());self.assertEqual(page.locator('.leaderboard-page:visible tbody tr').count(),50)
                    self.assertEqual(page.locator('#page-1 .name').first.inner_text(),rows[0]['name'])
                    zebra=page.locator('#page-1 tbody tr').evaluate_all('(rs)=>rs.slice(0,2).map(r=>getComputedStyle(r).backgroundColor)')
                    self.assertNotEqual(zebra[0],zebra[1])
                    seen=[]
                    for index in range(3):
                        seen+=page.locator('.leaderboard-page:visible tr[data-rank]').evaluate_all('(rs)=>rs.map(r=>Number(r.dataset.rank))')
                        if index<2:page.locator('#next').click()
                    self.assertEqual(seen,list(range(1,124)));self.assertTrue(page.locator('#next').is_disabled())
                    self.assertEqual(page.locator('.leaderboard-page:visible tbody tr').count(),23)
                    self.assertEqual(page.locator('.leaderboard-page:visible th').all_inner_texts(),['#','Name','✅','❌','Score','Time','%'])
                    page.locator('.leaderboard-page:visible .table-scroll').evaluate('(e)=>{e.scrollLeft=e.scrollWidth;}')
                    self.assertGreater(page.locator('.leaderboard-page:visible .table-scroll').evaluate('(e)=>e.scrollLeft'),0)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                    page.locator('#page-select').select_option('1');self.assertEqual(page.locator('#page-status').inner_text(),'Page 1 of 3')
                    self.assertEqual(errors,[])
                finally:page.close()
    def test_without_javascript_all_pages_and_names_remain_accessible(self):
        context=self.browser.new_context(java_script_enabled=False,viewport={'width':390,'height':844})
        try:
            page=context.new_page();page.set_content(html_report.build_group_leaderboard_html('Quiz',fixture_rows(123),asked=5,total=5).decode())
            self.assertEqual(page.locator('.leaderboard-page:visible').count(),3)
            self.assertEqual(page.locator('.leaderboard-page:visible tbody tr').count(),123)
            self.assertFalse(page.locator('#pagination').is_visible())
        finally:context.close()
    def test_saved_file_opens_offline_and_untrusted_name_is_literal(self):
        rows=fixture_rows(2);rows[0]['name']='<img src=x onerror="window.injected=1"> 👑'
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'leaderboard.html';file.write_bytes(html_report.build_group_leaderboard_html('Quiz',rows,asked=5,total=5))
            page=self.browser.new_page();requests=[]
            page.on('request',lambda req:requests.append(req.url))
            try:
                page.goto(file.as_uri(),wait_until='load')
                self.assertEqual(page.locator('.name').first.inner_text(),rows[0]['name'])
                self.assertIsNone(page.evaluate('window.injected'));self.assertEqual(page.locator('img').count(),0)
                self.assertFalse(any(u.startswith(('http:','https:')) for u in requests))
            finally:page.close()


if __name__=='__main__':unittest.main()
