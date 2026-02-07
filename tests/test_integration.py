import pytest
import json
import pyotp
import io
from datetime import datetime, timedelta
import app as flask_app
from unittest.mock import patch, MagicMock

def test_auth_to_expense_lifecycle(client):
    # 1. Signup
    resp = client.post('/signup', data={
        "username": "lifecycle_user",
        "email": "life@test.com",
        "password": "Password123!",
        "confirm_password": "Password123!"
    }, follow_redirects=True)
    assert "Please set up Two-Factor Authentication" in resp.text
    
    # Get secret from DB
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        user = conn.execute("SELECT totp_secret FROM users WHERE username='lifecycle_user'").fetchone()
        secret = user['totp_secret']
        conn.close()
    
    # 2. Setup 2FA (implicitly done by signup redirecting to setup_2fa)
    # 3. Verify 2FA
    totp = pyotp.TOTP(secret)
    token = totp.now()
    resp = client.post('/verify_2fa', data={"token": token}, follow_redirects=True)
    assert "Logged in successfully" in resp.text
    
    # 4. Add Expense
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "100",
            "currency": "USD",
            "category": "Food",
            "description": "Lifecycle Expense",
            "date": "2026-02-07"
        }, follow_redirects=True)
    
    # 5. Check Dashboard
    resp = client.get('/dashboard')
    assert "100.0" in resp.text
    assert "Food" in resp.text

def test_login_flow(client):
    # Create user first
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        hashed = flask_app.generate_password_hash("pass")
        secret = pyotp.random_base32()
        conn.execute("INSERT INTO users (username, email, password, totp_secret) VALUES (?, ?, ?, ?)",
                     ("login_user", "login@test.com", hashed, secret))
        conn.commit()
        conn.close()
    
    # Login Step 1
    resp = client.post('/login', data={"username": "login_user", "password": "pass"}, follow_redirects=True)
    assert "verify_2fa" in resp.request.url
    
    # Login Step 2 (2FA)
    totp = pyotp.TOTP(secret)
    resp = client.post('/verify_2fa', data={"token": totp.now()}, follow_redirects=True)
    assert "dashboard" in resp.request.url

@patch('app.groq_client')
def test_chatbot_mock(mock_groq, client, auth_token):
    # Mock Groq response
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content="Save more money!"))]
    mock_groq.chat.completions.create.return_value = mock_completion
    
    # Chatbot uses session, so we need to log in first to set session
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'testuser'
    
    response = client.post('/chatbot', json={"message": "How am I doing?"})
    
    assert response.status_code == 200
    assert "Save more money!" in response.get_json()['reply']

