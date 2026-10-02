from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, WrapValidator, model_validator
from typing_extensions import Self


def _validate_state_id(value: Any, handler: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("state id must be an integer or string")
    return handler(value)


NonEmptyId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
StateId = Annotated[
    Union[int, NonEmptyId],
    WrapValidator(_validate_state_id),
]


class ProcessInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    states: Annotated[list[StateId], Field(min_length=1, max_length=8)]
    initial: StateId
    finals: Annotated[list[StateId], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def validate_states(self) -> Self:
        if len(set(self.states)) != len(self.states):
            raise ValueError("states must not contain duplicate values")
        if self.initial not in self.states:
            raise ValueError("initial state must be one of states")
        if len(set(self.finals)) != len(self.finals):
            raise ValueError("finals must not contain duplicate values")
        unknown = [state for state in self.finals if state not in self.states]
        if unknown:
            raise ValueError("every final state must be one of states")
        return self


class TransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: NonEmptyId
    sender: Literal["A", "B"] = Field(alias="from")
    receiver: Literal["A", "B"] = Field(alias="to")
    action: Literal["send", "receive"]
    message: NonEmptyId
    source: StateId
    target: StateId

    @model_validator(mode="after")
    def validate_endpoints(self) -> Self:
        if self.sender == self.receiver:
            raise ValueError("from and to must be different endpoints")
        return self


class DeadlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    A: ProcessInput
    B: ProcessInput
    transitions: Annotated[list[TransitionInput], Field(max_length=30)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_transitions(self) -> Self:
        ids = [transition.id for transition in self.transitions]
        if len(set(ids)) != len(ids):
            raise ValueError("transition ids must be globally unique")

        messages = {transition.message for transition in self.transitions}
        if len(messages) > 8:
            raise ValueError("there can be at most 8 distinct message types")

        processes = {"A": self.A, "B": self.B}
        for transition in self.transitions:
            owner = transition.sender if transition.action == "send" else transition.receiver
            process = processes[owner]
            if transition.source not in process.states:
                raise ValueError(
                    f"transition {transition.id!r} source is not a state of endpoint {owner}"
                )
            if transition.target not in process.states:
                raise ValueError(
                    f"transition {transition.id!r} target is not a state of endpoint {owner}"
                )
        return self


class CompletionRequest(DeadlockRequest):
    required_transition_ids: Annotated[list[NonEmptyId], Field(min_length=0, max_length=6)] = Field(
        default_factory=list
    )
    @model_validator(mode="after")
    def validate_required_transition_ids(self) -> Self:
        if len(set(self.required_transition_ids)) != len(self.required_transition_ids):
            raise ValueError("required_transition_ids must not contain duplicates")
        known = {transition.id for transition in self.transitions}
        unknown = [transition_id for transition_id in self.required_transition_ids if transition_id not in known]
        if unknown:
            raise ValueError(f"unknown required_transition_ids: {unknown}")
        return self


class DeactivationRequest(DeadlockRequest):
    removable_transition_ids: Annotated[list[NonEmptyId], Field(min_length=0, max_length=8)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_removable_transition_ids(self) -> Self:
        if len(set(self.removable_transition_ids)) != len(self.removable_transition_ids):
            raise ValueError("removable_transition_ids must not contain duplicates")
        known = {transition.id for transition in self.transitions}
        unknown = [transition_id for transition_id in self.removable_transition_ids if transition_id not in known]
        if unknown:
            raise ValueError(f"unknown removable_transition_ids: {unknown}")
        return self


# The completion-guarantee endpoint accepts the existing protocol request
# unchanged; an alias keeps the endpoint self-documenting.
GuaranteeRequest = DeadlockRequest


class StateView(BaseModel):
    A: StateId
    B: StateId


class QueuesView(BaseModel):
    A_to_B: str | None
    B_to_A: str | None


class ConfigurationView(BaseModel):
    state: StateView
    queues: QueuesView


class PathStep(BaseModel):
    index: int
    transition_id: str
    from_: Literal["A", "B"] = Field(alias="from")
    to: Literal["A", "B"]
    action: Literal["send", "receive"]
    message: str
    state: StateView
    queues: QueuesView

    model_config = ConfigDict(populate_by_name=True)


class DeadlockResponse(BaseModel):
    deadlock: bool
    visited_states: int
    initial: ConfigurationView
    deadlock_state: ConfigurationView | None = None
    path: list[PathStep] | None = None


class CompletionResponse(BaseModel):
    completable: bool
    visited_combinations: int
    initial: ConfigurationView
    path: list[PathStep] | None = None


class DeactivationResponse(BaseModel):
    solvable: bool
    deactivated_transition_ids: list[str] | None = None
    reachable_configurations: int | None = None


class DeadlockWitness(BaseModel):
    kind: Literal["deadlock"]
    deadlock_state: ConfigurationView
    path: list[PathStep]


class CycleWitness(BaseModel):
    kind: Literal["cycle"]
    entry_state: ConfigurationView
    prefix: list[PathStep]
    cycle: list[PathStep]


GuaranteeWitness = Annotated[
    Union[DeadlockWitness, CycleWitness],
    Field(discriminator="kind"),
]


class GuaranteeResponse(BaseModel):
    result: Literal["guaranteed", "deadlock", "cycle"]
    visited_states: int
    initial: ConfigurationView
    witness: GuaranteeWitness | None = None


# ---------------------------------------------------------------------------
# Stateful protocol-drill sessions
# ---------------------------------------------------------------------------


# A session is created with exactly the same protocol payload as the offline
# analysis endpoints, so their validation (unique ids, state references, message
# limits, ...) is reused unchanged.
SessionCreateRequest = DeadlockRequest


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transition_id: NonEmptyId
    expected_version: Annotated[int, Field(ge=0)]


class ProtocolReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol: DeadlockRequest
    expected_version: Annotated[int, Field(ge=0)]


class SessionView(BaseModel):
    id: str
    version: int
    protocol: DeadlockRequest
    configuration: ConfigurationView
    trace: list[PathStep]
    enabled_transition_ids: list[str]
