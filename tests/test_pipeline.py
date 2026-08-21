from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from zoneinfo import ZoneInfo

from paper_digest.backends import OpenAICompatibleBackend
from paper_digest.budget import Budget
from paper_digest.cli import _overrides
from paper_digest.config import load_config, validate_config
from paper_digest.emailer import build_message, read_result, resolve_smtp_settings, send_digest_email
from paper_digest.filtering import llm_prioritize, llm_rerank, rule_rank
from paper_digest.fulltext import CUTOFF
from paper_digest.models import Paper
from paper_digest.outputs import latex_escape, render_latex, render_markdown, sanitize_text, write_outputs
from paper_digest.orchestrator import _completion_status, job_label, rank_and_select, run_pipeline
from paper_digest.review_prompt import build_review_prompt
from paper_digest.sources.arxiv import (
    build_query,
    build_range_query,
    fetch_arxiv,
    freeze_relative_window,
    parse_feed,
    resolve_date,
    resolve_relative_window,
    set_explicit_window,
)
from paper_digest.sources.common import get_bytes
from paper_digest.sources.crossref import parse_items, venue_similarity
from paper_digest.sources.openreview import parse_notes


class ArxivTests(unittest.TestCase):
    def test_network_timeout_is_retried(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"recovered"

        with patch(
            "paper_digest.sources.common.urllib.request.urlopen",
            side_effect=[TimeoutError("slow response"), FakeResponse()],
        ) as mocked_open, patch("paper_digest.sources.common.time.sleep") as mocked_sleep:
            payload = get_bytes("https://example.test/feed", attempts=2, backoff_seconds=0.01)
        self.assertEqual(payload, b"recovered")
        self.assertEqual(mocked_open.call_count, 2)
        mocked_sleep.assert_called_once_with(0.01)

    def test_rate_limit_honors_retry_after_before_recovering(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"recovered"

        headers = Message()
        headers["Retry-After"] = "125"
        rate_limit = HTTPError(
            "https://example.test/feed", 429, "Rate exceeded", headers,
            BytesIO(b"Rate exceeded."),
        )
        with patch(
            "paper_digest.sources.common.urllib.request.urlopen",
            side_effect=[rate_limit, FakeResponse()],
        ), patch("paper_digest.sources.common.time.sleep") as mocked_sleep:
            payload = get_bytes(
                "https://example.test/feed", attempts=2,
                backoff_seconds=5, rate_limit_backoff_seconds=60,
                max_backoff_seconds=300,
            )
        self.assertEqual(payload, b"recovered")
        mocked_sleep.assert_called_once_with(125.0)

    def test_rate_limit_without_header_uses_long_fallback(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"recovered"

        rate_limit = HTTPError(
            "https://example.test/feed", 429, "Rate exceeded", Message(),
            BytesIO(b"Rate exceeded."),
        )
        with patch(
            "paper_digest.sources.common.urllib.request.urlopen",
            side_effect=[rate_limit, FakeResponse()],
        ), patch("paper_digest.sources.common.time.sleep") as mocked_sleep:
            get_bytes("https://example.test/feed", attempts=2)
        mocked_sleep.assert_called_once_with(60.0)

    def test_invalid_atom_response_is_retried(self):
        config, _ = load_config(Path(__file__).parent / "fixtures" / "dryrun.toml")
        config["discovery"].update({"source": "arxiv", "date": "2026-07-30"})
        fixture = (Path(__file__).parent / "fixtures" / "arxiv.xml").read_bytes()
        responses = iter([b"Rate exceeded.", fixture])
        with patch("paper_digest.sources.arxiv.time.sleep") as mocked_sleep:
            papers = fetch_arxiv(config, get=lambda _url: next(responses))
        self.assertEqual([paper.id for paper in papers], ["2607.12345v1"])
        mocked_sleep.assert_called_once_with(5.0)

    def test_date_and_query(self):
        date = resolve_date("2026-07-28")
        self.assertEqual(str(date), "2026-07-28")
        query = build_query(date, ["cs.LG", "cs.AI"])
        self.assertIn("submittedDate:[202607280000 TO 202607282359]", query)
        self.assertIn("cat:cs.LG OR cat:cs.AI", query)

    def test_relative_window_uses_local_boundaries_and_gmt_query(self):
        discovery = {"window": {
            "timezone": "Asia/Shanghai",
            "start_days_ago": 2, "start_time": "12:00",
            "end_days_ago": 1, "end_time": "12:00",
        }}
        now = dt.datetime(2026, 8, 16, 1, 30, tzinfo=dt.timezone.utc)
        start, end = resolve_relative_window(discovery, now=now)

        self.assertEqual(start, dt.datetime(2026, 8, 14, 4, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(end, dt.datetime(2026, 8, 15, 4, 0, tzinfo=dt.timezone.utc))
        shanghai = ZoneInfo("Asia/Shanghai")
        self.assertEqual(start.astimezone(shanghai).isoformat(), "2026-08-14T12:00:00+08:00")
        self.assertEqual(end.astimezone(shanghai).isoformat(), "2026-08-15T12:00:00+08:00")
        self.assertEqual(
            build_range_query(start, end, []),
            "submittedDate:[202608140400 TO 202608150359]",
        )

    def test_relative_window_is_frozen_for_one_pipeline_run(self):
        discovery = {"window": {
            "timezone": "Asia/Shanghai",
            "start_days_ago": 2, "start_time": "12:00",
            "end_days_ago": 1, "end_time": "12:00",
        }}
        frozen = freeze_relative_window(
            discovery,
            now=dt.datetime(2026, 8, 16, 15, 59, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(resolve_relative_window(discovery), frozen)
        self.assertEqual(
            discovery["_resolved_window_utc"],
            ["2026-08-14T04:00:00+00:00", "2026-08-15T04:00:00+00:00"],
        )

    def test_explicit_window_uses_configured_timezone_and_stable_label(self):
        config = {"discovery": {
            "source": "arxiv", "date": "yesterday", "window": {
                "enabled": True, "timezone": "Asia/Shanghai",
            },
        }}
        args = argparse.Namespace(
            date=None,
            window_start="2026-08-17 12:00",
            window_end="2026-08-18 12:00",
            source=None, max_papers=None, max_selected=None, backend=None,
        )
        _overrides(config, args)
        self.assertEqual(
            resolve_relative_window(config["discovery"]),
            (
                dt.datetime(2026, 8, 17, 4, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 8, 18, 4, 0, tzinfo=dt.timezone.utc),
            ),
        )
        self.assertEqual(
            job_label(config),
            "arxiv-2026-08-17-1200_to_2026-08-18-1200",
        )

    def test_explicit_window_rejects_incomplete_or_reversed_values(self):
        discovery = {"window": {"enabled": True, "timezone": "Asia/Shanghai"}}
        with self.assertRaisesRegex(ValueError, "later than"):
            set_explicit_window(
                discovery, "2026-08-18 12:00", "2026-08-17 12:00",
            )
        config = {"discovery": {
            "source": "arxiv", "date": "yesterday",
            "window": {"enabled": True, "timezone": "Asia/Shanghai"},
        }}
        args = argparse.Namespace(
            date=None, window_start="2026-08-17 12:00", window_end=None,
            source=None, max_papers=None, max_selected=None, backend=None,
        )
        with self.assertRaisesRegex(ValueError, "provided together"):
            _overrides(config, args)

    def test_explicit_date_override_disables_relative_window(self):
        config = {"discovery": {"date": "yesterday", "window": {"enabled": True}}}
        args = argparse.Namespace(
            date="2026-08-15", window_start=None, window_end=None,
            source=None, max_papers=None,
            max_selected=None, backend=None,
        )
        _overrides(config, args)
        self.assertEqual(config["discovery"]["date"], "2026-08-15")
        self.assertFalse(config["discovery"]["window"]["enabled"])

    def test_reversed_relative_window_is_rejected(self):
        config, _ = load_config(Path(__file__).parent / "fixtures" / "dryrun.toml")
        config["discovery"]["source"] = "arxiv"
        config["discovery"]["window"].update({
            "enabled": True,
            "start_days_ago": 1,
            "end_days_ago": 2,
        })
        with self.assertRaisesRegex(ValueError, "start must be earlier"):
            validate_config(config, require_backend=False)

    def test_parse_feed(self):
        fixture = Path(__file__).parent / "fixtures" / "arxiv.xml"
        papers, total = parse_feed(fixture.read_bytes())
        self.assertEqual(total, 1)
        self.assertEqual(papers[0].id, "2607.12345v1")
        self.assertEqual(papers[0].categories, ["cs.LG"])
        self.assertTrue(papers[0].pdf_url.endswith("2607.12345v1"))

    def test_explicit_date_is_used_in_stable_job_label(self):
        config = {"discovery": {"source": "arxiv", "date": "2026-07-30"}}
        self.assertEqual(job_label(config), "arxiv-2026-07-30")

    def test_relative_window_is_used_in_stable_job_label(self):
        config = {"discovery": {
            "source": "arxiv", "date": "yesterday", "window": {"enabled": True},
        }}
        with patch(
            "paper_digest.orchestrator.relative_window_label",
            return_value="2026-08-14-1200_to_2026-08-15-1200",
        ):
            self.assertEqual(
                job_label(config),
                "arxiv-2026-08-14-1200_to_2026-08-15-1200",
            )


class SelectionTests(unittest.TestCase):
    def test_rules_rank_and_exclude(self):
        papers = [
            Paper(id="a", title="Graph Agents", abstract="retrieval with a graph neural network", categories=["cs.LG"]),
            Paper(id="b", title="Medical Imaging", abstract="graph segmentation", categories=["cs.CV"]),
        ]
        preferences = {
            "interests": ["graph neural network", "retrieval agent"],
            "include_keywords": ["graph"], "exclude_keywords": ["medical imaging"], "categories": ["cs.LG"],
        }
        ranked = rule_rank(papers, preferences)
        self.assertEqual(ranked[0].id, "a")
        self.assertEqual(ranked[-1].score, -1.0)

    def test_budget_is_hard_gate(self):
        budget = Budget(100, 1.0, 0.0, 0.0)
        with self.assertRaisesRegex(RuntimeError, "Token budget exceeded"):
            budget.reserve("x" * 1000, 20)

    def test_llm_binary_decision_never_reintroduces_hard_exclusions(self):
        papers = [
            Paper(id="blocked", title="Blocked Medical Paper", selection_decision="hard_exclude", score=-1.0),
            Paper(id="keep", title="Graph Agent", abstract="A graph reasoning agent.", selection_decision="rule_score"),
        ]

        class FakeRanker:
            prompt = ""

            def generate_json(self, system, prompt, **kwargs):
                self.prompt = prompt
                return {"decisions": [{
                    "id": "p000001", "decision": "include",
                    "match_area": "A",
                    "reason": "Direct graph-agent alignment.",
                }]}, {}

        backend = FakeRanker()
        ranked = llm_rerank(papers, {"interests": ["graph agent"]}, backend)
        kept = next(paper for paper in ranked if paper.id == "keep")
        blocked = next(paper for paper in ranked if paper.id == "blocked")
        self.assertEqual(kept.score, 0.0)
        self.assertEqual(kept.selection_decision, "include")
        self.assertEqual(blocked.selection_decision, "hard_exclude")
        self.assertNotIn("Blocked Medical Paper", backend.prompt)
        self.assertIn("Do not assign scores", backend.prompt)

    def test_two_thousand_candidates_are_batched_and_capped_at_five_hundred(self):
        papers = [Paper(id=f"paper-{index}", title=f"Graph Agent {index}", abstract="Graph agent method.") for index in range(2000)]

        class BulkRanker:
            calls = 0

            def generate_json(self, system, prompt, **kwargs):
                self.calls += 1
                batch = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                return {"decisions": [{
                    "id": item["id"], "decision": "include",
                    "match_area": "A",
                    "reason": "Relevant graph-agent paper.",
                } for item in batch]}, {}

        backend = BulkRanker()
        config = {
            "preferences": {"interests": ["graph agent"], "include_keywords": [], "exclude_keywords": [], "categories": []},
            "selection": {
                "ranker": "llm", "min_score": 0.0, "max_selected_papers": 500,
                "llm_batch_size": 40, "llm_abstract_chars": 1600,
                "llm_max_output_tokens": 4000, "llm_thinking_mode": "disabled",
                "decision_rounds": 1,
            },
            "backend": {},
        }
        selected = rank_and_select(config, papers, backend=backend)
        self.assertEqual(len(selected), 500)
        self.assertEqual(backend.calls, 50)
        self.assertTrue(all(paper.selection_decision == "include" for paper in selected))

    def test_llm_binary_decision_rejects_incomplete_batches(self):
        papers = [Paper(id="one", title="One"), Paper(id="two", title="Two")]

        class IncompleteRanker:
            def generate_json(self, system, prompt, **kwargs):
                return {"decisions": [{
                    "id": "p000000", "decision": "exclude", "match_area": "none", "reason": "Not relevant.",
                }]}, {}

        with self.assertRaisesRegex(RuntimeError, "omitted 1 of 2"):
            llm_rerank(papers, {"interests": ["multimodal"]}, IncompleteRanker(), response_attempts=1)

    def test_llm_binary_decision_retries_an_incomplete_batch(self):
        papers = [Paper(id="one", title="One"), Paper(id="two", title="Two")]

        class RecoveringRanker:
            calls = 0

            def generate_json(self, system, prompt, **kwargs):
                self.calls += 1
                count = 1 if self.calls == 1 else 2
                return {"decisions": [
                    {"id": f"p{index:06d}", "decision": "exclude", "match_area": "none", "reason": "Not relevant."}
                    for index in range(count)
                ]}, {}

        backend = RecoveringRanker()
        result = llm_rerank(papers, {"interests": ["multimodal"]}, backend)
        self.assertEqual(len(result), 2)
        self.assertEqual(backend.calls, 2)

    def test_binary_only_selection_respects_configured_limit_and_source_order(self):
        papers = [Paper(id=f"paper-{index}", title=f"Paper {index}") for index in range(8)]

        class IncludeAllBackend:
            def generate_json(self, system, prompt, **kwargs):
                compact = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                return {"decisions": [
                    {"id": item["id"], "decision": "include", "match_area": "A", "reason": "Relevant."}
                    for item in compact
                ]}, {}

        config = {
            "preferences": {"interests": ["paper"], "include_keywords": [], "exclude_keywords": [], "categories": []},
            "selection": {
                "ranker": "llm", "min_score": 0.0, "max_selected_papers": 3,
                "llm_batch_size": 40, "llm_abstract_chars": 1600,
                "llm_max_output_tokens": 4000, "llm_thinking_mode": "disabled",
                "decision_policy": "Include relevant papers.", "llm_prioritize": False,
            },
            "backend": {},
        }
        selected = rank_and_select(config, papers, backend=IncludeAllBackend())
        self.assertEqual([paper.id for paper in selected], ["paper-0", "paper-1", "paper-2"])

    def test_llm_priority_uses_rotating_panels_without_model_scores(self):
        papers = [
            Paper(id=f"paper-{index}", title=f"Multimodal Method {index}", abstract="Detailed method and experiments.")
            for index in range(120)
        ]

        class PriorityBackend:
            calls = 0
            prompts = []

            def generate_json(self, system, prompt, **kwargs):
                self.calls += 1
                self.prompts.append(prompt)
                compact = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                quota = int(prompt.split("Return at most ", 1)[1].split(" unique papers", 1)[0])
                return {"shortlist": [
                    {"id": item["id"], "reason": "Strong fit and substantive experiments."}
                    for item in compact[:quota]
                ]}, {}

        backend = PriorityBackend()
        selected = llm_prioritize(
            papers, backend, max_papers=50, batch_size=40,
            priority_policy="Prefer high-value multimodal methods.",
        )
        self.assertEqual(len(selected), 50)
        self.assertEqual(backend.calls, 9)
        self.assertTrue(all("Do not output scores" in prompt for prompt in backend.prompts))
        self.assertTrue(selected[0].score_reasons[-1].startswith("LLM priority #1 ("))

    def test_llm_priority_accepts_a_shorter_shortlist(self):
        papers = [Paper(id=f"p{index}", title=f"Paper {index}") for index in range(3)]

        class ShortPriorityBackend:
            def generate_json(self, system, prompt, **kwargs):
                return {"shortlist": [{"id": "p000000", "reason": "Only strong candidate."}]}, {}

        selected = llm_prioritize(
            papers, ShortPriorityBackend(), max_papers=2, batch_size=3, rounds=1,
        )
        self.assertEqual([paper.id for paper in selected], ["p0"])

    def test_llm_priority_caps_an_oversized_shortlist(self):
        papers = [Paper(id=f"p{index}", title=f"Paper {index}") for index in range(3)]

        class OversizedPriorityBackend:
            def generate_json(self, system, prompt, **kwargs):
                compact = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                return {"shortlist": [
                    {"id": item["id"], "reason": "Candidate returned by provider."}
                    for item in compact
                ]}, {}

        selected = llm_prioritize(
            papers, OversizedPriorityBackend(), max_papers=2, batch_size=3, rounds=1,
        )
        self.assertEqual(len(selected), 2)

    def test_rotating_priority_panels_do_not_starve_the_source_tail(self):
        papers = [
            Paper(id=f"paper-{index}", title=f"Multimodal Method {index}", abstract="Relevant method.")
            for index in range(64)
        ]

        class PositionBiasedBackend:
            def generate_json(self, system, prompt, **kwargs):
                compact = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                quota = int(prompt.split("Return at most ", 1)[1].split(" unique papers", 1)[0])
                return {"shortlist": [
                    {"id": item["id"], "reason": "Preferred within this panel."}
                    for item in compact[:quota]
                ]}, {}

        selected = llm_prioritize(
            papers, PositionBiasedBackend(), max_papers=50, batch_size=50,
            rounds=3, shuffle_seed="tail-bias-regression",
        )
        selected_ids = {paper.id for paper in selected}
        self.assertEqual(len(selected_ids), 50)
        self.assertTrue(any(f"paper-{index}" in selected_ids for index in range(50, 64)))
        self.assertNotEqual(selected_ids, {f"paper-{index}" for index in range(50)})

    def test_conflicting_binary_rounds_receive_final_adjudication(self):
        papers = [
            Paper(id="target", title="Multimodal Evidence Method", abstract="Diagnoses visual evidence failures."),
            Paper(id="other", title="Unrelated Paper", abstract="Generic application."),
        ]

        class DisagreeingBackend:
            calls = 0

            def generate_json(self, system, prompt, **kwargs):
                self.calls += 1
                compact = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                target_decision = "include" if "pass 1/2" in prompt or "final adjudication" in prompt else "exclude"
                return {"decisions": [{
                    "id": item["id"],
                    "decision": target_decision if item["id"] == "p000000" else "exclude",
                    "match_area": "B" if item["id"] == "p000000" and target_decision == "include" else "none",
                    "reason": "Direct visual-evidence method." if item["id"] == "p000000" else "Outside policy.",
                } for item in compact]}, {}

        backend = DisagreeingBackend()
        ranked = llm_rerank(
            papers, {"interests": ["visual evidence"]}, backend,
            decision_policy="B. Visual evidence methods.", decision_rounds=2,
            decision_shuffle_seed="decision-regression",
        )
        target = next(paper for paper in ranked if paper.id == "target")
        self.assertEqual(backend.calls, 3)
        self.assertEqual(target.selection_decision, "include")
        self.assertEqual(target.selection_scores, {"include_votes": 1, "decision_rounds": 2})
        self.assertIn("adjudicated", target.score_reasons[0])

    def test_rank_and_select_applies_configured_priority_shortlist(self):
        papers = [
            Paper(id=f"paper-{index}", title=f"Multimodal Paper {index}", abstract="A relevant method.")
            for index in range(60)
        ]

        class CombinedBackend:
            def generate_json(self, system, prompt, **kwargs):
                compact = json.loads(prompt.rsplit("Papers:\n", 1)[1])
                if "Decide whether to include" in prompt:
                    return {"decisions": [
                        {"id": item["id"], "decision": "include", "match_area": "A", "reason": "Relevant multimodal method."}
                        for item in compact
                    ]}, {}
                quota = int(prompt.split("Return at most ", 1)[1].split(" unique papers", 1)[0])
                return {"shortlist": [
                    {"id": item["id"], "reason": "High-value research fit."}
                    for item in reversed(compact[:quota])
                ]}, {}

        config = {
            "preferences": {"interests": ["multimodal"], "include_keywords": [], "exclude_keywords": [], "categories": []},
            "selection": {
                "ranker": "llm", "min_score": 0.0, "max_selected_papers": 10,
                "llm_batch_size": 40, "llm_abstract_chars": 1600,
                "llm_max_output_tokens": 4000, "llm_thinking_mode": "disabled",
                "decision_policy": "Include relevant multimodal papers.",
                "llm_prioritize": True, "priority_batch_size": 30,
                "priority_abstract_chars": 1200, "priority_max_output_tokens": 5000,
                "priority_policy": "Prefer the most valuable methods.",
            },
            "backend": {},
        }
        selected = rank_and_select(config, papers, backend=CombinedBackend())
        self.assertEqual(len(selected), 10)
        self.assertTrue(all(any("LLM priority" in reason for reason in paper.score_reasons) for paper in selected))


class BackendTests(unittest.TestCase):
    def test_deepseek_json_and_thinking_parameters(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                }).encode()

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode())
            return FakeResponse()

        config = {
            "api_key_env": "PAPER_DIGEST_API_KEY", "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash", "temperature": 0.2, "max_output_tokens": 100,
            "timeout_seconds": 30, "json_mode": True, "thinking_mode": "disabled",
            "supports_thinking_toggle": True,
        }
        with patch.dict(os.environ, {"PAPER_DIGEST_API_KEY": "test-only-key"}), patch("urllib.request.urlopen", fake_urlopen):
            value, usage = OpenAICompatibleBackend(config).generate_json("JSON only", "Return JSON")
        self.assertTrue(value["ok"])
        self.assertEqual(usage["output_tokens"], 2)
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})

    def test_transient_model_api_failure_is_retried(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'

        config = {
            "api_key_env": "PAPER_DIGEST_API_KEY", "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash", "temperature": 0.2, "max_output_tokens": 100,
            "timeout_seconds": 30, "request_attempts": 2, "request_backoff_seconds": 0,
            "json_mode": True, "thinking_mode": "disabled", "supports_thinking_toggle": True,
        }
        with patch.dict(os.environ, {"PAPER_DIGEST_API_KEY": "test-only-key"}), patch(
            "urllib.request.urlopen", side_effect=[TimeoutError("temporary timeout"), FakeResponse()]
        ) as mocked_open:
            value, _usage = OpenAICompatibleBackend(config).generate_json("JSON only", "Return JSON")
        self.assertTrue(value["ok"])
        self.assertEqual(mocked_open.call_count, 2)


class EmailTests(unittest.TestCase):
    def _config(self):
        config, _ = load_config(Path(__file__).parent / "fixtures" / "dryrun.toml")
        config["email"].update({
            "enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 465,
            "security": "ssl", "username_env": "TEST_SMTP_USER",
            "password_env": "TEST_SMTP_PASSWORD", "to_env": "TEST_EMAIL_TO",
            "from_env": "TEST_EMAIL_FROM", "attach_pdf": True,
            "attach_markdown": True, "attach_log_on_failure": True,
        })
        return config

    def test_message_contains_counts_and_configured_attachments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            markdown = root / "digest.md"
            pdf = root / "digest.pdf"
            markdown.write_text("# Digest", encoding="utf-8")
            pdf.write_bytes(b"%PDF-test")
            result = {
                "job_label": "arxiv-2026-08-10",
                "candidate_count": 200, "selected_count": 12,
                "review_target_count": 2, "completed_count": 2, "failed_count": 0,
                "selection_decisions": {"include": 12, "exclude": 188, "hard_exclude": 0},
                "outputs": {"markdown": str(markdown), "pdf": str(pdf)},
            }
            env = {
                "TEST_SMTP_USER": "sender@example.com", "TEST_SMTP_PASSWORD": "test-app-password",
                "TEST_EMAIL_TO": "one@example.com,two@example.com", "TEST_EMAIL_FROM": "",
            }
            with patch.dict(os.environ, env, clear=False):
                message, username, password, recipients = build_message(
                    self._config(), result, status="success", run_url="https://example.com/run/1",
                )
            self.assertEqual(username, "sender@example.com")
            self.assertEqual(password, "test-app-password")
            self.assertEqual(recipients, ["one@example.com", "two@example.com"])
            self.assertIn("候选论文：200", message.get_body().get_content())
            self.assertEqual(
                sorted(part.get_filename() for part in message.iter_attachments()),
                ["digest-2026-08-10.pdf", "digest.md"],
            )

    def test_smtp_ssl_is_mocked_and_password_is_not_returned(self):
        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, **kwargs):
                captured.update({"host": host, "port": port})

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def login(self, username, password):
                captured.update({"username": username, "password": password})

            def send_message(self, message):
                captured["subject"] = str(message["Subject"])

        env = {
            "TEST_SMTP_USER": "sender@example.com", "TEST_SMTP_PASSWORD": "secret-value",
            "TEST_EMAIL_TO": "receiver@example.com", "TEST_EMAIL_FROM": "",
        }
        with patch.dict(os.environ, env, clear=False), patch("smtplib.SMTP_SSL", FakeSMTP):
            response = send_digest_email(self._config(), {}, status="failure")
        self.assertTrue(response["sent"])
        self.assertEqual(captured["host"], "smtp.example.com")
        self.assertNotIn("secret-value", json.dumps(response))

    def test_duplicate_recipients_are_sent_only_once(self):
        env = {
            "TEST_SMTP_USER": "sender@example.com", "TEST_SMTP_PASSWORD": "test-app-password",
            "TEST_EMAIL_TO": "Receiver@example.com, receiver@example.com;other@example.com",
            "TEST_EMAIL_FROM": "",
        }
        with patch.dict(os.environ, env, clear=False):
            message, _username, _password, recipients = build_message(
                self._config(), {}, status="failure",
            )
        self.assertEqual(recipients, ["Receiver@example.com", "other@example.com"])
        self.assertEqual(message["To"], "Receiver@example.com, other@example.com")

    def test_invalid_or_missing_result_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(read_result(path), {})
            self.assertEqual(read_result(Path(temp) / "missing.json"), {})

    def test_success_email_requires_a_compiled_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            markdown = Path(temp) / "digest.md"
            markdown.write_text("# Digest", encoding="utf-8")
            result = {"outputs": {"markdown": str(markdown)}}
            env = {
                "TEST_SMTP_USER": "sender@example.com", "TEST_SMTP_PASSWORD": "test-app-password",
                "TEST_EMAIL_TO": "receiver@example.com", "TEST_EMAIL_FROM": "",
            }
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(RuntimeError, "requires digest.pdf"):
                    build_message(self._config(), result, status="success")

    def test_qq_gmail_and_netease_provider_presets(self):
        base = {"smtp_host": "", "smtp_port": 0, "security": "auto"}
        cases = [
            ("qq", "sender@qq.com", ("smtp.qq.com", 465, "ssl", "qq")),
            ("gmail", "sender@gmail.com", ("smtp.gmail.com", 465, "ssl", "gmail")),
            ("netease", "sender@163.com", ("smtp.163.com", 465, "ssl", "netease")),
            ("netease", "sender@126.com", ("smtp.126.com", 465, "ssl", "netease")),
            ("netease", "sender@yeah.net", ("smtp.yeah.net", 465, "ssl", "netease")),
        ]
        for provider, username, expected in cases:
            with self.subTest(provider=provider, username=username):
                settings = base | {"provider": provider}
                self.assertEqual(resolve_smtp_settings(settings, username), expected)

    def test_auto_provider_detection_and_custom_override(self):
        auto = {"provider": "auto", "smtp_host": "", "smtp_port": 0, "security": "auto"}
        self.assertEqual(
            resolve_smtp_settings(auto, "sender@googlemail.com"),
            ("smtp.gmail.com", 465, "ssl", "gmail"),
        )
        custom = {
            "provider": "custom", "smtp_host": "mail.example.com",
            "smtp_port": 587, "security": "starttls",
        }
        self.assertEqual(
            resolve_smtp_settings(custom, "sender@example.com"),
            ("mail.example.com", 587, "starttls", "custom"),
        )

    def test_netease_preset_rejects_unknown_sender_domain(self):
        settings = {"provider": "netease", "smtp_host": "", "smtp_port": 0, "security": "auto"}
        with self.assertRaisesRegex(RuntimeError, "NetEase preset supports"):
            resolve_smtp_settings(settings, "sender@example.com")


class FullTextTests(unittest.TestCase):
    def test_main_text_cutoff_recognizes_common_backmatter_headings(self):
        headings = [
            "Appendix A: Additional Results", "A. Appendix", "Supplementary Material",
            "Supplemental Information", "8 References", "Acknowledgments",
        ]
        for heading in headings:
            with self.subTest(heading=heading):
                self.assertIsNotNone(CUTOFF.search(heading))


class OpenReviewTests(unittest.TestCase):
    def test_parse_v2_note_values(self):
        payload = {"notes": [{
            "id": "note1", "forum": "forum1", "cdate": 123,
            "content": {
                "title": {"value": "A Conference Paper"},
                "abstract": {"value": "An abstract."},
                "authors": {"value": ["A", "B"]},
                "keywords": {"value": ["agents"]},
            },
        }]}
        paper = parse_notes(payload, "ICLR.cc/2026/Conference")[0]
        self.assertEqual(paper.title, "A Conference Paper")
        self.assertEqual(paper.authors, ["A", "B"])
        self.assertIn("forum1", paper.url)


class CrossrefTests(unittest.TestCase):
    def test_parse_proceedings_item(self):
        item = {
            "DOI": "10.1/example", "title": ["A Graph Paper"],
            "container-title": ["Advances in Neural Information Processing Systems 37"],
            "abstract": "<jats:p>An <b>abstract</b>.</jats:p>",
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "published": {"date-parts": [[2024, 12, 1]]},
            "URL": "https://doi.org/10.1/example", "subject": ["AI"],
        }
        self.assertGreater(venue_similarity("Neural Information Processing Systems", item["container-title"]), 0.8)
        paper = parse_items([item], "Neural Information Processing Systems", 0.1)[0]
        self.assertEqual(paper.authors, ["Ada Lovelace"])
        self.assertEqual(paper.abstract, "An abstract .")
        self.assertEqual(paper.published, "2024-12-01")


class OutputTests(unittest.TestCase):
    def test_partial_delivery_policy_keeps_usable_digest_green(self):
        self.assertEqual(_completion_status(49, 50, fail_on_incomplete=False), "complete")
        self.assertEqual(_completion_status(49, 50, fail_on_incomplete=True), "partial")
        self.assertEqual(_completion_status(0, 50, fail_on_incomplete=False), "partial")

    def test_failed_math_compile_retries_with_safe_text_and_produces_pdf(self):
        record = {
            "paper": {"title": "Risky $x&y$ title", "url": "https://arxiv.org/abs/1", "authors": []},
            "review": {key: "Formula $x&y$ and explanation." for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]}
            | {"evidence_level": "fulltext", "limitations": ""},
        }
        attempts = []

        def fake_compile(engine, tex_path, run_dir):
            attempts.append(tex_path.read_text(encoding="utf-8"))
            if len(attempts) == 1:
                return False, "Misplaced alignment tab character &"
            (run_dir / "digest.pdf").write_bytes(b"%PDF-safe-retry")
            return True, "safe compile ok"

        with tempfile.TemporaryDirectory() as temp:
            with patch("paper_digest.outputs.shutil.which", return_value="xelatex"):
                with patch("paper_digest.outputs._compile_latex", side_effect=fake_compile):
                    outputs = write_outputs(Path(temp), [record], ["latex"], True)
            self.assertEqual(len(attempts), 2)
            self.assertIn("$x&y$", attempts[0])
            self.assertIn(r"\$x\&y\$", attempts[1])
            self.assertTrue(Path(outputs["pdf"]).is_file())
            self.assertTrue((Path(temp) / "latex-error-first-pass.log").is_file())

    def test_output_failure_preserves_progress_manifest(self):
        config_path = Path(__file__).parent / "fixtures" / "dryrun.toml"
        config, resolved = load_config(config_path)
        config["fulltext"]["download_pdf"] = False
        config["output"] = {"formats": ["latex"], "compile_pdf": True}

        class FakeBackend:
            def generate_json(self, system, prompt, **kwargs):
                value = {
                    key: f"Readable {key} paragraph."
                    for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]
                }
                value.update({"evidence_level": "abstract", "limitations": ""})
                return value, {}

        with tempfile.TemporaryDirectory() as temp:
            config["project"]["output_dir"] = temp
            with patch("paper_digest.orchestrator.make_backend", return_value=FakeBackend()), patch(
                "paper_digest.orchestrator.write_outputs", side_effect=RuntimeError("synthetic PDF failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic PDF failure"):
                    run_pipeline(config, resolved)
            manifest = json.loads(next(Path(temp).glob("*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["failure_stage"], "output")
        self.assertEqual(manifest["completed_count"], 1)
        self.assertIn("synthetic PDF failure", manifest["error"])

    def test_discovery_failure_preserves_the_actual_error(self):
        config_path = Path(__file__).parent / "fixtures" / "dryrun.toml"
        config, resolved = load_config(config_path)
        with tempfile.TemporaryDirectory() as temp:
            config["project"]["output_dir"] = temp
            with patch(
                "paper_digest.orchestrator.discover",
                side_effect=RuntimeError("HTTP 429: Rate exceeded"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Rate exceeded"):
                    run_pipeline(config, resolved)
            manifest = json.loads(next(Path(temp).glob("*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["failure_stage"], "discovery")
        self.assertIn("HTTP 429: Rate exceeded", manifest["error"])

    def test_selection_failure_is_also_written_to_the_manifest(self):
        config_path = Path(__file__).parent / "fixtures" / "dryrun.toml"
        config, resolved = load_config(config_path)
        with tempfile.TemporaryDirectory() as temp:
            config["project"]["output_dir"] = temp
            with patch(
                "paper_digest.orchestrator.rank_and_select",
                side_effect=RuntimeError("selection backend unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
                    run_pipeline(config, resolved)
            manifest = json.loads(next(Path(temp).glob("*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["failure_stage"], "selection")
        self.assertIn("selection backend unavailable", manifest["error"])

    def test_latex_escape_distinguishes_currency_from_math(self):
        rendered = latex_escape("cost $100/千条轨迹, then $1.36; model $β$ uses λ")
        self.assertIn(r"\$100/千条轨迹", rendered)
        self.assertIn(r"\$1.36", rendered)
        self.assertIn(r"$\beta$", rendered)
        self.assertIn(r"\ensuremath{\lambda}", rendered)

    def test_model_control_characters_are_repaired_before_every_output(self):
        self.assertEqual(sanitize_text("mean \bar{g}\x00"), r"mean \bar{g}")
        record = {
            "paper": {"title": "Paper", "url": "https://arxiv.org/abs/1", "authors": []},
            "review": {
                key: "平均梯度 \bar{g}，扰动为 δ。"
                for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]
            } | {"evidence_level": "fulltext", "limitations": ""},
        }
        with tempfile.TemporaryDirectory() as temp:
            outputs = write_outputs(Path(temp), [record], ["json", "markdown", "latex"], False)
            for path in outputs.values():
                content = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("\x08", content)
                self.assertNotIn("\x00", content)
            self.assertIn(r"\bar{g}", Path(outputs["markdown"]).read_text(encoding="utf-8"))
            self.assertIn(r"\ensuremath{\delta}", Path(outputs["latex"]).read_text(encoding="utf-8"))

    def test_latex_sections_do_not_duplicate_automatic_numbers(self):
        record = {
            "paper": {"title": "Paper", "url": "https://arxiv.org/abs/1", "authors": []},
            "review": {key: "Text" for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]}
            | {"evidence_level": "fulltext", "limitations": ""},
        }
        rendered = render_latex([record])
        self.assertIn(r"\section{论文精读}", rendered)
        self.assertIn(r"\subsection{Paper}", rendered)
        self.assertNotIn(r"\subsection{1. Paper}", rendered)

    def test_latex_uses_generic_daily_digest_template_and_dynamic_evidence_counts(self):
        records = [
            {
                "paper": {
                    "title": "Full Text Paper", "url": "https://arxiv.org/abs/1",
                    "authors": ["Ada Lovelace"], "published": "2026-07-31T01:02:03Z",
                    "venue": "ExampleConf", "categories": ["cs.AI", "cs.LG"],
                },
                "review": {key: "正文。" for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]}
                | {"evidence_level": "fulltext", "limitations": "局限。"},
            },
            {
                "paper": {"title": "Abstract Paper", "url": "https://arxiv.org/abs/2", "authors": []},
                "review": {key: "摘要。" for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]}
                | {"evidence_level": "abstract", "limitations": ""},
            },
        ]
        rendered = render_latex(records)
        self.assertIn("每日论文精读", rendered)
        self.assertIn("六段式深度总结", rendered)
        self.assertIn("本期收录：2 篇", rendered)
        self.assertIn("全文 1｜摘要 1｜元数据 0", rendered)
        self.assertIn(r"\newcommand{\paperbox}", rendered)
        self.assertIn(r"\textbf{分类标签：}cs.AI; cs.LG", rendered)
        self.assertIn(r"\href{\detokenize{https://arxiv.org/abs/1}}{打开论文来源}", rendered)
        self.assertIn(r"\parthead{Method｜实现流程与关键公式}", rendered)
        self.assertNotIn("张文涛老师", rendered)

    def test_chinese_review_prompt_requires_simplified_chinese_fields(self):
        prompt = build_review_prompt(Paper(id="x", title="Paper"), "Evidence", "fulltext", "zh-CN")
        self.assertIn("所有叙述字段必须使用简体中文", prompt)
        self.assertIn("Do not return an English narrative paragraph", prompt)

    def test_markdown_has_clickable_link_and_sections(self):
        record = {
            "paper": {"title": "Paper", "url": "https://arxiv.org/abs/1", "authors": [], "published": "", "venue": ""},
            "review": {key: "Text" for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]} | {"evidence_level": "fulltext", "limitations": ""},
        }
        text = render_markdown([record])
        self.assertIn("[Paper](https://arxiv.org/abs/1)", text)
        self.assertIn("核心思想 / Core Idea", text)

    def test_end_to_end_generation_and_resume_with_fake_backend(self):
        config_path = Path(__file__).parent / "fixtures" / "dryrun.toml"
        config, resolved = load_config(config_path)
        config["fulltext"]["download_pdf"] = False
        config["output"] = {"formats": ["json", "markdown", "latex"], "compile_pdf": False}

        class FakeBackend:
            calls = 0

            def generate_json(self, system, prompt, **kwargs):
                self.calls += 1
                value = {key: f"Readable {key} paragraph." for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]}
                value.update({"evidence_level": "abstract", "limitations": "Full text was unavailable."})
                return value, {"input_tokens": 100, "output_tokens": 200}

        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as temp:
            config["project"]["output_dir"] = temp
            with patch("paper_digest.orchestrator.make_backend", return_value=backend):
                first = run_pipeline(config, resolved)
                second = run_pipeline(config, resolved)
            self.assertEqual(first["status"], "complete")
            self.assertEqual(second["completed_count"], 1)
            self.assertEqual(backend.calls, 1)
            self.assertTrue(Path(first["outputs"]["json"]).exists())
            self.assertTrue(Path(first["outputs"]["latex"]).exists())

    def test_fulltext_review_retries_a_transient_model_failure(self):
        config_path = Path(__file__).parent / "fixtures" / "dryrun.toml"
        config, resolved = load_config(config_path)
        config["fulltext"]["download_pdf"] = False
        config["output"] = {"formats": ["json"], "compile_pdf": False}
        config["review"]["max_attempts"] = 3

        class FlakyBackend:
            calls = 0

            def generate_json(self, system, prompt, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary provider error")
                value = {
                    key: f"Readable {key} paragraph."
                    for key in ["background", "motivation", "idea", "method", "experiments", "conclusion"]
                }
                value.update({"evidence_level": "abstract", "limitations": "Full text was unavailable."})
                return value, {"input_tokens": 100, "output_tokens": 200}

        backend = FlakyBackend()
        with tempfile.TemporaryDirectory() as temp:
            config["project"]["output_dir"] = temp
            with patch("paper_digest.orchestrator.make_backend", return_value=backend):
                result = run_pipeline(config, resolved)
            summary_path = next(Path(result["run_dir"]).glob("summaries/*.json"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(backend.calls, 2)
        self.assertEqual(summary["generation"]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
