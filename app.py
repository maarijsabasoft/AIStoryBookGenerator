from flask import flash, Flask, render_template, session, request, jsonify, send_from_directory, url_for, redirect
from flask_session import Session
import os
from groq import Groq
import google.generativeai as genai
from reportlab.lib.pagesizes import letter, A4, legal
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Image, Table, TableStyle
from reportlab.lib.units import inch
from PIL import Image as PILImage
from PIL import ImageDraw
from io import BytesIO
import uuid
import base64
import json
import arabic_reshaper
from bidi.algorithm import get_display
import sqlite3
import bcrypt
from google.oauth2 import id_token
from google.auth.transport import requests
from functools import wraps
import logging
import secrets
import datetime
import stripe
from groq_sdk import GroqClient  # make sure this is installed
# Conditional import for elevenlabs
try:
    from elevenlabs.client import ElevenLabs
    ELEVENLABS_AVAILABLE = True
except ImportError:
    print("Warning: elevenlabs not found. Video generation will be disabled.")
    ELEVENLABS_AVAILABLE = False

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET") 
stripe.api_key = STRIPE_SECRET_KEY

# Stripe Price IDs
STRIPE_PLANS = {
    "standard": "price_1SKDxKBHO8g2Q8ZQNntVamDV",
    "premium": "price_1SKDxsBHO8g2Q8ZQRatSKXNj"
}

# OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Flask App Configuration
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config['SECRET_KEY'] = secrets.token_hex(16)
app.config['SESSION_COOKIE_SECURE'] = False  # Use secure cookies in production
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=7)
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# OAuth Setup
from authlib.integrations.flask_client import OAuth
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
pdfmetrics.registerFont(TTFont('Comic', 'fonts/Comic.ttf'))

# Database Setup
USER_DB = os.path.join(os.path.dirname(__file__), "users.db")
os.makedirs('static', exist_ok=True)
os.makedirs('templates', exist_ok=True)

