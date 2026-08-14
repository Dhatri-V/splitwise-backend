from fastapi import FastAPI
from pydantic import BaseModel
from database import engine
from models import Base
from database import SessionLocal
from models import ExpenseDB
from fastapi.middleware.cors import CORSMiddleware


class Expense(BaseModel):
    
    title:str
    amount:float
    paid_by:str

app=FastAPI()
Base.metadata.create_all(bind=engine)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    return{"message":"splitwise backend API"}


@app.get("/expenses")
def get_expenses():

    db = SessionLocal()

    expenses = db.query(ExpenseDB).all()

    db.close()

    return expenses


@app.get("/expenses/{expense_id}")
def get_expense(expense_id: int):

    db = SessionLocal()

    expense = db.query(ExpenseDB).filter(
        ExpenseDB.id == expense_id
    ).first()

    db.close()

    return expense


@app.post("/expenses")
def create_expenses(expense:Expense):
    db = SessionLocal()

    db_expense = ExpenseDB(

        title=expense.title,

        amount=expense.amount,

        paid_by=expense.paid_by

    )

    db.add(db_expense)

    db.commit()

    db.refresh(db_expense)

    db.close()
    return db_expense



@app.put("/expenses/{expense_id}")
def update_expense(expense_id: int, expense: Expense):

    db = SessionLocal()

    db_expense = db.query(ExpenseDB).filter(
        ExpenseDB.id == expense_id
    ).first()

    db_expense.title = expense.title
    db_expense.amount = expense.amount
    db_expense.paid_by = expense.paid_by

    db.commit()
    db.refresh(db_expense)

    db.close()

    return db_expense

@app.delete("/expenses/{expense_id}")
def delete_expense(expense_id: int):

    db = SessionLocal()

    db_expense = db.query(ExpenseDB).filter(
        ExpenseDB.id == expense_id
    ).first()

    db.delete(db_expense)
    db.commit()

    db.close()

    return {"message": "Expense deleted successfully"}