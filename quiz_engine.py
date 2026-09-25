import json

def load_questions(path="questions.json"):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("questions.json must contain a list")
    return data

def validate_questions(questions):
    if not questions:
        raise ValueError("Question bank is empty")
    if len(questions) > 100:
        raise ValueError("Maximum 100 questions allowed")
    for q in questions:
        if not q.get("question"):
            raise ValueError(f"Question {q.get('number')} is empty")
        if len(q.get("options", [])) != 4:
            raise ValueError(f"Question {q.get('number')} must have 4 options")
        labels = [x.get("label") for x in q["options"]]
        if labels != ["A", "B", "C", "D"]:
            raise ValueError(f"Question {q.get('number')} options must be A-D")
        if q.get("correct") not in ["A", "B", "C", "D"]:
            raise ValueError(f"Question {q.get('number')} has invalid answer")
    return True

def result_text(total, correct, wrong, skipped):
    answered = correct + wrong
    percentage = (correct / total * 100) if total else 0
    return (
        "🏁 Quiz समाप्त\n\n"
        f"कुल प्रश्न: {total}\n"
        f"✅ सही: {correct}\n"
        f"❌ गलत: {wrong}\n"
        f"⏭️ छोड़े गए: {skipped}\n"
        f"📊 प्रतिशत: {percentage:.2f}%"
    )
