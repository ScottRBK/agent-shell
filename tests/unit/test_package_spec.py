import pytest

from agent_shell.models.agent import PackageSpec


@pytest.mark.parametrize("source", ["", "   ", "npm:tools\x00", None, 123])
def test_package_source_must_be_nonempty_text(source):
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="source"):
        PackageSpec(source=source)