def test_not_found_errors(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Non-existent expense
    resp = client.get('/edit_expense/999', follow_redirects=True)
    assert "Expense not found" in resp.text
    
    # Non-existent budget
    resp = client.get('/edit_budget/999', follow_redirects=True)
    assert "Budget not found" in resp.text
    
    # Non-existent category
    resp = client.get('/edit_category/999', follow_redirects=True)
    assert "Category not found" in resp.text
    
    # Non-existent group
    resp = client.get('/group/999', follow_redirects=True)
    assert "Group not found" in resp.text

def test_group_management_edge_cases(client):
    # 1. Create a real user for the session
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute("INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                     ("group_admin", "admin@test.com", "pass"))
        conn.commit()
        conn.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'group_admin'
    
    # Create group
    client.post('/create_group', data={"name": "Edge Case Group"}, follow_redirects=True)
    
    # Add non-existent user - now creates a Ghost user
    # This user will have ID 2
    resp = client.post('/group/1/add_member', data={"username": "ghost_user"}, follow_redirects=True)
    assert 'Created new user' in resp.text
    assert 'ghost_user' in resp.text
    
    # Settle up with non-existent users
    resp = client.post('/group/1/settle_up', data={
        "from_id": "999",
        "to_id": "888",
        "amount": "10"
    }, follow_redirects=True)
    assert resp.status_code == 200 # Should land back on groups or group detail page

def test_category_management(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add Category
    client.post('/add_category', data={
        "name": "Travel",
        "icon": "✈️",
        "color": "#0000ff"
    }, follow_redirects=True)
    
    # Check if category exists
    resp = client.get('/categories')
    assert "Travel" in resp.text
    
    # Edit Category
    client.post('/edit_category/1', data={
        "name": "Business Travel",
        "icon": "💼",
        "color": "#ff0000"
    }, follow_redirects=True)
    
    resp = client.get('/categories')
    assert "Business Travel" in resp.text
    
    # Delete Category
    client.post('/delete_category/1', follow_redirects=True)
    resp = client.get('/categories')
    assert "Business Travel" not in resp.text

def test_budget_management(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add Budget
    client.post('/add_budget', data={
        "category": "Food",
        "amount": "500",
        "currency": "USD",
        "period": "monthly",
        "start_date": "2026-02-01"
    }, follow_redirects=True)
    
    resp = client.get('/budgets')
    assert "Food" in resp.text
    assert "500" in resp.text
    
    # Edit Budget
    client.post('/edit_budget/1', data={
        "category": "Food",
        "amount": "600",
        "currency": "USD",
        "period": "monthly",
        "start_date": "2026-02-01"
    }, follow_redirects=True)
    
    resp = client.get('/budgets')
    assert "600" in resp.text
    
    # Delete Budget
    client.get('/delete_budget/1', follow_redirects=True)
    resp = client.get('/budgets')
    assert "Food" not in resp.text

def test_bulk_actions(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add some expenses
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={"amount": "10", "currency": "USD", "category": "Food", "description": "E1", "date": "2026-02-01"})
        client.post('/add_expense', data={"amount": "20", "currency": "USD", "category": "Food", "description": "E2", "date": "2026-02-02"})
    
    # Bulk Update Category
    client.post('/bulk_update_category', data={
        "expense_ids": ["1", "2"],
        "new_category": "Shopping"
    }, follow_redirects=True)
    
    resp = client.get('/expenses')
    assert "Shopping" in resp.text
    
    # Bulk Delete
    client.post('/bulk_delete_expenses', data={
        "expense_ids": ["1", "2"]
    }, follow_redirects=True)
    
    resp = client.get('/expenses')
    assert "E1" not in resp.text
    assert "E2" not in resp.text

def test_activity_log(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Perform an action to generate log (Expense)
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "100",
            "currency": "USD",
            "category": "Food",
            "description": "LogTestExpense",
            "date": "2026-02-07"
        }, follow_redirects=True)
    
    resp = client.get('/activity_log')
    assert "LogTestExpense" in resp.text
    assert "Expense" in resp.text
    
    # Activity log clear route seems to be missing in app.py? 
    # Let me check if /activity_log/clear exists

def test_search_expenses(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={"amount": "50", "currency": "USD", "category": "Food", "description": "SearchMe", "date": "2026-02-01"})
    
    # Search by description
    resp = client.get('/expenses?search=SearchMe')
    assert "SearchMe" in resp.text
    
    # Search by category
    resp = client.get('/expenses?category=Food')
    assert "SearchMe" in resp.text
    
    # Search by date range
    resp = client.get('/expenses?start_date=2026-02-01&end_date=2026-02-01')
    assert "SearchMe" in resp.text

def test_analytics(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add some data for analytics
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={"amount": "100", "currency": "USD", "category": "Food", "description": "A1", "date": "2026-02-01"})
        client.post('/add_expense', data={"amount": "200", "currency": "USD", "category": "Shopping", "description": "A2", "date": "2026-02-01"})
        
        # Add budget to cover budget performance logic in analytics
        client.post('/add_budget', data={
            "category": "Food",
            "amount": "50",
            "currency": "USD",
            "period": "monthly",
            "start_date": "2026-02-01"
        })
    
    # Check analytics page with different ranges
    resp = client.get('/analytics?range=7')
    assert "Analytics" in resp.text
    assert "Food" in resp.text
    
    # This should trigger budget performance logic
    assert "Food" in resp.text 
    
    resp = client.get('/analytics?range=30')
    assert resp.status_code == 200
    
    resp = client.get('/analytics?range=custom&from=2026-02-01&to=2026-02-07')
    assert resp.status_code == 200

def test_category_management_edge_cases(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # 1. Add category with empty name
    resp = client.post('/add_category', data={"name": ""}, follow_redirects=True)
    assert 'Category name is required!' in resp.text

    # 2. Add duplicate category
    client.post('/add_category', data={"name": "Food"}, follow_redirects=True)
    resp = client.post('/add_category', data={"name": "Food"}, follow_redirects=True)
    assert 'A category with this name already exists!' in resp.text

    # 3. Edit category with empty name
    resp = client.post('/edit_category/1', data={"name": ""}, follow_redirects=True)
    assert 'Category name is required!' in resp.text

    # 4. Edit non-existent category
    resp = client.get('/edit_category/999', follow_redirects=True)
    assert 'Category not found!' in resp.text

    # 5. Delete non-existent category
    resp = client.get('/delete_category/999', follow_redirects=True)
    assert 'Category not found!' in resp.text

def test_budget_management_edge_cases(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # 1. Add budget
    client.post('/add_budget', data={
        "category": "Food",
        "amount": "100",
        "currency": "USD",
        "period": "monthly",
        "start_date": "2026-02-01"
    }, follow_redirects=True)

    # 2. Add duplicate budget
    resp = client.post('/add_budget', data={
        "category": "Food",
        "amount": "200",
        "currency": "USD",
        "period": "monthly",
        "start_date": "2026-02-01"
    }, follow_redirects=True)
    assert 'Budget already exists for this category and period!' in resp.text

    # 3. Edit non-existent budget
    resp = client.get('/edit_budget/999', follow_redirects=True)
    assert 'Budget not found!' in resp.text

    # 4. Delete non-existent budget
    resp = client.get('/delete_budget/999', follow_redirects=True)
    assert 'Budget not found!' in resp.text

def test_export_import(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Export
    resp = client.get('/export/csv')
    assert resp.status_code == 200
    assert "text/csv" in resp.headers['Content-Type']
    
    # Import (Mocking file upload)
    csv_content = "date,category,amount,currency,description\n2026-02-07,Food,10.5,USD,Imported"
    data = {
        'file': (io.BytesIO(csv_content.encode()), 'test.csv')
    }
    resp = client.post('/import', data=data, content_type='multipart/form-data', follow_redirects=True)
    assert "Imported 1 expenses" in resp.text

def test_group_management_full(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
    
    # Create Group
    client.post('/create_group', data={"name": "Full Group"}, follow_redirects=True)
    
    # Add Member
    client.post('/group/1/add_member', data={"username": "member1"}, follow_redirects=True)
    
    # Add Group Expense
    client.post('/group/1/add_expense', data={
        "description": "Pizza",
        "amount": "30",
        "payer_id": "1"
    }, follow_redirects=True)
    
    # Check Group Detail
    resp = client.get('/group/1')
    assert "Pizza" in resp.text
    assert "30" in resp.text
    
    # Settle Up
    client.post('/group/1/settle_up', data={
        "from_id": "2",
        "to_id": "1",
        "amount": "15"
    }, follow_redirects=True)
    
    # Delete Group Expense (Covers 1830)
    resp = client.post('/group/1/expense/1/delete', follow_redirects=True)
    assert 'Expense deleted' in resp.text

def test_expense_management_extended(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # 1. Add Expense
    client.post('/add_expense', data={
        "amount": "50",
        "currency": "USD",
        "category": "Food",
        "description": "Lunch",
        "date": "2026-02-07"
    }, follow_redirects=True)
    
    # Get Expense ID
    with client.application.app_context():
        from app import get_db_connection
        conn = get_db_connection()
        expense = conn.execute("SELECT id FROM expenses WHERE user_id=1").fetchone()
        expense_id = expense['id']
        conn.close()

    # 2. GET Edit Expense (Covers 1501-1505)
    resp = client.get(f'/edit_expense/{expense_id}')
    assert resp.status_code == 200
    assert "Lunch" in resp.text

    # 3. POST Edit Expense
    client.post(f'/edit_expense/{expense_id}', data={
        "amount": "60",
        "currency": "USD",
        "category": "Food",
        "description": "Dinner",
        "date": "2026-02-07"
    }, follow_redirects=True)

    # 4. Delete Expense (Covers 1509-1521)
    resp = client.get(f'/delete_expense/{expense_id}', follow_redirects=True)
    assert 'Expense deleted successfully!' in resp.text

    # 5. Edit non-existent expense
    resp = client.get('/edit_expense/999', follow_redirects=True)
    assert 'Expense not found!' in resp.text

def test_analytics_periods(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add weekly budget
    client.post('/add_budget', data={
        "category": "Food",
        "amount": "100",
        "currency": "USD",
        "period": "weekly",
        "start_date": "2026-02-01"
    }, follow_redirects=True)

    # Add yearly budget
    client.post('/add_budget', data={
        "category": "Travel",
        "amount": "1000",
        "currency": "USD",
        "period": "yearly",
        "start_date": "2026-01-01"
    }, follow_redirects=True)

    # Access analytics to trigger period logic (Covers 1586-1589)
    resp = client.get('/analytics')
    assert resp.status_code == 200

def test_group_edge_cases(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
    
    # 1. Access non-existent group (Covers 1645-1647)
    resp = client.get('/group/999', follow_redirects=True)
    assert 'Group not found' in resp.text

    # 2. Add non-existent member to group (Covers 1731)
    # First create a group
    client.post('/create_group', data={"name": "Edge Group"}, follow_redirects=True)
    
    # Get the ID of the new group
    with client.application.app_context():
        from app import get_db_connection
        conn = get_db_connection()
        group = conn.execute("SELECT id FROM groups WHERE name='Edge Group'").fetchone()
        group_id = group['id']
        conn.close()

    resp = client.post(f'/group/{group_id}/add_member', data={"username": "nonexistent_user"}, follow_redirects=True)
    assert 'Created new user' in resp.text
    assert 'nonexistent_user' in resp.text

    # 3. Settle up in non-existent group (Covers 1806-1809)
    resp = client.post('/group/999/settle_up', data={
        "from_id": "1",
        "to_id": "2",
        "amount": "10"
    }, follow_redirects=True)
    assert 'Group not found' in resp.text

def test_api_endpoints(client):
    # Get JWT Token
    # First ensure a user exists
    client.post('/signup', data={
        "username": "api_user",
        "email": "api@test.com",
        "password": "password123",
        "confirm_password": "password123"
    })
    
    resp = client.post('/api/auth/login', json={
        "username": "api_user",
        "password": "password123"
    })
    token = resp.json['data']['token']
    headers = {'Authorization': f'Bearer {token}'}

    # 1. API Add Expense (Covers 2395-2399)
    resp = client.post('/api/expenses', json={
        "amount": 100,
        "currency": "USD",
        "category": "Food",
        "description": "API Expense"
    }, headers=headers)
    assert resp.status_code == 201

    # 2. API Get Expenses (Covers 2382)
    resp = client.get('/api/expenses', headers=headers)
    assert resp.status_code == 200
    assert len(resp.json['data']) >= 1

    # 3. API Get Categories (Covers 2413)
    resp = client.get('/api/categories', headers=headers)
    assert resp.status_code == 200

    # 4. API Get Budgets (Covers 2442-2449)
    resp = client.get('/api/budgets', headers=headers)
    assert resp.status_code == 200

def test_auth_edge_cases(client):
    # 1. Signup existing user (Covers 979-983)
    client.post('/signup', data={
        "username": "admin",
        "email": "admin@example.com",
        "password": "password123",
        "confirm_password": "password123"
    })
    resp = client.post('/signup', data={
        "username": "admin",
        "email": "admin2@example.com",
        "password": "password123",
        "confirm_password": "password123"
    }, follow_redirects=True)
    assert 'Username already exists!' in resp.text

    # 2. Login wrong password (Covers 1009-1010)
    resp = client.post('/login', data={
        "username": "admin",
        "password": "wrongpassword"
    }, follow_redirects=True)
    assert 'Invalid credentials!' in resp.text

    # 3. Verify 2FA wrong OTP (Covers 1045, 1075-1076)
    with client.session_transaction() as sess:
        sess['pre_2fa_id'] = 1
    resp = client.post('/verify_2fa', data={"token": "000000"}, follow_redirects=True)
    assert 'Invalid 2FA Token' in resp.text

    # 4. Logout (Covers 1082-1084)
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    resp = client.get('/logout', follow_redirects=True)
    assert 'Logged out successfully!' in resp.text

def test_recurring_expenses(client):
    # 1. Create a real user for the session with totp_secret
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute("INSERT INTO users (username, email, password, totp_secret) VALUES (?, ?, ?, ?)",
                     ("recurring_user", "recurring@test.com", "pass", "JBSWY3DPEHPK3PXP"))
        conn.commit()
        conn.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add recurring expense
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "50",
            "currency": "USD",
            "category": "Bills",
            "description": "Netflix",
            "date": "2026-01-01",
            "is_recurring": "on",
            "frequency": "monthly"
        }, follow_redirects=True)
    
    # Trigger recurring process
    with client.session_transaction() as sess:
        sess.pop('user_id', None)
        sess['pre_2fa_id'] = 1
    
    # Mocking TOTP to pass 2FA
    with patch('pyotp.TOTP') as mock_totp:
        mock_totp.return_value.verify.return_value = True
        client.post('/verify_2fa', data={"token": "123456"}, follow_redirects=True)
    
    # Check if a new expense was created
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        rows = conn.execute("SELECT description FROM expenses").fetchall()
        count = 0
        for row in rows:
            if flask_app.decrypt_data(row['description']) == 'Netflix':
                count += 1
        conn.close()
    
    assert count >= 2

def test_api_full_lifecycle(client):
    # 1. API Signup
    resp = client.post('/api/auth/signup', json={
        "username": "api_user",
        "email": "api@test.com",
        "password": "password123"
    })
    assert resp.status_code == 201

    # 2. API Login to get token
    resp = client.post('/api/auth/login', json={
        "username": "api_user",
        "password": "password123"
    })
    assert resp.status_code == 200
    token = resp.get_json()['data']['token']
    auth_headers = {'Authorization': f'Bearer {token}'}

    # 3. Add Expense
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.post('/api/expenses', headers=auth_headers, json={
            "amount": 50,
            "currency": "USD",
            "category": "Food",
            "description": "API Expense",
            "date": "2026-02-07"
        })
        assert resp.status_code == 201
        expense_id = resp.get_json()['data']['id']

    # 4. Get Expenses
    resp = client.get('/api/expenses', headers=auth_headers)
    assert resp.status_code == 200
    assert any(e['id'] == expense_id for e in resp.get_json()['data'])

    # 5. Update Expense
    resp = client.put(f'/api/expenses/{expense_id}', headers=auth_headers, json={
        "amount": 75,
        "description": "API Expense Updated"
    })
    assert resp.status_code == 200

    # 6. Add Budget
    resp = client.post('/api/budgets', headers=auth_headers, json={
        "category": "Food",
        "amount": 500,
        "currency": "USD",
        "period": "monthly"
    })
    assert resp.status_code == 201
    budget_id = resp.get_json()['data']['id']

    # 7. Get Budgets
    resp = client.get('/api/budgets', headers=auth_headers)
    assert resp.status_code == 200
    assert any(b['id'] == budget_id for b in resp.get_json()['data'])

    # 8. Get Categories
    resp = client.get('/api/categories', headers=auth_headers)
    assert resp.status_code == 200

    # 9. Delete Expense
    resp = client.delete(f'/api/expenses/{expense_id}', headers=auth_headers)
    assert resp.status_code == 200

def test_search_expenses(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add some expenses
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={"amount": "100", "currency": "USD", "category": "Food", "description": "Pizza", "date": "2026-02-01"})
        client.post('/add_expense', data={"amount": "200", "currency": "USD", "category": "Rent", "description": "Office", "date": "2026-02-05"})
    
    # Search by keyword
    resp = client.get('/search_expenses?keyword=Pizza')
    assert 'Pizza' in resp.text
    assert 'Office' not in resp.text

    # Search by category
    resp = client.get('/search_expenses?categories=Food')
    assert 'Pizza' in resp.text
    assert 'Office' not in resp.text

    # Search by amount range
    resp = client.get('/search_expenses?amount_min=150')
    assert 'Office' in resp.text
    assert 'Pizza' not in resp.text

    # Search by date range
    resp = client.get('/search_expenses?date_from=2026-02-04')
    assert 'Office' in resp.text
    assert 'Pizza' not in resp.text

    # Test sorting
    resp = client.get('/search_expenses?sort_by=amount&sort_order=asc')
    assert resp.text.find('Pizza') < resp.text.find('Office')

def test_group_deletion_and_expense_deletion(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'lifecycle_user'
    
    # 1. Create group
    client.post('/create_group', data={"name": "Delete Group"}, follow_redirects=True)
    
    # 2. Add member (Ghost)
    client.post('/group/1/add_member', data={"username": "ghost_member"}, follow_redirects=True)

    # 3. Add group expense
    client.post('/group/1/add_expense', data={
        "amount": "100",
        "description": "Pizza Party"
    }, follow_redirects=True)

    # 4. Try delete expense as non-payer (not possible here as user 1 is payer)
    # 5. Delete expense
    client.post('/group/1/expense/1/delete', follow_redirects=True)
    
    # 6. Try delete group as non-creator
    with client.session_transaction() as sess:
        sess['user_id'] = 2 # ghost_member
    
    resp = client.post('/group/1/delete', follow_redirects=True)
    assert 'Only the group creator can delete this group.' in resp.text

    # 7. Delete group as creator
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    resp = client.post('/group/1/delete', follow_redirects=True)
    assert 'Group deleted successfully!' in resp.text

def test_delete_category_with_expenses(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # 1. Add category and an expense for it
    client.post('/add_category', data={"name": "Travel"}, follow_redirects=True)
    
    # Get the ID of the new category
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        cat_id = conn.execute("SELECT id FROM categories WHERE name='Travel'").fetchone()[0]
        conn.close()

    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "100",
            "currency": "USD",
            "category": "Travel",
            "description": "Flight",
            "date": "2026-02-01"
        }, follow_redirects=True)
    
    # 2. Try to delete the category
    resp = client.get(f'/delete_category/{cat_id}', follow_redirects=True)
    # print(resp.text) # For debugging if needed
    assert 'Cannot delete' in resp.text
    assert 'Travel' in resp.text
    assert '1 expense(s)' in resp.text

def test_dashboard_budget_alerts(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['currency'] = 'USD'
    
    # 1. Add budget (80% warning)
    client.post('/add_budget', data={
        "category": "Food",
        "amount": "100",
        "currency": "USD",
        "period": "monthly",
        "start_date": "2026-02-01"
    }, follow_redirects=True)
    
    # 2. Add expense (90 USD)
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "90",
            "currency": "USD",
            "category": "Food",
            "description": "Groceries",
            "date": datetime.now().strftime('%Y-%m-%d')
        }, follow_redirects=True)
    
    # 3. Check dashboard for warning
    resp = client.get('/dashboard')
    assert '90.0% of budget used' in resp.text

    # 4. Add expense to exceed budget
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "20",
            "currency": "USD",
            "category": "Food",
            "description": "Dinner",
            "date": datetime.now().strftime('%Y-%m-%d')
        }, follow_redirects=True)
    
    # 5. Check dashboard for exceeded status
    resp = client.get('/dashboard')
    assert '110.0% used' in resp.text

def test_bulk_actions(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # 1. Add expenses
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={"amount": "10", "currency": "USD", "category": "Food", "description": "E1", "date": "2026-02-01"})
        client.post('/add_expense', data={"amount": "20", "currency": "USD", "category": "Food", "description": "E2", "date": "2026-02-01"})
    
    # 2. Bulk update category
    resp = client.post('/bulk_update_category', json={
        "ids": [1, 2],
        "category": "Dining"
    })
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    # 3. Verify update
    resp = client.get('/expenses')
    assert 'Dining' in resp.text
    assert 'Food' not in resp.text

    # 4. Bulk delete
    resp = client.post('/bulk_delete_expenses', json={"ids": [1, 2]})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    # 5. Verify delete
    resp = client.get('/expenses')
    assert 'E1' not in resp.text
    assert 'E2' not in resp.text

def test_process_import(client):
    import pandas as pd
    import io
    
    # 1. Prepare data
    df = pd.DataFrame({
        'val': [10.5, 20.0],
        'cur': ['USD', 'USD'],
        'cat': ['Food', 'Rent'],
        'desc': ['Imp1', 'Imp2'],
        'dt': ['2026-02-01', '2026-02-02']
    })
    df_json = df.to_json()
    
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['import_df'] = df_json
    
    # 2. Post mapping
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.post('/process_import', data={
            "amount": "val",
            "currency": "cur",
            "category": "cat",
            "description": "desc",
            "date": "dt"
        }, follow_redirects=True)
    
    assert 'Expenses imported successfully!' in resp.text
    assert 'Imp1' in resp.text
    assert 'Imp2' in resp.text

def test_api_unauthorized(client):
    response = client.get('/api/expenses')
    assert response.status_code == 401
    assert "Token is missing" in response.get_json()['message']

def test_web_routes(client):
    # Test public routes
    assert client.get('/').status_code in [200, 302]
    assert client.get('/login').status_code == 200
    assert client.get('/signup').status_code == 200

def test_session_auth_redirects(client):
    # Test private routes redirect to login when no session
    routes = ['/dashboard', '/expenses', '/budgets', '/analytics', '/groups']
    for route in routes:
        response = client.get(route)
        assert response.status_code == 302
        assert '/login' in response.location

def test_full_web_lifecycle(client):
    # 1. Signup
    client.post('/signup', data={
        "username": "webuser",
        "email": "web@example.com",
        "password": "password123"
    })
    
    # After signup, user is in pre_2fa state. Let's bypass 2FA for this test by setting user_id in session
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'webuser'
    
    # 3. Access Dashboard
    dash_resp = client.get('/dashboard')
    assert dash_resp.status_code == 200
    
    # 4. Add Expense via Web
    with patch('app.get_usd_rate', return_value=1.0):
        add_resp = client.post('/add_expense', data={
            "amount": "100",
            "currency": "USD",
            "category": "Food",
            "description": "Web Lunch",
            "date": "2026-02-07"
        }, follow_redirects=True)
    assert add_resp.status_code == 200
    assert b"Expense added successfully" in add_resp.data

def test_2fa_verification(client):
    # 1. Create user with known TOTP secret
    secret = pyotp.random_base32()
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute('INSERT INTO users (username, email, password, totp_secret) VALUES (?, ?, ?, ?)',
                     ('2fauser', '2fa@test.com', 'hash', secret))
        user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        conn.close()
    
    # 2. Set pre_2fa_id in session
    with client.session_transaction() as sess:
        sess['pre_2fa_id'] = user_id
    
    # 3. Verify with correct token
    totp = pyotp.TOTP(secret)
    token = totp.now()
    
    response = client.post('/verify_2fa', data={'token': token}, follow_redirects=True)
    assert response.status_code == 200
    with client.session_transaction() as sess:
        assert sess['user_id'] == user_id


def test_category_management(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add
    client.post('/add_category', data={
        "name": "Gym",
        "icon": "🏋️",
        "color": "#FF0000"
    }, follow_redirects=True)
    
    # Edit
    client.post('/edit_category/1', data={
        "name": "Health & Fitness",
        "icon": "💪",
        "color": "#00FF00"
    }, follow_redirects=True)
    
    # Delete
    client.post('/delete_category/1', follow_redirects=True)
    
    resp = client.get('/categories')
    assert resp.status_code == 200

def test_expense_management(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['currency'] = 'USD'
    
    # Add
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "50",
            "currency": "USD",
            "category": "Food",
            "description": "Lunch",
            "date": "2026-02-07"
        }, follow_redirects=True)
    
    # Edit
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/edit_expense/1', data={
            "amount": "60",
            "currency": "USD",
            "category": "Food",
            "description": "Dinner",
            "date": "2026-02-07"
        }, follow_redirects=True)
    
    # Delete
    client.post('/delete_expense/1', follow_redirects=True)
    
    resp = client.get('/expenses')
    assert resp.status_code == 200

def test_budget_management(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Add
    client.post('/add_budget', data={
        "category": "Food",
        "amount": "500",
        "currency": "USD",
        "period": "monthly"
    }, follow_redirects=True)
    
    # Edit
    client.post('/edit_budget/1', data={
        "category": "Food",
        "amount": "600",
        "currency": "USD",
        "period": "monthly"
    }, follow_redirects=True)
    
    # Delete
    client.post('/delete_budget/1', follow_redirects=True)
    
    resp = client.get('/budgets')
    assert resp.status_code == 200

def test_export_import(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Export
    assert client.get('/export/expenses/csv').status_code == 200
    
    # Import
    data = {
        'file': (io.BytesIO(b"date,category,description,amount,currency\n2026-02-07,Food,Lunch,10,USD"), 'test.csv')
    }
    resp = client.post('/import_expenses', data=data, content_type='multipart/form-data', follow_redirects=True)
    assert resp.status_code == 200

def test_process_import(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        # Set import_df in session
        df_json = json.dumps([{"dt": "2026-02-07", "cat": "Food", "desc": "Lunch", "amt": 10, "cur": "USD"}])
        sess['import_df'] = df_json
    
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.post('/process_import', data={
            "date": "dt",
            "category": "cat",
            "description": "desc",
            "amount": "amt",
            "currency": "cur"
        }, follow_redirects=True)
    
    assert resp.status_code == 200
    assert b"Expenses imported successfully" in resp.data

def test_bulk_actions(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Create some expenses
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute("INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) VALUES (1, 10, 'USD', 10, 'Food', 'e1', '2026-02-07')")
        conn.execute("INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) VALUES (1, 20, 'USD', 20, 'Food', 'e2', '2026-02-07')")
        conn.commit()
        ids = [row['id'] for row in conn.execute("SELECT id FROM expenses").fetchall()]
        conn.close()

    # Bulk update category
    client.post('/bulk_update_category', json={"ids": ids, "category": "Travel"})
    
    # Bulk delete
    client.post('/bulk_delete_expenses', json={"ids": ids})

def test_bulk_actions_errors(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Empty IDs
    client.post('/bulk_update_category', json={"ids": [], "category": "Travel"})
    client.post('/bulk_delete_expenses', json={"ids": []})
    
    # Create some expenses
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute("INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) VALUES (1, 10, 'USD', 10, 'Food', 'e1', '2026-02-07')")
        conn.execute("INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) VALUES (1, 20, 'USD', 20, 'Food', 'e2', '2026-02-07')")
        conn.commit()
        ids = [row['id'] for row in conn.execute("SELECT id FROM expenses").fetchall()]
        conn.close()

    # Bulk update category
    client.post('/bulk_update_category', json={"ids": ids, "category": "Travel"})
    
    # Bulk delete
    client.post('/bulk_delete_expenses', json={"ids": ids})

def test_api_lifecycle(client):
    # 1. API Signup
    resp = client.post('/api/auth/signup', json={
        "username": "apiuser",
        "email": "api@test.com",
        "password": "apipassword"
    })
    assert resp.status_code == 201
    
    # 2. API Login
    resp = client.post('/api/auth/login', json={
        "username": "apiuser",
        "password": "apipassword"
    })
    assert resp.status_code == 200
    token = resp.get_json()['data']['token']
    headers = {"Authorization": f"Bearer {token}"}
    
    # 3. API Add Expense
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.post('/api/expenses', json={
            "amount": 50,
            "currency": "USD",
            "category": "Food",
            "description": "API Expense",
            "date": "2026-02-07"
        }, headers=headers)
    assert resp.status_code == 201
    expense_id = resp.get_json()['data']['id']
    
    # 4. API Get Expenses
    resp = client.get('/api/expenses', headers=headers)
    assert resp.status_code == 200
    assert any(e['id'] == expense_id for e in resp.get_json()['data'])
    
    # 5. API Update Expense
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.put(f'/api/expenses/{expense_id}', json={
            "amount": 75,
            "description": "Updated API Expense"
        }, headers=headers)
    assert resp.status_code == 200
    
    # 6. API Delete Expense
    resp = client.delete(f'/api/expenses/{expense_id}', headers=headers)
    assert resp.status_code == 200
    
    # 7. API Budget CRUD
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.post('/api/budgets', json={
            "category": "Food",
            "amount": 500,
            "currency": "USD"
        }, headers=headers)
    assert resp.status_code == 201
    
    resp = client.get('/api/budgets', headers=headers)
    assert resp.status_code == 200
    assert len(resp.get_json()['data']) == 1

    # 8. API Categories
    resp = client.get('/api/categories', headers=headers)
    assert resp.status_code == 200
    
    resp = client.post('/api/categories', json={
        "name": "NewCat",
        "icon": "💰"
    }, headers=headers)
    assert resp.status_code == 201

    # 9. API Groups
    resp = client.post('/api/groups', json={"name": "APIGroup"}, headers=headers)
    assert resp.status_code == 201
    
    resp = client.get('/api/groups', headers=headers)
    assert resp.status_code == 200
    assert len(resp.get_json()['data']) == 1

def test_web_additional_features(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'testuser'
    
    # Category management
    client.post('/add_category', data={"name": "NewWebCat", "icon": "💰", "color": "#000000"}, follow_redirects=True)
    client.post('/edit_category/1', data={"name": "UpdatedWebCat", "icon": "💰", "color": "#111111"}, follow_redirects=True)
    client.post('/delete_category/1', follow_redirects=True)
    
    # Budget management
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_budget', data={"category": "Food", "amount": "1000", "currency": "USD", "period": "monthly"}, follow_redirects=True)
    client.post('/delete_budget/1', follow_redirects=True)
    
    # Currency
    client.post('/set_currency', data={"currency": "EUR"}, follow_redirects=True)

def test_analytics_and_chatbot(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['currency'] = 'USD'
    
    # Analytics
    assert client.get('/analytics').status_code == 200
    assert client.get('/analytics?range=30').status_code == 200
    
    # Chatbot
    with patch('app.groq_client') as mock_groq:
        mock_groq.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="Advice"))])
        resp = client.post('/chatbot', json={"message": "budget tip"})
        assert resp.status_code == 200
        assert "Advice" in resp.get_json()['reply']

def test_group_management(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Create
    client.post('/create_group', data={"name": "Test Group"}, follow_redirects=True)
    
    # View Group
    resp = client.get('/group/1')
    assert resp.status_code == 200
    
    # Add Member (Existing user)
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute("INSERT INTO users (username, email, password) VALUES (?, ?, ?)", ("otheruser", "other@test.com", "hash"))
        conn.commit()
        conn.close()

    client.post('/group/1/add_member', data={"username": "otheruser"}, follow_redirects=True)
    
    # Add Member (Ghost user - auto creation)
    client.post('/group/1/add_member', data={"username": "ghostuser"}, follow_redirects=True)

    # Add Group Expense
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/group/1/add_expense', data={
            "amount": "120",
            "description": "Group Pizza",
            "payer_id": "1"
        }, follow_redirects=True)
    
    # Settle Up (Ghost user pays back)
    # Ghost user should be ID 3 (1: testuser, 2: otheruser, 3: ghostuser)
    client.post('/group/1/settle_up', data={
        "from_id": "3",
        "to_id": "1",
        "amount": "40"
    }, follow_redirects=True)

    # Delete Group Expense
    client.post('/group/1/expense/1/delete', follow_redirects=True)

    # Delete Group
    client.post('/group/1/delete', follow_redirects=True)
    
    resp = client.get('/groups')
    assert resp.status_code == 200

def test_export_formats(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Valid formats
    assert client.get('/export/expenses/xlsx').status_code == 200
    assert client.get('/export/expenses/pdf').status_code == 200
    assert client.get('/export/budgets/csv').status_code == 200
    
    # Invalid data type
    resp = client.get('/export/invalid/csv')
    assert resp.status_code == 400
    
    # Invalid format
    resp = client.get('/export/expenses/txt')
    assert resp.status_code == 400

def test_import_validation(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # No file
    resp = client.post('/import_expenses', data={}, follow_redirects=True)
    assert b"No file part" in resp.data
    
    # Empty filename
    data = {'file': (io.BytesIO(b""), '')}
    resp = client.post('/import_expenses', data=data, content_type='multipart/form-data', follow_redirects=True)
    assert b"No selected file" in resp.data

def test_chatbot_edge_cases(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Empty message
    resp = client.post('/chatbot', json={"message": ""})
    assert "Please enter a message" in resp.get_json()['reply']
    
    # Groq Error
    with patch('app.groq_client') as mock_groq:
        mock_groq.chat.completions.create.side_effect = Exception("API Down")
        resp = client.post('/chatbot', json={"message": "hello"})
        assert "AI service error" in resp.get_json()['reply']

def test_group_access_denied(client):
    # Create group by user 1
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        now = flask_app.datetime.now()
        conn.execute("INSERT INTO groups (id, name, created_by, created_at) VALUES (99, 'Private', 1, ?)", (now,))
        conn.execute("INSERT INTO group_members (group_id, user_id, joined_at) VALUES (99, 1, ?)", (now,))
        conn.commit()
        conn.close()
    
    # User 2 tries to access
    with client.session_transaction() as sess:
        sess['user_id'] = 2
    
    resp = client.get('/group/99', follow_redirects=True)
    assert "not a member" in resp.text

def test_search_expenses_filters(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['currency'] = 'USD'
    
    # Add some test expenses
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        conn.execute("INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) VALUES (1, 10, 'USD', 10, 'Food', ?, '2026-01-01')", (flask_app.encrypt_data("Coffee"),))
        conn.execute("INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) VALUES (1, 50, 'USD', 50, 'Travel', ?, '2026-02-01')", (flask_app.encrypt_data("Flight"),))
        conn.commit()
        conn.close()
    
    # 1. Keyword Search
    resp = client.get('/search_expenses?keyword=Coffee')
    assert "Coffee" in resp.text
    assert "Flight" not in resp.text
    
    # 2. Category Filter
    resp = client.get('/search_expenses?categories=Travel')
    assert "Flight" in resp.text
    assert "Coffee" not in resp.text
    
    # 3. Date Range
    resp = client.get('/search_expenses?date_from=2026-01-01&date_to=2026-01-15')
    assert "Coffee" in resp.text
    assert "Flight" not in resp.text
    
    # 4. Amount Range
    with patch('app.get_usd_rate', return_value=1.0):
        resp = client.get('/search_expenses?amount_min=40&amount_max=60')
    assert "Flight" in resp.text
    assert "Coffee" not in resp.text
    
    # 5. Sorting
    resp = client.get('/search_expenses?sort_by=amount&sort_order=asc')
    # Coffee (10) should come before Flight (50)
    assert resp.text.find("Coffee") < resp.text.find("Flight")

def test_add_expense_recurring(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    with patch('app.get_usd_rate', return_value=1.0):
        client.post('/add_expense', data={
            "amount": "100",
            "currency": "USD",
            "category": "Bills",
            "description": "Netflix",
            "date": "2026-02-07",
            "is_recurring": "on",
            "frequency": "monthly"
        }, follow_redirects=True)
    
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        expense = conn.execute("SELECT * FROM expenses WHERE is_recurring = 1").fetchone()
        assert expense is not None
        assert expense['frequency'] == 'monthly'
        conn.close()

def test_unauthorized_access(client):
    # Try to access dashboard without login
    resp = client.get('/dashboard', follow_redirects=True)
    assert "login" in resp.request.url
    
    # Try to access group detail without being a member
    with client.session_transaction() as sess:
        sess['user_id'] = 1
    
    # Create group by user 1
    client.post('/create_group', data={"name": "Private Group"}, follow_redirects=True)
    
    # Switch to user 2
    with client.session_transaction() as sess:
        sess['user_id'] = 2
    
    resp = client.get('/group/1', follow_redirects=True)
    assert "not a member" in resp.text

def test_recurring_expense_generation(client):
    # Ensure user 1 exists with a secret
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        hashed = flask_app.generate_password_hash("pass")
        secret = pyotp.random_base32()
        conn.execute("INSERT OR REPLACE INTO users (id, username, email, password, totp_secret) VALUES (?, ?, ?, ?, ?)",
                     (1, "testuser", "test@test.com", hashed, secret))
        
        # Insert a recurring expense in the past
        today = flask_app.datetime.now().date()
        past_date = (today - flask_app.timedelta(days=1)).strftime('%Y-%m-%d')
        conn.execute('''
            INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date, is_recurring, frequency, next_due_date)
            VALUES (?, 100, 'USD', 100, 'Bills', 'Internet', ?, 1, 'monthly', ?)
        ''', (1, past_date, past_date))
        conn.commit()
        conn.close()
    
    # Set pre_2fa_id in session
    with client.session_transaction() as sess:
        sess['pre_2fa_id'] = 1
    
    totp = pyotp.TOTP(secret)
    client.post('/verify_2fa', data={"token": totp.now()}, follow_redirects=True)
    
    # Check if a new expense was added
    with flask_app.app.app_context():
        conn = flask_app.get_db_connection()
        count = conn.execute("SELECT COUNT(*) FROM expenses WHERE description LIKE '%Auto-generated%'").fetchone()[0]
        assert count > 0
        conn.close()







