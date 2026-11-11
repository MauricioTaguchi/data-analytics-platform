from typing import Literal

from app.core.security import validate_bcrypt_password_length

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeleteConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["DELETE"]


class DeleteAccountConfirmation(DeleteConfirmation):
    current_password: str = Field(min_length=1, max_length=72)

    @field_validator("current_password")
    @classmethod
    def validate_current_password(cls, value: str) -> str:
        return validate_bcrypt_password_length(value)


class DeletionResponse(BaseModel):
    status: Literal["deleted"]
    scope: Literal["dataset", "project", "account"]
    cleanup_pending: bool
    message: str
