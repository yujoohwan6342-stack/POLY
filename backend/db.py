"""DB 엔진 + 세션 helper."""
from sqlmodel import SQLModel, create_engine, Session
from . import config

engine = create_engine(config.DATABASE_URL, echo=False, connect_args={"check_same_thread": False})


def init_db():
    from . import models  # 모델 등록을 위해 import 필요
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
