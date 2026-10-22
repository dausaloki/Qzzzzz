"""End-to-end tests for merging multiple files of the same topic into ONE quiz.

Covers:
1. Uploading 3 TXT files of the same topic ("Topic_02_Varnamala_evam_Lipi_Full.txt",
   "Topic_02_Varnamala_evam_Lipi_Part2.txt", "Topic_02_Varnamala_evam_Lipi_Part3.txt")
   derived from the actual Topic 02 Hindi वर्णमाला dataset -> creates ONE combined quiz
   preserving every question, option, answer, explanation, and original file/question order.
2. Explicit multi-file batching via the "➕ इसी Topic की और File जोड़ें" (i:addmore) button
   and "Finish Upload / Create Combined Quiz" action.
3. Accidental duplicate file import protection (re-uploading an identical file in the
   same batch is skipped and counted in Duplicates) while preserving legitimate repeated
   questions across distinct files.
4. Different topics ("Topic_02_..." vs "Topic_03_...") and ambiguous names ("file1.txt" vs
   "file2.txt") -> asks for confirmation instead of silently combining.
5. Large multi-file imports exceeding QUESTIONS_PER_PART (500) -> automatically splits
   into numbered parts without losing any questions.
6. Merging a newly uploaded Part-2 file of the same topic into an already-created quiz
   via "i:appendquiz:<qid>".
"""
from __future__ import annotations

from pathlib import Path

import config
import database as db
from tests.test_bot_flow import Harness, buttons, isolated, run

_ = isolated

FIXTURE_FULL_TXT = (
    Path(__file__).resolve().parent / "fixtures" / "Topic_02_Varnamala_evam_Lipi_Full.txt"
)


def _split_topic02_into_three_files() -> tuple[bytes, bytes, bytes]:
    """Split the actual 50-question Topic_02_Varnamala_evam_Lipi_Full.txt content into 3
    distinct files of the same topic (Q1..Q20, Q21..Q35, Q36..Q50) so all 50 real Hindi
    questions are tested across 3 files in order."""
    full_text = FIXTURE_FULL_TXT.read_text(encoding="utf-8")
    idx21 = full_text.index("[प्रश्न 21]")
    idx36 = full_text.index("[प्रश्न 36]")

    file1 = full_text[:idx21].encode("utf-8")
    file2 = (
        "===================================================================\n"
        "अध्याय 2: वर्णमाला (ध्वनि) एवं लिपि - भाग 2 (प्रश्न 21 से 35)\n"
        "===================================================================\n\n"
        + full_text[idx21:idx36]
    ).encode("utf-8")
    file3 = (
        "===================================================================\n"
        "अध्याय 2: वर्णमाला (ध्वनि) एवं लिपि - भाग 3 (प्रश्न 36 से 50)\n"
        "===================================================================\n\n"
        + full_text[idx36:]
    ).encode("utf-8")
    return file1, file2, file3


