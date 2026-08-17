from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import validate_bcrypt_password_length


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(
        min_length=10,
        max_length=128,
        description="At least 10 characters with a letter and a number.",
    )

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        validate_bcrypt_password_length(value)
        if not any(character.isalpha() for character in value):
            raise ValueError("Password must contain at least one letter.")
        if not any(character.isdigit() for character in value):
            raise ValueError("Password must contain at least one number.")
        return value

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, value: str) -> str:
        return validate_bcrypt_password_length(value)

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class LogoutRequest(BaseModel):
    refresh_token: str
