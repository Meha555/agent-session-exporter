import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import agent_session_exporter as exporter


def create_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (
            id text PRIMARY KEY,
            project_id text NOT NULL,
            parent_id text,
            slug text NOT NULL,
            directory text NOT NULL,
            title text NOT NULL,
            version text NOT NULL,
            share_url text,
            summary_additions integer,
            summary_deletions integer,
            summary_files integer,
            summary_diffs text,
            revert text,
            permission text,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            time_compacting integer,
            time_archived integer,
            workspace_id text,
            path text,
            agent text,
            model text,
            cost real DEFAULT 0 NOT NULL,
            tokens_input integer DEFAULT 0 NOT NULL,
            tokens_output integer DEFAULT 0 NOT NULL,
            tokens_reasoning integer DEFAULT 0 NOT NULL,
            tokens_cache_read integer DEFAULT 0 NOT NULL,
            tokens_cache_write integer DEFAULT 0 NOT NULL,
            metadata text
        );
        CREATE TABLE session_message (
            id text PRIMARY KEY,
            session_id text NOT NULL,
            type text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL,
            seq integer NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO session (
            id, project_id, slug, directory, title, version,
            time_created, time_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ses_test",
            "proj_test",
            "test-session",
            "C:/repo",
            "Synthetic Export Test",
            "1.0.0",
            1000,
            2000,
        ),
    )
    rows = [
        (
            "msg_user_1",
            "ses_test",
            "user",
            1000,
            1000,
            {"text": "Please inspect the build failure and patch the file."},
            1,
        ),
        (
            "msg_synthetic_1",
            "ses_test",
            "synthetic",
            1050,
            1050,
            {"text": "Called the Read tool with synthetic payload."},
            2,
        ),
        (
            "msg_assistant_1",
            "ses_test",
            "assistant",
            1100,
            1200,
            {
                "content": [
                    {"type": "reasoning", "text": "I need to inspect the failing test."},
                    {"type": "text", "text": "I found the failing path."},
                    {
                        "type": "tool",
                        "id": "call_1",
                        "name": "apply_patch",
                        "state": {
                            "status": "completed",
                            "input": {
                                "patchText": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch"
                            },
                            "structured": {
                                "diff": "Index: src/app.py\n@@\n-old\n+new\n"
                            },
                        },
                    },
                ]
            },
            3,
        ),
        (
            "msg_model_switched_1",
            "ses_test",
            "model-switched",
            1250,
            1250,
            {"model": "other-model"},
            4,
        ),
        (
            "msg_compaction_1",
            "ses_test",
            "compaction",
            1275,
            1275,
            {
                "summary": "SECRET COMPACTION SUMMARY THAT SHOULD NOT BE RENDERED",
                "tokens": 12345,
            },
            5,
        ),
        (
            "msg_user_2",
            "ses_test",
            "user",
            1300,
            1300,
            {"text": "Run the tests again."},
            6,
        ),
        (
            "msg_assistant_2",
            "ses_test",
            "assistant",
            1400,
            1500,
            {
                "content": [
                    {
                        "type": "tool",
                        "id": "call_2",
                        "name": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "python -m unittest"},
                            "content": [{"type": "text", "text": "OK"}],
                        },
                    },
                    {"type": "text", "text": "Tests pass now."},
                ]
            },
            7,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO session_message (
            id, session_id, type, time_created, time_updated, data, seq
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [(a, b, c, d, e, json.dumps(f), g) for a, b, c, d, e, f, g in rows],
    )
    conn.commit()
    conn.close()


def add_empty_session(path: Path, session_id: str = "ses_empty") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO session (
            id, project_id, slug, directory, title, version,
            time_created, time_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            "proj_test",
            "empty-session",
            "C:/repo",
            "Empty Session",
            "1.0.0",
            500,
            600,
        ),
    )
    conn.commit()
    conn.close()


