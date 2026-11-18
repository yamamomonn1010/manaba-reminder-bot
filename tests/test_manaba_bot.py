"""manaba_bot.py のテスト(本物のmanabaにもDiscordにも接続しません)"""
from datetime import datetime, timedelta

import pytest

import manaba_bot as mb
from fake_manaba_server import Handler, start_server

TABLE = """<table><tr><th>タイトル</th><th>コース</th><th>受付開始日時</th><th>受付終了日時</th></tr>
<tr><td><a href="course_1_report_22">課題1</a></td><td><a href="course_1">国語</a></td>
<td>2026-10-01 09:00</td><td>2026-10-08 23:59</td></tr>
<tr><td><a href="course_1_query_33">小テスト</a></td><td><a href="course_1">国語</a></td>
<td>2026-10-01 09:00</td><td></td></tr></table>"""


@pytest.fixture(scope="module")
def server():
    srv, url = start_server()
    yield url
    srv.shutdown()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "DB_PATH", str(tmp_path / "t.db"))
    mb.init_db()


# ---------- 日時まわり ----------
def test_parse_dt():
    assert mb.parse_dt("2026-10-08 23:59") == datetime(2026, 10, 8, 23, 59, tzinfo=mb.JST)
    assert mb.parse_dt("2026/2/30 10:00") is None          # 存在しない日付
    assert mb.parse_dt("") is None


@pytest.mark.parametrize("text, expected", [
    ("2026-10-08 23:59", (10, 8, 23, 59)),       # 年つき
    ("10/8 23:59", (10, 8, 23, 59)),
    ("１０／１０ １０：００", (10, 10, 10, 0)),    # 全角
    ("1010", (10, 10, 23, 59)),                  # 時刻を省略 → 既定の時刻
    ("１０１０", (10, 10, 23, 59)),               # 全角の数字だけ
    ("1010 1830", (10, 10, 18, 30)),
    ("1010 18", (10, 10, 18, 0)),
    ("10/10", (10, 10, 23, 59)),
    ("10月10日 18時", (10, 10, 18, 0)),
    ("10月10日　18時30分", (10, 10, 18, 30)),      # 全角スペース
])
def test_parse_user_due(text, expected):
    dt = mb.parse_user_due(text)
    assert (dt.month, dt.day, dt.hour, dt.minute) == expected


@pytest.mark.parametrize("text", ["abc", "", "1332", "1010 2560", "10/32", "1010 18:30 x", "10"])
def test_parse_user_due_rejects_invalid(text):
    assert mb.parse_user_due(text) is None


def test_parse_user_due_default_time_is_configurable(monkeypatch):
    monkeypatch.setattr(mb, "DEFAULT_DUE_HOUR", 20)
    monkeypatch.setattr(mb, "DEFAULT_DUE_MINUTE", 0)
    dt = mb.parse_user_due("1010")
    assert (dt.hour, dt.minute) == (20, 0)


@pytest.mark.parametrize("hours, expected", [(100, 24), (72, 24), (30, 12), (24, 12), (13, 6), (12, 6), (5, 3), (3, 3), (2, 1), (0.5, 1)])
def test_interval_for(hours, expected):
    assert mb.interval_for(hours) == expected


def test_fmt_remaining():
    assert mb.fmt_remaining(timedelta(days=3, hours=19)) == "3日19時間"
    assert mb.fmt_remaining(timedelta(hours=5, minutes=12)) == "5時間12分"
    assert mb.fmt_remaining(timedelta(minutes=40)) == "40分"
    assert mb.fmt_remaining(timedelta(minutes=-1)) == "期限切れ"


def test_quiet_hours(monkeypatch):
    monkeypatch.setattr(mb, "QUIET_START", 23)
    monkeypatch.setattr(mb, "QUIET_END", 7)
    now = datetime(2026, 10, 1, tzinfo=mb.JST)
    assert [mb.in_quiet_hours(now.replace(hour=h)) for h in (22, 23, 0, 6, 7, 12)] == \
           [False, True, True, True, False, False]


# ---------- HTML解析 ----------
def test_parse_list_page_uses_header_and_allows_empty_deadline():
    items, ok = mb.ManabaClient.parse_list_page(TABLE, "https://x.jp/ct/home_library_report")
    assert ok and len(items) == 2
    a, b = items
    assert (a.id, a.kind, a.course) == ("report:1:22", "report", "国語")
    assert a.due == datetime(2026, 10, 8, 23, 59, tzinfo=mb.JST)
    assert b.due is None                                    # 空欄は「期限なし」(開始日時を誤用しない)
    assert a.url == "https://x.jp/ct/course_1_report_22"


