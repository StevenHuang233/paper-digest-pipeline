from __future__ import annotations

import copy
import unittest
import xml.etree.ElementTree as ET
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from paper_digest.config import load_config
from paper_digest.sources.arxiv import ATOM, ATOM_ACCEPT, fetch_arxiv, parse_feed
from paper_digest.sources.common import USER_AGENT, get_bytes


FIXTURE = Path(__file__).parent / "fixtures" / "arxiv.xml"
TOTAL = "{http://a9.com/-/spec/opensearch/1.1/}totalResults"


def feed(start: int = 0, count: int = 1, total: int = 1) -> bytes:
    root = ET.fromstring(FIXTURE.read_bytes())
    template = root.find(f"{ATOM}entry")
    root.remove(template)
    root.find(TOTAL).text = str(total)
    for index in range(start, start + count):
        entry = copy.deepcopy(template)
        entry.find(f"{ATOM}id").text = f"https://arxiv.org/abs/2607.{index:05d}v1"
        root.append(entry)
    return ET.tostring(root)


def config_for_test(**overrides) -> dict:
    config, _ = load_config(Path(__file__).parent / "fixtures" / "dryrun.toml")
    config["discovery"].update({
        "source": "arxiv", "date": "2026-07-30", "request_attempts": 2,
        "page_size": 200, "max_candidates": 250, **overrides,
    })
    return config


def http_error(url: str, code: int = 406, retry_after: str = "") -> HTTPError:
    headers = Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return HTTPError(url, code, "Not Acceptable" if code == 406 else "Bad Request", headers, BytesIO())


