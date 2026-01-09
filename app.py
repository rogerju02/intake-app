import streamlit as st
import streamlit.components.v1 as components
import base64
from PIL import Image 
import numpy as np
from ultralytics import YOLO
import io
import hashlib
import os
import requests
import sqlite3
import json
import uuid
import re
import pickle
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from dotenv import load_dotenv

# Gmail API imports
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

# Anthropic import for email parsing
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# Load environment variables
load_dotenv()

try:
    SHOPIFY_STORE_URL = st.secrets.get("SHOPIFY_STORE_URL", os.getenv("SHOPIFY_STORE_URL"))
    SHOPIFY_ACCESS_TOKEN = st.secrets.get("SHOPIFY_ACCESS_TOKEN", os.getenv("SHOPIFY_ACCESS_TOKEN"))
except:
    SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
    SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

# Google OAuth credentials
try:
    GOOGLE_CLIENT_ID = st.secrets.get("GOOGLE_CLIENT_ID", os.getenv("GOOGLE_CLIENT_ID"))
    GOOGLE_CLIENT_SECRET = st.secrets.get("GOOGLE_CLIENT_SECRET", os.getenv("GOOGLE_CLIENT_SECRET"))
except:
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Anthropic API key for email parsing
try:
    ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY"))
except:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Google OAuth scopes and config
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
GMAIL_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "gmail_token.pickle")

# Database path for form persistence
DB_PATH = os.path.join(os.path.dirname(__file__), "form_drafts.db")
DRAFT_EXPIRY_HOURS = 24  # Auto-delete drafts older than this

# ============================================
# AVAILABLE FORM FIELDS CONFIGURATION
# ============================================

# Define all possible item fields with their properties
AVAILABLE_FIELDS = {
    'name': {
        'label': 'Item Name',
        'type': 'text',
        'required': True,  # Always shown, can't be disabled
        'default_enabled': True,
        'pdf_header': 'Title',
        'pdf_width': 2.2,
    },
    'notes': {
        'label': 'Notes',
        'type': 'textarea',
        'required': False,
        'default_enabled': True,
        'pdf_header': 'Notes',
        'pdf_width': 1.5,
    },
    'quantity': {
        'label': 'Quantity',
        'type': 'number',
        'required': False,
        'default_enabled': True,
        'pdf_header': 'QTY',
        'pdf_width': 0.5,
    },
    'price': {
        'label': 'Price ($)',
        'type': 'currency',
        'required': False,
        'default_enabled': False,
        'pdf_header': 'Price',
        'pdf_width': 0.7,
    },
    'status': {
        'label': 'Accept/Reject',
        'type': 'status',
        'required': False,
        'default_enabled': False,
        'pdf_header': 'Status',
        'pdf_width': 0.5,
    },
    'condition': {
        'label': 'Condition',
        'type': 'select',
        'options': ['Excellent', 'Good', 'Fair', 'Poor'],
        'required': False,
        'default_enabled': False,
        'pdf_header': 'Cond.',
        'pdf_width': 0.6,
    },
    'category': {
        'label': 'Category',
        'type': 'select',
        'options': ['Furniture', 'Decor', 'Lighting', 'Art', 'Textiles', 'Other'],
        'required': False,
        'default_enabled': False,
        'pdf_header': 'Category',
        'pdf_width': 0.8,
    },
    'dimensions': {
        'label': 'Dimensions',
        'type': 'text',
        'required': False,
        'default_enabled': False,
        'pdf_header': 'Dims',
        'pdf_width': 1.0,
    },
}

# ============================================
# DATABASE FUNCTIONS FOR FORM PERSISTENCE
# ============================================

def init_database():
    """Initialize SQLite database for form drafts"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS form_drafts (
            id TEXT PRIMARY KEY,
            name TEXT,
            app_mode TEXT,
            form_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def cleanup_old_drafts():
    """Remove drafts older than DRAFT_EXPIRY_HOURS"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    expiry_time = datetime.now() - timedelta(hours=DRAFT_EXPIRY_HOURS)
    cursor.execute('DELETE FROM form_drafts WHERE updated_at < ?', (expiry_time,))
    conn.commit()
    conn.close()

def save_draft(draft_id, name, app_mode, form_data):
    """Save or update a form draft"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Serialize form_data (including binary images as base64)
    serializable_data = form_data.copy()
    
    # Convert item_images to base64 strings
    if 'item_images' in serializable_data:
        images_b64 = {}
        for k, v in serializable_data['item_images'].items():
            if isinstance(v, bytes):
                images_b64[str(k)] = base64.b64encode(v).decode('utf-8')
        serializable_data['item_images'] = images_b64
    
    # Convert main image to base64
    if 'image_data' in serializable_data and serializable_data['image_data']:
        if isinstance(serializable_data['image_data'], bytes):
            serializable_data['image_data'] = base64.b64encode(serializable_data['image_data']).decode('utf-8')
    
    json_data = json.dumps(serializable_data)
    
    cursor.execute('''
        INSERT INTO form_drafts (id, name, app_mode, form_data, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            app_mode = excluded.app_mode,
            form_data = excluded.form_data,
            updated_at = CURRENT_TIMESTAMP
    ''', (draft_id, name, app_mode, json_data))
    conn.commit()
    conn.close()

def load_draft(draft_id):
    """Load a form draft by ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT form_data, app_mode FROM form_drafts WHERE id = ?', (draft_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        form_data = json.loads(result[0])
        
        # Convert base64 images back to bytes
        if 'item_images' in form_data:
            images_bytes = {}
            for k, v in form_data['item_images'].items():
                if isinstance(v, str):
                    images_bytes[int(k)] = base64.b64decode(v)
            form_data['item_images'] = images_bytes
        
        # Convert main image back to bytes
        if 'image_data' in form_data and form_data['image_data']:
            if isinstance(form_data['image_data'], str):
                form_data['image_data'] = base64.b64decode(form_data['image_data'])
        
        return form_data, result[1]
    return None, None

def get_all_drafts():
    """Get list of all saved drafts"""
    cleanup_old_drafts()  # Clean up on every list fetch
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, app_mode, created_at, updated_at FROM form_drafts ORDER BY updated_at DESC')
    drafts = cursor.fetchall()
    conn.close()
    return drafts

def delete_draft(draft_id):
    """Delete a draft by ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM form_drafts WHERE id = ?', (draft_id,))
    conn.commit()
    conn.close()

# Initialize database on startup
init_database()

# ============================================
# GMAIL API FUNCTIONS
# ============================================

def get_gmail_auth_url():
    """Generate OAuth URL for Gmail authorization"""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return None, "Google OAuth credentials not configured"
    
    # For Streamlit Cloud, use the app URL; for local dev, use localhost
    redirect_uri = os.getenv("GMAIL_REDIRECT_URI", "http://localhost:8501")
    
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri]
            }
        },
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri
    )
    
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    
    return auth_url, state

def exchange_code_for_token(auth_code):
    """Exchange authorization code for access token"""
    redirect_uri = os.getenv("GMAIL_REDIRECT_URI", "http://localhost:8501")
    
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri]
            }
        },
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri
    )
    
    try:
        flow.fetch_token(code=auth_code)
        credentials = flow.credentials
        
        # Save credentials for future use
        with open(GMAIL_TOKEN_PATH, 'wb') as token_file:
            pickle.dump(credentials, token_file)
        
        return credentials, None
    except Exception as e:
        return None, str(e)

def load_gmail_credentials():
    """Load saved Gmail credentials"""
    if os.path.exists(GMAIL_TOKEN_PATH):
        try:
            with open(GMAIL_TOKEN_PATH, 'rb') as token_file:
                credentials = pickle.load(token_file)
                
            # Check if credentials are expired
            if credentials and credentials.expired and credentials.refresh_token:
                from google.auth.transport.requests import Request
                credentials.refresh(Request())
                # Save refreshed credentials
                with open(GMAIL_TOKEN_PATH, 'wb') as token_file:
                    pickle.dump(credentials, token_file)
            
            return credentials
        except Exception:
            return None
    return None

def get_gmail_service():
    """Get authenticated Gmail service"""
    credentials = load_gmail_credentials()
    if credentials:
        try:
            return build('gmail', 'v1', credentials=credentials)
        except Exception:
            return None
    return None

def get_recent_gmail_threads(max_results=15):
    """Get recent email threads from inbox"""
    service = get_gmail_service()
    if not service:
        return None, "Gmail not authenticated"
    
    try:
        # Get recent threads from inbox
        results = service.users().threads().list(
            userId='me',
            maxResults=max_results,
            labelIds=['INBOX']
        ).execute()
        
        threads = results.get('threads', [])
        
        if not threads:
            return [], None
        
        # Get thread details
        thread_list = []
        for thread in threads:
            thread_data = service.users().threads().get(
                userId='me',
                id=thread['id'],
                format='metadata',
                metadataHeaders=['Subject', 'From', 'Date']
            ).execute()
            
            messages = thread_data.get('messages', [])
            if messages:
                # Get headers from first message
                headers = messages[0].get('payload', {}).get('headers', [])
                header_dict = {h['name']: h['value'] for h in headers}
                
                thread_list.append({
                    'id': thread['id'],
                    'subject': header_dict.get('Subject', 'No Subject'),
                    'from': header_dict.get('From', 'Unknown'),
                    'date': header_dict.get('Date', ''),
                    'message_count': len(messages),
                    'snippet': thread_data.get('snippet', '')[:100]
                })
        
        return thread_list, None
        
    except HttpError as e:
        return None, f"Gmail API Error: {str(e)}"
    except Exception as e:
        return None, f"Error: {str(e)}"

def search_gmail_threads(query, max_results=20):
    """Search Gmail for threads matching query"""
    service = get_gmail_service()
    if not service:
        return None, "Gmail not authenticated"
    
    try:
        # Search for threads
        results = service.users().threads().list(
            userId='me',
            q=query,
            maxResults=max_results
        ).execute()
        
        threads = results.get('threads', [])
        
        if not threads:
            return [], None
        
        # Get thread details
        thread_list = []
        for thread in threads:
            thread_data = service.users().threads().get(
                userId='me',
                id=thread['id'],
                format='metadata',
                metadataHeaders=['Subject', 'From', 'Date']
            ).execute()
            
            messages = thread_data.get('messages', [])
            if messages:
                # Get headers from first message
                headers = messages[0].get('payload', {}).get('headers', [])
                header_dict = {h['name']: h['value'] for h in headers}
                
                thread_list.append({
                    'id': thread['id'],
                    'subject': header_dict.get('Subject', 'No Subject'),
                    'from': header_dict.get('From', 'Unknown'),
                    'date': header_dict.get('Date', ''),
                    'message_count': len(messages),
                    'snippet': thread_data.get('snippet', '')[:100]
                })
        
        return thread_list, None
        
    except HttpError as e:
        return None, f"Gmail API Error: {str(e)}"
    except Exception as e:
        return None, f"Error: {str(e)}"

def get_thread_messages(thread_id):
    """Get all messages in a thread"""
    service = get_gmail_service()
    if not service:
        return None, "Gmail not authenticated"
    
    try:
        thread = service.users().threads().get(
            userId='me',
            id=thread_id,
            format='full'
        ).execute()
        
        messages = []
        for msg in thread.get('messages', []):
            # Get headers
            headers = msg.get('payload', {}).get('headers', [])
            header_dict = {h['name']: h['value'] for h in headers}
            
            # Get body
            body = extract_email_body(msg.get('payload', {}))
            
            messages.append({
                'id': msg['id'],
                'from': header_dict.get('From', 'Unknown'),
                'to': header_dict.get('To', ''),
                'date': header_dict.get('Date', ''),
                'subject': header_dict.get('Subject', 'No Subject'),
                'body': body
            })
        
        return messages, None
        
    except HttpError as e:
        return None, f"Gmail API Error: {str(e)}"
    except Exception as e:
        return None, f"Error: {str(e)}"

def extract_email_body(payload):
    """Extract plain text body from email payload"""
    body = ""
    
    if 'body' in payload and payload['body'].get('data'):
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    
    elif 'parts' in payload:
        for part in payload['parts']:
            mime_type = part.get('mimeType', '')
            
            if mime_type == 'text/plain':
                if part.get('body', {}).get('data'):
                    body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                    break
            elif mime_type == 'text/html' and not body:
                if part.get('body', {}).get('data'):
                    html = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                    # Strip HTML tags for plain text
                    body = re.sub(r'<[^>]+>', ' ', html)
                    body = re.sub(r'\s+', ' ', body).strip()
            elif mime_type.startswith('multipart'):
                # Recursive for nested parts
                body = extract_email_body(part)
                if body:
                    break
    
    return body

def clear_gmail_auth():
    """Clear saved Gmail credentials"""
    if os.path.exists(GMAIL_TOKEN_PATH):
        os.remove(GMAIL_TOKEN_PATH)

# ============================================
# EMAIL PARSING WITH CLAUDE
# ============================================

def parse_email_thread_with_claude(messages):
    """Use Claude to parse email thread and extract item information"""
    if not ANTHROPIC_AVAILABLE or not ANTHROPIC_API_KEY:
        return None, "Anthropic API not configured"
    
    # Combine all messages into a single thread text
    thread_text = ""
    for msg in messages:
        thread_text += f"\n--- Email from {msg['from']} on {msg['date']} ---\n"
        thread_text += f"Subject: {msg['subject']}\n"
        thread_text += f"{msg['body']}\n"
    
    # Create the parsing prompt
    prompt = f"""Analyze this email thread between a consigner and a consignment store about furniture items for potential consignment.