def test_parse_list_page_distinguishes_empty_from_broken():
    _, ok_empty = mb.ManabaClient.parse_list_page("<p>未提出のレポートはありません</p>", "https://x.jp/")
    _, ok_broken = mb.ManabaClient.parse_list_page("<html><body>メンテナンス中</body></html>", "https://x.jp/")
    assert ok_empty is True and ok_broken is False


# ---------- ログインと取得(模擬サーバー) ----------
def test_login_and_fetch_library(server):
    c = mb.ManabaClient(server, "u1", "pw")
    data = c.fetch_library()
    assert data.errors == [] and data.authoritative
    assert [i.title for i in data.items_by_kind["report"]] == ["レポート課題1", "最終レポート"]
    assert data.items_by_kind["survey"] == []               # 「ありません」は正常な0件
    assert data.items_by_kind["query"][0].course == "デザインと表現"


def test_wrong_password_is_sent_only_once(server):
    Handler.login_posts = 0
    with pytest.raises(mb.LoginError):
        mb.ManabaClient(server, "u1", "WRONG").login()
    assert Handler.login_posts == 1                         # ロック防止: 再送しない


def test_course_mode_skips_submitted(server, monkeypatch):
    monkeypatch.setattr(mb, "SOURCE_MODE", "course")
    data = mb.ManabaClient(server, "u1", "pw").fetch_courses()
    titles = [i.title for i in data.items_by_kind["report"]]
    assert "レポート課題1" in titles and not any("提出済" in t for t in titles)
    assert data.authoritative is False


# ---------- DB反映 ----------
def _data(items):
    return mb.SyncData(items_by_kind={"report": items}, authoritative=True)


def _item(due_day=8):
    return mb.Item("report:1:22", "report", "課題1", "国語", "https://x.jp/a",
                   datetime(2026, 10, due_day, 23, 59, tzinfo=mb.JST))


def test_apply_sync_new_changed_autodone(tmp_db):
    r1 = mb.apply_sync(_data([_item()]))
    assert r1.first_run and len(r1.new) == 1
    r2 = mb.apply_sync(_data([_item()]))
    assert not r2.new and not r2.changed
    r3 = mb.apply_sync(_data([_item(due_day=9)]))           # 締切変更
    assert len(r3.changed) == 1
    assert len(mb.apply_sync(_data([])).auto_done) == 0     # 1回消えただけでは完了にしない
    assert len(mb.apply_sync(_data([])).auto_done) == 1     # 2回連続で消えたら自動完了
    assert mb.pending_rows() == []


def test_failed_kind_does_not_touch_db(tmp_db):
    mb.apply_sync(_data([_item()]))
    only_query = mb.SyncData(items_by_kind={"query": []}, authoritative=True)   # reportの取得は失敗した想定
    for _ in range(3):
        mb.apply_sync(only_query)
    assert len(mb.pending_rows()) == 1


def test_collect_reminders_respects_interval(tmp_db):
    now = datetime.now(mb.JST)
    with mb.db() as c:
        c.execute("INSERT INTO tasks(id,source,kind,title,due,done,last_notified_at,created_at,updated_at) "
                  "VALUES('a','manual','report','x',?,0,?,?,?)",
                  ((now + timedelta(hours=5)).isoformat(), (now - timedelta(hours=2)).isoformat(),
                   now.isoformat(), now.isoformat()))
    assert mb.collect_reminders(now) == []                  # 残り5h=3h間隔。2時間前の通知なので、まだ
    with mb.db() as c:
        c.execute("UPDATE tasks SET last_notified_at=?", ((now - timedelta(hours=3, minutes=1)).isoformat(),))
    assert [r["id"] for r in mb.collect_reminders(now)] == ["a"]
    with mb.db() as c:
        c.execute("UPDATE tasks SET due=?", ((now - timedelta(minutes=1)).isoformat(),))
    assert mb.collect_reminders(now) == []                  # 期限切れは通知しない


# ---------- 同期の全体フロー(Discordには送らず、送信内容を記録するだけ) ----------
import asyncio


