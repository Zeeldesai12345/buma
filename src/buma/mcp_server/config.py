from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    """
    The ONLY configuration the MCP server reads (N1 / DD-26).

    Deliberately not buma.core.config.Settings: that class requires the webhook secret and would
    load the GitHub App key and Anthropic key into this process. The MCP server needs a database
    URL and nothing else. Fields not declared here are ignored even if present in .env.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Preferred: a URL reachable from where the MCP client launches the server (usually your
    # machine, e.g. localhost:5433 for the Docker db). .env's DATABASE_URL points at the Docker
    # hostname "db", which only resolves inside the compose network.
    buma_mcp_database_url: str | None = None
    database_url: str | None = None

    def resolved_database_url(self) -> str:
        url = self.buma_mcp_database_url or self.database_url
        if not url:
            raise ValueError("Set BUMA_MCP_DATABASE_URL (or DATABASE_URL) for the Buma MCP server.")
        return url
