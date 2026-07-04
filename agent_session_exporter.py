#!/usr/bin/env python3
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol

import markdown as markdown_lib


SUMMARY_CHARS = 160
DEFAULT_DIFF_HIGHLIGHT_LINES = 2000
DEFAULT_JOBS = max(1, min(4, os.cpu_count() or 1))
DEFAULT_PAGE_MESSAGE_COUNT = 120
DEFAULT_HIDDEN_MESSAGE_TYPES = {
    "synthetic",
    "model-switched",
    "agent-switched",
}


def default_codex_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export agent session chat history from a supported data source."
    )
    parser.add_argument(
        "--source",
        default="opencode",
        help="Source provider to read from (default: opencode)",
    )
    parser.add_argument("--db", help="Path to opencode.db; required for --source opencode")
    parser.add_argument(
        "--sessions-dir",
        default=str(default_codex_sessions_dir()),
        help="Path to Codex sessions directory; used by --source codex",
    )
    parser.add_argument("--session-id", help="Export or summarize one session id")
    parser.add_argument(
        "--output",
        default="opencode-session-export",
        help="Output directory for HTML, or JSON file/directory for --summary-only",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print/write JSON summary only; do not export HTML",
    )
    parser.add_argument(
        "--summary-chars",
        type=int,
        default=SUMMARY_CHARS,
        help=f"Maximum characters per summary snippet (default: {SUMMARY_CHARS})",
    )
    parser.add_argument(
        "--include-synthetic",
        action="store_true",
        help="Include synthetic/model-switched/agent-switched event messages in HTML",
    )
    parser.add_argument(
        "--jobs",
        type=positive_int,
        default=DEFAULT_JOBS,
        help=f"Number of sessions to render concurrently (default: {DEFAULT_JOBS})",
    )
    parser.add_argument(
        "--diff-highlight-lines",
        type=non_negative_int,
        default=DEFAULT_DIFF_HIGHLIGHT_LINES,
        help=(
            "Maximum diff lines to render with per-line highlighting; larger diffs "
            f"use a lighter plain text block (default: {DEFAULT_DIFF_HIGHLIGHT_LINES}, 0 disables highlighting)"
        ),
    )
    parser.add_argument(
        "--page-message-count",
        type=non_negative_int,
        default=DEFAULT_PAGE_MESSAGE_COUNT,
        help=(
            "Maximum visible messages per HTML page before splitting a session "
            f"(default: {DEFAULT_PAGE_MESSAGE_COUNT}, 0 disables pagination)"
        ),
    )
    parser.add_argument(
        "--single-file",
        action="store_true",
        help="Force one HTML file per session; equivalent to --page-message-count 0",
    )
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return parsed


@contextlib.contextmanager
def copied_database(db_path: Path):
    db_path = db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    with tempfile.TemporaryDirectory(prefix="opencode-db-copy-") as tmp:
        tmp_dir = Path(tmp)
        copied = tmp_dir / db_path.name
        shutil.copy2(db_path, copied)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(copied) + suffix))
        yield copied


def open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


