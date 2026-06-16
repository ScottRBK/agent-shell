from agent_shell.adapters.tool_denial import (
    CANONICAL_TOOLS,
    resolve_disallowed_tools,
)


# A representative native map. `edit` fans out to several native names;
# `web_fetch` is intentionally omitted to exercise the "unsupported" path.
SAMPLE_MAP = {
    "bash": ["Bash"],
    "edit": ["Edit", "Write", "NotebookEdit"],
    "read": ["Read"],
    "web_search": ["WebSearch"],
    # web_fetch intentionally absent
}


class TestCanonicalTools:
    def test_canonical_set_is_the_core_five(self):
        # Arrange / Act / Assert
        assert CANONICAL_TOOLS == {"bash", "edit", "read", "web_search", "web_fetch"}


class TestResolveDisallowedTools:
    def test_returns_empty_for_none(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(None, SAMPLE_MAP)

        # Assert
        assert native == []
        assert unsupported == []

    def test_returns_empty_for_empty_list(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools([], SAMPLE_MAP)

        # Assert
        assert native == []
        assert unsupported == []

    def test_maps_single_canonical_to_native(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(["bash"], SAMPLE_MAP)

        # Assert
        assert native == ["Bash"]
        assert unsupported == []

    def test_read_maps_to_single_native(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(["read"], SAMPLE_MAP)

        # Assert
        assert native == ["Read"]
        assert unsupported == []

    def test_edit_fans_out_to_modify_family(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(["edit"], SAMPLE_MAP)

        # Assert
        assert native == ["Edit", "Write", "NotebookEdit"]
        assert unsupported == []

    def test_unknown_name_passes_through_verbatim(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(["mcp__foo__bar"], SAMPLE_MAP)

        # Assert
        assert native == ["mcp__foo__bar"]
        assert unsupported == []

    def test_canonical_absent_from_map_is_unsupported(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(["web_fetch"], SAMPLE_MAP)

        # Assert
        assert native == []
        assert unsupported == ["web_fetch"]

    def test_dedups_native_preserving_order(self):
        # Arrange / Act
        # `edit` fans out to Edit,Write,NotebookEdit; passing Write verbatim too
        # must not duplicate it, and original order is preserved.
        native, unsupported = resolve_disallowed_tools(["edit", "Write"], SAMPLE_MAP)

        # Assert
        assert native == ["Edit", "Write", "NotebookEdit"]
        assert unsupported == []

    def test_dedups_repeated_canonical(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(["bash", "bash"], SAMPLE_MAP)

        # Assert
        assert native == ["Bash"]
        assert unsupported == []

    def test_mixed_canonical_unknown_and_unsupported(self):
        # Arrange / Act
        native, unsupported = resolve_disallowed_tools(
            ["bash", "web_fetch", "mcp__x__y"], SAMPLE_MAP
        )

        # Assert
        assert native == ["Bash", "mcp__x__y"]
        assert unsupported == ["web_fetch"]

    def test_dedups_unsupported_preserving_order(self):
        # Arrange / Act — repeated unsupported canonical must not duplicate in the warning.
        native, unsupported = resolve_disallowed_tools(
            ["web_fetch", "web_fetch"], SAMPLE_MAP
        )

        # Assert
        assert native == []
        assert unsupported == ["web_fetch"]
