import os
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# ডেটাবেজ কনফিগারেশন (SQLite ডেটাবেজ লোকাল এবং রেন্ডারে কাজ করবে)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///shops.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# শপ বা দোকানদারের ডেটাবেজ মডেল
class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_name = db.Column(db.String(150), nullable=False)
    owner_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.Text, nullable=False)
    qr_image_url = db.Column(db.String(300), nullable=False)

# অ্যাপ চালুর সময় ডেটাবেজ তৈরি করার জন্য
with app.app_context():
    db.create_all()

# হোম পেজ - সব দোকানের লিস্ট বা পেমেন্ট পেজ দেখানোর জন্য
@app.route('/')
def index():
    # আপাতত ডিফল্ট বা প্রথম দোকানটি দেখাবে
    shop = Shop.query.first()
    if not shop:
        # যদি ডেটাবেজে কোনো দোকান না থাকে, তবে ডিফল্ট তথ্য দেখাবে
        shop_data = {
            'shop_name': 'KAMRUL ONLINE SERVICE',
            'address': 'Gaon Pradhan office vill: Garoimari p.o:garoimari dist: Morigaon pin-782104',
            'qr_image_url': 'https://raw.githubusercontent.com/kamrulonlineservice/qr-print-service/main/static/qr.png'
        }
    else:
        shop_data = {
            'shop_name': shop.shop_name,
            'address': shop.address,
            'qr_image_url': shop.qr_image_url
        }
    return render_template('index.html', shop=shop_data)

# নতুন দোকান রেজিস্টার করার ফর্ম পেজ
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        shop_name = request.form.get('shop_name')
        owner_name = request.form.get('owner_name')
        address = request.form.get('address')
        qr_image_url = request.form.get('qr_image_url')

        # নতুন দোকান ডেটাবেজে সেভ করা
        new_shop = Shop(shop_name=shop_name, owner_name=owner_name, address=address, qr_image_url=qr_image_url)
        db.session.add(new_shop)
        db.commit() if hasattr(db, 'commit') else db.session.commit()

        return redirect(url_for('index'))
    
    return render_template('register.html')

if __name__ == '__main__':
    app.run(debug=True)
