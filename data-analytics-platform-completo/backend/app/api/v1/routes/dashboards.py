from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.chart import Chart
from app.models.dashboard import Dashboard
from app.models.user import User
from app.schemas.dashboard import (
    ChartCreate,
    ChartDataResponse,
    ChartResponse,
    DashboardCreate,
    DashboardResponse,
)
from app.services.dashboard_service import DashboardService

router = APIRouter()

@router.post("", response_model=DashboardResponse, status_code=201)
def create_dashboard(
    payload: DashboardCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        DashboardService.ensure_project(db, payload.project_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    dashboard = Dashboard(**payload.model_dump())
    db.add(dashboard)
    db.commit()
    db.refresh(dashboard)
    return dashboard

@router.get("/project/{project_id}", response_model=list[DashboardResponse])
def list_dashboards(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        DashboardService.ensure_project(db, project_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return db.query(Dashboard).filter(Dashboard.project_id == project_id).all()

@router.post("/{dashboard_id}/charts", response_model=ChartResponse, status_code=201)
def create_chart(
    dashboard_id: int,
    payload: ChartCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        DashboardService.ensure_dashboard(db, dashboard_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    chart = Chart(dashboard_id=dashboard_id, **payload.model_dump())
    db.add(chart)
    db.commit()
    db.refresh(chart)
    return chart

@router.get("/{dashboard_id}/charts", response_model=list[ChartResponse])
def list_charts(
    dashboard_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        DashboardService.ensure_dashboard(db, dashboard_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return db.query(Chart).filter(Chart.dashboard_id == dashboard_id).all()

@router.get("/charts/{chart_id}/data", response_model=ChartDataResponse)
def chart_data(
    chart_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    chart = db.query(Chart).filter(Chart.id == chart_id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="Chart not found.")
    try:
        return DashboardService.build_chart_data(db, chart, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
