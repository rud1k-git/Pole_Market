import os
import uuid
import socket
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
import pyodbc

app = Flask(__name__)
app.secret_key = 'super-secret-market-key-change-me'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'agro.accdb')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

CATEGORY_TREE = {
    'Урожай и продукты': ['Овощи и зелень', 'Фрукты и ягоды', 'Грибы и дикоросы', 'Мёд и заготовки', 'Молоко, сыр и яйца'],
    'Для хозяйства': ['Семена и рассада', 'Саженцы и растения', 'Корма для животных', 'Инвентарь и техника', 'Удобрения и грунт'],
    'Услуги': ['Вспашка и обработка земли', 'Покос и уборка участка', 'Аренда техники', 'Доставка урожая', 'Помощь по хозяйству']
}

@app.context_processor
def inject_globals():
    return dict(category_tree=CATEGORY_TREE)

@app.template_filter('img_list')
def img_list_filter(val):
    if not val:
        return []
    return [x.strip() for x in str(val).split(',') if x.strip()]

@app.template_filter('first_img')
def first_img_filter(val):
    lst = img_list_filter(val)
    return str(next(iter(lst), ''))

@app.template_filter('main_cat')
def main_cat_filter(val):
    if not val:
        return 'Для хозяйства'
    return str(val).split(':', 1)[0].strip()

@app.template_filter('sub_cat')
def sub_cat_filter(val):
    if not val or ':' not in str(val):
        return str(val or '')
    return str(val).split(':', 1)[1].strip()

if not os.path.exists(DB_PATH):
    raise FileNotFoundError(f"Файл базы не найден по пути: {DB_PATH}")

def allowed_file(filename):
    if '.' not in filename:
        return False
    return filename.rsplit('.', 1)[-1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_file(file):
    if file and file.filename and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[-1].lower()
        new_name = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_name))
        return new_name
    return ''

def get_db():
    conn_str = (
        r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
        f'DBQ={DB_PATH};'
    )
    return pyodbc.connect(conn_str)

@app.before_request
def check_user_blocked():
    if 'user_id' in session and session.get('role') != 'admin':
        if request.endpoint in ('static', 'logout', 'login'):
            return
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_blocked FROM Users WHERE ID_Users = ?", (int(session['user_id']),))
        row = cursor.fetchone()
        conn.close()
        if not row or bool(row.is_blocked):
            session.clear()
            flash('Ваш аккаунт заблокирован администратором.', 'danger')
            return redirect(url_for('login'))

def login_required(roles=None):
    def wrapper(fn):
        @wraps(fn)
        def decorated_view(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if roles and session.get('role') not in roles:
                flash('Нет доступа к этому разделу.', 'danger')
                return redirect(url_for('dashboard'))
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper
@app.route('/')
def index():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.ID_Products, p.title, p.category, p.price, p.unit, p.description, p.image_filename, u.company_name AS seller_name, p.seller_id 
        FROM Products p INNER JOIN Users u ON p.seller_id = u.ID_Users
        WHERE u.is_blocked = False OR u.is_blocked IS NULL
        ORDER BY p.ID_Products DESC
    """)
    products = cursor.fetchall()
    conn.close()
    return render_template('index.html', products=products)

@app.route('/product/<int:pid>')
def product_detail(pid):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.ID_Products, p.title, p.category, p.price, p.unit, p.description, p.image_filename, 
               u.company_name AS seller_name, u.avatar AS seller_avatar, p.seller_id 
        FROM Products p INNER JOIN Users u ON p.seller_id = u.ID_Users
        WHERE p.ID_Products = ?
    """, (pid,))
    product = cursor.fetchone()
    if not product:
        conn.close()
        flash('Объявление не найдено.', 'danger')
        return redirect(url_for('index'))

    seller_id = int(product.seller_id)

    cursor.execute("""
        SELECT r.rating, r.review_text, r.review_image, r.created_at, u.company_name AS buyer_name, p.title AS product_title
        FROM (Reviews r
        INNER JOIN Users u ON r.buyer_id = u.ID_Users)
        INNER JOIN Products p ON r.product_id = p.ID_Products
        WHERE r.seller_id = ?
        ORDER BY r.ID_Reviews DESC
    """, (seller_id,))
    reviews = cursor.fetchall()

    avg_rating = round(sum(int(r.rating) for r in reviews) / len(reviews), 1) if reviews else 0

    cursor.execute("""
        SELECT ID_Products, title, category, price, unit, image_filename
        FROM Products
        WHERE seller_id = ? AND ID_Products <> ?
        ORDER BY ID_Products DESC
    """, (seller_id, pid))
    other_products = cursor.fetchall()

    conn.close()
    return render_template('product.html', product=product, reviews=reviews, avg_rating=avg_rating, other_products=other_products)

