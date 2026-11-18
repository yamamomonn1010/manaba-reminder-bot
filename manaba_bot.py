#!/usr/bin/env python3
    # -*- coding: utf-8 -*-
"""
    概要説明：

    manaba 課題リマインダー Discord Bot (千葉工業大学 manaba 用)

    - manaba にログインして「未提出課題」(小テスト / アンケート / レポート)を定期取得
    - 新しい課題を検出したらDiscordの指定チャンネルに通知
    - 締切までの残り時間に応じて間隔を変えながらリマインド
    - スラッシュコマンドで一覧表示・完了・手入力追加ができる

    使い方：

    python manaba_bot.py --check              # manabaへのログインと取得だけを試す(Discord不要)
    python manaba_bot.py --check --dump-html  # 取得したHTMLを ./debug に保存(解析の調整用)
    python manaba_bot.py                      # Botを起動
    
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
import uuid as uuid #どうなるんだろこれ
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import discord
import requests
import unicodedata
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

#設定
load_dotenv()

def _env_int(name:str, default:int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default

JST = ZoneInfo("Asia/Tokyo")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID = _env_int("DISCORD_CHANNEL_ID", 0)      # 通知を送るチャンネル
OWNER_ID = _env_int("DISCORD_OWNER_ID", 0)          # 自分のユーザーID(コマンド制限とメンション用。任意)
GUILD_ID = _env_int("DISCORD_GUILD_ID", 0)          # サーバーID(指定するとコマンド反映が即時。任意)

#manabaの設定
# https://cit.manaba.jp/ct/home のように入れても、ベースURL(https://cit.manaba.jp)に直せる
BASE_URL = re.sub(r"/ct(/.*)?$", "", os.getenv("MANABA_BASE_URL", "https://cit.manaba.jp").strip().rstrip("/"))
MANABA_USER = os.getenv("MANABA_USER", "")
MANABA_PASSWORD = os.getenv("MANABA_PASSWORD", "")

#library = 「未提出課題一覧」ページから取得 / course = 各コースの課題ページを巡回
SOURCE_MODE = os.getenv("MANABA_SOURCE", "library").strip().lower()

#manabaへのアクセス頻度
SYNC_INTERVAL_MIN = max(15, _env_int("SYNC_INTERVAL_MIN", 30))
DB_PATH = os.getenv("DB_PATH", "tasks.db")

#お休みモード
QUIET_START = _env_int("QUIET_START_HOUR", 0)
QUIET_END = _env_int("QUIET_END_HOUR", 0)

#締切まで何日以上ならリマインドしないか
#新規検出時、/tasksでは出る。この日付に入るとリマインドが開始する
REMIND_WITHIN_DAYS = _env_int("REMIND_WITHIN_DAYS", 7)

DEFAULT_DUE_HOUR = _env_int("DEFAULT_DUE_HOUR", 23)
DEFAULT_DUE_MINUTE = _env_int("DEFAULT_DUE_MINUTE", 59)


# 残り時間(時間) -> リマインド間隔(時間)。上から順に判定する
#   残り72h以上: 24h毎 / 24h以上: 12h毎 / 12h以上: 6h毎 / それ未満: 3h毎

REMIND_STEPS = [(72, 24), (24, 12), (12, 6), (3, 3), (0, 1)]
KIND_LABEL = {"query": "小テスト", "survey": "アンケート", "report": "レポート"}
WEEKDAYS = "月火水木金土日"
DEBUG_DIR = Path("debug")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("manaba-bot")

#データ型、日時ユーティリティ
@dataclass
class Item:
    """manabaから取得した1件の課題"""
    id: str
    kind: str
    title: str
    course: str
    url: str
    due: Optional[datetime]

@dataclass
class SyncData:
    items_by_kind: Dict[str, List[Item]] = field(default_factory=dict)
    authoritative: bool = False     #消えた＝提出済み、なるほど！
    errors: List[str] = field(default_factory=list)

@dataclass
class SyncReport:
    first_run: bool = False
    new: List[Item] = field(default_factory=list)
    changed: List[Tuple[Item, Optional[str]]] = field(default_factory=list)
    auto_done: List[sqlite3.Row] = field(default_factory=list)

DATE_RE = re.compile(r"(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})日?\s*(\d{1,2})[:時]\s*(\d{2})")

# /add 用。日付: 10/10 10-10 10月10日 1010(月日4桁) / 時刻: 18:30 18時30分 18時 1830 18
USER_DATE_RE = re.compile(r"(\d{1,2})[/\-.月](\d{1,2})日?|(\d{2})(\d{2})")
USER_TIME_RE = re.compile(r"(\d{1,2})[:時](\d{1,2})分?|(\d{1,2})時|(\d{2})(\d{2})|(\d{1,2})")

def parse_dt(text: str) -> Optional[datetime]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=JST)
    except ValueError:
        return None

def parse_user_due(text: str) -> Optional[datetime]:
    """
    /add 用の締切の読み取り。全角でも半角でもよい。
      '2026-10-08 23:59'              年つき
      '10/8 23:59' '10月8日 18時' '1008 1830'   月日 + 時刻
      '1008' '10/8' '10月8日'          時刻を省略 → DEFAULT_DUE_HOUR:DEFAULT_DUE_MINUTE
    """
    t = unicodedata.normalize("NFKC", text or "").strip()    # 全角→半角('１０／１０ １０：００' → '10/10 10:00')
    dt = parse_dt(t)
    if dt:
        return dt
    parts = t.split()
    if len(parts) not in (1, 2):
        return None
    m = USER_DATE_RE.fullmatch(parts[0])
    if not m:
        return None
    month, day = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))
    hour, minute = DEFAULT_DUE_HOUR, DEFAULT_DUE_MINUTE
    if len(parts) == 2:
        tm = USER_TIME_RE.fullmatch(parts[1])
        if not tm:
            return None
        g = tm.groups()
        if g[0] is not None:
            hour, minute = int(g[0]), int(g[1])
        elif g[2] is not None:
            hour, minute = int(g[2]), 0
        elif g[3] is not None:
            hour, minute = int(g[3]), int(g[4])
        else:
            hour, minute = int(g[5]), 0
    now = datetime.now(JST)
    try:
        dt = datetime(now.year, month, day, hour, minute, tzinfo=JST)
        if dt < now - timedelta(days=1):          # 1日以上過去なら来年とみなす(12月に「1/5」と入れた場合など)
            dt = dt.replace(year=now.year + 1)
    except ValueError:                            # 13月・25時・存在しない日付
        return None
    return dt

def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec = "seconds") if dt else None

def from_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None

def fmt_due(dt: Optional[datetime]) -> str:
    if not dt:
        return "期限なし"
    return f"{dt.month}/{dt.day}({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"

def fmt_remaining(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        return "期限切れ"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}日{hours}時間"
    if hours:
        return f"{hours}時間{mins}分"
    return f"{mins}分"

def interval_for(remaining_hours: float) -> int:
    for threshold, interval in REMIND_STEPS:
        if remaining_hours >= threshold:
            return interval
    return REMIND_STEPS[-1][1]

def in_quiet_hours(now: datetime) -> bool:
    if QUIET_START == QUIET_END:
        return False
    h = now.hour
    if QUIET_START < QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END

#manabaから情報をとってくる
class LoginError(Exception):    #manabaログインエラー
    pass

class ParseError(Exception):    #manaba読み取りエラー
    pass

ITEM_HREF_RE = re.compile(r"course_(\d+)_(query|survey|report)_([A-Za-z0-9]+)")     #講義番号、種類、課題番号の形のリンクをみつけ取り出す
COURSE_HREF_RE = re.compile(r"course_(\d+)(?:$|[?#/])")                             #正規表現、(?:$|[?#/]) は「数字の直後が、文字列の末尾か ? # / のとき
LIBRARY_HREF_RE = re.compile(r"home_library_(query|survey|report)")
DONE_WORDS = ("提出済", "回答済", "受験済", "提出完了")
DEADLINE_HEAD_WORDS = ("終了", "締切", "締め切り", "期限")

class ManabaClient:
    def __init__(self, base: str, user: str, password: str, dump: bool = False):
        self.base = base
        self.user = user
        self.password = password
        self.dump = dump
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) manaba-reminder-bot/1.0 (personal use)",
            "Accept-Language": "ja,en;q=0.8",
        })
        self.logged_in = False
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = 1.0 - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _fix_encoding(self, r: requests.Response) -> requests.Response:
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = "utf-8"
        return r

    def _get(self, url: str) -> requests.Response:
        self._throttle()
        r = self.s.get(url, timeout=30)
        self._last_request = time.time()
        return self._fix_encoding(r)

    def _send_form(self, method: str, url: str, data: Dict[str, str]) -> requests.Response:
        self._throttle()
        if method.lower() == "get":
            r = self.s.get(url, params=data, timeout=30)
        else:
            r = self.s.post(url, data=data, timeout=30)
        self._last_request = time.time()
        return self._fix_encoding(r)

#デバック用HTMLに保存する
    def _dump(self, name: str, html: str) -> None:
        if not self.dump:
            return
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{name}.html").write_text(html, encoding="utf-8")

# ---- ログイン ----
    @staticmethod
    def _looks_logged_in(soup: BeautifulSoup) -> bool:
        for a in soup.find_all("a"):
            if "ログアウト" in a.get_text() or "logout" in (a.get("href") or "").lower():
                return True
        return False

    @staticmethod
    def _login_form(soup: BeautifulSoup):
        for form in soup.find_all("form"):
            if form.find("input", attrs={"type": "password"}):
                return form
        return None

    @staticmethod
    def _auto_submit_form(soup: BeautifulSoup):
        """SSOなどで挟まる「hiddenだけのフォーム」(自動送信ページ)を探す"""
        for form in soup.find_all("form"):
            inputs = form.find_all("input")
            hidden = [i for i in inputs if (i.get("type") or "").lower() == "hidden"]
            visible = [i for i in inputs if (i.get("type") or "text").lower() not in ("hidden", "submit", "button", "image")]
            if hidden and not visible:
                return form
        return None

    @staticmethod
    def _form_payload(form, page_url: str, creds: Optional[Tuple[str, str]] = None):
        data: Dict[str, str] = {}
        last_text_name: Optional[str] = None
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            if el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                data[name] = (opt.get("value", opt.get_text(strip=True)) if opt else "")
                continue
            if el.name == "textarea":
                data[name] = el.get_text()
                continue
            typ = (el.get("type") or "text").lower()
            if typ in ("submit", "button", "image", "reset", "file"):
                if typ == "submit" and name not in data:
                    data[name] = el.get("value", "")
                continue
            if typ in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    data[name] = el.get("value", "on")
                continue
            if typ == "password" and creds:
                data[name] = creds[1]
                if last_text_name:
                    data[last_text_name] = creds[0]
                continue
            if typ == "hidden":
                data[name] = el.get("value", "")
                continue
            # text / email / tel など(ユーザIDの入力欄候補)
            data[name] = el.get("value", "")
            last_text_name = name
        action = urljoin(page_url, form.get("action") or page_url)
        method = (form.get("method") or "post").lower()
        return method, action, data

#ログイン処理の本体
    def login(self) -> None:
        """ID/パスワードでログイン。資格情報の送信は1回だけ(連続失敗によるロック防止)。"""
        self.s.cookies.clear()
        self.logged_in = False
        r = self._get(f"{self.base}/ct/home")
        sent = False
        for step in range(8):
            #成功したため終了
            soup = BeautifulSoup(r.text, "lxml")
            self._dump(f"login_step{step}", r.text)
            if not soup.find("input", attrs={"type": "password"}) and self._looks_logged_in(soup):
                self.logged_in = True
                return
            form = self._login_form(soup)
            #なんかおかしい。ログイン後もログインフォーム
            if form is not None:
                if sent:
                    raise LoginError("ログイン後もログインフォームが表示されました。"
                                        "ID/パスワードの誤り、または追加の認証があるかもしれません。")
                method, action, data = self._form_payload(form, r.url, (self.user, self.password))
                r = self._send_form(method, action, data)
                sent = True
                continue
            auto = self._auto_submit_form(soup)
            #そのまま次へ
            if auto is not None:
                method, action, data = self._form_payload(auto, r.url)
                r = self._send_form(method, action, data)
                continue
            break
        raise LoginError("ログイン後のページを確認できませんでした。"
                             "`python manaba_bot.py --check --dump-html` で状況を確認してください。")

#セッション切れに対応
    #セッション切れなら再ログイン
    def get_page(self, url: str, dump_name: str = "") -> requests.Response:
            for _ in range(2):
                if not self.logged_in:
                    self.login()
                r = self._get(url)
                if dump_name:
                    self._dump(dump_name, r.text)
                if re.search(r"type=[\"']password[\"']", r.text, re.I):
                    self.logged_in = False   # セッション切れ → 再ログインして1回だけ再試行
                    continue
                return r
            raise LoginError("セッションを維持できませんでした。")

#一覧ページの解析
    @staticmethod
    def parse_list_page(html: str, page_url: str, skip_done: bool = False) -> Tuple[List[Item], bool]:
        """
        課題一覧のHTMLから課題を取り出す。
        戻り値: (課題のリスト, 一覧ページとして解釈できたか)
        ※ 列の位置は固定せず、ヘッダー(th)の文言と日付の形式から判断する
        """
        soup = BeautifulSoup(html, "lxml")
        items: Dict[str, Item] = {}
        has_header_table = False
        for table in soup.find_all("table"):
            ths: List[str] = []
            for tr in table.find_all("tr"):
                head = [th.get_text(strip=True) for th in tr.find_all("th")]
                if head:
                    ths = head
                    has_header_table = True
                    break

            #課題の行を見つける処理
            deadline_idx = next((i for i, h in enumerate(ths)
                                 if any(w in h for w in DEADLINE_HEAD_WORDS) and "開始" not in h), None)
            for tr in table.find_all("tr"):
                a_item = next((a for a in tr.find_all("a", href=True) if ITEM_HREF_RE.search(a["href"])), None)
                if a_item is None:
                    continue
                m = ITEM_HREF_RE.search(a_item["href"])
                cid, kind, iid = m.group(1), m.group(2), m.group(3)
                row_text = tr.get_text(" ", strip=True)
                if skip_done and any(w in row_text for w in DONE_WORDS):
                    continue
                course = ""
                for a in tr.find_all("a", href=True):
                    if COURSE_HREF_RE.search(a["href"]) and a.get_text(strip=True):
                        course = a.get_text(" ", strip=True)
                        break
                cells = tr.find_all("td")
                due: Optional[datetime] = None
                if deadline_idx is not None and len(cells) == len(ths):
                    due = parse_dt(cells[deadline_idx].get_text(" ", strip=True))
                else:   # ヘッダーが読めない場合は、最後に見つかった日付を締切とみなす
                    for c in reversed(cells):
                        due = parse_dt(c.get_text(" ", strip=True))
                        if due:
                            break

                #
                item_id = f"{kind}:{cid}:{iid}"
                items[item_id] = Item(
                    id=item_id, kind=kind, title=a_item.get_text(" ", strip=True) or "(無題)",
                    course=course or f"コース{cid}", url=urljoin(page_url, a_item["href"]), due=due,
                )
        empty_ok = has_header_table or "ありません" in soup.get_text()
        return list(items.values()), bool(items) or empty_ok

#ここすごいね
#課題が0件に見える理由は2つある。(1)本当に未提出がない (2)画面が変わって読めていない。
#(2)を(1)と同じに扱うと、DBの全課題が「提出済み」として自動完了されてしまう。
#そこで「解釈できたか」を別に返し、読めなかった場合は失敗として扱えるようにしている。
#ってわけらしいい

#取得の入り口
    def fetch_library(self) -> SyncData:
        """「未提出課題」一覧(小テスト/アンケート/レポート)から取得"""
        data = SyncData(authoritative=True)
        urls = {k: f"{self.base}/ct/home_library_{k}" for k in KIND_LABEL}
        home = self.get_page(f"{self.base}/ct/home", "home")
        for a in BeautifulSoup(home.text, "lxml").find_all("a", href=True):   # 実際のリンクがあればそちらを優先
            m = LIBRARY_HREF_RE.search(a["href"])
            if m:
                urls[m.group(1)] = urljoin(home.url, a["href"])
        for kind, url in urls.items():
            try:
                r = self.get_page(url, f"library_{kind}")
                items, ok = self.parse_list_page(r.text, r.url)
                if not ok:
                    raise ParseError("一覧表を解釈できませんでした")
                data.items_by_kind[kind] = items
            except LoginError:
                raise
            except Exception as e:  # 1種類の失敗で全体を止めない
                data.errors.append(f"{KIND_LABEL[kind]}: {e}")
        return data

    def fetch_courses(self) -> SyncData:
        """各コースの課題ページを巡回して取得(提出済みは除外。自動完了判定はしない)"""
        data = SyncData(authoritative=False)
        home = self.get_page(f"{self.base}/ct/home", "home")
        courses: Dict[str, str] = {}
        for a in BeautifulSoup(home.text, "lxml").find_all("a", href=True):
            m = COURSE_HREF_RE.search(a["href"])
            if m and a.get_text(strip=True):
                courses.setdefault(m.group(1), a.get_text(" ", strip=True))
        if not courses:
            raise ParseError("ホームからコース一覧を見つけられませんでした")
        for kind in KIND_LABEL:
            data.items_by_kind[kind] = []
        for cid, cname in courses.items():
            for kind in KIND_LABEL:
                try:
                    r = self.get_page(f"{self.base}/ct/course_{cid}_{kind}", f"course_{cid}_{kind}")
                    items, _ = self.parse_list_page(r.text, r.url, skip_done=True)
                    for it in items:
                        it.course = cname
                    data.items_by_kind[kind].extend(items)
                except LoginError:
                    raise
                except Exception as e:
                    data.errors.append(f"{cname}/{KIND_LABEL[kind]}: {e}")
        return data

    def fetch(self) -> SyncData:
        return self.fetch_courses() if SOURCE_MODE == "course" else self.fetch_library()

#データベース（SQLlite）
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,              -- 'manaba' | 'manual'
            kind TEXT,                         -- query | survey | report | NULL(手入力)
            title TEXT NOT NULL,
            course TEXT,
            url TEXT,
            due TEXT,                          -- ISO8601(JST) / NULL=期限なし
            done INTEGER NOT NULL DEFAULT 0,
            done_by TEXT,                      -- 'user' | 'auto'
            last_notified_at TEXT,
            missing_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
def meta_set(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))

def meta_get(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def apply_sync(data: SyncData) -> SyncReport:
    """取得結果をDBに反映(新規追加 / 締切変更 / 提出済みの自動完了)"""
    now_s = iso(datetime.now(JST))
    rep = SyncReport()
    with db() as c:
        rep.first_run = c.execute("SELECT COUNT(*) FROM tasks WHERE source='manaba'").fetchone()[0] == 0
        for kind, items in data.items_by_kind.items():
            seen = set()
            for it in items:
                seen.add(it.id)

#初めて見る課題を＿＿＿追加
                row = c.execute("SELECT * FROM tasks WHERE id=?", (it.id,)).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO tasks(id,source,kind,title,course,url,due,done,last_notified_at,"
                        "missing_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?,0,?,?)",
                        (it.id, "manaba", it.kind, it.title, it.course, it.url, iso(it.due), now_s, now_s, now_s))
                    rep.new.append(it)
                    continue

#既にある課題の期限が変更されたりの場合
                due_changed = row["due"] != iso(it.due)
                reopen = row["done"] == 1 and row["done_by"] == "auto"
                c.execute(
                    "UPDATE tasks SET title=?, course=?, url=?, due=?, missing_count=0, updated_at=?, "
                    "last_notified_at=CASE WHEN ? THEN NULL ELSE last_notified_at END, "
                    "done=CASE WHEN ? THEN 0 ELSE done END, done_by=CASE WHEN ? THEN NULL ELSE done_by END "
                    "WHERE id=?",
                    (it.title, it.course, it.url, iso(it.due), now_s, int(due_changed), int(reopen), int(reopen), it.id))
                if due_changed and not row["done"]:
                    rep.changed.append((it, row["due"]))

#一覧から消えた課題を提出済みに
            if data.authoritative:
                # 未提出一覧から2回続けて消えたもの = 提出済みとみなして自動完了
                rows = c.execute("SELECT * FROM tasks WHERE source='manaba' AND kind=? AND done=0", (kind,)).fetchall()
                for row in rows:
                    if row["id"] in seen:
                        continue
                    mc = row["missing_count"] + 1
                    if mc >= 2:
                        c.execute("UPDATE tasks SET done=1, done_by='auto', missing_count=?, updated_at=? WHERE id=?",
                                  (mc, now_s, row["id"]))
                        rep.auto_done.append(row)
                    else:
                        c.execute("UPDATE tasks SET missing_count=?, updated_at=? WHERE id=?", (mc, now_s, row["id"]))
    return rep

#未提出の課題を「締め切りが近い順」に取り出す
def pending_rows() -> List[sqlite3.Row]:
    with db() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE done=0 ORDER BY due IS NULL, due ASC").fetchall()

#「通知するべき課題」を選ぶ
def collect_reminders(now: datetime) -> List[sqlite3.Row]:
    out = []
    with db() as c:
        for row in c.execute("SELECT * FROM tasks WHERE done=0 AND due IS NOT NULL ORDER BY due ASC"):
            remaining = from_iso(row["due"]) - now
            if remaining.total_seconds() <= 0:
                continue

            #締切がまだ遠い課題は、リマインドしない
            if REMIND_WITHIN_DAYS and remaining > timedelta(days=REMIND_WITHIN_DAYS):
                continue              
            
            interval = timedelta(hours = interval_for(remaining.total_seconds() / 3600))
            last = from_iso(row["last_notified_at"])
            if last is None or now - last >= interval - timedelta(minutes = 1):
                out.append(row)
    return out

#「通知した」を記憶する
def mark_notified(ids: List[str], now: datetime) -> None:
    if not ids:
        return
    with db() as c:
        c.executemany("UPDATE tasks SET last_notified_at=? WHERE id=?", [(iso(now), i) for i in ids])

#====================================
# #形を整えるパートだよん
#====================================
def _esc(text: str) -> str:
    """課題名・科目名が、Discordのリンク書式やMarkdownを壊さないようにする。
    [ ] は全角にする。_ * ~ | ` などは、記号ではなく文字として表示する(\\_ のように打ち消す)。"""
    text = (text or "").replace("[", "［").replace("]", "］")
    return discord.utils.escape_markdown(text)


# リンクの表示名の中では、「\_」のような打ち消しが効かず、\ がそのまま見えてしまう。
# そこで、Markdownに使われる記号は、見た目がそっくりな全角の記号に置き換える。
_LINK_TEXT_TABLE = str.maketrans({"[": "［", "]": "］", "_": "＿", "*": "＊", "~": "～", "|": "｜", "`": "｀"})


