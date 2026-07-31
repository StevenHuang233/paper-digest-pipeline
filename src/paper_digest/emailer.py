from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def read_result(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    result_path = Path(path)
    if not result_path.exists() or result_path.stat().st_size == 0:
        return {}
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _env_required(name: str, field: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing email environment variable {name} (configured by email.{field})")
    return value


def _recipients(raw: str) -> list[str]:
    values = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    if not values:
        raise RuntimeError("No email recipients were configured")
    return values


def _summary_body(result: dict[str, Any], status: str, run_url: str) -> str:
    decisions = result.get("selection_decisions") or {}
    lines = [
        "Paper Digest 每日任务已结束。",
        "",
        f"运行状态：{status}",
        f"候选论文：{result.get('candidate_count', '未知')}",
        f"筛选收录：{result.get('selected_count', '未知')}",
        f"LLM include：{decisions.get('include', '未知')}",
        f"LLM exclude：{decisions.get('exclude', '未知')}",
        f"硬排除：{decisions.get('hard_exclude', '未知')}",
        f"计划总结：{result.get('review_target_count', '未知')}",
        f"完成总结：{result.get('completed_count', '未知')}",
        f"失败数量：{result.get('failed_count', '未知')}",
    ]
    if result.get("candidate_limit_reached"):
        lines.append("提示：候选数量触及上限，可能还有论文未进入筛选。")
    if result.get("selected_limit_reached"):
        lines.append("提示：入选数量触及上限，部分 include 论文未进入保留列表。")
    if run_url:
        lines.extend(["", f"GitHub Actions 运行详情：{run_url}"])
    lines.extend(["", "详细总结和运行日志见附件或 GitHub Actions Artifacts。"])
    return "\n".join(lines)


def build_message(
    config: dict[str, Any], result: dict[str, Any], *, status: str,
    run_url: str = "", log_path: str | Path | None = None,
) -> tuple[EmailMessage, str, str, list[str]]:
    settings = config["email"]
    username = _env_required(str(settings["username_env"]), "username_env")
    password = _env_required(str(settings["password_env"]), "password_env")
    recipients = _recipients(_env_required(str(settings["to_env"]), "to_env"))
    from_env = str(settings.get("from_env") or "").strip()
    sender = os.getenv(from_env, "").strip() if from_env else ""
    sender = sender or username

    project = str(config["project"].get("name") or "paper-digest")
    date_label = str(result.get("job_label") or config["discovery"].get("date") or "")
    prefix = str(settings.get("subject_prefix") or "[Paper Digest]").strip()
    message = EmailMessage()
    message["Subject"] = f"{prefix} {status.upper()} · {project} · {date_label}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(_summary_body(result, status, run_url))

    max_bytes = int(float(settings["max_attachment_mb"]) * 1024 * 1024)
    attachments: list[Path] = []
    outputs = result.get("outputs") or {}
    if bool(settings.get("attach_pdf")) and outputs.get("pdf"):
        attachments.append(Path(outputs["pdf"]))
    if bool(settings.get("attach_markdown")) and outputs.get("markdown"):
        attachments.append(Path(outputs["markdown"]))
    if status != "success" and bool(settings.get("attach_log_on_failure")) and log_path:
        attachments.append(Path(log_path))

    for path in attachments:
        if not path.is_file() or path.stat().st_size > max_bytes:
            continue
        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return message, username, password, recipients


def send_digest_email(
    config: dict[str, Any], result: dict[str, Any], *, status: str,
    run_url: str = "", log_path: str | Path | None = None,
) -> dict[str, Any]:
    settings = config["email"]
    if not bool(settings["enabled"]):
        return {"sent": False, "reason": "email.disabled"}
    message, username, password, recipients = build_message(
        config, result, status=status, run_url=run_url, log_path=log_path,
    )
    host = str(settings["smtp_host"])
    port = int(settings["smtp_port"])
    timeout = int(settings["timeout_seconds"])
    context = ssl.create_default_context()
    if settings["security"] == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as client:
            client.login(username, password)
            client.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            client.login(username, password)
            client.send_message(message)
    return {"sent": True, "recipient_count": len(recipients), "subject": str(message["Subject"])}
