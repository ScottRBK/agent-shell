"""Canonical tool-name vocabulary for disallowed_tools and per-adapter translation.

`agent_shell` owns a small canonical deny vocabulary so callers do not need to know
each CLI's tool names. Each adapter provides a `dict[canonical -> list[native]]` and
calls `resolve_disallowed_tools` to translate a caller's deny-list into that adapter's
native tool identifiers (and to learn which canonical names it cannot enforce).
"""

# Security-relevant core. `write`/`edit`/`patch` are intentionally one concept: "edit".
CANONICAL_TOOLS = frozenset({"bash", "edit", "read", "web_search", "web_fetch"})


def resolve_disallowed_tools(
    disallowed_tools: list[str] | None,
    native_map: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    """Translate canonical deny-names into one adapter's native tool identifiers.

    Returns (native, unsupported):
      - native:      deduped native identifiers to deny on this adapter. Canonical
                     names are mapped via native_map (and may fan out to several
                     native names); names outside CANONICAL_TOOLS pass through verbatim.
      - unsupported: canonical names this adapter cannot deny (native_map omits them);
                     the caller should warnings.warn(...) about these.
    """
    if not disallowed_tools:
        return [], []

    native: list[str] = []
    unsupported: list[str] = []
    for name in disallowed_tools:
        if name in CANONICAL_TOOLS:
            mapped = native_map.get(name)
            if mapped is None:
                unsupported.append(name)
            else:
                native.extend(mapped)
        else:
            native.append(name)  # passthrough, verbatim

    return _dedup(native), _dedup(unsupported)


def _dedup(items: list[str]) -> list[str]:
    """Drop duplicates while preserving first-seen order."""
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]
