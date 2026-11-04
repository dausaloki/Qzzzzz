"""Offline regression tests. Synthetic fixtures + external read-only TXT attachments; never contacts Telegram.
Attachment tests read QZZZZZ_TXT_TEST_DIR (default /home/user/uploads). The attachments are not bundled.
Run from the project root: python -m unittest discover -s tests -p test_independent_formats_support.py -v
"""
import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
import backup
import bot
import config
import database as db
import group_runner
import keyboards as kb
import quiz_creator as creator
import quiz_engine as engine
import quiz_runner

ADMIN, USER = 9001, 2001
LEGACY = "1. Synthetic question?\n(A) One\n(B) Two\n(C) Three\n(D) Four\nAnswer: B\nExplanation: Synthetic explanation."
CHECKED = "Synthetic checked question?\nA) One\nB) Two ✅\nC) Three\nD) Four\nEx: Synthetic explanation."
CUSTOM = "Q: Synthetic question\ncontinued stem\n| column | value |\nA) One\nB) Two ✅\ncontinued option\nC) Three\nD) Four\nEx: Synthetic explanation\ncontinued explanation"
# The checked marker belongs at the END of a multiline option, not inside it.
CUSTOM = CUSTOM.replace("B) Two ✅\ncontinued option", "B) Two\ncontinued option ✅")


def rendering_fixtures():
    base = {"id":1,"quiz_id":"fixture","number":7,"question":"Synthetic प्रश्न?", "options":["One","Two","Three","Four"], "correct_index":1,"explanation":"Because two."}
    fixtures = [base]
    for field, value in (
        ("question", "🧠" * 190 + "\n| A | B |\n| 1 | 2 |"),
        ("question", "Synthetic line\nstatement 1\nstatement 2"),
        ("question", "X" * 299),
        ("options", ["X"*150,"Two","Three","Four"]),
        ("options", ["One\ncontinued","Two","Three","Four"]),
        ("options", ["Same","Same","Three","Four"]),
        ("options", ["","Two","Three","Four"]),
        ("explanation", "विवरण\n"*300),
    ):
        q = copy.deepcopy(base)
        q[field] = value
        fixtures.append(q)
    fixtures.append({**base,"options":["Item "+str(i) for i in range(12)],"correct_index":11})
    fixtures.append({**base,"options":["One","Two"],"correct_index":0})
    return fixtures


def default_render_digest(module=engine):
    outputs = []
    for q in rendering_fixtures():
        n = len(q["options"])
        for perm in (list(range(n)),list(reversed(range(n))),list(range(1,n))+[0]):
            for builder in (module.build_poll_payload,module.compact_fallback):
                outputs.append(asdict(builder(q,perm,2,9)))
    raw = json.dumps(outputs,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode()).hexdigest(),len(outputs)


class DatabaseFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.get_db_path()
        db.set_db_path(Path(self.tmp.name)/"test.sqlite3")
        self.admin_patch = patch.object(config,"is_admin",lambda uid:uid==ADMIN)
        self.admin_patch.start()
        db.init_db()
        db.ensure_user_row(USER)
        db.ensure_user_row(ADMIN)

    def tearDown(self):
        db.set_db_path(self.old_path)
        self.admin_patch.stop()
        self.tmp.cleanup()

    def profile(self,kind="presentation",name="Synthetic profile",settings=None):
        return db.save_quiz_format(kind,name,settings or {},ADMIN)

    def qdict(self):
        return rendering_fixtures()[0]


