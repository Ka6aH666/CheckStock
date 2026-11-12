from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WarehouseInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value):
        if not value.strip():
            raise ValueError("Название не может быть пустым")
        return value.strip()


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID
    kind: Literal["receipt", "transfer", "fbs", "fbo"]
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, strict=True)
    source_id: int | None = Field(default=None, gt=0)
    destination_id: int | None = Field(default=None, gt=0)
    target: str | None = Field(default=None, max_length=160)

    @field_validator("target")
    @classmethod
    def strip_target(cls, value):
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def check_route(self):
        if self.kind == "receipt":
            valid = self.destination_id is not None and self.source_id is None and not self.target
        elif self.kind == "transfer":
            valid = (
                self.source_id is not None
                and self.destination_id is not None
                and self.source_id != self.destination_id
                and not self.target
            )
        else:
            valid = self.source_id is not None and self.destination_id is None and bool(self.target)
        if not valid:
            raise ValueError("Укажите корректные источник и назначение для типа операции")
        return self
