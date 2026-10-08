import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from functools import wraps

import qrcode
from flask import (
    Flask, abort, flash, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

# ==============================================================
# SECTION 1b — Student auth helper (alag namespace from admin/staff session)
# ==============================================================

def student_required(f):
    """Sirf logged-in student andar aaye. Unauthenticated -> login page pe redirect."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_student"):
            flash("Pehle student login karein.")
            return redirect(url_for("student_login"))
        return f(*args, **kwargs)
    return wrapper

# ==============================================================
# SECTION 0 — Configuration (settings)
# ==============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "campus_events.db"))
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")
os.makedirs(QR_FOLDER, exist_ok=True)

app = Flask(__name__)

# secret_key se session (login) cookie "sign" hoti hai -> koi use chheda nahi sakta.
# Production me hamesha env variable SECRET_KEY set karo.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,                 # JS se cookie chori nahi ho sakti
    SESSION_COOKIE_SAMESITE="Lax",                # dusri website se fake POST nahi aayega
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ONLY") == "1",   # HTTPS pe hi cookie bhejo
)

# QR code ke andar jo link hota hai wo BASE_URL se banega (env me set karo).
BASE_URL = os.environ.get("BASE_URL", "")


# ==============================================================
# SECTION 1 — Database (data rakhne ki jagah)
# ==============================================================

def get_db():
    """SQLite se connection lo. Row ko dict jaisa access kar sakte ho (row['name'])."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, params=(), one=False):
    """Chhota helper: SQL chalao, result lo, connection khud close ho jaaye."""
    conn = get_db()
    try:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        conn.commit()
        if one:
            return rows[0] if rows else None
        return rows
    finally:
        conn.close()


def init_db():
    """Tables banao (agar nahi hain) + users table ke liye setup + students table."""
    conn = get_db()
    cur = conn.cursor()

    # registrations: har student ki entry (student_id nullable for backward compat)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            email      TEXT NOT NULL,
            event_name TEXT NOT NULL,
            ticket_id  TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            student_id INTEGER
        )
    """)

    # users: jinke paas login hai (ADMIN + STAFF)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL CHECK(role IN ('admin', 'staff')),
            full_name     TEXT NOT NULL DEFAULT ''
        )
    """)

    # Purane DB me created_at column nahi hai toh add kar do
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # pehle se hai -> chhod do

    # Student registration ke liye student_id column (existing rows ke liye NULL default)
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN student_id INTEGER REFERENCES students(id)")
    except sqlite3.OperationalError:
        pass  # pehle se hai -> chhod do

    # Students table banao (agar nahi hai)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            roll_no       TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            full_name     TEXT    NOT NULL DEFAULT '',
            email         TEXT    NOT NULL DEFAULT ''
        )
    """)

    # First admin account create karna (secure way)
    # Agar pehle se koi admin nahi hai, to env variable se first admin banayein
    # Production me hamesha FIRST_ADMIN_USER/FIRST_ADMIN_PASS env set karein
    if os.environ.get("FIRST_ADMIN_SETUP") == "1":
        first_user = os.environ.get("FIRST_ADMIN_USER", "admin")
        first_pass = os.environ.get("FIRST_ADMIN_PASS")
        if first_pass:
            exists = cur.execute("SELECT 1 FROM users WHERE username = ?", (first_user,)).fetchone()
            if not exists:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, ?, ?)",
                    (first_user, generate_password_hash(first_pass), "admin", "First Administrator"),
                )
                print(f"[INIT] First admin user '{first_user}' create ho gaya.")
            else:
                print(f"[INIT] Admin user '{first_user}' pehle se exist karta hai.")

    conn.commit()
    conn.close()


init_db()


# ==============================================================
# SECTION 2 — DTO (Data Transfer Object): form data ki VALIDATION
# ==============================================================
# DTO ka matlab: "jo data browser se aaya, usko ek jagah check karne wala class".
# Route sirf DTO se baat karta hai -> data sahi hai toh hi aage jaata hai.

class RegisterDTO:
    ALLOWED_EVENTS = [
        "Tech Symposium 2026",
        "AI & Robotics Workshop",
        "Annual Cultural Fest",
    ]

    # Naam: shuruaat letter se, phir letters/space/apostrophe/dot/dash (2-80 chars)
    NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,79}$")
    # Email: kuch bhi @ se pehle, kuch bhi @ ke baad, phir dot + 2+ letters
    EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
    # Ticket: verify/scan route me sirf yahi format allow hoga
    TICKET_RE = re.compile(r"^TICKET-[0-9A-F]{6,12}$")

    def __init__(self, form):
        self.name = (form.get("name") or "").strip()
        self.email = (form.get("email") or "").strip().lower()
        self.event_name = (form.get("event_name") or "").strip()
        self.errors = []   # galat cheezein ki list
        self.data = None   # sab sahi hone par yahan clean data milega

    def validate(self):
        """Saare checks ek jagah. Ek bhi fail -> errors bharo, False return."""
        self.errors = []

        if not self.NAME_RE.fullmatch(self.name):
            self.errors.append(
                "Naam sahi nahi hai: sirf letters, space, dot, apostrophe, "
                "dash use karein (min 2 characters)."
            )

        if not self.EMAIL_RE.fullmatch(self.email):
            self.errors.append("Email sahi nahi hai. Example: rahul@college.edu")

        if self.event_name not in self.ALLOWED_EVENTS:
            self.errors.append("Event choose karein (list me se ek).")

        if not self.errors:
            self.data = {
                "name": self.name,
                "email": self.email,
                "event_name": self.event_name,
            }
        return not self.errors


# ==============================================================
# SECTION 3 — Helpers: login check, role check, CSRF, rate limit
# ==============================================================

def roles_required(*allowed_roles):
    """Sirf allowed role wale user andar aaye (server-side authorization)."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Pehle login karein.")
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in allowed_roles:
                abort(403)   # "Access denied"
            return f(*args, **kwargs)
        return wrapper
    return decorator


