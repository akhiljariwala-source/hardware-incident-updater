"""Config loading: env vars + YAML files under config/.

Stubbed at Step 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_DIR = REPO_ROOT / "docs"
FIXTURES_DIR = REPO_ROOT / "fixtures"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    slack_bot_token: str
    notion_api_key: str
    notion_rubric_page_id: str
    instatus_api_key: str
    instatus_page_id: str
    post_to_channel_id: str
    github_run_id: str | None
    github_repository: str | None


def load_settings() -> Settings:
    raise NotImplementedError("load_settings will be implemented in Step 8.")