class FormatTests(DatabaseFixture,unittest.TestCase):
    def test_default_rendering_matches_pre_feature_golden(self):
        digest,count = default_render_digest()
        self.assertEqual(count,66)
        self.assertEqual(digest,"fb26a258b6b018a9f414a57cf76a291438652b83287d1bd8e023c6029a04f037")
        for q in rendering_fixtures():
            perm = list(range(len(q["options"])))
            self.assertEqual(asdict(engine.build_poll_payload(q,perm,0,1)),asdict(engine.build_poll_payload(q,perm,0,1,presentation=None)))

    def test_legacy_and_checked_builtins_preserved(self):
        for source in (LEGACY,CHECKED):
            expected = engine.parse_txt_source(source)
            self.assertEqual(len(expected.questions),1)
            self.assertFalse(expected.errors)
            for fid in (db.BUILTIN_DEFAULT_FORMAT,db.BUILTIN_CHECKED_FORMAT):
                actual = creator.parse_txt_with_format(source,fid)
                self.assertEqual([q.to_dict() for q in actual.questions],[q.to_dict() for q in expected.questions])

    def test_independent_defaults_and_snapshot_edit_delete(self):
        existing = db.new_quiz(USER,"Old")
        presentation = self.profile(settings={"header":"Brand {quiz_title}","numbering":"roman"})
        importer = self.profile("import","Literal import")
        db.set_default_format("presentation",presentation,ADMIN)
        db.set_default_format("import",importer,ADMIN)
        new = db.new_quiz(USER,"New")
        snapshot = db.get_quiz(new)["presentation_json"]
        aid = db.create_attempt(new,USER,USER,[],45,False,1.0)
        sid = db.create_group_session(new,-10088,USER,[],45,False,1.0)
        self.assertEqual(db.get_attempt(aid)["presentation_json"],snapshot)
        self.assertEqual(db.get_group_session(sid)["presentation_json"],snapshot)
        db.save_quiz_format("presentation","Renamed",{"header":"Edited"},ADMIN,presentation)
        self.assertEqual(db.get_quiz(new)["presentation_json"],snapshot)
        db.delete_quiz_format(presentation,"presentation",ADMIN)
        self.assertIsNone(db.get_quiz(existing)["presentation_json"])
        self.assertEqual(db.get_quiz(new)["presentation_json"],snapshot)
        self.assertEqual(db.get_default_format("presentation"),db.BUILTIN_DEFAULT_FORMAT)
        self.assertEqual(db.get_default_format("import"),importer)
        self.assertEqual(db.get_attempt(aid)["timer"],45)
        self.assertFalse(db.get_attempt(aid)["shuffle_options"])

    def test_multiple_profiles_and_kind_isolation(self):
        one = self.profile(name="One")
        two = self.profile(name="Two")
        three = self.profile("import","One")
        self.assertEqual(len(db.list_quiz_formats("presentation")),2)
        self.assertEqual(len(db.list_quiz_formats("import")),1)
        self.assertIsNone(db.get_quiz_format(three,"presentation"))
        db.set_default_format("presentation",two,ADMIN)
        self.assertEqual(db.get_default_format("presentation"),two)
        self.assertNotEqual(one,two)
        with self.assertRaises(ValueError):
            db.set_default_format("presentation",three,ADMIN)

    def test_builtins_immutable_and_mutations_admin_only(self):
        for kind in ("presentation","import"):
            with self.assertRaises(ValueError):
                db.save_quiz_format(kind,"Hijack",{},ADMIN,db.BUILTIN_DEFAULT_FORMAT)
            with self.assertRaises(ValueError):
                db.delete_quiz_format(db.BUILTIN_DEFAULT_FORMAT,kind,ADMIN)
            with self.assertRaises(PermissionError):
                db.save_quiz_format(kind,"Unauthorized",{},USER)
            with self.assertRaises(PermissionError):
                db.set_default_format(kind,db.BUILTIN_DEFAULT_FORMAT,USER)
        with self.assertRaises(PermissionError):
            db.set_support_group({"ref":None},USER)

    def test_import_selection_fallback_and_delete(self):
        fid = self.profile("import")
        db.set_default_format("import",fid,ADMIN)
        self.assertEqual(db.effective_import_format(USER),fid)
        db.update_user_settings(USER,txt_import_format_id=db.BUILTIN_DEFAULT_FORMAT)
        self.assertEqual(db.effective_import_format(USER),db.BUILTIN_DEFAULT_FORMAT)
        db.update_user_settings(USER,txt_import_format_id=fid)
        db.delete_quiz_format(fid,"import",ADMIN)
        self.assertIsNone(db.get_user(USER)["txt_import_format_id"])
        self.assertEqual(db.effective_import_format(USER),db.BUILTIN_DEFAULT_FORMAT)

    def test_custom_import_preserves_multiline_table_and_order(self):
        fid = self.profile("import")
        res = creator.parse_txt_with_format(CUSTOM+"\n\n"+CUSTOM.replace("Synthetic question","Second question"),fid)
        self.assertFalse(res.errors)
        self.assertEqual([q.number for q in res.questions],[1,2])
        first = res.questions[0]
        self.assertEqual(first.question,"Synthetic question\ncontinued stem\n| column | value |")
        self.assertEqual(first.options,["One","Two\ncontinued option","Three","Four"])
        self.assertEqual(first.correct_index,1)
        self.assertEqual(first.explanation,"Synthetic explanation\ncontinued explanation")
        self.assertTrue(res.questions[1].question.startswith("Second question"))

    def test_custom_alternative_literal_grammar_and_explicit_answer(self):
        settings={"question_prefix":"Question:","option_labels":["1>","2>","3>"],"correct_markers":[],"answer_prefixes":["Key:"],"explanation_prefixes":["Why:"]}
        res = engine.parse_custom_txt_source("Question: Pick\n1> A\n2> B\n3> C\nKey: 2\nWhy: B",settings)
        self.assertFalse(res.errors)
        self.assertEqual(res.questions[0].correct_index,1)
        self.assertEqual(res.questions[0].explanation,"B")

    def test_custom_import_never_guesses_or_silently_accepts_bad_blocks(self):
        for source in (CUSTOM.replace(" ✅",""),CUSTOM.replace("A) One","A) One ✅"),
                       CUSTOM.replace("D) Four\n",""),CUSTOM.replace("B) Two","C) Two"),
                       CUSTOM.replace("Ex:","Answer: D\nEx:")):
            result=engine.parse_custom_txt_source(source,{})
            self.assertFalse(result.questions)
            self.assertEqual(len(result.errors),1)
        good_bad = engine.parse_custom_txt_source(CUSTOM+"\nQ: Bad\nA) Only",{})
        self.assertEqual(len(good_bad.questions),1)
        self.assertEqual(len(good_bad.errors),1)
        self.assertEqual(good_bad.detected_blocks,2)

    def test_restricted_configuration_schema_no_execution_or_behavior_fields(self):
        for settings in ({"timer":90},{"shuffle":True},{"heading":"{number.__class__}"},{"header":"{number!r}"},{"heading":"{number:>5}"},{"option_labels":["x","x"]}):
            with self.assertRaises((ValueError,TypeError)):
                engine.validate_format_settings("presentation",settings)
        for settings in ({"regex":".*"},{"question_prefix":"A)"},{"option_labels":["A","AB"]},{"correct_markers":[],"answer_prefixes":[]}):
            with self.assertRaises(ValueError):
                engine.validate_format_settings("import",settings)

    def test_custom_display_full_source_permutation_explanation_and_limits(self):
        q=self.qdict()
        q.update(question="| table | value |\n"+"🧠"*500,options=["First\ncontinued","X"*220,"Third","Fourth"],explanation="Long explanation\n"*100)
        profile=engine.validate_format_settings("presentation",{"header":"Brand {quiz_title}","heading":"Item {number}/{total}","footer":"End {quiz_id}","numbering":"roman","option_labels":["①","②","③","④"]})
        perm=[3,1,0,2]
        original=copy.deepcopy(q)
        for builder in (engine.build_poll_payload,engine.compact_fallback):
            payload=builder(q,perm,1,4,presentation=profile,quiz_title="Fixture")
            full="".join(payload.full_text_chunks)
            for value in (q["question"],*q["options"],"Brand Fixture","Item II/4","End fixture"):
                self.assertIn(value,full)
            self.assertLess(full.index("Fourth"),full.index("First"))
            self.assertEqual(payload.correct_option_id,1)
            self.assertEqual(payload.perm,perm)
            self.assertEqual(payload.post_explanation,q["explanation"].strip())
            self.assertLessEqual(engine.tg_len(payload.question),config.POLL_QUESTION_MAX)
            self.assertTrue(all(0<engine.tg_len(x)<=config.POLL_OPTION_MAX and "\n" not in x for x in payload.options))
        self.assertEqual(q,original)

    def test_decimal_roman_none_and_safe_bad_snapshot(self):
        self.assertEqual(engine._presentation_number(9,"roman"),"IX")
        self.assertEqual(engine._presentation_number(9,"decimal"),"9")
        self.assertEqual(engine._presentation_number(9,"none"),"")
        self.assertIsNone(engine.presentation_from({}))
        self.assertIsNone(engine.presentation_from({"presentation_json":"broken"}))

    def test_public_owner_support_and_admin_visibility(self):
        def buttons(markup):
            return [b for row in markup.inline_keyboard for b in row]
        self.assertIn("https://t.me/LOKESH_DAUSA",[b.url for b in buttons(kb.main_menu())])
        public=buttons(kb.settings_menu(db.get_user(USER),"https://t.me/Dausa_123"))
        self.assertIn("https://t.me/LOKESH_DAUSA",[b.url for b in public])
        self.assertIn("https://t.me/Dausa_123",[b.url for b in public])
        self.assertNotIn("fm:support",[b.callback_data for b in public])
        admin=buttons(kb.settings_menu(db.get_user(ADMIN)))
        self.assertIn("fm:support",[b.callback_data for b in admin])
        self.assertNotIn("https://t.me/Dausa_123",[b.url for b in public if b.text=="👤 Owner"])
        self.assertFalse(any(b.text=="👥 Support Group" for b in buttons(kb.settings_menu(db.get_user(USER),None))))

    def test_additive_upgrade_preserves_legacy_rows(self):
        qid=db.new_quiz(USER,"Existing title")
        # Remove only new columns/tables to model an old database; no production DB is used.
        with db.connect() as con:
            for table,col in (("users","txt_import_format_id"),("quizzes","presentation_json"),("attempts","presentation_json"),("group_sessions","presentation_json")):
                con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            con.execute("DROP TABLE quiz_formats")
            con.execute("DROP TABLE txt_upload_deliveries")
        db.init_db();db.init_db()
        self.assertEqual(db.get_quiz(qid)["title"],"Existing title")
        self.assertIsNone(db.get_quiz(qid)["presentation_json"])
        self.assertEqual(db.get_user(USER)["user_id"],USER)

    def test_full_backup_roundtrip_idempotence_and_older_backup(self):
        p=self.profile()
        i=self.profile("import","Importer")
        db.set_default_format("presentation",p,ADMIN)
        qid=db.new_quiz(USER,"Snapshot")
        aid=db.create_attempt(qid,USER,USER,[],45,False,1.0)
        sid=db.create_group_session(qid,-10099,USER,[],45,False,1.0)
        db.update_user_settings(USER,txt_import_format_id=i)
        db.set_support_group({"ref":"-10099","chat_id":-10099,"url":"https://t.me/+private_fixture"},ADMIN)
        db.claim_txt_forward("fixture-event",USER,"a"*64,-10099)
        db.finish_txt_forward("fixture-event","sent",88)
        payload=backup.build_payload()
        self.assertFalse(backup.validate_payload(payload))
        target=Path(self.tmp.name)/"restored.sqlite3"
        first=backup.restore_payload(payload,db_file=target,apply=True)
        self.assertTrue(first["ok"],first)
        again=backup.restore_payload(payload,db_file=target,apply=True)
        self.assertTrue(again["ok"],again)
        self.assertFalse(any(again["inserted"].values()))
        with sqlite3.connect(target) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM quiz_formats").fetchone()[0],2)
            self.assertEqual(con.execute("SELECT status FROM txt_upload_deliveries").fetchone()[0],"sent")
            for table,rid in (("quizzes",qid),("attempts",aid),("group_sessions",sid)):
                self.assertEqual(con.execute(f"SELECT presentation_json FROM {table} WHERE id=?",(rid,)).fetchone()[0],db.get_quiz(qid)["presentation_json"])
        old=copy.deepcopy(payload)
        for name in ("quiz_formats","txt_upload_deliveries"):
            old.pop(name);old["counts"].pop(name)
        self.assertTrue(backup.restore_payload(old,db_file=target,apply=True)["ok"])
        with sqlite3.connect(target) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM quiz_formats").fetchone()[0],2)

    def test_github_redaction_no_support_private_metadata(self):
        self.profile()
        db.set_support_group({"ref":"-100999","chat_id":-100999,"url":"https://t.me/+PRIVATE_ONLY"},ADMIN)
        db.claim_txt_forward("PRIVATE_EVENT_123",USER,"a"*64,-100999)
        redacted=backup.github_payload(backup.build_payload())
        raw=json.dumps(redacted)
        self.assertNotIn("PRIVATE_ONLY",raw)
        self.assertNotIn("PRIVATE_EVENT_123",raw)
        self.assertNotIn("-100999",raw)
        self.assertFalse(redacted["txt_upload_deliveries"])
        self.assertIsNone(redacted["quiz_formats"][0]["created_by"])
        target=Path(self.tmp.name)/"redacted.sqlite3"
        self.assertFalse(backup.restore_payload(redacted,db_file=target,apply=True)["ok"])
        self.assertFalse(target.exists())

    def test_restore_refuses_schema_that_would_drop_snapshots(self):
        p=self.profile();db.set_default_format("presentation",p,ADMIN)
        db.new_quiz(USER,"Snapshot")
        payload=backup.build_payload()
        with db.connect() as con:
            con.execute("ALTER TABLE quizzes DROP COLUMN presentation_json")
        result=backup.restore_payload(payload,apply=True)
        self.assertFalse(result["ok"])
        self.assertIn("quizzes.presentation_json",result["schema_outdated"])

    def test_delivery_ledger_atomic_and_persistent(self):
        self.assertTrue(db.claim_txt_forward("one",USER,"d"*64,-1001))
        self.assertFalse(db.claim_txt_forward("one",USER,"d"*64,-1001))
        self.assertFalse(db.claim_txt_forward("two",USER,"d"*64,-1001))
        db.finish_txt_forward("one","failed")
        self.assertFalse(db.claim_txt_forward("one",USER,"d"*64,-1001))
        self.assertTrue(db.claim_txt_forward("two",USER,"d"*64,-1001))
        db.finish_txt_forward("two","uncertain")
        db.init_db()
        self.assertFalse(db.claim_txt_forward("three",USER,"d"*64,-1001))
        self.assertTrue(db.claim_txt_forward("different-user",ADMIN,"d"*64,-1001))
        self.assertTrue(db.claim_txt_forward("different-target",USER,"d"*64,-1002))

    def test_concurrent_claims_allow_only_one_copy(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes=list(pool.map(lambda i:db.claim_txt_forward("concurrent-"+str(i),USER,"c"*64,-1007),range(20)))
        self.assertEqual(sum(outcomes),1)
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM txt_upload_deliveries").fetchone()[0],1)

    def test_checked_builtin_cannot_be_overwritten_or_deleted(self):
        with self.assertRaises(ValueError):
            db.save_quiz_format("import","Overwrite",{},ADMIN,db.BUILTIN_CHECKED_FORMAT)
        with self.assertRaises(ValueError):
            db.delete_quiz_format(db.BUILTIN_CHECKED_FORMAT,"import",ADMIN)
        db.set_default_format("import",db.BUILTIN_CHECKED_FORMAT,ADMIN)
        self.assertEqual(db.get_default_format("import"),db.BUILTIN_CHECKED_FORMAT)



class AsyncTests(DatabaseFixture,unittest.IsolatedAsyncioTestCase):
    def update(self,uid=USER,name="sample.txt",mime="text/plain",raw=CHECKED.encode(),text=None):
        file=NS(download_as_bytearray=AsyncMock(return_value=bytearray(raw)))
        doc=NS(file_name=name,mime_type=mime,file_size=len(raw),get_file=AsyncMock(return_value=file))
        msg=NS(document=doc,text=text,caption=None,message_id=77,date=datetime(2026,1,2,3,4,5,tzinfo=timezone.utc),reply_text=AsyncMock(),edit_text=AsyncMock())
        chat=NS(id=uid,type="private",send_message=AsyncMock(),send_document=AsyncMock())
        user=NS(id=uid,username="fixture_user",first_name="Fixture",last_name="")
        callback=NS(data="",answer=AsyncMock(),message=msg,edit_message_text=AsyncMock())
        return NS(effective_user=user,effective_message=msg,effective_chat=chat,callback_query=callback)

    def telegram(self,status="member",can_send=True,username="RealFixture",invite=None):
        permissions=NS(can_send_messages=can_send,can_send_documents=can_send)
        chat=NS(id=-100123456,type="supergroup",username=username,invite_link=invite,permissions=permissions)
        member=NS(status=status,is_member=True,can_send_messages=can_send,can_send_documents=can_send)
        client=NS(id=999,get_chat=AsyncMock(return_value=chat),get_chat_member=AsyncMock(return_value=member),send_document=AsyncMock(return_value=NS(message_id=88)))
        return client,chat,member

    async def test_real_resolution_and_permission_failures(self):
        client,chat,member=self.telegram()
        resolved=await creator.resolve_support_group(client,"https://t.me/RealFixture")
        self.assertEqual(resolved,{"ref":"@RealFixture","chat_id":chat.id,"url":"https://t.me/RealFixture"})
        client.get_chat.assert_awaited_with("@RealFixture")
        for status,can_send in (("left",True),("kicked",True),("member",False),("restricted",False)):
            member.status=status;member.can_send_documents=can_send;chat.permissions.can_send_documents=can_send
            with self.assertRaises(ValueError):
                await creator.resolve_support_group(client,"@RealFixture")
        member.status="administrator";chat.type="channel"
        with self.assertRaises(ValueError):
            await creator.resolve_support_group(client,"@RealFixture")

    async def test_private_group_requires_real_url_never_invents(self):
        client,chat,_=self.telegram(username=None)
        with self.assertRaises(ValueError):
            await creator.resolve_support_group(client,str(chat.id))
        chat.invite_link="https://t.me/+REAL_API_FIXTURE"
        result=await creator.resolve_support_group(client,str(chat.id))
        self.assertEqual(result["url"],chat.invite_link)
        self.assertEqual(result["chat_id"],chat.id)
        client.get_chat.assert_awaited_with(chat.id)
        with self.assertRaises(ValueError):
            await creator.resolve_support_group(client,chat.invite_link)

    async def test_original_bytes_metadata_and_dedup_across_restart(self):
        update=self.update(raw=b'\xef\xbb\xbfunchanged\r\nbytes\x00')
        client,_,_=self.telegram()
        context=NS(bot=client)
        raw=b'\xef\xbb\xbfunchanged\r\nbytes\x00'
        self.assertTrue(await creator.forward_txt_upload(update,context,raw,"original.txt"))
        sent=client.send_document.await_args.kwargs
        self.assertEqual(sent["document"].input_file_content,raw)
        self.assertEqual(sent["document"].filename,"original.txt")
        for token in (str(USER),"@fixture_user","original.txt","2026-01-02T03:04:05+00:00"):
            self.assertIn(token,sent["caption"])
        self.assertEqual(db.support_group_config()["chat_id"],-100123456)
        db.init_db()
        self.assertFalse(await creator.forward_txt_upload(update,context,raw,"original.txt"))
        update.effective_message.message_id+=1
        self.assertFalse(await creator.forward_txt_upload(update,context,raw,"again.txt"))
        self.assertEqual(client.send_document.await_count,1)

    async def test_definite_failure_separate_upload_retry_and_timeout_suppression(self):
        update=self.update();client,_,_=self.telegram();context=NS(bot=client)
        client.send_document.side_effect=BadRequest("documents prohibited")
        self.assertFalse(await creator.forward_txt_upload(update,context,b"one","x.txt"))
        self.assertFalse(await creator.forward_txt_upload(update,context,b"one","x.txt"))
        self.assertEqual(client.send_document.await_count,1)
        update.effective_message.message_id+=1
        client.send_document.side_effect=None
        self.assertTrue(await creator.forward_txt_upload(update,context,b"one","x.txt"))
        update.effective_message.message_id+=1
        client.send_document.side_effect=NetworkError("ambiguous timeout")
        self.assertFalse(await creator.forward_txt_upload(update,context,b"two","x.txt"))
        update.effective_message.message_id+=1
        client.send_document.side_effect=None
        self.assertFalse(await creator.forward_txt_upload(update,context,b"two","x.txt"))
        self.assertEqual(client.send_document.await_count,3)

    async def test_disabled_and_unresolved_target_never_send(self):
        update=self.update();client,_,_=self.telegram();context=NS(bot=client)
        client.get_chat.side_effect=BadRequest("not found")
        self.assertFalse(await creator.forward_txt_upload(update,context,b"x","x.txt"))
        db.set_support_group({"ref":None,"chat_id":None,"url":None},ADMIN)
        client.get_chat.reset_mock()
        self.assertFalse(await creator.forward_txt_upload(update,context,b"x","x.txt"))
        client.get_chat.assert_not_awaited();client.send_document.assert_not_awaited()

    async def test_admin_target_change_during_resolution_aborts_send(self):
        update=self.update();client,_,_=self.telegram();context=NS(bot=client)
        async def resolve(*args):
            db.set_support_group({"ref":None,"chat_id":None,"url":None},ADMIN)
            return {"ref":"@Dausa_123","chat_id":-100123456,"url":"https://t.me/Dausa_123"}
        with patch.object(creator,"resolve_support_group",side_effect=resolve):
            self.assertFalse(await creator.forward_txt_upload(update,context,b"x","x.txt"))
        client.send_document.assert_not_awaited()

    async def test_txt_document_route_uses_selected_parser_and_copy_failure_does_not_block(self):
        fid=self.profile("import")
        db.update_user_settings(USER,txt_import_format_id=fid)
        update=self.update(raw=CUSTOM.encode())
        update.callback_query=None
        client,_,_=self.telegram()
        client.send_document.side_effect=Forbidden("cannot send")
        with patch.object(creator,"_present_batch_review",new_callable=AsyncMock) as review:
            await creator.handle_import_document(update,NS(bot=client))
            review.assert_awaited_once()
            entry=review.await_args.args[2][0]
            self.assertTrue(entry)
            self.assertEqual(client.send_document.await_count,1)

    async def test_non_txt_documents_and_pasted_txt_not_forwarded(self):
        for filename,mime in (("a.pdf","application/pdf"),("a.html","text/html"),("a.json","application/json")):
            self.assertFalse(creator.is_txt_document(self.update(name=filename,mime=mime).effective_message.document))
        update=self.update(text=CHECKED);update.effective_message.document=None;update.callback_query=None
        client,_,_=self.telegram()
        with patch.object(creator,"_present_batch_review",new_callable=AsyncMock),patch.object(creator,"forward_txt_upload",new_callable=AsyncMock) as copy_upload:
            await creator._parse_and_review(update,USER,CHECKED,"Pasted")
            copy_upload.assert_not_awaited()
            client.send_document.assert_not_awaited()

    async def test_callbacks_and_persisted_wizards_enforce_admin(self):
        update=self.update()
        for data in ("fm:new:presentation","fm:new:import","fm:default:import:builtin_default","fm:support_disable","fm:list:presentation"):
            update.callback_query.data=data
            await bot.format_settings_callback(update,NS())
            self.assertIn("Admin",update.callback_query.answer.await_args.args[0])
        db.set_state(USER,"fmt_save",{"kind":"presentation"})
        await bot.format_settings_message(update,NS(),"fmt_save",{"kind":"presentation"})
        self.assertFalse(db.list_quiz_formats("presentation"))
        self.assertIsNone(db.get_state(USER)[0])

    async def test_admin_crud_default_and_preview_without_quiz_creation(self):
        update=self.update(ADMIN,text=json.dumps({"name":"Wizard profile","settings":{"header":"Brand"}}))
        update.effective_message.document=None
        await bot.format_settings_message(update,NS(),"fmt_save",{"kind":"presentation"})
        profile=db.list_quiz_formats("presentation")[0]
        update.callback_query.data=f"fm:default:presentation:{profile['id']}"
        await bot.format_settings_callback(update,NS())
        self.assertEqual(db.get_default_format("presentation"),profile["id"])
        update.callback_query.data=f"fm:preview:presentation:{profile['id']}"
        await bot.format_settings_callback(update,NS())
        state,data=db.get_state(ADMIN)
        self.assertEqual(state,"fmt_preview")
        update.effective_message.text=CHECKED
        await bot.format_settings_message(update,NS(),state,data)
        update.effective_chat.send_document.assert_awaited()
        self.assertTrue(update.effective_message.reply_text.await_count)
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0],0)
        update.callback_query.data=f"fm:confirm_delete:presentation:{profile['id']}"
        await bot.format_settings_callback(update,NS())
        self.assertFalse(db.list_quiz_formats("presentation"))

    async def test_wizard_resumes_existing_import_state(self):
        db.set_state(ADMIN,"imp_review",{"batch_files":[{"name":"retained.txt"}]})
        bot._format_enter_state(ADMIN,"fmt_save",{"kind":"import"})
        bot._format_enter_state(ADMIN,"fmt_preview",{"kind":"import","id":db.BUILTIN_DEFAULT_FORMAT})
        bot._format_leave_state(ADMIN)
        self.assertEqual(db.get_state(ADMIN),("imp_review",{"batch_files":[{"name":"retained.txt"}]}))

    async def test_private_and_group_runners_pass_snapshots_without_rule_changes(self):
        fid=self.profile(settings={"header":"Snapshot brand"})
        db.set_default_format("presentation",fid,ADMIN)
        qid=db.new_quiz(USER,"Snapshot quiz")
        aid=db.create_attempt(qid,USER,USER,[1],45,False,1.0)
        sid=db.create_group_session(qid,-10087,USER,[1],45,False,1.0)
        q=self.qdict();q["quiz_id"]=qid
        # Stop after payload construction: this isolates real runner render wiring without jobs/network.
        class StopRender(Exception):
            pass
        def capture(*args,**kwargs):
            self.assertEqual(kwargs["presentation"]["header"],"Snapshot brand")
            self.assertEqual(kwargs["quiz_title"],"Snapshot quiz")
            raise StopRender()
        app=NS(bot=NS())
        with patch.object(db,"get_question",return_value=q),patch.object(engine,"build_poll_payload",side_effect=capture),patch.object(engine,"compact_fallback",side_effect=capture):
            with self.assertLogs("quiz_runner",level="ERROR"),self.assertRaises(StopRender):
                await quiz_runner.send_current(app,aid)
            with self.assertLogs("group_runner",level="ERROR"),self.assertRaises(StopRender):
                await group_runner._send_current(app,sid)
        self.assertEqual(db.get_attempt(aid)["timer"],45)
        self.assertEqual(db.get_group_session(sid)["timer"],45)

    async def test_private_group_native_send_and_rejection_fallback_keep_rules(self):
        fid=self.profile(settings={"header":"Visible brand","option_labels":["①","②","③","④"]})
        db.set_default_format("presentation",fid,ADMIN)
        qid=db.new_quiz(USER,"Native fixture")
        question_id=db.add_question(qid,"Synthetic native question",["First","Second","Third","Fourth"],1,"Synthetic explanation")
        aid=db.create_attempt(qid,USER,USER,[question_id],45,True,1.0)
        sid=db.create_group_session(qid,-10087,USER,[question_id],45,True,1.0,negative_mark=0.25)
        perm=[3,1,0,2]
        for module,rid,send in ((quiz_runner,aid,quiz_runner.send_current),(group_runner,sid,group_runner._send_current)):
            result=NS(poll=NS(id="fixture_poll_"+module.__name__),message_id=81)
            client=NS(send_message=AsyncMock(),send_poll=AsyncMock(side_effect=[BadRequest("force compact fallback"),result]))
            with patch.object(module,"_schedule") as schedule,patch.object(engine,"make_permutation",return_value=perm):
                await send(NS(bot=client),rid)
            self.assertEqual(client.send_poll.await_count,2)
            for call in client.send_poll.await_args_list:
                self.assertEqual(call.kwargs["open_period"],45)
                self.assertEqual(call.kwargs["type"],"quiz")
                self.assertEqual(call.kwargs["correct_option_ids"],[1])
            self.assertEqual(client.send_poll.await_args.kwargs["options"],["①","②","③","④"])
            self.assertIn("Visible brand","".join(c.args[1] for c in client.send_message.await_args_list))
            self.assertTrue(schedule.called)
        self.assertEqual(db.get_attempt(aid)["cur_perm"],perm)
        self.assertEqual(db.get_group_session(sid)["negative_mark"],0.25)

    async def test_support_admin_save_and_disable_require_valid_api_result(self):
        update=self.update(ADMIN,text="@RealFixture")
        update.effective_message.document=None
        client,chat,_=self.telegram()
        await bot.format_settings_message(update,NS(bot=client),"fmt_support",{})
        self.assertEqual(db.support_group_config()["chat_id"],chat.id)
        before=db.support_group_config()
        client.get_chat.side_effect=BadRequest("not found")
        await bot.format_settings_message(update,NS(bot=client),"fmt_support",{})
        self.assertEqual(db.support_group_config(),before)
        update.callback_query.data="fm:support_disable"
        await bot.format_settings_callback(update,NS(bot=client))
        self.assertIsNone(db.support_group_config()["url"])
        self.assertIsNone(db.support_group_config()["ref"])

    async def test_named_import_preview_for_non_admin_and_paste_selected_route(self):
        fid=self.profile("import")
        db.update_user_settings(USER,txt_import_format_id=fid)
        update=self.update(text=CUSTOM);update.effective_message.document=None
        update.callback_query.data=f"fm:preview:import:{fid}"
        await bot.format_settings_callback(update,NS())
        state,data=db.get_state(USER)
        self.assertEqual(state,"fmt_preview")
        await bot.format_settings_message(update,NS(),state,data)
        update.effective_chat.send_document.assert_awaited_once()
        with patch.object(creator,"parse_txt_for_user",wraps=creator.parse_txt_for_user) as selected_parser:
            await creator._parse_and_review(update,USER,CUSTOM,"Custom pasted")
            selected_parser.assert_called_once_with(USER,CUSTOM)
        imported_state,imported_data=db.get_state(USER)
        self.assertEqual(imported_state,"imp_review")
        self.assertEqual(len(imported_data["questions"]),1)
        self.assertEqual(imported_data["questions"][0]["correct_index"],1)
        self.assertEqual(imported_data["questions"][0]["options"][1],"Two\ncontinued option")
        self.assertFalse(imported_data["errors"])

    async def test_settings_json_upload_supports_long_custom_templates(self):
        sample={"name":"Long template","settings":{"header":"H"*800,"footer":"F"*800,"branding":"B"*800,"accompanying_text":"A"*800}}
        update=self.update(ADMIN,name="named_format.json",mime="application/json",raw=json.dumps(sample).encode())
        with patch.object(creator,"forward_txt_upload",new_callable=AsyncMock) as forward:
            await bot.format_settings_message(update,NS(),"fmt_save",{"kind":"presentation"})
            forward.assert_not_awaited()
        profile=db.list_quiz_formats("presentation")[0]
        self.assertEqual(json.loads(profile["settings_json"])["header"],"H"*800)
        update.callback_query.data=f"fm:edit:presentation:{profile['id']}"
        await bot.format_settings_callback(update,NS())
        self.assertEqual(db.get_state(ADMIN)[0],"fmt_save")
        update.effective_chat.send_document.assert_awaited()

    async def test_timeout_retry_after_and_missing_username_handled(self):
        update=self.update();update.effective_user.username=None
        client,_,_=self.telegram();context=NS(bot=client)
        client.send_document.side_effect=RetryAfter(1)
        self.assertFalse(await creator.forward_txt_upload(update,context,b"rate-limited","x.txt"))
        update.effective_message.message_id+=1
        client.send_document.side_effect=None
        self.assertTrue(await creator.forward_txt_upload(update,context,b"rate-limited","x.txt"))
        self.assertIn("(not set)",client.send_document.await_args.kwargs["caption"])
        update.effective_message.message_id+=1
        client.send_document.side_effect=asyncio.TimeoutError()
        self.assertFalse(await creator.forward_txt_upload(update,context,b"timeout","x.txt"))
        update.effective_message.message_id+=1
        self.assertFalse(await creator.forward_txt_upload(update,context,b"timeout","x.txt"))
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT status FROM txt_upload_deliveries WHERE sha256=?",(hashlib.sha256(b"timeout").hexdigest(),)).fetchone()[0],"uncertain")

    async def test_txt_sent_during_other_wizard_still_forwarded_without_state_change(self):
        update=self.update();db.set_state(USER,"c_q",{"quiz_id":"retained"})
        client,_,_=self.telegram()
        with patch.object(creator,"handle_creator_message",new_callable=AsyncMock) as handler:
            await bot.private_message_router(update,NS(bot=client))
            handler.assert_awaited_once()
        client.send_document.assert_awaited_once()
        self.assertEqual(db.get_state(USER),("c_q",{"quiz_id":"retained"}))


    async def test_actual_non_txt_import_branches_never_copy(self):
        import pdf_parser
        for filename,mime in (("fixture.pdf","application/pdf"),("fixture.html","text/html"),("fixture.json","application/json")):
            update=self.update(ADMIN,name=filename,mime=mime,raw=b"fixture");update.callback_query=None
            client,_,_=self.telegram()
            with patch.object(config,"HTML_IMPORT_ADMIN_ONLY",False),patch.object(creator,"forward_txt_upload",new_callable=AsyncMock) as forward,patch.object(creator,"_present_batch_review",new_callable=AsyncMock),patch.object(pdf_parser,"parse_pdf_result",return_value=pdf_parser.ParseResult()),patch.object(creator,"_extract_html",new_callable=AsyncMock,return_value=(pdf_parser.ParseResult(),[])),patch("ai_gen.parse_json_quiz_report",return_value=("Synthetic",[],[])):
                await creator.handle_import_document(update,NS(bot=client))
                forward.assert_not_awaited()
                client.send_document.assert_not_awaited()



