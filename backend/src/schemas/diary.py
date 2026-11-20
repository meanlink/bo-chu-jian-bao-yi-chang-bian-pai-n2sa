from pydantic import BaseModel, Field


class WiringEdgeInput(BaseModel):
    upstream: str = Field(min_length=1, max_length=80)
    downstream: str = Field(min_length=1, max_length=80)


class WiringInput(BaseModel):
    effective_from: str = Field(min_length=8, max_length=10)
    edges: list[WiringEdgeInput] = Field(min_length=1, max_length=200)
    sources: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default='', max_length=500)
    allow_future: bool = False


class BatchInput(BaseModel):
    dataset_id: int
    kind: str = Field(default='base', pattern='^(base|supplement)$')
    parent_batch_id: int | None = None
    label: str = Field(default='', max_length=200)


class BuildInput(BaseModel):
    batch_id: int
    threshold: float | None = None
