import { useEffect, useState } from "react"
import "./App.css"

const API_URL = "http://127.0.0.1:8000"
const emptyForm = { title: "", amount: "", paid_by: "" }

function App() {
  const [expenses, setExpenses] = useState([])
  const [form, setForm] = useState(emptyForm)
  const [editingId, setEditingId] = useState(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    async function loadExpenses() {
      try {
        const response = await fetch(`${API_URL}/expenses`)

        if (!response.ok) {
          throw new Error("Could not load expenses.")
        }

        const data = await response.json()
        setExpenses(data)
      } catch (err) {
        setError(err.message)
      } finally {
        setIsLoading(false)
      }
    }

    loadExpenses()
  }, [])

  function handleChange(event) {
    const { name, value } = event.target
    setForm((currentForm) => ({ ...currentForm, [name]: value }))
  }

  function resetForm() {
    setForm(emptyForm)
    setEditingId(null)
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError("")
    setIsSaving(true)

    const expenseData = {
      title: form.title.trim(),
      amount: Number(form.amount),
      paid_by: form.paid_by.trim(),
    }

    const isEditing = editingId !== null
    const url = isEditing
      ? `${API_URL}/expenses/${editingId}`
      : `${API_URL}/expenses`

    try {
      // The same form sends POST for a new expense and PUT while editing.
      const response = await fetch(url, {
        method: isEditing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(expenseData),
      })

      if (!response.ok) {
        throw new Error(`Could not ${isEditing ? "update" : "add"} the expense.`)
      }

      const savedExpense = await response.json()

      if (isEditing) {
        setExpenses((currentExpenses) =>
          currentExpenses.map((expense) =>
            expense.id === editingId ? savedExpense : expense,
          ),
        )
      } else {
        setExpenses((currentExpenses) => [...currentExpenses, savedExpense])
      }

      resetForm()
    } catch (err) {
      setError(err.message)
    } finally {
      setIsSaving(false)
    }
  }

  function startEditing(expense) {
    setEditingId(expense.id)
    setForm({
      title: expense.title,
      amount: String(expense.amount),
      paid_by: expense.paid_by,
    })
    setError("")
    window.scrollTo({ top: 0, behavior: "smooth" })
  }

  async function deleteExpense(expenseId) {
    setError("")

    try {
      const response = await fetch(`${API_URL}/expenses/${expenseId}`, {
        method: "DELETE",
      })

      if (!response.ok) {
        throw new Error("Could not delete the expense.")
      }

      setExpenses((currentExpenses) =>
        currentExpenses.filter((expense) => expense.id !== expenseId),
      )

      if (editingId === expenseId) {
        resetForm()
      }
    } catch (err) {
      setError(err.message)
    }
  }

  const total = expenses.reduce((sum, expense) => sum + expense.amount, 0)

  return (
    <main className="page-shell">
      <section className="app-card">
        <header className="app-header">
          <div>
            <p className="eyebrow">Shared expenses</p>
            <h1>Splitwise</h1>
            <p className="subtitle">Track expenses with friends, without the fuss.</p>
          </div>

          <div className="total-card">
            <span>Total spent</span>
            <strong>₹{total.toFixed(2)}</strong>
          </div>
        </header>

        <section className="form-section">
          <div className="section-heading">
            <h2>{editingId !== null ? "Edit expense" : "Add an expense"}</h2>
            {editingId !== null && (
              <button className="text-button" type="button" onClick={resetForm}>
                Cancel edit
              </button>
            )}
          </div>

          <form className="expense-form" onSubmit={handleSubmit}>
            <label>
              Expense title
              <input
                name="title"
                type="text"
                value={form.title}
                onChange={handleChange}
                placeholder="For example, Dinner"
                required
              />
            </label>

            <label>
              Amount
              <input
                name="amount"
                type="number"
                min="0.01"
                step="0.01"
                value={form.amount}
                onChange={handleChange}
                placeholder="0.00"
                required
              />
            </label>

            <label>
              Paid by
              <input
                name="paid_by"
                type="text"
                value={form.paid_by}
                onChange={handleChange}
                placeholder="Friend's name"
                required
              />
            </label>

            <button className="primary-button" type="submit" disabled={isSaving}>
              {isSaving
                ? "Saving..."
                : editingId !== null
                  ? "Save changes"
                  : "Add expense"}
            </button>
          </form>
        </section>

        {error && <p className="error-message">{error}</p>}

        <section className="expenses-section">
          <div className="section-heading">
            <h2>Expenses</h2>
            <span className="expense-count">
              {expenses.length} {expenses.length === 1 ? "expense" : "expenses"}
            </span>
          </div>

          {isLoading ? (
            <p className="status-message">Loading expenses...</p>
          ) : expenses.length === 0 ? (
            <div className="empty-state">
              <span>₹</span>
              <h3>No expenses yet</h3>
              <p>Add your first shared expense using the form above.</p>
            </div>
          ) : (
            <div className="expense-list">
              {expenses.map((expense) => (
                <article className="expense-item" key={expense.id}>
                  <div className="expense-icon">
                    {expense.title.charAt(0).toUpperCase()}
                  </div>
                  <div className="expense-details">
                    <h3>{expense.title}</h3>
                    <p>Paid by {expense.paid_by}</p>
                  </div>
                  <strong className="expense-amount">
                    ₹{expense.amount.toFixed(2)}
                  </strong>
                  <div className="expense-actions">
                    <button type="button" onClick={() => startEditing(expense)}>
                      Edit
                    </button>
                    <button
                      className="delete-button"
                      type="button"
                      onClick={() => deleteExpense(expense.id)}
                    >
                      Delete
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </section>
    </main>
  )
}

export default App
