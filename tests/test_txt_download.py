"""Focused TXT-only download reliability tests; no live Telegram or production data.
Includes real PTB/HTTPX requests to a local bounded-lifetime HTTP test server.
Run from project root: python -m unittest discover -s tests -p test_txt_download.py -v
"""
import asyncio
import copy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from telegram import Bot,Document,User
from telegram.error import BadRequest,Forbidden,NetworkError,TimedOut
from telegram.request import HTTPXRequest
import bot as routes
import config
import database as db
import pdf_parser
import quiz_creator as creator

LEGACY='1. Synthetic question?\n(A) One\n(B) Two\n(C) Three\n(D) Four\nAnswer: B'
CHECKED='Synthetic checked question?\nA) One\nB) Two ✅\nC) Three\nD) Four\nEx: Reason'


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/'download.sqlite');db.init_db()
        db.upsert_user(User(701,'Offline',False))
        self.sleep=patch.object(creator.asyncio,'sleep',new=AsyncMock());self.backoff=self.sleep.start()
    def tearDown(self):
        self.sleep.stop();db.set_db_path(self.previous);self.tmp.cleanup()
    def fixture(self,raw=LEGACY.encode(),name='quiz.txt',mime='text/plain'):
        file=NS(file_size=len(raw),download_as_bytearray=AsyncMock(return_value=bytearray(raw)))
        doc=NS(file_name=name,mime_type=mime,file_size=len(raw),get_file=AsyncMock(return_value=file))
        status=NS(edit_text=AsyncMock(),delete=AsyncMock())
        msg=NS(document=doc,caption=None,text=None,message_id=77,reply_text=AsyncMock(return_value=status))
        chat=NS(id=701,type='private',send_message=AsyncMock(),send_document=AsyncMock())
        client=NS(get_me=AsyncMock(return_value=NS(username='offline_fixture_bot')))
        update=NS(effective_message=msg,effective_user=User(701,'Offline',False),effective_chat=chat,
                  callback_query=None,get_bot=lambda:client)
        return update,NS(bot=client),doc,file,status
    def dump(self):
        with db.connect() as con:return '\n'.join(con.iterdump())
    async def test_small_bytes_are_identical_and_timeouts_are_scoped(self):
        raw='हिन्दी H₂O ±\r\nOriginal bytes\r\n'.encode();_,_,doc,file,_=self.fixture(raw)
        self.assertEqual(await creator._download_txt_bytes(doc),raw)
        doc.get_file.assert_awaited_once_with(**creator._TXT_METADATA_TIMEOUTS)
        file.download_as_bytearray.assert_awaited_once_with(**creator._TXT_TRANSFER_TIMEOUTS)
        self.backoff.assert_not_awaited()
    async def test_large_8mib_txt_download_is_byte_exact(self):
        raw=(b'Original TXT\r\n'*(8*1024*1024//14+1))[:8*1024*1024]
        _,_,doc,_,_=self.fixture(raw)
        self.assertEqual(await creator._download_txt_bytes(doc),raw)
    async def test_getfile_timeout_retries_without_parser_or_database_mutation(self):
        _,_,doc,file,_=self.fixture();before=self.dump()
        doc.get_file.side_effect=[TimedOut(),file]
        self.assertEqual(await creator._download_txt_bytes(doc),LEGACY.encode())
        self.assertEqual(doc.get_file.await_count,2);self.assertEqual(file.download_as_bytearray.await_count,1)
        self.backoff.assert_awaited_once_with(1.0);self.assertEqual(self.dump(),before)
    async def test_transfer_timeout_gets_fresh_metadata_then_recovers(self):
        _,_,doc,file,_=self.fixture()
        file.download_as_bytearray.side_effect=[TimedOut(),bytearray(LEGACY.encode())]
        self.assertEqual(await creator._download_txt_bytes(doc),LEGACY.encode())
        self.assertEqual(doc.get_file.await_count,2);self.assertEqual(file.download_as_bytearray.await_count,2)
    async def test_temporary_networkerror_recovers_with_bounded_backoff(self):
        _,_,doc,file,_=self.fixture()
        doc.get_file.side_effect=[NetworkError('temporary'),TimedOut(),file]
        self.assertEqual(await creator._download_txt_bytes(doc),LEGACY.encode())
        self.assertEqual(self.backoff.await_args_list[0].args,(1.0,));self.assertEqual(self.backoff.await_args_list[1].args,(2.0,))
    async def test_exhausted_download_preserves_existing_quiz_and_pending_import(self):
        qid=db.new_quiz(701,'Existing quiz');db.add_question(qid,'Retained',['A','B'],0)
        db.set_state(701,'imp_title',{'questions':[{'retained':True}],'default_title':'Keep'})
        before=self.dump();update,ctx,doc,file,status=self.fixture()
        file.download_as_bytearray.side_effect=TimedOut()
        with patch.object(creator,'parse_txt_for_user') as parse,patch.object(creator,'forward_txt_upload',new=AsyncMock()) as forward:
            await creator.handle_import_document(update,ctx)
            parse.assert_not_called();forward.assert_not_awaited()
        self.assertEqual(self.dump(),before);self.assertEqual(doc.get_file.await_count,3)
        self.assertEqual(file.download_as_bytearray.await_count,3)
        self.assertIn('File download नहीं हुई: Timed out',status.edit_text.await_args.args[0])
    async def test_permanent_api_rejection_is_not_retried(self):
        for error in (BadRequest('file not found'),Forbidden('denied')):
            with self.subTest(error=type(error).__name__):
                _,_,doc,file,_=self.fixture();doc.get_file.side_effect=error
                with self.assertRaises(type(error)):await creator._download_txt_bytes(doc)
                self.assertEqual(doc.get_file.await_count,1);file.download_as_bytearray.assert_not_awaited()
        self.backoff.assert_not_awaited()
    async def test_incomplete_transfer_retries_fresh_buffer_and_never_parses_partial_bytes(self):
        _,_,doc,file,_=self.fixture();file.download_as_bytearray.side_effect=[bytearray(b'partial'),bytearray(LEGACY.encode())]
        result=await creator._download_txt_bytes(doc)
        self.assertEqual(result,LEGACY.encode());self.assertEqual(doc.get_file.await_count,2)
        self.assertTrue(all(not call.args for call in file.download_as_bytearray.await_args_list))
    async def test_oversize_download_with_unknown_document_size_is_rejected(self):
        _,_,doc,file,_=self.fixture(b'x'*11);doc.file_size=None;file.file_size=None
        with patch.object(config,'MAX_DOWNLOAD_BYTES',10):
            with self.assertRaises(pdf_parser.SourceError):await creator._download_txt_bytes(doc)
        self.assertEqual(doc.get_file.await_count,1);self.backoff.assert_not_awaited()
    async def test_remote_metadata_size_limit_prevents_transfer(self):
        _,_,doc,file,_=self.fixture();doc.file_size=None;file.file_size=config.MAX_DOWNLOAD_BYTES+1
        with self.assertRaises(pdf_parser.SourceError):await creator._download_txt_bytes(doc)
        file.download_as_bytearray.assert_not_awaited()
    async def test_declared_oversize_still_uses_existing_early_rejection(self):
        update,ctx,doc,_,_=self.fixture();doc.file_size=config.MAX_DOWNLOAD_BYTES+1
        before=self.dump();await creator.handle_import_document(update,ctx)
        doc.get_file.assert_not_awaited();self.assertEqual(self.dump(),before)
    async def test_pdf_download_path_remains_unchanged(self):
        update,ctx,doc,file,_=self.fixture(b'PDF fixture',name='quiz.pdf',mime='application/pdf')
        # Stop after proving the existing, non-TXT path reached its original parser.
        with patch.object(creator,'_download_txt_bytes',new=AsyncMock()) as txt,patch.object(pdf_parser,'parse_pdf_result',side_effect=pdf_parser.SourceError('fixture end')):
            await creator.handle_import_document(update,ctx)
        txt.assert_not_awaited();doc.get_file.assert_awaited_once_with();file.download_as_bytearray.assert_awaited_once_with()
    async def test_two_supported_txt_styles_complete_import_after_retry(self):
        for i,text in enumerate((LEGACY,CHECKED)):
            with self.subTest(format=i):
                update,ctx,doc,file,_=self.fixture(text.encode());file.download_as_bytearray.side_effect=[TimedOut(),bytearray(text.encode())]
                db.set_state(701,'imp_wait',{'buffer':''})
                with patch.object(creator,'forward_txt_upload',new=AsyncMock()) as forward:
                    await creator.handle_import_document(update,ctx)
                forward.assert_awaited_once();self.assertEqual(forward.await_args.args[2],text.encode())
                state,data=db.get_state(701);self.assertEqual(state,'imp_review');self.assertFalse(data['errors'])
                update.callback_query=NS(data='i:create',answer=AsyncMock())
                await routes.callback_router(update,ctx);update.callback_query.data='i:usedefault';await routes.callback_router(update,ctx)
                self.assertIsNone(db.get_state(701)[0])
                self.assertEqual(len(db.get_owner_quizzes(701)),i+1)
    async def test_actual_307_question_file_download_retry_and_complete_import(self):
        path=Path('/home/user/uploads/dainik_vigyan_topicwise_mcq.txt')
        if not path.is_file():raise AssertionError('Required external actual TXT fixture missing; no test skipped')
        raw=path.read_bytes();update,ctx,doc,file,_=self.fixture(raw,name=path.name)
        file.download_as_bytearray.side_effect=[TimedOut(),bytearray(raw)]
        db.set_state(701,'imp_wait',{'buffer':''})
        with patch.object(creator,'forward_txt_upload',new=AsyncMock()) as forward:
            await creator.handle_import_document(update,ctx)
        self.assertEqual(forward.await_args.args[2],raw)
        _,data=db.get_state(701);self.assertFalse(data['errors']);self.assertEqual(len(data['questions']),307)
        update.callback_query=NS(data='i:create',answer=AsyncMock());await routes.callback_router(update,ctx)
        update.callback_query.data='i:usedefault';await routes.callback_router(update,ctx)
        quizzes=db.get_owner_quizzes(701);self.assertEqual(len(quizzes),1)
        self.assertEqual(db.count_questions(quizzes[0]['id']),307)
    async def test_cancellation_is_not_swallowed_or_retried(self):
        _,_,doc,_,_=self.fixture();started=asyncio.Event();release=asyncio.Event()
        async def blocked(**kwargs):
            started.set();await release.wait()
        doc.get_file.side_effect=blocked
        task=asyncio.create_task(creator._download_txt_bytes(doc))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(doc.get_file.await_count,1);self.backoff.assert_not_awaited()

    async def test_outer_deadline_retries_and_returns_compatible_timedout(self):
        _,_,doc,_,_=self.fixture()
        with patch.object(creator,'_TXT_DOWNLOAD_DEADLINE',0):
            with self.assertRaises(TimedOut):await creator._download_txt_bytes(doc)
        self.assertEqual(self.backoff.await_count,2)
    async def test_phase_logs_distinguish_metadata_and_transfer_without_exception_urls(self):
        for stage in ('getFile','download_as_bytearray'):
            _,_,doc,file,_=self.fixture()
            if stage=='getFile':doc.get_file.side_effect=NetworkError('https://private.invalid/secret')
            else:file.download_as_bytearray.side_effect=NetworkError('https://private.invalid/secret')
            with self.assertLogs(creator.log,level='WARNING') as logs:
                with self.assertRaises(NetworkError):await creator._download_txt_bytes(doc)
            self.assertIn('stage='+stage,'\n'.join(logs.output));self.assertNotIn('private.invalid','\n'.join(logs.output))


class RealTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ptb_httpx_read_timeout_is_reproduced_then_scoped_budgets_succeed(self):
        raw=LEGACY.encode();state={'delay_metadata':0,'delay_transfer':0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass  # never log synthetic token-shaped URL
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                time.sleep(state['delay_metadata'])
                body=json.dumps({'ok':True,'result':{'file_id':'fixture','file_unique_id':'unique','file_size':len(raw),'file_path':'quiz.txt'}}).encode()
                self.send_bytes(body)
            def do_GET(self):
                time.sleep(state['delay_transfer']);self.send_bytes(raw)
            def send_bytes(self,body):
                try:
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        request=HTTPXRequest(read_timeout=.05,connect_timeout=1,pool_timeout=1)
        base='http://127.0.0.1:'+str(server.server_port)
        bot=Bot('123456:offline-fixture-not-live',base_url=base+'/bot',base_file_url=base+'/file/bot',request=request)
        doc=Document('fixture','unique',file_name='quiz.txt',mime_type='text/plain',file_size=len(raw));doc.set_bot(bot)
        await request.initialize()
        try:
            for stage in ('delay_metadata','delay_transfer'):
                state.update(delay_metadata=0,delay_transfer=0);state[stage]=.15
                with self.subTest(stage=stage):
                    with self.assertRaises(TimedOut):
                        file=await doc.get_file();await file.download_as_bytearray()
                    self.assertEqual(await creator._download_txt_bytes(doc),raw)
        finally:
            await request.shutdown();server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
