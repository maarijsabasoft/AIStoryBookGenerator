import sqlite3

conn = sqlite3.connect('users.db')  # or your DB path
c = conn.cursor()
c.execute("ALTER TABLE users ADD COLUMN subscription_tier TEXT")
conn.commit()
conn.close()

print("✅ Column 'stripe_customer_id' added to users table.")