@app.route('/report_product/<int:pid>', methods=['POST'])
@login_required()
def report_product(pid):
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Укажите причину жалобы.', 'danger')
        return redirect(url_for('product_detail', pid=pid))

    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    cursor.execute(
        "INSERT INTO Reports (product_id, reporter_id, reason, status, created_at) VALUES (?, ?, ?, 'open', ?)",
        (pid, int(session['user_id']), reason, now)
    )
    conn.commit()
    conn.close()
    flash('Жалоба отправлена администратору на проверку.', 'success')
    return redirect(url_for('product_detail', pid=pid))

@app.route('/seller/<int:seller_id>')
def seller_profile(seller_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT ID_Users, company_name, avatar FROM Users WHERE ID_Users = ?", (seller_id,))
    seller = cursor.fetchone()
    if not seller:
        conn.close()
        flash('Продавец не найден.', 'danger')
        return redirect(url_for('index'))

    cursor.execute("""
        SELECT ID_Products, title, category, price, unit, description, image_filename
        FROM Products WHERE seller_id = ? ORDER BY ID_Products DESC
    """, (seller_id,))
    products = cursor.fetchall()

    cursor.execute("""
        SELECT r.rating, r.review_text, r.review_image, r.created_at, u.company_name AS buyer_name, p.title AS product_title
        FROM (Reviews r
        INNER JOIN Users u ON r.buyer_id = u.ID_Users)
        INNER JOIN Products p ON r.product_id = p.ID_Products
        WHERE r.seller_id = ? ORDER BY r.ID_Reviews DESC
    """, (seller_id,))
    reviews = cursor.fetchall()

    avg_rating = round(sum(int(r.rating) for r in reviews) / len(reviews), 1) if reviews else 0
    conn.close()
    return render_template('seller_profile.html', seller=seller, products=products, reviews=reviews, avg_rating=avg_rating)
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        raw_pass = request.form['password']
        confirm_pass = request.form.get('confirm_password', '')
        role = request.form.get('role')
        full_name = request.form['full_name'].strip()
        phone = request.form['phone'].strip()
        email = request.form['email'].strip()

        if raw_pass != confirm_pass:
            flash('Пароли не совпадают! Проверьте ввод.', 'danger')
            return redirect(url_for('register'))

        if len(raw_pass) < 4:
            flash('Пароль должен содержать минимум 4 символа.', 'danger')
            return redirect(url_for('register'))

        if role not in ('buyer', 'seller', 'both'):
            flash('Недопустимая роль при регистрации!', 'danger')
            return redirect(url_for('register'))

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ID_Users FROM Users WHERE username = ?", (username,))
        if cursor.fetchone():
            flash('Этот логин уже занят!', 'danger')
            conn.close()
            return redirect(url_for('register'))

        password_hash = generate_password_hash(raw_pass)
        cursor.execute(
            "INSERT INTO Users (username, password_hash, role, company_name, phone, email, avatar, is_blocked) VALUES (?, ?, ?, ?, ?, ?, '', False)",
            (username, password_hash, role, full_name, phone, email)
        )
        conn.commit()
        conn.close()
        flash('Регистрация прошла успешно! Войдите в аккаунт.', 'success')
        return redirect(url_for('login'))
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ID_Users, password_hash, role, company_name, is_blocked FROM Users WHERE username = ?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user.password_hash, password):
            if bool(user.is_blocked) and user.role != 'admin':
                flash('Ваш аккаунт заблокирован администратором.', 'danger')
                return redirect(url_for('login'))

            session['user_id'] = int(user.ID_Users)
            session['username'] = username
            session['role'] = user.role
            session['company_name'] = user.company_name
            return redirect(url_for('index'))
        flash('Неверный логин или пароль', 'danger')
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/change_role', methods=['POST'])
@login_required()
def change_role():
    if session.get('role') == 'admin':
        return redirect(url_for('dashboard'))
    new_role = request.form.get('new_role')
    if new_role in ('buyer', 'seller', 'both'):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE Users SET role = ? WHERE ID_Users = ?", (new_role, int(session['user_id'])))
        conn.commit()
        conn.close()
        session['role'] = new_role
        flash('Режим профиля обновлен!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/update_profile', methods=['POST'])