# New-format oracle deliberately uses the attachment's promotional question
# headings, NOT the production parser's A)-run question-boundary algorithm.
_ATTACHMENT_PROMO = " 𝐉𝐔𝐒𝐓 𝐂𝐑𝐀𝐂𝐊 𝐈𝐓!! 📚"


def attachment_path(name):
    path=Path(os.environ.get("QZZZZZ_TXT_TEST_DIR","/home/user/uploads"))/name
    if not path.is_file():
        raise FileNotFoundError(f"Provide the real attachment {name!r} in QZZZZZ_TXT_TEST_DIR; no synthetic substitute is used")
    return path


def actual_attachment_oracle():
    lines=attachment_path("message (1).txt").read_text(encoding="utf-8-sig").splitlines()
    starts=[i for i,line in enumerate(lines) if line.endswith(_ATTACHMENT_PROMO)]
    expected=[]
    for number,start in enumerate(starts,1):
        stop=starts[number] if number<len(starts) else len(lines)
        rows=lines[start:stop]
        while rows and not rows[-1].strip():
            rows.pop()
        option_rows=[i for i,line in enumerate(rows) if line.lstrip()[:2] in ("A)","B)","C)","D)")]
        if len(option_rows)!=4:
            raise AssertionError(f"Source question {number} does not have four option rows")
        stem=[rows[0][:-len(_ATTACHMENT_PROMO)]]+rows[1:option_rows[0]]
        while stem and not stem[-1].strip():
            stem.pop()
        options=[];marked=[]
        for index,pos in enumerate(option_rows):
            label,sep,body=rows[pos].lstrip().partition(")")
            if label!="ABCD"[index] or not sep:
                raise AssertionError(f"Source option order invalid at question {number}")
            body=body.lstrip(" \t")
            if "✅" in body:
                if not body.endswith(" ✅") or body.count("✅")!=1:
                    raise AssertionError("Real attachment marker is not the observed suffix structure")
                body=body[:-2]
                marked.append(index)
            options.append(body)
        if len(marked)!=1:
            raise AssertionError(f"Source question {number} does not contain exactly one check")
        expected.append({"number":number,"question":"\n".join(stem),"options":options,
                         "correct_index":marked[0],"explanation":"","raw_block":"\n".join(rows)})
    if len(expected)!=113:
        raise AssertionError(f"Expected actual 113-card reference, got {len(expected)}")
    return expected


