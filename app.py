import os
import fitz  # PyMuPDF
import glob
from flask import Flask, render_template, request, send_from_directory, redirect, url_for

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

SHOP_UPI_ID = "7907205717@mbk"
pending_orders = []

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/edit', methods=['POST'])
def edit_file():
    if 'file' not in request.files:
        return 'No file uploaded', 400
    file = request.files['file']
    customer_phone = request.form.get('customer_phone')
    if file.filename == '':
        return 'No selected file', 400
    if file:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filepath)
        
        pages_list = []
        if file.filename.lower().endswith('.pdf'):
            try:
                doc = fitz.open(filepath)
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    jpg_name = f"{file.filename.rsplit('.', 1)[0]}_page_{i+1}.jpg"
                    jpg_path = os.path.join(app.config['UPLOAD_FOLDER'], jpg_name)
                    pix.save(jpg_path)
                    pages_list.append(jpg_name)
            except Exception as e:
                print(f"PDF conversion error: {e}")
        else:
            pages_list.append(file.filename)

        return render_template('edit.html', filename=file.filename, pages=pages_list, customer_phone=customer_phone)

@app.route('/preview', methods=['POST'])
def preview_file():
    filename = request.form.get('filename')
    customer_phone = request.form.get('customer_phone')
    pages_input = request.form.get('pages_input', '').strip()
    
    base_name = filename.rsplit('.', 1)[0]
    all_generated_pages = sorted(glob.glob(os.path.join(app.config['UPLOAD_FOLDER'], f"{base_name}_page_*.jpg")))
    total_doc_pages = len(all_generated_pages) if all_generated_pages else 1

    selected_pages = []
    
    if pages_input:
        try:
            parts = pages_input.split(',')
            for part in parts:
                part = part.strip()
                if '-' in part:
                    start, end = map(int, part.split('-'))
                    for p in range(start, end + 1):
                        if 1 <= p <= total_doc_pages:
                            selected_pages.append(f"{base_name}_page_{p}.jpg")
                else:
                    p = int(part)
                    if 1 <= p <= total_doc_pages:
                        selected_pages.append(f"{base_name}_page_{p}.jpg")
        except Exception as e:
            print(f"Page parse error: {e}")

    if not selected_pages:
        selected_pages = [f"{base_name}_page_1.jpg"]

    display_filename = selected_pages[0]
    total_selected_pages = len(selected_pages)

    return render_template('preview.html', 
                           filename=filename, 
                           display_filename=display_filename, 
                           selected_pages=selected_pages,
                           total_pages=total_selected_pages,
                           customer_phone=customer_phone)
           

@app.route('/pay', methods=['POST'])
def pay():
    filename = request.form.get('filename')
    customer_phone = request.form.get('customer_phone')
    print_type = request.form.get('print_type', 'bw')
    
    # মোট কতটি পেজ সিলেক্ট করা হয়েছে তা রিসিভ করা
    total_pages = int(request.form.get('total_pages', 1))
    copies = int(request.form.get('copies', 1))
    
    rate = 10 if print_type == 'color' else 5
    # সঠিক হিসাব: মোট পেজ * কপি সংখ্যা * রেট
    total_amount = total_pages * copies * rate
    
    return render_template('payment.html', 
                           filename=filename, 
                           customer_phone=customer_phone, 
                           print_type=print_type, 
                           copies=copies, 
                           total_pages=total_pages,
                           rate=rate, 
                           amount=total_amount, 
                           upi_id=SHOP_UPI_ID)

@app.route('/pay-counter', methods=['POST'])
def pay_counter():
    filename = request.form.get('filename', 'Document')
    copies = request.form.get('copies', '1')
    print_type = request.form.get('print_type', 'bw')
    customer_phone = request.form.get('customer_phone', 'N/A')
    rate = 10 if print_type == 'color' else 5
    total_amount = int(copies) * rate
    
    pending_orders.append({
        'filename': filename,
        'copies': copies,
        'print_type': print_type,
        'amount': total_amount,
        'phone': customer_phone
    })
    
    return render_template('success.html', filename=filename, copies=copies, print_type=print_type, amount=total_amount)

@app.route('/admin')
def admin_dashboard():
    return render_template('admin.html', orders=pending_orders)

@app.route('/print-now/<int:order_index>')
def print_now(order_index):
    if 0 <= order_index < len(pending_orders):
        order = pending_orders.pop(order_index)
        # ক্লাউড সার্ভারে শুধু প্রিন্ট রিকোয়েস্ট জমা রাখবে বা পাস করবে
        print(f"Print requested for: {order}")
    return redirect(url_for('admin_dashboard'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