@login_required()
def update_profile():
    conn = get_db()
    cursor = conn.cursor()
    uid = int(session['user_id'])

    cursor.execute("SELECT avatar FROM Users WHERE ID_Users = ?", (uid,))
    row = cursor.fetchone()
    avatar_name = row.avatar if (row and row.avatar) else ''

    file = request.files.get('avatar')
    if file and file.filename and allowed_file(file.filename):
        if avatar_name:
            old_p = os.path.join(app.config['UPLOAD_FOLDER'], avatar_name)
            if os.path.exists(old_p):
                os.remove(old_p)
        avatar_name = save_uploaded_file(file)

    full_name = request.form['full_name'].strip()
    phone = request.form['phone'].strip()
    email = request.form['email'].strip()

    cursor.execute(
        "UPDATE Users SET company_name = ?, phone = ?, email = ?, avatar = ? WHERE ID_Users = ?",
        (full_name, phone, email, avatar_name, uid)
    )
    conn.commit()
    conn.close()

    session['company_name'] = full_name
    flash('Данные профиля сохранены!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/admin/toggle_block/<int:uid>', methods=['POST'])
@login_required(['admin'])
def toggle_block(uid):
    if uid == int(session['user_id']):
        flash('Нельзя заблокировать самого себя.', 'danger')
        return redirect(url_for('dashboard'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_blocked, role FROM Users WHERE ID_Users = ?", (uid,))
    u = cursor.fetchone()
    if u and u.role != 'admin':
        new_state = not bool(u.is_blocked)
        cursor.execute("UPDATE Users SET is_blocked = ? WHERE ID_Users = ?", (new_state, uid))
        conn.commit()
        flash('Пользователь заблокирован.' if new_state else 'Пользователь разблокирован.', 'info')
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/admin/dismiss_report/<int:rid>', methods=['POST'])
@login_required(['admin'])
def dismiss_report(rid):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE Reports SET status = 'closed' WHERE ID_Reports = ?", (rid,))
    conn.commit()
    conn.close()
    flash('Жалоба закрыта.', 'info')
    return redirect(url_for('dashboard'))
@app.route('/dashboard')
@login_required()
def dashboard():
    conn = get_db()
    cursor = conn.cursor()
    role = session['role']
    uid = int(session['user_id'])

    cursor.execute("SELECT company_name, phone, email, avatar FROM Users WHERE ID_Users = ?", (uid,))
    user_info = cursor.fetchone()

    filled_steps = 1
    missing_tips = []
    if user_info.company_name: filled_steps += 1
    else: missing_tips.append('укажите имя')
    if user_info.phone: filled_steps += 1
    else: missing_tips.append('добавьте телефон')
    if user_info.email: filled_steps += 1
    else: missing_tips.append('укажите почту')
    if user_info.avatar: filled_steps += 1
    else: missing_tips.append('загрузите аватарку')
    profile_percent = int((filled_steps / 5) * 100)

    cursor.execute("""
        SELECT c.ID_ChatRooms, p.title, p.price, p.unit, u_sell.company_name AS partner_name,
               c.status, c.buyer_confirmed, c.seller_confirmed
        FROM (ChatRooms c 
        INNER JOIN Products p ON c.product_id = p.ID_Products)
        INNER JOIN Users u_sell ON c.seller_id = u_sell.ID_Users
        WHERE c.buyer_id = ?
        ORDER BY c.ID_ChatRooms DESC
    """, (uid,))
    my_purchases = cursor.fetchall()

    cursor.execute("""
        SELECT c.ID_ChatRooms, p.title, p.price, p.unit, u_buy.company_name AS partner_name,
               c.status, c.buyer_confirmed, c.seller_confirmed
        FROM (ChatRooms c 
        INNER JOIN Products p ON c.product_id = p.ID_Products)
        INNER JOIN Users u_buy ON c.buyer_id = u_buy.ID_Users
        WHERE c.seller_id = ?
        ORDER BY c.ID_ChatRooms DESC
    """, (uid,))
    my_sales = cursor.fetchall()

    users_list, all_products, reports_list, all_chats, support_users = [], [], [], [], []
    if role == 'admin':
        cursor.execute("SELECT ID_Users, username, role, company_name, phone, email, is_blocked FROM Users ORDER BY ID_Users DESC")
        users_list = cursor.fetchall()

        cursor.execute("""
            SELECT p.ID_Products, p.title, p.category, p.price, p.unit, p.image_filename, u.company_name AS seller_name
            FROM Products p INNER JOIN Users u ON p.seller_id = u.ID_Users
            ORDER BY p.ID_Products DESC
        """)
        all_products = cursor.fetchall()

        cursor.execute("""
            SELECT r.ID_Reports, r.product_id, r.reason, r.created_at, p.title AS product_title, u.company_name AS reporter_name
            FROM (Reports r
            INNER JOIN Products p ON r.product_id = p.ID_Products)
            INNER JOIN Users u ON r.reporter_id = u.ID_Users
            WHERE r.status = 'open'
            ORDER BY r.ID_Reports DESC
        """)
        reports_list = cursor.fetchall()

        cursor.execute("""
            SELECT c.ID_ChatRooms, p.title, u_buy.company_name AS buyer_name, u_sell.company_name AS seller_name, c.status
            FROM ((ChatRooms c
            INNER JOIN Products p ON c.product_id = p.ID_Products)
            INNER JOIN Users u_buy ON c.buyer_id = u_buy.ID_Users)
            INNER JOIN Users u_sell ON c.seller_id = u_sell.ID_Users
            ORDER BY c.ID_ChatRooms DESC
        """)
        all_chats = cursor.fetchall()

        cursor.execute("""
            SELECT DISTINCT u.ID_Users, u.company_name, u.username
            FROM SupportTickets s INNER JOIN Users u ON s.user_id = u.ID_Users
        """)
        support_users = cursor.fetchall()

    elif role in ('seller', 'both'):
        cursor.execute("SELECT ID_Products, title, category, price, unit, image_filename FROM Products WHERE seller_id = ? ORDER BY ID_Products DESC", (uid,))
        all_products = cursor.fetchall()

    conn.close()
    return render_template(
        'dashboard.html',
        user_info=user_info,
        profile_percent=profile_percent,
        missing_tips=missing_tips,
        purchases=my_purchases,
        sales=my_sales,
        users=users_list,
        products=all_products,
        reports=reports_list,
        all_chats=all_chats,
        support_users=support_users
    )

@app.route('/add_product', methods=['GET', 'POST'])
@login_required(['seller', 'both'])
def add_product():
    if request.method == 'POST':
        main_cat = request.form.get('main_category', 'Товары')
        sub_cat = request.form.get('sub_category', '')
        full_category = f"{main_cat}: {sub_cat}" if sub_cat else main_cat

        files = request.files.getlist('images')
        saved_names = []
        for f in files:
            fname = save_uploaded_file(f)
            if fname:
                saved_names.append(fname)
        images_str = ','.join(saved_names)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO Products (seller_id, title, category, price, unit, description, image_filename) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(session['user_id']), request.form['title'], full_category, float(request.form['price']), request.form['unit'], request.form['description'], images_str)
        )
        conn.commit()
        conn.close()
        flash('Объявление успешно опубликовано!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('add_product.html')

@app.route('/edit_product/<int:pid>', methods=['GET', 'POST'])
@login_required(['admin', 'seller', 'both'])
def edit_product(pid):
    conn = get_db()
    cursor = conn.cursor()
    uid = int(session['user_id'])

    if session['role'] == 'admin':
        cursor.execute("SELECT * FROM Products WHERE ID_Products = ?", (pid,))
    else:
        cursor.execute("SELECT * FROM Products WHERE ID_Products = ? AND seller_id = ?", (pid, uid))
    product = cursor.fetchone()

    if not product:
        conn.close()
        flash('Объявление не найдено.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        main_cat = request.form.get('main_category', 'Товары')
        sub_cat = request.form.get('sub_category', '')
        full_category = f"{main_cat}: {sub_cat}" if sub_cat else main_cat

        kept_images = request.form.getlist('keep_images')
        old_images = img_list_filter(product.image_filename)

        for old_img in old_images:
            if old_img not in kept_images:
                old_path = os.path.join(app.config['UPLOAD_FOLDER'], old_img)
                if os.path.exists(old_path):
                    os.remove(old_path)

        new_files = request.files.getlist('images')
        for f in new_files:
            fname = save_uploaded_file(f)
            if fname:
                kept_images.append(fname)

        images_str = ','.join(kept_images)

        cursor.execute("""
            UPDATE Products 
            SET title = ?, category = ?, price = ?, unit = ?, description = ?, image_filename = ?
            WHERE ID_Products = ?
        """, (
            request.form['title'],
            full_category,
            float(request.form['price']),
            request.form['unit'],
            request.form['description'],
            images_str,
            pid
        ))
        conn.commit()
        conn.close()
        flash('Изменения сохранены!', 'success')
        return redirect(url_for('dashboard'))

    conn.close()
    return render_template('edit_product.html', product=product)

@app.route('/delete_product/<int:pid>')
@login_required(['admin', 'seller', 'both'])
def delete_product(pid):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT image_filename FROM Products WHERE ID_Products = ?", (pid,))
    row = cursor.fetchone()
    if row and row.image_filename:
        for img in img_list_filter(row.image_filename):
            img_path = os.path.join(app.config['UPLOAD_FOLDER'], img)
            if os.path.exists(img_path):
                os.remove(img_path)

    cursor.execute("UPDATE Reports SET status = 'closed' WHERE product_id = ?", (pid,))

    if session['role'] == 'admin':
        cursor.execute("DELETE FROM Products WHERE ID_Products = ?", (pid,))
    else:
        cursor.execute("DELETE FROM Products WHERE ID_Products = ? AND seller_id = ?", (pid, int(session['user_id'])))
    conn.commit()
    conn.close()
    flash('Объявление удалено.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/start_chat/<int:product_id>')
@login_required(['buyer', 'both'])
def start_chat(product_id):
    conn = get_db()
    cursor = conn.cursor()
    buyer_id = int(session['user_id'])

    cursor.execute("SELECT seller_id FROM Products WHERE ID_Products = ?", (product_id,))
    prod = cursor.fetchone()
    if not prod:
        conn.close()
        return redirect(url_for('index'))

    seller_id = int(prod.seller_id)
    if seller_id == buyer_id:
        conn.close()
        flash('Нельзя начать сделку по собственному объявлению.', 'danger')
        return redirect(url_for('product_detail', pid=product_id))

    cursor.execute(
        "SELECT ID_ChatRooms FROM ChatRooms WHERE buyer_id = ? AND seller_id = ? AND product_id = ? AND status = 'in_progress'",
        (buyer_id, seller_id, product_id)
    )
    room = cursor.fetchone()
    if room:
        room_id = int(room.ID_ChatRooms)
    else:
        cursor.execute(
            "INSERT INTO ChatRooms (buyer_id, seller_id, product_id, status, buyer_confirmed, seller_confirmed) VALUES (?, ?, ?, 'in_progress', False, False)",
            (buyer_id, seller_id, product_id)
        )
        conn.commit()
        cursor.execute("SELECT MAX(ID_ChatRooms) AS max_id FROM ChatRooms")
        room_id = int(cursor.fetchone().max_id)

    conn.close()
    return redirect(url_for('chat_room', room_id=room_id))


@app.route('/confirm_deal/<int:room_id>', methods=['POST'])
@login_required()
def confirm_deal(room_id):
    conn = get_db()
    cursor = conn.cursor()
    uid = int(session['user_id'])

    cursor.execute(
        "SELECT buyer_id, seller_id, buyer_confirmed, seller_confirmed FROM ChatRooms WHERE ID_ChatRooms = ?",
        (room_id,))
    room = cursor.fetchone()
    if not room or uid not in (int(room.buyer_id), int(room.seller_id)):
        conn.close()
        return redirect(url_for('dashboard'))

    b_conf = bool(room.buyer_confirmed)
    s_conf = bool(room.seller_confirmed)

    if uid == int(room.buyer_id):
        b_conf = True
    if uid == int(room.seller_id):
        s_conf = True

    new_status = 'completed' if (b_conf and s_conf) else 'in_progress'

    cursor.execute(
        "UPDATE ChatRooms SET buyer_confirmed = ?, seller_confirmed = ?, status = ? WHERE ID_ChatRooms = ?",
        (b_conf, s_conf, new_status, room_id)
    )

    sys_msg = (
        "Сделка завершена. Обе стороны подтвердили выполнение."
        if new_status == 'completed'
        else f"Пользователь {session['company_name']} подтвердил завершение сделки. Ожидается подтверждение второй стороны."
    )
    now = datetime.now().strftime('%H:%M • %d.%m')
    cursor.execute(
        "INSERT INTO Messages (room_id, sender_id, message_text, created_at) VALUES (?, ?, ?, ?)",
        (room_id, uid, sys_msg, now)
    )

    conn.commit()
    conn.close()
    return redirect(url_for('chat_room', room_id=room_id))


@app.route('/add_review/<int:room_id>', methods=['POST'])
@login_required()
def add_review(room_id):
    conn = get_db()
    cursor = conn.cursor()
    uid = int(session['user_id'])

    cursor.execute("SELECT buyer_id, seller_id, product_id, status FROM ChatRooms WHERE ID_ChatRooms = ?", (room_id,))
    room = cursor.fetchone()

    if not room or uid != int(room.buyer_id) or room.status != 'completed':
        conn.close()
        flash('Отзыв доступен только после завершения сделки.', 'danger')
        return redirect(url_for('chat_room', room_id=room_id))

    cursor.execute("SELECT ID_Reviews FROM Reviews WHERE room_id = ?", (room_id,))
    if cursor.fetchone():
        conn.close()
        flash('Вы уже оставили отзыв по этой сделке.', 'info')
        return redirect(url_for('chat_room', room_id=room_id))

    rating = int(request.form.get('rating', 5))
    review_text = request.form.get('review_text', '').strip()
    review_img = save_uploaded_file(request.files.get('review_image'))
    now = datetime.now().strftime('%d.%m.%Y')

    cursor.execute(
        "INSERT INTO Reviews (room_id, product_id, seller_id, buyer_id, rating, review_text, review_image, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (room_id, int(room.product_id), int(room.seller_id), uid, rating, review_text, review_img, now)
    )
    conn.commit()
    conn.close()
    flash('Спасибо! Ваш отзыв опубликован в профиле продавца.', 'success')
    return redirect(url_for('chat_room', room_id=room_id))


@app.route('/chat/<int:room_id>')
@login_required()
def chat_room(room_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.ID_ChatRooms, c.product_id, p.title, p.category, p.price, p.unit, p.image_filename,
               u_buy.company_name AS buyer_name, u_sell.company_name AS seller_name,
               c.buyer_id, c.seller_id, c.status, c.buyer_confirmed, c.seller_confirmed
        FROM ((ChatRooms c 
        INNER JOIN Products p ON c.product_id = p.ID_Products)
        INNER JOIN Users u_buy ON c.buyer_id = u_buy.ID_Users)
        INNER JOIN Users u_sell ON c.seller_id = u_sell.ID_Users
        WHERE c.ID_ChatRooms = ?
    """, (room_id,))
    room = cursor.fetchone()

    has_review = False
    if room:
        cursor.execute("SELECT ID_Reviews FROM Reviews WHERE room_id = ?", (room_id,))
        has_review = bool(cursor.fetchone())

    conn.close()

    is_admin = (session.get('role') == 'admin')
    if not room or (not is_admin and int(session['user_id']) not in (int(room.buyer_id), int(room.seller_id))):
        return redirect(url_for('dashboard'))
    return render_template('chat.html', room=room, has_review=has_review, is_admin_view=is_admin)


@app.route('/api/messages/<int:room_id>', methods=['GET', 'POST'])
@login_required()
def api_messages(room_id):
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        text = request.json.get('text', '').strip()
        if text:
            now = datetime.now().strftime('%H:%M • %d.%m')
            cursor.execute(
                "INSERT INTO Messages (room_id, sender_id, message_text, created_at) VALUES (?, ?, ?, ?)",
                (room_id, int(session['user_id']), text, now)
            )
            conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})

    cursor.execute("""
        SELECT m.sender_id, u.company_name, m.message_text, m.created_at 
        FROM Messages m INNER JOIN Users u ON m.sender_id = u.ID_Users
        WHERE m.room_id = ? ORDER BY m.ID_Messages ASC
    """, (room_id,))
    rows = cursor.fetchall()
    conn.close()

    messages = [{
        'is_me': int(r.sender_id) == int(session['user_id']),
        'sender': r.company_name,
        'text': r.message_text,
        'time': r.created_at
    } for r in rows]
    return jsonify(messages)


@app.route('/support', methods=['GET', 'POST'])
@app.route('/support/<int:target_uid>', methods=['GET', 'POST'])
@login_required()
def support(target_uid=None):
    conn = get_db()
    cursor = conn.cursor()
    role = session['role']
    uid = int(session['user_id'])

    if role == 'admin':
        if not target_uid:
            conn.close()
            return redirect(url_for('dashboard'))
        chat_user_id = target_uid
        sender_type = 'admin'
    else:
        chat_user_id = uid
        sender_type = 'user'

    if request.method == 'POST':
        msg = request.form.get('message_text', '').strip()
        if msg:
            now = datetime.now().strftime('%H:%M • %d.%m.%Y')
            cursor.execute(
                "INSERT INTO SupportTickets (user_id, sender_role, message_text, created_at) VALUES (?, ?, ?, ?)",
                (chat_user_id, sender_type, msg, now)
            )
            conn.commit()
        conn.close()
        return redirect(url_for('support', target_uid=chat_user_id) if role == 'admin' else url_for('support'))

    cursor.execute("SELECT company_name, username FROM Users WHERE ID_Users = ?", (chat_user_id,))
    ticket_user = cursor.fetchone()

    cursor.execute(
        "SELECT sender_role, message_text, created_at FROM SupportTickets WHERE user_id = ? ORDER BY ID_Tickets ASC",
        (chat_user_id,)
    )
    messages = cursor.fetchall()
    conn.close()

    return render_template('support.html', messages=messages, ticket_user=ticket_user)


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


if __name__ == '__main__':
    local_ip = get_local_ip()
    print("\n" + "=" * 55)
    print(f" Сервер запущен в локальной сети!")
    print(f" На этом ПК:        http://127.0.0.1:5000")
    print(f" С телефона / сети: http://{local_ip}:5000")
    print("=" * 55 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