Extract the following information and return it as JSON:

1. "customer_name": The name of the person consigning items (the customer, not the store)
2. "customer_email": Their email address if visible
3. "customer_phone": Their phone number if mentioned
4. "customer_address": Their address if mentioned (especially for pickup)
5. "items": An array of items discussed, where each item has:
   - "name": Item description/name
   - "status": "approved", "rejected", "pending", or "unknown" based on the conversation
   - "notes": Any relevant notes about condition, dimensions, etc.
   - "quantity": Number of items (default 1)
6. "pickup_required": true/false if pickup was mentioned
7. "pickup_address": Address for pickup if different from customer_address
8. "pickup_date": Scheduled pickup date if mentioned
9. "summary": Brief summary of the conversation outcome

EMAIL THREAD:
{thread_text}

Return ONLY valid JSON, no other text. If information is not found, use null for strings and empty array for items."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        # Extract JSON from response
        response_text = response.content[0].text.strip()
        
        # Try to parse JSON (handle potential markdown code blocks)
        if response_text.startswith("```"):
            response_text = re.sub(r'^```json?\n?', '', response_text)
            response_text = re.sub(r'\n?```$', '', response_text)
        
        parsed_data = json.loads(response_text)
        return parsed_data, None
        
    except json.JSONDecodeError as e:
        return None, f"Failed to parse response as JSON: {str(e)}"
    except Exception as e:
        return None, f"Error calling Claude API: {str(e)}"

# ============================================
# CACHE MODEL
# ============================================

@st.cache_resource
def load_model():
    return YOLO("yolov8m.pt")

# ============================================
# SHOPIFY API FUNCTIONS
# ============================================

def search_consigner_by_account(account_number):
    """Search Shopify for all items with this account number and return the highest item number"""
    if not SHOPIFY_STORE_URL or not SHOPIFY_ACCESS_TOKEN:
        return None, "Shopify credentials not configured"
    
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    
    graphql_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/graphql.json"
    
    query = """
    {
      productVariants(first: 250, query: "sku:%s-*") {
        edges {
          node {
            sku
            price
            inventoryQuantity
            product {
              title
            }
          }
        }
      }
    }
    """ % account_number
    
    try:
        response = requests.post(graphql_url, headers=headers, json={"query": query})
        
        if response.status_code == 200:
            data = response.json()
            
            if 'errors' in data:
                return None, f"GraphQL Error: {data['errors']}"
            
            variants = data.get('data', {}).get('productVariants', {}).get('edges', [])
            
            if not variants:
                return None, "No items found for this account number"
            
            items = []
            item_numbers = []
            
            for edge in variants:
                node = edge['node']
                sku = node.get('sku', '')
                try:
                    item_num = int(sku.split('-')[1])
                    item_numbers.append(item_num)
                    items.append({
                        'sku': sku,
                        'item_number': item_num,
                        'price': node.get('price'),
                        'title': node.get('product', {}).get('title', 'N/A'),
                        'qty': node.get('inventoryQuantity')
                    })
                except (IndexError, ValueError):
                    continue
            
            if item_numbers:
                highest = max(item_numbers)
                return {
                    'account_number': account_number,
                    'highest_item_number': highest,
                    'next_item_number': highest + 1,
                    'total_items': len(items),
                    'items': sorted(items, key=lambda x: x['item_number'])
                }, None
            else:
                return None, "Could not parse item numbers from SKUs"
        else:
            return None, f"API Error: {response.status_code}"
    
    except Exception as e:
        return None, f"Error: {str(e)}"

# ============================================
# SESSION STATE INITIALIZATION
# ============================================

def get_default_enabled_fields():
    """Get the default set of enabled fields"""
    return {field_id: config['default_enabled'] for field_id, config in AVAILABLE_FIELDS.items()}

