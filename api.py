from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from celery.result import AsyncResult
from sqlalchemy import create_engine, Column, String, DateTime, Enum, func
from sqlalchemy.orm import declarative_base, sessionmaker
import uuid
import os

# ------------- database setup -------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tmp.db")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class BuildJob(Base):
    __tablename__ = "build_jobs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_url = Column(String, nullable=False)
    package_name = Column(String)
    status = Column(Enum("queued", "running", "success", "failed", name="job_status"), default="queued")
    created_at = Column(DateTime, server_default=func.now())
    apk_url = Column(String)

Base.metadata.create_all(bind=engine)

# ------------- FastAPI setup -------------
app = FastAPI()

class BuildRequest(BaseModel):
    repo_url: str
    package_name: str = "com.example.app"
    keystore_password: str = "android"

@app.post("/build")
def create_build(req: BuildRequest):
    db = SessionLocal()
    job = BuildJob(repo_url=req.repo_url, package_name=req.package_name)
    db.add(job)
    db.commit()
    db.refresh(job)

    # hand job to Celery
    from tasks import build_apk_task
    task = build_apk_task.delay(job.id, req.repo_url, req.package_name, req.keystore_password)
    job.id = task.id
    db.commit()
    return {"job_id": task.id}

@app.get("/status/{job_id}")
def job_status(job_id: str):
    res = AsyncResult(job_id)
    db = SessionLocal()
    job = db.query(BuildJob).filter(BuildJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "job not found")
    return {"status": res.status, "apk_url": job.apk_url}