def csrf_protect(f):
    """
    CSRF = dusri website tumhare browser se fake submit na karwa sake.
    Har POST form me hidden token hota hai -> wo session wale se match hona chahiye.
    NOTE: Jab curl ya API test kar rahe hon, to CSRF check nahi hona chahiye
    (sirf browser forms ke liye).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Sirf browser forms ke liye CSRF protection
        if request.content_type and 'application/x-www-form-urlencoded' in request.content_type:
            token = session.get("csrf_token")
            submitted = request.form.get("csrf_token")
            if not token or not submitted or submitted != token:
                abort(400, description="CSRF token missing ya galat. Page ko dobara kholen.")
        return f(*args, **kwargs)
    return wrapper


def safe_student_next(next_url):
    """Student login ke baad usi page pe wapas bhejo (par sirf apni site ke path par)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for("home")  # student ke liye default home/register page


# Simple in-memory rate limiter: window seconds me max N attempts.
_attempts = {}


def rate_limit(key, max_hits, window_sec):
    """True = limit cross ho gayi (rok do). False = theek hai."""
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window_sec]
    if len(hits) >= max_hits:
        _attempts[key] = hits
        return True
    hits.append(now)
    _attempts[key] = hits
    return False


@app.context_processor
def inject_globals():
    """Har template ko csrf_token + event list + student info (agar logged-in) milti hai."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return {
        "csrf_token": session["csrf_token"],
        "EVENTS": RegisterDTO.ALLOWED_EVENTS,
        "student": {
            "id": session.get("student_id"),
            "roll_no": session.get("roll_no"),
            "full_name": session.get("full_name"),
            "email": session.get("email"),
        } if session.get("is_student") else None,
    }


def safe_next(next_url, role):
    """Login ke baad usi page pe wapas bhejo (par sirf apni site ke path par)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    if role == "admin":
        return url_for("dashboard")
    if role == "staff":
        return url_for("scan")
    # student role ke liye (agar future student role same users table me) -> home
    return url_for("home")


# ==============================================================
# SECTION 4 — AUTH routes: login / logout / user registration
# ==============================================================

# ---- User registration (admin-only: admin creates new staff users) ----

@app.route("/register-user", methods=["GET", "POST"])
@roles_required("admin")
def register_user():
    """Admin creates new staff user accounts. Public registration is not allowed
    for security - each user must be approved by an admin."""
    return _render_user_management(error=None, success=None)


