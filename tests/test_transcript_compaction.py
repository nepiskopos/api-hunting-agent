"""Offline test for transcript compaction (agent.loop.AgentLoop._compact_transcript).

This is the fix for a real failure mode found during development: without
bounding history growth, a run exhausted its entire token budget by step 27
with zero findings, purely from re-sending an ever-growing transcript on
every stateless chat-completions call. See loop.py's module-level comment
above KEEP_FULL_TOOL_MESSAGES for the full story.
"""

from __future__ import annotations

import json
import unittest

from agent.loop import (
    KEEP_FULL_TOOL_MESSAGES,
    OLD_TOOL_MESSAGE_CLIP_CHARS,
    AgentLoop,
    _replace_singleton_message,
    _strip_headers_for_clip,
    _tool_response_signature,
)


def _tool_message(content: str) -> dict:
    return {"role": "tool", "tool_call_id": "x", "content": content}


def _realistic_observation(body: str) -> dict:
    """Mirrors agent.tools.http_tool.HttpToolkit.http_request's real result
    shape and key order (body before headers) -- a realistic ~500-char
    header block that would, under the old (headers-first) key order,
    consume the entire compaction budget on its own.
    """
    return json.dumps({
        "status_code": 200,
        "body": body,
        "body_truncated": False,
        "body_original_length": len(body),
        "headers": {
            "Server": "openresty/1.27.1.2",
            "Date": "Mon, 27 Jul 2026 00:47:18 GMT",
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
            "Connection": "keep-alive",
            "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
            "X-Content-Type-Options": "nosniff",
            "X-XSS-Protection": "0",
            "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Strict-Transport-Security": "max-age=31536000 ; includeSubDomains",
            "X-Frame-Options": "DENY",
        },
        "elapsed_ms": 12.2,
        "error": None,
    })