def _esc_link(text: str) -> str:
    return (text or "").translate(_LINK_TEXT_TABLE)



#DBの1行を、表示用の文字にする
def row_line(row: sqlite3.Row, now: datetime) -> str:
    label = KIND_LABEL.get(row["kind"], "手入力")
    name = f"[{_esc_link(row['title'])}]({row['url']})" if row["url"] else _esc(row["title"])
    course = f"{_esc(row['course'])}｜" if row["course"] else ""
    due = from_iso(row["due"])
    when = f"締切 {fmt_due(due)}"
    if due:
        when += f"(あと{fmt_remaining(due - now)})" if due > now else "(期限切れ)"
    return f"**[{label}]** {course}{name}\n　{when}"

#Itemから表示用の文字を
def item_line(it: Item, now: datetime) -> str:
    label = KIND_LABEL.get(it.kind, "課題")
    when = f"締切 {fmt_due(it.due)}"
    if it.due:
        when += f"(あと{fmt_remaining(it.due - now)})" if it.due > now else "(期限切れ)"
    return f"**[{label}]** {_esc(it.course)}｜[{_esc_link(it.title)}]({it.url})\n　{when}"

#カードを作る
def build_embed(title: str, lines: List[str], color: int) -> discord.Embed:
    body, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 2 > 3800:
            body.append(f"…ほか{len(lines) - i}件")
            break
        body.append(line)
        used += len(line) + 2
    return discord.Embed(title=title, description="\n\n".join(body), color=color)

