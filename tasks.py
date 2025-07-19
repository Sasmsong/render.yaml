import os
import shutil
import subprocess
import tempfile
from celery import Celery
from git import Repo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Column, String, DateTime, Enum, func
from sqlalchemy.orm import declarative_base

# ------------- Celery setup -------------
celery = Celery(
    "tasks",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379"),
)

# ------------- database setup (same as api.py) -------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tmp.db")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class BuildJob(Base):
    __tablename__ = "build_jobs"
    id = Column(String, primary_key=True)
    repo_url = Column(String, nullable=False)
    package_name = Column(String)
    status = Column(Enum("queued", "running", "success", "failed", name="job_status"), default="queued")
    created_at = Column(DateTime, server_default=func.now())
    apk_url = Column(String)

Base.metadata.create_all(bind=engine)

# ------------- Celery task -------------
@celery.task(bind=True)
def build_apk_task(self, job_id: str, repo_url: str, package_name: str, keystore_pwd: str):
    db = SessionLocal()
    job = db.query(BuildJob).get(job_id)
    if job:
        job.status = "running"
        db.commit()

    workdir = tempfile.mkdtemp(prefix="build_")
    try:
        # 1. clone repo
        Repo.clone_from(repo_url, workdir)

        # 2. create dummy keystore
        keystore_path = os.path.join(workdir, "keystore.jks")
        subprocess.check_call(
            [
                "keytool", "-genkey", "-v", "-keystore", keystore_path,
                "-alias", "upload", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                "-dname", "CN=code2phone, OU=Dev, O=Org, L=City, S=State, C=US",
                "-storepass", keystore_pwd, "-keypass", keystore_pwd,
            ]
        )

        # 3. build APK
        subprocess.check_call(["./gradlew", "assembleRelease"], cwd=workdir)

        # 4. locate APK
        apk_path = None
        for root, _, files in os.walk(os.path.join(workdir, "app", "build", "outputs", "apk")):
            for f in files:
                if f.endswith(".apk"):
                    apk_path = os.path.join(root, f)
                    break
        if not apk_path:
            raise RuntimeError("APK not found after build")

        # 5. move to artifacts folder served by nginx
        artifact_dir = "/artifacts"
        os.makedirs(artifact_dir, exist_ok=True)
        final_name = f"{job_id}.apk"
        shutil.copy(apk_path, os.path.join(artifact_dir, final_name))

        # 6. update DB
        job.status = "success"
        job.apk_url = f"https://www.code2phone.com/artifacts/{final_name}"
        db.commit()

    except Exception as e:
        job.status = "failed"
        db.commit()
        raise e
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
