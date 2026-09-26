import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from app.auth.schemas import CurrentUserResponse, TokenResponse
from app.auth.service import AuthService, AuthUser, InvalidCredentialsError
from app.db import SessionFactory
from app.security import create_access_token, decode_access_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_auth_service() -> AuthService:
    return AuthService(SessionFactory)


def _credentials_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    try:
        user = await service.authenticate(form_data.username, form_data.password)
    except InvalidCredentialsError as exc:
        raise _credentials_error("Incorrect username or password") from exc
    return TokenResponse(access_token=create_access_token(user.username, user.role))


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    service: AuthService = Depends(get_auth_service),
) -> AuthUser:
    try:
        payload = decode_access_token(token)
    except jwt.InvalidTokenError as exc:
        raise _credentials_error("Could not validate credentials") from exc

    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise _credentials_error("Could not validate credentials")

    try:
        return await service.get_active_user(username)
    except InvalidCredentialsError as exc:
        raise _credentials_error("Could not validate credentials") from exc


def require_role(*roles: str):
    async def dependency(
        current_user: AuthUser = Depends(get_current_user),
    ) -> AuthUser:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user

    return dependency


@router.get("/me", response_model=CurrentUserResponse)
async def read_me(
    current_user: AuthUser = Depends(get_current_user),
) -> CurrentUserResponse:
    return CurrentUserResponse(
        username=current_user.username,
        role=current_user.role,
        workgroup=current_user.workgroup,
    )
