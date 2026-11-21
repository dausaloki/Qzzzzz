"""Actual nine TXT formats, source oracles, import/runners, and frozen compatibility.
No live Telegram. Only temporary SQLite. Required actual sample files are read from
QZZZZZ_TXT_TEST_DIR (default /home/user/uploads); missing fixtures fail, never skip.
Run from project root: python -m unittest discover -s tests -p test_txt_formats_display.py -v
"""
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from telegram import User
from telegram.error import BadRequest
import bot
import config
import database as db
import group_runner
import pdf_parser
import quiz_creator as creator
import quiz_engine as engine
import quiz_runner

SAMPLES = [
('Format_1_Assertion_Reason_Questions.txt',42,'5ed305b5a13a031c404e43628dd482c61a84ad2463780ad26869d061e9382258'),
('Format_2_Statement_Based_Questions.txt',60,'880830642ec78e4d573a880ec7f9e1d9d18c1818ed073e76f3381a4a24524db7'),
('Format_3_Match_The_Column_Questions.txt',18,'73c6131d3853cc4e355f985857074381ad96e053b26c85afe4fc22bcb4bd7f70'),
('Format_4_Direct_Single_Choice_MCQs.txt',20,'bfb456dabd15be0cefabd42d7c93399f5f4cab41bcffe87da404b73b5b64fcf4'),
('Format_5_True_False_Questions.txt',14,'183d8ee3af42f4e7d6ab068ddf96917ce41d374d3c5aa92ff92b395b4f75f9fb'),
('Format_6_Fill_In_The_Blanks.txt',18,'bad29a5e429e07ac444f37ba47d00ed6759dffbf0d04426b2abeea880292b9a0'),
('Format_7_Short_Answer_One_Word_Questions.txt',20,'06809df6588d628454d8660fa598c6cf11b2094712cf12564d4907b5e92fc3fe'),
('Format_8_Scenario_Case_Study_Questions.txt',8,'1bde270ca7e9c235c25890f7c84d3755079568a45c07744cec2cf998e5459535'),
('Format_9_Multi_Select_Questions.txt',5,'fe4706cb0552f85136b6c65e20d1784c05bd36d1f904151330b07511bab397db')]
SUPPORTED={1,2,3,4,8}


def sample(number):
    name,_,sha=SAMPLES[number-1]
    raw=(Path(os.environ.get('QZZZZZ_TXT_TEST_DIR','/home/user/uploads'))/name).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha:raise AssertionError('Original fixture hash mismatch: '+name)
    return raw.decode('utf-8')


def source_oracle(text):
    """Independent source extraction for supplied single-choice samples.

    Does not call any production parser/normalizer. Explicit answer lines delimit
    source questions; the first explicit header after the last answer is outer.
    Final ordered choice runs distinguish list data from answer codes.
    """
    lines=text.splitlines();previous=-1;out=[]
    for end,line in enumerate(lines):
        if not re.match(r'^उत्तर[ \t]*:',line):continue
        start=next(i for i in range(previous+1,end) if re.match(r'^प्रश्न[ \t]+\d+\.',lines[i]))
        header=re.match(r'^प्रश्न[ \t]+(\d+)\.[ \t]*(.*)$',lines[start]);body=lines[start:end]
        marks=[]
        for i,l in enumerate(body):
            m=re.match(r'^[ \t]*(?:\(([a-dA-D1-4])\)|([1-4])\.)[ \t]+(.*)$',l)
            if m:marks.append((i,(m[1] or m[2]).lower(),m[3]))
        groups=[marks[k:k+4] for k in range(len(marks)-3) if [m[1] for m in marks[k:k+4]] in [list('abcd'),list('1234')]]
        group=groups[-1];opts=[]
        for j,(pos,_,rest) in enumerate(group):
            stop=group[j+1][0] if j<3 else len(body)
            opts.append('\n'.join([rest]+body[pos+1:stop]).strip())
        a=re.match(r'^उत्तर:[ \t]*(?:\(([a-dA-D1-4])\)|\[([a-dA-D1-4])\]|([1-4])\.)',line)
        tok=next(s.lower() for s in a.groups() if s)
        correct='abcd'.index(tok) if tok in 'abcd' else '1234'.index(tok)
        stem='\n'.join(([header[2]] if header[2] else [])+body[1:group[0][0]]).strip()
        out.append(dict(number=int(header[1]),question=stem,options=opts,correct_index=correct))
        previous=end
    return out


