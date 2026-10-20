import socket
old_getaddrinfo = socket.getaddrinfo
def new_getaddrinfo(*args, **kwargs):
    responses = old_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET]
socket.getaddrinfo = new_getaddrinfo

import os
import time
import re
import sys
import base64
import logging
import datetime
from flask import Flask, render_template, redirect, url_for, session, request, jsonify
import dateutil.parser
import spacy

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

# Environment setup
if os.environ.get('FLASK_ENV') == 'development' or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-mailwatch-12345')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load spaCy NLP model
try:
    nlp = spacy.load("en_core_web_sm")
except Exception as e:
    logger.warning("en_core_web_sm model load failed, using blank English model: %s", e)
    nlp = spacy.blank("en")

# Google OAuth2 Settings
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
CREDENTIALS_FILE = 'credentials.json'
REDIRECT_URI = 'http://localhost:5000/oauth2callback'


def validate_credentials_file():
    if not os.path.exists(CREDENTIALS_FILE):
        error_msg = (
            "\n" + "=" * 80 + "\n"
            "CRITICAL ERROR: 'credentials.json' file is missing!\n"
            "Please create a Google Cloud OAuth 2.0 Web Client in Google Cloud Console,\n"
            "download the client secret JSON file, and save it as 'credentials.json' in:\n"
            f"  {os.path.abspath(CREDENTIALS_FILE)}\n"
            "=" * 80 + "\n"
        )
        sys.stderr.write(error_msg)
        raise RuntimeError("Missing credentials.json")

    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            content = f.read()
            if 'YOUR_CLIENT_ID' in content or 'YOUR_CLIENT_SECRET' in content:
                error_msg = (
                    "\n" + "=" * 80 + "\n"
                    "CRITICAL ERROR: 'credentials.json' contains placeholder values!\n"
                    "Please replace 'YOUR_CLIENT_ID' and 'YOUR_CLIENT_SECRET' with your actual\n"
                    "OAuth 2.0 Web Application credentials from Google Cloud Console.\n"
                    "=" * 80 + "\n"
                )
                sys.stderr.write(error_msg)
                raise RuntimeError("Unconfigured credentials.json placeholder")
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise e
        logger.error("Error reading credentials file: %s", e)


# Run validation on startup
validate_credentials_file()

# Trigger Phrases
TRIGGER_PHRASES = [
    "please respond by",
    "reply by",
    "get back to me by",
    "confirm by",
    "response needed by",
    "respond by",
    "rsvp by",
    "let me know by",
    "deadline is",
    "due by",
    "required by",
    "submit by",
    "complete by",
    "return by"
]

# Regex pattern for date fallback matching
MONTH_NAMES = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'

DATE_REGEX_PATTERNS = [
    r'\b\d{1,2}/\d{1,2}/\d{4}\b',
    r'\b\d{1,2}-\d{1,2}-\d{4}\b',
    r'\b\d{1,2}\s+' + MONTH_NAMES + r'\s+\d{4}\b',
    r'\b\d{1,2}\s+' + MONTH_NAMES + r'\b',
    r'\b' + MONTH_NAMES + r'\s+\d{1,2},?\s+\d{4}\b',
    r'\b' + MONTH_NAMES + r'\s+\d{1,2}\b',
    r'\b(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?\s+of\s+' + MONTH_NAMES + r'\b'
]

COMBINED_DATE_REGEX = re.compile('|'.join(DATE_REGEX_PATTERNS), re.IGNORECASE)


def decode_base64url(data_str):
    if not data_str:
        return ""
    missing_padding = len(data_str) % 4
    if missing_padding:
        data_str += '=' * (4 - missing_padding)
    try:
        decoded_bytes = base64.urlsafe_b64decode(data_str)
        return decoded_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error("Error decoding base64url data: %s", e)
        return ""


def extract_plain_text_body(payload):
    if not payload:
        return ""

    mime_type = payload.get('mimeType', '')
    if mime_type == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        body = decode_base64url(data)
        if body:
            return body

    parts = payload.get('parts', [])
    for part in parts:
        if part.get('mimeType') == 'text/plain':
            data = part.get('body', {}).get('data', '')
            body = decode_base64url(data)
            if body:
                return body
        if 'parts' in part:
            body = extract_plain_text_body(part)
            if body:
                return body

    # Fallback to any available body data
    data = payload.get('body', {}).get('data', '')
    return decode_base64url(data)


