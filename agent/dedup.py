"""Bonus: semantic de-duplication of near-identical findings (the optional/bonus features, bonus bullet 3).

The discovery loop can plausibly re-find "the same" disclosure more than
once -- e.g. an excessive-data-exposure bug on a per-resource endpoint
(``GET /workshop/api/mechanic/reports/<id-1>`` and ``.../<id-2>``) looks
like two different ``endpoint`` strings but is one underlying issue. Left
alone, this would inflate the findings count and hurt precision (the evaluation metrics) without adding real signal.

Two-stage matching, both deliberately simple/deterministic (no LLM call --
consistent with keeping the *judgment* of what counts as a duplicate
auditable, matching the reasoning already used in ``agent.scope``):

1. **Structural**: normalize path segments that look like IDs (UUIDs,
   numeric IDs, long opaque tokens) to a placeholder, so
   ``/workshop/api/mechanic/reports/42`` and ``.../99`` collapse to the same
   pattern. Findings sharing a ``(method, normalized_path)`` are candidates.
2. **Textual**: within a structural group, merge findings whose
   ``why_disclosure`` text *or* ``evidence`` text is similar (``difflib``
   ratio above a threshold) -- two different bugs can legitimately live on
   the same endpoint pattern (e.g. both a leaked internal field *and* a
   leaked other user's email on the same list endpoint), and those must NOT
   be merged away. Both fields are checked (not ``why_disclosure`` alone)
   because a live run surfaced the real gap: the same disclosed data
   (a byte-for-byte identical ``.env`` credential dump, ``evidence``
   similarity ~0.95) proposed twice with differently-worded commentary
   (``why_disclosure`` similarity ~0.10) is one underlying finding, not two
   -- ``why_disclosure`` is free-text framing of *why it matters*, not a
   reliable signal for *is this the same disclosed data*. Genuinely distinct
   bugs on a shared endpoint pattern still have distinct evidence excerpts
   (different leaked fields), so checking evidence doesn't reintroduce the
   over-merging this design already guards against. A *third* signal
   (``_same_disclosure_by_fields``) merges two findings whose evidence
   exposes a near-identical set of JSON field *names* even when neither text
   ratio clears the threshold -- see that constant's comment for the two live
   duplicate shapes it closes (length-asymmetric quotes of one endpoint; one
   BOLA shown against two different objects). It keeps the same "different
   leaked fields stay distinct" guarantee, just measured on the key set
   rather than on raw text that value-level differences can drag below
   threshold.

When findings are merged, the kept representative is the one with the
highest confidence (ties broken by the longer/more detailed reproduction
list), but its ``reproduction`` steps are extended with any distinct
reproduction steps from the merged-away duplicates, so no reproduction
detail is silently lost.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .schemas import Finding

_ID_SEGMENT_RE = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$"
)

# Opaque non-hex identifiers: nanoid/base62 tokens like crAPI's community-post
# IDs (``VguigU9DTrSwxcWFL4m6XN``). The hex-only rule above never matched these
# -- so three findings on ``/community/.../posts/<nanoid>`` landed in three
# separate structural buckets and were never even compared for merging (a real
# gap seen on a live run: one post-detail disclosure reported three times, once
# per sampled post). A path segment is treated as an opaque ID when it is a
# 16+-char run of letters/digits that contains at least one digit: no English
# resource-name segment (``dashboard``, ``vehicles``, ``location``,
# ``notifications``) is both that long and digit-bearing, while nanoids (default
# length 21) effectively always are. The 16-char floor and the mandatory digit
# are what keep ordinary words -- however long -- out; this only ever collapses
# *more* ID-shaped segments, never a real resource name.
_OPAQUE_ID_SEGMENT_RE = re.compile(r"^(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{16,}$")


def _segment_is_id(segment: str) -> bool:
    return bool(_ID_SEGMENT_RE.match(segment) or _OPAQUE_ID_SEGMENT_RE.match(segment))

_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

_SIMILARITY_THRESHOLD = 0.75

# Field-name-set overlap: a third merge signal for the specific case a live
# run exposed where neither why_disclosure nor evidence *text* similarity
# crosses the threshold, yet the two findings are plainly one disclosure:
#   * the same concrete endpoint quoted with length-asymmetric evidence
#     excerpts (one a short prose-padded snippet, one the raw JSON prefix) --
#     SequenceMatcher's length-normalized ratio sinks well below threshold
#     even when the shorter excerpt is almost wholly contained in the longer
#     (observed: evidence_sim 0.54, text containment 0.74, both under 0.75);
#   * a BOLA on a per-object endpoint demonstrated against two different
#     objects (e.g. two users' /vehicle/{id}/location) -- identical response
#     *shape*, but every leaked *value* (UUID, coords, name, email) differs,
#     so the evidence text diverges though the bug is one (observed: 0.72).
# Both cases share the same tell: the set of JSON field names in the two
# evidence excerpts is (near-)identical. That is exactly the discriminator
# the structural/textual design already reasons in terms of -- "two different
# bugs ... both a leaked internal field *and* a leaked other user's email ...
# must NOT be merged" -- made explicit: *different* leaked fields => distinct;
# *same* leaked fields => same disclosure (the two real pairs above score a
# Jaccard of 1.0 on their key sets; a synthetic distinct-fields counterexample
# on a shared endpoint pattern scores 0.29, well below threshold). Keys are
# extracted with a tolerant ``"key":`` regex rather than json.loads because
# real evidence is frequently truncated mid-record or padded with trailing
# prose. Gated on a high overlap ratio AND a minimum shared-key count so a
# tiny/degenerate body cannot trivially match -- this signal only ever *adds*
# merges, never removes one.
#
# Two robustness details, both from the same later live run that first showed
# the nanoid bucketing gap above:
#   * The quote-matching is tolerant of a leading/trailing backslash
#     (``\"key\":`` as well as ``"key":``) because the model sometimes
#     double-escapes embedded quotes while building its tool-call JSON -- the
#     exact artifact fix #16 handles in ``validation.py``. Without this, one of
#     three otherwise-identical post-detail findings extracted *zero* keys and
#     stayed un-merged purely over escaping.
#   * A non-JSON ``KEY=value`` config dump (the ``.env`` leak) exposes its data
#     shape as env-var *names*, not JSON keys. Capturing those too lets the same
#     "same key set => same disclosure" logic merge a raw ``.env`` dump against a
#     prose-wrapped quote of the same dump (observed: text sim 0.34, but 9 of 11
#     env-var names shared, Jaccard 0.82) -- the case fix #35's evidence-text
#     similarity used to rely on, now robust to prose padding too. Literal
#     ``\n``/``\r``/``\t`` escape sequences are collapsed to whitespace first
#     (again mirroring fix #16) so env vars separated by an escaped newline are
#     still seen as separate keys.
_ESCAPE_RE = re.compile(r"\\[nrt]")
_JSON_KEY_RE = re.compile(r'\\?"([A-Za-z_][A-Za-z0-9_]*)\\?"\s*:')
_ENV_KEY_RE = re.compile(r"(?:^|[\s,;{])([A-Z][A-Z0-9_]{2,})=")
_FIELD_OVERLAP_THRESHOLD = 0.7
_MIN_SHARED_FIELDS = 3


def _evidence_field_names(evidence: str) -> set[str]:
    normalized = _ESCAPE_RE.sub(" ", evidence)
    json_keys = {match.lower() for match in _JSON_KEY_RE.findall(normalized)}
    env_keys = {match.lower() for match in _ENV_KEY_RE.findall(normalized)}
    return json_keys | env_keys


def _same_disclosure_by_fields(a: str, b: str) -> bool:
    """True when two evidence excerpts expose a (near-)identical set of data
    keys -- JSON field names and/or ``KEY=`` config-var names -- i.e. the same
    disclosed data shape, regardless of the concrete values or of how the
    surrounding prose is worded."""
    keys_a, keys_b = _evidence_field_names(a), _evidence_field_names(b)
    shared = keys_a & keys_b
    if len(shared) < _MIN_SHARED_FIELDS:
        return False
    return len(shared) / len(keys_a | keys_b) >= _FIELD_OVERLAP_THRESHOLD


def normalize_endpoint_pattern(endpoint: str) -> str:
    """'GET /workshop/api/mechanic/reports/42' -> 'GET /workshop/api/mechanic/reports/{id}'."""
    try:
        method, path = endpoint.split(" ", 1)
    except ValueError:
        return endpoint
    segments = path.split("/")
    normalized = [("{id}" if _segment_is_id(seg) else seg) for seg in segments]
    return f"{method} {'/'.join(normalized)}"


# A path segment that is an explicit route placeholder rather than a concrete
# value: ``{id}``/``{userId}`` (OpenAPI/Spring), ``<id>`` (Flask), ``:id``
# (Express). A finding's claimed ``endpoint`` typically carries one of these
# where the real request carried a concrete value, so both must collapse to the
# same wildcard for an endpoint-identity comparison to line up.
_PLACEHOLDER_ENDPOINT_SEG_RE = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|:[\w-]+)$")


def endpoint_grounding_key(method: str, path: str) -> tuple[str, tuple[str, ...]]:
    """A comparable key answering "do this request and that claimed endpoint
    refer to the same API endpoint?", ignoring concrete id values, route
    placeholders, query strings, and casing.

    Used by ``agent.validation`` to bind a finding's evidence to the response
    of a request that actually matches the finding's *claimed* ``endpoint`` --
    not merely to any response captured this run (fix #43). Reuses this
    module's ``_segment_is_id`` so opaque/nanoid ids collapse the same way
    de-duplication already collapses them (``/posts/<nanoid>`` vs
    ``/posts/{postId}``), which ``http_tool``'s own lighter ``_ID_VALUE_RE``
    would miss.
    """
    bare = path.split("?", 1)[0]
    segments = tuple(
        "{id}" if (_PLACEHOLDER_ENDPOINT_SEG_RE.match(seg) or _segment_is_id(seg)) else seg.lower()
        for seg in bare.strip("/").split("/")
        if seg
    )
    return (method.strip().upper(), segments)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _cluster_by_similarity(bucket: list[Finding]) -> list[list[Finding]]:
    """Group a structural bucket into connected components under the
    "why_disclosure OR evidence text similarity >= threshold, OR identical
    evidence field-name set" relation.

    This is proper single-linkage clustering via union-find, not sequential
    online clustering against a single representative: comparing each new
    finding only against one existing member (whether that's a cluster's
    first member, or even *any* one already-placed member) made the result
    depend on arbitrary discovery order for a genuine transitive chain --
    e.g. A~B and B~C similar but A~C not, discovered in an order where the
    bridging item (B) is processed after A and C have already become
    separate clusters, would never retroactively merge them. Computing
    connected components over the full pairwise-similarity graph is what
    actually guarantees A, B, and C end up in one group regardless of which
    order they were discovered in.
    """
    n = len(bucket)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            reasoning_sim = _similarity(bucket[i].why_disclosure, bucket[j].why_disclosure)
            evidence_sim = _similarity(bucket[i].evidence, bucket[j].evidence)
            if (
                max(reasoning_sim, evidence_sim) >= _SIMILARITY_THRESHOLD
                or _same_disclosure_by_fields(bucket[i].evidence, bucket[j].evidence)
            ):
                root_i, root_j = find(i), find(j)
                if root_i != root_j:
                    parent[root_i] = root_j

    groups: dict[int, list[Finding]] = {}
    for i, finding in enumerate(bucket):
        groups.setdefault(find(i), []).append(finding)
    return list(groups.values())


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Merge near-duplicate findings. Order-preserving for the first
    occurrence of each surviving group.
    """
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        pattern = normalize_endpoint_pattern(finding.endpoint)
        groups.setdefault(pattern, []).append(finding)

    result: list[Finding] = []
    for bucket in groups.values():
        clusters = _cluster_by_similarity(bucket)

        for cluster in clusters:
            # max() keeps the first element on a tie, matching the old
            # left-to-right reduce's tie-breaking behavior exactly.
            representative = max(cluster, key=lambda f: (_CONFIDENCE_RANK[f.confidence], len(f.reproduction)))
            # Fold in any reproduction steps not already present, from every
            # member of the cluster, so merging never loses detail.
            merged_repro = list(representative.reproduction)
            for member in cluster:
                for step in member.reproduction:
                    if step not in merged_repro:
                        merged_repro.append(step)
            updates: dict[str, object] = {}
            if merged_repro != representative.reproduction:
                updates["reproduction"] = merged_repro
            # on_challenge_list disagreement within one merged cluster: keep
            # off-list. The members describe one underlying disclosure, so a
            # split vote means the keyword match that flagged it on-list was
            # not robust (here: one of two .env proposals incidentally said
            # "PII", a load-bearing challenge-4 keyword, so the classifier
            # legitimately tagged just that one on-list). agent.challenge_
            # reference's own docstring states the asymmetry this follows: a
            # false positive (an off-list generalization win mislabeled as a
            # known challenge) is the worse error, while a false negative is
            # harmless bookkeeping -- so on a tie we take the harmless side.
            if representative.on_challenge_list and any(not m.on_challenge_list for m in cluster):
                updates["on_challenge_list"] = False
            if updates:
                representative = representative.model_copy(update=updates)
            result.append(representative)

    return result