class ArxivRecoveryTests(unittest.TestCase):
    def test_406_relaxes_accept_preserves_user_agent_and_closes_response(self):
        observed = []
        error = http_error("https://export.arxiv.org/api/query")
        response = BytesIO(FIXTURE.read_bytes())
        outcomes = iter([error, response])

        def open_url(request, timeout):
            observed.append((request.get_header("Accept"), request.get_header("User-agent")))
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("paper_digest.sources.common.urllib.request.urlopen", side_effect=open_url), patch(
            "paper_digest.sources.common.time.sleep",
        ) as sleep:
            papers = fetch_arxiv(config_for_test())
        self.assertEqual(len(papers), 1)
        self.assertEqual(observed, [(ATOM_ACCEPT, USER_AGENT), ("*/*", USER_AGENT)])
        sleep.assert_called_once_with(60.0)
        self.assertTrue(error.closed)
        self.assertTrue(response.closed)

    def test_406_retry_after_and_maximum_delay(self):
        for retry_after, expected in [("", 60.0), ("125", 125.0), ("900", 300.0)]:
            with self.subTest(retry_after=retry_after), patch(
                "paper_digest.sources.common.urllib.request.urlopen",
                side_effect=[http_error("https://export.arxiv.org/api/query", retry_after=retry_after),
                             BytesIO(FIXTURE.read_bytes())],
            ), patch("paper_digest.sources.common.time.sleep") as sleep:
                fetch_arxiv(config_for_test())
                sleep.assert_called_once_with(expected)

    def test_other_sources_do_not_retry_406_by_default(self):
        for code in (400, 406):
            error = http_error("https://example.test/feed", code)
            with self.subTest(code=code), patch(
                "paper_digest.sources.common.urllib.request.urlopen", side_effect=error,
            ) as opened, patch("paper_digest.sources.common.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, f"HTTP {code}"):
                    get_bytes("https://example.test/feed")
                opened.assert_called_once()
                sleep.assert_not_called()
                self.assertTrue(error.closed)

    def test_persistent_406_is_bounded_and_never_changes_source(self):
        errors = []

        def reject(request, timeout):
            self.assertEqual(urlsplit(request.full_url).netloc, "export.arxiv.org")
            error = http_error(request.full_url)
            errors.append(error)
            raise error

        with patch("paper_digest.sources.common.urllib.request.urlopen", side_effect=reject) as opened, patch(
            "paper_digest.sources.common.time.sleep",
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, r"HTTP 406 .*Not Acceptable.*Accept: \*/\*"):
                fetch_arxiv(config_for_test())
        self.assertEqual(opened.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60.0, 60.0, 60.0])
        self.assertTrue(all(error.closed for error in errors))

    def test_reduced_pages_keep_window_offsets_and_candidate_limit(self):
        calls = []

        def get(url):
            params = parse_qs(urlsplit(url).query)
            start = int(params["start"][0])
            size = int(params["max_results"][0])
            calls.append((start, size, params["search_query"][0]))
            if size > 100:
                raise RuntimeError(f"HTTP 406 for {url}")
            return feed(start, size, 500)

        papers = fetch_arxiv(config_for_test(), get=get)
        self.assertEqual([(start, size) for start, size, _ in calls],
                         [(0, 200), (0, 200), (0, 100), (100, 100), (200, 50)])
        self.assertEqual(len({query for _, _, query in calls}), 1)
        self.assertIn("submittedDate:[202607300000 TO 202607302359]", calls[0][2])
        self.assertEqual([paper.id for paper in papers], [f"2607.{index:05d}v1" for index in range(250)])

    def test_non_406_error_does_not_try_query_variants(self):
        with patch("paper_digest.sources.arxiv.get_bytes", side_effect=RuntimeError("HTTP 403 forbidden")) as get:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                fetch_arxiv(config_for_test())
        get.assert_called_once()

    def test_later_page_failure_does_not_return_partial_success(self):
        def get(url):
            if parse_qs(urlsplit(url).query)["start"] == ["0"]:
                return feed(total=2)
            raise RuntimeError(f"HTTP 406 for {url}")

        with self.assertRaisesRegex(RuntimeError, "HTTP 406"):
            fetch_arxiv(config_for_test(page_size=1), get=get)

    def test_empty_page_before_total_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "Incomplete arXiv response"):
            fetch_arxiv(config_for_test(), get=lambda url: feed(count=0, total=3))

    def test_valid_zero_results_are_allowed(self):
        self.assertEqual(parse_feed(feed(count=0, total=0)), ([], 0))
        self.assertEqual(fetch_arxiv(config_for_test(), get=lambda url: feed(count=0, total=0)), [])

    def test_xml_error_pages_are_rejected(self):
        invalid = [
            b"<html><body>Service unavailable</body></html>",
            f'<feed xmlns="{ATOM[1:-1]}" />'.encode(),
            feed(count=0, total=-1),
            feed(count=1, total=0),
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_feed(payload)

    def test_atom_api_error_is_not_a_paper(self):
        root = ET.fromstring(feed())
        entry = root.find(f"{ATOM}entry")
        entry.find(f"{ATOM}id").text = "http://arxiv.org/api/errors#incorrect_query"
        entry.find(f"{ATOM}summary").text = "incorrect query"
        with self.assertRaisesRegex(ValueError, "arXiv API error: incorrect query"):
            parse_feed(ET.tostring(root))

    def test_missing_entry_id_or_title_is_rejected(self):
        for field in ("id", "title"):
            root = ET.fromstring(feed())
            root.find(f"{ATOM}entry/{ATOM}{field}").text = ""
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_feed(ET.tostring(root))

    def test_xml_error_response_recovers_within_attempt_limit(self):
        responses = iter([b"<html>Unavailable</html>", feed()])
        with patch("paper_digest.sources.arxiv.time.sleep") as sleep:
            papers = fetch_arxiv(config_for_test(), get=lambda url: next(responses))
        self.assertEqual(len(papers), 1)
        sleep.assert_called_once_with(5.0)

    def test_persistent_invalid_xml_fails_instead_of_zero_papers(self):
        with patch("paper_digest.sources.arxiv.get_bytes", return_value=b"<html>Unavailable</html>") as get, patch(
            "paper_digest.sources.arxiv.time.sleep",
        ):
            with self.assertRaisesRegex(RuntimeError, "Invalid arXiv Atom response after 2 attempts"):
                fetch_arxiv(config_for_test())
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
