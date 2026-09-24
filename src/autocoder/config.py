import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autocoder.redaction import register_secret

Login = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def pinned_image(value: str) -> str:
    if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", value):
        raise ValueError("Image must be pinned by sha256 digest or image ID (64 lowercase hex digits)")
    return value


class RulesetConfig(ConfigModel):
    # 0 is accepted only in operator_pat mode: GitHub never lets you approve your own pull request.
    min_approvals: int = Field(default=1, ge=0, le=6)
    require_last_push_approval: bool = True
    block_force_push: bool = True
    block_deletion: bool = True


class GitHubConfig(ConfigModel):
    # bot_pat (default, recommended): a separate write-only bot account. app: GitHub App tokens.
    # operator_pat: the operator's own token; opt-in with weaker separation (docs/DEPLOYMENT.md).
    mode: Literal["bot_pat", "app", "operator_pat"] = "bot_pat"
    bot_login: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?$")
    token_secret: Path = Path("/run/secrets/github_bot")
    commit_name: str = Field(default="Hermes Autocoder", min_length=1)
    commit_email: str = Field(min_length=1)
    delete_merged_branches: bool = True
    ruleset_recheck_seconds: int = Field(default=600, ge=1)
    require_ruleset: RulesetConfig = Field(default_factory=RulesetConfig)
    app_id: int | None = Field(default=None, gt=0)
    installation_id: int | None = Field(default=None, gt=0)
    private_key_secret: Path | None = None

    @model_validator(mode="after")
    def validate_identity(self):
        if any(c in self.commit_name + self.commit_email for c in "\r\n\x00"):
            raise ValueError("Commit identity cannot contain control characters")
        if "@" not in self.commit_email:
            raise ValueError("github.commit_email must be an email address")
        if self.mode == "app" and not all((self.app_id, self.installation_id, self.private_key_secret)):
            raise ValueError("App mode requires app_id, installation_id and private_key_secret")
        if self.mode != "operator_pat" and self.require_ruleset.min_approvals < 1:
            raise ValueError("require_ruleset.min_approvals may be 0 only in operator_pat mode")
        return self


class RepositoryConfig(ConfigModel):
    auto_enroll: bool = False


class SchedulerConfig(ConfigModel):
    tick_seconds: int = Field(default=15, ge=1)
    pr_poll_seconds: int = Field(default=60, ge=1)
    feedback_debounce_seconds: int = Field(default=180, ge=0)
    sequential: bool = True
    max_attempts_per_task: int = Field(default=3, ge=1, le=10)
    max_repairs_per_task: int = Field(default=5, ge=0, le=100)

    @field_validator("sequential", mode="before")
    @classmethod
    def require_sequential(cls, value):
        if value is not True:
            raise ValueError("parallel plans are not supported in v2")
        return value


