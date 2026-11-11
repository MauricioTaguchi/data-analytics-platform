from datetime import timedelta
from hashlib import sha256

import pytest

from app.db.session import SessionLocal
from app.models.chart import Chart
from app.models.dashboard import Dashboard
from app.models.dataset import Dataset
from app.models.dataset_snapshot import DatasetSnapshot
from app.models.job import JobRecord
from app.models.password_reset import PasswordResetToken
from app.models.password_reset_delivery import PasswordResetDelivery
from app.models.project import Project
from app.models.report import Report
from app.models.session import RefreshSession
from app.models.storage_artifact import StorageArtifact
from app.models.transformation import Transformation
from app.models.user import User
from app.services.artifact_service import ArtifactService
from app.services.job_service import database_now
from app.services.recovery_delivery_service import enqueue_reset_request
from app.services.storage_service import storage


def _fixture(client, headers, tmp_path):
    project_id = client.post('/api/v1/projects', headers=headers, json={'name': 'Lifecycle'}).json()['id']
    with SessionLocal() as db:
        owner_id = db.get(Project, project_id).owner_id
        source = tmp_path / 'source.csv'
        historical = tmp_path / 'history.csv'
        pdf = tmp_path / 'report.pdf'
        for path in (source, historical, pdf):
            path.write_bytes(b'value\n1\n')
        dataset = Dataset(project_id=project_id, original_filename='source.csv', stored_path=str(source), status='ready', version=1, profile_json={'private': 'profile'})
        db.add(dataset)
        db.flush()
        db.add(DatasetSnapshot(id='a' * 32, dataset_id=dataset.id, stored_path=str(source), version=1, size_bytes=source.stat().st_size, sha256=sha256(source.read_bytes()).hexdigest(), pinned=True, preview_cache_json={'private': 'preview'}))
        # Historical legacy files have no snapshot manifest; they must still be
        # collected on deletion without changing the transformation audit row.
        db.add(Transformation(dataset_id=dataset.id, user_id=owner_id, operation='drop_duplicates', parameters={}, status='completed', expected_version=1, input_path=str(historical), output_path=str(source), before_rows=1, after_rows=1, before_columns=1, after_columns=1))
        report = Report(project_id=project_id, dataset_id=dataset.id, status='completed', file_path=str(pdf))
        dashboard = Dashboard(project_id=project_id, name='Dashboard')
        db.add_all([report, dashboard])
        db.flush()
        chart = Chart(dashboard_id=dashboard.id, dataset_id=dataset.id, chart_type='bar', title='Values', x_column='value', y_column='value', aggregation='sum')
        db.add(chart)
        job = JobRecord(task_id='finished-import', owner_id=owner_id, dataset_id=dataset.id, kind='import', status='SUCCESS')
        db.add(job)
        db.commit()
        return {'project': project_id, 'dataset': dataset.id, 'report': report.id, 'dashboard': dashboard.id, 'chart': chart.id, 'owner': owner_id, 'paths': (source, historical, pdf)}


def _delete(client, headers, path, **extra):
    return client.request('DELETE', '/api/v1/lifecycle/' + path, headers=headers, json={'confirmation': 'DELETE', **extra})


def test_dataset_deletion_hides_every_read_and_preserves_audit(client, auth_headers, tmp_path):
    ids = _fixture(client, auth_headers, tmp_path)
    response = _delete(client, auth_headers, f"datasets/{ids['dataset']}")
    assert response.status_code == 202, response.text
    assert response.json()['cleanup_pending'] is True
    assert 'audit' in response.json()['message'] and 'backups' in response.json()['message']
    for endpoint in (f"datasets/{ids['dataset']}/preview", f"datasets/{ids['dataset']}/export", f"datasets/{ids['dataset']}/snapshots", f"datasets/{ids['dataset']}/quality/contracts", f"reports/{ids['report']}", f"reports/{ids['report']}/download", 'datasets/jobs/finished-import'):
        assert client.get('/api/v1/' + endpoint, headers=auth_headers).status_code == 404, endpoint
    assert client.get(f"/api/v1/datasets/project/{ids['project']}", headers=auth_headers).json() == []
    assert client.get(f"/api/v1/reports/project/{ids['project']}", headers=auth_headers).json() == []
    assert client.get(f"/api/v1/dashboards/{ids['dashboard']}/charts", headers=auth_headers).json() == []
    assert client.get(f"/api/v1/dashboards/charts/{ids['chart']}/data", headers=auth_headers).status_code in (400, 404)
    with SessionLocal() as db:
        assert db.get(Dataset, ids['dataset']).profile_json is None
        snapshot = db.query(DatasetSnapshot).one()
        assert snapshot.availability == 'EXPIRED' and not snapshot.pinned
        assert snapshot.preview_cache_json is None
        assert db.query(Transformation).count() == 1
        assert db.query(StorageArtifact).count() == 3
    assert all(path.exists() for path in ids['paths'])
    assert ArtifactService.cleanup()['count'] == 3
    assert all(not path.exists() for path in ids['paths'])
    # Repeated logical deletion cannot revive the record or audit history.
    assert _delete(client, auth_headers, f"datasets/{ids['dataset']}").status_code == 404


