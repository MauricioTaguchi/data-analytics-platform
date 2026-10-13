from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

class DomainError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(_: Request, exc: RequestValidationError):
        # Raw rejected values can contain NaN/Infinity (which JSONResponse cannot
        # encode) or credentials. Return useful locations and messages without
        # echoing input values or exception objects from validator context.
        return JSONResponse(
            status_code=422,
            content={"detail": [
                {key: error[key] for key in ("type", "loc", "msg")}
                for error in exc.errors()
            ]},
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message},
        )