def get_sentence_context(text, match_idx):
    if not text:
        return ""
    left = match_idx
    while left > 0 and text[left - 1] not in '.!?\n\r':
        left -= 1
    right = match_idx
    while right < len(text) and text[right] not in '.!?\n\r':
        right += 1
    if right < len(text) and text[right] in '.!?':
        right += 1
    return text[left:right].strip()


def process_email_deadline(body):
    if not body:
        return None, None, None

    body_lower = body.lower()

    for phrase in TRIGGER_PHRASES:
        match_idx = body_lower.find(phrase)
        if match_idx != -1:
            context_sentence = get_sentence_context(body, match_idx)
            start_after = match_idx + len(phrase)
            snippet_80 = body[start_after:start_after + 80]

            date_candidate = None

            # Step A: spaCy NER looking for DATE entities
            doc = nlp(snippet_80)
            for ent in doc.ents:
                if ent.label_ == "DATE":
                    date_candidate = ent.text.strip()
                    break

            # Step B: Regex fallback
            if not date_candidate:
                match = COMBINED_DATE_REGEX.search(snippet_80)
                if match:
                    date_candidate = match.group(0).strip()

            # Parse with dateutil
            if date_candidate:
                try:
                    parsed_dt = dateutil.parser.parse(date_candidate, dayfirst=True, fuzzy=True)
                    deadline_str = parsed_dt.strftime("%d/%m/%Y")
                    return parsed_dt, deadline_str, context_sentence
                except Exception as e:
                    logger.debug("Failed parsing date candidate '%s': %s", date_candidate, e)

    return None, None, None


def calculate_urgency(deadline_dt):
    if not deadline_dt:
        return None

    today_midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    target_dt = deadline_dt
    if target_dt.tzinfo is not None:
        target_dt = target_dt.astimezone().replace(tzinfo=None)
    target_dt = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    days_diff = (target_dt - today_midnight).days

    if days_diff < 0:
        return "overdue"
    elif 0 <= days_diff <= 2:
        return "urgent"
    elif 3 <= days_diff <= 7:
        return "soon"
    else:
        return "upcoming"


def parse_email_date(date_header):
    if not date_header:
        return None, ""
    try:
        parsed = dateutil.parser.parse(date_header)
        if parsed.tzinfo is not None:
            utc_dt = parsed.astimezone(datetime.timezone.utc)
        else:
            utc_dt = parsed
        iso_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return parsed, iso_str
    except Exception as e:
        logger.error("Error parsing Date header '%s': %s", date_header, e)
        return None, date_header


# Routes

@app.route('/')
def index():
    if 'credentials' in session:
        return render_template('index.html')
    return redirect(url_for('login'))


@app.before_request
def ensure_localhost():
    """Ensure all requests are routed via localhost to preserve OAuth session cookies.

    Returns:
        Optional[werkzeug.wrappers.Response]: Redirect response if accessing via IP, otherwise None.
    """
    if request.host.startswith('127.0.0.1'):
        new_url = request.url.replace('127.0.0.1:5000', 'localhost:5000', 1)
        return redirect(new_url, code=302)
    return None


@app.route('/login')
def login():
    """Render login page or redirect to index if already authenticated.

    Returns:
        Union[str, werkzeug.wrappers.Response]: Rendered template or redirect response.
    """
    if 'credentials' in session:
        return redirect(url_for('index'))
    error = request.args.get('error')
    return render_template('login.html', error=error)


@app.route('/favicon.ico')
def favicon():
    """Handle browser favicon request.

    Returns:
        Response: 204 No Content response.
    """
    from flask import Response
    return Response(status=204)


