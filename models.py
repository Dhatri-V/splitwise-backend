from sqlalchemy import Column, Integer, String, Float
from database import Base


class ExpenseDB(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    amount = Column(Float)
    paid_by = Column(String)