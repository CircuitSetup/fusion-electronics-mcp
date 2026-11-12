"""Limits and diagnostics for deterministic offline routing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from time import monotonic_ns


# The full A* pass keeps two eight-byte arrays per state (at most 128 MB).
MAX_SEARCH_STATES = 8_000_000
ROUTING_BUDGET_MAXIMUMS = {
    "max_nodes_per_attempt": 1_000_000,
    "max_nodes_per_connection": 2_000_000,
    "max_attempts_per_connection": 8,
    "max_seconds_per_attempt": 60,
    "max_seconds_per_job": 1800,
    "max_vias_per_connection": 8,
    "ripup_rounds": 8,
}


@dataclass(frozen=True)
class RoutingBudget:
    max_nodes_per_attempt: int = 200_000
    max_nodes_per_connection: int = 300_000
    max_attempts_per_connection: int = 2
    max_seconds_per_attempt: float = 15
    max_seconds_per_job: float = 600
    max_vias_per_connection: int = 2
    ripup_rounds: int = 2

    def __post_init__(self):
        for name, ceiling in ROUTING_BUDGET_MAXIMUMS.items():
            value = getattr(self, name)
            types = (int, float) if name.startswith("max_seconds") else (int,)
            if type(value) not in types or not 0 < value <= ceiling:
                raise ValueError(f"{name} must be positive and at most {ceiling}")


ROUTING_BUDGET_DEFAULTS = asdict(RoutingBudget())


@dataclass(frozen=True)
class SearchDiagnostics:
    termination: str
    attempts: int
    expanded_nodes: int
    elapsed_ms: int
    layers_attempted: tuple[int, ...]
    via_limit: int
    vias_used: int | None
    candidate_blockers: tuple[dict, ...]
    closest_distance_mm: float | None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["layers_attempted"] = list(data["layers_attempted"])
        data["candidate_blockers"] = list(data["candidate_blockers"])
        return data


class BudgetStop(Exception):
    def __init__(self, termination: str, message: str):
        super().__init__(message)
        self.termination = termination


@dataclass
class RoutingJob:
    budget: RoutingBudget
    clock: Callable[[], int] = monotonic_ns
    deadline_ns: int = field(init=False)

    def __post_init__(self):
        self.deadline_ns = self.clock() + int(self.budget.max_seconds_per_job * 1e9)

    def check(self, now: int):
        if now >= self.deadline_ns:
            raise BudgetStop("job_deadline", "Routing job deadline reached")


@dataclass
class SearchProgress:
    job: RoutingJob
    via_limit: int
    attempts: int = 0
    expanded_nodes: int = 0
    elapsed_ns: int = 0
    layers: tuple[int, ...] = ()
    blockers: tuple[dict, ...] = ()
    closest_squared: int | None = None
    step: float = 0.0
    via_pruned: bool = False
    started_ns: int | None = None
    attempt_node_end: int = 0

    @property
    def can_attempt(self) -> bool:
        budget = self.job.budget
        return (self.attempts < budget.max_attempts_per_connection
                and self.expanded_nodes < budget.max_nodes_per_connection)

    def begin(self, max_nodes: int):
        now = self.job.clock()
        self.job.check(now)
        if not self.can_attempt:
            if self.expanded_nodes >= self.job.budget.max_nodes_per_connection:
                raise BudgetStop("node_limit", "Routing connection node limit reached")
            raise BudgetStop("attempt_limit", "Routing connection attempt limit reached")
        self.started_ns = now
        self.attempts += 1
        self.via_pruned = False
        self.attempt_node_end = min(self.job.budget.max_nodes_per_connection,
                                    self.expanded_nodes + min(max_nodes, self.job.budget.max_nodes_per_attempt))

    def checkpoint(self):
        now = self.job.clock()
        self.job.check(now)
        if self.started_ns is not None and now - self.started_ns >= self.job.budget.max_seconds_per_attempt * 1e9:
            raise BudgetStop("time_limit", "Routing attempt deadline reached")

    def finish(self, termination: str, vias_used: int | None = None) -> SearchDiagnostics:
        if self.started_ns is not None:
            self.elapsed_ns += self.job.clock() - self.started_ns
            self.started_ns = None
        distance = None if self.closest_squared is None else round(self.closest_squared ** .5 * self.step, 4)
        return SearchDiagnostics(termination, self.attempts, self.expanded_nodes, self.elapsed_ns // 1_000_000,
                                 self.layers, self.via_limit, vias_used, self.blockers, distance)
