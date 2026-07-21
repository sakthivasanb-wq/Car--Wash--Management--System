from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import sqlite3
from datetime import datetime
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

app = Flask(__name__)
app.secret_key = "carwash_secret_key"

DB = "carwash.db"

def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def col_exists(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r["name"] for r in cur.fetchall()]
    return col in cols

def add_col_if_missing(cur, table, col, coltype):
    if not col_exists(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

def init_db():
    conn = db_conn()
    cur = conn.cursor()

    # --- base tables ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            car_number TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS services(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT,
            price INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            service_id INTEGER,
            wash_date TEXT,
            status TEXT DEFAULT 'Pending',
            created_at TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER,
            amount INTEGER,
            payment_mode TEXT,
            paid_at TEXT,
            FOREIGN KEY(booking_id) REFERENCES bookings(id)
        )
    """)

    # --- NEW: staff ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_name TEXT,
            phone TEXT
        )
    """)

    # --- NEW: membership plans + customer membership ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS membership_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name TEXT,
            monthly_fee INTEGER,
            washes_per_month INTEGER,
            discount_percent INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customer_membership(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER UNIQUE,
            plan_id INTEGER,
            start_date TEXT,
            active INTEGER DEFAULT 1,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(plan_id) REFERENCES membership_plans(id)
        )
    """)

    # --- NEW: coupons ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coupons(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            discount_type TEXT,         -- 'PERCENT' or 'FLAT'
            discount_value INTEGER,
            active INTEGER DEFAULT 1
        )
    """)

    # --- NEW: points ledger (optional) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS points_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            points INTEGER,
            reason TEXT,
            created_at TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """)

    # --- add new columns to existing tables (safe upgrade) ---
    add_col_if_missing(cur, "customers", "points", "INTEGER DEFAULT 0")
    add_col_if_missing(cur, "services", "category", "TEXT DEFAULT 'Basic'")

    add_col_if_missing(cur, "bookings", "time_slot", "TEXT")
    add_col_if_missing(cur, "bookings", "staff_id", "INTEGER")
    add_col_if_missing(cur, "bookings", "queue_no", "INTEGER")

    add_col_if_missing(cur, "payments", "coupon_code", "TEXT")
    add_col_if_missing(cur, "payments", "discount", "INTEGER DEFAULT 0")
    add_col_if_missing(cur, "payments", "final_amount", "INTEGER")
    add_col_if_missing(cur, "payments", "points_added", "INTEGER DEFAULT 0")

    # default admin
    cur.execute("SELECT * FROM admin WHERE username='admin'")
    if cur.fetchone() is None:
        cur.execute("INSERT INTO admin(username,password) VALUES(?,?)", ("admin", "admin123"))

    # default staff
    cur.execute("SELECT COUNT(*) as c FROM staff")
    if cur.fetchone()["c"] == 0:
        cur.execute("INSERT INTO staff(staff_name, phone) VALUES(?,?)", ("Karthi", "9000000000"))
        cur.execute("INSERT INTO staff(staff_name, phone) VALUES(?,?)", ("Siva", "9000000001"))

    # default membership plans
    cur.execute("SELECT COUNT(*) as c FROM membership_plans")
    if cur.fetchone()["c"] == 0:
        cur.execute("INSERT INTO membership_plans(plan_name, monthly_fee, washes_per_month, discount_percent) VALUES(?,?,?,?)",
                    ("Silver", 499, 4, 5))
        cur.execute("INSERT INTO membership_plans(plan_name, monthly_fee, washes_per_month, discount_percent) VALUES(?,?,?,?)",
                    ("Gold", 899, 8, 10))
        cur.execute("INSERT INTO membership_plans(plan_name, monthly_fee, washes_per_month, discount_percent) VALUES(?,?,?,?)",
                    ("Platinum", 1299, 12, 15))

    # default coupons
    cur.execute("SELECT COUNT(*) as c FROM coupons")
    if cur.fetchone()["c"] == 0:
        cur.execute("INSERT INTO coupons(code, discount_type, discount_value, active) VALUES(?,?,?,1)", ("FESTIVE10", "PERCENT", 10))
        cur.execute("INSERT INTO coupons(code, discount_type, discount_value, active) VALUES(?,?,?,1)", ("SAVE50", "FLAT", 50))

    conn.commit()
    conn.close()

def require_login():
    return "admin" in session

# ---------------- AUTH ----------------
@app.route("/")
def home():
    if require_login():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM admin WHERE username=? AND password=?", (username, password))
        admin = cur.fetchone()
        conn.close()
        if admin:
            session["admin"] = username
            return redirect(url_for("dashboard"))
        flash("Invalid login! Try admin / admin123", "danger")
        return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("login"))

# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as total FROM customers")
    total_customers = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as total FROM bookings")
    total_bookings = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as total FROM bookings WHERE status='Pending'")
    pending_bookings = cur.fetchone()["total"]

    cur.execute("SELECT IFNULL(SUM(final_amount),0) as revenue FROM payments")
    revenue = cur.fetchone()["revenue"]

    cur.execute("""
        SELECT b.*, c.name as customer_name, c.car_number, s.service_name, s.price,
               st.staff_name
        FROM bookings b
        JOIN customers c ON b.customer_id=c.id
        JOIN services s ON b.service_id=s.id
        LEFT JOIN staff st ON b.staff_id=st.id
        ORDER BY b.id DESC LIMIT 5
    """)
    recent = cur.fetchall()
    conn.close()

    return render_template("dashboard.html",
                           total_customers=total_customers,
                           total_bookings=total_bookings,
                           pending_bookings=pending_bookings,
                           revenue=revenue,
                           recent=recent)

# ---------------- CUSTOMERS ----------------
@app.route("/customers", methods=["GET", "POST"])
def customers():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    # plans list for membership assign
    cur.execute("SELECT * FROM membership_plans ORDER BY monthly_fee")
    plans = cur.fetchall()

    if request.method == "POST":
        name = request.form["name"]
        phone = request.form["phone"]
        car_number = request.form["car_number"]
        plan_id = request.form.get("plan_id", "")
        cur.execute("INSERT INTO customers(name,phone,car_number) VALUES(?,?,?)",
                    (name, phone, car_number))
        customer_id = cur.lastrowid

        # membership optional
        if plan_id and plan_id != "0":
            cur.execute("""
                INSERT OR REPLACE INTO customer_membership(customer_id, plan_id, start_date, active)
                VALUES(?,?,?,1)
            """, (customer_id, int(plan_id), datetime.now().strftime("%Y-%m-%d")))

        conn.commit()
        conn.close()
        flash("Customer Added ✅", "success")
        return redirect(url_for("customers"))

    cur.execute("""
        SELECT c.*,
               mp.plan_name,
               mp.discount_percent
        FROM customers c
        LEFT JOIN customer_membership cm ON cm.customer_id=c.id AND cm.active=1
        LEFT JOIN membership_plans mp ON mp.id=cm.plan_id
        ORDER BY c.id DESC
    """)
    all_customers = cur.fetchall()
    conn.close()

    return render_template("customers.html", customers=all_customers, plans=plans)

@app.route("/customers/edit/<int:id>", methods=["GET", "POST"])
def edit_customer(id):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM customers WHERE id=?", (id,))
    customer = cur.fetchone()

    cur.execute("SELECT * FROM membership_plans ORDER BY monthly_fee")
    plans = cur.fetchall()

    cur.execute("""
        SELECT cm.*, mp.plan_name
        FROM customer_membership cm
        LEFT JOIN membership_plans mp ON mp.id=cm.plan_id
        WHERE cm.customer_id=? AND cm.active=1
    """, (id,))
    active_mem = cur.fetchone()

    if request.method == "POST":
        name = request.form["name"]
        phone = request.form["phone"]
        car_number = request.form["car_number"]
        plan_id = request.form.get("plan_id", "0")

        cur.execute("UPDATE customers SET name=?, phone=?, car_number=? WHERE id=?",
                    (name, phone, car_number, id))

        # update membership
        if plan_id and plan_id != "0":
            cur.execute("""
                INSERT OR REPLACE INTO customer_membership(customer_id, plan_id, start_date, active)
                VALUES(?,?,?,1)
            """, (id, int(plan_id), datetime.now().strftime("%Y-%m-%d")))
        else:
            cur.execute("UPDATE customer_membership SET active=0 WHERE customer_id=?", (id,))

        conn.commit()
        conn.close()
        flash("Customer Updated ✅", "success")
        return redirect(url_for("customers"))

    conn.close()
    return render_template("edit_customer.html", customer=customer, plans=plans, active_mem=active_mem)

@app.route("/customers/delete/<int:id>")
def delete_customer(id):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM customers WHERE id=?", (id,))
    cur.execute("DELETE FROM customer_membership WHERE customer_id=?", (id,))
    conn.commit()
    conn.close()
    flash("Customer Deleted 🗑️", "success")
    return redirect(url_for("customers"))

# ---------------- SERVICES ----------------
@app.route("/services", methods=["GET", "POST"])
def services():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    if request.method == "POST":
        service_name = request.form["service_name"]
        price = request.form["price"]
        category = request.form.get("category", "Basic")
        cur.execute("INSERT INTO services(service_name,price,category) VALUES(?,?,?)",
                    (service_name, price, category))
        conn.commit()
        conn.close()
        flash("Service Added ✅", "success")
        return redirect(url_for("services"))

    cur.execute("SELECT * FROM services ORDER BY id DESC")
    all_services = cur.fetchall()
    conn.close()

    return render_template("services.html", services=all_services)

@app.route("/services/edit/<int:id>", methods=["GET", "POST"])
def edit_service(id):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM services WHERE id=?", (id,))
    service = cur.fetchone()

    if request.method == "POST":
        service_name = request.form["service_name"]
        price = request.form["price"]
        category = request.form.get("category", "Basic")
        cur.execute("UPDATE services SET service_name=?, price=?, category=? WHERE id=?",
                    (service_name, price, category, id))
        conn.commit()
        conn.close()
        flash("Service Updated ✅", "success")
        return redirect(url_for("services"))

    conn.close()
    return render_template("edit_service.html", service=service)

@app.route("/services/delete/<int:id>")
def delete_service(id):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM services WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Service Deleted 🗑️", "success")
    return redirect(url_for("services"))

# ---------------- STAFF ----------------
@app.route("/staff", methods=["GET", "POST"])
def staff():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    if request.method == "POST":
        staff_name = request.form["staff_name"]
        phone = request.form["phone"]
        cur.execute("INSERT INTO staff(staff_name, phone) VALUES(?,?)", (staff_name, phone))
        conn.commit()
        conn.close()
        flash("Staff Added ✅", "success")
        return redirect(url_for("staff"))

    cur.execute("SELECT * FROM staff ORDER BY id DESC")
    staff_list = cur.fetchall()
    conn.close()
    return render_template("staff.html", staff=staff_list)

@app.route("/staff/delete/<int:id>")
def delete_staff(id):
    if not require_login():
        return redirect(url_for("login"))
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM staff WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Staff Deleted 🗑️", "success")
    return redirect(url_for("staff"))

# ---------------- BOOKINGS (Time Slot + Staff + Queue) ----------------
TIME_SLOTS = ["09:00-10:00","10:00-11:00","11:00-12:00","12:00-01:00","02:00-03:00","03:00-04:00","04:00-05:00"]

@app.route("/bookings", methods=["GET", "POST"])
def bookings():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM customers ORDER BY name")
    customers_list = cur.fetchall()

    cur.execute("SELECT * FROM services ORDER BY service_name")
    services_list = cur.fetchall()

    cur.execute("SELECT * FROM staff ORDER BY staff_name")
    staff_list = cur.fetchall()

    if request.method == "POST":
        customer_id = int(request.form["customer_id"])
        service_id = int(request.form["service_id"])
        wash_date = request.form["wash_date"]
        time_slot = request.form["time_slot"]
        staff_id = request.form.get("staff_id", "")
        staff_id = int(staff_id) if staff_id else None
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")

        # queue number per date (today queue)
        cur.execute("SELECT IFNULL(MAX(queue_no),0) as m FROM bookings WHERE wash_date=?", (wash_date,))
        q = cur.fetchone()["m"] + 1

        cur.execute("""
            INSERT INTO bookings(customer_id, service_id, wash_date, time_slot, staff_id, queue_no, created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (customer_id, service_id, wash_date, time_slot, staff_id, q, created_at))

        conn.commit()
        conn.close()
        flash(f"Booking Created ✅ | Queue No: {q}", "success")
        return redirect(url_for("bookings"))

    cur.execute("""
        SELECT b.*, c.name as customer_name, c.car_number,
               s.service_name, s.price, s.category,
               st.staff_name
        FROM bookings b
        JOIN customers c ON b.customer_id = c.id
        JOIN services s ON b.service_id = s.id
        LEFT JOIN staff st ON b.staff_id = st.id
        ORDER BY b.id DESC
    """)
    all_bookings = cur.fetchall()
    conn.close()

    return render_template("bookings.html",
                           bookings=all_bookings,
                           customers=customers_list,
                           services=services_list,
                           staff=staff_list,
                           time_slots=TIME_SLOTS)

