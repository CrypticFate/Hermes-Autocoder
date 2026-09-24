"""Dependency-light wire types shared with the independently installed Hermes runtime."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskContext(BaseModel):
    schema_version: Literal[1] = 1
    task_id: str
    attempt_id: str
    mode: Literal["plan", "implement", "review"]
    prompt: str
    base_sha: str
    model: str
    max_iterations: int
    max_output_tokens: int
    timeout_seconds: int
    kind: Literal["implementation", "plan_draft"] = "implementation"
    plan_path: str = ""
    report_path: str = ""
    title: str = ""
    objective: str = ""
    criteria: list[str] = Field(default_factory=list)
    context: str = ""
    services: list[str] = Field(default_factory=list)
    extra_checks: list[str] = Field(default_factory=list)
    service_env_names: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    branch: str = ""
    repair: dict | None = None
    operator_login: str = ""


class Proposal(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9-]{1,60}$")
    title: str = Field(min_length=3, max_length=180)
    objective: str = Field(min_length=10, max_length=12000)
    evidence: str = Field(min_length=10, max_length=12000)
    acceptance: list[str] = Field(min_length=1, max_length=10)
    dependencies: list[str] = Field(default_factory=list)
    category: Literal["bug", "test", "documentation", "ci", "user"]


class PlanResult(BaseModel):
    tasks: list[Proposal] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def check_graph(self):
        mapping = {t.key: t for t in self.tasks}
        if len(mapping) != len(self.tasks):
            raise ValueError("Duplicate task keys")
        done, visiting = set(), set()

        def visit(key):
            if key not in mapping or key in visiting:
                raise ValueError("Missing dependency or cycle")
            if key in done:
                return
            visiting.add(key)
            for dependency in mapping[key].dependencies:
                visit(dependency)
            visiting.remove(key)
            done.add(key)
        for key in mapping:
            visit(key)
        return self


class ReviewResult(BaseModel):
    accepted: bool
    acceptance_met: list[bool]
    objections: list[str] = Field(default_factory=list)


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    status: Literal["met", "not_met", "partial"]
    evidence: str


class FileRationale(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    why: str


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    completed: bool = False
    summary: str = Field(default="", max_length=2000)
    plan: PlanResult | None = None
    review: ReviewResult | None = None
    criteria: list[Criterion] = Field(default_factory=list)
    files_changed_rationale: list[FileRationale] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)
    deviations: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    def validate_criteria(self, expected):
        if [c.criterion for c in self.criteria] != expected:
            raise ValueError("Result criteria must match the plan 1:1 in order")
        return self