@app.route("/users")
@roles_required("admin")
def list_users():
    """Admin can see all users (without password hashes)."""
    users = query("SELECT id, username, role, full_name FROM users ORDER BY id")
    return render_template("list_users.html", users=users)


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_user(user_id):
    """Admin can edit user details (username, full_name, role) but NOT password here.
    Password change is separate via /change-password."""
    user = query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if not user:
        abort(404)

    error = None
    success = None

    if request.method == "POST":
        new_username = (request.form.get("username") or "").strip().lower()
        new_full_name = (request.form.get("full_name") or "").strip()
        new_role = request.form.get("role") or "staff"

        if not new_username or len(new_username) < 3:
            error = "Username kam se 3 characters ka hona chahiye."
        elif not re.match(r"^[a-z0-9_]+$", new_username):
            error = "Username sirf letters, numbers, aur underscore ho sakta hai."
        elif not new_full_name:
            error = "Full name zaroori hai."
        elif new_role not in ("admin", "staff"):
            error = "Invalid role."
        else:
            # Check if new username conflicts with another user
            conflict = query(
                "SELECT 1 FROM users WHERE username = ? AND id != ?",
                (new_username, user_id),
                one=True,
            )
            if conflict:
                error = f"Username '{new_username}' pehle se exist karta hai."
            else:
                query(
                    "UPDATE users SET username = ?, full_name = ?, role = ? WHERE id = ?",
                    (new_username, new_full_name, new_role, user_id),
                )
                success = f"User '{new_username}' update ho gaya!"

    return render_template(
        "edit_user.html",
        user=user,
        error=error,
        success=success,
    )


@app.route("/change-password", methods=["GET", "POST"])
@roles_required("admin")
def change_password_page():
    """Admin can change their own password from the dashboard."""
    return _render_password_change(error=None, success=None)


@app.route("/users/<int:user_id>/change-password", methods=["GET", "POST"])
@roles_required("admin")
def change_user_password(user_id):
    """Admin can change another user's password (e.g., staff password reset)."""
    user = query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if not user:
        abort(404)

    return _render_password_change(error=None, success=None, target_user=user)


def _render_password_change(error=None, success=None, target_user=None):
    """Common password change logic for self or other user."""
    target_label = target_user["username"] if target_user else "apna"

    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        # Validate: must have current password to change (prevents CSRF + unauthorized)
        if not current_password:
            error = "Current password dalein (security ke liye)."
        elif len(new_password) < 6:
            error = "Naya password kam se 6 characters ka hona chahiye."
        elif new_password != confirm_password:
            error = "Naya password aur confirm password match nahi kar rahe."
        else:
            # Verify current password (only for self-change, not for admin resetting others)
            if target_user:  # Admin resetting another user's password
                # Admin can reset any user's password without knowing old one
                pass  # Admin privilege - no current password needed for others
            else:  # Self password change
                current_user = query(
                    "SELECT * FROM users WHERE id = ?", (session.get("user_id"),), one=True
                )
                if not current_user or not check_password_hash(
                    current_user["password_hash"], current_password
                ):
                    error = "Current password galat hai."

        if not error:
            # Update password
            target_id = target_user["id"] if target_user else session.get("user_id")
            query(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), target_id),
            )
            success = f"Password change ho gaya (user: {target_label})."

    return render_template(
        "change_password.html",
        error=error,
        success=success,
        target_user=target_user,
        target_label=target_label,
    )


