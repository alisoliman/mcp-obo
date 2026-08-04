"""Everything this workload reads from its environment, resolved once at startup.

Kustomize sets these from `azure.yaml`, which reads them from Bicep outputs. The
chain is listed in AGENTS.md; adding a variable means touching every link in it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FRONTEND_DIST = ROOT / "frontend" / "dist"


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    client_id: str
    redirect_uri: str
    app_url: str
    mcp_scope: str
    mcp_endpoint: str
    foundry_project_endpoint: str
    foundry_model: str
    token_file: Path

    @classmethod
    def from_env(cls) -> Settings:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required")
            return value

        return cls(
            tenant_id=required("AZURE_TENANT_ID"),
            client_id=required("ENTRA_CLIENT_CLIENT_ID"),
            redirect_uri=required("OAUTH_REDIRECT_URI"),
            app_url=required("APP_URL").rstrip("/"),
            mcp_scope=required("MCP_SCOPE"),
            mcp_endpoint=required("MCP_ENDPOINT"),
            foundry_project_endpoint=required("FOUNDRY_PROJECT_ENDPOINT"),
            foundry_model=required("FOUNDRY_MODEL_DEPLOYMENT_NAME"),
            token_file=Path(
                os.getenv(
                    "AZURE_FEDERATED_TOKEN_FILE",
                    "/var/run/secrets/azure/tokens/azure-identity-token",
                )
            ),
        )
