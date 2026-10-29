from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=255)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    email_verified: bool
    is_demo: bool


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ResetRequestIn(BaseModel):
    email: EmailStr


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class ProvidersOut(BaseModel):
    github: bool
    demo: bool = True