def _render_user_management(error=None, success=None):
    """Render register_user form (same as before)."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        full_name = (request.form.get("full_name") or "").strip()
        role = request.form.get("role") or "staff"

        if not username or len(username) < 3:
            error = "Username kam se 3 characters ka hona chahiye."
        elif not re.match(r"^[a-z0-9_]+$", username):
            error = "Username sirf letters, numbers, aur underscore ho sakta hai."
        elif len(password) < 6:
            error = "Password kam se 6 characters ka hona chahiye."
        elif password != confirm_password:
            error = "Password aur confirm password match nahi kar rahe."
        elif not full_name:
            error = "Full name zaroori hai."
        elif role not in ("admin", "staff"):
            error = "Invalid role."
        else:
            existing = query("SELECT 1 FROM users WHERE username = ?", (username,), one=True)
            if existing:
                error = f"Username '{username}' pehle se exist karta hai."
            else:
                query(
                    "INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, ?, ?)",
                    (username, generate_password_hash(password), role, full_name),
                )
                success = f"User '{username}' successfully create ho gaya!"

    return render_template(
        "register_user.html",
        error=error,
        success=success,
        current_admin=session.get("full_name"),
    )


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if session.get("user_id"):            return redirect(safe_next(request.args.get("next", ""), session.get("role", "staff")))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""

        # Brute-force se bachne ke liye: 5 try / 5 minute per IP+username
        if rate_limit(f"login:{request.remote_addr}:{username}", 5, 300):
            abort(429)

        user = query("SELECT * FROM users WHERE username = ?", (username,), one=True)

        # check_password_hash hi purana hash + naya password match karta hai
        if user and check_password_hash(user["password_hash"], password):
            _attempts.pop(f"login:{request.remote_addr}:{username}", None)  # success -> reset
            session.clear()                       # session fixation se bachne ke liye
            session["csrf_token"] = secrets.token_hex(16)
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["full_name"] = user["full_name"]
            flash(f"Welcome, {user['full_name']}!")
            return redirect(safe_next(request.args.get("next", ""), user["role"]))

        # Generic message -> attacker ko pata na chale user exist karta hai
        return render_template("login.html", error="Galat username ya password."), 401

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logout ho gaya.")
    return redirect(url_for("home"))


# ---- Student login (roll_no + password) ----

@app.route("/student-login", methods=["GET", "POST"])
def student_login():
    if session.get("is_student"):
        return redirect(url_for("home"))

    if request.method == "POST":
        roll_no = (request.form.get("roll_no") or "").strip()
        password = request.form.get("password") or ""

        # Brute-force se bachne ke liye: 5 try / 5 minute per IP+roll_no
        if rate_limit(f"student_login:{request.remote_addr}:{roll_no}", 5, 300):
            abort(429)

        # Specific columns only (verify pattern)
        student = query(
            "SELECT id, roll_no, password_hash, full_name, email FROM students WHERE roll_no = ?",
            (roll_no,),
            one=True,
        )

        if student and check_password_hash(student["password_hash"], password):
            _attempts.pop(f"student_login:{request.remote_addr}:{roll_no}", None)  # success -> reset
            session.clear()                       # session fixation se bachne ke liye
            session["csrf_token"] = secrets.token_hex(16)
            session["is_student"] = True
            session["student_id"] = student["id"]
            session["roll_no"] = student["roll_no"]
            session["full_name"] = student["full_name"]
            session["email"] = student["email"]
            # student ke liye role='student' hi set nahi hoga -> alag namespace
            flash(f"Welcome, {student['full_name']}!")
            return redirect(safe_student_next(request.args.get("next", "")))

        # Generic message -> attacker ko pata na chale roll_no exist karta hai
        return render_template("student_login.html", error="Galat roll number ya password."), 401

    return render_template("student_login.html")


@app.route("/student-logout")
def student_logout():
    session.clear()
    flash("Logout ho gaya.")
    return redirect(url_for("home"))


# ==============================================================
# SECTION 5 — Public routes: form + register (DTO validation) + verify
# ==============================================================

@app.route('/')
def home():
    return render_template("index.html", form={}, errors=None)


@app.route("/register", methods=["POST"])
def register():
    # ---- AUTH: sirf logged-in student hi registration kar sake ----
    if not session.get("is_student"):
        flash("Pehle student login karein, phir event register karein.")
        return redirect(url_for("student_login"))

    # IP wise spam rok do: 1 minute me max 10 registration
    if rate_limit(f"register:{request.remote_addr}", 10, 60):
        abort(429)

    # ---- Validation: event allowlist check (name/email student account se aate hain) ----
    # Form se sirf event_name le; name/email form se nahi -> session/database se lete hain
    event_name = (request.form.get("event_name") or "").strip()
    if event_name not in RegisterDTO.ALLOWED_EVENTS:
        return render_template(
            "index.html",
            form={"name": session.get("full_name", ""), "email": session.get("email", ""), "event_name": event_name},
            errors=["Event choose karein (list me se ek)."],
        ), 400

    # ---- Ticket banao: 12 random hex chars (48 bits -> guess karna mushkil) ----
    ticket_id = "TICKET-" + secrets.token_hex(6).upper()

    # ---- Student account se name/email lete hain (server-side, client form se nahi) ----
    name = session.get("full_name", "")
    email = session.get("email", "")
    student_id = session.get("student_id")

    query(
        "INSERT INTO registrations (name, email, event_name, ticket_id, created_at, student_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            name,
            email,
            event_name,
            ticket_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            student_id,
        ),
    )

    # ---- QR code banao ----
    # BASE_URL env se aata hai -> host-header injection se bachav
    base = (BASE_URL or request.host_url).rstrip("/")
    qr_data = f"{base}/verify/{ticket_id}"
    qr_filename = f"{ticket_id}.png"   # ticket_id sirf hex hai -> path traversal nahi
    qrcode.make(qr_data).save(os.path.join(QR_FOLDER, qr_filename))

    return render_template(
        "success.html",
        name=name,
        email=email,
        event=event_name,
        ticket_id=ticket_id,
        qr_file=f"qrcodes/{qr_filename}",
    )


@app.route('/verify/<ticket_id>')
def verify(ticket_id):
    """
    QR scan karne par ye page khulta hai (gate pe).
    Data TEMPLATE me render hota hai -> Jinja khud escape karta hai -> XSS nahi.
    """
    ticket_id = (ticket_id or "").strip().upper()
    registration = None

    # Pehle format check, phir DB query (koi bhi string seedha SQL me nahi jaati)
    if RegisterDTO.TICKET_RE.fullmatch(ticket_id):
        registration = query(
            "SELECT name, email, event_name, ticket_id FROM registrations WHERE ticket_id = ?",
            (ticket_id,),
            one=True,
        )

    return render_template(
        "verify.html",
        valid=registration is not None,
        reg=registration,
        ticket_id=ticket_id,
    ), (200 if registration else 404)


# ==============================================================
# SECTION 6 — STAFF route: gate pe ticket check karo
# ==============================================================

@app.route("/staff/scan", methods=["GET", "POST"])
@roles_required("admin", "staff")
def scan():
    checked = False
    reg = None
    typed = ""

    if request.method == "POST":
        checked = True
        typed = (request.form.get("ticket_id") or "").strip().upper()
        if RegisterDTO.TICKET_RE.fullmatch(typed):
            reg = query(
                "SELECT * FROM registrations WHERE ticket_id = ?", (typed,), one=True
            )

    return render_template("scan.html", checked=checked, reg=reg, typed=typed)


# ==============================================================
# SECTION 7 — ADMIN route: dashboard (saara data ek jagah)
# ==============================================================

@app.route("/dashboard")
@roles_required("admin")
def dashboard():
    q = (request.args.get("q") or "").strip()

    total = query("SELECT COUNT(*) AS c FROM registrations", one=True)["c"]
    unique_students = query(
        "SELECT COUNT(DISTINCT email) AS c FROM registrations", one=True
    )["c"]
    per_event = query(
        "SELECT event_name, COUNT(*) AS c FROM registrations"
        " GROUP BY event_name ORDER BY c DESC"
    )

    if q:
        like = f"%{q}%"   # % wildcards hain -> par value parameterized hai (safe)
        rows = query(
            "SELECT * FROM registrations WHERE name LIKE ? OR email LIKE ?"
            " OR ticket_id LIKE ? ORDER BY id DESC",
            (like, like, like),
        )
    else:
        rows = query("SELECT * FROM registrations ORDER BY id DESC")

    return render_template(
        "dashboard.html",
        total=total,
        unique_students=unique_students,
        per_event=per_event,
        rows=rows,
        q=q,
    )


# ==============================================================
# SECTION 8 — Error pages (traceback mat dikhao)
# ==============================================================

def error_page(code, message):
    return render_template("error.html", code=code, message=message), code


@app.errorhandler(400)
def handle_400(e):
    return error_page(400, getattr(e, "description", None) or "Galat request.")


@app.errorhandler(403)
def handle_403(e):
    return error_page(403, "Aapke paas is page ka access nahi hai.")


@app.errorhandler(404)
def handle_404(e):
    return error_page(404, "Ye page ya ticket nahi mili.")


@app.errorhandler(429)
def handle_429(e):
    return error_page(429, "Bahut zyada requests! Kuch minute ruk kar dobara try karein.")


@app.errorhandler(500)
def handle_500(e):
    return error_page(500, "Server ko kuch problem hui. Terminal me error check karein.")


# ==============================================================
# SECTION 9 — Run
# ==============================================================
if __name__ == '__main__':
    # NOTE: debug sirf apne laptop pe rakho. Production me DEBUG=0 aur HTTPS.
    debug = os.environ.get("DEBUG", "1") == "1"
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")   # sirf apne computer pe (secure)
    app.run(host=host, port=port, debug=debug)
   