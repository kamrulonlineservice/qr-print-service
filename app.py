from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
import os

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # সেশন সিকিউরিটির জন্য

# ডেটাবেজ ইনিশিয়ালাইজেশন
def init_db():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_name TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            subscription_status TEXT DEFAULT 'Active'
        )
    ''')
    conn.commit()
    conn.close()

@app.route('/')
def home():
    if 'shop_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# ১. দোকানদার রেজিস্ট্রেশন রুট
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        shop_name = request.form['shop_name']
        owner_name = request.form['owner_name']
        email = request.form['email']
        password = request.form['password'] # প্রোডাকশনে পাসওয়ার্ড হ্যাশ (hash) করে নিতে হবে

        try:
            conn = sqlite3.connect('database.db')
            cursor = conn.cursor()
            cursor.execute("INSERT INTO shops (shop_name, owner_name, email, password) VALUES (?, ?, ?, ?)",
                           (shop_name, owner_name, email, password))
            conn.commit()
            conn.close()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            return "এই ইমেইল দিয়ে ইতিমধ্যেই একটি অ্যাকাউন্ট খোলা হয়েছে!"

    return render_template('register.html')

# ২. দোকানদার লগইন রুট
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = sqlite3.connect('database.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shops WHERE email = ? AND password = ?", (email, password))
        shop = cursor.fetchone()
        conn.close()

        if shop:
            session['shop_id'] = shop[0]
            session['shop_name'] = shop[1]
            session['owner_name'] = shop[2]
            return redirect(url_for('dashboard'))
        else:
            return "ভুল ইমেইল অথবা পাসওয়ার্ড!"

    return render_template('login.html')

# ৩. ড্যাশবোর্ড রুট
@app.route('/dashboard')
def dashboard():
    if 'shop_id' not in session:
        return redirect(url_for('login'))
    
    shop_name = session['shop_name']
    owner_name = session['owner_name']
    shop_id = session['shop_id']

    # প্রতিটি দোকানের জন্য ইউনিক কিউআর কোডের লিংক জেনারেট হবে
    qr_link = f"https://qrseprint.in/print/{shop_id}"

    return render_template('dashboard.html', shop_name=shop_name, owner_name=owner_name, qr_link=qr_link)

# ৪. লগআউট রুট
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
