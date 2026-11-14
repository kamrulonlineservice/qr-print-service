import sqlite3

def init_db():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    
    # Shops Table (Updated with new address columns)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_name TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            country TEXT,
            state TEXT,
            district TEXT,
            village TEXT,
            pincode TEXT,
            upi_id TEXT,
            bw_rate REAL DEFAULT 2.0,
            color_rate REAL DEFAULT 10.0,
            registration_date TIMESTAMP,
            trial_end_date TIMESTAMP,
            subscription_end_date TIMESTAMP,
            recharge_amount REAL DEFAULT 0.0,
            amount REAL DEFAULT 0.0,
            payment_status TEXT DEFAULT 'Unpaid',
            status TEXT DEFAULT 'Active',
            created_at TIMESTAMP,
            plan_name TEXT,
            utr_number TEXT
        )
    ''')

    # Print Jobs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER,
            customer_phone TEXT,
            file_name TEXT,
            copies INTEGER,
            print_type TEXT,
            amount REAL,
            status TEXT,
            timestamp TIMESTAMP,
            pages_input INTEGER,
            pay_mode TEXT,
            payment_id TEXT,
            order_id TEXT,
            FOREIGN KEY (shop_id) REFERENCES shops (id)
        )
    ''')

    # Recharge History Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recharge_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER,
            plan_name TEXT,
            amount REAL,
            utr_number TEXT,
            status TEXT,
            timestamp TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops (id)
        )
    ''')

    conn.commit()
    conn.close()