def legacy_corpus():
    choices='\n(A) One\n(B) Two\n(C) Three\n(D) Four\nAnswer: B\nExplanation: Saved हिंदी ± H₂O.'
    return [
      'प्रश्न 1. Assertion (A): A fact.\nReason (R): A reason.'+choices,
      'प्रश्न 1. A direct question?'+choices,
      'प्रश्न 1. Match the Column:\nList I: X, Y\nList II: 1, 2'+choices,
      'प्रश्न 1. पृथ्वी गोल है।\n(A) सत्य\n(B) असत्य\nउत्तर: A',
      'प्रश्न 1. Water is ____. '+choices,
      'प्रश्न 1. Choose the one-word answer.'+choices,
      'प्रश्न 1. Case study: A patient arrives.\nHistory: First line.\nWhat is appropriate?'+choices,
      'प्रश्न 1. Select all correct values.'+choices.replace('Answer: B','Answer: A, C'),
      'Plain stem\nA) One\nB) Two ✅\nC) Three\nD) Four\nEx: Stored explanation',
      'प्रश्न 1. अंकीय 𝑥 — symbol?\n(A) One\n(B) Two\nउत्तर: A']


def compatibility_digest(module):
    output=[]
    for text in legacy_corpus():
        result=module.parse_txt_source(text);item={'parse':result.to_dict(),'payloads':[]}
        for parsed in result.questions:
            q=parsed.to_dict();q['source_number']=parsed.number
            for perm in [list(range(len(q['options']))),list(reversed(range(len(q['options']))))]:
                for build in (module.build_poll_payload,module.compact_fallback):
                    item['payloads'].append(asdict(build(q,perm,0,1)))
        output.append(item)
    return hashlib.sha256(json.dumps(output,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class PureTests(unittest.TestCase):
    def test_actual_five_supported_files_match_source_not_merely_counts(self):
        for n in sorted(SUPPORTED):
            with self.subTest(format=n):
                text=sample(n);expected=source_oracle(text);result=engine.parse_txt_source(text)
                self.assertEqual(len(expected),SAMPLES[n-1][1]);self.assertFalse(result.errors)
                self.assertEqual(len(result.questions),len(expected))
                for q,wanted in zip(result.questions,expected):
                    self.assertEqual((q.number,q.question,q.options,q.correct_index),(wanted['number'],wanted['question'],wanted['options'],wanted['correct_index']))
                    self.assertEqual(q.explanation,'')  # actual files have answer repetitions, not explanation paragraphs
                self.assertEqual(len({(q.question,tuple(q.options)) for q in result.questions}),len(expected))
    def test_case_context_belongs_only_to_its_pair_of_questions(self):
        text=sample(8);result=engine.parse_txt_source(text)
        contexts=re.findall(r'(\[केस[^\n]+\]\nस्थिति:[^\n]+)',text)
        self.assertEqual(len(contexts),4)
        for i,q in enumerate(result.questions):
            self.assertEqual(q.pre_text,contexts[i//2]);self.assertEqual(q.explanation,'')
    def test_screenshot_source_outer26_and_embedded16_and_actual_key_c(self):
        result=engine.parse_txt_source(sample(2));q=result.questions[25]
        self.assertEqual(q.number,26);self.assertTrue(q.question.startswith('प्रश्न 16.\n'))
        self.assertIn('Head Tilt-Chin Lift',q.question);self.assertEqual(q.correct_index,2)
        self.assertEqual(q.options,['केवल 1 और 4','केवल 2 और 3','केवल 1, 2 और 3','केवल 1, 3 और 4'])
        self.assertIn('निम्नलिखित कथनों पर विचार कीजिए',result.questions[0].question)
        self.assertIn('\n1. प्राथमिक चिकित्सा',result.questions[0].question)
    def test_structural_rules_not_explanations_and_topic_labels_retained(self):
        for n in (1,2,3,4):
            result=engine.parse_txt_source(sample(n));self.assertTrue(result.sections)
            for q in result.questions:
                self.assertTrue(q.section_label.startswith('Topic '));self.assertEqual(q.pre_text,'');self.assertEqual(q.explanation,'')
    def test_nine_actual_files_accounted_for_and_unsupported_modes_rejected(self):
        for n in range(1,10):
            with self.subTest(format=n):
                result=engine.parse_txt_source(sample(n))
                self.assertEqual(len(result.questions),SAMPLES[n-1][1] if n in SUPPORTED else 0)
                self.assertEqual(bool(result.errors),n not in SUPPORTED)
    def test_unaffected_legacy_parse_and_payload_frozen_before_patch(self):
        self.assertEqual(compatibility_digest(engine),'f13dfb454a3f57338aaefd1d105c75ed58178e39d9345232a1d56ace79350abb')
    def test_topicwise_two_option_regression_and_no_pdf_parser_change(self):
        text='1. Topic one\n-----\nQ1. Water is wet?\n(a) True\n(b) False\nAnswer: (a)\n\n2. Topic two\n-----\nQ1. Earth is flat?\n(a) True\n(b) False\nAnswer: (b)'
        res=engine.parse_txt_source(text);self.assertFalse(res.errors);self.assertEqual(len(res.questions),2)
        self.assertEqual([q.correct_index for q in res.questions],[0,1])
    def test_explicit_explanations_unicode_blank_lines_and_stem_rule_preserved(self):
        text='प्रश्न 26.\nप्रश्न 16.\nConsider statements हिंदी ± H₂O 𝑥:\n\n1. First.\n2. Second.\n----\nChoose:\n(A) Only 1\n(B) Only 2\nAnswer: A\nExplanation: Original explanation — सही।\n----'
        res=engine.parse_txt_source(text);self.assertFalse(res.errors)
        q=res.questions[0];self.assertIn('\n\n1. First.',q.question);self.assertIn('\n----\n',q.question)
        self.assertEqual(q.explanation,'Original explanation — सही।');self.assertEqual(q.number,26)
    def test_conflicting_or_missing_answers_never_guessed(self):
        prefix='प्रश्न 1.\nप्रश्न 16.\nChoose:\n(A) One\n(B) Two\n'
        for tail in ('उत्तर: (A) One [b]','उत्तर: (C)','उत्तर: (A), (B)','उत्तर: (A)\nउत्तर: (B)',''):
            with self.subTest(tail=tail):
                res=engine.parse_txt_source(prefix+tail);self.assertFalse(res.questions);self.assertTrue(res.errors)
    def test_plain_numbered_statement_list_never_becomes_choices(self):
        text='प्रश्न 1.\nप्रश्न 16.\nConsider statements:\n1. First\n2. Second\n3. Third\n4. Fourth\nउत्तर: (A)'
        res=engine.parse_txt_source(text);self.assertFalse(res.questions);self.assertTrue(res.errors)
    def test_multiline_options_preserved(self):
        text='प्रश्न 1.\nप्रश्न 16.\nChoose:\n(A) First\ncontinued हिंदी\n(B) Second\nAnswer: A'
        res=engine.parse_txt_source(text);self.assertFalse(res.errors)
        self.assertEqual(res.questions[0].options,['First\ncontinued हिंदी','Second'])
        p=engine.build_poll_payload(res.questions[0].to_dict(),[0,1],0,1)
        self.assertIn('First\ncontinued हिंदी',''.join(p.full_text_chunks))
    def test_inline_choices_in_existing_valid_structured_layout_fall_back_unchanged(self):
        text='Topic 1: Example\n====\nप्रश्न 1. Choose a value?\n(A) One (B) Two (C) Three (D) Four\nउत्तर: A'
        self.assertEqual(engine.parse_txt_source(text).to_dict(),pdf_parser.parse_source(text).to_dict())
    def test_statement_variations_short_long_and_permutations_preserve_everything(self):
        for language in ('hi','en'):
            for count in (2,3,4,7):
                for length in (8,150,1500):
                    stem=('निम्न कथनों पर विचार करें—' if language=='hi' else 'Consider the statements:')+'\n'+'\n'.join(f'{i}. '+('क' if language=='hi' else 'x')*length for i in range(1,count+1))
                    opts=['केवल 1 और 2','सभी कथन सही हैं'] if language=='hi' else ['Only 1 and 2','All statements']
                    q=dict(question=stem,options=opts,correct_index=1,source_number=26,explanation='Stored explanation')
                    for perm in ([0,1],[1,0]):
                        p=engine.build_poll_payload(q,perm,0,1)
                        self.assertLessEqual(engine.tg_len(p.question),config.POLL_QUESTION_MAX)
                        output=''.join(p.full_text_chunks) or p.question
                        self.assertIn(stem,output);self.assertEqual(p.correct_option_id,list(perm).index(1))
                        self.assertTrue(all(o in output or o in p.options for o in opts))
                        if p.full_text_chunks:self.assertTrue(all(engine.tg_len(x)<=config.SAFE_MESSAGE_CHUNK for x in p.full_text_chunks))
    def test_short_statement_uses_full_native_stem_without_companion(self):
        q=dict(question='Consider statements:\n1. First.\n2. Second.',options=['Only 1','Only 2'],correct_index=0,source_number=26)
        p=engine.build_poll_payload(q,[0,1],0,1)
        self.assertIn(q['question'],p.question);self.assertIn('Q26',p.question);self.assertFalse(p.full_text_chunks)
    def test_statement_multiline_options_never_lose_full_option_text(self):
        q=dict(question='Consider statements:\n1. First.\n2. Second.',options=['Only 1\ncontinued हिंदी','Only 2'],correct_index=0)
        p=engine.build_poll_payload(q,[0,1],0,1)
        self.assertTrue(p.full_text_chunks);self.assertIn(q['options'][0],''.join(p.full_text_chunks))
    def test_all_actual_answer_keys_survive_reverse_and_rotated_options(self):
        for n in SUPPORTED:
            for parsed in engine.parse_txt_source(sample(n)).questions:
                q=parsed.to_dict();q['source_number']=parsed.number;count=len(q['options'])
                for perm in (list(reversed(range(count))),list(range(1,count))+[0]):
                    p=engine.build_poll_payload(q,perm,0,SAMPLES[n-1][1])
                    self.assertEqual(perm[p.correct_option_id],q['correct_index'])
                    visible=p.question+''.join(p.full_text_chunks)+'\n'.join(p.options)
                    self.assertIn(q['question'],visible)
                    for option in q['options']:self.assertIn(option,visible)
    def test_unruled_topic_sentence_inside_question_is_not_removed(self):
        text='प्रश्न 1.\nप्रश्न 16.\nStem हिंदी\nTopic 2: literal content\n(A) One\n(B) Two\nAnswer: A'
        res=engine.parse_txt_source(text);self.assertFalse(res.errors)
        self.assertIn('Topic 2: literal content',res.questions[0].question)
    def test_unrecognized_leading_passage_keeps_original_parser_path(self):
        text='Important original passage — do not remove.\nTopic 1: Example\n====\nप्रश्न 1. Choose a value?\n(A) One\n(B) Two\nउत्तर: A'
        self.assertEqual(engine.parse_txt_source(text).to_dict(),pdf_parser.parse_source(text).to_dict())
    def test_duplicate_outer_number_not_silently_imported_twice(self):
        block='प्रश्न 1.\nप्रश्न 16.\nStem\n(A) One\n(B) Two\nAnswer: A\n'
        res=engine.parse_txt_source(block+block)
        self.assertEqual(len(res.questions),1);self.assertTrue(res.errors)
    def test_unsupported_actual_modes_frozen_before_patch(self):
        data=[engine.parse_txt_source(sample(n)).to_dict() for n in (5,6,7,9)]
        digest=hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest,'d8cd03ba9d52c551484694e337d8e508a5bde47464c75976790ce329877155ef')
    def test_custom_presentation_user_content_not_removed(self):
        settings=engine.default_format_settings('presentation')
        q=dict(question='Consider statements:\n1. First.\n2. Second.',options=['Only 1','Only 2'],correct_index=0)
        p=engine.build_poll_payload(q,[0,1],0,1,presentation=settings)
        self.assertTrue(p.full_text_chunks);self.assertIn(q['question'],''.join(p.full_text_chunks))


class Jobs:
    def get_jobs_by_name(self,name):return []
    def run_once(self,*args,**kwargs):return NS(schedule_removal=lambda:None)


class ImportAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'audit.sqlite');db.init_db();self.seq=0
    def tearDown(self):db.set_db_path(self.old);self.tmp.cleanup()
    async def poll(self,**kwargs):
        self.seq+=1;return NS(poll=NS(id=f'audit-{self.seq}'),message_id=self.seq)
    def fixture(self,n):
        user=User(9000+n,'Audit',False);db.upsert_user(user)
        status=NS(edit_text=AsyncMock(),delete=AsyncMock())
        raw=sample(n).encode();file=NS(file_size=len(raw),download_as_bytearray=AsyncMock(return_value=bytearray(raw)))
        document=NS(file_name=SAMPLES[n-1][0],mime_type='text/plain',file_size=len(raw),get_file=AsyncMock(return_value=file))
        msg=NS(document=document,caption=None,text=None,message_id=77,reply_text=AsyncMock(return_value=status))
        chat=NS(id=user.id,type='private',send_message=AsyncMock(),send_document=AsyncMock())
        client=NS(get_me=AsyncMock(return_value=NS(username='offline_audit_bot')),send_message=AsyncMock(return_value=NS(message_id=999)),send_document=AsyncMock(),send_poll=AsyncMock(side_effect=self.poll))
        update=NS(effective_user=user,effective_chat=chat,effective_message=msg,callback_query=None,get_bot=lambda:client)
        return update,NS(bot=client,application=NS(bot=client,job_queue=Jobs()))
    async def test_all_actual_files_document_import_and_full_private_group_question_delivery(self):
        for n in range(1,10):
            with self.subTest(format=n):
                update,ctx=self.fixture(n);uid=update.effective_user.id
                db.set_state(uid,'imp_wait',{'buffer':''})
                with patch.object(creator,'forward_txt_upload',new=AsyncMock(return_value=False)):
                    await creator.handle_import_document(update,ctx)
                state,data=db.get_state(uid);self.assertEqual(state,'imp_review')
                if n not in SUPPORTED:
                    self.assertTrue(data['errors']);self.assertFalse(db.get_owner_quizzes(uid));continue
                self.assertFalse(data['errors']);self.assertEqual(len(data['questions']),SAMPLES[n-1][1])
                update.callback_query=NS(data='i:reviewed' if data.get('review') else 'i:create',answer=AsyncMock())
                await bot.callback_router(update,ctx);self.assertEqual(db.get_state(uid)[0],'imp_title')
                update.callback_query.data='i:usedefault';await bot.callback_router(update,ctx)
                quizzes=db.get_owner_quizzes(uid);self.assertEqual(len(quizzes),1)
                qid=quizzes[0]['id'];rows=db.get_questions(qid);expected=source_oracle(sample(n))
                for row,wanted in zip(rows,expected):
                    self.assertEqual((row['source_number'],row['question'],row['options'],row['correct_index']),(wanted['number'],wanted['question'],wanted['options'],wanted['correct_index']))
                aid=db.create_attempt(qid,uid,uid,[q['id'] for q in rows],45,False,time.time(),snapshot_rules=True,pdf_allowed=False)
                sid=db.create_group_session(qid,-10000-n,uid,[q['id'] for q in rows],45,False,time.time(),pdf_enabled_at_start=False)
                for i,q in enumerate(rows):
                    p=engine.build_poll_payload(q,list(range(len(q['options']))),i,len(rows))
                    for kind in ('private','group'):
                        ctx.bot.send_message.reset_mock();ctx.bot.send_poll.reset_mock()
                        if kind=='private':await quiz_runner.send_current(ctx.application,aid)
                        else:await group_runner._send_current(ctx.application,sid)
                        ctx.bot.send_poll.assert_awaited_once();sent=ctx.bot.send_poll.await_args.kwargs
                        self.assertEqual(sent['question'],p.question);self.assertEqual(sent['options'],p.options)
                        self.assertEqual(sent['correct_option_ids'],[q['correct_index']])
                        self.assertFalse(sent['allows_multiple_answers']);self.assertEqual(sent['type'],'quiz')
                        messages=[call.args[1] for call in ctx.bot.send_message.await_args_list]
                        self.assertEqual(messages,([q['pre_text']] if q['pre_text'] else [])+p.full_text_chunks)
                    self.assertTrue(db.record_answer(aid,q['id'],i,q['correct_index'],True,'correct',1))
                    live=db.get_group_session(sid)
                    self.assertTrue(db.record_group_answer(sid,qid,-10000-n,live['cur_poll_id'],q['id'],i,uid,q['correct_index'],q['correct_index'],True,1,time.time()))
                    self.assertTrue(db.close_group_question(sid,live['cur_poll_id']))
                self.assertEqual(db.get_attempt(aid)['current_index'],len(rows))
                self.assertEqual(db.get_group_session(sid)['current_index'],len(rows))
                self.assertEqual(db.get_questions(qid),rows)  # sending never edits stored source
                db.finish_group_session(sid,'finished',time.time())
    async def test_telegram_rejection_uses_existing_lossless_fallback(self):
        update,ctx=self.fixture(2);q=engine.parse_txt_source(sample(2)).questions[0]
        qid=db.new_quiz(update.effective_user.id,'Fallback fixture');qidrow=db.add_question(qid,q.question,q.options,q.correct_index,source_number=q.number)
        aid=db.create_attempt(qid,update.effective_user.id,update.effective_user.id,[qidrow],45,False,time.time(),pdf_allowed=False)
        ctx.bot.send_poll.side_effect=[BadRequest('fixture rejected'),NS(poll=NS(id='recovered'),message_id=99)]
        await quiz_runner.send_current(ctx.application,aid)
        self.assertEqual(ctx.bot.send_poll.await_count,2)
        messages=''.join(call.args[1] for call in ctx.bot.send_message.await_args_list)
        self.assertIn(q.question,messages);self.assertTrue(all(o in messages for o in q.options))
        self.assertEqual(db.get_attempt(aid)['cur_poll_id'],'recovered')


if __name__=='__main__':unittest.main()
