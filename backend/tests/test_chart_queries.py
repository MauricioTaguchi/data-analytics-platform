from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.config import settings
from app.schemas.dashboard import ChartCreate
from app.services.chart_cache import ChartCache, chart_cache
from app.services.chart_filters import apply_filters
from app.services.chart_query import ChartResourceError, DeadlineConnection, QueryGate, json_value, query_parquet
from app.services.dashboard_service import DashboardService
from app.services.dataset_service import DatasetService


def chart(**changes):
    values = dict(dashboard_id=1, dataset_id=1, chart_type="bar", title="Revenue",
                  x_column="region", y_column="revenue", aggregation="sum", filters_json={})
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize("filters", [
    {"missing": {"eq": 1}}, {"revenue": {"unknown": 1}},
    {"revenue": "South"}, {"revenue": {}}, {"revenue": {"gte": "12"}},
    {"region": {"gte": "South"}}, {"region": {"contains": 1}},
    {"revenue": {"contains": "1"}}, {"revenue": {"eq": True}},
    {"revenue": {"eq": [1]}}, {"revenue": {"lte": None}},
])
def test_invalid_filters_fail_for_pandas_and_parquet(tmp_path, filters):
    frame = pd.DataFrame({"region": ["South", "North"], "revenue": [1, 2]})
    path = tmp_path / "source.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError):
        apply_filters(frame, filters)
    with pytest.raises(ValueError):
        query_parquet(path, chart(filters_json=filters))


def test_filter_contract_rejects_malformed_payload_before_persistence():
    with pytest.raises(ValueError, match="Unknown filter operator"):
        ChartCreate(dataset_id=1, title="Revenue", chart_type="bar", filters_json={"x": {"regex": ".*"}})


@pytest.mark.parametrize("aggregation", ["sum", "mean", "count", "min", "max"])
@pytest.mark.parametrize("chart_type", ["bar", "pie", "kpi"])
def test_parquet_aggregation_matches_pandas_null_zero_and_filters(tmp_path, aggregation, chart_type):
    frame = pd.DataFrame({"region": ["North", "South", "South", "Empty", None],
                          "revenue": [0.0, 0.0001, 2.002, None, 4.0], "ignored": ["x"] * 5})
    path = tmp_path / "source.parquet"
    frame.to_parquet(path, index=False)
    definition = chart(aggregation=aggregation, chart_type=chart_type)
    expected = json_value(DashboardService._build_pandas_chart(frame, definition))
    assert query_parquet(path, definition) == expected
    definition.filters_json = {"revenue": {"gte": 0, "lte": 2.002}}
    assert query_parquet(path, definition) == json_value(DashboardService._build_pandas_chart(frame, definition))


@pytest.mark.parametrize("values", [[0, 1, 2, 3, 4, 5, 10], [4, 4, 4], [None, None]])
def test_parquet_histogram_preserves_pandas_bins(tmp_path, values):
    frame = pd.DataFrame({"revenue": values})
    path = tmp_path / "source.parquet"
    frame.to_parquet(path, index=False)
    definition = chart(chart_type="histogram", x_column="revenue", y_column=None)
    assert query_parquet(path, definition) == DashboardService._build_pandas_chart(frame, definition)


def test_parquet_queries_quote_columns_bind_values_and_keep_contains_literal(tmp_path):
    column = 'region"; DROP VIEW snapshot; --'
    frame = pd.DataFrame({column: ["a.b", "axb", "A.B"], "value": [1, 100, 2]})
    path = tmp_path / "source.parquet"
    frame.to_parquet(path, index=False)
    definition = chart(x_column=column, y_column="value", filters_json={column: {"eq": "a.b"}})
    result = query_parquet(path, definition)
    assert result == {"labels": ["a.b"], "values": [1], "rows": []}
    definition.filters_json = {column: {"contains": "."}}
    assert query_parquet(path, definition) is None
    assert DashboardService._build_pandas_chart(frame, definition) == {
        "labels": ["A.B", "a.b"], "values": [2, 1], "rows": [],
    }


def test_parquet_table_is_limited_and_normalizes_json_nulls(tmp_path):
    path = tmp_path / "source.parquet"
    pd.DataFrame({"region": ["South"] * 200, "revenue": [None] * 200}).to_parquet(path, index=False)
    result = query_parquet(path, chart(chart_type="table"))
    assert len(result["rows"]) == 100
    assert result["rows"][0] == {"region": "South", "revenue": None}


