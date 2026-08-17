import pandas as pd
import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.transformation import Transformation
from app.models.user import User
from app.services.dataset_service import (
    DatasetExpansionLimitError,
    DatasetService,
    UserStorageQuotaError,
)
from app.services.storage_service import StorageCapacityError, storage


def test_limited_csv_writer_stops_expansion_and_removes_partial_file(tmp_path):
    output_path = tmp_path / ".expanded.part.csv"
    dataframe = pd.DataFrame({"value": ["x" * 100, "y" * 100]})

    with pytest.raises(DatasetExpansionLimitError):
        DatasetService.write_dataframe_limited(
            dataframe,
            output_path,
            expanded_limit_bytes=64,
            quota_remaining_bytes=10_000,
        )

    assert not output_path.exists()


def test_limited_json_writer_enforces_remaining_user_quota(tmp_path):
    output_path = tmp_path / ".quota.part.json"
    dataframe = pd.DataFrame({"value": ["a" * 100]})

    with pytest.raises(UserStorageQuotaError):
        DatasetService.write_dataframe_limited(
            dataframe,
            output_path,
            expanded_limit_bytes=10_000,
            quota_remaining_bytes=32,
        )

    assert not output_path.exists()


def test_limited_writer_rechecks_disk_capacity_before_writing(
    monkeypatch,
    tmp_path,
):
    output_path = tmp_path / ".capacity.part.csv"
    required_sizes = []

    def reject_capacity(required_bytes=0):
        required_sizes.append(required_bytes)
        raise StorageCapacityError("disk floor reached")

    monkeypatch.setattr(storage, "ensure_capacity", reject_capacity)

    with pytest.raises(StorageCapacityError):
        DatasetService.write_dataframe_limited(
            pd.DataFrame({"value": [1, 2, 3]}),
            output_path,
            expanded_limit_bytes=10_000,
            quota_remaining_bytes=10_000,
        )

    assert required_sizes and required_sizes[0] > 0
    assert not output_path.exists()


@pytest.mark.parametrize("suffix", [".csv", ".json", ".xlsx", ".parquet"])
def test_limited_writer_supports_each_transformation_format(tmp_path, suffix):
    output_path = tmp_path / f".output.part{suffix}"

    DatasetService.write_dataframe_limited(
        pd.DataFrame({"category": ["A", "B"], "amount": [1.5, 2.5]}),
        output_path,
        expanded_limit_bytes=1_000_000,
        quota_remaining_bytes=1_000_000,
    )

    assert output_path.stat().st_size > 0


def test_transformation_aborts_expanding_fill_nulls_before_commit(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "MAX_DATASET_EXPANDED_SIZE_MB", 1)
    monkeypatch.setattr(settings, "USER_STORAGE_QUOTA_MB", 10)
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    input_path.write_text('value\n""\n', encoding="utf-8")

    db = SessionLocal()
    fence_observations = []
    try:
        user = User(
            name="Output limits",
            email="output-limits@example.com",
            password_hash="not-used",
        )
        project = Project(name="Output limits", owner=user)
        dataset = Dataset(
            project=project,
            original_filename="input.csv",
            stored_path=str(input_path),
            status="transforming",
            version=1,
        )
        db.add(dataset)
        db.flush()
        transformation = Transformation(
            dataset_id=dataset.id,
            operation="fill_nulls",
            parameters={"values": {"value": "x" * 1_100_000}},
            user_id=user.id,
            status="pending",
            expected_version=1,
            input_path=str(input_path),
            output_path=str(output_path),
            before_rows=0,
            after_rows=0,
            before_columns=0,
            after_columns=0,
        )
        db.add(transformation)
        db.commit()

        with pytest.raises(DatasetExpansionLimitError):
            DatasetService.execute_prepared_transformation(
                db,
                transformation.id,
                transaction_fence=lambda: fence_observations.append(
                    (output_path.exists(), bool(list(tmp_path.glob(".*.part.csv"))))
                ),
            )

        db.refresh(dataset)
        db.refresh(transformation)
        assert dataset.status == "ready"
        assert transformation.status == "failed"
        assert fence_observations == []
        assert not output_path.exists()
        assert not list(tmp_path.glob(".*.part.csv"))
    finally:
        db.close()


def test_transformation_fences_after_temp_write_and_before_final_rename(tmp_path):
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    input_path.write_text("value\n1\n2\n", encoding="utf-8")
    observations = []
    db = SessionLocal()
    try:
        user = User(
            name="Finalization fence",
            email="finalization-fence@example.com",
            password_hash="not-used",
        )
        project = Project(name="Finalization fence", owner=user)
        dataset = Dataset(
            project=project,
            original_filename="input.csv",
            stored_path=str(input_path),
            status="transforming",
            version=1,
        )
        db.add(dataset)
        db.flush()
        transformation = Transformation(
            dataset_id=dataset.id,
            operation="drop_duplicates",
            parameters={},
            user_id=user.id,
            status="pending",
            expected_version=1,
            input_path=str(input_path),
            output_path=str(output_path),
            before_rows=0,
            after_rows=0,
            before_columns=0,
            after_columns=0,
        )
        db.add(transformation)
        db.commit()

        completed = DatasetService.execute_prepared_transformation(
            db,
            transformation.id,
            transaction_fence=lambda: observations.append(
                (
                    output_path.exists(),
                    len(list(tmp_path.glob(".*.part.csv"))),
                )
            ),
        )
        db.commit()

        assert observations == [(False, 1)]
        assert completed.status == "completed"
        assert output_path.exists()
        assert not list(tmp_path.glob(".*.part.csv"))
    finally:
        db.close()