# API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Initialize clients
groq_client = GroqClient(api_key=GROQ_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_AVAILABLE and ELEVENLABS_API_KEY != "your_elevenlabs_api_key_here" else None

RTL_LANGUAGES = ['Urdu', 'Arabic']

# SQLite Database Setup
def init_db():
    with sqlite3.connect(USER_DB) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT,
            google_id TEXT,
            subscription_tier TEXT DEFAULT 'basic',
            stripe_customer_id TEXT,
            stories_this_month INTEGER DEFAULT 0,
            last_story_month TEXT DEFAULT ''
        )''')
        conn.commit()

init_db()

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please sign in to access this feature', 'error')
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function

# Helper function to get database connection
def get_db():
    conn = sqlite3.connect(USER_DB)
    conn.row_factory = sqlite3.Row
    return conn

# Flash messages endpoint
@app.route('/api/flash-messages')
def get_flash_messages():
    messages = []
    with app.test_request_context():
        for message, category in get_flashed_messages(with_categories=True):
            messages.append({'message': message, 'category': category})
    return jsonify(messages)

# Signup route
@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    
    if not all([name, email, password]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    
    try:
        with get_db() as conn:
            c = conn.cursor()
            user_id = str(uuid.uuid4())
            c.execute('INSERT INTO users (id, name, email, password) VALUES (?, ?, ?, ?)',
                     (user_id, name, email, hashed_password))
            conn.commit()
            
            session['user_id'] = user_id
            session['user_name'] = name
            session['user_email'] = email
            session.permanent = True
            
            return jsonify({
                'message': 'User created successfully',
                'user': {
                    'id': user_id,
                    'name': name,
                    'email': email,
                    'subscription_tier': 'basic'
                }
            }), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Email already exists'}), 400
    except Exception as e:
        logger.error(f"Signup error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

# Google Login
@app.route("/google-login")
def google_login():
    callback_url = url_for('google_auth_callback', _external=True)
    nonce = secrets.token_urlsafe(16)
    session['google_nonce'] = nonce
    logger.debug(f"Initiating Google login with callback: {callback_url}, nonce: {nonce}")
    try:
        return google.authorize_redirect(callback_url, nonce=nonce)
    except Exception as e:
        logger.error(f"Google login initiation failed: {str(e)}", exc_info=True)
        flash(f"Google login failed: {str(e)}", "error")
        return redirect("/")

# Google Auth Callback
@app.route("/auth/google/callback")
def google_auth_callback():
    try:
        token = google.authorize_access_token()
        nonce = session.pop('google_nonce', None)
        user_info = google.parse_id_token(token, nonce=nonce)
        
        email = user_info.get("email")
        username = user_info.get("name", email.split("@")[0])
        
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE email = ?", (email,))
            user = c.fetchone()
            
            if not user:
                user_id = str(uuid.uuid4())
                c.execute(
                    "INSERT INTO users (id, name, email, password) VALUES (?, ?, ?, ?)",
                    (user_id, username, email, None)
                )
                conn.commit()
            else:
                user_id = user["id"]
            
            session['user_id'] = user_id
            session['user_name'] = username
            session['user_email'] = email
            session.permanent = True
            
            flash("Logged in successfully via Google!", "success")
            return redirect("/")
    except Exception as e:
        logger.error(f"Google login failed: {str(e)}", exc_info=True)
        flash(f"Google login failed: {str(e)}", "error")
        return redirect("/")

# Login route
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    
    if not all([email, password]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['user_email'] = user['email']
            session.permanent = True
            return jsonify({
                'message': 'Login successful',
                'user': {
                    'id': user['id'],
                    'name': user['name'],
                    'email': user['email'],
                    'subscription_tier': user['subscription_tier']
                }
            }), 200
        else:
            return jsonify({'error': 'Invalid credentials'}), 401

# Logout route
@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out successfully'}), 200

# Get current user
@app.route('/api/current_user', methods=['GET'])
def current_user():
    if 'user_id' in session:
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT id, name, email, subscription_tier FROM users WHERE id = ?', (session['user_id'],))
            user = c.fetchone()
            if user:
                return jsonify({
                    'user': {
                        'id': user['id'],
                        'name': user['name'],
                        'email': user['email'],
                        'subscription_tier': user['subscription_tier']
                    }
                }), 200
    return jsonify({'user': None}), 200

# Create Stripe Checkout Session
@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    data = request.get_json()
    plan = data.get('plan')

    if plan not in STRIPE_PLANS:
        return jsonify({'error': 'Invalid plan selected'}), 400

    price_id = STRIPE_PLANS[plan]

    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT stripe_customer_id FROM users WHERE id = ?', (session['user_id'],))
        result = c.fetchone()
        stripe_customer_id = result[0] if result else None

    if not stripe_customer_id:
        customer = stripe.Customer.create(email=session['user_email'])
        stripe_customer_id = customer['id']
        with get_db() as conn:
            conn.execute(
                'UPDATE users SET stripe_customer_id = ? WHERE id = ?',
                (stripe_customer_id, session['user_id'])
            )
            conn.commit()

    state_token = secrets.token_urlsafe(32)
    session['stripe_state'] = state_token

    checkout_session = stripe.checkout.Session.create(
        customer=stripe_customer_id,
        payment_method_types=['card'],
        line_items=[{
            'price': price_id,
            'quantity': 1,
        }],
        mode='subscription',
        success_url=f'http://127.0.0.1:5000/success?session_id={{CHECKOUT_SESSION_ID}}&state={state_token}',
        cancel_url=f'http://127.0.0.1:5000/cancel?state={state_token}',
        metadata={'plan': plan}
    )

    return jsonify({
        'id': checkout_session.id,
        'url': checkout_session.url
    })

# Cancel Subscription
@app.route('/api/cancel-subscription', methods=['POST'])
@login_required
def cancel_subscription():
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT stripe_customer_id FROM users WHERE id = ?', (session['user_id'],))
            result = c.fetchone()
            stripe_customer_id = result[0] if result else None

            if stripe_customer_id:
                subscriptions = stripe.Subscription.list(customer=stripe_customer_id, status='active')
                for sub in subscriptions.data:
                    stripe.Subscription.delete(sub.id)

                c.execute('UPDATE users SET subscription_tier = "basic" WHERE id = ?', (session['user_id'],))
                conn.commit()

            return jsonify({'message': 'Subscription cancelled successfully'})
    except Exception as e:
        logger.error(f"Cancel subscription error: {str(e)}", exc_info=True)
        return jsonify({'error': f'Failed to cancel subscription: {str(e)}'}), 500

# Stripe Webhook
@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        return jsonify({'error': 'Invalid signature'}), 400
    
    if event['type'] == 'checkout.session.completed':
        session_data = event['data']['object']
        if session_data['mode'] == 'subscription':
            email = session_data.get('customer_email')
            customer_id = session_data['customer']
            plan = session_data['metadata'].get('plan')
            if plan and email:
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute('UPDATE users SET subscription_tier = ?, stripe_customer_id = ? WHERE email = ?',
                              (plan, customer_id, email))
                    conn.commit()
    
    elif event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        customer_id = sub['customer']
        with get_db() as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET subscription_tier = "basic" WHERE stripe_customer_id = ?', (customer_id,))
            conn.commit()
    
    return jsonify(success=True)

# Success Route
@app.route('/success')
def success():
    state = request.args.get('state')
    session_id = request.args.get('session_id')
    
    if state and session.get('stripe_state') == state:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            plan = checkout_session.metadata.get('plan')
            customer_id = checkout_session.customer
            email = checkout_session.customer_email or session.get('user_email')

            with get_db() as conn:
                c = conn.cursor()
                c.execute('UPDATE users SET subscription_tier = ?, stripe_customer_id = ? WHERE email = ?',
                          (plan, customer_id, email))
                conn.commit()

            flash('Subscription successful!', 'success')
            return redirect('/')
        except stripe.error.StripeError as e:
            flash(f'Subscription error: {str(e)}', 'error')
            return redirect('/')
    else:
        flash('Invalid session state', 'error')
        return redirect('/')

# Cancel Route
@app.route('/cancel')
def cancel():
    state = request.args.get('state')
    if state and session.get('stripe_state') == state:
        flash('Subscription cancelled', 'info')
    else:
        flash('Invalid session state', 'error')
    return redirect('/')

def get_font_for_language(language):
    return 'Comic'

def process_text_for_pdf(text, language):
    if language in RTL_LANGUAGES:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    return text

def validate_image_for_pdf(img_data):
    try:
        img = PILImage.open(BytesIO(img_data))
        if img.format not in ['PNG', 'JPEG']:
            logger.warning(f"Unsupported image format: {img.format}")
            img = img.convert('RGB')
            buf = BytesIO()
            img.save(buf, format='PNG', quality=95)
            return buf.getvalue()
        return img_data
    except Exception as e:
        logger.error(f"Image validation failed: {str(e)}", exc_info=True)
        return None

def create_fallback_image(width, height, message="Image Generation Failed"):
    img = PILImage.new('RGB', (width, height), color=(255, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((10, height//2), message, fill=(255, 255, 255))
    buf = BytesIO()
    img.save(buf, format='PNG', quality=95)
    return buf.getvalue()

def create_story_image(text, width=512, height=512):
    try:
        summary_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "Convert the given text into a concise, vivid prompt (1-2 sentences) for generating a colorful children's story illustration."
                },
                {
                    "role": "user",
                    "content": f"Text: {text}"
                }
            ],
            model="llama-3.3-70b-versatile",
            max_tokens=80
        )
        image_prompt = summary_completion.choices[0].message.content
        logger.info(f"Generated image prompt: {image_prompt}")

        model = genai.GenerativeModel('gemini-2.5-flash-image')
        response = model.generate_content([image_prompt])
        logger.debug(f"Gemini API response: {response}")

        image_parts = [
            part.inline_data.data
            for part in response.candidates[0].content.parts
            if part.inline_data and hasattr(part.inline_data, 'data')
        ]
        if not image_parts:
            logger.error("No valid image data received from Gemini API")
            return create_fallback_image(width, height, "No Image Data")

        img = PILImage.open(BytesIO(image_parts[0]))
        img = img.resize((width, height))
        buf = BytesIO()
        img.save(buf, format='PNG', quality=95)
        return buf.getvalue()

    except Exception as e:
        logger.error(f"Error creating image: {str(e)}", exc_info=True)
        if "Quota exceeded" in str(e):
            return create_fallback_image(width, height, "Quota Exceeded")
        return create_fallback_image(width, height)

def generate_video(title, story_pages, images_bytes, language):
    # if not MOVIEPY_AVAILABLE or not ELEVENLABS_AVAILABLE or not eleven_client:
    #     logger.warning("Video generation unavailable: Missing moviepy or elevenlabs.")
    #     return None

    # clips = []
    # voice_id = "EXAVITQu4vr4xnSDxMaL"
    # model_id = "eleven_multilingual_v2" if language != 'English' else "eleven_monolingual_v1"

    # try:
    #     title_audio = eleven_client.generate(text=title, voice=voice_id, model=model_id)
    #     with open('temp_title.mp3', 'wb') as f:
    #         f.write(title_audio)
    #     title_audio_clip = AudioFileClip('temp_title.mp3')
    #     dur = title_audio_clip.duration

    #     title_img = ImageClip(images_bytes[0]).set_duration(dur).fadein(1).set_fps(30)
    #     title_img = title_img.resize(lambda t: 1 + 0.05 * (t / dur))
    #     title_text_clip = TextClip(
    #         lambda t: title[:int(len(title) * (t / dur))], 
    #         fontsize=50, 
    #         color='white', 
    #         font='Arial', 
    #         method='caption',
    #         align='center' if language not in RTL_LANGUAGES else 'East'
    #     ).set_position('center').set_duration(dur)
    #     title_comp = CompositeVideoClip([title_img, title_text_clip])
    #     clips.append(title_comp.set_audio(title_audio_clip))
    #     os.unlink('temp_title.mp3')
    # except Exception as e:
    #     logger.error(f"Title video clip generation failed: {e}", exc_info=True)

    # for i, text in enumerate(story_pages):
    #     try:
    #         page_audio = eleven_client.generate(text=text, voice=voice_id, model=model_id)
    #         with open(f'temp_page_{i}.mp3', 'wb') as f:
    #             f.write(page_audio)
    #         page_audio_clip = AudioFileClip(f'temp_page_{i}.mp3')
    #         dur = page_audio_clip.duration

    #         page_img = ImageClip(images_bytes[i + 1]).set_duration(dur).fadein(1).set_fps(30)
    #         page_img = page_img.resize(lambda t: 1 + 0.05 * (t / dur))

    #         page_text_clip = TextClip(
    #             lambda t: text[:int(len(text) * (t / dur))], 
    #             fontsize=24, 
    #             color='white', 
    #             font='Arial', 
    #             method='caption', 
    #             size=(600, None),
    #             align='center' if language not in RTL_LANGUAGES else 'East'
    #         ).set_position(('center', 'bottom')).set_duration(dur)

    #         page_comp = CompositeVideoClip([page_img, page_text_clip])
    #         clips.append(page_comp.set_audio(page_audio_clip))
    #         os.unlink(f'temp_page_{i}.mp3')
    #     except Exception as e:
    #         logger.error(f"Page {i+1} video clip generation failed: {e}", exc_info=True)

    # try:
    #     final_clip = concatenate_videoclips(clips, method="compose")
    #     uid = str(uuid.uuid4())
    #     video_path = f"static/{uid}.mp4"
    #     final_clip.write_videofile(video_path, fps=30, codec='libx264', audio_codec='aac')
    #     return f"/static/{uid}.mp4"
    # except Exception as e:
    #     logger.error(f"Video concatenation failed: {e}", exc_info=True)
    return None

@app.route('/')
def home():
    return render_template('index.html', stripe_pk=STRIPE_PUBLIC_KEY)
@app.route('/create')
def create_page():
    return redirect('/')  # redirect to home

@app.route('/generate', methods=['POST'])
def generate():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400
            
        prompt = data.get('prompt', '')
        length = int(data.get('length', 5))
        language = data.get('language', 'English')
        premium = False  # always non-premium
        orientation = 'a4_portrait'  # always A4 portrait


        # Validate page length based on subscription tier
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT subscription_tier FROM users WHERE id = ?', (session['user_id'],))
            user = c.fetchone()
            subscription_tier = user['subscription_tier'] if user else 'basic'

        max_pages = {'basic': 5, 'standard': 10, 'premium': 20}
        if length < 1 or length > max_pages.get(subscription_tier, 5):
            return jsonify({'error': f'Invalid page length for {subscription_tier} tier. Choose between 1 and {max_pages.get(subscription_tier)} pages.'}), 400

        if not prompt:
            return jsonify({'error': 'Prompt is required'}), 400

        # # Validate orientation
        # orientation_map = {
        #     'a4_portrait': A4,
        #     'a4_landscape': (A4[1], A4[0]),
        #     'letter_portrait': letter,
        #     'letter_landscape': (letter[1], letter[0]),
        #     'legal_portrait': legal,
        #     'legal_landscape': (legal[1], legal[0])
        # # }
        # if orientation not in orientation_map:
        #     return jsonify({'error': 'Invalid orientation selected'}), 400
        # pagesize = orientation_map[orientation]

        logger.info(f"Generating story: prompt={prompt}, length={length}, language={language}")

        # Generate story with Groq
        try:
            chat_completion = groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "system", 
                        "content": f"""You are a children's story writer. Create a simple, engaging story in {language}.
                        Each page should have detailed text (200-250 words) that tells a complete part of the story.
                        Make it fun and educational for children ages 4-8.
                        Do not add page numbers in the text.
                        Output must be valid JSON format only:
                        {{"title": "Story Title", "pages": ["page 1 text", "page 2 text", ...]}}
                        Ensure each page text is substantial enough to fill a page with proper formatting."""
                    },
                    {
                        "role": "user", 
                        "content": f"Create a {length-1} page children's story in {language} about: {prompt}. Make each page text detailed and engaging with 200-250 words."
                    }
                ],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"}
            )
            
            story_response = chat_completion.choices[0].message.content
            logger.debug(f"Story response: {story_response}")
            
            story_data = json.loads(story_response)
            title = story_data.get('title', 'Magical Adventure')
            story_pages = story_data.get('pages', [])
            
        except Exception as e:
            logger.error(f"Story generation failed: {str(e)}", exc_info=True)
            title = "The Magical Adventure"
            story_pages = [
                "Once upon a time, in a beautiful land filled with colorful flowers and singing birds, there lived a curious little explorer named Alex. Alex loved discovering new places and meeting new friends. Every day was an adventure waiting to happen in this magical world. The sun shone brightly, painting the sky in shades of blue and gold. Alex's home was a cozy cottage at the edge of the Enchanted Forest, surrounded by tall trees and sparkling streams. Surrounded by a garden of roses and daisies, the cottage had a thatched roof and a chimney puffing gentle smoke. Inside, Alex had a collection of maps and treasures from past journeys. One day, Alex found an old map that promised hidden wonders deep in the forest. Excited, Alex packed a backpack with snacks, a water bottle, and a flashlight. The map showed a path leading to a secret clearing where magic happened. Alex wondered what surprises awaited. As Alex stepped into the forest, the air filled with the sweet scent of pine and wildflowers. Birds chirped melodies, and butterflies fluttered by in a rainbow of colors. ",
                "One sunny morning, Alex decided to explore the Enchanted Forest. The trees whispered secrets as Alex walked along the winding path. Bright butterflies danced in the air, and friendly squirrels played hide and seek among the branches. It was the most wonderful forest anyone had ever seen. Sunlight filtered through the leaves, creating patterns of light and shadow on the ground. Alex spotted colorful mushrooms growing in clusters, some red with white spots, others blue and glowing faintly. A gentle breeze rustled the leaves, carrying the sound of distant laughter. As Alex ventured deeper, the path led to a meadow filled with wildflowers of every hue. Bees buzzed from bloom to bloom, collecting nectar. In the center stood an ancient oak tree, its branches reaching high into the sky. Alex rested against its trunk, feeling the rough bark and listening to the heartbeat of the earth. Suddenly, a family of deer appeared, grazing peacefully. The fawn approached curiously, its big eyes full of wonder. Alex shared some apples from the backpack, and they became instant friends. ",
                "As Alex ventured deeper into the forest, a talking rabbit with a tiny waistcoat appeared. 'Hello there!' said the rabbit. 'I'm Cotton, the guardian of the forest. Would you like to see something truly magical?' Alex nodded excitedly, wondering what amazing discovery awaited them. Cotton hopped ahead, leading the way through thick underbrush and over mossy logs. The forest grew denser, with vines hanging like curtains and fireflies beginning to glow as dusk approached. They passed a grove of glowing trees, their trunks illuminated with soft light. Cotton explained how the trees absorbed sunlight during the day and shared it at night. Alex marveled at the wonder. Soon, they arrived at a hidden glade where fairies danced in the moonlight. The fairies had delicate wings shimmering like rainbows, and they sang songs that made flowers bloom instantly. Cotton introduced Alex as a friend of the forest. The fairies welcomed Alex with open arms, teaching dances and sharing stories of ancient magic. Alex learned spells for kindness and bravery. One fairy gifted a magical amulet that glowed when danger was near.",
                "Cotton led Alex to a hidden clearing where a magnificent crystal fountain sparkled in the sunlight. The water shimmered with all the colors of the rainbow. 'This is the Fountain of Friendship,' explained Cotton. 'Its waters help all creatures understand each other and live in harmony.' The fountain was carved from pure crystal, with intricate designs of animals and plants. Water bubbled up from the center, cascading into pools where reflections danced like living paintings. Alex dipped a hand in the cool water, feeling a warm tingle of magic. Suddenly, animals from all over the forest gathered around. Birds, squirrels, deer, and even a wise fox came to drink from the fountain. They shared stories in a language Alex could now understand. The fox told of clever escapes, the birds sang of high adventures, and the deer spoke of peaceful meadows. Alex realized the importance of listening and empathy. Together, they solved a puzzle to reveal a hidden treasure chest filled with glowing gems. Each gem represented a virtue like courage, kindness, and wisdom. Alex chose one for friendship and promised to cherish it.",
                "Alex and Cotton became the best of friends, exploring the forest together every day. They learned that true friendship is the greatest magic of all. And they lived happily ever after, sharing many more wonderful adventures in their magical world full of joy and laughter. Back at the cottage, Alex hung the magical amulet and placed the gem on a shelf. Every evening, Alex and Cotton recounted their tales to other forest friends. They organized gatherings where everyone shared experiences, fostering unity. Alex discovered new parts of the forest, like a waterfall that sang lullabies and a cave with glowing crystals. Each adventure taught valuable lessons about nature, cooperation, and imagination. Cotton taught Alex how to read animal tracks and identify plants. In return, Alex shared human stories and games. Together, they helped solve problems, like finding a lost baby bird or mending a broken bridge. Their friendship inspired others to form bonds across species. The Enchanted Forest thrived with harmony and magic. As years passed, Alex grew, but the wonder never faded. The map led to endless discoveries, each more enchanting than the last."
            ]

        # Ensure we have enough pages
        while len(story_pages) < length - 1:
            additional_pages = [
                "The adventure continued with even more excitement and wonder. New friends joined the journey, each bringing their own special talents and stories to share. Every corner of the magical world revealed new surprises and lessons about kindness and courage. The group explored a hidden valley where flowers sang and rivers flowed with honey. They met a wise turtle who shared ancient wisdom. Together, they solved riddles to open secret doors. Behind each door was a new wonder, like a garden of floating lanterns or a library of living books. Pages turned themselves, telling tales of heroes past. Alex learned the power of knowledge and perseverance. As night fell, they camped under the stars, sharing dreams and hopes. The bonds grew stronger, creating a family of adventurers. The valley echoed with laughter and songs, teaching the value of unity and exploration.",
                "As the sun began to set, painting the sky in hues of orange and pink, our heroes realized how much they had grown through their adventures. They learned that working together made every challenge easier and every victory sweeter. The bonds of friendship grew stronger with each passing day. They reflected on their journey, from the first step into the forest to the magical discoveries. Each memory was a treasure. They promised to protect the Enchanted Forest and its wonders. With hearts full of joy, they headed home, knowing more adventures awaited. The magic lived on in their spirits, inspiring future tales of bravery and kindness.",
                "In the heart of the magical kingdom, a grand celebration began. All the forest creatures gathered to share stories, songs, and delicious treats. Laughter filled the air as everyone danced under the twinkling stars, creating memories that would last forever in this enchanted land. The party featured games, music, and fireworks of colorful lights. Alex and Cotton were honored as heroes. Gifts were exchanged, and promises made for future gatherings. The night ended with a group hug, sealing their eternal friendship. The kingdom flourished with joy and harmony."
            ]
            story_pages.append(additional_pages[len(story_pages) % len(additional_pages)])

        # Generate images (only for title page)
        images_base64 = []
        images_bytes = []
        
        # Title page image
        try:
            title_img_bytes = create_story_image(f"{title} - Group of characters", 500, 400)
            if isinstance(title_img_bytes, tuple):
                return title_img_bytes
            validated_title_img = validate_image_for_pdf(title_img_bytes)
            if validated_title_img:
                images_bytes.append(validated_title_img)
                images_base64.append(base64.b64encode(validated_title_img).decode('utf-8'))
            else:
                logger.error("Title image validation failed")
                images_base64.append("")
                images_bytes.append(None)
        except Exception as e:
            logger.error(f"Title image creation failed: {e}", exc_info=True)
            images_base64.append("")
            images_bytes.append(None)

        # Store images in session for debugging
        session['last_images_base64'] = images_base64

        # # Generate PDF with background
        # uid = str(uuid.uuid4())
        # pdf_path = f"static/{uid}.pdf"
        
        # try:
        #     doc = SimpleDocTemplate(
        #         pdf_path,
        #         # pagesize=pagesize,
        #         topMargin=0.5*inch,
        #         bottomMargin=0.5*inch,
        #         leftMargin=0.5*inch,
        #         rightMargin=0.5*inch
        #     )
            
        #     styles = getSampleStyleSheet()
        #     story_style = ParagraphStyle(
        #         'StoryStyle',
        #         parent=styles['Normal'],
        #         fontName=get_font_for_language(language),
        #         fontSize=14,
        #         leading=18,
        #         spaceAfter=12,
        #         textColor='#333333',
        #         alignment=4,  # Justified
        #         wordWrap='CJK'
        #     )
            
        #     title_style = ParagraphStyle(
        #         'TitleStyle',
        #         parent=styles['Heading1'],
        #         fontName=get_font_for_language(language),
        #         fontSize=28,
        #         spaceAfter=20,
        #         alignment=1,  # Center
        #         textColor='#d81b60'  # Bright pink for children
        #     )
            
        #     def add_background(canvas, doc):
        #         bg_path = os.path.join('static', 'bg.png')
        #         if os.path.exists(bg_path):
        #             bg_img = PILImage.open(bg_path)
        #             bg_img = bg_img.resize((int(doc.pagesize[0]), int(doc.pagesize[1])), PILImage.Resampling.LANCZOS)
        #             buf = BytesIO()
        #             bg_img.save(buf, format='PNG')
        #             canvas.drawImage(ImageReader(BytesIO(buf.getvalue())), 0, 0, width=doc.pagesize[0], height=doc.pagesize[1])

        #     elements = []
            
        #     # Title page
        #     elements.append(Paragraph(process_text_for_pdf(title, language), title_style))
        #     elements.append(Spacer(1, 0.3*inch))
            
        #     if images_base64 and images_base64[0]:
        #         try:
        #             title_img_data = base64.b64decode(images_base64[0])
        #             if not title_img_data:
        #                 logger.error("Empty title image data after base64 decoding")
        #                 raise ValueError("Empty title image data")
        #             title_image = Image(BytesIO(title_img_data), width=5*inch, height=3.75*inch)
        #             elements.append(title_image)
        #         except Exception as e:
        #             logger.error(f"Error adding title image to PDF: {e}", exc_info=True)
        #             elements.append(Paragraph("Title Image Unavailable", styles['Normal']))
            
        #     elements.append(Spacer(1, 0.3*inch))
        #     elements.append(Paragraph(process_text_for_pdf("A Magical Adventure Story", language), styles['Heading2']))
        #     elements.append(Spacer(1, 0.5*inch))
            
        #     # Story pages
        #     for i, page_text in enumerate(story_pages):
        #         if i > 0:
        #             elements.append(Spacer(1, 0.5*inch))
                
        #         page_text = process_text_for_pdf(page_text, language)
        #         elements.append(Paragraph(page_text, story_style))
            
        #     doc.build(elements, onFirstPage=add_background, onLaterPages=add_background)
            
        # except Exception as e:
        #     logger.error(f"PDF generation failed: {str(e)}", exc_info=True)
        #     return jsonify({'error': f'PDF generation failed: {str(e)}'}), 500

        # Generate video (if premium)
        video_url = None
        if images_bytes and all(img is not None for img in images_bytes):
            video_url = generate_video(title, story_pages, images_bytes, language)

        return jsonify({
            'title': title,
            'pages': story_pages,
            'images': images_base64,
            # 'pdf_url': f"/static/{uid}.pdf",
            'video_url': video_url
        })

    except Exception as e:
        logger.error(f"General error: {str(e)}", exc_info=True)
        return jsonify({'error': f'Story generation failed: {str(e)}'}), 500
@app.route('/preview')
def preview_page():
    # Always redirect to home if accessed directly or refreshed
    return redirect('/')

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@app.route('/debug_images/<uid>')
def debug_images(uid):
    return jsonify({'images': session.get('last_images_base64', [])})

@app.before_request
def log_session():
    logger.debug(f"Session data: {session}")

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