def test_project_deletion_rejects_new_children_and_hides_reports_dashboards(client, auth_headers, tmp_path):
    ids = _fixture(client, auth_headers, tmp_path)
    response = _delete(client, auth_headers, f"projects/{ids['project']}")
    assert response.status_code == 202
    assert client.get('/api/v1/projects', headers=auth_headers).json() == []
    for endpoint in (f"projects/{ids['project']}", f"datasets/project/{ids['project']}", f"reports/project/{ids['project']}", f"dashboards/project/{ids['project']}", f"dashboards/{ids['dashboard']}/charts"):
        assert client.get('/api/v1/' + endpoint, headers=auth_headers).status_code == 404, endpoint
    assert client.post('/api/v1/dashboards', headers=auth_headers, json={'project_id': ids['project'], 'name': 'Late dashboard'}).status_code == 404
    upload = client.post(f"/api/v1/datasets/project/{ids['project']}", headers=auth_headers,
                         files={'file': ('late.csv', b'value\n2\n', 'text/csv')})
    assert upload.status_code == 404
    with SessionLocal() as db:
        assert db.query(JobRecord).count() == 1
    with SessionLocal() as db:
        assert db.get(Project, ids['project']).deleted_at is not None
        assert db.get(Dataset, ids['dataset']).deleted_at is not None


@pytest.mark.parametrize('scope', ['datasets', 'projects'])
def test_foreign_and_missing_deletion_have_same_response_and_do_not_mutate(client, auth_headers, tmp_path, scope):
    ids = _fixture(client, auth_headers, tmp_path)
    auth = client.post('/api/v1/auth/register', json={'name': 'Other', 'email': 'other@example.com', 'password': 'password12345'}).json()
    other = {'Authorization': 'Bearer ' + auth['access_token']}
    target = ids['dataset' if scope == 'datasets' else 'project']
    foreign = _delete(client, other, f'{scope}/{target}')
    missing = _delete(client, other, f'{scope}/999999')
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    with SessionLocal() as db:
        assert db.get(Dataset, ids['dataset']).deleted_at is None
        assert db.get(Project, ids['project']).deleted_at is None
        assert db.query(StorageArtifact).count() == 0
    assert all(path.exists() for path in ids['paths'])


@pytest.mark.parametrize('status', ['PENDING', 'STARTED', 'CANCELLATION_REQUESTED'])
@pytest.mark.parametrize('scope', ['dataset', 'project', 'account'])
def test_active_jobs_block_deletion_without_mutation(client, auth_headers, tmp_path, status, scope):
    ids = _fixture(client, auth_headers, tmp_path)
    with SessionLocal() as db:
        # An upload still being staged has no dataset yet and must block too.
        db.add(JobRecord(task_id='active-upload', owner_id=ids['owner'], dataset_id=None, kind='import', status=status))
        db.commit()
    path = 'account' if scope == 'account' else f"{scope}s/{ids[scope]}"
    response = _delete(client, auth_headers, path, **({'current_password': 'password12345'} if scope == 'account' else {}))
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(User, ids['owner']).is_active
        assert db.get(Dataset, ids['dataset']).deleted_at is None
        assert db.get(Project, ids['project']).deleted_at is None
        assert db.query(StorageArtifact).count() == 0


