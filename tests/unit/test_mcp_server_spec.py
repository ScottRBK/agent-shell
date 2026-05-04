import pytest

from agent_shell.models.agent import MCPServerSpec, MCPServerType


class TestStdioValidation:
    def test_valid_stdio_spec_constructs(self):
        # Arrange / Act
        spec = MCPServerSpec(
            name="forgetful",
            type=MCPServerType.STDIO,
            command="uvx",
            args=["forgetful-ai"],
            env={"FORGETFUL_API_KEY": "x"},
        )

        # Assert
        assert spec.name == "forgetful"
        assert spec.command == "uvx"

    def test_stdio_without_command_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="STDIO.*command"):
            MCPServerSpec(name="x", type=MCPServerType.STDIO)

    def test_stdio_with_url_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="STDIO.*url"):
            MCPServerSpec(
                name="x",
                type=MCPServerType.STDIO,
                command="uvx",
                url="https://example.com",
            )

    def test_stdio_with_headers_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="STDIO.*headers"):
            MCPServerSpec(
                name="x",
                type=MCPServerType.STDIO,
                command="uvx",
                headers={"Authorization": "Bearer x"},
            )


class TestHttpValidation:
    def test_valid_http_spec_constructs(self):
        # Arrange / Act
        spec = MCPServerSpec(
            name="remote",
            type=MCPServerType.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer x"},
        )

        # Assert
        assert spec.url == "https://example.com/mcp"

    def test_http_without_url_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="HTTP.*url"):
            MCPServerSpec(name="x", type=MCPServerType.HTTP)

    def test_http_with_command_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="HTTP.*command"):
            MCPServerSpec(
                name="x",
                type=MCPServerType.HTTP,
                url="https://example.com",
                command="uvx",
            )

    def test_http_with_args_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="HTTP.*args"):
            MCPServerSpec(
                name="x",
                type=MCPServerType.HTTP,
                url="https://example.com",
                args=["foo"],
            )

    def test_http_with_env_raises(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="HTTP.*env"):
            MCPServerSpec(
                name="x",
                type=MCPServerType.HTTP,
                url="https://example.com",
                env={"KEY": "value"},
            )