def test_three_txt_files_of_same_topic_combine_into_one_quiz():
    async def t():
        async with Harness() as h:
            u = h.f.user(4101)
            f1, f2, f3 = _split_topic02_into_three_files()
            h.api.files["F1"] = f1
            h.api.files["F2"] = f2
            h.api.files["F3"] = f3

            # Upload 3 files belonging to Topic_02_Varnamala_evam_Lipi as separate messages
            await h.send(
                h.f.document(u, "F1", "Topic_02_Varnamala_evam_Lipi_Full.txt", "text/plain", len(f1))
            )
            await h.send(
                h.f.document(u, "F2", "Topic_02_Varnamala_evam_Lipi_Part2.txt", "text/plain", len(f2))
            )
            await h.send(
                h.f.document(u, "F3", "Topic_02_Varnamala_evam_Lipi_Part3.txt", "text/plain", len(f3))
            )

            last_msg = h.api.sent("sendMessage", 4101)[-1]
            txt = last_msg["text"]
            assert "Combined Topic Batch (3 files)" in txt
            assert "Topic_02_Varnamala_evam_Lipi_Full.txt" in txt
            assert "Topic_02_Varnamala_evam_Lipi_Part2.txt" in txt
            assert "Topic_02_Varnamala_evam_Lipi_Part3.txt" in txt
            assert "• Files combined: 3" in txt
            assert "• Total detected: 50" in txt
            assert "• Parsed: 50" in txt
            assert "• Failed: 0" in txt
            assert "• Duplicates: 0" in txt
            assert "• Answer mapped: 50" in txt
            assert "• Parts created: 1" in txt

            # Button explicitly shows Finish Upload / Create Combined Quiz
            b_list = buttons(last_msg["reply_markup"])
            assert "Finish Upload / Create Combined Quiz" in b_list[0]["text"]
            assert b_list[0]["callback_data"] in ("i:create", "i:reviewed")

            await h.cb(u, b_list[0]["callback_data"])
            await h.cb(u, "i:usedefault")

            quizzes = db.get_owner_quizzes(4101)
            assert len(quizzes) == 1, f"Expected 1 combined quiz, got {len(quizzes)}"
            assert quizzes[0]["title"] == "Topic_02_Varnamala_evam_Lipi"

            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 50
            assert [q["position"] for q in qs] == list(range(1, 51))
            assert [q["source_number"] for q in qs] == list(range(1, 51))
            assert qs[0]["question"] == "हिन्दी वर्णमाला में स्वरों की संख्या कितनी मानी गई है?"
            assert qs[0]["options"] == ["10", "11", "13", "14"]
            assert qs[0]["correct_index"] == 1
            assert qs[20]["source_number"] == 21
            assert qs[35]["source_number"] == 36
            assert qs[49]["question"] == "वर्ण किसे कहते हैं?"
            assert qs[49]["correct_index"] == 1

    run(t())


