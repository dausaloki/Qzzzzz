import random

def build_order(questions, shuffle_questions=False):
    items = list(questions)
    if shuffle_questions:
        random.shuffle(items)
    return items

def poll_data(question, shuffle_options=False):
    options = list(question["options"])
    correct = question["correct_index"]
    pairs = list(enumerate(options))
    if shuffle_options:
        random.shuffle(pairs)
    new_options = [text for _, text in pairs]
    new_correct = next(i for i, (old_i, _) in enumerate(pairs) if old_i == correct)
    return new_options, new_correct

def short_text(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit-1)] + "…"