class ModelConfig(ConfigModel):
    provider_url: str = "https://openrouter.ai/api/v1"
    model: str = Field(default="nvidia/nemotron-3-super-120b-a12b:free", min_length=1)
    input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)
    output_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)
    max_output_tokens: int = Field(default=8192, ge=128, le=32768)

    @field_validator("provider_url")
    @classmethod
    def require_https(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("model.provider_url must use HTTPS without embedded credentials")
        return value


class BuilderPool(ConfigModel):
    requests_per_day: int = Field(default=500, ge=1)
    requests_per_attempt: int = Field(default=60, ge=1, le=1000)


class ConciergePool(ConfigModel):
    requests_per_day: int = Field(default=300, ge=1)


class Pools(ConfigModel):
    builder: BuilderPool = Field(default_factory=BuilderPool)
    concierge: ConciergePool = Field(default_factory=ConciergePool)


class BudgetConfig(ConfigModel):
    daily_usd: Decimal = Field(default=Decimal("5"), gt=0, allow_inf_nan=False)
    monthly_usd: Decimal = Field(default=Decimal("20"), gt=0, allow_inf_nan=False)
    pools: Pools = Field(default_factory=Pools)


class BuilderConfig(ConfigModel):
    image: str
    runtime: Literal["runc", "runsc"] = "runc"
    cpus: float = Field(default=2, gt=0, allow_inf_nan=False)
    memory: str = "3g"
    pids: int = Field(default=512, ge=1)
    deadline_seconds: int = Field(default=1800, ge=10, le=86400)
    hermes_max_iterations: int = Field(default=40, ge=1, le=200)
    setup_egress: bool = True
    _pin = field_validator("image")(pinned_image)


class ServiceConfig(ConfigModel):
    image: str
    cpus: float = Field(default=1, gt=0, allow_inf_nan=False)
    memory: str = "1g"
    tmpfs: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    healthcheck: list[str] = Field(min_length=1)
    expose_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("image")
    @classmethod
    def require_digest(cls, value):
        if "@sha256:" not in value:
            raise ValueError("Service image must contain @sha256:")
        return pinned_image(value)


class Profile(ConfigModel):
    setup: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    command_seconds: int = Field(default=600, ge=1, le=3600)


class MCPConfig(ConfigModel):
    listen: str = "0.0.0.0:8765"
    token_secret: Path = Path("/run/secrets/mcp_concierge_token")
    rate_limit_per_minute: int = Field(default=60, ge=1)


class ConciergeConfig(ConfigModel):
    model_token_secret: Path = Path("/run/secrets/concierge_model_token")
    memory_user_id: str = Field(default="operator", min_length=1)


class Settings(ConfigModel):
    owners: list[Login] = Field(min_length=1)
    operator_login: Login
    github: GitHubConfig
    repositories: RepositoryConfig = Field(default_factory=RepositoryConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    builder: BuilderConfig
    services_allowlist: dict[str, ServiceConfig] = Field(default_factory=dict)
    profiles: dict[str, Profile] = Field(default_factory=dict)
    repository_profiles: dict[str, str] = Field(default_factory=dict)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    concierge: ConciergeConfig = Field(default_factory=ConciergeConfig)
    data_dir: Path = Path("/data")

    # Deployment controls retained while subsequent phases migrate the v1 runtime.
    database_url: str = "postgresql+psycopg://autocoder@database/autocoder"
    database_password_file: Path | None = None
    provider_key_file: Path = Path("/run/secrets/model_provider")
    excluded_repositories: list[str] = Field(default_factory=list)
    self_repository: str | None = None
    include_forks: bool = False
    pilot_repository: str | None = None
    proxy_url: str = "http://model-proxy:8080/v1"
    worker_network: str = "hermes-workers"
    worker_uid: int = Field(default=1000, ge=1)
    worker_gid: int = Field(default=1000, ge=1)
    worker_tmp_bytes: int = Field(default=536870912, ge=10485760)
    max_open_prs: int = Field(default=1, ge=1, le=20)
    max_workspace_bytes: int = Field(default=2147483648, ge=10485760)
    minimum_free_bytes: int = Field(default=5368709120, ge=0)
    lease_seconds: int = Field(default=600, ge=30)
    discovery_seconds: int = Field(default=3600, ge=60)
    scan_seconds: int = Field(default=86400, ge=60)
    log_retention_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def validate_config(self):
        same = self.operator_login.lower() == self.github.bot_login.lower()
        if self.github.mode == "operator_pat" and not same:
            raise ValueError("In operator_pat mode github.bot_login must equal operator_login (the token owner)")
        if self.github.mode != "operator_pat" and same:
            raise ValueError("operator_login and github.bot_login must differ")
        missing = set(self.repository_profiles.values()) - self.profiles.keys()
        if missing:
            raise ValueError(f"Undefined runtime profiles: {sorted(missing)}")
        return self

    def paid_errors(self) -> list[str]:
        return []  # Pricing is validated at configuration load, including free models.


def load_settings(path: Path = Path("config.yaml")) -> Settings:
    settings = Settings.model_validate(yaml.safe_load(path.read_text()) or {})
    settings.data_dir = settings.data_dir.resolve()
    return settings


def secret(path: Path, *, multiline=False) -> str:
    value = path.read_text().strip()
    if not value or ("\n" in value and not multiline) or value.startswith(("<", "PLACEHOLDER")):
        raise ValueError(f"Missing or placeholder secret: {path.name}")
    register_secret(value)
    return value