class CompactionTests(unittest.TestCase):
    def test_recent_tool_messages_are_left_untouched(self) -> None:
        long_content = "x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 500)
        messages = [{"role": "system", "content": "sys"}] + [
            _tool_message(long_content) for _ in range(KEEP_FULL_TOOL_MESSAGES)
        ]
        AgentLoop._compact_transcript(messages)
        for m in messages[1:]:
            self.assertEqual(m["content"], long_content)

    def test_older_tool_messages_are_clipped(self) -> None:
        long_content = "x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 500)
        # One more tool message than the keep-full window.
        messages = [_tool_message(long_content) for _ in range(KEEP_FULL_TOOL_MESSAGES + 1)]
        AgentLoop._compact_transcript(messages)
        self.assertLess(len(messages[0]["content"]), len(long_content))
        self.assertIn("compacted", messages[0]["content"])
        # The most recent KEEP_FULL_TOOL_MESSAGES remain untouched.
        for m in messages[1:]:
            self.assertEqual(m["content"], long_content)

    def test_idempotent_on_already_compacted_messages(self) -> None:
        long_content = "x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 500)
        messages = [_tool_message(long_content) for _ in range(KEEP_FULL_TOOL_MESSAGES + 1)]
        AgentLoop._compact_transcript(messages)
        once = messages[0]["content"]
        # Running it again (as the loop does every turn) must not further
        # shrink or corrupt an already-compacted message.
        AgentLoop._compact_transcript(messages)
        AgentLoop._compact_transcript(messages)
        self.assertEqual(messages[0]["content"], once)

    def test_body_survives_compaction_despite_a_realistic_header_block(self) -> None:
        # Regression test for a real correctness bug: with headers listed
        # before body in http_tool.py's result dict, a compacted (aged-out)
        # observation's kept prefix was consumed entirely by ~500 chars of
        # routine response headers, and the body -- where
        # information-disclosure signal actually lives -- was silently
        # dropped in full, not just shortened.
        body = '{"id":8,"name":"Agent Primary","email":"agent.primary@example.com","role":"ROLE_USER"}'
        stale = _tool_message(_realistic_observation(body))
        recent = [_tool_message(_realistic_observation("{}")) for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        messages = [stale] + recent
        AgentLoop._compact_transcript(messages)
        self.assertIn("agent.primary@example.com", messages[0]["content"])

    def test_short_messages_are_never_touched(self) -> None:
        short_content = "short body"
        messages = [_tool_message(short_content) for _ in range(KEEP_FULL_TOOL_MESSAGES + 3)]
        AgentLoop._compact_transcript(messages)
        for m in messages:
            self.assertEqual(m["content"], short_content)

    def test_system_and_user_messages_are_ignored(self) -> None:
        messages = [{"role": "system", "content": "x" * 5000}, {"role": "user", "content": "y" * 5000}] + [
            _tool_message("x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 500)) for _ in range(KEEP_FULL_TOOL_MESSAGES + 1)
        ]
        AgentLoop._compact_transcript(messages)
        self.assertEqual(len(messages[0]["content"]), 5000)
        self.assertEqual(len(messages[1]["content"]), 5000)

    def test_recent_assistant_messages_are_left_untouched(self) -> None:
        long_content = "x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 500)
        messages = [{"role": "assistant", "content": long_content} for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        AgentLoop._compact_transcript(messages)
        for m in messages:
            self.assertEqual(m["content"], long_content)

    def test_older_assistant_messages_are_clipped_but_tool_calls_survive(self) -> None:
        long_content = "x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 500)
        stale = {"role": "assistant", "content": long_content, "tool_calls": [{"id": "1", "type": "function"}]}
        recent = [{"role": "assistant", "content": long_content} for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        messages = [stale] + recent
        AgentLoop._compact_transcript(messages)
        self.assertLess(len(messages[0]["content"]), len(long_content))
        self.assertIn("compacted", messages[0]["content"])
        # tool_calls must survive untouched -- a later turn's tool-role
        # response is matched against it by tool_call_id.
        self.assertEqual(messages[0]["tool_calls"], [{"id": "1", "type": "function"}])
        for m in messages[1:]:
            self.assertEqual(m["content"], long_content)


class DuplicateResponseCollapseTests(unittest.TestCase):
    """Regression tests for the duplicate-response collapse found analyzing a
    real 150-step run: ~15 consecutive `dashboard?id=N` requests all returned
    the identical body, and each aged-out copy used to cost its own
    independent OLD_TOOL_MESSAGE_CLIP_CHARS-sized excerpt.
    """

    def test_repeated_identical_bodies_are_collapsed_to_a_pointer(self) -> None:
        same_body = '{"id":8,"name":"Agent Primary"}'
        stale = [_tool_message(_realistic_observation(same_body)) for _ in range(3)]
        recent = [_tool_message(_realistic_observation("{}")) for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        messages = stale + recent
        AgentLoop._compact_transcript(messages)

        # First occurrence keeps a real excerpt (the body is still readable).
        self.assertIn("Agent Primary", messages[0]["content"])
        # The next two, sharing the same (status_code, body), collapse to a
        # short duplicate pointer instead of their own independent excerpt.
        for m in messages[1:3]:
            self.assertIn("duplicate", m["content"])
            self.assertLess(len(m["content"]), OLD_TOOL_MESSAGE_CLIP_CHARS)

    def test_different_bodies_are_not_merged(self) -> None:
        stale = [
            _tool_message(_realistic_observation('{"id":8}')),
            _tool_message(_realistic_observation('{"id":9}')),
        ]
        recent = [_tool_message(_realistic_observation("{}")) for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        messages = stale + recent
        AgentLoop._compact_transcript(messages)
        # Body is JSON-encoded within JSON, so quotes are backslash-escaped
        # in the outer message content.
        self.assertIn('\\"id\\":8', messages[0]["content"])
        self.assertIn('\\"id\\":9', messages[1]["content"])
        self.assertNotIn("duplicate", messages[0]["content"])
        self.assertNotIn("duplicate", messages[1]["content"])

    def test_duplicate_recognized_even_after_first_occurrence_ages_further(self) -> None:
        # Turn 1: only the first occurrence has aged out yet.
        same_body = '{"id":8,"name":"Agent Primary"}'
        first = _tool_message(_realistic_observation(same_body))
        messages = [first] + [_tool_message(_realistic_observation("{}")) for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        AgentLoop._compact_transcript(messages)
        self.assertIn("Agent Primary", messages[0]["content"])

        # Turn 2: a duplicate of the same body ages out later, after more
        # steps have pushed both out of the full-detail window. The first
        # occurrence's own content is by now already clipped down to an
        # excerpt+sig, not the raw JSON -- detection must still work.
        duplicate = _tool_message(_realistic_observation(same_body))
        messages.insert(1, duplicate)
        AgentLoop._compact_transcript(messages)
        self.assertIn("duplicate", messages[1]["content"])

    def test_non_http_tool_result_falls_back_to_plain_clip(self) -> None:
        # A tool result with no "body"/"status_code" keys (e.g.
        # list_visited_endpoints's own output) has no signature, so it's
        # never treated as a duplicate of anything.
        content = json.dumps({"count": 5, "requests": ["x" * (OLD_TOOL_MESSAGE_CLIP_CHARS + 200)]})
        messages = [_tool_message(content) for _ in range(KEEP_FULL_TOOL_MESSAGES + 2)]
        AgentLoop._compact_transcript(messages)
        self.assertIn("compacted", messages[0]["content"])
        self.assertNotIn("duplicate", messages[0]["content"])

    def test_signature_ignores_shape_without_body_or_status_code(self) -> None:
        self.assertIsNone(_tool_response_signature(json.dumps({"count": 5})))
        self.assertIsNone(_tool_response_signature("not json"))
        self.assertIsNotNone(_tool_response_signature(json.dumps({"status_code": 200, "body": "x"})))


class HeaderStrippingTests(unittest.TestCase):
    """Regression tests for stripping `headers` entirely before an aged-out
    tool message is clipped, rather than letting it silently eat into the
    fixed character budget ahead of the body whenever the body itself is
    short (see the module comment above _strip_headers_for_clip in loop.py).
    """

    def test_headers_are_absent_from_a_clipped_first_occurrence(self) -> None:
        body = "OK"
        stale = _tool_message(_realistic_observation(body))
        recent = [_tool_message(_realistic_observation("{}")) for _ in range(KEEP_FULL_TOOL_MESSAGES)]
        messages = [stale] + recent
        AgentLoop._compact_transcript(messages)
        self.assertNotIn("openresty", messages[0]["content"])
        self.assertNotIn("Strict-Transport-Security", messages[0]["content"])
        self.assertIn('"body": "OK"', messages[0]["content"])

    def test_strip_headers_for_clip_removes_the_headers_key(self) -> None:
        with_headers = json.dumps({"status_code": 200, "body": "x", "headers": {"Server": "openresty"}})
        stripped = _strip_headers_for_clip(with_headers)
        self.assertNotIn("headers", stripped)
        self.assertNotIn("openresty", stripped)
        self.assertIn('"body": "x"', stripped)

    def test_strip_headers_for_clip_is_a_noop_without_a_headers_key(self) -> None:
        no_headers = json.dumps({"count": 5})
        self.assertEqual(_strip_headers_for_clip(no_headers), no_headers)

    def test_strip_headers_for_clip_is_a_noop_on_non_json_content(self) -> None:
        self.assertEqual(_strip_headers_for_clip("not json"), "not json")


class ReplaceSingletonMessageTests(unittest.TestCase):
    def test_first_call_just_appends(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        _replace_singleton_message(messages, "MARK", {"role": "user", "content": "MARK v1"})
        self.assertEqual(messages, [{"role": "system", "content": "sys"}, {"role": "user", "content": "MARK v1"}])

    def test_second_call_replaces_not_stacks(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        _replace_singleton_message(messages, "MARK", {"role": "user", "content": "MARK v1"})
        _replace_singleton_message(messages, "MARK", {"role": "user", "content": "MARK v2, bigger"})
        matching = [m for m in messages if m.get("content", "").startswith("MARK")]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["content"], "MARK v2, bigger")

    def test_unrelated_messages_are_left_alone(self) -> None:
        messages = [{"role": "assistant", "content": "unrelated"}, {"role": "tool", "tool_call_id": "x", "content": "MARK-ish but not user role"}]
        _replace_singleton_message(messages, "MARK", {"role": "user", "content": "MARK v1"})
        self.assertEqual(len(messages), 3)  # tool-role "MARK-ish" message must not be treated as a match

    def test_two_independent_singletons_do_not_corrupt_each_other(self) -> None:
        # Replacing "REMINDER" after "NUDGE" was inserted first must not
        # disturb the NUDGE message's own content -- the original bug risk
        # this guards against is a stale remembered index, not a marker scan.
        messages = [{"role": "system", "content": "sys"}]
        _replace_singleton_message(messages, "NUDGE", {"role": "user", "content": "NUDGE v1"})
        _replace_singleton_message(messages, "REMINDER", {"role": "user", "content": "REMINDER v1"})
        _replace_singleton_message(messages, "NUDGE", {"role": "user", "content": "NUDGE v2"})
        _replace_singleton_message(messages, "REMINDER", {"role": "user", "content": "REMINDER v2, bigger"})
        contents = [m["content"] for m in messages if m["role"] == "user"]
        self.assertEqual(sorted(contents), ["NUDGE v2", "REMINDER v2, bigger"])


if __name__ == "__main__":
    unittest.main()