#緊急度で色をチェンジ
def urgency_color(rows: List[sqlite3.Row], now: datetime) -> int:
    soonest = min((from_iso(r["due"]) for r in rows if r["due"]), default=None)
    if soonest is None:
        return 0x3498DB
    hours = (soonest - now).total_seconds() / 3600
    return 0xE74C3C if hours < 24 else 0xF39C12 if hours < 72 else 0x2ECC71

#====================================
#Bot本体
#====================================

#Owner専用
class OwnerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("このBotは所有者専用です。", ephemeral=True)
            return False
        return True

#Bot本体
    #syncはコマンドの登録
class ReminderClient(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = OwnerTree(self)

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        sync_loop.start()
        remind_loop.start()

    async def on_ready(self) -> None:
        log.info("ログイン完了: %s", self.user)

#Bot全体で共有するもの
client = ReminderClient()
tree = client.tree

SYNC_LOCK = asyncio.Lock()
_manaba: Optional[ManabaClient] = None
_state = {"login_failures": 0, "paused": False, "last_error_sent": ""}
MENTION = f"<@{OWNER_ID}>" if OWNER_ID else None

#ログイン状態を使いまわす処理
def get_manaba() -> ManabaClient:
    global _manaba
    if _manaba is None:
        _manaba = ManabaClient(BASE_URL, MANABA_USER, MANABA_PASSWORD)
    return _manaba

#チャンネルにメッセージを送る
async def post(content: Optional[str] = None, embed: Optional[discord.Embed] = None) -> bool:
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(CHANNEL_ID)
        except discord.DiscordException as e:
            log.error("通知チャンネルを取得できません: %s", e)
            return False
    try:
        await ch.send(content=content, embed=embed)
        return True
    except discord.DiscordException as e:
        log.error("送信に失敗しました: %s", e)
        return False


async def do_sync(manual: bool) -> str:
    """manabaから取得 → DB反映 → 通知。結果の要約文を返す"""
    async with SYNC_LOCK:
        try:
            data: SyncData = await asyncio.to_thread(get_manaba().fetch)
        except LoginError as e:
            _state["login_failures"] += 1
            msg = f"⚠️ manabaにログインできませんでした: {e}"
            if _state["login_failures"] >= 3:
                _state["paused"] = True
                msg += "\nロック防止のため自動同期を停止しました。ID/パスワードを確認し、`/sync` で再開できます。"
            if not manual and _state["login_failures"] in (1, 3):
                await post(msg)
            meta_set("last_sync", f"{datetime.now(JST):%m/%d %H:%M} ログイン失敗")
            return msg
        except Exception as e:
            log.exception("同期中にエラー")
            msg = f"⚠️ 同期中にエラーが発生しました: {type(e).__name__}: {e}"
            if not manual and _state["last_error_sent"] != msg:
                _state["last_error_sent"] = msg
                await post(msg)
            meta_set("last_sync", f"{datetime.now(JST):%m/%d %H:%M} エラー")
            return msg

        #DBに反映
        _state["login_failures"] = 0
        _state["paused"] = False
        _state["last_error_sent"] = ""
        rep = apply_sync(data)
        now = datetime.now(JST)

        if rep.new:
            title = f"📥 初回同期: {len(rep.new)}件取り込みました" if rep.first_run \
                else f"🆕 新しい課題を{len(rep.new)}件検出しました"
            lines = [item_line(it, now) for it in sorted(rep.new, key=lambda i: (i.due is None, i.due))]
            await post(MENTION if not rep.first_run else None, build_embed(title, lines, 0x3498DB))
        if rep.changed:
            lines = [f"{item_line(it, now)}\n　(変更前: {fmt_due(from_iso(old))})" for it, old in rep.changed]
            await post(MENTION, build_embed("📝 締切が変更されました", lines, 0x9B59B6))
        if data.errors:
            await post("⚠️ 一部の取得に失敗しました:\n" + "\n".join(f"・{e}" for e in data.errors[:10]))

#結果発表
        summary = (f"同期完了: 新規{len(rep.new)}件 / 締切変更{len(rep.changed)}件 / "
                   f"提出済み(自動完了){len(rep.auto_done)}件 / 取得失敗{len(data.errors)}件")
        meta_set("last_sync", f"{now:%m/%d %H:%M} {summary}")
        log.info(summary)
        return summary

#30minおきにmanabaを見に行く
@tasks.loop(minutes=SYNC_INTERVAL_MIN)
async def sync_loop() -> None:
    if _state["paused"]:
        return
    try:
        await do_sync(manual=False)
    except Exception:
        log.exception("sync_loop で予期しないエラー")

#5minおきにリマインドを確認し送る
@tasks.loop(minutes=5)
async def remind_loop() -> None:
    try:
        now = datetime.now(JST)
        if in_quiet_hours(now):
            return
        rows = collect_reminders(now)
        if not rows:
            return
        embed = build_embed("⏰ 課題リマインド", [row_line(r, now) for r in rows], urgency_color(rows, now))
        if await post(MENTION, embed):
            mark_notified([r["id"] for r in rows], now)
    except Exception:
        log.exception("remind_loop で予期しないエラー")

#接続が完了するまで待つ
@sync_loop.before_loop
@remind_loop.before_loop
async def _wait_ready() -> None:
    await client.wait_until_ready()

#-----スラッシュコマンド-----
#入力中に候補を表示
async def task_autocomplete(interaction:discord.Interaction, current: str):
    cur = current.lower()
    out = []
    for r in pending_rows():
        label = f"[{KIND_LABEL.get(r['kind'], '手入力')}] {r['title']} ({fmt_due(from_iso(r['due']))})"[:100]
        if cur in label.lower():
            out.append(app_commands.Choice(name=label, value=r["id"]))
        if len(out) >= 25:
            break
    return out

async def manual_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE source='manual' ORDER BY due IS NULL, due").fetchall()
    return [app_commands.Choice(name=f"{r['title']} ({fmt_due(from_iso(r['due']))})"[:100], value=r["id"])
            for r in rows if cur in r["title"].lower()][:25]

#未完了の課題を一覧表示コマンド
@tree.command(name="tasks", description="未完了の課題を一覧表示します")
@app_commands.describe(include_overdue="期限切れも表示する")
async def cmd_tasks(interaction: discord.Interaction, include_overdue: bool = False):
    now = datetime.now(JST)
    rows = [r for r in pending_rows()
            if include_overdue or not r["due"] or from_iso(r["due"]) > now]
    if not rows:
        await interaction.response.send_message("未完了の課題はありません 🎉")
        return
    await interaction.response.send_message(
        embed=build_embed(f"📚 未完了の課題 ({len(rows)}件)", [row_line(r, now) for r in rows], urgency_color(rows, now)))

#同期するコマンド
@tree.command(name="sync", description="今すぐmanabaから課題を取得します")
async def cmd_sync(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    _state["paused"] = False
    _state["login_failures"] = 0
    await interaction.followup.send(await do_sync(manual=True))

#課題を完了にするコマンド
@tree.command(name="done", description="課題を完了にします")
@app_commands.describe(task="完了にする課題")
@app_commands.autocomplete(task=task_autocomplete)
async def cmd_done(interaction: discord.Interaction, task: str):
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            await interaction.response.send_message("その課題が見つかりません。候補から選んでください。", ephemeral=True)
            return
        c.execute("UPDATE tasks SET done=1, done_by='user', updated_at=? WHERE id=?", (iso(datetime.now(JST)), task))
    await interaction.response.send_message(f"✅ 完了にしました: {row['title']}")

#課題を手入力で追加するためのコマンド
@tree.command(name="add", description="課題を手入力で追加します(manaba以外の予定用)")
@app_commands.describe(title="課題名", due="締切 例: 2026-10-08 23:59 または 10/8 23:59", course="科目名(任意)")
async def cmd_add(interaction: discord.Interaction, title: str, due: str, course: str = ""):
    dt = parse_user_due(due)
    if dt is None:
        await interaction.response.send_message("締切は `2026-10-08 23:59` または `10/8 23:59` の形式で入力してください。",
                                                ephemeral=True)
        return
    now_s = iso(datetime.now(JST))
    tid = f"manual:{uuid.uuid4().hex[:8]}"
    with db() as c:
        c.execute("INSERT INTO tasks(id,source,kind,title,course,url,due,done,last_notified_at,missing_count,"
                  "created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?,0,?,?)",
                  (tid, "manual", None, title, course, None, iso(dt), None, now_s, now_s))
    await interaction.response.send_message(f"➕ 追加しました: {title}(締切 {fmt_due(dt)})")

#手入力した課題を消す。（間違えた時用）
@tree.command(name="remove", description="手入力した課題を削除します")
@app_commands.describe(task="削除する課題(手入力分のみ)")
@app_commands.autocomplete(task=manual_autocomplete)
async def cmd_remove(interaction: discord.Interaction, task: str):
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=? AND source='manual'", (task,)).fetchone()
        if row is None:
            await interaction.response.send_message("手入力の課題だけ削除できます。", ephemeral=True)
            return
        c.execute("DELETE FROM tasks WHERE id=?", (task,))
    await interaction.response.send_message(f"🗑️ 削除しました: {row['title']}")

#Botがちゃんと動いているか調べられる
@tree.command(name="status", description="Botの状態を表示します")
async def cmd_status(interaction: discord.Interaction):
    rows = pending_rows()
    text = (f"最終同期: {meta_get('last_sync', 'まだ同期していません')}\n"
            f"未完了: {len(rows)}件 / 同期間隔: {SYNC_INTERVAL_MIN}分 / 取得方式: {SOURCE_MODE}\n"
            f"自動同期: {'⏸ 停止中(/syncで再開)' if _state['paused'] else '稼働中'}")
    await interaction.response.send_message(text)

#=====================================
#動作確認モードおおおおおおおおお
#=====================================

def run_check(dump: bool) -> int:
    if not (MANABA_USER and MANABA_PASSWORD):
        print("`.env` に MANABA_USER と MANABA_PASSWORD を設定してください。")
        return 1
    m = ManabaClient(BASE_URL, MANABA_USER, MANABA_PASSWORD, dump=dump)
    print(f"ベースURL: {BASE_URL} / 取得方式: {SOURCE_MODE}")
    try:
        m.login()
        print("✅ ログイン成功")
        data = m.fetch()
    except (LoginError, ParseError) as e:
        print(f"❌ {e}")
        if not dump:
            print("→ `--dump-html` を付けて再実行すると、HTMLを ./debug に保存します。")
        return 2
    now = datetime.now(JST)
    for kind, items in data.items_by_kind.items():
        print(f"\n■ {KIND_LABEL[kind]}: {len(items)}件")
        for it in sorted(items, key=lambda i: (i.due is None, i.due)):
            print(f"  - {it.course} | {it.title} | 締切 {fmt_due(it.due)}"
                  + (f" (あと{fmt_remaining(it.due - now)})" if it.due and it.due > now else ""))
    for e in data.errors:
        print(f"⚠️ {e}")
    if dump:
        print(f"\nHTMLを {DEBUG_DIR.resolve()} に保存しました(個人情報を含むので、共有前に氏名などを伏せてください)。")
    return 0

#起動できるようにするよ
def main() -> None:
    ap = argparse.ArgumentParser(description="manaba課題リマインダーBot")
    ap.add_argument("--check", action="store_true", help="manabaへのログインと課題取得だけを試す")
    ap.add_argument("--dump-html", action="store_true", help="取得したHTMLを ./debug に保存する")
    args = ap.parse_args()

    if args.check:
        sys.exit(run_check(args.dump_html))

    missing = [n for n, v in [("DISCORD_TOKEN", DISCORD_TOKEN), ("DISCORD_CHANNEL_ID", CHANNEL_ID),
                              ("MANABA_USER", MANABA_USER), ("MANABA_PASSWORD", MANABA_PASSWORD)] if not v]
    if missing:
        print(".env に次の項目を設定してください: " + ", ".join(missing))
        sys.exit(1)
    init_db()
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()