@app.route("/bookings/status/<int:id>/<status>")
def booking_status(id, status):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status=? WHERE id=?", (status, id))
    conn.commit()
    conn.close()
    flash(f"Booking marked as {status} ✅", "success")
    return redirect(url_for("bookings"))

# ---------------- COUPONS ----------------
def get_coupon(cur, code):
    if not code:
        return None
    cur.execute("SELECT * FROM coupons WHERE code=? AND active=1", (code.strip().upper(),))
    return cur.fetchone()

# ---------------- PAYMENTS (Auto price fill + Coupons + Points + Invoice PDF) ----------------
@app.route("/payments", methods=["GET", "POST"])
def payments():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    if request.method == "POST":
        booking_id = int(request.form["booking_id"])
        amount = int(request.form["amount"])  # base amount from service
        mode = request.form["payment_mode"]
        coupon_code = request.form.get("coupon_code", "").strip().upper()
        paid_at = datetime.now().strftime("%Y-%m-%d %H:%M")

        # get customer + membership discount
        cur.execute("""
            SELECT b.id as booking_id, c.id as customer_id,
                   mp.discount_percent
            FROM bookings b
            JOIN customers c ON b.customer_id=c.id
            LEFT JOIN customer_membership cm ON cm.customer_id=c.id AND cm.active=1
            LEFT JOIN membership_plans mp ON mp.id=cm.plan_id
            WHERE b.id=?
        """, (booking_id,))
        info = cur.fetchone()

        member_disc_percent = int(info["discount_percent"]) if info and info["discount_percent"] is not None else 0

        # coupon discount
        discount = 0
        coupon = get_coupon(cur, coupon_code)
        if coupon:
            if coupon["discount_type"] == "PERCENT":
                discount += (amount * int(coupon["discount_value"])) // 100
            else:
                discount += int(coupon["discount_value"])

        # membership discount (stacked)
        if member_disc_percent > 0:
            discount += (amount * member_disc_percent) // 100

        if discount < 0:
            discount = 0
        if discount > amount:
            discount = amount

        final_amount = amount - discount

        # points: ₹100 = 1 point (simple)
        points_added = final_amount // 100

        cur.execute("""
            INSERT INTO payments(booking_id, amount, payment_mode, paid_at, coupon_code, discount, final_amount, points_added)
            VALUES(?,?,?,?,?,?,?,?)
        """, (booking_id, amount, mode, paid_at, coupon_code if coupon_code else None, discount, final_amount, points_added))

        payment_id = cur.lastrowid

        # add points to customer
        customer_id = info["customer_id"]
        cur.execute("UPDATE customers SET points = IFNULL(points,0) + ? WHERE id=?", (points_added, customer_id))
        cur.execute("""
            INSERT INTO points_log(customer_id, points, reason, created_at)
            VALUES(?,?,?,?)
        """, (customer_id, points_added, f"Payment #{payment_id} added", paid_at))

        conn.commit()
        conn.close()
        flash(f"Payment Added 💰 | Final ₹{final_amount} | Points +{points_added}", "success")
        return redirect(url_for("payments"))

    # booking list for dropdown (include service price for auto fill)
    cur.execute("""
        SELECT b.id as booking_id, c.name as customer_name, c.car_number,
               s.service_name, s.price, b.status
        FROM bookings b
        JOIN customers c ON b.customer_id=c.id
        JOIN services s ON b.service_id=s.id
        ORDER BY b.id DESC
    """)
    booking_list = cur.fetchall()

    # payments history
    cur.execute("""
        SELECT p.*, c.name as customer_name, s.service_name, b.wash_date
        FROM payments p
        JOIN bookings b ON p.booking_id=b.id
        JOIN customers c ON b.customer_id=c.id
        JOIN services s ON b.service_id=s.id
        ORDER BY p.id DESC
    """)
    payment_list = cur.fetchall()

    conn.close()
    return render_template("payments.html", bookings=booking_list, payments=payment_list)