def test_parquet_aggregation_does_not_materialize_dataframe(tmp_path, monkeypatch):
    path = tmp_path / "source.parquet"
    pd.DataFrame({"region": ["South"] * 1000, "revenue": [1] * 1000, "unrelated": ["x" * 1000] * 1000}).to_parquet(path, index=False)
    dataset = SimpleNamespace(stored_path=str(path), version=1)
    monkeypatch.setattr(DashboardService, "ensure_dataset_for_dashboard", classmethod(lambda *_: dataset))
    def forbidden(*_args, **_kwargs):
        pytest.fail("Parquet aggregation must not materialize a dataframe")
    monkeypatch.setattr(pd, "read_parquet", forbidden)
    monkeypatch.setattr(DatasetService, "read_dataframe", forbidden)
    assert DashboardService.build_chart_data(None, chart(), 1)["values"] == [1000]


def test_cache_checks_authorization_and_separates_owner_revision_definition(tmp_path, monkeypatch):
    chart_cache.clear()
    path = tmp_path / "source.csv"
    frame = pd.DataFrame({"region": ["South"], "revenue": [1]})
    frame.to_csv(path, index=False)
    dataset = SimpleNamespace(stored_path=str(path), version=1)
    calls, owners = [], []
    permitted = True
    def authorize(_cls, _db, _dashboard, _dataset, owner):
        owners.append(owner)
        if not permitted:
            raise ValueError("Dashboard not found.")
        return dataset
    def read(_path):
        calls.append(True)
        return frame
    monkeypatch.setattr(DashboardService, "ensure_dataset_for_dashboard", classmethod(authorize))
    monkeypatch.setattr(DatasetService, "read_dataframe", read)
    definition = chart()
    first = DashboardService.build_chart_data(None, definition, 1)
    first["values"][0] = 999
    assert DashboardService.build_chart_data(None, definition, 1)["values"] == [1]
    assert len(calls) == 1 and owners == [1, 1]
    dataset.version = 2
    DashboardService.build_chart_data(None, definition, 1)
    DashboardService.build_chart_data(None, definition, 2)
    definition.filters_json = {"revenue": {"eq": 1}}
    DashboardService.build_chart_data(None, definition, 2)
    assert len(calls) == 4
    permitted = False
    with pytest.raises(ValueError, match="not found"):
        DashboardService.build_chart_data(None, definition, 2)
    assert len(calls) == 4


