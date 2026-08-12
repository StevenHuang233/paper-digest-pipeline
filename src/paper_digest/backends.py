from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "background": {"type": "string"},
        "motivation": {"type": "string"},
        "idea": {"type": "string"},
        "method": {"type": "string"},
        "experiments": {"type": "string"},
        "conclusion": {"type": "string"},
        "evidence_level": {"type": "string", "enum": ["fulltext", "abstract", "metadata"]},
        "limitations": {"type": "string"},
    },
    "required": ["background", "motivation", "idea", "method", "experiments", "conclusion", "evidence_level", "limitations"],
}


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


class OpenAICompatibleBackend:
    def __init__(self, config: dict):
        self.config = config
        key_name = config["api_key_env"]
        self.api_key = os.getenv(key_name, "")
        if not self.api_key:
            raise RuntimeError(f"Missing API key environment variable: {key_name}")

    def generate_json(
        self, system: str, prompt: str, *, max_output_tokens: int | None = None,
        schema: dict[str, Any] | None = None, thinking_mode: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        url = self.config["base_url"].rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = {
            "model": self.config["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": float(self.config["temperature"]),
            "max_tokens": int(max_output_tokens or self.config["max_output_tokens"]),
        }
        if bool(self.config.get("json_mode", True)):
            payload["response_format"] = {"type": "json_object"}
        effective_thinking = thinking_mode if thinking_mode is not None else str(self.config.get("thinking_mode") or "")
        if bool(self.config.get("supports_thinking_toggle", False)) and effective_thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": effective_thinking}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json",
        })
        attempts = int(self.config.get("request_attempts", 3))
        backoff = float(self.config.get("request_backoff_seconds", 2.0))
        retryable_statuses = {408, 429, 500, 502, 503, 504}
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=int(self.config["timeout_seconds"])) as response:
                    result = json.loads(response.read().decode("utf-8"))
                content = result["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise ValueError("Model API returned empty JSON content")
                value = _extract_json(content)
                usage = result.get("usage") or {}
                return value, {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                }
            except urllib.error.HTTPError as exc:
                detail = exc.read(2000).decode("utf-8", errors="replace")
                if exc.code not in retryable_statuses or attempt >= attempts:
                    raise RuntimeError(f"Model API HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"Model API request failed after {attempts} attempts: {type(exc).__name__}: {exc}"
                    ) from exc
            time.sleep(max(0.0, backoff) * (2 ** (attempt - 1)))


class CodexBackend:
    def __init__(self, config: dict):
        self.config = config
        self.executable = shutil.which("codex")
        if os.name == "nt" and self.executable and Path(self.executable).suffix.lower() == ".ps1":
            cmd_shim = Path(self.executable).with_suffix(".cmd")
            if cmd_shim.exists():
                self.executable = str(cmd_shim)
        if not self.executable:
            raise RuntimeError("codex executable was not found on PATH")

    def generate_json(
        self, system: str, prompt: str, *, max_output_tokens: int | None = None,
        schema: dict[str, Any] | None = None, thinking_mode: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        combined = f"{system}\n\n{prompt}\n\nReturn only the JSON object matching the supplied schema."
        with tempfile.TemporaryDirectory(prefix="paper-digest-codex-") as temp:
            temp_dir = Path(temp)
            schema_path = temp_dir / "schema.json"
            result_path = temp_dir / "result.json"
            command = [self.executable, "exec", "--sandbox", "read-only", "--ephemeral"]
            if schema:
                schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                command.extend(["--output-schema", str(schema_path)])
            command.extend(["--output-last-message", str(result_path), "-"])
            codex_model = str(self.config.get("codex_model") or "").strip()
            if codex_model:
                command[2:2] = ["--model", codex_model]
            completed = subprocess.run(
                command, input=combined, text=True, encoding="utf-8", errors="replace",
                capture_output=True, timeout=int(self.config["timeout_seconds"]), check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"codex exec failed ({completed.returncode}): {completed.stderr[-3000:]}")
            return _extract_json(result_path.read_text(encoding="utf-8")), {"input_tokens": 0, "output_tokens": 0}


def make_backend(config: dict):
    if config["type"] == "codex":
        return CodexBackend(config)
    return OpenAICompatibleBackend(config)
