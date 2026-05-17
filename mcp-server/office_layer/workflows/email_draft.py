"""Email draft — Phase 2.

Stages a reply email as a markdown file under ``<workspace>/drafts/``,
grounded in an :class:`EvidencePacket` the caller already produced from
``build_evidence_packet`` or ``build_client_history``. The LLM rewrite
happens in the parent agent (Claude); this tool only assembles a
skeleton + the citation block + the "before sending" checklist, and
resolves a safe write target.

Design notes:

* Packets are runtime-only in this codebase (see
  :mod:`engine.evidence`) — they are not persisted in storage. So the
  signature accepts the packet itself (dict or :class:`EvidencePacket`)
  rather than a ``packet_id`` handle the tool would otherwise be unable
  to dereference.
* The safety gate goes through :func:`safety.pretool.intercept` with
  operation ``"new_draft"`` (MEDIUM by default; read-only workspace
  escalates to HIGH and refuses; target outside the workspace root
  escalates to HIGH and refuses). The refusal shape is centralised
  there so the next write tool inherits it for free.
* The draft is ALWAYS staged under ``<workspace_root>/drafts/`` —
  callers cannot redirect into another subdir. ``send_email`` is HIGH-
  risk per spec §9.10.1; this workflow never crosses that line.
* Output filename: ``<YYYY-MM-DD>-<recipient_slug>-<topic>-<HHMMSS>.md``.
  The timestamp suffix is mandatory (same contract as
  :mod:`workflows.invoices_table`) so re-running never silently
  overwrites a prior draft.
* The body lists each evidence source as a one-liner with its locator
  (page / sheet / cell / slide). The full extracted text is intentionally
  NOT inlined — the packet stays canonical context and Claude is
  expected to quote from it. The draft body is meant to be edited.
* Low-confidence packet items become explicit checklist rows so the user
  is reminded to verify them before they hit send in their own mail
  client.
* Extracted fields with the keys ``total`` / ``due_date`` / ``issue_date``
  / ``invoice_number`` are surfaced into the checklist verbatim when
  present, because those are the things that, if wrong, embarrass the
  sender most.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..engine import get_engine
from ..models import EvidencePacket
from ..safety import intercept as _pretool_intercept

# -- constants ---------------------------------------------------------------

_DRAFTS_DIR_NAME = "drafts"
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")
_JP_CHAR_RE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")

# Extracted-field keys we surface into the "before sending" checklist.
# Anything wrong in these is exactly the embarrassing-mistake bucket.
_CHECKLIST_FIELD_KEYS: tuple[str, ...] = (
    "invoice_number",
    "total",
    "due_date",
    "issue_date",
)


# -- helpers -----------------------------------------------------------------


def _slugify(value: str, *, max_len: int = 40) -> str:
    """Filesystem-safe slug. Empty / all-symbol input → ``"x"`` so the
    filename never collapses to an empty segment."""
    lower = value.strip().lower()
    # Treat the email local-part as the recipient name; the domain rarely
    # adds disambiguation and only bloats the filename.
    if "@" in lower:
        lower = lower.split("@", 1)[0]
    slug = _SAFE_SLUG_RE.sub("-", lower).strip("-")
    return (slug[:max_len] or "x")


def _detect_language(packet: EvidencePacket, *, hint: str | None) -> str:
    """``hint='auto'`` → look at packet text/intent for JP characters,
    fall back to ``en``. Any explicit non-auto value is honoured as-is."""
    if hint and hint != "auto":
        return hint
    haystack = packet.intent or ""
    for src in packet.sources[:5]:
        haystack += "\n" + (src.extracted_text or "")
    return "ja" if _JP_CHAR_RE.search(haystack) else "en"


def _format_locator(src: Any) -> str:
    bits: list[str] = []
    if getattr(src, "page_number", None):
        bits.append(f"p.{src.page_number}")
    if getattr(src, "sheet_name", None):
        bits.append(f"sheet={src.sheet_name}")
    if getattr(src, "cell_range", None):
        bits.append(f"cells={src.cell_range}")
    if getattr(src, "slide_number", None):
        bits.append(f"slide={src.slide_number}")
    return " ".join(bits)


def _first_nonempty_line(text: str, *, max_len: int = 140) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        return line[:max_len] + ("…" if len(line) > max_len else "")
    return ""


# -- packet coercion ---------------------------------------------------------


def _coerce_packet(packet: Any) -> EvidencePacket | None:
    """Accept either an :class:`EvidencePacket` or its ``model_dump``
    dict (the MCP boundary always carries dicts). ``None`` for anything
    we cannot make sense of so the caller surfaces a clean error."""
    if packet is None:
        return None
    if isinstance(packet, EvidencePacket):
        return packet
    if isinstance(packet, dict):
        try:
            return EvidencePacket.model_validate(packet)
        except Exception:
            return None
    return None


# -- body assembly -----------------------------------------------------------


@dataclass(frozen=True)
class DraftEmail:
    draft_id: str
    body_markdown: str
    language: str
    citation_count: int
    checklist_item_count: int


def build_email_draft_body(
    packet: EvidencePacket,
    *,
    recipient: str,
    subject: str,
    intent: str,
    extra_context: str | None = None,
    language: str = "auto",
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> DraftEmail:
    """Pure function: assemble the markdown body for a draft email.

    No I/O — safe to call from tests without touching storage or disk.
    The caller is responsible for picking a save path and writing the
    result; see :func:`draft_email_from_evidence` for the full flow.
    """
    lang = _detect_language(packet, hint=language)
    now = now or datetime.now(timezone.utc)
    draft_id = f"ed_{uuid.uuid4().hex[:12]}"

    # Front-matter — machine-readable handle on what this draft is. We
    # write a YAML-shaped header so editors that highlight markdown
    # front-matter render it cleanly.
    fm = [
        "---",
        f"draft_id: {draft_id}",
        f"created_at: {now.isoformat()}",
        f"recipient: {recipient}",
        f"subject: {subject}",
        f"intent: {intent}",
    ]
    if workspace_id:
        fm.append(f"workspace_id: {workspace_id}")
    if packet.packet_id:
        fm.append(f"packet_id: {packet.packet_id}")
    fm.append("status: draft (never sent)")
    fm.append("---")
    fm.append("")

    # Heading + recipient line.
    body: list[str] = list(fm)
    body.append(f"# {subject}")
    body.append("")
    body.append(f"To: {recipient}")
    body.append("")

    # Opening + intent. Kept skeletal — Claude is expected to rewrite
    # this into natural prose using the citations below.
    if lang == "ja":
        body.append(f"## 用件")
        body.append("")
        body.append(f"{intent}")
        body.append("")
        body.append(
            "(本文は下記のエビデンスを引用しながら Claude が清書する想定です。)"
        )
    else:
        body.append("## Purpose")
        body.append("")
        body.append(intent)
        body.append("")
        body.append(
            "(Claude is expected to rewrite this into natural prose, "
            "citing the evidence below.)"
        )
    body.append("")

    # Citations — one per evidence source, locator-aware, with a short
    # preview so the reader recognises the doc without opening it.
    if packet.sources:
        body.append("## Context (from evidence)")
        body.append("")
        for src in packet.sources:
            loc = _format_locator(src)
            loc_part = f" ({loc})" if loc else ""
            preview = _first_nonempty_line(src.extracted_text)
            preview_part = f" — {preview}" if preview else ""
            body.append(f"- `{src.file_path}`{loc_part}{preview_part}")
        body.append("")
    else:
        # No sources is unusual but valid — surface it so Claude knows
        # to ask the user for more context rather than fabricate it.
        if lang == "ja":
            body.append("## Context")
            body.append("")
            body.append(
                "(エビデンスが添付されていません。Claude は推測で文面を埋めず、"
                "ユーザーに追加情報を求めてください。)"
            )
        else:
            body.append("## Context")
            body.append("")
            body.append(
                "(No evidence was attached. Claude should ask the user for "
                "more context instead of guessing.)"
            )
        body.append("")

    # Optional extra context block from the caller.
    if extra_context:
        body.append("## Additional context")
        body.append("")
        body.append(extra_context.strip())
        body.append("")

    # Before-sending checklist. Items that are wrong here are the
    # embarrassing-mistake bucket: amounts, dates, invoice numbers,
    # plus anything the extractor already flagged as low confidence.
    flagged_fields: list[tuple[str, str, str]] = []  # (field_key, value, file_path)
    for src in packet.sources:
        for f in src.extracted_fields:
            if f.key in _CHECKLIST_FIELD_KEYS and f.value:
                flagged_fields.append((f.key, f.value, src.file_path))

    body.append("## Before sending — please confirm")
    body.append("")
    if lang == "ja":
        body.append("- [ ] 宛先と件名が正しい")
        body.append("- [ ] 言い回し・敬語が受信者に合っている")
        body.append("- [ ] 添付ファイル / 引用元のリンクが正しい")
    else:
        body.append("- [ ] Recipient and subject are correct")
        body.append("- [ ] Tone and register match the recipient")
        body.append("- [ ] Attachments / referenced files are correct")
    seen: set[tuple[str, str]] = set()
    for key, value, path in flagged_fields:
        item_key = (key, value)
        if item_key in seen:
            continue
        seen.add(item_key)
        if lang == "ja":
            body.append(f"- [ ] {key} = `{value}` (出所: `{path}`) を確認")
        else:
            body.append(f"- [ ] Verify {key} = `{value}` (source: `{path}`)")
    for item in packet.low_confidence_items:
        if lang == "ja":
            body.append(f"- [ ] 低信頼項目を再確認: {item}")
        else:
            body.append(f"- [ ] Re-check low-confidence item: {item}")
    body.append("")

    body.append("---")
    if lang == "ja":
        body.append(
            "本ドラフトは office-layer によって staging されました。 まだ送信されていません。"
        )
    else:
        body.append(
            "This draft was staged by office-layer. It has NOT been sent."
        )
    body.append("")

    body_str = "\n".join(body)
    citation_count = len(packet.sources)
    # Header (3) + per-field + per-low-confidence.
    checklist_count = 3 + len(seen) + len(packet.low_confidence_items)

    return DraftEmail(
        draft_id=draft_id,
        body_markdown=body_str,
        language=lang,
        citation_count=citation_count,
        checklist_item_count=checklist_count,
    )


# -- path resolution ---------------------------------------------------------


def _draft_filename(
    *,
    recipient: str,
    subject: str,
    when: datetime,
) -> str:
    date = when.strftime("%Y-%m-%d")
    time = when.strftime("%H%M%S")
    topic = _slugify(subject)
    who = _slugify(recipient)
    return f"{date}-{who}-{topic}-{time}.md"


def _resolve_drafts_path(
    workspace_root: Path, *, recipient: str, subject: str, when: datetime
) -> Path:
    """Always under ``<workspace_root>/drafts/``. Callers cannot escape
    this dir — the workflow doesn't accept an output_path argument by
    design, the contract is "drafts live under drafts/"."""
    return workspace_root / _DRAFTS_DIR_NAME / _draft_filename(
        recipient=recipient, subject=subject, when=when
    )


# -- workflow entry point ----------------------------------------------------


def draft_email_from_evidence(
    workspace_id: str,
    *,
    packet: Any,
    recipient: str,
    subject: str | None = None,
    intent: str | None = None,
    extra_context: str | None = None,
    language: str = "auto",
) -> dict[str, Any]:
    """Stage an evidence-grounded email draft under ``drafts/``.

    Parameters
    ----------
    workspace_id : str
        Workspace under which the draft will be written. The drafts
        directory is always ``<workspace_root>/drafts/``.
    packet : EvidencePacket | dict
        The packet returned by ``build_evidence_packet`` /
        ``build_client_history``. Either the pydantic model or its
        ``model_dump`` dict shape (the MCP boundary carries dicts).
    recipient : str
        Free-form recipient identifier (email or name). Used in the
        body header AND in the filename slug; emails are reduced to
        their local part for the filename.
    subject : str, optional
        Email subject. Falls back to ``packet.intent`` when omitted.
    intent : str, optional
        Short statement of what the email is for. Falls back to
        ``packet.intent``.
    extra_context : str, optional
        Free-form text appended verbatim under "Additional context" —
        for caller-supplied details the packet alone would not carry
        (e.g. "the client asked for a timeline by Friday").
    language : {"auto", "ja", "en"}, default "auto"
        Body language. ``"auto"`` infers from the packet content (any
        JP characters → ``"ja"``).
    """
    engine = get_engine()
    ws = engine.workspaces.get(workspace_id)
    if ws is None:
        return {"error": f"workspace '{workspace_id}' not found"}

    coerced = _coerce_packet(packet)
    if coerced is None:
        return {
            "error": (
                "packet must be an EvidencePacket or its model_dump dict — "
                "got "
                f"{type(packet).__name__}"
            )
        }

    if not recipient or not recipient.strip():
        return {"error": "recipient must be a non-empty string"}

    effective_subject = (subject or coerced.intent or "Follow-up").strip()
    effective_intent = (intent or coerced.intent or "Follow up on the evidence below.").strip()

    workspace_root = Path(ws.root_path).resolve()
    when = datetime.now(timezone.utc)
    target = _resolve_drafts_path(
        workspace_root, recipient=recipient, subject=effective_subject, when=when
    )

    # Safety gate — same pattern as workflows.invoices_table. "new_draft"
    # is MEDIUM by default; read-only workspace + outside-workspace
    # target both escalate to HIGH and are refused. The refuse contract
    # lives in ``safety.pretool``.
    gate = _pretool_intercept(
        "new_draft", targets=[str(target)], workspace=ws
    )
    if gate.refusal is not None:
        return gate.refusal
    risk = gate.risk

    draft = build_email_draft_body(
        coerced,
        recipient=recipient,
        subject=effective_subject,
        intent=effective_intent,
        extra_context=extra_context,
        language=language,
        workspace_id=workspace_id,
        now=when,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(draft.body_markdown, encoding="utf-8")

    return {
        "workspace_id": workspace_id,
        "draft_id": draft.draft_id,
        "output_path": str(target),
        "recipient": recipient,
        "subject": effective_subject,
        "intent": effective_intent,
        "language": draft.language,
        "citation_count": draft.citation_count,
        "checklist_item_count": draft.checklist_item_count,
        "packet_id": coerced.packet_id,
        "packet_source_count": len(coerced.sources),
        "body_markdown": draft.body_markdown,
        "risk": risk.model_dump(mode="json"),
    }


__all__ = [
    "draft_email_from_evidence",
    "build_email_draft_body",
    "DraftEmail",
]
