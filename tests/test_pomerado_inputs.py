from browser_platform.pomerado.inputs import auto_answers_for_request


def test_auto_answers_known_search_term():
    request = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "agent",
        "questions": [
            {"id": "term", "type": "text", "prompt": "What search term should be used?"},
        ],
    }
    answers = auto_answers_for_request(request, {"term": "browser automation"})
    assert answers == {"term": "browser automation"}


def test_auto_answers_choice_recommended_and_skips_secrets():
    request = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "agent",
        "questions": [
            {
                "id": "result",
                "type": "choice",
                "prompt": "Which article?",
                "options": [{"id": "first", "label": "Browser automation"}, {"id": "other", "label": "Other"}],
                "recommended": "first",
            }
        ],
    }
    assert auto_answers_for_request(request, {}) == {"result": "first"}

    secret = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "agent",
        "questions": [{"id": "code", "type": "secret", "secretKind": "one_time_code", "prompt": "Code"}],
    }
    assert auto_answers_for_request(secret, {"code": "123456"}) is None


def test_auto_answers_does_not_confirm_purchases_or_notices():
    purchase = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "script",
        "questions": [{"id": "ok", "type": "confirm", "prompt": "Place the order now?"}],
    }
    assert auto_answers_for_request(purchase, {}) is None
    notice = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "system",
        "notice": "Complete the captcha in the browser.",
        "questions": [{"id": "done", "type": "confirm", "prompt": "Did you do it?"}],
    }
    assert auto_answers_for_request(notice, {}) is None
    proceed = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "agent",
        "questions": [{"id": "go", "type": "confirm", "prompt": "Continue with this read-only example?"}],
    }
    assert auto_answers_for_request(proceed, {}) == {"go": {"confirmed": True}}