def init_session_state():
    defaults = {
        # App mode: "detection", "general", or "email"
        'app_mode': None,
        'show_form': False,
        'image_data': None,
        'image_hash': None,
        'boxes_data': [],
        'detection_complete': False,
        'num_items': 0,
        'form_values': {},
        'had_active_input': False,
        'is_new_consigner': True,
        'starting_item_number': 1,
        'consigner_type_selection': "New Consigner",
        'consigner_search_result': None,
        'searched_account_number': "",
        'manual_account_number': "",
        'search_failed': False,
        'item_images': {},
        'adding_photo_for_item': None,
        # Draft management
        'current_draft_id': None,
        'show_drafts_panel': False,
        # Email import mode
        'gmail_authenticated': False,
        'email_search_results': None,
        'email_recent_threads': None,
        'email_recent_loaded': False,
        'selected_thread_id': None,
        'selected_thread_messages': None,
        'parsed_email_data': None,
        'email_import_step': 'queue',  # 'queue', 'search', 'select', 'review', 'edit'
        'email_show_search': False,
        # Dynamic field configuration
        'enabled_fields': get_default_enabled_fields(),
        'show_field_config': False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_session_state()

# ============================================
# UTILITY FUNCTIONS
# ============================================

def get_image_hash(image_bytes):
    return hashlib.md5(image_bytes).hexdigest()

def clear_all_data():
    st.session_state.image_data = None
    st.session_state.image_hash = None
    st.session_state.boxes_data = []
    st.session_state.detection_complete = False
    st.session_state.num_items = 0
    st.session_state.form_values = {}
    st.session_state.had_active_input = False
    st.session_state.item_images = {}
    st.session_state.adding_photo_for_item = None

def reset_to_mode_selection():
    """Full reset back to mode selection"""
    clear_all_data()
    st.session_state.app_mode = None
    st.session_state.show_form = False
    st.session_state.is_new_consigner = True
    st.session_state.consigner_type_selection = "New Consigner"
    st.session_state.consigner_search_result = None
    st.session_state.searched_account_number = ""
    st.session_state.manual_account_number = ""
    st.session_state.search_failed = False
    st.session_state.current_draft_id = None
    # Reset email import state
    st.session_state.email_search_results = None
    st.session_state.email_recent_threads = None
    st.session_state.email_recent_loaded = False
    st.session_state.selected_thread_id = None
    st.session_state.selected_thread_messages = None
    st.session_state.parsed_email_data = None
    st.session_state.email_import_step = 'queue'
    st.session_state.email_show_search = False
    # Reset field configuration to defaults
    st.session_state.enabled_fields = get_default_enabled_fields()
    st.session_state.show_field_config = False

def save_form_values():
    for i in range(st.session_state.num_items):
        for field_id in AVAILABLE_FIELDS.keys():
            key = f"{field_id}_{i}"
            if key in st.session_state:
                st.session_state.form_values[key] = st.session_state[key]
    
    st.session_state.form_values['consigner_type_selection'] = st.session_state.consigner_type_selection
    st.session_state.form_values['starting_item_number'] = st.session_state.starting_item_number
    st.session_state.form_values['is_new_consigner'] = st.session_state.is_new_consigner
    st.session_state.form_values['searched_account_number'] = st.session_state.searched_account_number
    st.session_state.form_values['manual_account_number'] = st.session_state.manual_account_number
    st.session_state.form_values['enabled_fields'] = st.session_state.enabled_fields.copy()

def get_form_value(key, default):
    return st.session_state.form_values.get(key, default)

def on_field_change(field_key):
    """Callback to auto-save field value when it changes"""
    if field_key in st.session_state:
        st.session_state.form_values[field_key] = st.session_state[field_key]

def is_field_enabled(field_id):
    """Check if a field is enabled"""
    return st.session_state.enabled_fields.get(field_id, False)

def process_new_image(image_bytes):
    new_hash = get_image_hash(image_bytes)
    if st.session_state.image_hash != new_hash:
        st.session_state.image_data = image_bytes
        st.session_state.image_hash = new_hash
        st.session_state.boxes_data = []
        st.session_state.detection_complete = False
        st.session_state.num_items = 0
        st.session_state.form_values = {}
        st.session_state.item_images = {}
    st.session_state.had_active_input = True

def get_accepted_items_count():
    """Get count of accepted items (or all items if status field is disabled)"""
    count = 0
    for i in range(st.session_state.num_items):
        if is_field_enabled('status'):
            if get_form_value(f"status_{i}", "Accept") == "Accept":
                count += 1
        else:
            # If status field is disabled, all items are included
            count += 1
    return count

def get_total_quantity():
    """Get total quantity of all accepted items"""
    total = 0
    for i in range(st.session_state.num_items):
        include_item = True
        if is_field_enabled('status'):
            include_item = get_form_value(f"status_{i}", "Accept") == "Accept"
        
        if include_item:
            if is_field_enabled('quantity'):
                total += get_form_value(f"quantity_{i}", 1)
            else:
                total += 1  # Default quantity of 1 if field disabled
    return total

def get_draft_display_name():
    """Get the best name to display for drafts - prefer customer name over account number"""
    customer_name = get_form_value('customer_name', '')
    if customer_name and customer_name.strip():
        return customer_name.strip()[:30]
    
    if st.session_state.searched_account_number:
        return f"Account {st.session_state.searched_account_number}"
    
    if st.session_state.manual_account_number:
        return f"Account {st.session_state.manual_account_number}"
    
    return f"Draft {datetime.now().strftime('%m/%d %H:%M')}"

def save_current_form_to_draft():
    """Save current form state to database"""
    if not st.session_state.current_draft_id:
        st.session_state.current_draft_id = str(uuid.uuid4())[:8]
    
    # Collect all form state
    form_data = {
        'form_values': st.session_state.form_values,
        'num_items': st.session_state.num_items,
        'item_images': st.session_state.item_images,
        'image_data': st.session_state.image_data,
        'image_hash': st.session_state.image_hash,
        'boxes_data': st.session_state.boxes_data,
        'detection_complete': st.session_state.detection_complete,
        'is_new_consigner': st.session_state.is_new_consigner,
        'consigner_type_selection': st.session_state.consigner_type_selection,
        'searched_account_number': st.session_state.searched_account_number,
        'manual_account_number': st.session_state.manual_account_number,
        'search_failed': st.session_state.search_failed,
        'enabled_fields': st.session_state.enabled_fields,
    }
    
    name = get_draft_display_name()
    
    save_draft(
        st.session_state.current_draft_id,
        name,
        st.session_state.app_mode,
        form_data
    )
    return st.session_state.current_draft_id

def restore_draft_to_session(draft_id):
    """Restore a draft from database to session state"""
    form_data, app_mode = load_draft(draft_id)
    if form_data:
        st.session_state.app_mode = app_mode
        st.session_state.form_values = form_data.get('form_values', {})
        st.session_state.num_items = form_data.get('num_items', 0)
        st.session_state.item_images = form_data.get('item_images', {})
        st.session_state.image_data = form_data.get('image_data')
        st.session_state.image_hash = form_data.get('image_hash')
        st.session_state.boxes_data = form_data.get('boxes_data', [])
        st.session_state.detection_complete = form_data.get('detection_complete', False)
        st.session_state.is_new_consigner = form_data.get('is_new_consigner', True)
        st.session_state.consigner_type_selection = form_data.get('consigner_type_selection', "New Consigner")
        st.session_state.searched_account_number = form_data.get('searched_account_number', "")
        st.session_state.manual_account_number = form_data.get('manual_account_number', "")
        st.session_state.search_failed = form_data.get('search_failed', False)
        st.session_state.enabled_fields = form_data.get('enabled_fields', get_default_enabled_fields())
        st.session_state.current_draft_id = draft_id
        st.session_state.show_form = False
        return True
    return False

# ============================================
# PDF GENERATION
# ============================================

def generate_pdf(customer_name, customer_address, account_number, phone_number, starting_item_num, is_new_consigner):
    """Generate a PDF receipt with dynamic fields based on enabled_fields"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch
    )
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        spaceAfter=2,
        fontName='Helvetica-Bold'
    )
    
    address_style = ParagraphStyle(
        'Address',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.Color(0.4, 0.4, 0.4),
        spaceAfter=20,
        leading=12
    )
    
    section_header = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=12,
        fontName='Helvetica-Bold',
        spaceAfter=8
    )
    
    story = []
    
    # Company Header
    story.append(Paragraph("Consigned By Design", title_style))
    story.append(Paragraph("7035 East 96th Street<br/>Suite A<br/>Indianapolis, Indiana 46250", address_style))
    story.append(Paragraph("Item List", section_header))
    
    # Customer Info - only show for new consigners
    if is_new_consigner:
        customer_style = ParagraphStyle(
            'Customer',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=4
        )
        
        if customer_name:
            story.append(Paragraph(f"<b>{customer_name}</b>", customer_style))
        if customer_address:
            for line in customer_address.split('\n'):
                if line.strip():
                    story.append(Paragraph(line.strip(), customer_style))
        
        story.append(Spacer(1, 12))
    
    # Date/Account row
    today = datetime.now().strftime('%m/%d/%Y')
    
    if is_new_consigner:
        info_data = [[
            f"Today's Date: {today}",
            f"Phone: {phone_number or 'N/A'}",
            "Page #: 1"
        ]]
        info_table = Table(info_data, colWidths=[2.5*inch, 2.5*inch, 1.5*inch])
    else:
        info_data = [[
            f"Today's Date: {today}",
            f"Account #: {account_number or 'N/A'}",
            f"Phone: {phone_number or 'N/A'}",
            "Page #: 1"
        ]]
        info_table = Table(info_data, colWidths=[1.8*inch, 1.8*inch, 1.8*inch, 1.1*inch])
    
    info_table.hAlign = "CENTER"   
    info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 8))
    
    # Build dynamic column headers based on enabled fields
    headers = ["Date", "Item #"]
    col_widths = [0.7*inch, 0.5*inch]
    
    # Always include name
    headers.append("Title")
    col_widths.append(2.0*inch)
    
    # Add enabled optional fields
    if is_field_enabled('notes'):
        headers.append("Notes")
        col_widths.append(1.3*inch)
    
    if is_field_enabled('status'):
        headers.append("Status")
        col_widths.append(0.5*inch)
    
    if is_field_enabled('price'):
        headers.append("Price")
        col_widths.append(0.65*inch)
    
    if is_field_enabled('quantity'):
        headers.append("QTY")
        col_widths.append(0.4*inch)
    
    if is_field_enabled('condition'):
        headers.append("Cond.")
        col_widths.append(0.6*inch)
    
    if is_field_enabled('category'):
        headers.append("Cat.")
        col_widths.append(0.7*inch)
    
    if is_field_enabled('dimensions'):
        headers.append("Dims")
        col_widths.append(0.9*inch)
    
    table_data = [headers]
    
    # Add items
    total_price = 0.0
    item_count = 0
    total_qty = 0
    current_item_num = 1
    
    for i in range(st.session_state.num_items):
        # Check if item should be included
        include_item = True
        if is_field_enabled('status'):
            include_item = get_form_value(f"status_{i}", "Accept") == "Accept"
        
        if include_item:
            item_count += 1
            name = get_form_value(f"name_{i}", "") or ""
            
            # Start building row
            row = [today, str(current_item_num), name]
            
            # Add enabled optional field values
            if is_field_enabled('notes'):
                notes = get_form_value(f"notes_{i}", "")
                row.append(notes[:30] if notes else "")
            
            if is_field_enabled('status'):
                row.append("A")  # Already filtered to accepted
            
            if is_field_enabled('price'):
                price = get_form_value(f'price_{i}', 0.0)
                row.append(f"${price:.2f}")
                qty = get_form_value(f'quantity_{i}', 1) if is_field_enabled('quantity') else 1
                total_price += price * qty
            
            if is_field_enabled('quantity'):
                qty = get_form_value(f'quantity_{i}', 1)
                row.append(str(qty))
                total_qty += qty
            else:
                total_qty += 1
            
            if is_field_enabled('condition'):
                condition = get_form_value(f'condition_{i}', "")
                row.append(condition[:4] if condition else "")
            
            if is_field_enabled('category'):
                category = get_form_value(f'category_{i}', "")
                row.append(category[:6] if category else "")
            
            if is_field_enabled('dimensions'):
                dims = get_form_value(f'dimensions_{i}', "")
                row.append(dims[:12] if dims else "")
            
            table_data.append(row)
            current_item_num += 1
    
    # Calculate total table width for consistent sizing
    total_table_width = sum(col_widths)
    
    # Create items table
    items_table = Table(table_data, colWidths=col_widths)
    
    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.3, 0.3, 0.3)),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 1), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.Color(0.95, 0.95, 0.95)]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.Color(0.7, 0.7, 0.7)),
        ('BOX', (0, 0), (-1, -1), 1, colors.Color(0.5, 0.5, 0.5)),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]
    items_table.hAlign = "CENTER"   
    items_table.setStyle(TableStyle(table_style))
    story.append(items_table)
    
    # Summary row - build dynamically, matching table width
    story.append(Spacer(1, 2))
    
    summary_parts = [f"{item_count} Items"]
    
    if is_field_enabled('quantity'):
        summary_parts.append(f"Total Qty: {total_qty}")
    
    if is_field_enabled('price'):
        summary_parts.append(f"Total: ${total_price:.2f}")
    
    summary_data = [summary_parts]
    # Divide the total table width evenly among summary columns
    summary_col_width = total_table_width / len(summary_parts)
    summary_table = Table(summary_data, colWidths=[summary_col_width] * len(summary_parts))
    
    summary_table.hAlign = "CENTER"
    
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.Color(0.9, 0.9, 0.9)),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 1, colors.Color(0.5, 0.5, 0.5)),
        ('LINEBELOW', (0, 0), (-1, -1), 1, colors.black),
    ]))
    
    story.append(summary_table)
    
    doc.build(story)
    buffer.seek(0)
    return buffer

def generate_photo_sheet(account_number, customer_name=""):
    """Generate a PDF with item photos"""
    from reportlab.platypus import Image as RLImage
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch
    )
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        spaceAfter=2,
        fontName='Helvetica-Bold'
    )
    
    subtitle_style = ParagraphStyle(
        'Subtitle',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.Color(0.4, 0.4, 0.4),
        spaceAfter=20
    )
    
    story = []
    
    # Header - show customer name if available, otherwise account number
    story.append(Paragraph("Consigned By Design - Item Photos", title_style))
    if customer_name:
        story.append(Paragraph(f"Consigner: {customer_name} | Date: {datetime.now().strftime('%m/%d/%Y')}", subtitle_style))
    else:
        story.append(Paragraph(f"Account #: {account_number or 'N/A'} | Date: {datetime.now().strftime('%m/%d/%Y')}", subtitle_style))
    
    # Collect items (respecting status if enabled)
    current_item_num = 1
    items_with_photos = []
    
    for i in range(st.session_state.num_items):
        include_item = True
        if is_field_enabled('status'):
            include_item = get_form_value(f"status_{i}", "Accept") == "Accept"
        
        if include_item:
            name = get_form_value(f"name_{i}", "") or f"Item {current_item_num}"
            price = get_form_value(f'price_{i}', 0.0) if is_field_enabled('price') else None
            qty = get_form_value(f'quantity_{i}', 1) if is_field_enabled('quantity') else 1
            has_photo = i in st.session_state.get('item_images', {})
            
            items_with_photos.append({
                'index': i,
                'item_num': current_item_num,
                'name': name,
                'price': price,
                'qty': qty,
                'has_photo': has_photo
            })
            current_item_num += 1
    
    # Create 2-column grid of photos
    row_data = []
    current_row = []
    
    for item in items_with_photos:
        cell_content = []
        
        # Item header with qty
        cell_content.append(Paragraph(f"<b>#{item['item_num']}</b> - {item['name'][:30]}", styles['Normal']))
        
        # Show price and qty if enabled
        if item['price'] is not None:
            cell_content.append(Paragraph(f"${item['price']:.2f} x {item['qty']}", styles['Normal']))
        elif is_field_enabled('quantity'):
            cell_content.append(Paragraph(f"Qty: {item['qty']}", styles['Normal']))
        
        if item['has_photo']:
            img_bytes = st.session_state.item_images[item['index']]
            img = Image.open(io.BytesIO(img_bytes))
            
            max_size = (200, 200)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            img_buffer = io.BytesIO()
            img.save(img_buffer, format='PNG')
            img_buffer.seek(0)
            
            rl_img = RLImage(img_buffer, width=2.5*inch, height=2.5*inch, kind='proportional')
            cell_content.append(Spacer(1, 5))
            cell_content.append(rl_img)
        else:
            cell_content.append(Spacer(1, 5))
            cell_content.append(Paragraph("<i>No photo</i>", styles['Normal']))
        
        current_row.append(cell_content)
        
        if len(current_row) == 2:
            row_data.append(current_row)
            current_row = []
    
    if current_row:
        current_row.append([])
        row_data.append(current_row)
    
    if row_data:
        photo_table = Table(row_data, colWidths=[3.5*inch, 3.5*inch])
        photo_table.hAlign = "CENTER"
        photo_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.Color(0.8, 0.8, 0.8)),
        ]))
        story.append(photo_table)
    else:
        story.append(Paragraph("No items to display.", styles['Normal']))
    
    doc.build(story)
    buffer.seek(0)
    return buffer

def get_pdf_print_button(pdf_buffer, button_label, key):
    pdf_base64 = base64.b64encode(pdf_buffer.getvalue()).decode("utf-8")

    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        html, body {{
          margin: 0 !important;
          padding: 0 !important;
          height: 100%;
          background: transparent;
          overflow: hidden;
        }}
        .wrap {{
          height: 42px;              
          display: flex;
          align-items: center;
          justify-content: center;
        }}
        button {{
          width: 100%;
          height: 42px;
          padding: 0 1rem;
          font-size: 14px;
          font-weight: 400;
          border-radius: 0.5rem;
          border: 1px solid rgba(49, 51, 63, 0.2);
          background: white;
          color: rgb(49, 51, 63);
          cursor: pointer;
          box-sizing: border-box;
          margin: 0;
          line-height: 1;            
        }}
        button:hover {{
          background: #F0F2F6;
        }}
        button:active {{
          transform: translateY(1px);
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <button id="btn_{key}">{button_label}</button>
      </div>

      <script>
        (function() {{
          const b64 = "{pdf_base64}";
          const btn = document.getElementById("btn_{key}");

          btn.addEventListener("click", () => {{
            const byteCharacters = atob(b64);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {{
              byteNumbers[i] = byteCharacters.charCodeAt(i);
            }}
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], {{ type: "application/pdf" }});
            const url = URL.createObjectURL(blob);

            const w = window.open(url, "_blank");
            if (!w) {{
              alert("Pop-up blocked. Please allow pop-ups to print.");
              return;
            }}

            setTimeout(() => {{
              try {{ w.focus(); w.print(); }} catch(e) {{}}
            }}, 700);
          }});
        }})();
      </script>
    </body>
    </html>
    """

# ============================================
# FIELD CONFIGURATION UI
# ============================================

def render_field_configuration():
    """Render the field configuration panel"""
    st.markdown("### Configure Form Fields")
    st.markdown("Select which fields to include for each item:")
    
    # Create columns for field toggles
    col1, col2 = st.columns(2)
    
    field_items = list(AVAILABLE_FIELDS.items())
    mid = len(field_items) // 2 + len(field_items) % 2
    
    with col1:
        for field_id, config in field_items[:mid]:
            if config['required']:
                st.checkbox(
                    config['label'],
                    value=True,
                    disabled=True,
                    key=f"field_toggle_{field_id}",
                    help="This field is required"
                )
            else:
                current = st.session_state.enabled_fields.get(field_id, config['default_enabled'])
                new_value = st.checkbox(
                    config['label'],
                    value=current,
                    key=f"field_toggle_{field_id}"
                )
                st.session_state.enabled_fields[field_id] = new_value
    
    with col2:
        for field_id, config in field_items[mid:]:
            if config['required']:
                st.checkbox(
                    config['label'],
                    value=True,
                    disabled=True,
                    key=f"field_toggle_{field_id}",
                    help="This field is required"
                )
            else:
                current = st.session_state.enabled_fields.get(field_id, config['default_enabled'])
                new_value = st.checkbox(
                    config['label'],
                    value=current,
                    key=f"field_toggle_{field_id}"
                )
                st.session_state.enabled_fields[field_id] = new_value
    
    st.divider()

# ============================================
# ITEM FIELDS RENDERING
# ============================================

def render_item_fields(item_index, prefix=""):
    """Render the enabled fields for a single item"""
    
    # Name field (always shown)
    name_key = f"name_{item_index}"
    st.text_input(
        "Item name", 
        key=f"{prefix}{name_key}", 
        value=get_form_value(name_key, ""),
        on_change=lambda k=name_key: on_field_change(k)
    )
    
    # Notes field
    if is_field_enabled('notes'):
        notes_key = f"notes_{item_index}"
        st.text_area(
            "Notes", 
            key=f"{prefix}{notes_key}", 
            value=get_form_value(notes_key, ""),
            on_change=lambda k=notes_key: on_field_change(k)
        )
    
    # Create columns for smaller fields
    enabled_small_fields = []
    
    if is_field_enabled('price'):
        enabled_small_fields.append('price')
    if is_field_enabled('quantity'):
        enabled_small_fields.append('quantity')
    if is_field_enabled('condition'):
        enabled_small_fields.append('condition')
    if is_field_enabled('category'):
        enabled_small_fields.append('category')
    
    if enabled_small_fields:
        cols = st.columns(len(enabled_small_fields))
        
        for idx, field_id in enumerate(enabled_small_fields):
            with cols[idx]:
                field_key = f"{field_id}_{item_index}"
                config = AVAILABLE_FIELDS[field_id]
                
                if config['type'] == 'currency':
                    st.number_input(
                        config['label'],
                        min_value=0.0,
                        step=0.01,
                        key=f"{prefix}{field_key}",
                        value=float(get_form_value(field_key, 0.0)),
                        on_change=lambda k=field_key: on_field_change(k)
                    )
                elif config['type'] == 'number':
                    st.number_input(
                        config['label'],
                        min_value=1,
                        step=1,
                        key=f"{prefix}{field_key}",
                        value=int(get_form_value(field_key, 1)),
                        on_change=lambda k=field_key: on_field_change(k)
                    )
                elif config['type'] == 'select':
                    options = config.get('options', [])
                    current_val = get_form_value(field_key, options[0] if options else "")
                    current_idx = options.index(current_val) if current_val in options else 0
                    st.selectbox(
                        config['label'],
                        options=options,
                        index=current_idx,
                        key=f"{prefix}{field_key}",
                        on_change=lambda k=field_key: on_field_change(k)
                    )
    
    # Dimensions field (separate row since it's text)
    if is_field_enabled('dimensions'):
        dims_key = f"dimensions_{item_index}"
        st.text_input(
            "Dimensions",
            key=f"{prefix}{dims_key}",
            value=get_form_value(dims_key, ""),
            placeholder="e.g., 24\" x 36\" x 18\"",
            on_change=lambda k=dims_key: on_field_change(k)
        )

# ============================================
# CONSIGNER INFO SECTION (shared between modes)
# ============================================

def render_consigner_section():
    """Render the consigner information section - used by all modes"""
    st.markdown("### Step 1: Consigner Information")
    
    default_index = 0 if get_form_value('consigner_type_selection', "New Consigner") == "New Consigner" else 1
    
    consigner_type = st.radio(
        "Consigner Type:",
        ["New Consigner", "Existing Consigner"],
        horizontal=True,
        index=default_index,
        key="consigner_type_radio"
    )
    
    st.session_state.consigner_type_selection = consigner_type
    
    if consigner_type == "New Consigner":
        st.session_state.is_new_consigner = True
        st.session_state.starting_item_number = 1
        st.session_state.consigner_search_result = None
        st.session_state.searched_account_number = ""
        st.session_state.manual_account_number = ""
        st.session_state.search_failed = False
        
        # Customer info fields for new consigners - collect upfront
        st.markdown("**Enter the new consigner's information:**")
        
        customer_name = st.text_input(
            "Consigner Name", 
            value=get_form_value("customer_name", ""),
            key="customer_name_step1",
            placeholder="Full name"
        )
        st.session_state.form_values["customer_name"] = customer_name
        
        customer_address = st.text_area(
            "Consigner Address", 
            value=get_form_value("customer_address", ""), 
            height=80,
            key="customer_address_step1",
            placeholder="Street address, city, state, zip"
        )
        st.session_state.form_values["customer_address"] = customer_address
        
        phone_number = st.text_input(
            "Phone Number", 
            value=get_form_value("phone_number", ""),
            key="phone_number_step1",
            placeholder="(XXX) XXX-XXXX"
        )
        st.session_state.form_values["phone_number"] = phone_number
        
    else:
        st.session_state.is_new_consigner = False
        
        col1, col2 = st.columns([3, 1])
        with col1:
            account_search = st.text_input(
                "Account Number:",
                value=get_form_value('searched_account_number', '') or st.session_state.manual_account_number,
                placeholder="Enter account number (e.g., 6732)",
                key="account_search_input"
            )
        with col2:
            st.write("")
            st.write("")
            search_clicked = st.button("Search", use_container_width=True)
        
        if search_clicked and account_search:
            with st.spinner(f"Searching for account {account_search}..."):
                result, error = search_consigner_by_account(account_search)
                
                if error:
                    st.warning(f"Account not found in Shopify: {error}")
                    st.session_state.consigner_search_result = None
                    st.session_state.search_failed = True
                    st.session_state.manual_account_number = account_search
                    st.session_state.searched_account_number = ""
                else:
                    st.session_state.consigner_search_result = result
                    st.session_state.searched_account_number = account_search
                    st.session_state.search_failed = False
                    st.session_state.manual_account_number = ""
        
        # Show search result or manual input option
        if st.session_state.consigner_search_result:
            result = st.session_state.consigner_search_result
            st.success(f"Found account {result['account_number']} ({result['total_items']} items on file)")
            
            # Add name field for existing consigner
            consigner_name = st.text_input(
                "Consigner Name (for reference):",
                value=get_form_value("customer_name", ""),
                key="existing_consigner_name",
                placeholder="Enter consigner's name"
            )
            st.session_state.form_values["customer_name"] = consigner_name
        
        elif st.session_state.search_failed:
            st.info(f"Using manually entered account number: {st.session_state.manual_account_number}")
            manual_input = st.text_input(
                "Confirm Account Number:",
                value=st.session_state.manual_account_number,
                key="manual_account_input"
            )
            st.session_state.manual_account_number = manual_input
            
            # Add name field for manual account entry too
            consigner_name = st.text_input(
                "Consigner Name (for reference):",
                value=get_form_value("customer_name", ""),
                key="manual_consigner_name",
                placeholder="Enter consigner's name"
            )
            st.session_state.form_values["customer_name"] = consigner_name
        
        st.session_state.starting_item_number = 1

# ============================================
# MAIN APP
# ============================================

st.title("CBD Intake Form")

# ============================================
# SIDEBAR: DRAFT MANAGEMENT
# ============================================

with st.sidebar:
    st.header("Saved Drafts")
    
    # Auto-save current form button
    if st.session_state.app_mode and st.session_state.num_items > 0:
        if st.button("Save Current Draft", use_container_width=True):
            save_form_values()
            draft_id = save_current_form_to_draft()
            st.success(f"Saved! ID: {draft_id}")
    
    st.divider()
    
    # List saved drafts
    drafts = get_all_drafts()
    
    if drafts:
        st.caption(f"Drafts auto-delete after {DRAFT_EXPIRY_HOURS} hours")
        
        for draft in drafts:
            draft_id, name, app_mode, created_at, updated_at = draft
            if app_mode == "detection":
                mode_label = "Detection"
            elif app_mode == "email":
                mode_label = "Email Import"
            else:
                mode_label = "General"
            
            with st.container():
                st.markdown(f"**{name}**")
                st.caption(f"{mode_label} | Updated: {updated_at[:16]}")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Load", key=f"load_{draft_id}", use_container_width=True):
                        if restore_draft_to_session(draft_id):
                            st.rerun()
                with col2:
                    if st.button("Delete", key=f"del_{draft_id}", use_container_width=True):
                        delete_draft(draft_id)
                        st.rerun()
                st.divider()
    else:
        st.info("No saved drafts")

# ============================================
# MODE SELECTION SCREEN
# ============================================

if st.session_state.app_mode is None:
    st.markdown("---")
    
    st.markdown("""
    ### What would you like to do?
    
    **Select the type of intake form you want to create.**
    """)
    
    st.markdown("")
    st.markdown("---")
    
    # Item Detection Intake Form option
    st.markdown("""
    #### Option 1: Item Detection Intake Form
    
    **Use this when:** You have multiple items to photograph at once.
    
    **How it works:** 
    1. Enter consigner information (new or existing)
    2. Take one photo of all items together
    3. The system will automatically detect and separate each item
    4. Fill in details for each item (customizable fields)
    5. Generate and print your completed intake form
    """)
    
    if st.button("Start Item Detection Intake Form", use_container_width=True, type="primary"):
        st.session_state.app_mode = "detection"
        st.rerun()
    
    st.markdown("")
    st.markdown("---")
    
    # General Intake Form option
    st.markdown("""
    #### Option 2: General Intake Form
    
    **Use this when:** You want to add items one at a time manually.
    
    **How it works:** 
    1. Enter consigner information (new or existing)
    2. Add each item individually with a photo (optional)
    3. Configure which fields to include (name, notes, price, quantity, etc.)
    4. Generate and print your completed intake form
    """)
    
    if st.button("Start General Intake Form", use_container_width=True, type="primary"):
        st.session_state.app_mode = "general"
        st.session_state.detection_complete = True  # Skip detection, go straight to manual entry
        st.rerun()
    
    st.markdown("")
    st.markdown("---")
    
    # Email Import Intake Form option
    st.markdown("""
    #### Option 3: Email Import Intake Form
    
    **Use this when:** Items were pre-approved via email and you need to generate an intake form.
    
    **How it works:** 
    1. View your recent emails or search for a specific thread
    2. Select the conversation with the consigner
    3. System automatically extracts approved items and customer info
    4. Review, edit if needed, and generate the form
    """)
    
    # Check if Gmail integration is available
    if not GMAIL_AVAILABLE:
        st.warning("Gmail integration requires additional packages. Install with: pip install google-auth google-auth-oauthlib google-api-python-client")
    elif not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        st.warning("Gmail integration requires Google OAuth credentials. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to your environment.")
    elif not ANTHROPIC_AVAILABLE or not ANTHROPIC_API_KEY:
        st.warning("Email parsing requires Anthropic API. Install with: pip install anthropic and add ANTHROPIC_API_KEY to your environment.")
    else:
        if st.button("Start Email Import Intake Form", use_container_width=True, type="primary"):
            st.session_state.app_mode = "email"
            st.session_state.email_import_step = 'queue'
            st.session_state.email_recent_loaded = False
            st.rerun()

# ============================================
# FORM PREVIEW/PRINT PAGE (All Modes)
# ============================================

elif st.session_state.show_form:
    # Header
    st.markdown("---")
    st.markdown("## Form Preview - Ready to Print")
    st.markdown("**Consigned By Design** | 7035 East 96th Street, Suite A, Indianapolis, Indiana 46250")
    st.markdown("---")
    
    st.info("Review your completed form below. You can download or print when ready.")
    
    is_new_consigner = get_form_value('is_new_consigner', True)
    
    # Get customer info (already collected in Step 1)
    customer_name = get_form_value("customer_name", "")
    customer_address = get_form_value("customer_address", "")
    phone_number = get_form_value("phone_number", "")
    
    if is_new_consigner:
        account_number = ""
        
        # Show consigner info summary with edit option
        st.markdown("**Consigner Information:**")
        if customer_name:
            st.markdown(f"Name: {customer_name}")
        if customer_address:
            st.markdown(f"Address: {customer_address}")
        if phone_number:
            st.markdown(f"Phone: {phone_number}")
        
        # Allow editing if needed
        with st.expander("Edit Consigner Information"):
            customer_name = st.text_input(
                "Customer Name", 
                value=customer_name,
                key="customer_name_edit"
            )
            st.session_state.form_values["customer_name"] = customer_name
            
            customer_address = st.text_area(
                "Customer Address", 
                value=customer_address, 
                height=80,
                key="customer_address_edit"
            )
            st.session_state.form_values["customer_address"] = customer_address
            
            phone_number = st.text_input(
                "Phone", 
                value=phone_number,
                key="phone_number_edit"
            )
            st.session_state.form_values["phone_number"] = phone_number
        
        st.divider()
    else:
        if st.session_state.search_failed:
            account_number = st.session_state.manual_account_number
        else:
            account_number = get_form_value("searched_account_number", "")
        
        # Show existing consigner info
        st.markdown("**Consigner Information:**")
        if customer_name:
            st.markdown(f"Name: {customer_name}")
        st.markdown(f"Account #: {account_number}")
        
        st.divider()
    
    # Display header info
    st.markdown("### Item List")
    if customer_name:
        st.markdown(f"**Consigner:** {customer_name}")
    if is_new_consigner:
        st.markdown(f"**Date:** {datetime.now().strftime('%m/%d/%Y')} | **Phone:** {phone_number or 'N/A'}")
    else:
        st.markdown(f"**Date:** {datetime.now().strftime('%m/%d/%Y')} | **Account #:** {account_number or 'N/A'}")
    
    # Show enabled fields summary
    enabled_list = [AVAILABLE_FIELDS[f]['label'] for f in st.session_state.enabled_fields if st.session_state.enabled_fields[f] and f != 'name']
    if enabled_list:
        st.caption(f"Fields: Name, {', '.join(enabled_list)}")
    
    st.divider()
    
    # Display items
    total_price = 0.0
    item_count = 0
    total_qty = 0
    current_item_num = 1
    
    for i in range(st.session_state.num_items):
        include_item = True
        if is_field_enabled('status'):
            include_item = get_form_value(f"status_{i}", "Accept") == "Accept"
        
        if include_item:
            item_count += 1
            name = get_form_value(f"name_{i}", "") or ""
            
            # Build item display
            item_text = f"**Item #{current_item_num}:** {name}"
            
            details = []
            
            if is_field_enabled('price'):
                price = get_form_value(f'price_{i}', 0.0)
                qty = get_form_value(f'quantity_{i}', 1) if is_field_enabled('quantity') else 1
                total_price += price * qty
                details.append(f"Price: ${price:.2f}")
            
            if is_field_enabled('quantity'):
                qty = get_form_value(f'quantity_{i}', 1)
                total_qty += qty
                details.append(f"Qty: {qty}")
            else:
                total_qty += 1
            
            if is_field_enabled('condition'):
                condition = get_form_value(f'condition_{i}', "")
                if condition:
                    details.append(f"Condition: {condition}")
            
            if is_field_enabled('category'):
                category = get_form_value(f'category_{i}', "")
                if category:
                    details.append(f"Category: {category}")
            
            if is_field_enabled('dimensions'):
                dims = get_form_value(f'dimensions_{i}', "")
                if dims:
                    details.append(f"Dimensions: {dims}")
            
            if is_field_enabled('notes'):
                notes = get_form_value(f"notes_{i}", "")
                if notes:
                    details.append(f"Notes: {notes}")
            
            st.markdown(item_text)
            if details:
                st.markdown("  \n".join(details))
            
            st.markdown("---")
            current_item_num += 1
    
    # Summary
    summary_parts = [f"{item_count} items"]
    if is_field_enabled('quantity'):
        summary_parts.append(f"Total quantity: {total_qty}")
    if is_field_enabled('price'):
        summary_parts.append(f"Total value: ${total_price:.2f}")
    
    st.markdown(f"**Summary:** {' | '.join(summary_parts)}")
    
    st.divider()
    
    # Back button
    if st.button("Back to Item Entry", use_container_width=True):
        st.session_state.show_form = False
        st.rerun()
    
    st.markdown("### Download or Print")
    
    pdf_buffer = generate_pdf(
        customer_name, 
        customer_address, 
        account_number, 
        phone_number,
        1,
        is_new_consigner
    )
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="Download Form",
            data=pdf_buffer,
            file_name=f"intake_form_{account_number or 'new'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    with col2:
        pdf_buffer.seek(0)
        components.html(get_pdf_print_button(pdf_buffer, "Print Form", "receipt"), height=50)
    
    st.markdown("### Photo Sheet")
    
    photo_buffer = generate_photo_sheet(account_number, customer_name)
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="Download Photos",
            data=photo_buffer,
            file_name=f"photos_{account_number or 'new'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    with col2:
        photo_buffer.seek(0)
        components.html(get_pdf_print_button(photo_buffer, "Print Photos", "photos"), height=50)
    
    st.markdown("---")
    
    # New form / Exit buttons
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Start New Form", use_container_width=True):
            reset_to_mode_selection()
            st.rerun()
    with col2:
        if st.button("Back to Form Type Selection", use_container_width=True):
            reset_to_mode_selection()
            st.rerun()

# ============================================
# ITEM DETECTION INTAKE FORM
# ============================================

elif st.session_state.app_mode == "detection":
    
    # Clear header showing what they're doing
    st.markdown("---")
    st.markdown("## Item Detection Intake Form")
    st.markdown("**You are creating an intake form.** Follow the steps below to add items, then generate your form for printing.")
    st.markdown("---")
    
    if st.button("Change Form Type"):
        reset_to_mode_selection()
        st.rerun()
    
    st.markdown("")
    
    # Check if we're in "add photo" mode for a specific item
    if st.session_state.adding_photo_for_item is not None:
        item_idx = st.session_state.adding_photo_for_item
        st.markdown(f"### Add Photo for Item {item_idx + 1}")
        
        photo_input = st.file_uploader(
            "Take a photo or choose from library",
            type=["jpeg", "png", "jpg"],
            key=f"add_photo_input_{item_idx}"
        )
        
        if photo_input:
            preview_image = Image.open(io.BytesIO(photo_input.getvalue()))
            st.image(preview_image, caption="Preview", width=300)
            
            st.session_state.item_images[item_idx] = photo_input.getvalue()
            st.success("Photo captured")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("Done", use_container_width=True, type="primary"):
                st.session_state.adding_photo_for_item = None
                st.rerun()
        
        with col2:
            if st.button("Cancel", use_container_width=True):
                if item_idx >= len(st.session_state.boxes_data):
                    if item_idx in st.session_state.item_images:
                        del st.session_state.item_images[item_idx]
                    st.session_state.num_items -= 1
                st.session_state.adding_photo_for_item = None
                st.rerun()
    
    else:
        # STEP 1: Consigner Information
        render_consigner_section()
        
        st.divider()
        
        # Field Configuration (collapsible)
        with st.expander("Configure Form Fields", expanded=False):
            render_field_configuration()
        
        st.divider()
        
        # STEP 2: Item Detection
        st.markdown("### Step 2: Photograph Items")
        
        if not st.session_state.detection_complete:
            st.markdown("Upload or take a photo of the items. The system will try to detect individual items automatically.")
            
            uploaded_file = st.file_uploader(
                "Upload or take a photo of items",
                type=["jpeg", "png", "jpg"],
                key="main_image_upload"
            )

            if uploaded_file:
                process_new_image(uploaded_file.getvalue())
                st.session_state.had_active_input = True
            
            image = None
            if st.session_state.image_data is not None:
                image = Image.open(io.BytesIO(st.session_state.image_data))

            if image:
                st.image(image, caption="Input Image", width=300)
                
                rgb_image = image.convert('RGB')
                img_array = np.array(rgb_image)
                
                model = load_model()

                with st.spinner("Detecting items..."):
                    results = model(img_array, conf=0.15)[0]
                
                boxes = results.boxes.xyxy if results.boxes else []
                
                st.session_state.boxes_data = [list(map(int, box)) for box in boxes]
                st.session_state.item_images = {}
                
                if len(boxes) > 0:
                    st.session_state.num_items = len(boxes)
                    
                    for i, box in enumerate(boxes):
                        x1, y1, x2, y2 = map(int, box)
                        crop_array = img_array[y1:y2, x1:x2]
                        if crop_array.size > 0:
                            crop_pil = Image.fromarray(crop_array)
                            img_byte_arr = io.BytesIO()
                            crop_pil.save(img_byte_arr, format='PNG')
                            st.session_state.item_images[i] = img_byte_arr.getvalue()
                else:
                    # No items detected - keep the image and create one item entry
                    st.session_state.num_items = 1
                    img_byte_arr = io.BytesIO()
                    rgb_image.save(img_byte_arr, format='PNG')
                    st.session_state.item_images[0] = img_byte_arr.getvalue()
                    st.info("No distinct items detected. The image has been added - describe the item(s) manually below.")
                
                st.session_state.detection_complete = True
                st.rerun()
            
            st.markdown("---")
            if st.button("Add Items Manually (Skip Photo)", use_container_width=True):
                st.session_state.detection_complete = True
                st.session_state.num_items = 1
                st.rerun()
        
        else:
            # Detection complete - show results
            
            if st.session_state.image_data:
                col1, col2 = st.columns([3, 1])
                with col1:
                    image = Image.open(io.BytesIO(st.session_state.image_data))
                    st.image(image, caption="Input Image", width=300)
                with col2:
                    if st.button("Reset Image", use_container_width=True):
                        clear_all_data()
                        st.rerun()
            
            detected_count = len(st.session_state.boxes_data)
            if detected_count > 0:
                st.success(f"Detected {detected_count} items - fill in details below")
            else:
                st.info(f"{st.session_state.num_items} item(s) ready for entry")
            
            st.divider()
            
            # STEP 3: Item Details
            st.markdown("### Step 3: Enter Item Details")
            
            for i in range(st.session_state.num_items):
                with st.container():
                    st.markdown(f"**Item {i+1}**")
                    col1, col2 = st.columns([1, 2])
                    
                    with col1:
                        if i in st.session_state.get('item_images', {}):
                            item_image = Image.open(io.BytesIO(st.session_state.item_images[i]))
                            st.image(item_image, caption=f"Item {i+1}", width=180)
                            
                            if st.button("Change Photo", key=f"change_photo_{i}", use_container_width=True):
                                save_form_values() 
                                st.session_state.adding_photo_for_item = i
                                st.rerun()
                        else:
                            st.markdown("*No photo*")
                            if st.button("Add Photo", key=f"add_photo_{i}", use_container_width=True):
                                save_form_values()
                                st.session_state.adding_photo_for_item = i
                                st.rerun()
                        
                        # Status field (if enabled)
                        if is_field_enabled('status'):
                            status_key = f"status_{i}"
                            st.radio(
                                "Status",
                                ["Accept", "Reject"],
                                key=status_key,
                                index=0 if get_form_value(status_key, "Accept") == "Accept" else 1,
                                horizontal=True,
                                on_change=lambda k=status_key: on_field_change(k)
                            )
                    
                    with col2:
                        render_item_fields(i)
                    
                    st.divider()
            
            # Add/Remove items section
            st.markdown("### Add or Remove Items")
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if st.button("Add Item (with photo)", use_container_width=True):
                    save_form_values()
                    new_index = st.session_state.num_items
                    st.session_state.num_items += 1
                    st.session_state.adding_photo_for_item = new_index
                    st.rerun()
            
            with col2:
                if st.button("Add Item (no photo)", use_container_width=True):
                    save_form_values()
                    st.session_state.num_items += 1
                    st.rerun()
            
            with col3:
                if st.session_state.num_items > 0:
                    if st.button("Remove Last Item", use_container_width=True):
                        save_form_values()
                        last_index = st.session_state.num_items - 1
                        if last_index in st.session_state.get('item_images', {}):
                            del st.session_state.item_images[last_index]
                        # Clean up all field values for this item
                        for field_id in AVAILABLE_FIELDS.keys():
                            key = f"{field_id}_{last_index}"
                            if key in st.session_state:
                                del st.session_state[key]
                            if key in st.session_state.form_values:
                                del st.session_state.form_values[key]
                        st.session_state.num_items -= 1
                        st.rerun()
            
            st.markdown("---")
            
            # Summary and Create Form
            st.markdown("### Step 4: Generate Your Form")
            
            if st.session_state.num_items > 0:
                accepted = get_accepted_items_count()
                total_qty = get_total_quantity()
                
                summary_text = f"**Ready to create form:** {accepted} items"
                if is_field_enabled('quantity'):
                    summary_text += f" ({total_qty} total quantity)"
                
                st.markdown(summary_text)
                
                col1, col2 = st.columns(2)
                
                with col1:
                    if st.button("Create Form", use_container_width=True, type="primary"):
                        save_form_values()
                        st.session_state.show_form = True
                        st.rerun()
                
                with col2:
                    if st.button("Save Draft", use_container_width=True):
                        save_form_values()
                        draft_id = save_current_form_to_draft()
                        st.success(f"Draft saved! ID: {draft_id}")

# ============================================
# GENERAL INTAKE FORM
# ============================================

elif st.session_state.app_mode == "general":
    
    # Clear header showing what they're doing
    st.markdown("---")
    st.markdown("## General Intake Form")
    st.markdown("**You are creating an intake form.** Add items one at a time below, then generate your form for printing.")
    st.markdown("---")
    
    if st.button("Change Form Type"):
        reset_to_mode_selection()
        st.rerun()
    
    st.markdown("")
    
    # Check if we're in "add photo" mode for a specific item
    if st.session_state.adding_photo_for_item is not None:
        item_idx = st.session_state.adding_photo_for_item
        st.markdown(f"### Add Photo for Item {item_idx + 1}")
        
        photo_input = st.file_uploader(
            "Take a photo or choose from library",
            type=["jpeg", "png", "jpg"],
            key=f"add_photo_input_{item_idx}"
        )
        
        if photo_input:
            preview_image = Image.open(io.BytesIO(photo_input.getvalue()))
            st.image(preview_image, caption="Preview", width=300)
            
            st.session_state.item_images[item_idx] = photo_input.getvalue()
            st.success("Photo captured")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("Done", use_container_width=True, type="primary"):
                st.session_state.adding_photo_for_item = None
                st.rerun()
        
        with col2:
            if st.button("Cancel", use_container_width=True):
                if item_idx in st.session_state.item_images:
                    del st.session_state.item_images[item_idx]
                # Only remove item if it was just added
                if item_idx == st.session_state.num_items - 1 and not get_form_value(f"name_{item_idx}", ""):
                    st.session_state.num_items -= 1
                st.session_state.adding_photo_for_item = None
                st.rerun()
    
    else:
        # STEP 1: Consigner Information (same as detection mode)
        render_consigner_section()
        
        st.divider()
        
        # Field Configuration (collapsible)
        with st.expander("Configure Form Fields", expanded=False):
            render_field_configuration()
        
        st.divider()
        
        # STEP 2: Add Items Manually
        st.markdown("### Step 2: Add Items")
        st.markdown("Add each item one at a time. Take a photo (optional), then enter the details.")
        
        # Display existing items
        for i in range(st.session_state.num_items):
            with st.container():
                st.markdown(f"**Item {i+1}**")
                col1, col2 = st.columns([1, 2])
                
                with col1:
                    if i in st.session_state.get('item_images', {}):
                        item_image = Image.open(io.BytesIO(st.session_state.item_images[i]))
                        st.image(item_image, caption=f"Item {i+1}", width=180)
                        
                        if st.button("Change Photo", key=f"change_photo_{i}", use_container_width=True):
                            save_form_values() 
                            st.session_state.adding_photo_for_item = i
                            st.rerun()
                    else:
                        st.markdown("*No photo*")
                        if st.button("Add Photo", key=f"add_photo_{i}", use_container_width=True):
                            save_form_values()
                            st.session_state.adding_photo_for_item = i
                            st.rerun()
                    
                    # Status field (if enabled)
                    if is_field_enabled('status'):
                        status_key = f"status_{i}"
                        st.radio(
                            "Status",
                            ["Accept", "Reject"],
                            key=status_key,
                            index=0 if get_form_value(status_key, "Accept") == "Accept" else 1,
                            horizontal=True,
                            on_change=lambda k=status_key: on_field_change(k)
                        )
                
                with col2:
                    render_item_fields(i)
                
                st.divider()
        
        # Add/Remove items section
        st.markdown("### Add or Remove Items")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("Add Item (with photo)", use_container_width=True, type="primary"):
                save_form_values()
                new_index = st.session_state.num_items
                st.session_state.num_items += 1
                st.session_state.adding_photo_for_item = new_index
                st.rerun()
        
        with col2:
            if st.button("Add Item (no photo)", use_container_width=True):
                save_form_values()
                st.session_state.num_items += 1
                st.rerun()
        
        with col3:
            if st.session_state.num_items > 0:
                if st.button("Remove Last Item", use_container_width=True):
                    save_form_values()
                    last_index = st.session_state.num_items - 1
                    if last_index in st.session_state.get('item_images', {}):
                        del st.session_state.item_images[last_index]
                    # Clean up all field values for this item
                    for field_id in AVAILABLE_FIELDS.keys():
                        key = f"{field_id}_{last_index}"
                        if key in st.session_state:
                            del st.session_state[key]
                        if key in st.session_state.form_values:
                            del st.session_state.form_values[key]
                    st.session_state.num_items -= 1
                    st.rerun()
        
        st.markdown("---")
        
        # Summary and Create Form
        st.markdown("### Step 3: Generate Your Form")
        
        if st.session_state.num_items > 0:
            accepted = get_accepted_items_count()
            total_qty = get_total_quantity()
            
            summary_text = f"**Ready to create form:** {accepted} items"
            if is_field_enabled('quantity'):
                summary_text += f" ({total_qty} total quantity)"
            
            st.markdown(summary_text)
            
            col1, col2 = st.columns(2)
            
            with col1:
                if st.button("Create Form", use_container_width=True, type="primary"):
                    save_form_values()
                    st.session_state.show_form = True
                    st.rerun()
            
            with col2:
                if st.button("Save Draft", use_container_width=True):
                    save_form_values()
                    draft_id = save_current_form_to_draft()
                    st.success(f"Draft saved! ID: {draft_id}")
        else:
            st.info("Add at least one item to create a form.")

# ============================================
# EMAIL IMPORT INTAKE FORM
# ============================================

elif st.session_state.app_mode == "email":
    
    # Header
    st.markdown("---")
    st.markdown("## Email Import Intake Form")
    st.markdown("**You are creating an intake form from an email thread.** Select from recent emails or search for a specific conversation.")
    st.markdown("---")
    
    if st.button("Change Form Type"):
        reset_to_mode_selection()
        st.rerun()
    
    st.markdown("")
    
    # Handle photo upload mode
    if st.session_state.adding_photo_for_item is not None:
        item_idx = st.session_state.adding_photo_for_item
        st.markdown(f"### Add Photo for Item {item_idx + 1}")
        
        photo_input = st.file_uploader(
            "Take a photo or choose from library",
            type=["jpeg", "png", "jpg"],
            key=f"email_photo_input_{item_idx}"
        )
        
        if photo_input:
            preview_image = Image.open(io.BytesIO(photo_input.getvalue()))
            st.image(preview_image, caption="Preview", width=300)
            
            st.session_state.item_images[item_idx] = photo_input.getvalue()
            st.success("Photo captured")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("Done", use_container_width=True, type="primary"):
                st.session_state.adding_photo_for_item = None
                st.rerun()
        
        with col2:
            if st.button("Cancel", use_container_width=True):
                if item_idx in st.session_state.item_images:
                    del st.session_state.item_images[item_idx]
                if item_idx == st.session_state.num_items - 1 and not get_form_value(f"name_{item_idx}", ""):
                    st.session_state.num_items -= 1
                st.session_state.adding_photo_for_item = None
                st.rerun()
    
    else:
        # Check Gmail authentication
        gmail_service = get_gmail_service()
        
        if not gmail_service:
            # Need to authenticate
            st.markdown("### Step 1: Connect to Gmail")
            st.markdown("To access your emails, you need to connect your Gmail account.")
            
            # Check for auth code in URL (redirect from Google)
            query_params = st.query_params
            auth_code = query_params.get("code")
            
            if auth_code:
                # Exchange code for token
                with st.spinner("Completing authentication..."):
                    credentials, error = exchange_code_for_token(auth_code)
                    
                    if error:
                        st.error(f"Authentication failed: {error}")
                    else:
                        st.success("Gmail connected successfully!")
                        # Clear the URL parameters
                        st.query_params.clear()
                        st.rerun()
            else:
                # Show auth button
                auth_url, state = get_gmail_auth_url()
                
                if auth_url:
                    st.markdown(f"""
                    Click the button below to authorize access to your Gmail account.
                    
                    **Note:** This only requests read-only access to search and view emails.
                    """)
                    
                    st.link_button("Connect Gmail Account", auth_url, use_container_width=True)
                else:
                    st.error("Could not generate authentication URL. Check your Google OAuth credentials.")
        
        else:
            # Gmail is authenticated - show the import workflow
            
            # Sidebar option to disconnect
            with st.sidebar:
                st.markdown("---")
                st.markdown("**Gmail Connected**")
                if st.button("Disconnect Gmail", use_container_width=True):
                    clear_gmail_auth()
                    st.rerun()
            
            # Step 1: Queue - Show recent emails with search fallback
            if st.session_state.email_import_step == 'queue':
                st.markdown("### Step 1: Select Email Thread")
                
                # Load recent threads automatically if not already loaded
                if not st.session_state.email_recent_loaded:
                    with st.spinner("Loading recent emails..."):
                        threads, error = get_recent_gmail_threads(max_results=15)
                        if error:
                            st.error(f"Failed to load emails: {error}")
                            st.session_state.email_recent_threads = []
                        else:
                            st.session_state.email_recent_threads = threads
                        st.session_state.email_recent_loaded = True
                        st.rerun()
                
                # Display recent threads
                if st.session_state.email_recent_threads:
                    st.markdown("**Recent Emails** - Click to select:")
                    
                    for i, thread in enumerate(st.session_state.email_recent_threads):
                        with st.container():
                            col1, col2 = st.columns([5, 1])
                            
                            with col1:
                                # Clickable thread display
                                st.markdown(f"**{thread['subject'][:60]}{'...' if len(thread['subject']) > 60 else ''}**")
                                st.caption(f"From: {thread['from'][:40]} | {thread['message_count']} message(s)")
                            
                            with col2:
                                if st.button("Select", key=f"select_recent_{i}", use_container_width=True):
                                    st.session_state.selected_thread_id = thread['id']
                                    st.session_state.email_import_step = 'select'
                                    st.rerun()
                            
                            st.markdown("---")
                    
                    # Refresh button
                    if st.button("Refresh Email List", use_container_width=True):
                        st.session_state.email_recent_loaded = False
                        st.rerun()
                
                else:
                    st.info("No recent emails found. Try searching below.")
                
                st.markdown("")
                
                # Search fallback section
                with st.expander("Search for Older Emails", expanded=False):
                    st.markdown("Can't find what you're looking for? Search your inbox:")
                    
                    col1, col2 = st.columns([3, 1])
                    
                    with col1:
                        search_query = st.text_input(
                            "Search emails:",
                            placeholder="Enter customer name, email, or keywords...",
                            key="email_search_query"
                        )
                    
                    with col2:
                        st.write("")
                        st.write("")
                        search_clicked = st.button("Search", use_container_width=True)
                    
                    # Search tips
                    st.caption("""
                    **Tips:** Use `from:email@example.com`, `subject:furniture`, `after:2024/01/01`
                    """)
                    
                    if search_clicked and search_query:
                        with st.spinner("Searching emails..."):
                            results, error = search_gmail_threads(search_query)
                            
                            if error:
                                st.error(f"Search failed: {error}")
                            elif not results:
                                st.warning("No email threads found matching your search.")
                            else:
                                st.session_state.email_search_results = results
                                st.success(f"Found {len(results)} email thread(s)")
                    
                    # Display search results
                    if st.session_state.email_search_results:
                        st.markdown("**Search Results:**")
                        
                        for i, thread in enumerate(st.session_state.email_search_results):
                            with st.container():
                                col1, col2 = st.columns([5, 1])
                                
                                with col1:
                                    st.markdown(f"**{thread['subject'][:60]}**")
                                    st.caption(f"From: {thread['from'][:40]} | {thread['date']}")
                                
                                with col2:
                                    if st.button("Select", key=f"select_search_{i}", use_container_width=True):
                                        st.session_state.selected_thread_id = thread['id']
                                        st.session_state.email_import_step = 'select'
                                        st.rerun()
                                
                                st.markdown("---")
            
            # Step 2: View thread and confirm
            elif st.session_state.email_import_step == 'select':
                st.markdown("### Step 2: Review Email Thread")
                
                if st.button("← Back to Email List"):
                    st.session_state.email_import_step = 'queue'
                    st.session_state.selected_thread_id = None
                    st.session_state.selected_thread_messages = None
                    st.rerun()
                
                # Load thread messages if not already loaded
                if not st.session_state.selected_thread_messages:
                    with st.spinner("Loading email thread..."):
                        messages, error = get_thread_messages(st.session_state.selected_thread_id)
                        
                        if error:
                            st.error(f"Failed to load thread: {error}")
                        else:
                            st.session_state.selected_thread_messages = messages
                
                if st.session_state.selected_thread_messages:
                    st.markdown("**Email Thread Contents:**")
                    
                    # Display messages in an expander
                    with st.expander("View Full Email Thread", expanded=True):
                        for msg in st.session_state.selected_thread_messages:
                            st.markdown(f"**From:** {msg['from']}")
                            st.markdown(f"**Date:** {msg['date']}")
                            st.markdown(f"**Subject:** {msg['subject']}")
                            st.markdown("---")
                            st.markdown(msg['body'][:2000] + ("..." if len(msg['body']) > 2000 else ""))
                            st.markdown("---")
                            st.markdown("")
                    
                    st.markdown("")
                    
                    if st.button("Parse This Thread", use_container_width=True, type="primary"):
                        with st.spinner("Analyzing email thread with AI..."):
                            parsed_data, error = parse_email_thread_with_claude(
                                st.session_state.selected_thread_messages
                            )
                            
                            if error:
                                st.error(f"Failed to parse email: {error}")
                            else:
                                st.session_state.parsed_email_data = parsed_data
                                st.session_state.email_import_step = 'review'
                                st.rerun()
            
            # Step 3: Review parsed data
            elif st.session_state.email_import_step == 'review':
                st.markdown("### Step 3: Review Extracted Information")
                st.markdown("Review the information extracted from the email thread. You can edit in the next step.")
                
                if st.button("← Back to Thread"):
                    st.session_state.email_import_step = 'select'
                    st.session_state.parsed_email_data = None
                    st.rerun()
                
                parsed = st.session_state.parsed_email_data
                
                if parsed:
                    # Customer Information
                    st.markdown("#### Customer Information")
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown(f"**Name:** {parsed.get('customer_name') or 'Not found'}")
                        st.markdown(f"**Email:** {parsed.get('customer_email') or 'Not found'}")
                    with col2:
                        st.markdown(f"**Phone:** {parsed.get('customer_phone') or 'Not found'}")
                        st.markdown(f"**Address:** {parsed.get('customer_address') or 'Not found'}")
                    
                    # Pickup Information
                    if parsed.get('pickup_required'):
                        st.markdown("#### Pickup Information")
                        st.markdown(f"**Pickup Required:** Yes")
                        if parsed.get('pickup_address'):
                            st.markdown(f"**Pickup Address:** {parsed.get('pickup_address')}")
                        if parsed.get('pickup_date'):
                            st.markdown(f"**Pickup Date:** {parsed.get('pickup_date')}")
                    
                    # Items
                    st.markdown("#### Items")
                    items = parsed.get('items', [])
                    
                    if items:
                        for i, item in enumerate(items):
                            status_color = {
                                'approved': 'green',
                                'rejected': 'red',
                                'pending': 'orange',
                                'unknown': 'gray'
                            }.get(item.get('status', 'unknown'), 'gray')
                            
                            st.markdown(f"""
                            **{i+1}. {item.get('name', 'Unknown Item')}**  
                            Status: :{status_color}[{item.get('status', 'unknown').upper()}]  
                            Quantity: {item.get('quantity', 1)}  
                            {f"Notes: {item.get('notes')}" if item.get('notes') else ''}
                            """)
                            st.markdown("---")
                    else:
                        st.warning("No items were extracted from the email thread.")
                    
                    # Summary
                    if parsed.get('summary'):
                        st.markdown("#### Summary")
                        st.info(parsed.get('summary'))
                    
                    st.markdown("")
                    
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("Edit and Create Form", use_container_width=True, type="primary"):
                            # Transfer parsed data to form values
                            st.session_state.is_new_consigner = True
                            st.session_state.form_values['customer_name'] = parsed.get('customer_name') or ''
                            st.session_state.form_values['customer_address'] = parsed.get('pickup_address') or parsed.get('customer_address') or ''
                            st.session_state.form_values['phone_number'] = parsed.get('customer_phone') or ''
                            
                            # Add items (only approved or unknown status)
                            approved_items = [item for item in items if item.get('status') in ['approved', 'unknown', 'pending']]
                            st.session_state.num_items = len(approved_items)
                            
                            for i, item in enumerate(approved_items):
                                st.session_state.form_values[f'name_{i}'] = item.get('name', '')
                                st.session_state.form_values[f'notes_{i}'] = item.get('notes', '')
                                st.session_state.form_values[f'quantity_{i}'] = item.get('quantity', 1)
                                st.session_state.form_values[f'status_{i}'] = 'Accept'
                                st.session_state.form_values[f'price_{i}'] = 0.0
                            
                            st.session_state.detection_complete = True
                            st.session_state.email_import_step = 'edit'
                            st.rerun()
                    
                    with col2:
                        if st.button("Re-parse Thread", use_container_width=True):
                            st.session_state.parsed_email_data = None
                            st.session_state.email_import_step = 'select'
                            st.rerun()
            
            # Step 4: Edit form
            elif st.session_state.email_import_step == 'edit':
                st.markdown("### Step 4: Edit and Finalize")
                st.markdown("Review and edit the imported items before generating your form.")
                
                if st.button("← Back to Review"):
                    st.session_state.email_import_step = 'review'
                    st.rerun()
                
                st.divider()
                
                # Customer info (editable)
                st.markdown("#### Customer Information")
                
                customer_name = st.text_input(
                    "Customer Name",
                    value=get_form_value("customer_name", ""),
                    key="email_customer_name"
                )
                st.session_state.form_values["customer_name"] = customer_name
                
                customer_address = st.text_area(
                    "Pickup/Customer Address",
                    value=get_form_value("customer_address", ""),
                    height=80,
                    key="email_customer_address"
                )
                st.session_state.form_values["customer_address"] = customer_address
                
                phone_number = st.text_input(
                    "Phone Number",
                    value=get_form_value("phone_number", ""),
                    key="email_phone_number"
                )
                st.session_state.form_values["phone_number"] = phone_number
                
                st.divider()
                
                # Field Configuration
                with st.expander("Configure Form Fields", expanded=False):
                    render_field_configuration()
                
                st.divider()
                
                # Items (editable)
                st.markdown("#### Items")
                
                for i in range(st.session_state.num_items):
                    with st.container():
                        st.markdown(f"**Item {i+1}**")
                        col1, col2 = st.columns([1, 2])
                        
                        with col1:
                            if i in st.session_state.get('item_images', {}):
                                item_image = Image.open(io.BytesIO(st.session_state.item_images[i]))
                                st.image(item_image, caption=f"Item {i+1}", width=180)
                                
                                if st.button("Change Photo", key=f"email_change_photo_{i}", use_container_width=True):
                                    save_form_values()
                                    st.session_state.adding_photo_for_item = i
                                    st.rerun()
                            else:
                                st.markdown("*No photo*")
                                if st.button("Add Photo", key=f"email_add_photo_{i}", use_container_width=True):
                                    save_form_values()
                                    st.session_state.adding_photo_for_item = i
                                    st.rerun()
                            
                            # Status field (if enabled)
                            if is_field_enabled('status'):
                                status_key = f"status_{i}"
                                current_status = get_form_value(status_key, "Accept")
                                new_status = st.radio(
                                    "Status",
                                    ["Accept", "Reject"],
                                    key=f"email_{status_key}",
                                    index=0 if current_status == "Accept" else 1,
                                    horizontal=True
                                )
                                st.session_state.form_values[status_key] = new_status
                        
                        with col2:
                            render_item_fields(i, prefix="email_")
                            # Sync back to form_values
                            for field_id in AVAILABLE_FIELDS.keys():
                                prefixed_key = f"email_{field_id}_{i}"
                                if prefixed_key in st.session_state:
                                    st.session_state.form_values[f"{field_id}_{i}"] = st.session_state[prefixed_key]
                        
                        st.divider()
                
                # Add/Remove items
                st.markdown("#### Add or Remove Items")
                
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    if st.button("Add Item (with photo)", key="email_add_photo", use_container_width=True):
                        save_form_values()
                        new_index = st.session_state.num_items
                        st.session_state.num_items += 1
                        st.session_state.adding_photo_for_item = new_index
                        st.rerun()
                
                with col2:
                    if st.button("Add Item (no photo)", key="email_add_no_photo", use_container_width=True):
                        save_form_values()
                        st.session_state.num_items += 1
                        st.rerun()
                
                with col3:
                    if st.session_state.num_items > 0:
                        if st.button("Remove Last Item", key="email_remove_last", use_container_width=True):
                            save_form_values()
                            last_index = st.session_state.num_items - 1
                            if last_index in st.session_state.get('item_images', {}):
                                del st.session_state.item_images[last_index]
                            # Clean up all field values for this item
                            for field_id in AVAILABLE_FIELDS.keys():
                                key = f"{field_id}_{last_index}"
                                if key in st.session_state.form_values:
                                    del st.session_state.form_values[key]
                            st.session_state.num_items -= 1
                            st.rerun()
                
                st.markdown("---")
                
                # Generate form
                st.markdown("### Step 5: Generate Your Form")
                
                if st.session_state.num_items > 0:
                    accepted = get_accepted_items_count()
                    total_qty = get_total_quantity()
                    
                    summary_text = f"**Ready to create form:** {accepted} items"
                    if is_field_enabled('quantity'):
                        summary_text += f" ({total_qty} total quantity)"
                    
                    st.markdown(summary_text)
                    
                    col1, col2 = st.columns(2)
                    
                    with col1:
                        if st.button("Create Form", key="email_create_form", use_container_width=True, type="primary"):
                            save_form_values()
                            st.session_state.show_form = True
                            st.rerun()
                    
                    with col2:
                        if st.button("Save Draft", key="email_save_draft", use_container_width=True):
                            save_form_values()
                            draft_id = save_current_form_to_draft()
                            st.success(f"Draft saved! ID: {draft_id}")
                else:
                    st.info("Add at least one item to create a form.")