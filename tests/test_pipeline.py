from __future__ import annotations

import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

from paper_digest.backends import OpenAICompatibleBackend
from paper_digest.budget import Budget
from paper_digest.config import load_config
from paper_digest.filtering import llm_rerank, rule_rank
from paper_digest.fulltext import CUTOFF
from paper_digest.models import Paper
from paper_digest.outputs import render_markdown
from paper_digest.orchestrator import rank_and_select, run_pipeline
from paper_digest.sources.arxiv import build_query, parse_feed, resolve_date
from paper_digest.sources.crossref import parse_items, venue_similarity
from paper_digest.sources.openreview import parse_notes


class ArxivTests(unittest.TestCase):
    def test_date_and_query(self):
        date = resolve_date("2026-07-28")
        self.assertEqual(str(date), "2026-07-28")
        query = build_query(date, ["cs.LG", "cs.AI"])
        self.assertIn("submittedDate:[202607280000 TO 202607282359]", query)
        self.assertIn("cat:cs.LG OR cat:cs.AI", query)

    def test_parse_feed(self):
        fixture = Path(__file__).parent / "fixtures" / "arxiv.xml"
        papers, total = parse_feed(fixture.read_bytes())
        self.assertEqual(total, 1)
        self.assertEqual(papers[0].id, "2607.12345v1")
        self.assertEqual(papers[0].categories, ["cs.LG"])
        self.assertTrue(papers[0].pdf_url.endswith("2607.12345v1"))


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
                    "reason": "Relevant graph-agent paper.",
                } for item in batch]}, {}

        backend = BulkRanker()
        config = {
            "preferences": {"interests": ["graph agent"], "include_keywords": [], "exclude_keywords": [], "categories": []},
            "selection": {
                "ranker": "llm", "min_score": 0.0, "max_selected_papers": 500,
                "llm_batch_size": 40, "llm_abstract_chars": 1600,
                "llm_max_output_tokens": 4000, "llm_thinking_mode": "disabled",
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
                    "id": "p000000", "decision": "exclude", "reason": "Not relevant.",
                }]}, {}

        with self.assertRaisesRegex(RuntimeError, "omitted 1 of 2"):
            llm_rerank(papers, {"interests": ["multimodal"]}, IncompleteRanker())


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


if __name__ == "__main__":
    unittest.main()
