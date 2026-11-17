"""Section typing + real topicwise TXT regression; no live Telegram calls.
Run from project root: python -m unittest discover -s tests -v
Real file read from QZZZZZ_TXT_TEST_DIR (default /home/user/uploads); never bundled.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from telegram import User
import database as db
import bot
import quiz_creator as creator
import quiz_engine as engine
import test_independent_formats_support as existing_tests


def actual_source():
    path=Path(os.environ.get('QZZZZZ_TXT_TEST_DIR','/home/user/uploads'))/'dainik_vigyan_topicwise_mcq.txt'
    if not path.is_file():
        raise AssertionError('Required external actual TXT fixture is missing: '+str(path))
    return path


def independent_oracle(text):
    """Read this original file directly, without calling any production parser.

    Subject titles precede ruled '-' lines. Q-prefixed blocks, four explicit
    option labels and the answer line are read independently in source order.
    """
    lines=text.splitlines()
    heads=[]
    for i,line in enumerate(lines[:-1]):
        title=line.strip()
        if title and title[0].isdigit() and '. ' in title and lines[i+1].strip() == '-'*80:
            heads.append((i,title))
    result=[]
    for part,(start,title) in enumerate(heads):
        end=heads[part+1][0] if part+1<len(heads) else len(lines)
        starts=[i for i in range(start+2,end) if lines[i].lstrip().startswith('Q') and lines[i].lstrip()[1:].partition('.')[0].isdigit()]
        for k,at in enumerate(starts):
            stop=starts[k+1] if k+1<len(starts) else end
            first=lines[at].lstrip();num,_,stem=first[1:].partition('.')
            body=[stem.lstrip()]+lines[at+1:stop]
            answer_positions=[j for j,line in enumerate(body) if line.strip().startswith('उत्तर:')]
            assert len(answer_positions)==1,(title,num)
            answer_pos=answer_positions[0]
            option_positions=[]
            for j,line in enumerate(body[:answer_pos]):
                t=line.lstrip()
                if len(t)>2 and t[0]=='(' and t[2]==')' and t[1].lower() in '1234abcd':
                    option_positions.append(j)
            assert len(option_positions)==4,(title,num,option_positions)
            opts=[]
            for n,j in enumerate(option_positions):
                last=option_positions[n+1] if n<3 else answer_pos
                opts.append('\n'.join([body[j].lstrip()[3:].lstrip()]+body[j+1:last]).strip())
            token=body[answer_pos].strip().partition(':')[2].strip()[1].lower()
            correct=int(token)-1 if token.isdigit() else ord(token)-ord('a')
            result.append(dict(number=int(num),question='\n'.join(body[:option_positions[0]]).strip(),
                               options=opts,correct_index=correct,section=title))
    return heads,result


class SectionTypes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'sections.sqlite3');db.init_db()
        self.qid=db.new_quiz(301,'Synthetic type regression')
    def tearDown(self):
        db.set_db_path(self.previous);self.tmp.cleanup()
    def test_bulk_types_and_label_preservation_in_source_order(self):
        cases=[({},''),({'section':None},''),({'section':0},'0'),({'section':1},'1'),
               ({'section':20},'20'),({'section':'  Chemistry रसायन  '},'Chemistry रसायन'),
               ({'section':3,'section_label':'  4. धातु (Metals)  '},'4. धातु (Metals)'),
               ({'section':'3','section_label':'4. धातु (Metals)'},'4. धातु (Metals)'),
               ({'section':None,'section_label':'जीव विज्ञान'},'जीव विज्ञान'),
               ({'section':'','section_label':7},'7'),
               ({'section':'Named section','section_label':'Fallback'},'Named section'),
               ({'section':0,'section_label':None},'0')]
        rows=[dict(question=f'Synthetic question {i}',options=['H₂O','CO₂','x²','±'],correct_index=i%4,number=i+1,**values)
              for i,(values,_) in enumerate(cases)]
        before=copy.deepcopy(rows);self.assertEqual(db.add_questions_bulk(self.qid,rows),len(rows))
        self.assertEqual(rows,before)
        for i,(stored,(_,expected)) in enumerate(zip(db.get_questions(self.qid),cases)):
            self.assertEqual(stored['section'],expected);self.assertEqual(stored['position'],i+1)
            self.assertEqual(stored['question'],rows[i]['question']);self.assertEqual(stored['options'],rows[i]['options'])
            self.assertEqual(stored['correct_index'],rows[i]['correct_index'])
    def test_single_question_handles_none_integer_zero_and_text(self):
        for i,(value,expected) in enumerate([(None,''),(0,'0'),(9,'9'),('  प्रकाश / Light  ','प्रकाश / Light')]):
            db.add_question(self.qid,'Synthetic question',['A','B'],0,section=value)
            self.assertEqual(db.get_questions(self.qid)[i]['section'],expected)
    def test_invalid_bulk_is_atomic_and_existing_question_untouched(self):
        db.add_question(self.qid,'Existing',['A','B'],0,section='History')
        before=db.get_questions(self.qid)
        with self.assertRaises(ValueError):
            db.add_questions_bulk(self.qid,[dict(question='Valid',options=['A','B'],correct_index=0,section=4),
                                           dict(question='Bad',options=['A'],correct_index=0,section=5)])
        self.assertEqual(db.get_questions(self.qid),before)


class ActualTopicwiseImport(unittest.IsolatedAsyncioTestCase):
    update=existing_tests.AsyncTests.update
    telegram=existing_tests.AsyncTests.telegram
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'actual.sqlite3');db.init_db()
        db.upsert_user(User(9001,'Offline',False))
    def tearDown(self):
        db.set_db_path(self.previous);self.tmp.cleanup()
    def assert_matches(self,rows,expected,stored=False):
        self.assertEqual(len(rows),len(expected))
        for i,(row,ref) in enumerate(zip(rows,expected)):
            for field in ('question','options','correct_index'):
                self.assertEqual(row[field],ref[field],f'{ref["section"]} Q{ref["number"]}: {field}')
            self.assertEqual(row['source_number'] if stored else row['number'],ref['number'])
            self.assertEqual(row['section'] if stored else row['section_label'],ref['section'])
            if stored:self.assertEqual(row['position'],i+1)
    async def test_complete_actual_document_callback_and_sqlite_pipeline(self):
        path=actual_source();raw=path.read_bytes();text=raw.decode('utf-8-sig')
        heads,expected=independent_oracle(text)
        self.assertEqual(len(heads),20);self.assertEqual(len(expected),307)
        self.assertEqual(sum(len(q['options']) for q in expected),1228)
        update=self.update(9001,name=path.name,raw=raw);update.callback_query=None
        client,_,_=self.telegram();client.get_me=AsyncMock(return_value=NS(username='offline_fixture_bot'))
        update.get_bot=lambda:client
        db.set_state(9001,'imp_wait',{'buffer':''})
        with patch.object(creator,'forward_txt_upload',new=AsyncMock(return_value=False)):
            await creator.handle_import_document(update,NS(bot=client))
        state,data=db.get_state(9001)
        self.assertEqual(state,'imp_review');self.assertFalse(data['errors']);self.assertFalse(data['review'])
        self.assertEqual(data['report']['total_detected'],307);self.assertEqual(data['report']['duplicates'],0)
        self.assert_matches(data['questions'],expected)
        # Exercise the real review -> title -> default-title callback path.
        update.callback_query=NS(data='i:create',answer=AsyncMock())
        await bot.callback_router(update,NS(bot=client))
        self.assertEqual(db.get_state(9001)[0],'imp_title')
        update.callback_query.data='i:usedefault'
        await bot.callback_router(update,NS(bot=client))
        quizzes=db.get_owner_quizzes(9001);self.assertEqual(len(quizzes),1)
        rows=db.get_questions(quizzes[0]['id']);self.assert_matches(rows,expected,stored=True)
        self.assertIsNone(db.get_state(9001)[0])
        self.assertEqual(len({r['id'] for r in rows}),307)
        self.assertEqual(rows[-1]['source_number'],20)
        client.send_document.assert_not_awaited()
    async def test_explicit_user_selection_and_bulk_add_to_existing_quiz(self):
        text=actual_source().read_text();_,expected=independent_oracle(text)
        parsed=creator.parse_txt_for_user(9001,text)
        self.assertFalse(parsed.errors)
        qid=db.new_quiz(9001,'Existing quiz')
        self.assertEqual(db.add_questions_bulk(qid,[q.to_dict() for q in parsed.questions]),307)
        self.assert_matches(db.get_questions(qid),expected,stored=True)
    async def test_actual_append_callback_preserves_existing_content_and_all_sections(self):
        parsed=creator.parse_txt_for_user(9001,actual_source().read_text())
        _,expected=independent_oracle(actual_source().read_text())
        qid=db.new_quiz(9001,'Existing quiz with history')
        db.add_question(qid,'Existing retained question',['A','B'],0,section='Existing section')
        before=db.get_questions(qid)[0]
        db.set_state(9001,'imp_review',{'questions':[q.to_dict() for q in parsed.questions],'errors':[]})
        update=self.update(9001);client,_,_=self.telegram()
        client.get_me=AsyncMock(return_value=NS(username='offline_fixture_bot'));update.get_bot=lambda:client
        update.callback_query.data='i:appendquiz:'+qid
        await bot.callback_router(update,NS(bot=client))
        rows=db.get_questions(qid);self.assertEqual(len(rows),308);self.assertEqual(rows[0],before)
        for got,ref in zip(rows[1:],expected):
            for field in ('question','options','correct_index','section'):
                self.assertEqual(got[field],ref[field])
            self.assertEqual(got['source_number'],ref['number'])
        self.assertIsNone(db.get_state(9001)[0])

    async def test_unparseable_block_is_reported_without_answer_invention(self):
        text=actual_source().read_text()
        # Only synthetic mutation of an in-memory copy: original attachment is untouched.
        bad=text.replace('   उत्तर: (1) भौतिक परिवर्तन','',1)
        parsed=engine.parse_txt_source(bad)
        self.assertEqual(len(parsed.detected_numbers),307);self.assertEqual(len(parsed.questions),306)
        self.assertEqual(len(parsed.errors),1);self.assertEqual(parsed.errors[0].number,1)
        self.assertIn('भौतिक',parsed.errors[0].section)
        bad=text.replace('   उत्तर: (1) भौतिक परिवर्तन','   उत्तर: (1) भौतिक परिवर्तन\n   उत्तर: (2)',1)
        parsed=engine.parse_txt_source(bad);self.assertEqual(len(parsed.errors),1)
    async def test_mixed_digit_and_letter_fourth_option_uses_explicit_source_answer(self):
        parsed=engine.parse_txt_source(actual_source().read_text())
        selected=[q for q in parsed.questions if q.section_label.startswith('11.') and q.number in (4,18)]
        self.assertEqual(len(selected),2)
        self.assertTrue(all(q.correct_index==3 and len(q.options)==4 for q in selected))
    async def test_named_section_detection_does_not_capture_standard_legacy_txt(self):
        text='1. Synthetic?\n(A) A\n(B) B\n(C) C\n(D) D\nAnswer: B'
        self.assertIsNone(engine._parse_topicwise_txt(text))
        self.assertEqual(engine.parse_txt_source(text).questions[0].correct_index,1)


if __name__=='__main__':unittest.main()