@app.route('/authorize')
def authorize():
    """Initiate Google OAuth 2.0 flow with PKCE challenge.

    Returns:
        werkzeug.wrappers.Response: Redirect to Google OAuth consent page.
    """
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )
    session['state'] = state
    session['code_verifier'] = flow.code_verifier
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    """Handle OAuth 2.0 callback, exchange code for tokens, and store in session.

    Returns:
        werkzeug.wrappers.Response: Redirect to index on success or login on failure.
    """
    error = request.args.get('error')
    if error:
        logger.warning(f"OAuth authorization denied or error returned from provider: {error}")
        session.clear()
        return redirect(url_for('login', error='access_denied'))

    state = session.get('state')
    code_verifier = session.get('code_verifier')

    if not state or not code_verifier:
        logger.warning("OAuth callback missing state or code_verifier in session. Redirecting to login.")
        session.clear()
        return redirect(url_for('login', error='session_expired'))

    try:
        flow = Flow.from_client_secrets_file(
            CREDENTIALS_FILE,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
            state=state,
            code_verifier=code_verifier
        )
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        return redirect(url_for('index'))
    except OAuth2Error as e:
        logger.error(f"OAuth2 protocol error during token exchange: {e}")
        session.clear()
        return redirect(url_for('login', error='invalid_grant'))
    except Exception as e:
        logger.error(f"Unexpected error during OAuth token exchange: {e}")
        session.clear()
        return redirect(url_for('login', error='auth_error'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


CLASSIFICATION_RULES = {
    "work": {
        "domains": ["linkedin.com", "github.com", "gitlab.com", "notion.so", "slack.com",
                    "zoom.us", "meet.google.com", "internshala.com", "naukri.com",
                    "lever.co", "greenhouse.io", "workday.com", "edu"],
        "keywords": ["internship", "interview", "project", "deadline", "meeting", "office",
                     "colleague", "manager", "team", "assignment", "submission", "college",
                     "university", "semester", "faculty", "professor", "department",
                     "offer letter", "joining", "onboarding", "sprint", "standup"],
        "headers": []
    },
    "finance": {
        "domains": ["hdfcbank.com", "sbi.co.in", "icicibank.com", "axisbank.com",
                    "paytm.com", "razorpay.com", "stripe.com", "paypal.com",
                    "phonepe.com", "gpay.com", "amazon.com", "flipkart.com",
                    "bankofamerica.com", "chase.com", "wellsfargo.com"],
        "keywords": ["invoice", "payment", "transaction", "bank statement", "debit",
                     "credit", "balance", "receipt", "order confirmed", "refund",
                     "subscription", "billing", "due amount", "emi", "upi"],
        "headers": []
    },
    "promotions": {
        "domains": ["mailchimp.com", "sendgrid.net", "constantcontact.com",
                    "klaviyo.com", "marketing.", "newsletter.", "noreply.",
                    "no-reply.", "deals.", "offers.", "promo."],
        "keywords": ["unsubscribe", "offer", "sale", "discount", "deal", "promo",
                     "limited time", "exclusive", "% off", "shop now", "buy now",
                     "free shipping", "newsletter", "weekly digest", "monthly update",
                     "click here", "don't miss", "hurry", "expires"],
        "headers": ["list-unsubscribe", "x-mailer", "x-mailchimp", "x-campaign",
                    "x-bulk-mail", "precedence: bulk", "x-marketing"]
    },
    "spam": {
        "domains": [],
        "keywords": ["you have won", "congratulations you", "claim your prize",
                     "click to verify", "urgent action required", "account suspended",
                     "verify your account immediately", "wire transfer", "nigerian",
                     "lottery winner", "inheritance", "crypto opportunity",
                     "make money fast", "work from home earn", "free gift"],
        "headers": ["x-spam", "x-phishing"]
    },
    "personal": {
        "domains": ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                    "icloud.com", "protonmail.com", "me.com"],
        "keywords": ["hey", "hi", "hello", "catch up", "how are you",
                     "family", "birthday", "wedding", "invitation", "reunion"],
        "headers": []
    }
}

CATEGORY_PRIORITY = ["spam", "finance", "work", "promotions", "personal"]


def classify_email(sender, subject, headers_dict):
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    headers_str = ' '.join(f"{k}:{v}" for k, v in headers_dict.items()).lower()

    scores = {cat: 0 for cat in CLASSIFICATION_RULES}

    for cat, rules in CLASSIFICATION_RULES.items():
        for domain in rules["domains"]:
            if domain in sender_lower:
                scores[cat] += 3
                break
        for kw in rules["keywords"]:
            if kw in subject_lower:
                scores[cat] += 2
        for flag in rules["headers"]:
            if flag in headers_str:
                scores[cat] += 2

    best_cat = max(
        CATEGORY_PRIORITY,
        key=lambda c: (scores[c], -CATEGORY_PRIORITY.index(c))
    )

    if scores[best_cat] == 0:
        best_cat = "personal"

    return best_cat


@app.route('/api/emails')
def get_emails():
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401

    page_token = request.args.get('pageToken', None)

    creds_info = session['credentials']

    creds = Credentials(
        token=creds_info.get('token'),
        refresh_token=creds_info.get('refresh_token'),
        token_uri=creds_info.get('token_uri'),
        client_id=creds_info.get('client_id'),
        client_secret=creds_info.get('client_secret'),
        scopes=creds_info.get('scopes')
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            session['credentials'] = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
        except Exception as e:
            logger.error("Failed to refresh access token: %s", e)
            return jsonify({"error": f"Failed to refresh token: {str(e)}"}), 401
    elif creds.expired:
        logger.error("Access token expired and no refresh token available.")
        session.clear()
        return jsonify({"error": "Token expired, please login again."}), 401

    try:
        service = build('gmail', 'v1', credentials=creds)
        list_kwargs = {'userId': 'me', 'q': 'label:INBOX', 'maxResults': 30}
        if page_token:
            list_kwargs['pageToken'] = page_token
        res = service.users().messages().list(**list_kwargs).execute()
        messages_list = res.get('messages', [])
        next_page_token = res.get('nextPageToken', None)
    except Exception as e:
        logger.error("Gmail API list messages failed: %s", e)
        return jsonify({"error": f"Gmail API error: {str(e)}"}), 500

    processed_emails = []

    messages_data = {}
    def batch_callback(request_id: str, response: dict, exception: Exception) -> None:
        """Callback for each message in a batch request."""
        if exception is None:
            messages_data[request_id] = response
        else:
            logger.error("Batch error on message %s: %s", request_id, exception)

    # Split into chunks of 10 to balance speed and avoid 429 concurrent limit.
    CHUNK_SIZE = 10
    if messages_list:
        for chunk_start in range(0, len(messages_list), CHUNK_SIZE):
            chunk = messages_list[chunk_start:chunk_start + CHUNK_SIZE]
            batch = service.new_batch_http_request(callback=batch_callback)
            for msg_item in chunk:
                msg_id = msg_item['id']
                batch.add(
                    service.users().messages().get(userId='me', id=msg_id, format='full'),
                    request_id=msg_id
                )
            batch.execute()
            if chunk_start + CHUNK_SIZE < len(messages_list):
                time.sleep(0.2)

    for msg_id, msg in messages_data.items():
        try:
            headers = msg.get('payload', {}).get('headers', [])
            header_dict = {h['name'].lower(): h['value'] for h in headers if 'name' in h and 'value' in h}

            sender = header_dict.get('from', '')
            subject = header_dict.get('subject', '')
            date_raw = header_dict.get('date', '')

            email_dt, date_iso = parse_email_date(date_raw)
            label_ids = msg.get('labelIds', [])
            unread = "UNREAD" in label_ids
            snippet = msg.get('snippet', '')

            payload = msg.get('payload', {})
            body = extract_plain_text_body(payload)

            deadline_dt, deadline_str, context = process_email_deadline(body)
            urgency = calculate_urgency(deadline_dt)
            category = classify_email(sender, subject, header_dict)

            email_obj = {
                "id": msg_id,
                "sender": sender,
                "subject": subject,
                "date": date_iso,
                "unread": unread,
                "snippet": snippet,
                "body": body if body else snippet,
                "deadline": deadline_str,
                "urgency": urgency,
                "context": context,
                "category": category,
                "_deadline_dt": deadline_dt,
                "_email_dt": email_dt
            }
            processed_emails.append(email_obj)
        except Exception as e:
            logger.error("Error processing email message %s: %s", msg_id, e)
            continue

    def sort_key(item):
        dl_dt = item['_deadline_dt']
        em_dt = item['_email_dt']

        if dl_dt is not None:
            return (0, dl_dt.timestamp() if dl_dt else 0)
        else:
            em_ts = em_dt.timestamp() if em_dt else 0
            return (1, -em_ts)

    processed_emails.sort(key=sort_key)

    output_list = []
    for item in processed_emails:
        clean_item = {
            "id": item["id"],
            "sender": item["sender"],
            "subject": item["subject"],
            "date": item["date"],
            "unread": item["unread"],
            "snippet": item["snippet"],
            "body": item.get("body", item["snippet"]),
            "deadline": item["deadline"],
            "urgency": item["urgency"],
            "context": item["context"],
            "category": item["category"]
        }
        output_list.append(clean_item)

    return jsonify({
        "emails": output_list,
        "nextPageToken": next_page_token,
        "count": len(output_list)
    })


if __name__ == '__main__':
    import webbrowser
    import threading

    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Timer(1.2, lambda: webbrowser.open('http://localhost:5000')).start()

    app.run(debug=True, port=5000)