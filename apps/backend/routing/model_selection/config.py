"""The ``auto:`` section of ``models.yaml``: tiers, their candidates, session settings.

::

    auto:
      default_profile: balance        # the tier bare ``auto`` means
      session:
        idle_timeout_seconds: 1800
        max_sessions: 10000
      profiles:
        intelligence:
          candidates:
            - {model: frontier-a, reasoning_effort: high}
            - {model: mid-b, reasoning_effort: high}
        balance:
          candidates:
            - {model: mid-b}
            - {model: frontier-a}

Each tier is served as the virtual model ``auto-<name>``. Its candidates are
tried in the order listed. The section sits beside ``models:`` and every other
reader of the file reads only ``models:``.

Checking is in two steps, because they fail differently:

- :func:`parse_auto_config` checks the section on its own. Any problem there is
  a mistake in the file, and the caller turns auto off and reports every problem
  at once; the models themselves are unaffected.
- :func:`check_against_catalog` holds the section against the models actually
  registered. A tier named like an existing model would hide it, so that turns
  auto off too. A candidate the catalog cannot serve as written -- unregistered,
  or at an effort its model does not take -- is only a warning: the route table
  changes after startup (a model whose setting is unset is skipped at boot, and
  an admin can add one at runtime), and selection leaves such a candidate out of
  each request until it can serve.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pydantic
from pydantic import BaseModel, Field

from routing.model_selection.scoring import MaxQuality, StaticScorer
from routing.model_selection.types import Candidate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from routing.model_selection.scoring import Objective, Scorer
    from routing.model_selection.types import CandidateInfo

__all__ = [
    "AUTO_MODEL_ID",
    "AUTO_MODEL_PREFIX",
    "AutoConfig",
    "AutoConfigError",
    "AutoProfile",
    "CatalogCheck",
    "check_against_catalog",
    "is_auto_model_id",
    "parse_auto_config",
]

#: The virtual model that means the default tier.
AUTO_MODEL_ID = "auto"
#: Prefix of every tier's virtual model: ``auto-<tier>``.
AUTO_MODEL_PREFIX = "auto-"


def is_auto_model_id(model_id: str) -> bool:
    """Return whether ``model_id`` is in the namespace auto models are served from.

    That is ``auto`` and every ``auto-<name>``, whether or not a tier of that name
    is configured: tiers are added by editing the section, and a model registered
    under such a name in the meantime would be hidden by the tier.
    """
    return model_id == AUTO_MODEL_ID or model_id.startswith(AUTO_MODEL_PREFIX)


_PROFILE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class _CandidateSpec(BaseModel):
    model_config = {"extra": "forbid"}

    model: str = Field(min_length=1)
    reasoning_effort: str | None = Field(default=None, min_length=1)


class _ProfileSpec(BaseModel):
    model_config = {"extra": "forbid"}

    candidates: list[_CandidateSpec] = Field(min_length=1)


class _SessionSpec(BaseModel):
    model_config = {"extra": "forbid"}

    idle_timeout_seconds: float = Field(default=1800.0, gt=0)
    max_sessions: int = Field(default=10_000, gt=0)


class _AutoSpec(BaseModel):
    model_config = {"extra": "forbid"}

    default_profile: str = "balance"
    session: _SessionSpec = Field(default_factory=_SessionSpec)
    profiles: dict[str, _ProfileSpec] = Field(min_length=1)


@dataclass(frozen=True, eq=False)
class AutoProfile:
    """One tier: its candidates, and how they are scored and ranked.

    Attributes:
        name: The tier's name; it is served as ``auto-<name>``.
        candidates: Its candidates, in the order the configuration lists them.
        version: A digest of everything above that decides the ranking. It is
            recorded with each decision, and session state from another version
            is not reused.
        scorer: Estimates the candidates.
        objective: Ranks them.
    """

    name: str
    candidates: tuple[Candidate, ...]
    version: str
    scorer: Scorer
    objective: Objective


@dataclass(frozen=True)
class AutoConfig:
    """A parsed and checked ``auto:`` section.

    Attributes:
        default_profile: The tier bare ``auto`` means.
        profiles: Every tier, by name.
        idle_timeout_seconds: How long a session's model is remembered without a
            request.
        max_sessions: How many sessions are remembered at once.
    """

    default_profile: str
    profiles: Mapping[str, AutoProfile]
    idle_timeout_seconds: float
    max_sessions: int

    @property
    def virtual_model_ids(self) -> frozenset[str]:
        """Return every model name this section serves."""
        return _virtual_model_ids(self.profiles)

    def resolve(self, model_id: str) -> AutoProfile | None:
        """Return the tier ``model_id`` names, or ``None`` if it names none."""
        if model_id == AUTO_MODEL_ID:
            return self.profiles[self.default_profile]
        if model_id.startswith(AUTO_MODEL_PREFIX):
            return self.profiles.get(model_id[len(AUTO_MODEL_PREFIX) :])
        return None


class AutoConfigError(ValueError):
    """The ``auto:`` section is unusable. ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        """Record ``problems`` and say all of them in the message."""
        self.problems = problems
        super().__init__("invalid auto configuration: " + "; ".join(problems))