def test_explicit_addmore_button_combines_files_with_answer_key_and_explanations():
    async def t():
        async with Harness() as h:
            u = h.f.user(4102)
            part1 = (
                "1. राजस्थान का राज्य वृक्ष कौन-सा है?\n"
                "(A) खेजड़ी\n(B) रोहिड़ा\n(C) पीपल\n(D) नीम\n"
                "उत्तर: (A)\nव्याख्या: खेजड़ी को 1983 में राज्य वृक्ष घोषित किया गया।\n"
            ).encode("utf-8")
            part2 = (
                "1. राजस्थान का राज्य पुष्प कौन-सा है?\n"
                "(A) कमल\n(B) रोहिड़ा\n(C) गुलाब\n(D) चमेली\n"
                "उत्तर कुंजी\n1-B\n"
            ).encode("utf-8")
            part3 = (
                "1. राजस्थान का राज्य पशु (वन्यजीव श्रेणी) कौन-सा है?\n"
                "(A) बाघ\n(B) चिंकारा\n(C) हाथी\n(D) शेर\n"
                "उत्तर: (B)\nव्याख्या: चिंकारा राज्य पशु है।\n"
            ).encode("utf-8")

            h.api.files["B1"] = part1
            h.api.files["B2"] = part2
            h.api.files["B3"] = part3

            await h.send(h.f.document(u, "B1", "set_a.txt", "text/plain", len(part1)))
            await h.cb(u, "i:addmore")
            await h.send(h.f.document(u, "B2", "set_b.txt", "text/plain", len(part2)))
            await h.cb(u, "i:addmore")
            await h.send(h.f.document(u, "B3", "set_c.txt", "text/plain", len(part3)))

            last = h.api.sent("sendMessage", 4102)[-1]
            assert "• Files combined: 3" in last["text"]
            assert "• Answer mapped: 3 (answer key से 1, प्रश्न के साथ लिखे 2)" in last["text"]
            cb0 = buttons(last["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.text(u, "राजस्थान प्रतीक चिह्न")

            quizzes = db.get_owner_quizzes(4102)
            assert len(quizzes) == 1
            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 3
            assert [q["correct_index"] for q in qs] == [0, 1, 1]
            assert "1983" in qs[0]["explanation"]
            assert "चिंकारा" in qs[2]["explanation"]

    run(t())


def test_accidental_duplicate_file_upload_skipped_while_legitimate_repeats_kept():
    async def t():
        async with Harness() as h:
            u = h.f.user(4103)
            f1 = (
                "[प्रश्न 1] निम्नलिखित में से शुद्ध शब्द कौन-सा है?\n"
                "(A) कवयित्री\n(B) कवियत्री\n(C) कवियित्री\n(D) कवयत्री\nउत्तर: (A)\n"
            ).encode("utf-8")
            # Distinct file of same topic with a legitimate repeated question stem + different options,
            # plus one identical question to test cross-file duplicate reporting without deletion
            f2 = (
                "[प्रश्न 1] निम्नलिखित में से शुद्ध शब्द कौन-सा है?\n"
                "(A) उज्ज्वल\n(B) उज्वल\n(C) उज्जवल\n(D) उजवल\nउत्तर: (A)\n\n"
                "[प्रश्न 2] निम्नलिखित में से शुद्ध शब्द कौन-सा है?\n"
                "(A) कवयित्री\n(B) कवियत्री\n(C) कवियित्री\n(D) कवयत्री\nउत्तर: (A)\n"
            ).encode("utf-8")

            h.api.files["D1"] = f1
            h.api.files["D1_DUP"] = f1  # Exact same file bytes uploaded accidentally
            h.api.files["D2"] = f2

            await h.send(h.f.document(u, "D1", "Topic_02_Shuddha_Part1.txt", "text/plain", len(f1)))
            # Accidental duplicate upload of the first file
            await h.send(h.f.document(u, "D1_DUP", "Topic_02_Shuddha_Part1.txt", "text/plain", len(f1)))
            # Second distinct file of same topic
            await h.send(h.f.document(u, "D2", "Topic_02_Shuddha_Part2.txt", "text/plain", len(f2)))

            last = h.api.sent("sendMessage", 4103)[-1]
            txt = last["text"]
            # Only 2 distinct files are combined; 1 duplicate file (1 question) + 1 cross-file repeat = 2 duplicates reported
            assert "• Files combined: 2" in txt
            assert "• Parsed: 3" in txt
            assert "• Duplicates: 2" in txt

            cb0 = buttons(last["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.cb(u, "i:usedefault")

            quizzes = db.get_owner_quizzes(4103)
            assert len(quizzes) == 1
            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 3  # Legitimate questions across the 2 distinct files are preserved

    run(t())


def test_different_topics_and_ambiguous_names_ask_for_confirmation():
    async def t():
        async with Harness() as h:
            u = h.f.user(4104)
            t2 = (
                "[प्रश्न 1] वर्णमाला का पहला स्वर कौन-सा है?\n"
                "(A) अ\n(B) आ\n(C) इ\n(D) ई\nउत्तर: (A)\n"
            ).encode("utf-8")
            t3 = (
                "[प्रश्न 1] दो वर्णों के मेल से उत्पन्न विकार को क्या कहते हैं?\n"
                "(A) समास\n(B) संधि\n(C) उपसर्ग\n(D) प्रत्यय\nउत्तर: (B)\n"
            ).encode("utf-8")
            h.api.files["TOP2"] = t2
            h.api.files["TOP3"] = t3

            await h.send(h.f.document(u, "TOP2", "Topic_02_Varnamala_Part1.txt", "text/plain", len(t2)))
            await h.send(h.f.document(u, "TOP3", "Topic_03_Sandhi_Part1.txt", "text/plain", len(t3)))

            last = h.api.sent("sendMessage", 4104)[-1]
            assert "Topic / Batch Confirmation" in last["text"]
            cbs = [b["callback_data"] for b in buttons(last["reply_markup"])]
            assert "i:merge_pending" in cbs
            assert "i:replace_pending" in cbs
            assert "i:drop_pending" in cbs

            # Choosing "सिर्फ नई File से अलग Quiz बनाएँ" (i:replace_pending) keeps only Topic 03
            await h.cb(u, "i:replace_pending")
            rev = h.api.sent("sendMessage", 4104)[-1]
            assert "Topic_03_Sandhi_Part1" in h.all_text(4104)
            cb0 = buttons(rev["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.cb(u, "i:usedefault")

            quizzes = db.get_owner_quizzes(4104)
            assert len(quizzes) == 1
            assert quizzes[0]["title"] == "Topic_03_Sandhi_Part1"
            qs = db.get_questions(quizzes[0]["id"])
            assert len(qs) == 1
            assert "संधि" in qs[0]["options"]

    run(t())


def test_large_multi_file_import_splits_into_numbered_parts(monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_PART", 40)

    async def t():
        async with Harness() as h:
            u = h.f.user(4105)
            f1, f2, f3 = _split_topic02_into_three_files()
            h.api.files["L1"] = f1
            h.api.files["L2"] = f2
            h.api.files["L3"] = f3

            await h.send(h.f.document(u, "L1", "Topic_02_Varnamala_evam_Lipi_Full.txt", "text/plain", len(f1)))
            await h.send(h.f.document(u, "L2", "Topic_02_Varnamala_evam_Lipi_Part2.txt", "text/plain", len(f2)))
            await h.send(h.f.document(u, "L3", "Topic_02_Varnamala_evam_Lipi_Part3.txt", "text/plain", len(f3)))

            last = h.api.sent("sendMessage", 4105)[-1]
            assert "• Parts created: 2" in last["text"]
            cb0 = buttons(last["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.cb(u, "i:usedefault")

            quizzes = sorted(db.get_owner_quizzes(4105), key=lambda q: q.get("part_no") or 0)
            assert len(quizzes) == 2
            assert quizzes[0]["title"] == "Topic_02_Varnamala_evam_Lipi — Part 1"
            assert quizzes[1]["title"] == "Topic_02_Varnamala_evam_Lipi — Part 2"
            assert len(db.get_questions(quizzes[0]["id"])) == 40
            assert len(db.get_questions(quizzes[1]["id"])) == 10

    run(t())


def test_ambiguous_filenames_confirm_merge_and_append_to_existing_quiz():
    async def t():
        async with Harness() as h:
            u = h.f.user(4106)
            f1 = (
                "[प्रश्न 1] देवनागरी लिपि का विकास किस लिपि से हुआ?\n"
                "(A) खरोष्ठी\n(B) ब्राह्मी\n(C) गुरुमुखी\n(D) शारदा\nउत्तर: (B)\n"
            ).encode("utf-8")
            f2 = (
                "[प्रश्न 2] हिन्दी दिवस कब मनाया जाता है?\n"
                "(A) 14 सितम्बर\n(B) 10 जनवरी\n(C) 21 फरवरी\n(D) 15 अगस्त\nउत्तर: (A)\n"
            ).encode("utf-8")
            f3 = (
                "[प्रश्न 3] विश्व हिन्दी दिवस कब मनाया जाता है?\n"
                "(A) 14 सितम्बर\n(B) 10 जनवरी\n(C) 26 जनवरी\n(D) 2 अक्टूबर\nउत्तर: (B)\n"
            ).encode("utf-8")

            h.api.files["A1"] = f1
            h.api.files["A2"] = f2
            h.api.files["A3"] = f3

            # Ambiguous filenames without clicking i:addmore -> asks confirmation
            await h.send(h.f.document(u, "A1", "set1.txt", "text/plain", len(f1)))
            await h.send(h.f.document(u, "A2", "set2.txt", "text/plain", len(f2)))
            last = h.api.sent("sendMessage", 4106)[-1]
            assert "Topic / Batch Confirmation" in last["text"]

            # Confirm merge -> combines set1.txt and set2.txt
            await h.cb(u, "i:merge_pending")
            rev = h.api.sent("sendMessage", 4106)[-1]
            assert "• Files combined: 2" in rev["text"]
            cb0 = buttons(rev["reply_markup"])[0]["callback_data"]
            await h.cb(u, cb0)
            await h.text(u, "Topic_02_Varnamala_evam_Lipi")

            quizzes = db.get_owner_quizzes(4106)
            assert len(quizzes) == 1
            qid = quizzes[0]["id"]
            assert len(db.get_questions(qid)) == 2

            # Now upload a 3rd file of the same topic after the quiz already exists ->
            # can also append directly via i:appendquiz:<qid>
            await h.send(
                h.f.document(u, "A3", "Topic_02_Varnamala_evam_Lipi_Part3.txt", "text/plain", len(f3))
            )
            rev3 = h.api.sent("sendMessage", 4106)[-1]
            cbs3 = [b["callback_data"] for b in buttons(rev3["reply_markup"])]
            assert f"i:appendquiz:{qid}" in cbs3
            await h.cb(u, f"i:appendquiz:{qid}")

            assert len(db.get_owner_quizzes(4106)) == 1
            qs = db.get_questions(qid)
            assert len(qs) == 3
            assert [q["position"] for q in qs] == [1, 2, 3]
            assert [q["correct_index"] for q in qs] == [1, 0, 1]

    run(t())