def test_cache_is_bounded_expires_and_does_not_share_mutable_results(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(settings, "DASHBOARD_CACHE_MAX_BYTES", 64)
    monkeypatch.setattr(settings, "DASHBOARD_CACHE_TTL_SECONDS", 5)
    clock = [0.0]
    cache = ChartCache(lambda: clock[0])
    cache.put("a", {"values": [1]})
    cache.put("b", {"values": [2]})
    cache.put("c", {"values": [3]})
    assert cache.get("a") is None
    assert cache.get("b") == {"values": [2]}
    cache.put("large", {"text": "x" * 100})
    assert cache.get("large") is None
    clock[0] = 6
    assert cache.get("b") is None and cache.get("c") is None


def test_concurrent_query_gate_rejects_without_waiting_and_recovers(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_QUERY_MAX_CONCURRENT", 1)
    gate = QueryGate()
    admitted, release = Event(), Event()
    def query():
        with gate.acquire():
            admitted.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(query)
        assert admitted.wait(5)
        try:
            with pytest.raises(ChartResourceError, match="capacity"):
                with gate.acquire():
                    pytest.fail("A second query was admitted")
        finally:
            release.set()
        future.result()
    with gate.acquire():
        pass


def test_expired_deadline_cannot_start_a_second_query():
    expired = Event()
    expired.set()
    connection = DeadlineConnection(None, expired)
    with pytest.raises(ChartResourceError, match="deadline"):
        connection.execute("SELECT 1", [])


@pytest.mark.parametrize("frame, filters", [
    (pd.DataFrame({"region": pd.Series([1, 2, None], dtype="Int64"), "revenue": [1, 2, 3]}), {}),
    (pd.DataFrame({"region": ["ß", "SS", None], "revenue": [1, 2, 3]}), {"region": {"contains": "ss"}}),
    (pd.DataFrame({"region": pd.to_datetime(["2026-01-01T00:00:00.000000001", "2026-01-01T00:00:00.000000002"]), "revenue": [1, 2]}), {}),
])
def test_semantic_fallback_projects_required_columns(tmp_path, monkeypatch, frame, filters):
    frame["unrelated"] = "x" * 1000
    path = tmp_path / "fallback.parquet"
    frame.to_parquet(path, index=False)
    dataset = SimpleNamespace(stored_path=str(path), version=1)
    monkeypatch.setattr(DashboardService, "ensure_dataset_for_dashboard", classmethod(lambda *_: dataset))
    actual_read = pd.read_parquet
    projections = []
    def read(path, **kwargs):
        projections.append(kwargs.get("columns"))
        return actual_read(path, **kwargs)
    monkeypatch.setattr(pd, "read_parquet", read)
    definition = chart(filters_json=filters)
    assert DashboardService.build_chart_data(None, definition, 1) == json_value(DashboardService._build_pandas_chart(frame, definition))
    assert projections == [["region", "revenue"]]


def test_chart_endpoint_rejects_unknown_columns_and_reports_saturation(client, auth_headers, monkeypatch):
    from app.services.chart_query import query_gate
    from tests.test_dashboards_and_reports import create_ready_dataset

    project_id, dataset_id = create_ready_dataset(client, auth_headers)
    dashboard = client.post("/api/v1/dashboards", json={"project_id": project_id, "name": "Revenue"}, headers=auth_headers).json()
    base = {"dataset_id": dataset_id, "title": "Revenue", "chart_type": "bar", "x_column": "region", "y_column": "revenue"}
    malformed = client.post(f"/api/v1/dashboards/{dashboard['id']}/charts", json=base | {"filters_json": {"region": {"invalid": "South"}}}, headers=auth_headers)
    assert malformed.status_code == 422
    invalid = client.post(f"/api/v1/dashboards/{dashboard['id']}/charts", json=base | {"filters_json": {"missing": {"eq": "South"}}}, headers=auth_headers).json()
    response = client.get(f"/api/v1/dashboards/charts/{invalid['id']}/data", headers=auth_headers)
    assert response.status_code == 400
    assert "Unknown filter column" in response.json()["detail"]
    valid = client.post(f"/api/v1/dashboards/{dashboard['id']}/charts", json=base, headers=auth_headers).json()
    monkeypatch.setattr(settings, "DASHBOARD_QUERY_MAX_CONCURRENT", 1)
    with query_gate.acquire():
        response = client.get(f"/api/v1/dashboards/charts/{valid['id']}/data", headers=auth_headers)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert client.get(f"/api/v1/dashboards/charts/{valid['id']}/data", headers=auth_headers).status_code == 200


def test_external_parquet_ieee_nan_groups_and_histogram_match_pandas(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "external.parquet"
    pq.write_table(pa.table({"region": pa.array([1.0, float("nan"), None]), "revenue": [1, 2, 3]}), path)
    frame = pd.read_parquet(path)
    definition = chart()
    assert query_parquet(path, definition) == json_value(DashboardService._build_pandas_chart(frame, definition))
    for rule in ({"eq": None}, {"gte": 1}):
        definition.filters_json = {"region": rule}
        assert query_parquet(path, definition) == json_value(DashboardService._build_pandas_chart(frame, definition))
    definition = chart(chart_type="histogram", x_column="region", y_column=None)
    assert query_parquet(path, definition) == DashboardService._build_pandas_chart(frame, definition)


def test_duckdb_interrupts_a_running_query_and_releases_connection(monkeypatch):
    from app.services.chart_query import bounded_connection

    monkeypatch.setattr(settings, "DASHBOARD_QUERY_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(ChartResourceError, match="deadline"):
        with bounded_connection() as connection:
            connection.execute("SELECT SUM(sqrt(i)) FROM range(1000000000) AS numbers(i)", []).fetchall()
    monkeypatch.setattr(settings, "DASHBOARD_QUERY_TIMEOUT_SECONDS", 5)
    with bounded_connection() as connection:
        assert connection.execute("SELECT 1", []).fetchone() == (1,)
