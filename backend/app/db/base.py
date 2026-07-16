from sqlalchemy.orm import declarative_base

Base = declarative_base()

from app.models.user import User  # noqa: E402,F401
from app.models.project import Project  # noqa: E402,F401
from app.models.dataset import Dataset  # noqa: E402,F401
from app.models.transformation import Transformation  # noqa: E402,F401
from app.models.dashboard import Dashboard  # noqa: E402,F401
from app.models.chart import Chart  # noqa: E402,F401
from app.models.report import Report  # noqa: E402,F401
