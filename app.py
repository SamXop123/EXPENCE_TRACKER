from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import json
import requests
import time


_RATES_CACHE = {
    "timestamp": 0,
    "rates": {}
}

CACHE_TTL = 60 * 60  # 1 hour

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  

def get_db_connection():
    conn = sqlite3.connect('expenses.db')
    conn.row_factory = sqlite3.Row
    return conn
def _fetch_usd_rates():
    url = "https://api.exchangerate-api.com/v4/latest/USD"
    response = requests.get(url, timeout=5)
    data = response.json()

    if "rates" not in data:
        raise ValueError(f"Invalid API response: {data}")

    return data["rates"]


def get_usd_rate(currency):
    if currency == "USD":
        return 1.0

    now = time.time()

    # Refresh cache if expired
    if (
        not _RATES_CACHE["rates"]
        or now - _RATES_CACHE["timestamp"] > CACHE_TTL
    ):
        try:
            _RATES_CACHE["rates"] = _fetch_usd_rates()
            _RATES_CACHE["timestamp"] = now
        except Exception as e:
            print("Rate fetch error:", e)
            return 1.0  # safe fallback

    rates = _RATES_CACHE["rates"]

    if currency not in rates:
        print(f"Unsupported currency: {currency}")
        return 1.0

    return float(rates[currency])


def convert_to_usd(amount, currency):
    rate = get_usd_rate(currency)
    return round(amount / rate, 2)

def convert_from_usd(amount_usd, currency):
    rate = get_usd_rate(currency)
    return round(amount_usd * rate, 2)


def init_db():

    

    conn = get_db_connection()
    

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
        amount_usd REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
               currency TEXT NOT NULL DEFAULT 'USD',
        amount_usd REAL NOT NULL,
            period TEXT NOT NULL DEFAULT 'monthly',
            start_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute(
       'CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)'
    )
    conn.execute(
      'CREATE INDEX IF NOT EXISTS idx_expenses_user_category ON expenses(user_id, category)'
    )
    conn.execute(
    'CREATE INDEX IF NOT EXISTS idx_budgets_user ON budgets(user_id)'
    )
    conn.commit()
    conn.close()

# Routes
@app.route('/set_currency', methods=['POST'])
def set_currency():
    currency = request.form.get('currency')
    if currency:
        session['currency'] = currency
    return redirect(url_for('dashboard'))
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
        conn = get_db_connection()
        
        try:
            hashed_password = generate_password_hash(password)
            conn.execute(
                'INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                (username, email, hashed_password)
            )
            conn.commit()
            flash('Account created successfully! Please login.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username or email already exists!')
        finally:
            conn.close()
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ?', (username,)
        ).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully!')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials!')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully!')
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    display_currency = session.get('currency', 'INR')

    # ================= TOTAL EXPENSES =================
    total_expenses_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ?',
        (session['user_id'],)
    ).fetchone()[0]

    total_expenses = convert_from_usd(total_expenses_usd, display_currency)

    # ================= MONTHLY EXPENSES =================
    current_month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')

    monthly_expenses_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ? AND date >= ?',
        (session['user_id'], current_month_start)
    ).fetchone()[0]

    monthly_expenses = convert_from_usd(monthly_expenses_usd, display_currency)

    # ================= RECENT EXPENSES =================
    rows = conn.execute('''
        SELECT * FROM expenses
        WHERE user_id = ?
        ORDER BY date DESC
        LIMIT 5
    ''', (session['user_id'],)).fetchall()

    recent_expenses = []
    for row in rows:
        exp = dict(row)
        exp['amount_display'] = convert_from_usd(
            exp['amount_usd'], display_currency
        )
        recent_expenses.append(exp)

    # ================= BUDGETS =================
    budgets = conn.execute(
        'SELECT * FROM budgets WHERE user_id = ?',
        (session['user_id'],)
    ).fetchall()

    total_budget_usd = 0
    total_budget_spent_usd = 0
    budget_alerts = []

    for budget in budgets:
        budget_amount_usd = float(budget['amount_usd'])
        total_budget_usd += budget_amount_usd

        # Period dates
        if budget['period'] == 'monthly':
            start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            start_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        else:  # yearly
            start_date = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')

        end_date = datetime.now().strftime('%Y-%m-%d')

        actual_spending_usd = conn.execute(
            '''
            SELECT COALESCE(SUM(amount_usd), 0) FROM expenses
            WHERE user_id = ? AND category = ? AND date BETWEEN ? AND ?
            ''',
            (session['user_id'], budget['category'], start_date, end_date)
        ).fetchone()[0]

        total_budget_spent_usd += actual_spending_usd

        percentage_used = (
            actual_spending_usd / budget_amount_usd * 100
            if budget_amount_usd > 0 else 0
        )

        remaining_usd = budget_amount_usd - actual_spending_usd

        if percentage_used >= 80:
            budget_alerts.append({
                'category': budget['category'],
                'status': 'exceeded' if percentage_used >= 100 else 'warning',
                'percentage': round(percentage_used, 1),
                'remaining': convert_from_usd(remaining_usd, display_currency)
            })

    conn.close()

    total_budget = convert_from_usd(total_budget_usd, display_currency)
    total_budget_spent = convert_from_usd(total_budget_spent_usd, display_currency)

    return render_template(
        'dashboard.html',
        total_expenses=total_expenses,
        monthly_expenses=monthly_expenses,
        total_budget=total_budget,
        total_budget_spent=total_budget_spent,
        
       
        recent_expenses=recent_expenses,
        
        
        budget_alerts=budget_alerts,
        currency=display_currency
    )


