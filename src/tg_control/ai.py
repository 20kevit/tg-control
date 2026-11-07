"""Provider-independent AI task contracts (synthetic-only, compliance-blocked).

Design notes (see ADR-011):
- There is no approved user-facing AI utility. This module provides the reusable
  task/provider contract, a conformance task used by synthetic tests, and hard
  fail-closed behavior. No task is registered by the running application, so the
  catalog is empty until the owner approves an exact utility.
- Invocation is explicit and authenticated. Nothing here subscribes to events,
  reacts to message arrival or can be reached without a caller-supplied request.
- There is deliberately no adapter, database or Telegram reference in this
  module: a task cannot read chats, history, media or another account. Only the
  exact caller-provided payload is passed to a provider.
- Real Telegram-derived input is rejected structurally: task payloads are strict
  schemas with explicit fields, and any Telegram reference field is refused
  before a provider is selected. A configuration flag cannot enable it.
- Providers are pluggable and capability-declared. Output is schema-validated,
  budgets and timeouts are enforced, retries require an explicit retryable,
  not-applied provider error, and audit contains no input or output text.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from . import audit
from .auth import Principal, is_active

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_COST_USD = 0.05
MAX_ATTEMPTS = 2
MAX_INPUT_CHARS_HARD = 8000
TELEGRAM_REFERENCE_FIELDS = frozenset(
    {
        "account_id",
        "chat_id",
        "message_id",
        "media",
        "media_id",
        "messages",
        "chat_history",
        "telegram",
        "tdlib",
        "session",
        "phone_number",
        "credentials",
    }
)
SECRET_FIELD_HINTS = ("token", "secret", "password", "api_key", "credential")


class AIError(Exception):
    """Normalized AI-domain failure. Codes only; never provider text."""

    def __init__(self, code: str, status_code: int, *, retry_after: float | None = None):
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(code)


class TaskInput(BaseModel):
    """Base for strict task inputs. Extra fields are always rejected."""

    model_config = ConfigDict(extra="forbid")


class ConformanceInput(TaskInput):
    """Synthetic conformance task: echoes exactly the supplied text.

    This is an engineering contract test, not a product utility. It is never
    registered by the application.
    """

    text: Annotated[str, Field(min_length=1, max_length=MAX_INPUT_CHARS_HARD)]
    source: Literal["caller_supplied", "synthetic_fixture"] = "caller_supplied"


class ConformanceOutput(TaskInput):
    text: str


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    models: tuple[str, ...]
    task_kinds: tuple[str, ...]
    max_input_chars: int
    max_output_tokens: int
    price_per_1k_input: float
    price_per_1k_output: float
    local: bool = True
    supports_cancellation: bool = True

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 64:
            raise ValueError("invalid provider name")
        if not self.models or not self.task_kinds:
            raise ValueError("provider must declare models and task kinds")
        if not (1 <= self.max_input_chars <= 200_000):
            raise ValueError("invalid provider input limit")
        if not (1 <= self.max_output_tokens <= 100_000):
            raise ValueError("invalid provider output limit")
        if min(self.price_per_1k_input, self.price_per_1k_output) < 0:
            raise ValueError("negative price")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "models": list(self.models),
            "task_kinds": list(self.task_kinds),
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "local": self.local,
        }


@dataclass(frozen=True)
class ProviderRequest:
    task_kind: str
    model: str
    input: dict[str, Any]
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class ProviderResult:
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("negative usage")


class ProviderError(Exception):
    """A provider failure. Only an explicit not-applied retryable error is retried."""

    def __init__(self, code: str, *, retryable: bool = False, applied: bool = True):
        self.code = code if isinstance(code, str) and len(code) <= 64 else "provider_error"
        self.retryable = retryable is True
        self.applied = applied is not False
        super().__init__(self.code)


class AIProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def run(self, request: ProviderRequest) -> ProviderResult: ...


@dataclass(frozen=True)
class TaskSpec:
    name: str
    version: int
    task_kind: str
    description: str
    input_model: type[TaskInput]
    output_model: type[TaskInput]
    max_input_chars: int = 2000
    max_cost_usd: float = DEFAULT_MAX_COST_USD
    invocation: tuple[str, ...] = ("client",)


def _estimate_tokens(text: str) -> int:
    # Conservative, provider-independent estimate for pre-flight budgeting.
    return max(1, (len(text) + 3) // 4)


class AIService:
    def __init__(self, sessions, *, clock: Callable[[], float] = time.time, sleep=None):
        self.sessions = sessions
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._providers: dict[str, AIProvider] = {}
        self._tasks: dict[str, TaskSpec] = {}

    # -- registry ----------------------------------------------------------

    def register_provider(self, provider: AIProvider) -> None:
        capabilities = provider.capabilities()
        if not isinstance(capabilities, ProviderCapabilities):
            raise ValueError("invalid provider capabilities")
        if capabilities.name in self._providers:
            raise ValueError("duplicate provider")
        self._providers[capabilities.name] = provider

    def register_task(self, spec: TaskSpec) -> None:
        if not isinstance(spec, TaskSpec):
            raise TypeError("invalid task spec")
        if not spec.name or len(spec.name) > 64 or spec.name in self._tasks:
            raise ValueError("invalid or duplicate task")
        if spec.invocation != ("client",):
            # Only explicit client invocation is approved.
            raise ValueError("only explicit client invocation is supported")
        if spec.max_input_chars > MAX_INPUT_CHARS_HARD or spec.max_input_chars < 1:
            raise ValueError("invalid task input limit")
        if not (0 <= spec.max_cost_usd <= 1.0):
            raise ValueError("invalid task cost budget")
        self._tasks[spec.name] = spec

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "invocation": list(spec.invocation),
                "max_input_chars": spec.max_input_chars,
            }
            for spec in self._tasks.values()
        ]

    def providers(self) -> list[dict[str, Any]]:
        return [provider.capabilities().as_dict() for provider in self._providers.values()]

    # -- invocation --------------------------------------------------------

    async def invoke(
        self,
        principal: Principal,
        task_name: str,
        payload: dict[str, Any],
        *,
        provider_name: str,
        model: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        spec = self._tasks.get(task_name)
        if spec is None:
            raise AIError("unknown_task", 404)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise AIError("credential_revoked", 401)
        if not isinstance(payload, dict) or TELEGRAM_REFERENCE_FIELDS & set(payload):
            # Never accept Telegram-derived references, regardless of configuration.
            raise self._fail(principal, task_name, request_id, "compliance_blocked", 422)
        for key in payload:
            lowered = str(key).lower()
            if any(hint in lowered for hint in SECRET_FIELD_HINTS):
                raise self._fail(principal, task_name, request_id, "invalid_input", 422)
        try:
            parsed = spec.input_model.model_validate(payload)
        except ValueError:
            raise self._fail(principal, task_name, request_id, "invalid_input", 422) from None
        encoded = parsed.model_dump(mode="json")
        text_size = sum(len(str(value)) for value in encoded.values())
        if text_size > spec.max_input_chars:
            raise self._fail(principal, task_name, request_id, "input_too_large", 413)
        provider = self._providers.get(provider_name)
        if provider is None:
            raise self._fail(principal, task_name, request_id, "unknown_provider", 404)
        capabilities = provider.capabilities()
        if spec.task_kind not in capabilities.task_kinds:
            # No silent fallback to another provider.
            raise self._fail(principal, task_name, request_id, "unsupported_task", 422)
        selected_model = model or capabilities.models[0]
        if selected_model not in capabilities.models:
            raise self._fail(principal, task_name, request_id, "unknown_model", 422)
        if text_size > capabilities.max_input_chars:
            raise self._fail(principal, task_name, request_id, "input_too_large", 413)
        input_tokens = _estimate_tokens(" ".join(str(value) for value in encoded.values()))
        max_output_tokens = min(capabilities.max_output_tokens, 1024)
        worst_cost = (
            input_tokens / 1000 * capabilities.price_per_1k_input
            + max_output_tokens / 1000 * capabilities.price_per_1k_output
        )
        if worst_cost > spec.max_cost_usd:
            raise self._fail(principal, task_name, request_id, "budget_exceeded", 422)

        request = ProviderRequest(
            task_kind=spec.task_kind,
            model=selected_model,
            input=encoded,
            max_output_tokens=max_output_tokens,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        )
        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await asyncio.wait_for(
                    provider.run(request), timeout=DEFAULT_TIMEOUT_SECONDS
                )
            except TimeoutError:
                raise self._fail(principal, task_name, request_id, "timeout", 504) from None
            except asyncio.CancelledError:
                raise
            except ProviderError as exc:
                if exc.retryable and not exc.applied and attempt < MAX_ATTEMPTS:
                    await self._sleep(0.05 * attempt)
                    continue
                # Only a not-applied retryable error is a transient availability issue;
                # an ambiguous (possibly applied) failure is reported as a provider error.
                transient = exc.retryable and not exc.applied
                code = "provider_unavailable" if transient else "provider_error"
                raise self._fail(principal, task_name, request_id, code, 502) from None
            except Exception:
                raise self._fail(
                    principal, task_name, request_id, "provider_error", 502
                ) from None
            break
        if not isinstance(result, ProviderResult):
            raise self._fail(principal, task_name, request_id, "invalid_provider_result", 502)
        try:
            output = spec.output_model.model_validate(result.output)
        except ValueError:
            raise self._fail(principal, task_name, request_id, "invalid_output", 502) from None
        actual_cost = (
            result.input_tokens / 1000 * capabilities.price_per_1k_input
            + result.output_tokens / 1000 * capabilities.price_per_1k_output
        )
        latency_ms = int(max(0.0, self._clock() - started) * 1000)
        self._audit(
            principal,
            task_name,
            request_id,
            "success",
            None,
        )
        return {
            "task": spec.name,
            "version": spec.version,
            "provider": capabilities.name,
            "model": result.model,
            "output": output.model_dump(mode="json"),
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "estimated_cost_usd": round(actual_cost, 6),
                "latency_ms": latency_ms,
                "attempts": attempt,
            },
        }

    def _fail(
        self,
        principal: Principal,
        task_name: str,
        request_id: str,
        code: str,
        status_code: int,
    ) -> AIError:
        self._audit(principal, task_name, request_id, "failure", code)
        return AIError(code, status_code)

    def _audit(
        self,
        principal: Principal,
        task_name: str,
        request_id: str,
        decision: str,
        error: str | None,
    ) -> None:
        # Task name and outcome only: no input, output, provider text or usage text.
        with self.sessions() as session:
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="ai_task",
                decision=decision,
                error_code=error,
            )
            session.commit()


@dataclass
class FakeProvider:
    """Deterministic synthetic provider used by conformance tests."""

    name: str = "synthetic-local"
    task_kinds: tuple[str, ...] = ("conformance.echo",)
    input_price: float = 0.0
    output_price: float = 0.0
    fail_first: int = 0
    failure: ProviderError | None = None
    delay: float = 0.0
    calls: list[ProviderRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            models=("synthetic-model-1",),
            task_kinds=self.task_kinds,
            max_input_chars=MAX_INPUT_CHARS_HARD,
            max_output_tokens=256,
            price_per_1k_input=self.input_price,
            price_per_1k_output=self.output_price,
        )

    async def run(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise self.failure or ProviderError("transient", retryable=True, applied=False)
        text = str(request.input.get("text", ""))
        return ProviderResult(
            output={"text": text},
            input_tokens=_estimate_tokens(text),
            output_tokens=_estimate_tokens(text),
            model=request.model,
        )


def conformance_task() -> TaskSpec:
    """The synthetic conformance task. Applications must not register it."""
    return TaskSpec(
        name="conformance.echo",
        version=1,
        task_kind="conformance.echo",
        description=(
            "Engineering conformance task: echoes exactly the caller-supplied text. "
            "Not a product utility and not approved user-facing behavior."
        ),
        input_model=ConformanceInput,
        output_model=ConformanceOutput,
        max_input_chars=2000,
    )
