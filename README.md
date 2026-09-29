# Smart Campus Event Registration & QR Ticket Portal

A web application built with Python (Flask) that allows students to register for campus events and instantly generates a unique QR code ticket for entry verification.

## Features
- Student registration form for campus events.
- Automatic SQLite database integration for saving participant details.
- Instant QR Code generation for tickets.
- Clean and modern HTML5/CSS3 user interface.

## Tech Stack
- **Backend:** Python, Flask
- **Database:** SQLite
- **QR Generation:** `qrcode`, `Pillow`
- **Frontend:** HTML5, CSS3

## How to Run Locally
1. Install dependencies:
   ```bash
   pip install flask qrcode pillow
#Run the application:
python app.py
Open http://127.0.0.1:5000 in your browser.
