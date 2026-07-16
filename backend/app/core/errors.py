from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

class DomainError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message},
        )