def add_message_part_session(path: Path, session_id: str = "ses_message_part") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS message (
            id text PRIMARY KEY,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL
        );
        CREATE TABLE IF NOT EXISTS part (
            id text PRIMARY KEY,
            message_id text NOT NULL,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO session (
            id, project_id, slug, directory, title, version,
            time_created, time_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            "proj_test",
            "message-part-session",
            "C:/repo",
            "Message Part Session",
            "1.0.0",
            700,
            900,
        ),
    )
    conn.executemany(
        """
        INSERT INTO message (
            id, session_id, time_created, time_updated, data
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                "msg_new_user",
                session_id,
                700,
                700,
                json.dumps({"role": "user", "time": {"created": 700}}),
            ),
            (
                "msg_new_assistant",
                session_id,
                800,
                900,
                json.dumps({"role": "assistant", "time": {"created": 800, "completed": 900}}),
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO part (
            id, message_id, session_id, time_created, time_updated, data
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "part_user_text",
                "msg_new_user",
                session_id,
                701,
                701,
                json.dumps({"type": "text", "text": "Please inspect the new opencode schema."}),
            ),
            (
                "part_assistant_reasoning",
                "msg_new_assistant",
                session_id,
                801,
                801,
                json.dumps({"type": "reasoning", "text": "Need to read message and part tables."}),
            ),
            (
                "part_assistant_text",
                "msg_new_assistant",
                session_id,
                802,
                802,
                json.dumps({"type": "text", "text": "The session is stored in message/part."}),
            ),
            (
                "part_assistant_tool",
                "msg_new_assistant",
                session_id,
                803,
                803,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "shell",
                        "callID": "call_new",
                        "state": {
                            "status": "completed",
                            "input": {"command": "sqlite query"},
                            "output": "OK",
                        },
                    }
                ),
            ),
        ],
    )
    conn.commit()
    conn.close()


def create_codex_sessions(root: Path) -> str:
    session_id = "019f-test-codex"
    day_dir = root / "2026" / "07" / "01"
    day_dir.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-01T08:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": "2026-07-01T08:00:00Z",
                "cwd": "C:/repo",
                "originator": "Codex Desktop",
            },
        },
        {
            "timestamp": "2026-07-01T08:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>\n</environment_context>"}],
            },
        },
        {
            "timestamp": "2026-07-01T08:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Please inspect the failing test."}],
            },
        },
        {
            "timestamp": "2026-07-01T08:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"text": "Need to inspect files first."}],
            },
        },
        {
            "timestamp": "2026-07-01T08:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I found the failing path."}],
            },
        },
        {
            "timestamp": "2026-07-01T08:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call_1",
                "arguments": json.dumps({"command": "python -m unittest"}),
            },
        },
        {
            "timestamp": "2026-07-01T08:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "OK",
            },
        },
    ]
    target = day_dir / f"rollout-2026-07-01T16-00-00-{session_id}.jsonl"
    target.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return session_id


class ExportOpenCodeSessionsTest(unittest.TestCase):
    def test_summary_pairs_human_requests_with_llm_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "opencode.db"
            create_test_db(db_path)

            result = exporter.build_summary(db_path, "ses_test", summary_chars=18)

        self.assertEqual(
            result,
            [
                {"human": "Please inspect...", "ai": "I found the fai..."},
                {"human": "Run the tests a...", "ai": "Tests pass now."},
            ],
        )

    def test_html_contains_outline_and_collapsible_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            diff_dir = root / "storage" / "session_diff"
            diff_dir.mkdir(parents=True)
            create_test_db(db_path)
            (diff_dir / "ses_test.json").write_text(
                json.dumps(
                    [
                        {
                            "file": "README.md",
                            "patch": "Index: README.md\n@@\n-old\n+new\n",
                            "additions": 1,
                            "deletions": 1,
                            "status": "modified",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root)

        self.assertIn('class="outline"', html)
        self.assertIn('href="#turn-1"', html)
        self.assertIn("<summary>Reasoning</summary>", html)
        self.assertIn("<summary>Tool: apply_patch completed</summary>", html)
        self.assertIn("<summary>File Diff: src/app.py</summary>", html)
        self.assertIn("<summary>Session Diff: README.md</summary>", html)
        self.assertNotIn("Called the Read tool with synthetic payload.", html)
        self.assertNotIn("model-switched", html)
        self.assertIn("Conversation compacted", html)
        self.assertNotIn("SECRET COMPACTION SUMMARY", html)
        self.assertNotIn('"tokens"', html)

    def test_session_html_does_not_duplicate_top_page_nav_by_default(self) -> None:
        session = {
            "id": "ses_nav",
            "title": "Navigation Test",
            "messages": [],
        }

        html = exporter.render_session_html(
            session,
            page_nav='<nav class="page-nav"><form class="page-jump"></form></nav>',
        )

        self.assertEqual(html.count('class="page-jump"'), 1)

    def test_session_diff_rows_do_not_render_extra_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            diff_dir = root / "storage" / "session_diff"
            diff_dir.mkdir(parents=True)
            create_test_db(db_path)
            (diff_dir / "ses_test.json").write_text(
                json.dumps(
                    [
                        {
                            "file": "README.md",
                            "patch": "Index: README.md\n@@\n-old\n+new\n",
                            "additions": 1,
                            "deletions": 1,
                            "status": "modified",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root)

        session_diff = html.split("<summary>Session Diff: README.md</summary>", 1)[1]
        session_diff = session_diff.split("</details>", 1)[0]
        self.assertNotIn("</span>\n<span", session_diff)
        self.assertIn('</span><span class="hunk">@@</span>', session_diff)

    def test_outline_only_lists_human_and_ai_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            diff_dir = root / "storage" / "session_diff"
            diff_dir.mkdir(parents=True)
            create_test_db(db_path)
            (diff_dir / "ses_test.json").write_text("[]", encoding="utf-8")

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root)

        outline = html.split('id="session-outline"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('href="#turn-1"', outline)
        self.assertIn('data-target="turn-1"', outline)
        self.assertIn('href="#ai-1"', outline)
        self.assertIn('data-target="ai-1"', outline)
        self.assertIn('href="#turn-2"', outline)
        self.assertIn('href="#ai-2"', outline)
        self.assertNotIn("Tool:", outline)
        self.assertNotIn("Diff:", outline)
        self.assertNotIn("Session diffs", outline)
        self.assertIn("IntersectionObserver", html)
        self.assertIn(".outline a.is-current", html)
        self.assertNotIn(".message.is-current", html)
        self.assertIn("is-flashing", html)
        self.assertIn("flashOutline", html)
        self.assertIn('class="anchor-copy"', html)
        self.assertIn('data-anchor="turn-1"', html)
        self.assertIn("copyText", html)

    def test_html_uses_browser_generic_font_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            create_test_db(db_path)

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root)

        self.assertIn("font-family: sans-serif;", html)
        self.assertIn("font-family: monospace;", html)
        self.assertIn("font-family: sans-serif;", exporter.INDEX_TEMPLATE)
        for font_name in (
            "Segoe UI",
            "Microsoft YaHei",
            "Noto Sans SC",
            "PingFang SC",
            "JetBrains Mono",
            "Cascadia Code",
            "SFMono-Regular",
            "ui-sans-serif",
            "system-ui",
        ):
            self.assertNotIn(font_name, html)
            self.assertNotIn(font_name, exporter.INDEX_TEMPLATE)

    def test_html_can_include_synthetic_events_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            create_test_db(db_path)

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root, include_synthetic=True)

        self.assertIn("Called the Read tool with synthetic payload.", html)
        self.assertIn("model-switched", html)

    def test_html_includes_polished_theme_switching_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            create_test_db(db_path)

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root)

        self.assertIn('class="session-export"', html)
        self.assertIn('id="theme-toggle"', html)
        self.assertIn("opencode-export-theme", html)
        self.assertIn('[data-theme="dark"]', html)
        self.assertIn('[data-theme="light"]', html)
        self.assertIn("--paper:", html)
        self.assertIn("--conversation-rule:", html)
        self.assertIn("--avatar-human-bg: #3f7fb3;", html)
        self.assertIn("--avatar-ai-bg: #c85d5d;", html)
        self.assertIn("--avatar-human-bg: #2e5f86;", html)
        self.assertIn("--avatar-ai-bg: #914545;", html)
        self.assertIn(".user .avatar { background: var(--avatar-human-bg);", html)
        self.assertIn(".assistant .avatar { background: var(--avatar-ai-bg);", html)
        self.assertIn("details {", html)
        self.assertIn("margin: 8px 0;", html)
        self.assertIn('id="back-to-top"', html)
        self.assertIn('href="#top"', html)
        self.assertIn(".back-to-top {", html)
        self.assertIn("position: fixed;", html)
        self.assertIn('id="outline-toggle"', html)
        self.assertIn('aria-controls="session-outline"', html)
        self.assertIn('id="session-outline"', html)
        self.assertIn("outline-collapsed", html)
        self.assertIn("outline-open", html)
        self.assertIn(".outline-toggle {", html)
        self.assertIn("transform: translateX(100%);", html)
        self.assertIn("position: fixed;", html)

    def test_html_renders_markdown_in_chat_text(self) -> None:
        rendered = exporter.render_markdownish(
            "## Plan\n\n"
            "- inspect `opencode.db`\n"
            "- export **HTML**\n\n"
            "```python\n"
            "print('ok')\n"
            "```\n"
        )

        self.assertIn("<h2>Plan</h2>", rendered)
        self.assertIn("<li>inspect <code>opencode.db</code></li>", rendered)
        self.assertIn("<strong>HTML</strong>", rendered)
        self.assertIn("<pre><code class=\"language-python\">", rendered)

    def test_html_renders_two_space_nested_markdown_lists(self) -> None:
        rendered = exporter.render_markdownish(
            "- `dcc-daemon`\n"
            "  - `/inspect/daemon`\n"
            "  - 本机编译器列表、local task mgr\n"
            "- `dcc-scheduler`\n"
            "  - `/inspect/scheduler`\n"
        )

        self.assertIn("<li><code>dcc-daemon</code><ul>", rendered)
        self.assertIn("<li><code>/inspect/daemon</code></li>", rendered)
        self.assertIn("<li>本机编译器列表、local task mgr</li>", rendered)
        self.assertIn("<li><code>dcc-scheduler</code><ul>", rendered)
        self.assertIn("<li><code>/inspect/scheduler</code></li>", rendered)

    def test_markdown_list_indent_normalization_skips_fenced_code(self) -> None:
        rendered = exporter.render_markdownish(
            "```text\n"
            "- parent\n"
            "  - child\n"
            "```\n"
        )

        self.assertIn("- parent\n  - child", rendered)

    def test_html_renders_mermaid_code_blocks_as_diagrams(self) -> None:
        rendered = exporter.render_markdownish(
            "```mermaid\n"
            "graph TD\n"
            "  A-->B\n"
            "```\n"
        )

        self.assertIn('<div class="mermaid" role="img">', rendered)
        self.assertIn("graph TD", rendered)
        self.assertIn("A--&gt;B", rendered)
        self.assertNotIn('class="language-mermaid"', rendered)

    def test_html_template_loads_mermaid_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            create_test_db(db_path)

            sessions = exporter.load_sessions(db_path, "ses_test")
            html = exporter.render_session_html(sessions[0], root)

        self.assertIn("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js", html)
        self.assertIn(".mermaid {", html)
        self.assertIn("window.mermaid.initialize", html)
        self.assertIn("window.mermaid.run", html)

    def test_large_diff_uses_plain_pre_to_keep_dom_small(self) -> None:
        rendered = exporter.render_diff(
            "\n".join(f"+line {index}" for index in range(5)),
            max_highlight_lines=3,
        )

        self.assertIn('class="diff-code diff-plain"', rendered)
        self.assertNotIn("<span", rendered)
        self.assertIn("+line 0\n+line 1", rendered)

    def test_parse_args_exposes_parallel_and_diff_controls(self) -> None:
        args = exporter.parse_args(
            [
                "--db",
                "opencode.db",
                "--output",
                "out",
                "--source",
                "opencode",
                "--jobs",
                "3",
                "--diff-highlight-lines",
                "7",
            ]
        )

        self.assertEqual(args.jobs, 3)
        self.assertEqual(args.diff_highlight_lines, 7)
        self.assertEqual(args.source, "opencode")

    def test_source_provider_registry_exposes_opencode(self) -> None:
        provider = exporter.get_source_provider("opencode")

        self.assertEqual(provider.name, "opencode")
        with self.assertRaises(ValueError):
            exporter.get_source_provider("unknown-agent")

    def test_codex_provider_reads_jsonl_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            session_id = create_codex_sessions(root)
            provider = exporter.get_source_provider("codex")

            sessions = provider.load_sessions(root, session_id)
            summary = exporter.build_summary(root, session_id, provider=provider)
            html = exporter.render_session_html(sessions[0], root)

        self.assertEqual(provider.name, "codex")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(summary, [{"human": "Please inspect the failing test.", "ai": "I found the failing path."}])
        self.assertIn("<summary>Reasoning</summary>", html)
        self.assertIn("<strong>AI</strong>", html)
        self.assertIn("<summary>Tool: shell_command completed</summary>", html)
        self.assertIn("python -m unittest", html)
        self.assertIn("OK", html)

    def test_codex_provider_copies_sessions_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            session_id = create_codex_sessions(root)
            provider = exporter.get_source_provider("codex")

            with provider.copied_source(root) as copied:
                self.assertNotEqual(root.resolve(), copied.resolve())
                (root / "2026" / "07" / "01" / f"extra-{session_id}.jsonl").write_text(
                    "",
                    encoding="utf-8",
                )
                sessions = provider.load_sessions(copied, session_id)

        self.assertEqual(len(sessions), 1)

    def test_codex_cli_exports_from_sessions_dir_without_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            output = Path(tmp) / "out"
            session_id = create_codex_sessions(root)

            status = exporter.main(
                [
                    "--source",
                    "codex",
                    "--sessions-dir",
                    str(root),
                    "--session-id",
                    session_id,
                    "--output",
                    str(output),
                    "--single-file",
                ]
            )

            html_files = list(output.glob("*.html"))

        self.assertEqual(status, 0)
        self.assertTrue(any(path.name != "index.html" for path in html_files))

    def test_parse_args_exposes_pagination_controls(self) -> None:
        args = exporter.parse_args(
            [
                "--db",
                "opencode.db",
                "--output",
                "out",
                "--page-message-count",
                "2",
                "--single-file",
            ]
        )

        self.assertEqual(args.page_message_count, 2)
        self.assertTrue(args.single_file)

    def test_html_export_accepts_multiple_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            output = root / "out"
            create_test_db(db_path)

            written = exporter.write_html_exports(db_path, output, None, jobs=2)

        self.assertEqual(len(written), 2)
        self.assertTrue(any(path.name.endswith("test-session.html") for path in written))

    def test_html_export_all_skips_sessions_without_visible_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            output = root / "out"
            create_test_db(db_path)
            add_empty_session(db_path)

            written = exporter.write_html_exports(db_path, output, None, jobs=1)
            names = sorted(path.name for path in written)
            index = (output / "index.html").read_text(encoding="utf-8")

        self.assertIn("ses_test-test-session.html", names)
        self.assertIn("index.html", names)
        self.assertNotIn("ses_empty-empty-session.html", names)
        self.assertNotIn("Empty Session", index)
        self.assertIn("1 session(s)", index)

    def test_html_export_keeps_explicit_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            output = root / "out"
            create_test_db(db_path)
            add_empty_session(db_path)

            written = exporter.write_html_exports(db_path, output, "ses_empty", jobs=1)
            names = sorted(path.name for path in written)
            index = (output / "index.html").read_text(encoding="utf-8")

        self.assertIn("ses_empty-empty-session.html", names)
        self.assertIn("Empty Session", index)
        self.assertIn("1 session(s)", index)

    def test_load_session_falls_back_to_message_part_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "opencode.db"
            create_test_db(db_path)
            add_message_part_session(db_path)

            session = exporter.load_session(db_path, "ses_message_part")
            html = exporter.render_session_html(session)

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual([message["type"] for message in session["messages"]], ["user", "assistant"])
        self.assertIn("Please inspect the new opencode schema.", html)
        self.assertIn("The session is stored in message/part.", html)
        self.assertIn("<summary>Reasoning</summary>", html)
        self.assertIn("<summary>Tool: shell completed</summary>", html)

    def test_html_export_all_keeps_message_part_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            output = root / "out"
            create_test_db(db_path)
            add_message_part_session(db_path)

            written = exporter.write_html_exports(db_path, output, None, jobs=1)
            names = sorted(path.name for path in written)
            index = (output / "index.html").read_text(encoding="utf-8")

        self.assertIn("ses_message_part-message-part-session.html", names)
        self.assertIn("Message Part Session", index)
        self.assertIn("2 session(s)", index)

    def test_html_export_paginates_large_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "opencode.db"
            output = root / "out"
            create_test_db(db_path)

            written = exporter.write_html_exports(
                db_path,
                output,
                None,
                jobs=1,
                page_message_count=2,
            )

            names = sorted(path.name for path in written)
            self.assertIn("ses_test-test-session-p001.html", names)
            self.assertIn("ses_test-test-session-p002.html", names)
            self.assertIn("ses_test-test-session-p003.html", names)
            self.assertIn("index.html", names)

            index = (output / "index.html").read_text(encoding="utf-8")
            first = (output / "ses_test-test-session-p001.html").read_text(encoding="utf-8")
            second = (output / "ses_test-test-session-p002.html").read_text(encoding="utf-8")

        self.assertIn("ses_test-test-session-p001.html", index)
        self.assertIn("<ul>", index)
        self.assertIn('<details class="session-pages"', index)
        self.assertNotIn('<details class="session-pages" open>', index)
        self.assertIn("<summary>", index)
        self.assertIn("Page 1", index)
        self.assertIn("Page 2", index)
        self.assertIn("Page 1 / 3", first)
        self.assertEqual(first.count('class="page-nav"'), 1)
        self.assertIn('class="page-jump"', first)
        self.assertEqual(first.count('class="page-jump"'), 1)
        self.assertIn('<span class="page-nav__disabled">First</span>', first)
        self.assertIn('href="ses_test-test-session-p003.html">Last</a>', first)
        self.assertIn('name="page"', first)
        self.assertIn('href="ses_test-test-session-p002.html"', first)
        self.assertIn('id="scroll-to-bottom"', first)
        self.assertIn('href="#page-bottom"', first)
        self.assertIn('id="page-bottom"', first)
        self.assertIn("Page 2 / 3", second)
        self.assertIn('href="ses_test-test-session-p001.html">First</a>', second)
        self.assertIn('href="ses_test-test-session-p001.html"', second)
        self.assertIn('href="ses_test-test-session-p003.html">Last</a>', second)
        self.assertIn('href="index.html"', second)


if __name__ == "__main__":
    unittest.main()