def test_confirmation_and_password_are_required_without_exposing_credentials(client, auth_headers, tmp_path):
    ids = _fixture(client, auth_headers, tmp_path)
    assert client.request('DELETE', f"/api/v1/lifecycle/datasets/{ids['dataset']}", headers=auth_headers, json={'confirmation': 'delete'}).status_code == 422
    response = _delete(client, auth_headers, 'account', current_password='wrong-password')
    assert response.status_code == 403
    assert 'wrong-password' not in response.text
    assert client.get('/api/v1/auth/me', headers=auth_headers).status_code == 200


def test_account_deletion_revokes_all_sessions_scrubs_identity_and_cancels_mail(client, auth_headers, tmp_path):
    ids = _fixture(client, auth_headers, tmp_path)
    login = client.post('/api/v1/auth/login', json={'email': 'mauricio@example.com', 'password': 'password12345'}).json()
    with SessionLocal() as db:
        enqueue_reset_request(db, 'mauricio@example.com')
        db.add(PasswordResetToken(user_id=ids['owner'], token_hash='d' * 64, expires_at=database_now(db) + timedelta(minutes=20)))
        db.commit()
    response = _delete(client, auth_headers, 'account', current_password='password12345')
    assert response.status_code == 202, response.text
    assert client.get('/api/v1/auth/me', headers=auth_headers).status_code == 401
    assert client.get('/api/v1/auth/me', headers={'Authorization': 'Bearer ' + login['access_token']}).status_code == 401
    assert client.post('/api/v1/auth/refresh', json={'refresh_token': login['refresh_token']}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'email': 'mauricio@example.com', 'password': 'password12345'}).status_code == 401
    with SessionLocal() as db:
        user = db.get(User, ids['owner'])
        assert not user.is_active and user.deleted_at and user.auth_version == 1
        assert user.name == 'Deleted account' and user.email.endswith('@deleted.invalid')
        assert db.query(RefreshSession).filter(RefreshSession.revoked_at.is_(None)).count() == 0
        assert db.query(PasswordResetToken).filter(PasswordResetToken.used_at.is_(None)).count() == 0
        mail = db.query(PasswordResetDelivery).one()
        assert mail.state == 'cancelled' and mail.encrypted_payload is None
    assert ArtifactService.cleanup()['count'] == 3


def test_durable_cleanup_retries_failure_and_collects_late_bytes(client, auth_headers, tmp_path, monkeypatch):
    ids = _fixture(client, auth_headers, tmp_path)
    assert _delete(client, auth_headers, f"datasets/{ids['dataset']}").status_code == 202
    original = storage.delete
    def fail(_path):
        raise OSError('temporary unavailable volume')
    monkeypatch.setattr(storage, 'delete', fail)
    assert ArtifactService.cleanup()['errors'] == 3
    assert all(path.exists() for path in ids['paths'])
    monkeypatch.setattr(storage, 'delete', original)
    assert ArtifactService.cleanup()['count'] == 3
    ids['paths'][0].write_bytes(b'late stale writer')
    assert ArtifactService.cleanup()['count'] == 1
    assert not ids['paths'][0].exists()


def test_deleting_one_dataset_keeps_another_dataset_and_its_files(client, auth_headers, tmp_path):
    ids = _fixture(client, auth_headers, tmp_path)
    with SessionLocal() as db:
        path = tmp_path / 'other.csv'
        path.write_text('other\n2\n')
        other = Dataset(project_id=ids['project'], original_filename='other.csv', stored_path=str(path), status='ready', version=1)
        db.add(other)
        db.commit()
        other_id = other.id
    assert _delete(client, auth_headers, f"datasets/{ids['dataset']}").status_code == 202
    ArtifactService.cleanup()
    assert path.exists()
    listing = client.get(f"/api/v1/datasets/project/{ids['project']}", headers=auth_headers).json()
    assert [item['id'] for item in listing] == [other_id]


def test_multibyte_account_password_is_validated(client, auth_headers):
    response = _delete(client, auth_headers, 'account', current_password='é' * 40)
    assert response.status_code == 422