@dataclass(frozen=True)
class CatalogCheck:
    """How an ``auto:`` section fits the registered models.

    Attributes:
        conflicts: Problems that must turn auto off.
        warnings: Candidates the catalog cannot serve as configured. Selection
            leaves them out of each request until it can.
    """

    conflicts: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def parse_auto_config(raw: Any) -> AutoConfig:
    """Parse the value of the ``auto:`` key.

    Raises:
        AutoConfigError: Listing every problem found.
    """
    if not isinstance(raw, dict):
        raise AutoConfigError([f"auto must be a mapping, got {type(raw).__name__}"])
    try:
        spec = _AutoSpec.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise AutoConfigError([_describe(error) for error in exc.errors()]) from None

    virtual = _virtual_model_ids(spec.profiles)
    problems: list[str] = []
    if spec.default_profile not in spec.profiles:
        problems.append(
            f"auto.default_profile: {spec.default_profile!r} is not one of the profiles "
            f"{sorted(spec.profiles)}"
        )
    profiles: dict[str, AutoProfile] = {}
    for name, profile_spec in spec.profiles.items():
        where = f"auto.profiles.{name}"
        if not _PROFILE_NAME.fullmatch(name):
            problems.append(
                f"{where}: a profile name is lowercase letters, digits and single hyphens"
            )
        candidates = tuple(
            Candidate(item.model, item.reasoning_effort) for item in profile_spec.candidates
        )
        seen: set[Candidate] = set()
        for index, candidate in enumerate(candidates):
            if candidate in seen:
                problems.append(f"{where}.candidates.{index}: {candidate} is listed twice")
            seen.add(candidate)
            if candidate.model_id in virtual:
                problems.append(
                    f"{where}.candidates.{index}: {candidate.model_id!r} is an auto model, "
                    "not a model to choose"
                )
        profiles[name] = AutoProfile(
            name=name,
            candidates=candidates,
            version=_profile_version(name, candidates),
            scorer=StaticScorer(candidates),
            objective=MaxQuality(),
        )
    if problems:
        raise AutoConfigError(problems)
    return AutoConfig(
        default_profile=spec.default_profile,
        profiles=profiles,
        idle_timeout_seconds=spec.session.idle_timeout_seconds,
        max_sessions=spec.session.max_sessions,
    )


def check_against_catalog(config: AutoConfig, catalog: Mapping[str, CandidateInfo]) -> CatalogCheck:
    """Hold ``config`` against the registered models.

    Args:
        config: A parsed section.
        catalog: Every name a request may use for a model -- canonical ids and
            aliases -- and what that model can serve.
    """
    conflicts = tuple(
        f"{model_id!r} is a registered model name; auto would hide it"
        for model_id in sorted(config.virtual_model_ids)
        if model_id in catalog
    )
    warnings: list[str] = []
    for profile in config.profiles.values():
        for candidate in profile.candidates:
            info = catalog.get(candidate.model_id)
            where = f"auto.profiles.{profile.name}: {candidate}"
            if info is None:
                warnings.append(f"{where}: model is not registered; skipped until it is")
            elif (
                candidate.reasoning_effort is not None
                and candidate.reasoning_effort not in info.reasoning_efforts
            ):
                accepted = sorted(info.reasoning_efforts) or "no reasoning_effort"
                warnings.append(f"{where}: model accepts {accepted}; skipped")
    return CatalogCheck(conflicts=conflicts, warnings=tuple(warnings))


def _virtual_model_ids(profile_names: Mapping[str, Any]) -> frozenset[str]:
    return frozenset({AUTO_MODEL_ID, *(f"{AUTO_MODEL_PREFIX}{name}" for name in profile_names)})


def _profile_version(name: str, candidates: tuple[Candidate, ...]) -> str:
    """Digest what decides a tier's ranking, so a decision says which tier made it."""
    canonical = json.dumps(
        {
            "profile": name,
            "scorer": StaticScorer.version,
            "objective": MaxQuality.name,
            "candidates": [[c.model_id, c.reasoning_effort] for c in candidates],
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _describe(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in ("auto", *error.get("loc", ())))
    return f"{location}: {error.get('msg', 'invalid')}"
