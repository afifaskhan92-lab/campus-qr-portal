import os
import sqlite3
import qrcode
from flask import Flask, render_template, request

app = Flask(__name__)

QR_FOLDER = os.path.join('static', 'qrcodes')
os.makedirs(QR_FOLDER, exist_ok=True)

def init_db():
    conn = sqlite3.connect('campus_events.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            event_name TEXT NOT NULL,
            ticket_id TEXT UNIQUE NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name')
    email = request.form.get('email')
    event_name = request.form.get('event_name')

    ticket_id = f"TICKET-{os.urandom(3).hex().upper()}"

    conn = sqlite3.connect('campus_events.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO registrations (name, email, event_name, ticket_id) VALUES (?, ?, ?, ?)',
                   (name, email, event_name, ticket_id))
    conn.commit()
    conn.close()

    qr_data = f"Name: {name} | Ticket ID: {ticket_id} | Event: {event_name}"
    qr_img = qrcode.make(qr_data)
    qr_filename = f"{ticket_id}.png"
    qr_path = os.path.join(QR_FOLDER, qr_filename)
    qr_img.save(qr_path)

    return render_template('success.html', name=name, ticket_id=ticket_id, event=event_name, qr_image=f"qrcodes/{qr_filename}")

if __name__ == '__main__':
    app.run(debug=True)