class SourceProvider(Protocol):
    name: str

    def source_path(self, args: argparse.Namespace) -> Path:
        ...

    def copied_source(self, source_path: Path):
        ...

    def load_sessions(self, db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
        ...

    def load_session(self, db_path: Path, session_id: str) -> dict[str, Any] | None:
        ...

    def load_session_headers(
        self, db_path: Path, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        ...

    def diff_base_dir(self, db_path: Path) -> Path:
        ...


class OpencodeProvider:
    name = "opencode"

    def source_path(self, args: argparse.Namespace) -> Path:
        if not args.db:
            raise ValueError("--db is required for --source opencode")
        return Path(args.db)

    def copied_source(self, source_path: Path):
        return copied_database(source_path)

    def load_sessions(self, db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
        return load_sessions(db_path, session_id)

    def load_session(self, db_path: Path, session_id: str) -> dict[str, Any] | None:
        return load_session(db_path, session_id)

    def load_session_headers(
        self, db_path: Path, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return load_session_headers(db_path, session_id)

    def diff_base_dir(self, db_path: Path) -> Path:
        return db_path.parent


class CodexProvider:
    name = "codex"

    def source_path(self, args: argparse.Namespace) -> Path:
        return Path(args.sessions_dir)

    @contextlib.contextmanager
    def copied_source(self, source_path: Path):
        source_path = source_path.resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Codex sessions directory not found: {source_path}")
        if not source_path.is_dir():
            raise NotADirectoryError(f"Codex sessions path is not a directory: {source_path}")
        with tempfile.TemporaryDirectory(prefix="codex-sessions-copy-") as tmp:
            copied = Path(tmp) / "sessions"
            shutil.copytree(source_path, copied)
            yield copied

    def load_sessions(self, db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
        return [self.load_session_from_file(path) for path in self.session_files(db_path, session_id)]

    def load_session(self, db_path: Path, session_id: str) -> dict[str, Any] | None:
        files = self.session_files(db_path, session_id)
        if not files:
            return None
        return self.load_session_from_file(files[0])

    def load_session_headers(
        self, db_path: Path, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [self.load_session_header(path) for path in self.session_files(db_path, session_id)]

    def diff_base_dir(self, db_path: Path) -> Path:
        return db_path

    def session_files(self, sessions_dir: Path, session_id: str | None = None) -> list[Path]:
        files = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not session_id:
            return files
        return [
            path
            for path in files
            if session_id in path.stem or self.load_session_header(path).get("id") == session_id
        ]

    def load_session_header(self, path: Path) -> dict[str, Any]:
        meta = self.read_session_meta(path)
        session_id = str(meta.get("session_id") or meta.get("id") or path.stem)
        timestamp = meta.get("timestamp")
        title = str(meta.get("title") or path.stem)
        if title.startswith("rollout-"):
            title = f"Codex session {session_id}"
        return {
            "id": session_id,
            "slug": path.stem,
            "title": title,
            "directory": str(meta.get("cwd") or ""),
            "time_created": timestamp or "",
            "time_updated": timestamp or "",
            "path": str(path),
        }

    def read_session_meta(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    record = json.loads(line)
                    if record.get("type") == "session_meta":
                        payload = record.get("payload")
                        return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
        return {}

    def load_session_from_file(self, path: Path) -> dict[str, Any]:
        session = self.load_session_header(path)
        messages: list[dict[str, Any]] = []
        pending_calls: dict[str, dict[str, Any]] = {}
        seq = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            timestamp = record.get("timestamp") or ""
            payload_type = payload.get("type")
            if payload_type == "message":
                role = payload.get("role")
                text = codex_message_text(payload.get("content") or [])
                if not text.strip() or role not in {"user", "assistant"}:
                    continue
                if role == "user" and codex_is_runtime_context(text):
                    continue
                seq += 1
                messages.append(codex_text_message(role, text, timestamp, seq))
            elif payload_type == "reasoning":
                text = codex_reasoning_text(payload)
                if text.strip():
                    seq += 1
                    messages.append(codex_reasoning_message(text, timestamp, seq))
            elif payload_type == "function_call":
                call_id = str(payload.get("call_id") or payload.get("id") or f"call_{seq + 1}")
                pending_calls[call_id] = payload
            elif payload_type == "function_call_output":
                call_id = str(payload.get("call_id") or "")
                call = pending_calls.pop(call_id, {"name": "tool", "call_id": call_id, "arguments": ""})
                seq += 1
                messages.append(codex_tool_message(call, payload, timestamp, seq))
        session["messages"] = messages
        return session


SOURCE_PROVIDERS: dict[str, SourceProvider] = {
    OpencodeProvider.name: OpencodeProvider(),
    CodexProvider.name: CodexProvider(),
}


def get_source_provider(name: str) -> SourceProvider:
    try:
        return SOURCE_PROVIDERS[name]
    except KeyError as exc:
        available = ", ".join(sorted(SOURCE_PROVIDERS))
        raise ValueError(f"Unknown source provider: {name}. Available: {available}") from exc


def codex_message_text(content: list[Any]) -> str:
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"input_text", "output_text", "text"}:
            parts.append(str(item.get("text") or ""))
        elif item_type == "input_image":
            parts.append("[Image attachment]")
    return "\n".join(part for part in parts if part)


def codex_is_runtime_context(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(("<environment_context>", "<skill>", "<developer_instructions>"))


def codex_reasoning_text(payload: dict[str, Any]) -> str:
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts = []
        for item in summary:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("summary") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if summary:
        return str(summary)
    return str(payload.get("text") or "")


def codex_text_message(role: str, text: str, timestamp: Any, seq: int) -> dict[str, Any]:
    if role == "user":
        return {
            "type": "user",
            "time_created": timestamp,
            "seq": seq,
            "json": {"text": text},
        }
    return {
        "type": "assistant",
        "time_created": timestamp,
        "seq": seq,
        "json": {"content": [{"type": "text", "text": text}]},
    }


def codex_reasoning_message(text: str, timestamp: Any, seq: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "time_created": timestamp,
        "seq": seq,
        "json": {"content": [{"type": "reasoning", "text": text}]},
    }


def codex_tool_message(
    call: dict[str, Any], output: dict[str, Any], timestamp: Any, seq: int
) -> dict[str, Any]:
    arguments = parse_json_maybe(call.get("arguments") or "")
    tool_output = parse_json_maybe(output.get("output") or "")
    return {
        "type": "assistant",
        "time_created": timestamp,
        "seq": seq,
        "json": {
            "content": [
                {
                    "type": "tool",
                    "id": call.get("call_id") or call.get("id") or f"call_{seq}",
                    "name": call.get("name") or "tool",
                    "state": {
                        "status": "completed",
                        "input": arguments,
                        "output": tool_output,
                    },
                }
            ]
        },
    }


def parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def load_sessions(db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    with contextlib.closing(open_readonly(db_path)) as conn:
        return [load_session_messages(conn, row) for row in query_session_rows(conn, session_id)]


def load_session(db_path: Path, session_id: str) -> dict[str, Any] | None:
    with contextlib.closing(open_readonly(db_path)) as conn:
        rows = query_session_rows(conn, session_id)
        if not rows:
            return None
        return load_session_messages(conn, rows[0])


def load_session_headers(db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    with contextlib.closing(open_readonly(db_path)) as conn:
        return [dict(row) for row in query_session_rows(conn, session_id)]


def query_session_rows(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ""
    if session_id:
        where = "WHERE id = ?"
        params.append(session_id)
    return conn.execute(
        f"SELECT * FROM session {where} ORDER BY time_updated DESC", params
    ).fetchall()


def load_session_messages(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    session = dict(row)
    messages = conn.execute(
        """
        SELECT * FROM session_message
        WHERE session_id = ?
        ORDER BY seq ASC, time_created ASC
        """,
        (session["id"],),
    ).fetchall()
    if messages:
        session["messages"] = [decode_message(dict(msg)) for msg in messages]
    else:
        session["messages"] = load_message_part_messages(conn, session["id"])
    return session


def decode_message(message: dict[str, Any]) -> dict[str, Any]:
    try:
        message["json"] = json.loads(message.get("data") or "{}")
    except json.JSONDecodeError:
        message["json"] = {"text": message.get("data", "")}
    return message


def load_message_part_messages(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "message") or not table_exists(conn, "part"):
        return []
    message_rows = conn.execute(
        """
        SELECT *
        FROM message
        WHERE session_id = ?
        ORDER BY time_created ASC, id ASC
        """,
        (session_id,),
    ).fetchall()
    if not message_rows:
        return []
    part_rows = conn.execute(
        """
        SELECT *
        FROM part
        WHERE session_id = ?
        ORDER BY time_created ASC, id ASC
        """,
        (session_id,),
    ).fetchall()
    parts_by_message: dict[str, list[dict[str, Any]]] = {}
    for part_row in part_rows:
        part = decode_part(dict(part_row))
        parts_by_message.setdefault(str(part["message_id"]), []).append(part)

    converted = []
    for seq, message_row in enumerate(message_rows, start=1):
        message = decode_message_part_message(
            dict(message_row),
            parts_by_message.get(str(message_row["id"]), []),
            seq,
        )
        if message is not None:
            converted.append(message)
    return converted


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def decode_part(part: dict[str, Any]) -> dict[str, Any]:
    try:
        part["json"] = json.loads(part.get("data") or "{}")
    except json.JSONDecodeError:
        part["json"] = {"type": "text", "text": part.get("data", "")}
    return part


def decode_message_part_message(
    message: dict[str, Any],
    parts: list[dict[str, Any]],
    seq: int,
) -> dict[str, Any] | None:
    try:
        data = json.loads(message.get("data") or "{}")
    except json.JSONDecodeError:
        data = {}
    role = data.get("role")
    if role == "user":
        return {
            "id": message.get("id"),
            "session_id": message.get("session_id"),
            "type": "user",
            "time_created": message.get("time_created"),
            "time_updated": message.get("time_updated"),
            "seq": seq,
            "json": {"text": "\n\n".join(user_part_texts(parts))},
        }
    if role == "assistant":
        return {
            "id": message.get("id"),
            "session_id": message.get("session_id"),
            "type": "assistant",
            "time_created": message.get("time_created"),
            "time_updated": message.get("time_updated"),
            "seq": seq,
            "json": {"content": assistant_part_content(parts)},
        }
    return None


def user_part_texts(parts: list[dict[str, Any]]) -> list[str]:
    texts = []
    for part in parts:
        data = part.get("json") or {}
        if data.get("synthetic"):
            continue
        if data.get("type") == "text" and data.get("text"):
            texts.append(str(data["text"]))
    return texts


def assistant_part_content(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content = []
    for part in parts:
        data = part.get("json") or {}
        part_type = data.get("type")
        if data.get("synthetic"):
            continue
        if part_type == "text":
            text = data.get("text")
            if text:
                content.append({"type": "text", "text": str(text)})
        elif part_type == "reasoning":
            text = data.get("text")
            if text:
                content.append({"type": "reasoning", "text": str(text)})
        elif part_type == "tool":
            content.append(
                {
                    "type": "tool",
                    "id": data.get("callID") or data.get("id") or part.get("id"),
                    "name": data.get("tool") or data.get("name") or "tool",
                    "state": data.get("state") or {},
                }
            )
    return content


def build_summary(
    db_path: Path,
    session_id: str | None = None,
    summary_chars: int = SUMMARY_CHARS,
    provider: SourceProvider | None = None,
) -> list[dict[str, str]] | dict[str, list[dict[str, str]]]:
    source = provider or get_source_provider("opencode")
    sessions = source.load_sessions(db_path, session_id)
    summarized = {session["id"]: summarize_session(session, summary_chars) for session in sessions}
    if session_id:
        return summarized.get(session_id, [])
    return summarized


def summarize_session(session: dict[str, Any], summary_chars: int) -> list[dict[str, str]]:
    pairs = []
    pending_human: str | None = None
    for message in session["messages"]:
        msg_type = message["type"]
        if msg_type == "user":
            if pending_human is not None:
                pairs.append({"human": pending_human, "ai": ""})
            pending_human = summarize_text(extract_user_text(message), summary_chars)
        elif msg_type == "assistant" and pending_human is not None:
            assistant = summarize_text(extract_assistant_text(message), summary_chars)
            if not assistant:
                continue
            pairs.append({"human": pending_human, "ai": assistant})
            pending_human = None
    if pending_human is not None:
        pairs.append({"human": pending_human, "ai": ""})
    return pairs


def summarize_text(text: str, limit: int) -> str:
    text = normalize_ws(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_user_text(message: dict[str, Any]) -> str:
    data = message.get("json") or {}
    text = data.get("text") or ""
    files = data.get("files") or []
    if files:
        names = [f.get("name") or f.get("uri") for f in files if isinstance(f, dict)]
        if names:
            text = f"{text}\nAttached files: {', '.join(names)}"
    return text


def extract_assistant_text(message: dict[str, Any]) -> str:
    data = message.get("json") or {}
    parts = []
    for item in data.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    if not parts and data.get("error"):
        parts.append(json.dumps(data["error"], ensure_ascii=False))
    return "\n".join(parts)


def render_session_html(
    session: dict[str, Any],
    base_dir: Path | None = None,
    include_synthetic: bool = False,
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
    include_session_diffs: bool = True,
    page_nav: str = "",
    page_nav_bottom: str | None = None,
    title_suffix: str = "",
    initial_turn: int = 0,
    initial_ai_turn: int = 0,
) -> str:
    title = str(session.get("title") or session.get("id") or "opencode session") + title_suffix
    messages = visible_messages(session.get("messages") or [], include_synthetic)
    outline = []
    body = []
    turn = initial_turn
    ai_turn = initial_ai_turn
    for message in messages:
        if message["type"] == "user":
            turn += 1
            anchor = f"turn-{turn}"
            label = summarize_text(extract_user_text(message), 48) or f"Turn {turn}"
            outline.append((anchor, f"Human {turn}: {label}"))
            body.append(render_user_message(message, anchor, turn))
        elif message["type"] == "assistant":
            ai_turn += 1
            anchor = f"ai-{ai_turn}"
            label = summarize_text(extract_assistant_text(message), 48) or "AI response"
            outline.append((anchor, f"AI {ai_turn}: {label}"))
            body.append(render_assistant_message(message, turn, anchor, diff_highlight_lines))
        elif message["type"] == "compaction":
            body.append(render_compaction_message(message))
        else:
            body.append(render_event_message(message))

    session_diffs = load_session_diffs(session.get("id"), base_dir) if include_session_diffs else []
    if include_session_diffs and session_diffs:
        body.append(render_session_diffs(session_diffs, diff_highlight_lines))

    return HTML_TEMPLATE.format(
        title=escape(title),
        session_id=escape(str(session.get("id") or "")),
        subtitle=escape(session_subtitle(session)),
        page_nav=page_nav,
        page_nav_bottom="" if page_nav_bottom is None else page_nav_bottom,
        outline="\n".join(
            f'<a href="#{escape(anchor)}" data-target="{escape(anchor)}">{escape(label)}</a>'
            for anchor, label in outline
        ),
        body="\n".join(body),
    )


def visible_messages(
    messages: list[dict[str, Any]], include_synthetic: bool
) -> list[dict[str, Any]]:
    if include_synthetic:
        return messages
    return [msg for msg in messages if msg.get("type") not in DEFAULT_HIDDEN_MESSAGE_TYPES]


def has_visible_chat_messages(messages: list[dict[str, Any]]) -> bool:
    return any(msg.get("type") in {"user", "assistant"} for msg in messages)


def render_user_message(message: dict[str, Any], anchor: str, turn: int) -> str:
    text = render_markdownish(extract_user_text(message))
    meta = message_meta(message)
    return f"""
<section class="message user" id="{escape(anchor)}">
  <div class="avatar">H</div>
  <div class="bubble">
    <div class="message-head"><span class="message-title"><strong>Human</strong><button class="anchor-copy" type="button" data-anchor="{escape(anchor)}" aria-label="Copy link to Human {turn}" title="Copy link">¶</button></span><span>{escape(meta)}</span></div>
    <div class="content">{text}</div>
  </div>
</section>
"""


def render_assistant_message(
    message: dict[str, Any],
    turn: int,
    anchor: str,
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
) -> str:
    data = message.get("json") or {}
    blocks = []
    for item in data.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            blocks.append(f'<div class="content">{render_markdownish(item.get("text") or "")}</div>')
        elif item_type == "reasoning":
            blocks.append(
                details_block("Reasoning", render_pre(item.get("text") or ""), "reasoning")
            )
        elif item_type == "tool":
            blocks.append(render_tool_block(item, turn, diff_highlight_lines))
    if data.get("error"):
        blocks.append(details_block("Error", render_json(data["error"]), "error"))
    if not blocks:
        blocks.append('<div class="content muted">(empty assistant message)</div>')
    meta = message_meta(message)
    return f"""
<section class="message assistant" id="{escape(anchor)}">
  <div class="avatar">AI</div>
  <div class="bubble">
    <div class="message-head"><span class="message-title"><strong>AI</strong><button class="anchor-copy" type="button" data-anchor="{escape(anchor)}" aria-label="Copy link to AI response" title="Copy link">¶</button></span><span>{escape(meta)}</span></div>
    {"".join(blocks)}
  </div>
</section>
"""


def render_tool_block(
    item: dict[str, Any],
    turn: int,
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
) -> str:
    name = str(item.get("name") or "tool")
    state = item.get("state") or {}
    status = str(state.get("status") or "unknown")
    anchor = f"tool-{escape_id(item.get('id') or name)}"
    sections = [f'<div id="{anchor}"></div>']
    if "input" in state:
        sections.append("<h4>Input</h4>" + render_json(state["input"]))
    for key in ("content", "output", "structured"):
        if key in state:
            sections.append(f"<h4>{escape(key.title())}</h4>" + render_json(state[key]))
    tool_html = details_block(
        f"Tool: {name} {status}", "\n".join(sections), f"tool {status}"
    )
    diff_html = render_tool_diffs(name, state, diff_highlight_lines)
    return tool_html + diff_html


def render_tool_diffs(
    name: str,
    state: dict[str, Any],
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
) -> str:
    diffs = []
    structured = state.get("structured")
    if isinstance(structured, dict) and structured.get("diff"):
        diffs.append(str(structured["diff"]))
    input_data = state.get("input")
    if isinstance(input_data, dict) and input_data.get("patchText"):
        patch = str(input_data["patchText"])
        if patch not in diffs:
            diffs.append(patch)
    blocks = []
    for index, diff in enumerate(diffs, start=1):
        file_name = extract_diff_file(diff) or name
        anchor = f"diff-{escape_id(file_name)}-{index}"
        content = f'<div id="{anchor}"></div>{render_diff(diff, diff_highlight_lines)}'
        blocks.append(details_block(f"File Diff: {file_name}", content, "diff"))
    return "".join(blocks)


def load_session_diffs(session_id: str | None, base_dir: Path | None) -> list[dict[str, Any]]:
    if not session_id or base_dir is None:
        return []
    diff_path = base_dir / "storage" / "session_diff" / f"{session_id}.json"
    if not diff_path.exists():
        return []
    try:
        data = json.loads(diff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def render_session_diffs(
    diffs: list[dict[str, Any]],
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
) -> str:
    blocks = ['<section class="message event" id="session-diffs"><div class="bubble full">']
    blocks.append('<div class="message-head"><strong>Session file changes</strong></div>')
    for diff in diffs:
        if not isinstance(diff, dict):
            continue
        file_name = str(diff.get("file") or "unknown file")
        patch = str(diff.get("patch") or "")
        stats = f'{diff.get("status", "")} +{diff.get("additions", 0)} -{diff.get("deletions", 0)}'
        blocks.append(
            details_block(
                f"Session Diff: {file_name}",
                f"<p>{escape(stats)}</p>{render_diff(patch, diff_highlight_lines)}",
                "diff",
            )
        )
    blocks.append("</div></section>")
    return "\n".join(blocks)


def render_session_diffs_html(
    session: dict[str, Any],
    diffs: list[dict[str, Any]],
    page_nav: str,
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
) -> str:
    title = str(session.get("title") or session.get("id") or "opencode session") + " - File changes"
    return HTML_TEMPLATE.format(
        title=escape(title),
        session_id=escape(str(session.get("id") or "")),
        subtitle=escape(session_subtitle(session)),
        page_nav=page_nav,
        page_nav_bottom=page_nav,
        outline="",
        body=render_session_diffs(diffs, diff_highlight_lines),
    )


def render_event_message(message: dict[str, Any]) -> str:
    label = str(message.get("type") or "event")
    return f"""
<section class="message event">
  <div class="bubble full">
    <div class="message-head"><strong>{escape(label)}</strong><span>{escape(message_meta(message))}</span></div>
    {render_json(message.get("json") or {})}
  </div>
</section>
"""


def render_compaction_message(message: dict[str, Any]) -> str:
    meta = message_meta(message)
    return f"""
<div class="compaction-separator" role="separator" aria-label="Conversation compacted">
  <span>Conversation compacted</span>
  <small>{escape(meta)}</small>
</div>
"""


def session_subtitle(session: dict[str, Any]) -> str:
    pieces = [
        str(session.get("id") or ""),
        str(session.get("directory") or ""),
        format_time(session.get("time_created")),
    ]
    return " | ".join(p for p in pieces if p)


def message_meta(message: dict[str, Any]) -> str:
    seq = message.get("seq")
    created = format_time(message.get("time_created"))
    return " | ".join(p for p in (f"seq {seq}" if seq is not None else "", created) if p)


def format_time(value: Any) -> str:
    if value is None:
        return ""
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(value)


def render_markdownish(text: str) -> str:
    rendered = markdown_lib.markdown(
        text or "",
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )
    return render_mermaid_blocks(rendered)


def render_mermaid_blocks(rendered: str) -> str:
    pattern = re.compile(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        return f'<div class="mermaid" role="img">{match.group(1)}</div>'

    return pattern.sub(replace, rendered)


def render_pre(text: str) -> str:
    return f"<pre>{escape(str(text))}</pre>"


def render_json(value: Any) -> str:
    return render_pre(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def render_diff(diff: str, max_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES) -> str:
    raw = str(diff)
    raw_lines = raw.splitlines()
    if max_highlight_lines == 0 or len(raw_lines) > max_highlight_lines:
        return f'<pre class="diff-code diff-plain">{escape(raw)}</pre>'
    lines = []
    for line in raw_lines:
        cls = "ctx"
        if line.startswith("+") and not line.startswith("+++"):
            cls = "add"
        elif line.startswith("-") and not line.startswith("---"):
            cls = "del"
        elif line.startswith("@@"):
            cls = "hunk"
        lines.append(f'<span class="{cls}">{escape(line)}</span>')
    return '<pre class="diff-code">' + "".join(lines) + "</pre>"


def details_block(summary: str, content: str, class_name: str) -> str:
    return f'<details class="{escape(class_name)}"><summary>{escape(summary)}</summary>{content}</details>'


def extract_diff_file(diff: str) -> str | None:
    patterns = [
        r"^Index:\s+(.+)$",
        r"^\*\*\* (?:Update|Add|Delete) File:\s+(.+)$",
        r"^\+\+\+\s+(?:b/)?(.+)$",
    ]
    for line in str(diff).splitlines():
        for pattern in patterns:
            match = re.match(pattern, line)
            if match:
                name = match.group(1).strip()
                if name and name != "/dev/null":
                    return name
    return None


def split_message_pages(
    messages: list[dict[str, Any]], page_message_count: int
) -> list[list[dict[str, Any]]]:
    if page_message_count <= 0 or len(messages) <= page_message_count:
        return [messages]
    return [
        messages[index : index + page_message_count]
        for index in range(0, len(messages), page_message_count)
    ]


def paginated_filename(base_name: str, page_index: int) -> str:
    return f"{base_name}-p{page_index:03d}.html"


def render_page_nav(
    current_page: int,
    total_pages: int,
    page_filenames: list[str],
    diff_filename: str | None = None,
    is_diff_page: bool = False,
    show_jump: bool = False,
) -> str:
    if total_pages <= 1 and not diff_filename:
        return ""
    label = "File changes" if is_diff_page else f"Page {current_page} / {total_pages}"
    first_href = None
    prev_href = None
    next_href = None
    last_href = None
    if is_diff_page:
        prev_href = page_filenames[-1] if page_filenames else None
    else:
        if current_page > 1 and page_filenames:
            first_href = page_filenames[0]
        if current_page > 1:
            prev_href = page_filenames[current_page - 2]
        if current_page < total_pages:
            next_href = page_filenames[current_page]
            last_href = page_filenames[-1]
        elif diff_filename:
            next_href = diff_filename
    pieces = [
        '<nav class="page-nav" aria-label="Page navigation">',
        '<a href="index.html">Index</a>',
        nav_link("First", first_href) if not is_diff_page else "",
        nav_link("Previous", prev_href),
        f'<span class="page-nav__label">{escape(label)}</span>',
        render_page_jump(current_page, total_pages, page_filenames[0])
        if show_jump and not is_diff_page
        else "",
        nav_link("Next", next_href),
        nav_link("Last", last_href) if not is_diff_page else "",
    ]
    if diff_filename and not is_diff_page:
        pieces.append(f'<a href="{escape(diff_filename)}">File changes</a>')
    pieces.append("</nav>")
    return "".join(pieces)


def nav_link(label: str, href: str | None) -> str:
    if not href:
        return f'<span class="page-nav__disabled">{escape(label)}</span>'
    return f'<a href="{escape(href)}">{escape(label)}</a>'


def render_page_jump(current_page: int, total_pages: int, first_filename: str) -> str:
    base_name = first_filename.removesuffix("-p001.html")
    return (
        '<form class="page-jump" aria-label="Jump to page" '
        f'data-page-base="{escape(base_name)}" data-page-count="{total_pages}">'
        '<label>Go to '
        f'<input class="page-jump__input" name="page" type="number" min="1" max="{total_pages}" '
        f'value="{current_page}" inputmode="numeric">'
        "</label></form>"
    )


def render_index_session_item(
    title: Any,
    first_filename: str,
    page_filenames: list[str],
    diff_filename: str | None,
) -> str:
    child_links = [
        f'<li><a href="{escape(filename)}">Page {index}</a></li>'
        for index, filename in enumerate(page_filenames, start=1)
    ]
    if diff_filename:
        child_links.append(f'<li><a href="{escape(diff_filename)}">File changes</a></li>')
    nested = f'<ul>{"".join(child_links)}</ul>' if child_links else ""
    return (
        '<li><details class="session-pages">'
        f'<summary><a href="{escape(first_filename)}">{escape(title)}</a></summary>'
        f"{nested}</details></li>"
    )


def write_html_exports(
    db_path: Path,
    output: Path,
    session_id: str | None,
    diff_base_dir: Path | None = None,
    include_synthetic: bool = False,
    jobs: int = DEFAULT_JOBS,
    diff_highlight_lines: int = DEFAULT_DIFF_HIGHLIGHT_LINES,
    page_message_count: int = DEFAULT_PAGE_MESSAGE_COUNT,
    provider: SourceProvider | None = None,
) -> list[Path]:
    source = provider or get_source_provider("opencode")
    session_headers = source.load_session_headers(db_path, session_id)
    source_root = diff_base_dir or source.diff_base_dir(db_path)
    output.mkdir(parents=True, exist_ok=True)

    def write_one(session_header: dict[str, Any]) -> tuple[list[Path], str]:
        session = source.load_session(db_path, str(session_header["id"]))
        if session is None:
            raise ValueError(f"Session not found: {session_header['id']}")
        base_name = safe_filename(f"{session.get('id')}-{session.get('slug') or 'session'}")
        all_messages = visible_messages(session.get("messages") or [], include_synthetic)
        if session_id is None and not has_visible_chat_messages(all_messages):
            return [], ""
        pages = split_message_pages(all_messages, page_message_count)
        written_paths: list[Path] = []
        if len(pages) == 1:
            filename = base_name + ".html"
            target = output / filename
            page_session = dict(session)
            page_session["messages"] = pages[0]
            target.write_text(
                render_session_html(
                    page_session,
                    source_root,
                    include_synthetic=True,
                    diff_highlight_lines=diff_highlight_lines,
                ),
                encoding="utf-8",
            )
            written_paths.append(target)
            index_link = (
                f'<li><a href="{escape(filename)}">{escape(session.get("title") or session.get("id"))}</a></li>'
            )
        else:
            page_filenames = [
                paginated_filename(base_name, page_index)
                for page_index in range(1, len(pages) + 1)
            ]
            session_diffs = load_session_diffs(session.get("id"), source_root)
            diff_filename = f"{base_name}-diffs.html" if session_diffs else None
            turn_offset = 0
            ai_offset = 0
            for page_index, page_messages in enumerate(pages, start=1):
                filename = page_filenames[page_index - 1]
                target = output / filename
                page_session = dict(session)
                page_session["messages"] = page_messages
                bottom_nav = render_page_nav(
                    page_index,
                    len(pages),
                    page_filenames,
                    diff_filename=diff_filename,
                    show_jump=True,
                )
                target.write_text(
                    render_session_html(
                        page_session,
                        source_root,
                        include_synthetic=True,
                        diff_highlight_lines=diff_highlight_lines,
                        include_session_diffs=False,
                        page_nav_bottom=bottom_nav,
                        title_suffix=f" - Page {page_index}",
                        initial_turn=turn_offset,
                        initial_ai_turn=ai_offset,
                    ),
                    encoding="utf-8",
                )
                written_paths.append(target)
                turn_offset += sum(1 for msg in page_messages if msg.get("type") == "user")
                ai_offset += sum(1 for msg in page_messages if msg.get("type") == "assistant")
            if diff_filename:
                target = output / diff_filename
                target.write_text(
                    render_session_diffs_html(
                        session,
                        session_diffs,
                        render_page_nav(
                            len(pages),
                            len(pages),
                            page_filenames,
                            diff_filename=diff_filename,
                            is_diff_page=True,
                        ),
                        diff_highlight_lines,
                    ),
                    encoding="utf-8",
                )
                written_paths.append(target)
            filename = page_filenames[0]
            index_link = render_index_session_item(
                session.get("title") or session.get("id"),
                filename,
                page_filenames,
                diff_filename,
            )
        return written_paths, index_link

    worker_count = max(1, min(jobs, len(session_headers) or 1))
    if worker_count == 1:
        results = [write_one(session) for session in session_headers]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(write_one, session_headers))

    written = [target for targets, _ in results for target in targets]
    index_links = [link for _, link in results if link]
    index = output / "index.html"
    index.write_text(
        INDEX_TEMPLATE.format(items="\n".join(index_links), count=len(index_links)),
        encoding="utf-8",
    )
    written.append(index)
    return written


def write_summary_output(summary: Any, output: Path | None) -> None:
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if output is None:
        print(text)
        return
    if output.suffix.lower() == ".json":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    else:
        output.mkdir(parents=True, exist_ok=True)
        (output / "summary.json").write_text(text + "\n", encoding="utf-8")


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value[:180] or "session"


def escape_id(value: Any) -> str:
    return safe_filename(str(value)).lower()


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output) if args.output else None
    provider = get_source_provider(args.source)
    source_path = provider.source_path(args)
    with provider.copied_source(source_path) as copied_source:
        if args.summary_only:
            summary = build_summary(
                copied_source,
                args.session_id,
                args.summary_chars,
                provider=provider,
            )
            write_summary_output(summary, output)
        else:
            if output is None:
                raise ValueError("--output is required for HTML export")
            written = write_html_exports(
                copied_source,
                output,
                args.session_id,
                provider.diff_base_dir(source_path),
                include_synthetic=args.include_synthetic,
                jobs=args.jobs,
                diff_highlight_lines=args.diff_highlight_lines,
                page_message_count=0 if args.single_file else args.page_message_count,
                provider=provider,
            )
            print(f"Exported {len(written)} file(s) to {output}")
    return 0


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script>
    (function () {{
      var saved = localStorage.getItem("opencode-export-theme");
      var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      document.documentElement.dataset.theme = saved || (prefersDark ? "dark" : "light");
    }})();
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f5f1;
      --paper: #fffefa;
      --paper-soft: #f8f7f3;
      --paper-raised: #ffffff;
      --text: #37352f;
      --heading: #24231f;
      --muted: #77736b;
      --faint: #a8a29a;
      --line: #e5e1d8;
      --line-strong: #d3cec2;
      --accent: #2f6f6d;
      --accent-soft: #e5f0ef;
      --accent-flash: #f3c65d;
      --human: #315f7d;
      --assistant: #5b665a;
      --avatar-human-bg: #3f7fb3;
      --avatar-ai-bg: #c85d5d;
      --conversation-rule: #d7d2c8;
      --code-bg: #f2f1ec;
      --code-text: #b64646;
      --code-block-bg: #f7f6f3;
      --code-block-text: #24292f;
      --shadow: 0 18px 55px rgba(45, 42, 35, 0.08), 0 2px 8px rgba(45, 42, 35, 0.04);
      --add: #e7f4ec;
      --add-text: #1f6b3a;
      --del: #f9e7e7;
      --del-text: #9c3434;
      --hunk: #e9eff7;
      --hunk-text: #315f8c;
    }}
    [data-theme="dark"] {{
      color-scheme: dark;
      --bg: #17191d;
      --paper: #20242a;
      --paper-soft: #252a31;
      --paper-raised: #2a3038;
      --text: #d9d7d1;
      --heading: #f1eee7;
      --muted: #a8a39a;
      --faint: #777168;
      --line: #373c44;
      --line-strong: #4a515c;
      --accent: #8fb8b4;
      --accent-soft: #263a3a;
      --accent-flash: #f0c86a;
      --human: #8fb7d9;
      --assistant: #a9b89f;
      --avatar-human-bg: #2e5f86;
      --avatar-ai-bg: #914545;
      --conversation-rule: #414851;
      --code-bg: #2d333b;
      --code-text: #ffb4a8;
      --code-block-bg: #171b22;
      --code-block-text: #d5d9e0;
      --shadow: 0 18px 55px rgba(0, 0, 0, 0.28), 0 2px 8px rgba(0, 0, 0, 0.22);
      --add: #173424;
      --add-text: #b8e7c8;
      --del: #3c2025;
      --del-text: #f0b8be;
      --hunk: #203048;
      --hunk-text: #bdd7f0;
    }}
    [data-theme="light"] {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body.session-export {{
      margin: 0;
      background:
        radial-gradient(circle at top left, color-mix(in srgb, var(--accent) 12%, transparent), transparent 34rem),
        var(--bg);
      color: var(--text);
      font-family: sans-serif;
      font-size: 16px;
      line-height: 1.62;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 292px;
      gap: 34px;
      width: min(1480px, calc(100vw - 44px));
      margin: 0 auto;
      padding: 28px 0 44px;
    }}
    .timeline {{
      min-width: 0;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: var(--shadow);
      padding: clamp(26px, 4vw, 58px);
    }}
    .document-header {{
      border-bottom: 1px solid var(--line);
      margin-bottom: 30px;
      padding-bottom: 22px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
    }}
    h1 {{
      color: var(--heading);
      font-size: clamp(26px, 4vw, 38px);
      line-height: 1.18;
      margin: 0 0 10px;
      letter-spacing: 0;
      font-weight: 760;
    }}
    .subtitle {{
      color: var(--muted);
      overflow-wrap: anywhere;
      font-size: 13px;
    }}
    .theme-toggle {{
      flex: 0 0 auto;
      border: 1px solid var(--line-strong);
      background: var(--paper-raised);
      color: var(--text);
      border-radius: 999px;
      padding: 7px 12px;
      font: inherit;
      font-size: 12px;
      line-height: 1.2;
      cursor: pointer;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
    }}
    .theme-toggle:hover {{ border-color: var(--accent); color: var(--accent); }}
    .theme-toggle:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
    .page-nav {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin: 14px 0 22px;
      padding: 9px 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }}
    .page-nav a,
    .page-nav__disabled,
    .page-nav__label {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      text-decoration: none;
      background: var(--paper-soft);
    }}
    .page-nav a:hover {{ background: var(--accent-soft); color: var(--heading); }}
    .page-nav__label {{ color: var(--heading); font-weight: 700; }}
    .page-nav__disabled {{ color: var(--faint); }}
    .page-jump {{
      display: inline-flex;
      align-items: center;
      margin: 0;
    }}
    .page-jump label {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      background: var(--paper-soft);
      color: var(--muted);
      line-height: 1.2;
    }}
    .page-jump__input {{
      width: 4.6em;
      min-width: 0;
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      background: var(--paper-raised);
      color: var(--heading);
      padding: 2px 7px;
      font: inherit;
    }}
    .page-jump__input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .message {{
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      gap: 16px;
      margin: 26px 0;
      position: relative;
      scroll-margin-top: 24px;
    }}
    .message::before {{
      content: "";
      position: absolute;
      left: 21px;
      top: 46px;
      bottom: -26px;
      border-left: 1px solid var(--conversation-rule);
    }}
    .message:last-of-type::before {{ display: none; }}
    .avatar {{
      width: 42px;
      height: 42px;
      border-radius: 50%;
      border: 1px solid var(--line-strong);
      background: var(--paper-raised);
      display: grid;
      place-items: center;
      color: var(--muted);
      font-weight: 760;
      font-size: 12px;
      letter-spacing: .02em;
      z-index: 1;
    }}
    .user .avatar {{ color: #ffffff; }}
    .assistant .avatar {{ color: #ffffff; }}
    .user .avatar {{ background: var(--avatar-human-bg); border-color: color-mix(in srgb, var(--human) 34%, var(--line)); }}
    .assistant .avatar {{ background: var(--avatar-ai-bg); border-color: color-mix(in srgb, var(--assistant) 34%, var(--line)); }}
    .bubble {{
      min-width: 0;
      background: transparent;
      border: 0;
      border-radius: 0;
      padding: 0 0 0 2px;
    }}
    .event {{ grid-template-columns: minmax(0, 1fr); }}
    .event::before {{ display: none; }}
    .full {{
      grid-column: 1 / -1;
      background: var(--paper-soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .compaction-separator {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin: 28px 0;
      color: var(--faint);
      font-size: 12px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    .compaction-separator::before,
    .compaction-separator::after {{
      content: "";
      flex: 1;
      border-top: 1px dashed var(--line-strong);
    }}
    .compaction-separator span {{
      color: var(--muted);
      white-space: nowrap;
    }}
    .compaction-separator small {{
      color: var(--faint);
      font-size: 11px;
      letter-spacing: 0;
      text-transform: none;
      white-space: nowrap;
    }}
    .message-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 16px;
      color: var(--muted);
      margin-bottom: 10px;
      font-size: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 6px;
    }}
    .message-head strong {{
      color: var(--heading);
      font-size: 13px;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .message-title {{
      display: inline-flex;
      align-items: baseline;
      gap: 6px;
      min-width: 0;
    }}
    .anchor-copy {{
      border: 0;
      background: transparent;
      color: var(--accent);
      cursor: pointer;
      font: inherit;
      font-size: 16px;
      font-weight: 700;
      line-height: 1;
      opacity: 0;
      padding: 0 3px;
      transform: translateY(1px);
      transition: opacity .14s ease, color .14s ease;
    }}
    .message-title:hover .anchor-copy,
    .anchor-copy:focus-visible,
    .anchor-copy.copied {{
      opacity: 1;
    }}
    .anchor-copy:hover,
    .anchor-copy:focus-visible {{
      color: var(--heading);
      outline: none;
    }}
    .content {{ max-width: 92ch; }}
    p {{ margin: 0 0 12px; overflow-wrap: anywhere; }}
    p:last-child {{ margin-bottom: 0; }}
    h1, h2, h3 {{
      color: var(--heading);
      margin: 1.25em 0 .55em;
      line-height: 1.28;
      letter-spacing: 0;
    }}
    h2 {{
      font-size: 1.45rem;
      border-bottom: 1px solid var(--line);
      padding-bottom: .28em;
    }}
    h3 {{ font-size: 1.18rem; }}
    a {{ color: var(--accent); text-decoration-thickness: .08em; text-underline-offset: .18em; }}
    ul, ol {{ margin: 8px 0 14px 1.45em; padding: 0; }}
    li {{ margin: 5px 0; }}
    blockquote {{
      margin: 16px 0;
      padding: 4px 0 4px 16px;
      color: var(--muted);
      border-left: 3px solid var(--accent);
    }}
    code {{
      background: var(--code-bg);
      color: var(--code-text);
      border: 1px solid var(--line);
      border-radius: 5px;
      padding: .08em .34em;
      font-family: monospace;
      font-size: 85%;
      line-height: 1.45;
    }}
    pre code {{
      background: transparent;
      color: inherit;
      border: 0;
      border-radius: 0;
      padding: 0;
      font: inherit;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 14px 0;
      display: block;
      overflow-x: auto;
    }}
    th, td {{ border: 1px solid var(--line); padding: 8px 10px; text-align: left; }}
    th {{ background: var(--paper-soft); color: var(--heading); }}
    details {{
      border: 1px solid var(--line);
      border-radius: 7px;
      margin: 8px 0;
      background: var(--paper-soft);
      overflow: hidden;
    }}
    details[open] {{ box-shadow: inset 3px 0 0 var(--accent); }}
    summary {{
      cursor: pointer;
      padding: 7px 10px;
      color: var(--accent);
      font-weight: 700;
      font-size: 13px;
      user-select: none;
    }}
    details > pre, details > h4, details > p, details > div {{ margin-left: 10px; margin-right: 10px; }}
    h4 {{ color: var(--muted); margin: 8px 0 4px; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }}
    pre {{
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: var(--code-block-bg);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      color: var(--code-block-text);
      font-family: monospace;
      font-size: 12px;
      line-height: 1.45;
    }}
    .mermaid {{
      margin: 16px 0;
      overflow: auto;
      background: var(--code-block-bg);
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 14px;
      color: var(--code-block-text);
      text-align: center;
    }}
    .mermaid svg {{
      max-width: 100%;
      height: auto;
    }}
    .diff-code span {{ display: block; min-height: 1.35em; padding: 0 5px; }}
    .diff-code .add {{ background: var(--add); color: var(--add-text); }}
    .diff-code .del {{ background: var(--del); color: var(--del-text); }}
    .diff-code .hunk {{ background: var(--hunk); color: var(--hunk-text); }}
    .outline {{
      position: sticky;
      top: 24px;
      align-self: start;
      max-height: calc(100vh - 48px);
      overflow: auto;
      border-left: 1px solid var(--line);
      padding: 4px 0 4px 18px;
      color: var(--muted);
      transition: transform .18s ease, opacity .18s ease;
    }}
    body.outline-collapsed .outline {{
      transform: translateX(100%);
      opacity: 0;
      pointer-events: none;
    }}
    body.outline-open .outline {{
      transform: translateX(0);
      opacity: 1;
      pointer-events: auto;
    }}
    .outline h2 {{
      font-size: 11px;
      text-transform: uppercase;
      color: var(--faint);
      letter-spacing: .12em;
      margin: 0 0 10px;
      border: 0;
      padding: 0;
    }}
    .outline a {{
      display: block;
      color: var(--muted);
      text-decoration: none;
      padding: 6px 8px;
      margin: 1px 0;
      border-radius: 6px;
      overflow-wrap: anywhere;
      font-size: 13px;
      line-height: 1.35;
      border-left: 3px solid transparent;
      transition: background-color .16s ease, color .16s ease, border-color .16s ease;
    }}
    .outline a:hover {{ background: var(--accent-soft); color: var(--heading); }}
    .outline a.is-current {{
      background: var(--accent-soft);
      border-left-color: var(--accent);
      color: var(--heading);
      font-weight: 700;
    }}
    .outline a.is-flashing {{
      animation: outline-flash 1.15s ease-out;
    }}
    .outline-toggle {{
      position: fixed;
      right: 24px;
      top: 24px;
      z-index: 30;
      border: 1px solid var(--line-strong);
      background: var(--paper-raised);
      color: var(--accent);
      border-radius: 999px;
      padding: 8px 12px;
      font: inherit;
      font-size: 12px;
      line-height: 1.2;
      cursor: pointer;
      box-shadow: var(--shadow);
    }}
    .outline-toggle:hover {{ background: var(--accent-soft); color: var(--heading); }}
    .outline-toggle:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
    .floating-jump {{
      position: fixed;
      right: 24px;
      bottom: 24px;
      display: grid;
      gap: 10px;
      z-index: 20;
    }}
    .floating-jump a {{
      width: 42px;
      height: 42px;
      border-radius: 999px;
      border: 1px solid var(--line-strong);
      background: var(--paper-raised);
      color: var(--accent);
      display: grid;
      place-items: center;
      text-decoration: none;
      font-size: 20px;
      line-height: 1;
      box-shadow: var(--shadow);
    }}
    .floating-jump a:hover {{ background: var(--accent-soft); color: var(--heading); }}
    .floating-jump a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
    .back-to-top {{ grid-row: 1; }}
    .scroll-to-bottom {{ grid-row: 2; }}
    .muted {{ color: var(--muted); }}
    @keyframes outline-flash {{
      0% {{ background: color-mix(in srgb, var(--accent-flash) 38%, var(--paper-raised)); color: var(--heading); }}
      100% {{ background: var(--accent-soft); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      html {{ scroll-behavior: auto; }}
      .outline a, .anchor-copy {{ transition: none; }}
      .outline a.is-flashing {{ animation: none; }}
    }}
    @media (max-width: 980px) {{
      .layout {{ grid-template-columns: 1fr; width: min(100vw - 24px, 920px); padding-top: 12px; }}
      .timeline {{ padding: 22px; }}
      .outline {{
        position: fixed;
        top: 72px;
        right: 12px;
        bottom: 76px;
        width: min(340px, calc(100vw - 24px));
        max-height: none;
        background: var(--paper-raised);
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 14px;
        box-shadow: var(--shadow);
        z-index: 25;
        transform: translateX(100%);
      }}
      body.outline-open .outline {{ transform: translateX(0); opacity: 1; pointer-events: auto; }}
      body.outline-collapsed .outline {{ transform: translateX(100%); opacity: 0; pointer-events: none; }}
      .topbar {{ flex-direction: column; }}
    }}
    @media print {{
      body.session-export {{ background: white; }}
      .layout {{ display: block; width: auto; padding: 0; }}
      .timeline {{ border: 0; box-shadow: none; padding: 0; }}
        .outline, .theme-toggle, .floating-jump, .outline-toggle {{ display: none; }}
      .page-nav {{ display: none; }}
    }}
  </style>
</head>
<body class="session-export" id="top">
  <div class="layout">
    <main class="timeline">
      <header class="document-header">
        <div class="topbar">
          <div>
            <h1>{title}</h1>
            <div class="subtitle">{subtitle}</div>
          </div>
          <button id="theme-toggle" class="theme-toggle" type="button" aria-label="Toggle color theme">Theme</button>
        </div>
      </header>
      {page_nav}
      {body}
      {page_nav_bottom}
      <div id="page-bottom"></div>
    </main>
    <nav id="session-outline" class="outline" aria-label="Session outline">
      <h2>Outline</h2>
      {outline}
    </nav>
  </div>
  <button id="outline-toggle" class="outline-toggle" type="button" aria-controls="session-outline" aria-expanded="true">Outline</button>
  <div class="floating-jump" aria-label="Page scroll shortcuts">
    <a id="back-to-top" class="back-to-top" href="#top" aria-label="Back to top" title="Back to top">↑</a>
    <a id="scroll-to-bottom" class="scroll-to-bottom" href="#page-bottom" aria-label="Scroll to bottom" title="Scroll to bottom">↓</a>
  </div>
  <script>
    (function () {{
      var key = "opencode-export-theme";
      var button = document.getElementById("theme-toggle");
      function currentTheme() {{
        return document.documentElement.dataset.theme || "light";
      }}
      function updateButton() {{
        var theme = currentTheme();
        button.textContent = theme === "dark" ? "Dark" : "Light";
        button.title = "Switch to " + (theme === "dark" ? "light" : "dark") + " mode";
      }}
      button.addEventListener("click", function () {{
        var next = currentTheme() === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        localStorage.setItem(key, next);
        updateButton();
      }});
      updateButton();
    }})();
    (function () {{
      function renderMermaid() {{
        var diagrams = document.querySelectorAll(".mermaid");
        if (!diagrams.length || !window.mermaid) {{
          return;
        }}
        var theme = document.documentElement.dataset.theme === "dark" ? "dark" : "default";
        window.mermaid.initialize({{
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme
        }});
        window.mermaid.run({{ nodes: diagrams }});
      }}
      if (document.readyState === "complete") {{
        renderMermaid();
      }} else {{
        window.addEventListener("load", renderMermaid, {{ once: true }});
      }}
    }})();
    (function () {{
      var button = document.getElementById("outline-toggle");
      var mobileQuery = window.matchMedia("(max-width: 980px)");
      function setOpen(open) {{
        document.body.classList.toggle("outline-open", open);
        document.body.classList.toggle("outline-collapsed", !open);
        button.setAttribute("aria-expanded", open ? "true" : "false");
        button.textContent = open ? "Hide outline" : "Outline";
      }}
      function resetForViewport() {{
        setOpen(!mobileQuery.matches);
      }}
      button.addEventListener("click", function () {{
        setOpen(!document.body.classList.contains("outline-open"));
      }});
      if (mobileQuery.addEventListener) {{
        mobileQuery.addEventListener("change", resetForViewport);
      }} else if (mobileQuery.addListener) {{
        mobileQuery.addListener(resetForViewport);
      }}
      resetForViewport();
    }})();
    (function () {{
      var outline = document.getElementById("session-outline");
      if (!outline) {{
        return;
      }}
      var links = Array.prototype.slice.call(outline.querySelectorAll("a[data-target]"));
      if (!links.length) {{
        return;
      }}
      var linkById = new Map();
      links.forEach(function (link) {{
        linkById.set(link.getAttribute("data-target"), link);
      }});
      var sections = links
        .map(function (link) {{
          return document.getElementById(link.getAttribute("data-target"));
        }})
        .filter(Boolean);
      var currentId = "";
      function setCurrent(id, scrollOutline) {{
        if (!id || id === currentId) {{
          return;
        }}
        var previousLink = linkById.get(currentId);
        if (previousLink) {{
          previousLink.classList.remove("is-current");
        }}
        currentId = id;
        var link = linkById.get(id);
        if (link) {{
          link.classList.add("is-current");
          if (scrollOutline) {{
            link.scrollIntoView({{ block: "nearest" }});
          }}
        }}
      }}
      function flashOutline(id) {{
        var link = linkById.get(id);
        if (!link) {{
          return;
        }}
        link.classList.remove("is-flashing");
        void link.offsetWidth;
        link.classList.add("is-flashing");
        window.setTimeout(function () {{
          link.classList.remove("is-flashing");
        }}, 1200);
      }}
      links.forEach(function (link) {{
        link.addEventListener("click", function () {{
          var id = link.getAttribute("data-target");
          setCurrent(id, true);
          window.setTimeout(function () {{
            flashOutline(id);
          }}, 180);
        }});
      }});
      if ("IntersectionObserver" in window) {{
        var visible = new Map();
        var observer = new IntersectionObserver(
          function (entries) {{
            entries.forEach(function (entry) {{
              if (entry.isIntersecting) {{
                visible.set(entry.target.id, entry.intersectionRatio);
              }} else {{
                visible.delete(entry.target.id);
              }}
            }});
            var best = null;
            var bestTop = Infinity;
            sections.forEach(function (section) {{
              if (!visible.has(section.id)) {{
                return;
              }}
              var top = Math.abs(section.getBoundingClientRect().top - window.innerHeight * 0.18);
              if (top < bestTop) {{
                bestTop = top;
                best = section;
              }}
            }});
            if (best) {{
              setCurrent(best.id, true);
            }}
          }},
          {{ root: null, rootMargin: "-12% 0px -58% 0px", threshold: [0, 0.1, 0.35, 0.7] }}
        );
        sections.forEach(function (section) {{
          observer.observe(section);
        }});
      }} else {{
        var ticking = false;
        window.addEventListener("scroll", function () {{
          if (ticking) {{
            return;
          }}
          ticking = true;
          window.requestAnimationFrame(function () {{
            ticking = false;
            var best = sections[0];
            var bestTop = Infinity;
            sections.forEach(function (section) {{
              var top = Math.abs(section.getBoundingClientRect().top - window.innerHeight * 0.18);
              if (top < bestTop) {{
                bestTop = top;
                best = section;
              }}
            }});
            if (best) {{
              setCurrent(best.id, true);
            }}
          }});
        }}, {{ passive: true }});
      }}
      setCurrent(sections[0].id, false);
    }})();
    (function () {{
      var buttons = Array.prototype.slice.call(document.querySelectorAll(".anchor-copy"));
      function copyText(text) {{
        if (navigator.clipboard && window.isSecureContext) {{
          return navigator.clipboard.writeText(text);
        }}
        var input = document.createElement("textarea");
        input.value = text;
        input.setAttribute("readonly", "");
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        input.select();
        try {{
          document.execCommand("copy");
        }} finally {{
          document.body.removeChild(input);
        }}
        return Promise.resolve();
      }}
      buttons.forEach(function (button) {{
        button.addEventListener("click", function (event) {{
          event.preventDefault();
          event.stopPropagation();
          var anchor = button.getAttribute("data-anchor");
          if (!anchor) {{
            return;
          }}
          var url = window.location.href.split("#")[0] + "#" + anchor;
          copyText(url).then(function () {{
            button.classList.add("copied");
            button.title = "Copied";
            window.setTimeout(function () {{
              button.classList.remove("copied");
              button.title = "Copy link";
            }}, 1200);
          }});
        }});
      }});
    }})();
    (function () {{
      var forms = Array.prototype.slice.call(document.querySelectorAll(".page-jump"));
      forms.forEach(function (form) {{
        form.addEventListener("submit", function (event) {{
          event.preventDefault();
          var input = form.querySelector("input[name='page']");
          var total = parseInt(form.getAttribute("data-page-count") || "1", 10);
          var page = parseInt(input.value || "1", 10);
          if (!Number.isFinite(page)) {{
            page = 1;
          }}
          page = Math.max(1, Math.min(total, page));
          var base = form.getAttribute("data-page-base") || "";
          var padded = String(page).padStart(3, "0");
          window.location.href = base + "-p" + padded + ".html";
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>opencode session exports</title>
  <style>
    body {{ margin: 32px; background: #111; color: #eee; font-family: sans-serif; font-size: 14px; line-height: 1.5; }}
    a {{ color: #8ec5ff; }}
    ul {{ padding-left: 1.25rem; }}
    li {{ margin: 0.35rem 0; }}
    .session-pages {{ margin: 0.55rem 0; }}
    .session-pages > summary {{ cursor: pointer; color: #ddd; }}
    .session-pages > summary a {{ font-weight: 700; }}
    .session-pages > ul {{
      margin-top: 0.35rem;
      border-left: 1px solid #3a3a3a;
      padding-left: 1.2rem;
    }}
  </style>
</head>
<body>
  <h1>opencode session exports</h1>
  <p>{count} session(s)</p>
  <ul>{items}</ul>
</body>
</html>
"""


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