class UnnumberedAttachmentTests(DatabaseFixture,unittest.TestCase):
    def assert_reference_equal(self,result,expected):
        self.assertEqual(result.detected_blocks,len(expected))
        self.assertEqual(len(result.questions),len(expected))
        self.assertFalse(result.errors)
        self.assertFalse(result.review)
        for actual,reference in zip(result.questions,expected):
            for field in ("number","question","options","correct_index","explanation"):
                self.assertEqual(getattr(actual,field),reference[field],f"Question {reference['number']}: {field}")

    def test_actual_113_questions_exact_against_independent_source_oracle(self):
        expected=actual_attachment_oracle()
        text=attachment_path("message (1).txt").read_text(encoding="utf-8-sig")
        result=creator.parse_txt_with_format(text,db.BUILTIN_UNNUMBERED_CHECKED_FORMAT)
        self.assert_reference_equal(result,expected)
        self.assertEqual(len({q.question for q in result.questions}),113)
        self.assertEqual(sum(len(q.options) for q in result.questions),452)
        self.assertEqual(len(result.removed),113)
        self.assertEqual(sum("|" in q.question for q in result.questions),3)
        source_tables=[line for line in text.splitlines() if "|" in line]
        parsed_tables=[line for q in result.questions for line in q.question.splitlines() if "|" in line]
        self.assertEqual(parsed_tables,source_tables)
        source_statements=[line for line in text.splitlines() if line.startswith(("1.","2.","3.","4."))]
        parsed_statements=[line for q in result.questions for line in q.question.splitlines() if line.startswith(("1.","2.","3.","4."))]
        self.assertEqual(parsed_statements,source_statements)
        self.assertEqual(sum(any(line.startswith("1.") for line in q.question.splitlines()) for q in result.questions),9)

    def test_each_actual_question_missing_or_conflicting_marker_is_flagged(self):
        expected=actual_attachment_oracle()
        blocks=[r["raw_block"] for r in expected]
        for index,reference in enumerate(expected):
            for mode in ("missing","conflicting"):
                mutated=list(blocks)
                if mode=="missing":
                    mutated[index]=mutated[index].replace("✅","",1)
                else:
                    other="ABCD"[(reference["correct_index"]+1)%4]+") "
                    mutated[index]=mutated[index].replace(other,other+"✅ ",1)
                result=engine.parse_unnumbered_checked_txt("\n\n".join(mutated))
                self.assertEqual(result.detected_blocks,113,(index,mode))
                self.assertEqual(len(result.questions),112,(index,mode))
                self.assertEqual(len(result.errors),1,(index,mode))
                self.assertEqual(len(result.review),1,(index,mode))
                self.assertEqual(result.errors[0].number,index+1)
                self.assertNotIn(index+1,[q.number for q in result.questions])
                for q in result.questions:
                    original=expected[q.number-1]
                    self.assertEqual(q.question,original["question"])
                    self.assertEqual(q.options,original["options"])
                    self.assertEqual(q.correct_index,original["correct_index"])

    def test_actual_source_without_blank_separators_and_inconsistent_option_spacing(self):
        expected=actual_attachment_oracle()
        lines=attachment_path("message (1).txt").read_text().splitlines()
        changed=[]
        for line in lines:
            if not line.strip():
                continue
            if line[:2] in ("A)","B)","C)","D)"):
                label,body=line[:2],line[2:].lstrip(" \t")
                line={"A)":"\t","B)":"   ","C)":"", "D)":" \t"}[label]+label+body
            changed.append(line)
        self.assert_reference_equal(engine.parse_unnumbered_checked_txt("\n".join(changed)),expected)

    def test_unicode_symbols_tables_statements_and_only_known_promo_suffixes(self):
        question="English हिन्दी 𝐱² + π = ₹5 — JUST CRACK IT!! inside text\n\n1. statement\n2. कथन\n| x | y |\n|---|---|\n| ① | √2 |"
        source=question+"\n  A)α\n\tB)✅ β²\nC)JUST CRACK IT!! 📚\n D)दूसरा"
        result=engine.parse_unnumbered_checked_txt(source)
        self.assertFalse(result.errors)
        self.assertEqual(result.questions[0].question,question)
        self.assertEqual(result.questions[0].options,["α","β²","JUST CRACK IT!! 📚","दूसरा"])
        self.assertEqual(result.questions[0].correct_index,1)
        for suffix in (" JUST CRACK IT!! 📚",_ATTACHMENT_PROMO):
            result=engine.parse_unnumbered_checked_txt("Question"+suffix+"\nA)one✅\nB)two\nC)three\nD)four")
            self.assertEqual(result.questions[0].question,"Question")
            self.assertEqual(result.questions[0].options[0],"one")

    def test_no_answer_guessing_and_incomplete_blocks_are_reviewed(self):
        for source in ("Question\nA)Answer: B\nB)two\nC)three\nD)four",
                       "Question\nA)one ☑\nB)two\nC)three\nD)four",
                       "Question\nA)one✅\nB)two✅\nC)three\nD)four",
                       "Question\nA)✅one✅\nB)two\nC)three\nD)four",
                       "Question\nA)one✅\nB)two\nC)three",
                       "Question only; no options"):
            result=engine.parse_unnumbered_checked_txt(source)
            self.assertFalse(result.questions)
            self.assertTrue(result.errors)
            self.assertTrue(result.review)

    def test_new_builtin_is_selectable_immutable_and_import_only(self):
        fid=db.BUILTIN_UNNUMBERED_CHECKED_FORMAT
        db.update_user_settings(ADMIN,txt_import_format_id=fid)
        self.assertEqual(db.effective_import_format(ADMIN),fid)
        db.set_default_format("import",fid,ADMIN)
        self.assertEqual(db.get_default_format("import"),fid)
        self.assertEqual(db.get_default_format("presentation"),db.BUILTIN_DEFAULT_FORMAT)
        self.assertTrue(bot._format_info("import",fid)["builtin"])
        self.assertIsNone(bot._format_info("presentation",fid))
        with self.assertRaises(ValueError):
            db.save_quiz_format("import","Overwrite",{},ADMIN,fid)
        with self.assertRaises(ValueError):
            db.delete_quiz_format(fid,"import",ADMIN)
        with self.assertRaises(ValueError):
            db.set_default_format("presentation",fid,ADMIN)
        self.assertIsNone(db.get_quiz(db.new_quiz(USER,"Unchanged display"))["presentation_json"])

    def test_previous_real_attachment_and_numbered_legacy_formats_unchanged(self):
        old=attachment_path("message.txt").read_text(encoding="utf-8-sig")
        default=engine.parse_txt_source(old)
        self.assertEqual(len(default.questions),28)
        self.assertFalse(default.errors)
        for fid in (db.BUILTIN_DEFAULT_FORMAT,db.BUILTIN_CHECKED_FORMAT):
            actual=creator.parse_txt_with_format(old,fid)
            self.assertEqual([q.to_dict() for q in actual.questions],[q.to_dict() for q in default.questions])
            self.assertFalse(actual.errors)
        for source in (LEGACY,LEGACY.replace("1. Synthetic", "Q1. Synthetic"),CHECKED):
            expected=engine.parse_txt_source(source)
            self.assertEqual(len(expected.questions),1)
            self.assertFalse(expected.errors)
            for fid in (db.BUILTIN_DEFAULT_FORMAT,db.BUILTIN_CHECKED_FORMAT):
                actual=creator.parse_txt_with_format(source,fid)
                self.assertEqual([q.to_dict() for q in actual.questions],[q.to_dict() for q in expected.questions])


