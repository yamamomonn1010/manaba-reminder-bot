"""
テスト用の「manabaもどき」サーバー。
本物のmanabaには一切アクセスせず、ログイン → SSO風の自動送信ページ → 一覧ページ
という流れだけを再現します(HTMLの構造は一般的なmanabaを想定した模擬です)。
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

LOGIN = """<html><body><form action="/ct/login" method="post">
<input type="hidden" name="csrf" value="tok123"><input type="text" name="userid">
<input type="password" name="password"><input type="submit" name="login" value="Login">
</form></body></html>"""

AUTO = """<html><body><form action="/ct/sso_done" method="post">
<input type="hidden" name="SAMLResponse" value="abc"><input type="submit" value="Continue">
</form></body></html>"""

HOME = """<html><body><a href="logout">ログアウト</a><a href="home_library_query">未提出課題一覧</a>
<a href="course_1678092">デザインと表現</a><a href="course_1700000">物理の世界</a></body></html>"""


def table(rows):
    head = ('<table class="stdlist"><tr><th>種類</th><th>タイトル</th><th>コース</th>'
            '<th>受付開始日時</th><th>受付終了日時</th></tr>')
    body = ""
    for href, title, course, start, end in rows:
        body += (f'<tr><td>x</td><td><a href="{href}">{title}</a></td>'
                 f'<td><a href="course_1678092">{course}</a></td><td>{start}</td><td>{end}</td></tr>')
    return "<html><body><a href='logout'>ログアウト</a>" + head + body + "</table></body></html>"


PAGES = {
    "/ct/home_library_query": table([
        ("course_1678092_query_111", "第3回 小テスト", "デザインと表現", "2026-10-01 09:00", "2026-10-08 23:59")]),
    "/ct/home_library_survey": ("<html><body><a href='logout'>ログアウト</a><table class='stdlist'><tr>"
                                "<th>タイトル</th><th>コース</th><th>受付開始日時</th><th>受付終了日時</th></tr>"
                                "</table><p>未提出のアンケートはありません</p></body></html>"),
    "/ct/home_library_report": table([
        ("course_1678092_report_222", "レポート課題1", "デザインと表現", "2026-10-01 09:00", "2026-10-06 12:00"),
        ("course_1700000_report_333", "最終レポート", "物理の世界", "2026-10-01 09:00", "")]),
    "/ct/course_1678092_report": table([
        ("course_1678092_report_222", "レポート課題1", "x", "2026-10-01", "2026-10-06 12:00"),
        ("course_1678092_report_999", "提出済みのレポート 提出済", "x", "2026-09-01", "2026-09-10 12:00")]),
}


class Handler(BaseHTTPRequestHandler):
    login_posts = 0          # ログインフォームが送信された回数(「1回しか送らない」テスト用)

    def log_message(self, *args):
        pass

    def _send(self, body, code=200, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        if "sess=ok" not in (self.headers.get("Cookie") or ""):
            return self._send(LOGIN) if path == "/ct/login" else self._send("", 302, {"Location": "/ct/login"})
        if path == "/ct/home":
            return self._send(HOME)
        if path in PAGES:
            return self._send(PAGES[path])
        self._send("not found", 404)

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path == "/ct/login":
            Handler.login_posts += 1
            if data.get("userid") == ["u1"] and data.get("password") == ["pw"] and data.get("csrf") == ["tok123"]:
                return self._send(AUTO)
            return self._send(LOGIN)
        if self.path == "/ct/sso_done" and data.get("SAMLResponse") == ["abc"]:
            return self._send("", 302, {"Location": "/ct/home", "Set-Cookie": "sess=ok; Path=/"})
        self._send("bad request", 400)


def start_server():
    """空いているポートでサーバーを起動し、(server, ベースURL) を返す"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
