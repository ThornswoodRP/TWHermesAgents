"""The app's chat search finds his own Hermes's chats by the words in their messages (TWHermesAgents #44).

Hermes Desktop searches every message through Hermes's own full-text index; the app's search matched titles
only. Checks the helper's `search` op against a stand-in for Hermes's store (the id lane, then the words of
every message, each word also as the start of a longer one, then titles; a compressed chat named by the chat
it goes on as; the snippet without the index's marks; only his own Hermes; a store that fails says so), the
connector's side (it asks Hermes's search for a searched page, and a search that could not run reaches the
phone as why, not as "no chat says that"), and the core keeping why only on an empty page.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_own import FakeCore, Gateways, passkey, state_db

from thornswood_decisions import own_connector
from thornswood_decisions.own import PAGES, clean_page
from thornswood_decisions.store import CoreError

HELPER = Path(own_connector.__file__).with_name("room_send.py")

# A stand-in for Hermes's store with the four calls the search makes. What each lane returns is in
# search.json; every call is logged with its arguments.
FAKE = {
    "hermes_state_registry.py": """
        import json, os
        from pathlib import Path
        HOME = Path(os.environ["TW_FAKE_HOME"])
        def log(name, **kw):
            with open(HOME / "calls.jsonl", "a") as f:
                f.write(json.dumps({"call": name, **kw}) + "\\n")
        class DB:
            def __init__(self):
                self.data = json.loads((HOME / "search.json").read_text())
                if self.data.get("broken"):
                    raise OSError("the store is locked")
            def search_sessions_by_id(self, q, limit=20, include_archived=True, source=None, sources=None,
                                      exclude_sources=None):
                log("by_id", q=q, limit=limit, include_archived=include_archived, exclude=exclude_sources)
                return [{"id": i} for i in self.data.get("by_id", [])]
            def search_messages(self, query, source_filter=None, exclude_sources=None, role_filter=None, limit=20,
                                offset=0, sort=None, include_inactive=False, fields=None, after_ts=None,
                                before_ts=None):
                log("messages", query=query, limit=limit, exclude=exclude_sources, fields=list(fields or ()))
                return [{"session_id": s, "snippet": t} for s, t in self.data.get("messages", [])]
            def list_sessions_rich(self, search_query=None, include_archived=False, order_by_last_active=False,
                                   exclude_sources=None, limit=20, **kw):
                log("titles", q=search_query, include_archived=include_archived, exclude=exclude_sources)
                return [{"id": i} for i in self.data.get("titles", [])]
            def get_compression_tip(self, sid):
                return self.data.get("tips", {}).get(sid, sid)
        def acquire(path=None):
            log("acquire", path=path)
            return DB()
        def release_or_close(db):
            log("release")
    """,
}


class TheHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name, text in FAKE.items():
            (self.dir / name).write_text(textwrap.dedent(text), encoding="utf-8")
        self.env = {**os.environ, "PYTHONPATH": str(self.dir), "TW_FAKE_HOME": str(self.dir)}

    def ask(self, store: dict, **req):
        (self.dir / "search.json").write_text(json.dumps(store))
        body = {"op": "search", "profile": "", "q": "nimb", "limit": 50, "exclude": ["kanban", "subagent"]} | req
        out = subprocess.run([sys.executable, str(HELPER)], input=json.dumps(body), capture_output=True, text=True,
                             env=self.env, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def calls(self):
        f = self.dir / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_ids_then_words_then_titles_each_chat_once_by_the_chat_it_goes_on_as(self):
        got = self.ask({"by_id": ["s_id"],
                        "messages": [("s_old", "the >>>nimbus<<< plan"), ("s_new", "again"), ("s_id", "x"),
                                     ("s_two", "a\n  >>>nimbus<<<   cloud")],
                        "titles": ["s_title", "s_new"], "tips": {"s_old": "s_new"}})
        self.assertEqual(got, {"found": [{"id": "s_id", "snippet": ""}, {"id": "s_new", "snippet": "the nimbus plan"},
                                         {"id": "s_two", "snippet": "a nimbus cloud"},
                                         {"id": "s_title", "snippet": ""}]})
        calls = {c["call"]: c for c in self.calls()}
        self.assertEqual(calls["acquire"]["path"], None, "his own Hermes's store")
        self.assertEqual(calls["messages"]["query"], "nimb*", "a word also finds a longer one, as Desktop's search")
        self.assertEqual(calls["messages"]["fields"], ["session_id", "snippet"])
        for lane in ("by_id", "messages", "titles"):
            self.assertEqual(calls[lane]["exclude"], ["kanban", "subagent"], lane)
        self.assertFalse(calls["by_id"]["include_archived"])
        self.assertIn("release", calls)

    def test_a_phrase_and_a_star_stay_as_typed_and_the_limit_holds(self):
        got = self.ask({"messages": [(f"s_{i}", "") for i in range(9)], "titles": ["s_t"]},
                       q=' "load order" rift* wisp ', limit=3)
        self.assertEqual([f["id"] for f in got["found"]], ["s_0", "s_1", "s_2"])
        calls = {c["call"]: c for c in self.calls()}
        self.assertEqual(calls["messages"]["query"], '"load order" rift* wisp*')
        self.assertEqual(calls["by_id"]["q"], '"load order" rift* wisp')
        self.assertNotIn("titles", calls, "no title lane once the page is full")

    def test_a_bad_request_reads_nothing(self):
        for bad in ({"q": ""}, {"q": "  "}, {"q": "two\nlines"}, {"q": "x" * 201}, {"q": 7}, {"limit": 0},
                    {"limit": 51}, {"limit": True}, {"exclude": ["../x"]}, {"exclude": "kanban"}):
            self.assertEqual(self.ask({}, **bad), {"found": None, "reason": "bad request"}, bad)
        self.assertEqual(self.calls(), [])

    def test_only_his_own_hermes_is_searched(self):
        self.assertEqual(self.ask({}, profile="rift"),
                         {"found": None, "reason": "a Bot's chats are not searched from here"})
        self.assertEqual(self.calls(), [])

    def test_a_store_that_fails_says_so(self):
        self.assertEqual(self.ask({"broken": True}),
                         {"found": None, "reason": "Hermes's search did not answer (OSError)"})


class TheConnector(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = state_db(self.dir)

    def hermes(self, script: str) -> own_connector.Hermes:
        (self.dir / "helper.py").write_text(textwrap.dedent(script), encoding="utf-8")
        return own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-I", str(self.dir / "helper.py")],
                                    self.dir, "One's Hermes")

    def test_it_asks_the_helper_with_the_chat_lists_rules(self):
        h = self.hermes(f"""
            import json, sys
            req = json.loads(sys.stdin.read())
            open({str(self.dir / 'req.json')!r}, "w").write(json.dumps(req))
            print(json.dumps({{"found": [{{"id": "s_plan", "snippet": "three things"}}]}}))
        """)
        self.assertEqual(h.search("three"), ([{"id": "s_plan", "snippet": "three things"}], ""))
        req = json.loads((self.dir / "req.json").read_text())
        self.assertEqual(req, {"op": "search", "profile": "", "q": "three", "limit": own_connector.PAGE_CHATS,
                               "exclude": list(own_connector.NOT_LISTED)})

    def test_a_search_that_could_not_run_says_why(self):
        self.assertEqual(self.hermes('print(\'{"found": null, "reason": "Hermes\\\'s search did not answer (OSError)"}\')')
                         .search("x"), (None, "Hermes's search did not answer (OSError)"))
        self.assertEqual(self.hermes("raise SystemExit(3)").search("x"),
                         (None, "could not run Hermes's search with Hermes's Python"))

    def test_the_found_page_lists_only_what_the_chat_list_lists_newest_first(self):
        found = [{"id": i, "snippet": "hit " + i} for i in
                 ("s_plan", "s_old", "s_once", "s_kanban", "s_sub", "s_arch", "s_tg", "s_gone")]
        page = own_connector.chats_page(self.db, None, "hit", found=found)
        self.assertEqual([(c["id"], c["preview"]) for c in page["chats"]],
                         [("s_plan", "hit s_plan"), ("s_tg", "hit s_tg")])
        self.assertFalse(page["more"])

    def test_his_bot_chat_is_found_by_its_words_or_its_name(self):
        self.assertEqual(own_connector.chats_page(self.db, None, "x", found=[{"id": "s_bot", "snippet": "the >run<"}])
                         ["chats"], [{"id": "s_bot", "title": "Bot Chat", "last_at": "1970-01-01T00:15:00+00:00",
                                      "preview": "the >run<", "bot_chat": True}])
        self.assertEqual([c["id"] for c in own_connector.chats_page(self.db, None, "bot chat", found=[])["chats"]],
                         ["s_bot"])
        self.assertEqual(own_connector.chats_page(self.db, None, "plan", found=[])["chats"], [])


class ThePhone(unittest.TestCase):
    def tearDown(self):
        with PAGES.cond:
            PAGES.held.clear()
            PAGES.asked.clear()

    def round(self, search):
        w = World()
        self.addCleanup(w.close)
        g = Gateways(w)
        g.hello("one")
        folder = Path(tempfile.mkdtemp())
        hermes = own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-I", "-c", "pass"], folder,
                                      "One's Hermes")
        hermes.ready = lambda: ("ready", "")
        hermes.search = search
        said = []
        conn = own_connector.OwnConnector(FakeCore(g.own, w.ws, "one", g.tok["one"]), hermes, state_db(folder),
                                          say=said.append)
        conn.once()
        p = passkey(w)
        self.assertEqual(g.own.page(p, "one", "chats", q="three"), {"pending": True})
        conn.once()
        return g.own.page(p, "one", "chats", q="three"), said

    def test_what_hermes_found_reaches_the_phone(self):
        got, _ = self.round(lambda q: ([{"id": "s_plan", "snippet": "three things"}], ""))
        self.assertEqual([(c["id"], c["preview"]) for c in got["chats"]], [("s_plan", "three things")])
        self.assertNotIn("why", got)

    def test_a_search_that_could_not_run_reaches_the_phone_as_why(self):
        got, said = self.round(lambda q: (None, "Hermes's search did not answer in a minute"))
        self.assertEqual((got["chats"], got["why"]), ([], "Hermes's search did not answer in a minute"))
        self.assertIn("FAIL page: Hermes's search did not answer in a minute", said)


class TheCore(unittest.TestCase):
    def test_why_goes_only_with_an_empty_page(self):
        self.assertEqual(clean_page("chats", {"chats": [], "more": False, "why": "w" * 300}),
                         {"chats": [], "more": False, "why": "w" * 200})
        row = {"id": "s_plan", "title": "Plan", "last_at": "", "preview": ""}
        self.assertNotIn("why", clean_page("chats", {"chats": [row], "more": False, "why": "stale"}))
        self.assertNotIn("why", clean_page("chats", {"chats": [], "more": False}))
        with self.assertRaises(CoreError):
            clean_page("chats", {"chats": [], "more": False, "why": {"x": 1}})


if __name__ == "__main__":
    unittest.main()