class UnnumberedUploadTests(DatabaseFixture,unittest.IsolatedAsyncioTestCase):
    update=AsyncTests.update
    telegram=AsyncTests.telegram

    async def test_actual_upload_review_and_sqlite_import_all_113_in_source_order(self):
        path=attachment_path("message (1).txt")
        expected=actual_attachment_oracle()
        update=self.update(ADMIN,name=path.name,raw=path.read_bytes())
        update.callback_query=None
        client,_,_=self.telegram()
        client.get_me=AsyncMock(return_value=NS(username="offline_fixture_bot"))
        update.get_bot=lambda:client
        db.update_user_settings(ADMIN,txt_import_format_id=db.BUILTIN_UNNUMBERED_CHECKED_FORMAT)
        db.set_state(ADMIN,"imp_wait",{"buffer":""})
        with patch.object(creator,"forward_txt_upload",new_callable=AsyncMock,return_value=False):
            await creator.handle_import_document(update,NS(bot=client))
        state,data=db.get_state(ADMIN)
        self.assertEqual(state,"imp_review")
        self.assertEqual(data["report"]["total_detected"],113)
        self.assertEqual(len(data["questions"]),113)
        self.assertFalse(data["errors"])
        self.assertFalse(data["review"])
        self.assertEqual(data["report"]["duplicates"],0)
        await creator._create_imported(update,ADMIN,data,"Actual TXT import regression")
        with db.connect() as con:
            quiz_ids=[row["id"] for row in con.execute("SELECT id FROM quizzes WHERE owner_id=? ORDER BY COALESCE(part_no,1),rowid",(ADMIN,))]
        imported=[q for qid in quiz_ids for q in db.get_questions(qid)]
        self.assertEqual(len(imported),113)
        self.assertEqual(len({q["id"] for q in imported}),113)
        for actual,reference in zip(imported,expected):
            for field in ("question","options","correct_index","explanation"):
                self.assertEqual(actual[field],reference[field],f"Stored question {reference['number']}: {field}")
            self.assertEqual(actual["source_number"],reference["number"])
        self.assertIsNone(db.get_state(ADMIN)[0])
        client.send_document.assert_not_awaited()  # No network forwarding in this offline test.

    async def test_new_format_visible_in_import_menu_not_presentation_menu(self):
        update=self.update(ADMIN)
        with patch.object(creator,"respond",new_callable=AsyncMock) as respond:
            await bot._formats_list(update,"import")
            buttons=[button for row in respond.await_args.args[2].inline_keyboard for button in row]
            self.assertTrue(any(button.callback_data=="fm:view:import:"+db.BUILTIN_UNNUMBERED_CHECKED_FORMAT for button in buttons))
            await bot._formats_list(update,"presentation")
            buttons=[button for row in respond.await_args.args[2].inline_keyboard for button in row]
            self.assertFalse(any(db.BUILTIN_UNNUMBERED_CHECKED_FORMAT in (button.callback_data or "") for button in buttons))



