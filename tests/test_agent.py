from askdata.agent import ask
from askdata.db import connect
from askdata.llm import LLMReply, parse_reply


def reply(sql="", refuse=False, reason=""):
    return LLMReply(sql, refuse, reason, "", 10, 5, 0, 1.0)


class Script:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, model, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_self_correction_feeds_error_back():
    con = connect()
    llm = Script(reply("SELECT nope FROM fct_posting"), reply("SELECT count(*) FROM fct_posting"))
    tr = ask(con, "how many postings?", ablation="d", llm=llm)
    assert tr.retries == 1 and tr.result.rows[0][0] == 5755
    assert "### Attempt 1 failed" in llm.prompts[1] and "nope" in llm.prompts[1]


def test_empty_result_triggers_retry_and_max_two():
    con = connect()
    empty = "SELECT * FROM fct_posting WHERE family = 'astronaut'"
    llm = Script(reply(empty), reply(empty), reply(empty))
    tr = ask(con, "q", ablation="d", llm=llm)
    assert len(tr.attempts) == 3 and tr.result.rows == []


def test_no_retries_below_ablation_d():
    con = connect()
    llm = Script(reply("SELECT nope FROM fct_posting"))
    tr = ask(con, "q", ablation="c", llm=llm)
    assert len(tr.attempts) == 1 and tr.result.error


def test_refusal_stops_the_loop():
    con = connect()
    tr = ask(con, "applicants?", ablation="d", llm=Script(reply(refuse=True, reason="no applicant data")))
    assert tr.refused and tr.result is None


def test_write_attempt_is_blocked_not_executed():
    con = connect()
    llm = Script(reply("DELETE FROM fct_posting"), reply(refuse=True, reason="read-only"))
    tr = ask(con, "delete everything", ablation="d", llm=llm)
    assert "guard" in tr.attempts[0].error and tr.refused
    assert con.execute("select count(*) from fct_posting").fetchone()[0] == 5755


def test_parse_reply_variants():
    assert parse_reply('{"sql":"SELECT 1","refuse":false,"reason":""}')[:2] == ("SELECT 1", False)
    assert parse_reply('```json\n{"sql":"","refuse":true,"reason":"x"}\n```')[1] is True
    assert parse_reply("SELECT 2")[0] == "SELECT 2"