@app.route('/expenses')
def expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    expenses_list = conn.execute('''
        SELECT * FROM expenses 
        WHERE user_id = ? 
        ORDER BY date DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()
    
    return render_template('expenses.html', expenses=expenses_list)

@app.route('/add_expense', methods=['GET', 'POST'])
def add_expense():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        amount = float(request.form['amount'])
        category = request.form['category']
        currency = request.form['currency']
        description = request.form['description']
        date = request.form['date'] or datetime.now().strftime('%Y-%m-%d')

        amount_usd = convert_to_usd(amount, currency)

        conn = get_db_connection()
        conn.execute(
            'INSERT INTO expenses (user_id, amount,currency,amount_usd, category, description, date) VALUES (?, ?, ?, ?, ?,?,?)',
            (session['user_id'], amount,currency,amount_usd, category, description, date)
        )
        conn.commit()
        conn.close()
        
        flash('Expense added successfully!')
        return redirect(url_for('expenses'))
    
    return render_template('add_expense.html')

@app.route('/edit_expense/<int:expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()

    if request.method == 'POST':
        amount = float(request.form['amount'])
        currency = request.form.get('currency')
        category = request.form['category']
        description = request.form['description']
        date = request.form['date'] or datetime.now().strftime('%Y-%m-%d')

       
        amount_usd = convert_to_usd(amount, currency)


        conn.execute(
            '''
            UPDATE expenses
            SET amount = ?, currency = ?, amount_usd = ?, category = ?, description = ?, date = ?
            WHERE id = ? AND user_id = ?
            ''',
            (amount, currency, amount_usd, category, description, date, expense_id, session['user_id'])
        )

        conn.commit()
        conn.close()

        flash('Expense updated successfully!')
        return redirect(url_for('expenses'))

    # ================= GET =================
    expense = conn.execute(
        'SELECT * FROM expenses WHERE id = ? AND user_id = ?',
        (expense_id, session['user_id'])
    ).fetchone()
    conn.close()

    if not expense:
        flash('Expense not found!')
        return redirect(url_for('expenses'))

    return render_template(
        'edit_expense.html',
        expense=expense,
        selected_currency=expense['currency']  
    )


@app.route('/delete_expense/<int:expense_id>')
def delete_expense(expense_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute(
        'DELETE FROM expenses WHERE id = ? AND user_id = ?',
        (expense_id, session['user_id'])
    )
    conn.commit()
    conn.close()
    
    flash('Expense deleted successfully!')
    return redirect(url_for('expenses'))
@app.route('/analytics')
def analytics():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()

    display_currency = session.get('currency', 'INR')

    # ================= DAILY SPENDING (LAST 7 DAYS) =================
    end_date = datetime.now()
    start_date = end_date - timedelta(days=6)

    daily_data = []
    labels = []

    for i in range(7):
        current_date = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        labels.append((start_date + timedelta(days=i)).strftime('%b %d'))

        total_usd = conn.execute(
            '''
            SELECT COALESCE(SUM(amount_usd), 0)
            FROM expenses
            WHERE user_id = ? AND date = ?
            ''',
            (session['user_id'], current_date)
        ).fetchone()[0]

        daily_data.append(
            convert_from_usd(total_usd, display_currency)
        )

    
    categories_data = conn.execute(
        '''
        SELECT category, COALESCE(SUM(amount_usd), 0) AS total_usd
        FROM expenses
        WHERE user_id = ?
        GROUP BY category
        ''',
        (session['user_id'],)
    ).fetchall()

    category_labels = [row['category'] for row in categories_data]
    category_totals = [
        convert_from_usd(row['total_usd'], display_currency)
        for row in categories_data
    ]

    conn.close()

    return render_template(
        'analytics.html',
        labels=json.dumps(labels),
        daily_data=json.dumps(daily_data),
        category_labels=json.dumps(category_labels),
        category_totals=json.dumps(category_totals),
        currency=display_currency
    )




@app.route('/budgets')
def budgets():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()

    display_currency = session.get('currency', 'INR')

    budgets_list = conn.execute(
        'SELECT * FROM budgets WHERE user_id = ? ORDER BY category',
        (session['user_id'],)
    ).fetchall()

    budgets_with_spending = []

    for budget in budgets_list:
        budget_dict = dict(budget)

        budget_amount_usd = float(budget['amount_usd'])

        # Period dates
        if budget['period'] == 'monthly':
            start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            start_date = (
                datetime.now() - timedelta(days=datetime.now().weekday())
            ).strftime('%Y-%m-%d')
        else:  # yearly
            start_date = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')

        end_date = datetime.now().strftime('%Y-%m-%d')

        actual_spending_usd = conn.execute(
            '''
            SELECT COALESCE(SUM(amount_usd), 0)
            FROM expenses
            WHERE user_id = ? AND category = ? AND date BETWEEN ? AND ?
            ''',
            (session['user_id'], budget['category'], start_date, end_date)
        ).fetchone()[0]

        remaining_usd = budget_amount_usd - actual_spending_usd

        percentage_used = (
            actual_spending_usd / budget_amount_usd * 100
            if budget_amount_usd > 0 else 0
        )

        # Convert values for display (use conversion functions directly)
        budget_dict['actual_spending'] = convert_from_usd(
            actual_spending_usd, display_currency
        )
        budget_dict['remaining'] = convert_from_usd(
            remaining_usd, display_currency
        )
        budget_dict['amount'] = convert_from_usd(
            budget_amount_usd, display_currency
        )
        budget_dict['percentage_used'] = round(percentage_used, 1)

        budgets_with_spending.append(budget_dict)

    conn.close()

    return render_template(
        'budgets.html',
        budgets=budgets_with_spending,
        currency=display_currency
    )



@app.route('/add_budget', methods=['GET', 'POST'])
def add_budget():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        category = request.form['category']
        amount = float(request.form['amount'])
        currency = request.form['currency']
        period = request.form['period']
        start_date = request.form['start_date'] or datetime.now().strftime('%Y-%m-%d')

        amount_usd = convert_to_usd(amount, currency)

        conn = get_db_connection()

        existing = conn.execute(
            '''
            SELECT * FROM budgets
            WHERE user_id = ? AND category = ? AND period = ?
            ''',
            (session['user_id'], category, period)
        ).fetchone()

        if existing:
            flash('Budget already exists for this category and period!')
            conn.close()
            return render_template('add_budget.html')

        conn.execute(
            '''
            INSERT INTO budgets
            (user_id, category, amount, currency, amount_usd, period, start_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (session['user_id'], category, amount, currency, amount_usd, period, start_date)
        )

        conn.commit()
        conn.close()

        flash('Budget added successfully!')
        return redirect(url_for('budgets'))

    return render_template('add_budget.html')


@app.route('/edit_budget/<int:budget_id>', methods=['GET', 'POST'])
def edit_budget(budget_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    
    if request.method == 'POST':
        amount = float(request.form['amount'])
        currency = request.form.get('currency')
        period = request.form['period']
        start_date = request.form['start_date'] or datetime.now().strftime('%Y-%m-%d')

        amount_usd = convert_to_usd(amount, currency)

        conn.execute(
            '''
            UPDATE budgets
            SET amount = ?, currency = ?, amount_usd = ?, period = ?, start_date = ?
            WHERE id = ? AND user_id = ?
            ''',
            (amount, currency, amount_usd, period, start_date, budget_id, session['user_id'])
        )
        conn.commit()
        conn.close()

        flash('Budget updated successfully!')
        return redirect(url_for('budgets'))

    # =============== GET ===============
    budget = conn.execute(
        'SELECT * FROM budgets WHERE id = ? AND user_id = ?',
        (budget_id, session['user_id'])
    ).fetchone()
    conn.close()

    if not budget:
        flash('Budget not found!')
        return redirect(url_for('budgets'))

    return render_template(
        'edit_budget.html',
        budget=budget,
        selected_currency=budget['currency']  
    )


@app.route('/delete_budget/<int:budget_id>')
def delete_budget(budget_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute(
        'DELETE FROM budgets WHERE id = ? AND user_id = ?',
        (budget_id, session['user_id'])
    )
    conn.commit()
    conn.close()
    
    flash('Budget deleted successfully!')
    return redirect(url_for('budgets'))

if __name__ == '__main__':
    init_db()
    try:
        _RATES_CACHE["rates"] = _fetch_usd_rates()
        _RATES_CACHE["timestamp"] = time.time()
    except Exception:
        pass
    app.run(debug=False)
