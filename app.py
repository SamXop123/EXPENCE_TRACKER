from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import json
import requests
import time
import os
import io
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from groq import Groq

# --- CONFIGURATION ---
app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Change to random key

_RATES_CACHE = {
    "timestamp": 0,
    "rates": {}
}
CACHE_TTL = 60 * 60  # 1 hour

# Initialize Groq Client (Ensure API Key is set)
# Ideally, use os.environ.get("GROQ_API_KEY")
groq_client = Groq(
    api_key="YOUR_GROQ_API_KEY_HERE" 
)

# --- DATABASE HELPERS ---
def get_db_connection():
    conn = sqlite3.connect('expenses.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    
    # Users Table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    
    # Expenses Table (with multi-currency support)
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
    
    # Budgets Table
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
    
    # Indexes
    conn.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_category ON expenses(user_id, category)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_budgets_user ON budgets(user_id)')
    
    conn.commit()
    conn.close()

# --- CURRENCY HELPERS ---
def _fetch_usd_rates():
    try:
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        response = requests.get(url, timeout=5)
        data = response.json()
        if "rates" not in data:
            raise ValueError(f"Invalid API response: {data}")
        return data["rates"]
    except Exception as e:
        print(f"Rate fetch error: {e}")
        return {}

def get_usd_rate(currency):
    if currency == "USD":
        return 1.0
    
    now = time.time()
    if not _RATES_CACHE["rates"] or now - _RATES_CACHE["timestamp"] > CACHE_TTL:
        _RATES_CACHE["rates"] = _fetch_usd_rates()
        _RATES_CACHE["timestamp"] = now
    
    rates = _RATES_CACHE["rates"]
    return float(rates.get(currency, 1.0))

def convert_to_usd(amount, currency):
    rate = get_usd_rate(currency)
    return round(amount / rate, 2)

def convert_from_usd(amount_usd, currency):
    rate = get_usd_rate(currency)
    return round(amount_usd * rate, 2)

# --- ROUTES ---

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
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
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
    user_id = session['user_id']

    # Total Expenses
    total_expenses_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ?', 
        (user_id,)
    ).fetchone()[0]
    total_expenses = convert_from_usd(total_expenses_usd, display_currency)

    # Monthly Expenses
    current_month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    monthly_expenses_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ? AND date >= ?',
        (user_id, current_month_start)
    ).fetchone()[0]
    monthly_expenses = convert_from_usd(monthly_expenses_usd, display_currency)

    # Recent Expenses
    rows = conn.execute(
        'SELECT * FROM expenses WHERE user_id = ? ORDER BY date DESC LIMIT 5',
        (user_id,)
    ).fetchall()
    
    recent_expenses = []
    for row in rows:
        exp = dict(row)
        exp['amount_display'] = convert_from_usd(exp['amount_usd'], display_currency)
        recent_expenses.append(exp)

    # Budgets
    budgets = conn.execute('SELECT * FROM budgets WHERE user_id = ?', (user_id,)).fetchall()
    total_budget_usd = 0
    total_budget_spent_usd = 0
    budget_alerts = []

    for budget in budgets:
        budget_amount_usd = float(budget['amount_usd'])
        total_budget_usd += budget_amount_usd

        # Determine start date based on period
        if budget['period'] == 'monthly':
            start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            start_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        else: # yearly
            start_date = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')
        
        end_date = datetime.now().strftime('%Y-%m-%d')

        actual_spending_usd = conn.execute(
            '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
               WHERE user_id = ? AND category = ? AND date BETWEEN ? AND ?''',
            (user_id, budget['category'], start_date, end_date)
        ).fetchone()[0]

        total_budget_spent_usd += actual_spending_usd
        
        percentage = (actual_spending_usd / budget_amount_usd * 100) if budget_amount_usd > 0 else 0
        remaining_usd = budget_amount_usd - actual_spending_usd

        if percentage >= 80:
            budget_alerts.append({
                'category': budget['category'],
                'status': 'exceeded' if percentage >= 100 else 'warning',
                'percentage': round(percentage, 1),
                'remaining': convert_from_usd(remaining_usd, display_currency)
            })

    conn.close()
    
    total_budget = convert_from_usd(total_budget_usd, display_currency)
    total_budget_spent = convert_from_usd(total_budget_spent_usd, display_currency)

    return render_template('dashboard.html',
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
    expenses_list = conn.execute(
        'SELECT * FROM expenses WHERE user_id = ? ORDER BY date DESC', 
        (session['user_id'],)
    ).fetchall()
    conn.close()
    
    # Optional: Convert amounts for display if needed inside template
    # For now, passing raw list (template handles logic or shows stored value)
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
            '''INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) 
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (session['user_id'], amount, currency, amount_usd, category, description, date)
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
            '''UPDATE expenses SET amount=?, currency=?, amount_usd=?, category=?, description=?, date=? 
               WHERE id=? AND user_id=?''',
            (amount, currency, amount_usd, category, description, date, expense_id, session['user_id'])
        )
        conn.commit()
        conn.close()
        flash('Expense updated successfully!')
        return redirect(url_for('expenses'))

    expense = conn.execute(
        'SELECT * FROM expenses WHERE id=? AND user_id=?', (expense_id, session['user_id'])
    ).fetchone()
    conn.close()

    if not expense:
        flash('Expense not found!')
        return redirect(url_for('expenses'))

    return render_template('edit_expense.html', expense=expense, selected_currency=expense['currency'])

