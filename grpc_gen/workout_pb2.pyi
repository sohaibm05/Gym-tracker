from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class LogSetRequest(_message.Message):
    __slots__ = ("session_id", "exercise_id", "exercise_name", "weight_kg", "reps", "cheat_reps", "is_warmup", "rpe", "rir", "idempotency_key")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    EXERCISE_ID_FIELD_NUMBER: _ClassVar[int]
    EXERCISE_NAME_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_KG_FIELD_NUMBER: _ClassVar[int]
    REPS_FIELD_NUMBER: _ClassVar[int]
    CHEAT_REPS_FIELD_NUMBER: _ClassVar[int]
    IS_WARMUP_FIELD_NUMBER: _ClassVar[int]
    RPE_FIELD_NUMBER: _ClassVar[int]
    RIR_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    session_id: int
    exercise_id: int
    exercise_name: str
    weight_kg: float
    reps: int
    cheat_reps: int
    is_warmup: bool
    rpe: float
    rir: int
    idempotency_key: str
    def __init__(self, session_id: _Optional[int] = ..., exercise_id: _Optional[int] = ..., exercise_name: _Optional[str] = ..., weight_kg: _Optional[float] = ..., reps: _Optional[int] = ..., cheat_reps: _Optional[int] = ..., is_warmup: bool = ..., rpe: _Optional[float] = ..., rir: _Optional[int] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class LoggedSet(_message.Message):
    __slots__ = ("log_id", "exercise_id", "exercise_name", "set_number", "weight_kg", "reps", "cheat_reps", "is_warmup", "volume_kg", "display", "logged_at")
    LOG_ID_FIELD_NUMBER: _ClassVar[int]
    EXERCISE_ID_FIELD_NUMBER: _ClassVar[int]
    EXERCISE_NAME_FIELD_NUMBER: _ClassVar[int]
    SET_NUMBER_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_KG_FIELD_NUMBER: _ClassVar[int]
    REPS_FIELD_NUMBER: _ClassVar[int]
    CHEAT_REPS_FIELD_NUMBER: _ClassVar[int]
    IS_WARMUP_FIELD_NUMBER: _ClassVar[int]
    VOLUME_KG_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_FIELD_NUMBER: _ClassVar[int]
    LOGGED_AT_FIELD_NUMBER: _ClassVar[int]
    log_id: int
    exercise_id: int
    exercise_name: str
    set_number: int
    weight_kg: float
    reps: int
    cheat_reps: int
    is_warmup: bool
    volume_kg: float
    display: str
    logged_at: str
    def __init__(self, log_id: _Optional[int] = ..., exercise_id: _Optional[int] = ..., exercise_name: _Optional[str] = ..., set_number: _Optional[int] = ..., weight_kg: _Optional[float] = ..., reps: _Optional[int] = ..., cheat_reps: _Optional[int] = ..., is_warmup: bool = ..., volume_kg: _Optional[float] = ..., display: _Optional[str] = ..., logged_at: _Optional[str] = ...) -> None: ...

class RecordBreak(_message.Message):
    __slots__ = ("record_type", "label", "value", "previous_value")
    RECORD_TYPE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_VALUE_FIELD_NUMBER: _ClassVar[int]
    record_type: str
    label: str
    value: float
    previous_value: float
    def __init__(self, record_type: _Optional[str] = ..., label: _Optional[str] = ..., value: _Optional[float] = ..., previous_value: _Optional[float] = ...) -> None: ...

class LogSetResponse(_message.Message):
    __slots__ = ("set", "records", "replayed")
    SET_FIELD_NUMBER: _ClassVar[int]
    RECORDS_FIELD_NUMBER: _ClassVar[int]
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    set: LoggedSet
    records: _containers.RepeatedCompositeFieldContainer[RecordBreak]
    replayed: bool
    def __init__(self, set: _Optional[_Union[LoggedSet, _Mapping]] = ..., records: _Optional[_Iterable[_Union[RecordBreak, _Mapping]]] = ..., replayed: bool = ...) -> None: ...