class _FakeManaba:
    def __init__(self, result):
        self.result = result

    def fetch(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _run_sync(monkeypatch, result, manual=False):
    sent = []

    async def fake_post(content=None, embed=None):
        sent.append((content, embed))
        return True

    monkeypatch.setattr(mb, "post", fake_post)
    monkeypatch.setattr(mb, "get_manaba", lambda: _FakeManaba(result))
    return asyncio.run(mb.do_sync(manual=manual)), sent


def test_do_sync_posts_new_task(tmp_db, monkeypatch):
    summary, sent = _run_sync(monkeypatch, _data([_item()]))
    assert "新規1件" in summary
    assert len(sent) == 1 and "初回同期" in sent[0][1].title


def test_login_failures_pause_auto_sync(tmp_db, monkeypatch):
    monkeypatch.setitem(mb._state, "login_failures", 0)
    monkeypatch.setitem(mb._state, "paused", False)
    for _ in range(3):
        _, sent = _run_sync(monkeypatch, mb.LoginError("NG"))
    assert mb._state["paused"] is True                      # 3回連続の失敗で自動同期を停止
    _run_sync(monkeypatch, _data([_item()]), manual=True)   # 成功すると解除される
    assert mb._state["paused"] is False


# ---------- /add(手入力)の最初のリマインド ----------
class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()


def test_add_command_is_reminded_even_when_deadline_is_close(tmp_db):
    """締切が30分後の手入力課題でも、追加後の最初の確認でリマインドされる(間隔より先に締切が来ても取りこぼさない)"""
    due = datetime.now(mb.JST) + timedelta(minutes=30)
    inter = _FakeInteraction()
    asyncio.run(mb.cmd_add.callback(inter, "AAA", due.strftime("%Y-%m-%d %H:%M"), ""))
    assert "追加しました" in inter.response.messages[0]
    with mb.db() as c:
        row = c.execute("SELECT * FROM tasks WHERE title='AAA'").fetchone()
    assert row["last_notified_at"] is None
    assert [r["title"] for r in mb.collect_reminders(datetime.now(mb.JST))] == ["AAA"]


# ---------- 締切が遠い課題は、定期リマインドしない ----------
def test_far_deadline_is_not_reminded_but_near_one_is(tmp_db, monkeypatch):
    monkeypatch.setattr(mb, "REMIND_WITHIN_DAYS", 7)
    now = datetime.now(mb.JST)
    with mb.db() as c:
        for tid, days in (("far", 43), ("edge", 8), ("near", 6)):
            c.execute("INSERT INTO tasks(id,source,kind,title,due,done,last_notified_at,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,0,NULL,?,?)",
                      (tid, "manaba", "survey", tid, (now + timedelta(days=days)).isoformat(), now.isoformat(), now.isoformat()))
    assert [r["id"] for r in mb.collect_reminders(now)] == ["near"]
    monkeypatch.setattr(mb, "REMIND_WITHIN_DAYS", 0)        # 0 = 制限なし
    assert {r["id"] for r in mb.collect_reminders(now)} == {"far", "edge", "near"}


# ---------- 表示:課題名の記号でリンクが壊れない ----------
def test_esc_protects_link_text():
    assert mb._esc("[第3回]小テスト") == "［第3回］小テスト"
    assert mb._esc("外部_資格") == "外部\\_資格"                # リンクの外(普通の文)は、\ で打ち消す
    assert mb._esc_link("2026年度後期_外部資格単位認定") == "2026年度後期＿外部資格単位認定"   # リンクの中は、全角に置き換える
    due = datetime(2026, 12, 23, 23, 55, tzinfo=mb.JST)
    it = mb.Item("survey:1:2", "survey", "2026年度後期_外部資格単位認定", "外部_資格", "https://x.jp/ct/course_1_survey_2", due)
    line = mb.item_line(it, datetime(2026, 11, 15, tzinfo=mb.JST))
    assert "[2026年度後期＿外部資格単位認定](https://x.jp/ct/course_1_survey_2)" in line
    assert "外部\\_資格｜" in line
    assert "\\_外部" not in line                               # リンクの表示名に、\ が見えない


def test_row_line_link_and_plain_title(tmp_db):
    now = datetime.now(mb.JST)
    with mb.db() as c:
        for tid, url in (("a", "https://x.jp/ct/course_1_survey_2"), ("b", None)):
            c.execute("INSERT INTO tasks(id,source,kind,title,course,url,due,done,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,0,?,?)",
                      (tid, "manaba" if url else "manual", "survey" if url else None, "A_B", "科目_C", url,
                       (now + timedelta(days=1)).isoformat(), now.isoformat(), now.isoformat()))
        linked = mb.row_line(c.execute("SELECT * FROM tasks WHERE id='a'").fetchone(), now)
        plain = mb.row_line(c.execute("SELECT * FROM tasks WHERE id='b'").fetchone(), now)
    assert "[A＿B](https://x.jp/ct/course_1_survey_2)" in linked
    assert "A\\_B" in plain and "[" not in plain.split("\n")[0].replace("**[手入力]**", "")
