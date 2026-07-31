from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_digest.budget import Budget
from paper_digest.config import load_config
from paper_digest.filtering import rule_rank
from paper_digest.models import Paper
from paper_digest.outputs import render_markdown
from paper_digest.orchestrator import run_pipeline
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