# ---------------- INVOICE PDF ----------------
@app.route("/invoice/<int:payment_id>")
def invoice(payment_id):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.*, b.id as booking_id, b.wash_date, b.time_slot, b.queue_no,
               c.name as customer_name, c.phone, c.car_number,
               s.service_name, s.price,
               st.staff_name
        FROM payments p
        JOIN bookings b ON p.booking_id=b.id
        JOIN customers c ON b.customer_id=c.id
        JOIN services s ON b.service_id=s.id
        LEFT JOIN staff st ON b.staff_id=st.id
        WHERE p.id=?
    """, (payment_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        flash("Invoice not found", "danger")
        return redirect(url_for("payments"))

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, h-60, "CarWash Pro - Invoice")

    c.setFont("Helvetica", 10)
    c.drawString(50, h-85, f"Invoice ID: {row['id']}   Payment Time: {row['paid_at']}")
    c.drawString(50, h-100, f"Booking ID: {row['booking_id']}   Wash Date: {row['wash_date']}   Slot: {row['time_slot'] or '-'}")
    c.drawString(50, h-115, f"Queue No: {row['queue_no'] or '-'}   Staff: {row['staff_name'] or '-'}")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, h-145, "Customer Details")
    c.setFont("Helvetica", 10)
    c.drawString(50, h-160, f"Name: {row['customer_name']}   Phone: {row['phone']}")
    c.drawString(50, h-175, f"Car Number: {row['car_number']}")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, h-205, "Service Details")
    c.setFont("Helvetica", 10)
    c.drawString(50, h-220, f"Service: {row['service_name']}")
    c.drawString(50, h-235, f"Base Amount: ₹{row['amount']}")
    c.drawString(50, h-250, f"Discount: ₹{row['discount']}  (Coupon: {row['coupon_code'] or '-'})")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, h-275, f"Final Amount Paid: ₹{row['final_amount']}")

    c.setFont("Helvetica", 10)
    c.drawString(50, 80, f"Payment Mode: {row['payment_mode']}   Points Added: {row['points_added']}")
    c.drawString(50, 60, "Thank you! Visit again 🚗✨")

    c.showPage()
    c.save()

    buffer.seek(0)
    return send_file(buffer, as_attachment=True,
                     download_name=f"invoice_{payment_id}.pdf",
                     mimetype="application/pdf")

# ---------------- MEMBERSHIP PLANS ----------------
@app.route("/memberships", methods=["GET", "POST"])
def memberships():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    if request.method == "POST":
        plan_name = request.form["plan_name"]
        monthly_fee = int(request.form["monthly_fee"])
        washes_per_month = int(request.form["washes_per_month"])
        discount_percent = int(request.form["discount_percent"])
        cur.execute("""
            INSERT INTO membership_plans(plan_name, monthly_fee, washes_per_month, discount_percent)
            VALUES(?,?,?,?)
        """, (plan_name, monthly_fee, washes_per_month, discount_percent))
        conn.commit()
        conn.close()
        flash("Plan Added ✅", "success")
        return redirect(url_for("memberships"))

    cur.execute("SELECT * FROM membership_plans ORDER BY monthly_fee")
    plans = cur.fetchall()
    conn.close()
    return render_template("memberships.html", plans=plans)

@app.route("/memberships/delete/<int:id>")
def delete_membership(id):
    if not require_login():
        return redirect(url_for("login"))
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM membership_plans WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Plan Deleted 🗑️", "success")
    return redirect(url_for("memberships"))

# ---------------- COUPONS PAGE ----------------
@app.route("/coupons", methods=["GET", "POST"])
def coupons():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()

    if request.method == "POST":
        code = request.form["code"].strip().upper()
        discount_type = request.form["discount_type"]
        discount_value = int(request.form["discount_value"])
        cur.execute("""
            INSERT OR REPLACE INTO coupons(code, discount_type, discount_value, active)
            VALUES(?,?,?,1)
        """, (code, discount_type, discount_value))
        conn.commit()
        conn.close()
        flash("Coupon Saved ✅", "success")
        return redirect(url_for("coupons"))

    cur.execute("SELECT * FROM coupons ORDER BY id DESC")
    cp = cur.fetchall()
    conn.close()
    return render_template("coupons.html", coupons=cp)

@app.route("/coupons/toggle/<int:id>")
def toggle_coupon(id):
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT active FROM coupons WHERE id=?", (id,))
    r = cur.fetchone()
    newv = 0 if r and r["active"] == 1 else 1
    cur.execute("UPDATE coupons SET active=? WHERE id=?", (newv, id))
    conn.commit()
    conn.close()
    flash("Coupon status updated ✅", "success")
    return redirect(url_for("coupons"))

# ---------------- POINTS REPORT ----------------
@app.route("/points")
def points():
    if not require_login():
        return redirect(url_for("login"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name, c.car_number, IFNULL(c.points,0) as points
        FROM customers c
        ORDER BY points DESC
    """)
    customers = cur.fetchall()

    cur.execute("""
        SELECT pl.*, c.name as customer_name
        FROM points_log pl
        JOIN customers c ON pl.customer_id=c.id
        ORDER BY pl.id DESC LIMIT 50
    """)
    logs = cur.fetchall()
    conn.close()
    return render_template("points.html", customers=customers, logs=logs)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