def test_deleted_import_id_is_not_reused_or_resurrected(client, auth_headers, tmp_path):
    from uuid import uuid4
    ids = _fixture(client, auth_headers, tmp_path)
    task_id = str(uuid4())
    with SessionLocal() as db:
        db.add(JobRecord(task_id=task_id, owner_id=ids['owner'], dataset_id=ids['dataset'], kind='import', status='SUCCESS'))
        db.commit()
    assert _delete(client, auth_headers, f"datasets/{ids['dataset']}").status_code == 202
    response = client.post(f"/api/v1/datasets/project/{ids['project']}",
                           headers={**auth_headers, 'X-Task-ID': task_id},
                           files={'file': ('new.csv', b'value\n3\n', 'text/csv')})
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.query(Dataset).count() == 1
        assert db.get(Dataset, ids['dataset']).deleted_at is not None


@pytest.mark.parametrize('with_receipt', [False, True])
def test_deleted_dataset_archived_job_is_not_exposed(client, auth_headers, tmp_path, with_receipt):
    from app.models.dataset_event import DatasetEvent
    ids = _fixture(client, auth_headers, tmp_path)
    with SessionLocal() as db:
        transformation = db.query(Transformation).one()
        transformation.task_id = 'archived-job'
        if with_receipt:
            db.add(DatasetEvent(id='b' * 32, dataset_id=ids['dataset'], actor_id=ids['owner'],
                                transformation_id=transformation.id, kind='transformation',
                                request_fingerprint='c' * 64, task_id='archived-job', previous_version=1,
                                result_version=2, input_path=transformation.input_path,
                                output_path=transformation.output_path, operation_version='1.0.0',
                                result_json={'status': 'completed'}))
        db.commit()
    assert client.get('/api/v1/datasets/jobs/archived-job', headers=auth_headers).status_code == 200
    assert _delete(client, auth_headers, f"datasets/{ids['dataset']}").status_code == 202
    assert client.get('/api/v1/datasets/jobs/archived-job', headers=auth_headers).status_code == 404


def test_account_closure_fences_a_waiting_authenticated_mutation(client, auth_headers, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import event
    from app.db.session import engine
    from app.services import lifecycle_service

    if engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL owner-row locking regression')
    ids = _fixture(client, auth_headers, tmp_path)
    deletion_holds_owner = Event()
    mutation_reached_owner_lock = Event()
    release_deletion = Event()
    original = lifecycle_service._remove_dataset

    def pause_removal(*args):
        deletion_holds_owner.set()
        assert release_deletion.wait(10), 'The competing mutation did not reach admission.'
        return original(*args)

    def observe_lock(_conn, _cursor, statement, _parameters, _context, _executemany):
        if 'FOR NO KEY UPDATE' in statement and 'users.password_hash' in statement:
            mutation_reached_owner_lock.set()

    def close_account():
        with SessionLocal() as db:
            return lifecycle_service.delete_account(db, ids['owner'], 'password12345')

    monkeypatch.setattr(lifecycle_service, '_remove_dataset', pause_removal)
    event.listen(engine, 'before_cursor_execute', observe_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            closure = pool.submit(close_account)
            assert deletion_holds_owner.wait(10)
            mutation = pool.submit(client.post, '/api/v1/projects', headers=auth_headers,
                                   json={'name': 'Must not survive account closure'})
            try:
                assert mutation_reached_owner_lock.wait(10)
            finally:
                release_deletion.set()
            assert closure.result(timeout=10)['scope'] == 'account'
            assert mutation.result(timeout=10).status_code == 401
    finally:
        release_deletion.set()
        event.remove(engine, 'before_cursor_execute', observe_lock)
    with SessionLocal() as db:
        assert db.query(Project).filter(Project.owner_id == ids['owner'], Project.deleted_at.is_(None)).count() == 0


def test_access_expiry_is_rechecked_after_owner_lookup(client, auth_headers, monkeypatch):
    from datetime import datetime, timezone
    from app.api import deps

    class AfterLockWait:
        @staticmethod
        def now(tz=None):
            return datetime.now(tz or timezone.utc) + timedelta(days=1)

    # JWT verification happens before the owner lock. Advance only the
    # post-query authorization clock to simulate expiry during contention.
    monkeypatch.setattr(deps, 'datetime', AfterLockWait)
    response = client.post('/api/v1/projects', headers=auth_headers, json={'name': 'Expired while waiting'})
    assert response.status_code == 401
    with SessionLocal() as db:
        assert db.query(Project).count() == 0