class FailedLegacyUploadRoutingTests(DatabaseFixture,unittest.IsolatedAsyncioTestCase):
    update=AsyncTests.update
    telegram=AsyncTests.telegram

    async def run_actual_workflow(self,raw,expected,selected=None):
        update=self.update(ADMIN,name="message (1).txt",raw=raw)
        update.callback_query=None
        client,_,_=self.telegram()
        client.get_me=AsyncMock(return_value=NS(username="offline_fixture_bot"))
        update.get_bot=lambda:client
        if selected:
            db.update_user_settings(ADMIN,txt_import_format_id=selected)
        before=db.effective_import_format(ADMIN)
        # Enter via the real private-message router, without preselecting the
        # new format or patching any parser/review/create function.
        with patch.object(creator,"forward_txt_upload",new_callable=AsyncMock,return_value=False):
            await bot.private_message_router(update,NS(bot=client))
        state,data=db.get_state(ADMIN)
        self.assertEqual(state,"imp_review")
        self.assertEqual(data["report"]["total_detected"],113)
        self.assertEqual(data["report"]["parsed"],113)
        self.assertFalse(data["errors"])
        self.assertFalse(data["review"])
        self.assertTrue(any("TXT parser used:" in note for note in data["notes"]))
        for actual,reference in zip(data["questions"],expected):
            for field in ("question","options","correct_index"):
                self.assertEqual(actual[field],reference[field])
        update.callback_query=NS(data="i:create",answer=AsyncMock())
        await creator.handle_import_callback(update,NS(bot=client))
        self.assertEqual(db.get_state(ADMIN)[0],"imp_title")
        update.callback_query=None
        update.effective_message.document=None
        update.effective_message.text="Boundary regression import"
        await bot.private_message_router(update,NS(bot=client))
        self.assertIsNone(db.get_state(ADMIN)[0])
        with db.connect() as con:
            ids=[r["id"] for r in con.execute("SELECT id FROM quizzes WHERE owner_id=? ORDER BY COALESCE(part_no,1),rowid",(ADMIN,))]
        imported=[q for qid in ids for q in db.get_questions(qid)]
        self.assertEqual(len(imported),113)
        self.assertEqual(len({q["id"] for q in imported}),113)
        for actual,reference in zip(imported,expected):
            for field in ("question","options","correct_index","explanation"):
                self.assertEqual(actual[field],reference[field])
            self.assertEqual(actual["source_number"],reference["number"])
        self.assertEqual(db.effective_import_format(ADMIN),before)
        return imported,data

    async def test_reproduce_four_failed_legacy_blocks_then_default_router_imports_113(self):
        raw=attachment_path("message (1).txt").read_bytes()
        failed=engine.parse_txt_source(raw.decode("utf-8-sig"))
        self.assertEqual(len(failed.questions),0)
        self.assertEqual(len(failed.errors),4)
        self.assertEqual(db.effective_import_format(ADMIN),db.BUILTIN_DEFAULT_FORMAT)
        await self.run_actual_workflow(raw,actual_attachment_oracle())

    async def test_dot_options_and_no_blank_lines_through_default_upload_callback_title(self):
        import re
        text=attachment_path("message (1).txt").read_text()
        text=re.sub(r"^([ABCD])\)[ \t]*",lambda m:"\u00a0\t"+m[1]+".",text,flags=re.M)
        text="\n".join(line for line in text.splitlines() if line.strip())
        await self.run_actual_workflow(text.encode(),actual_attachment_oracle())

    async def test_explicit_previous_checked_selection_also_recovers_upload(self):
        await self.run_actual_workflow(attachment_path("message (1).txt").read_bytes(),actual_attachment_oracle(),db.BUILTIN_CHECKED_FORMAT)

    async def test_missing_and_conflicting_marks_use_113_separate_review_blocks_in_default_route(self):
        text=attachment_path("message (1).txt").read_text()
        for raw in (text.replace("✅","",1).encode(),text.replace("B) केवल 2 व 3","B) केवल 2 व 3 ✅",1).encode()):
            db.clear_state(ADMIN)
            update=self.update(ADMIN,name="message (1).txt",raw=raw);update.callback_query=None
            client,_,_=self.telegram()
            with patch.object(creator,"forward_txt_upload",new_callable=AsyncMock,return_value=False):
                await bot.private_message_router(update,NS(bot=client))
            state,data=db.get_state(ADMIN)
            self.assertEqual(state,"imp_review")
            self.assertEqual(data["report"]["total_detected"],113)
            self.assertEqual(len(data["questions"]),112)
            self.assertEqual(len(data["errors"]),1)
            self.assertEqual(len(data["review"]),1)

    async def test_successful_legacy_results_and_custom_selection_never_overridden(self):
        for source in (LEGACY,CHECKED,attachment_path("message.txt").read_text()):
            old=engine.parse_txt_source(source)
            actual=creator.parse_txt_for_user(ADMIN,source)
            self.assertEqual(actual.to_dict(),old.to_dict())
            self.assertFalse(hasattr(actual,"txt_import_route_note"))
        fid=self.profile("import","Explicit custom grammar")
        db.update_user_settings(ADMIN,txt_import_format_id=fid)
        actual=creator.parse_txt_for_user(ADMIN,attachment_path("message (1).txt").read_text())
        self.assertFalse(hasattr(actual,"txt_import_route_note"))
        self.assertFalse(actual.questions)



if __name__ == "__main__":
    unittest.main()
