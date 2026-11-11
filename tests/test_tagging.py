from app.tagging import TagDef, tag_with_llm, tag_with_rules


class FakeLLM:
    available = True

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user, **kwargs):
        return self.payload


TAGS = [
    TagDef("project_application", "项目申报", "event", ("申报", "征集")),
    TagDef("finance", "金融", "domain", ("银行", "利率")),
]


def test_out_of_vocab_tags_are_dropped():
    llm = FakeLLM({"tags": [{"id": "policy", "confidence": 0.9}, {"id": "finance", "confidence": 0.8}]})
    hits = tag_with_llm(llm, TAGS, "标题", "银行降准", max_tags=5, body_chars=500)
    assert [h.tag_id for h in hits] == ["finance"]


def test_duplicate_tags_collapse():
    llm = FakeLLM(
        {"tags": [{"id": "finance", "confidence": 0.9}, {"id": "finance", "confidence": 0.7}]}
    )
    hits = tag_with_llm(llm, TAGS, "标题", "银行", max_tags=5, body_chars=500)
    assert [h.tag_id for h in hits] == ["finance"]


def test_max_tags_truncates_and_keeps_highest_confidence():
    llm = FakeLLM(
        {
            "tags": [
                {"id": "project_application", "confidence": 0.4},
                {"id": "finance", "confidence": 0.9},
            ]
        }
    )
    hits = tag_with_llm(llm, TAGS, "标题", "银行与项目申报", max_tags=1, body_chars=500)
    assert [h.tag_id for h in hits] == ["finance"]


def test_rules_engine_hits_multiple_tags():
    hits = tag_with_rules(TAGS, "关于申报科技项目的通知", "通知", max_tags=5, body_chars=500)
    assert [h.tag_id for h in hits] == ["project_application"]
    assert hits[0].engine == "rules"
