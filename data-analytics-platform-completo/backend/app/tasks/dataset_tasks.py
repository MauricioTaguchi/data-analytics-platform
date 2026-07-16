from pathlib import Path
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.services.dataset_service import DatasetService
from app.core.cache import CacheService
from app.worker import celery_app

@celery_app.task(name="datasets.profile")
def profile_dataset_task(dataset_id: int):
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            return {"status": "not_found", "dataset_id": dataset_id}

        dataset.status = "processing"
        db.commit()

        profile = DatasetService.build_profile(dataset)
        dataset.profile_json = profile
        dataset.status = "profiled"
        db.commit()

        CacheService.set_json(f"dataset:{dataset.id}:profile", profile, ttl=1800)
        return {"status": "completed", "dataset_id": dataset.id}
    except Exception as exc:
        if "dataset" in locals() and dataset:
            dataset.status = "failed"
            db.commit()
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()
