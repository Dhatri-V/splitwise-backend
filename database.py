import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base


# Overridable for tests; the defaults are unchanged (SQLite file and its built-in 5-second busy timeout).
DATABASE_URL = os.environ.get("SPLITWISE_DATABASE_URL", "sqlite:///./splitwise.db")


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": float(os.environ.get("SPLITWISE_SQLITE_TIMEOUT", "5"))}
)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


Base = declarative_base()