# ✅ BUG FIXED HERE: delete_expense (Unreachable code issue resolved)
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

    # Daily Spending (Last 7 Days)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=6)
    daily_data = []
    labels = []

    for i in range(7):
        current_date = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        labels.append((start_date + timedelta(days=i)).strftime('%b %d'))
        
        total_usd = conn.execute(
            'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND date=?',
            (session['user_id'], current_date)
        ).fetchone()[0]
        daily_data.append(convert_from_usd(total_usd, display_currency))

    # Category Breakdown
    categories_data = conn.execute(
        '''SELECT category, COALESCE(SUM(amount_usd), 0) as total_usd 
           FROM expenses WHERE user_id=? GROUP BY category''',
        (session['user_id'],)
    ).fetchall()

    category_labels = [row['category'] for row in categories_data]
    category_totals = [convert_from_usd(row['total_usd'], display_currency) for row in categories_data]

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
    budgets_list = conn.execute('SELECT * FROM budgets WHERE user_id=? ORDER BY category', (session['user_id'],)).fetchall()
    
    budgets_with_spending = []
    for budget in budgets_list:
        b_dict = dict(budget)
        amount_usd = float(budget['amount_usd'])
        
        # Period start date
        if budget['period'] == 'monthly':
            start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            start_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        else:
            start_date = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')

        actual_usd = conn.execute(
            '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
               WHERE user_id=? AND category=? AND date BETWEEN ? AND ?''',
            (session['user_id'], budget['category'], start_date, end_date)
        ).fetchone()[0]

        b_dict['actual_spending'] = convert_from_usd(actual_usd, display_currency)
        b_dict['remaining'] = convert_from_usd(amount_usd - actual_usd, display_currency)
        b_dict['amount'] = convert_from_usd(amount_usd, display_currency)
        b_dict['percentage_used'] = round((actual_usd / amount_usd * 100) if amount_usd > 0 else 0, 1)
        budgets_with_spending.append(b_dict)

    conn.close()
    return render_template('budgets.html', budgets=budgets_with_spending, currency=display_currency)

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
            'SELECT * FROM budgets WHERE user_id=? AND category=? AND period=?',
            (session['user_id'], category, period)
        ).fetchone()

        if existing:
            flash('Budget already exists for this category and period!')
            conn.close()
            return render_template('add_budget.html')

        conn.execute(
            '''INSERT INTO budgets (user_id, category, amount, currency, amount_usd, period, start_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
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
        start_date = request.form['start_date']
        amount_usd = convert_to_usd(amount, currency)

        conn.execute(
            '''UPDATE budgets SET amount=?, currency=?, amount_usd=?, period=?, start_date=? 
               WHERE id=? AND user_id=?''',
            (amount, currency, amount_usd, period, start_date, budget_id, session['user_id'])
        )
        conn.commit()
        conn.close()
        flash('Budget updated successfully!')
        return redirect(url_for('budgets'))

    budget = conn.execute('SELECT * FROM budgets WHERE id=? AND user_id=?', (budget_id, session['user_id'])).fetchone()
    conn.close()
    if not budget:
        flash('Budget not found!')
        return redirect(url_for('budgets'))

    return render_template('edit_budget.html', budget=budget, selected_currency=budget['currency'])

@app.route('/delete_budget/<int:budget_id>')
def delete_budget(budget_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute('DELETE FROM budgets WHERE id=? AND user_id=?', (budget_id, session['user_id']))
    conn.commit()
    conn.close()
    flash('Budget deleted successfully!')
    return redirect(url_for('budgets'))

@app.route('/export/<string:data_type>/<string:format>')
def export_data(data_type, format):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    conn = get_db_connection()
    
    if data_type == 'expenses':
        query = 'SELECT date, category, description, amount, currency, amount_usd FROM expenses WHERE user_id = ? ORDER BY date DESC'
        filename = f"expenses_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    elif data_type == 'budgets':
        query = 'SELECT category, amount, currency, amount_usd, period, start_date FROM budgets WHERE user_id = ? ORDER BY category'
        filename = f"budgets_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        return "Invalid data type", 400

    df = pd.read_sql_query(query, conn, params=(user_id,))
    conn.close()

    if format == 'csv':
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"{filename}.csv"
        )
    
    elif format == 'xlsx':
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=data_type.capitalize())
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"{filename}.xlsx"
        )
    
    elif format == 'pdf':
        output = io.BytesIO()
        doc = SimpleDocTemplate(output, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        
        # Title
        elements.append(Paragraph(f"{data_type.capitalize()} Report", styles['Title']))
        elements.append(Paragraph(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
        elements.append(Paragraph("<br/><br/>", styles['Normal']))
        
        # Table
        data = [df.columns.tolist()] + df.values.tolist()
        t = Table(data)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        elements.append(t)
        doc.build(elements)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"{filename}.pdf"
        )
    
    return "Invalid format", 400

@app.route('/import_expenses', methods=['GET', 'POST'])
def import_expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        
        if file and file.filename.endswith('.csv'):
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            df = pd.read_csv(stream)
            
            # Save DF to session temporarily (not ideal for large files, but works for this demo)
            # Better to save to a temp file or use a more robust state management
            session['import_df'] = df.to_json()
            return render_template('import_mapping.html', columns=df.columns.tolist())
            
    return render_template('import_expenses.html')

@app.route('/process_import', methods=['POST'])
def process_import():
    if 'user_id' not in session or 'import_df' not in session:
        return redirect(url_for('login'))
    
    mapping = request.form.to_dict()
    df_json = session.pop('import_df')
    df = pd.read_json(io.StringIO(df_json))
    
    conn = get_db_connection()
    try:
        for _, row in df.iterrows():
            amount = float(row[mapping['amount']])
            currency = row[mapping['currency']] if 'currency' in mapping and mapping['currency'] in row else 'USD'
            category = row[mapping['category']] if 'category' in mapping and mapping['category'] in row else 'Miscellaneous'
            description = row[mapping['description']] if 'description' in mapping and mapping['description'] in row else ''
            date = row[mapping['date']] if 'date' in mapping and mapping['date'] in row else datetime.now().strftime('%Y-%m-%d')
            
            amount_usd = convert_to_usd(amount, currency)
            
            conn.execute(
                '''INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) 
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (session['user_id'], amount, currency, amount_usd, category, description, str(date))
            )
        conn.commit()
        flash('Expenses imported successfully!')
    except Exception as e:
        conn.rollback()
        flash(f'Error importing expenses: {str(e)}')
    finally:
        conn.close()
        
    return redirect(url_for('expenses'))

@app.route('/bulk_delete_expenses', methods=['POST'])
def bulk_delete_expenses():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    expense_ids = request.json.get('ids', [])
    if not expense_ids:
        return jsonify({'success': False, 'error': 'No expenses selected'}), 400
    
    conn = get_db_connection()
    conn.execute(
        f"DELETE FROM expenses WHERE user_id = ? AND id IN ({','.join(['?']*len(expense_ids))})",
        (session['user_id'], *expense_ids)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/bulk_update_category', methods=['POST'])
def bulk_update_category():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    data = request.json
    expense_ids = data.get('ids', [])
    new_category = data.get('category')
    
    if not expense_ids or not new_category:
        return jsonify({'success': False, 'error': 'Missing data'}), 400
    
    conn = get_db_connection()
    conn.execute(
        f"UPDATE expenses SET category = ? WHERE user_id = ? AND id IN ({','.join(['?']*len(expense_ids))})",
        (new_category, session['user_id'], *expense_ids)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

# --- CHATBOT LOGIC ---
def get_user_financial_context(user_id):
    conn = get_db_connection()
    
    total_usd = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    
    month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    monthly_usd = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ? AND date >= ?",
        (user_id, month_start)
    ).fetchone()[0]

    categories = conn.execute(
        "SELECT category, SUM(amount_usd) as total FROM expenses WHERE user_id = ? GROUP BY category",
        (user_id,)
    ).fetchall()
    
    budgets = conn.execute("SELECT category, amount_usd FROM budgets WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()

    return {
        "total_expenses_usd": round(total_usd, 2),
        "monthly_expenses_usd": round(monthly_usd, 2),
        "categories": {row["category"]: round(row["total"], 2) for row in categories},
        "budgets": {row["category"]: round(row["amount_usd"], 2) for row in budgets},
    }

@app.route('/chatbot', methods=['POST'])
def chatbot():
    if 'user_id' not in session:
        return {"reply": "Unauthorized"}, 401

    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return {"reply": "Please enter a message."}

    context = get_user_financial_context(session['user_id'])
    system_prompt = f"""
    You are a personal finance assistant.
    User data (USD):
    - Total expenses: {context['total_expenses_usd']}
    - Monthly expenses: {context['monthly_expenses_usd']}
    - Category totals: {context['categories']}
    - Budgets: {context['budgets']}
    Rules: Do not invent data. Answer clearly.
    """

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.4,
            max_tokens=300
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        print(e)
        return {"reply": "AI service error. Try again later."}

if __name__ == '__main__':
    init_db()
    # Pre-fetch rates
    try:
        _RATES_CACHE["rates"] = _fetch_usd_rates()
        _RATES_CACHE["timestamp"] = time.time()
    except Exception:
        pass
    app.run(debug=True)