import streamlit as st
from PIL import Image 
import cv2
import numpy as np
from ultralytics import YOLO
import io
import hashlib
import os
import requests
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

st.title("CBD Intake Form")

# Shopify API functions
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
            
            # Extract item numbers and find the highest
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


# Initialize session state
def init_session_state():
    defaults = {
        'show_form': False,
        'image_data': None,
        'image_hash': None,
        'boxes_data': [],
        'detection_complete': False,
        'num_items': 0,
        'form_values': {},
        'had_active_input': False,
        'is_new_consigner': True,
        'starting_item_number': 0,
        'consigner_type_selection': "New Consigner",
        'consigner_search_result': None,
        'searched_account_number': ""
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_session_state()

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

def save_form_values():
    for i in range(st.session_state.num_items):
        for prefix in ['status_', 'name_', 'notes_', 'price_']:
            key = f"{prefix}{i}"
            if key in st.session_state:
                st.session_state.form_values[key] = st.session_state[key]
    
    st.session_state.form_values['consigner_type_selection'] = st.session_state.consigner_type_selection
    st.session_state.form_values['starting_item_number'] = st.session_state.starting_item_number
    st.session_state.form_values['is_new_consigner'] = st.session_state.is_new_consigner
    st.session_state.form_values['searched_account_number'] = st.session_state.searched_account_number

def get_form_value(key, default):
    return st.session_state.form_values.get(key, default)

def process_new_image(image_bytes):
    new_hash = get_image_hash(image_bytes)
    if st.session_state.image_hash != new_hash:
        st.session_state.image_data = image_bytes
        st.session_state.image_hash = new_hash
        st.session_state.boxes_data = []
        st.session_state.detection_complete = False
        st.session_state.num_items = 0
        st.session_state.form_values = {}
    st.session_state.had_active_input = True

def get_accepted_items_count():
    count = 0
    for i in range(st.session_state.num_items):
        if get_form_value(f"status_{i}", "Accept") == "Accept":
            count += 1
    return count

def generate_pdf(customer_name, customer_address, account_number, phone_number, starting_item_num, is_new_consigner):
    """Generate a PDF receipt matching the consignment form style"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    
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
    
    # Item List Header
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
    info_data = [[
        f"Today's Date: {today}",
        f"Account #: {account_number or 'N/A'}",
        f"Phone: {phone_number or 'N/A'}",
        "Page #: 1"
    ]]
    info_table = Table(info_data, colWidths=[1.8*inch, 1.8*inch, 1.8*inch, 1.1*inch])
    info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 8))
    
    # Items Table Header
    table_data = [[
        "Date\nProcessed", "Item #", "Title", "Status", "Price", "PC", "QTY", "Cost", "Pickup Date"
    ]]
    
    # Add accepted items
    total_price = 0.0
    item_count = 0
    current_item_num = starting_item_num
    
    for i in range(st.session_state.num_items):
        if get_form_value(f"status_{i}", "Accept") == "Accept":
            item_count += 1
            price = get_form_value(f'price_{i}', 0.0)
            total_price += price
            name = get_form_value(f"name_{i}", "") or ""
            notes = get_form_value(f"notes_{i}", "")
            
            if notes:
                title_text = f"{name}\n{notes}"
            else:
                title_text = name
            
            table_data.append([
                today,
                str(current_item_num),
                title_text,
                "A",
                f"${price:.2f}",
                "A",
                "1",
                "",
                ""
            ])
            current_item_num += 1
    
    # Create items table
    items_table = Table(
        table_data, 
        colWidths=[0.75*inch, 0.5*inch, 2.2*inch, 0.5*inch, 0.65*inch, 0.35*inch, 0.4*inch, 0.5*inch, 0.75*inch]
    )
    
    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.3, 0.3, 0.3)),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('ALIGN', (0, 1), (0, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'LEFT'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),
        ('ALIGN', (5, 1), (6, -1), 'CENTER'),
        ('VALIGN', (0, 1), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.Color(0.95, 0.95, 0.95)]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.Color(0.7, 0.7, 0.7)),
        ('BOX', (0, 0), (-1, -1), 1, colors.Color(0.5, 0.5, 0.5)),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]
    
    items_table.setStyle(TableStyle(table_style))
    story.append(items_table)
    
    # Summary row
    story.append(Spacer(1, 2))
    
    summary_data = [[
        f"{item_count} Unique Items",
        f"Quantity on Hand: {item_count}",
        f"${total_price:.2f}",
        "$0.00"
    ]]
    
    summary_table = Table(
        summary_data,
        colWidths=[1.25*inch, 2.7*inch, 1.4*inch, 1.25*inch]
    )
    
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.Color(0.9, 0.9, 0.9)),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('ALIGN', (1, 0), (1, 0), 'CENTER'),
        ('ALIGN', (2, 0), (2, 0), 'RIGHT'),
        ('ALIGN', (3, 0), (3, 0), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 1, colors.Color(0.5, 0.5, 0.5)),
        ('LINEBELOW', (0, 0), (-1, -1), 1, colors.black),
    ]))
    
    story.append(summary_table)
    
    doc.build(story)
    buffer.seek(0)
    return buffer

# Form creation page
if st.session_state.show_form:
    # Company Header
    st.markdown("""
    <div style="margin-bottom: 20px;">
        <h2 style="margin-bottom: 5px;">Consigned By Design</h2>
        <p style="margin: 0; color: #666;">7035 East 96th Street<br>
        Suite A<br>
        Indianapolis, Indiana 46250</p>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("### Item List")
    
    # Get values from form_values (persisted)
    is_new_consigner = get_form_value('is_new_consigner', True)
    starting_item_num = get_form_value('starting_item_number', 0)
    
    # Customer info section - only show for new consigners
    if is_new_consigner:
        col1, col2 = st.columns(2)
        with col1:
            customer_name = st.text_input("Customer Name", value=get_form_value("customer_name", ""))
            customer_address = st.text_area("Customer Address", value=get_form_value("customer_address", ""), height=100)
        with col2:
            account_number = st.text_input("Account #", value=get_form_value("account_number", ""))
            phone_number = st.text_input("Phone", value=get_form_value("phone_number", ""))
        
        st.divider()
    else:
        # For existing consigners, use saved values
        customer_name = get_form_value("customer_name", "")
        customer_address = get_form_value("customer_address", "")
        account_number = get_form_value("searched_account_number", "")
        phone_number = get_form_value("phone_number", "")
    
    # Date and page info
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f"**Today's Date:** {datetime.now().strftime('%m/%d/%Y')}")
    with col2:
        st.markdown(f"**Account #:** {account_number or 'N/A'}")
    with col3:
        st.markdown(f"**Phone:** {phone_number or 'N/A'}")
    with col4:
        st.markdown("**Page #:** 1")
    
    st.divider()
    
    # Table header
    cols = st.columns([1.2, 0.8, 2.5, 0.8, 1, 0.5, 0.5, 0.7, 1])
    headers = ["Date Processed", "Item #", "Title", "Status", "Price", "PC", "QTY", "Cost", "Pickup Date"]
    for col, header in zip(cols, headers):
        col.markdown(f"**{header}**")
    
    st.divider()
    
    # Table rows for accepted items
    total_price = 0.0
    item_count = 0
    current_item_num = starting_item_num
    
    for i in range(st.session_state.num_items):
        if get_form_value(f"status_{i}", "Accept") == "Accept":
            item_count += 1
            price = get_form_value(f'price_{i}', 0.0)
            total_price += price
            
            cols = st.columns([1.2, 0.8, 2.5, 0.8, 1, 0.5, 0.5, 0.7, 1])
            
            with cols[0]:
                st.write(datetime.now().strftime('%m/%d/%Y'))
            with cols[1]:
                st.write(str(current_item_num))
            with cols[2]:
                name = get_form_value(f"name_{i}", "") or ""
                notes = get_form_value(f"notes_{i}", "")
                if name:
                    st.markdown(f"**{name}**")
                if notes:
                    st.caption(notes)
            with cols[3]:
                st.write("A")
            with cols[4]:
                st.write(f"${price:.2f}")
            with cols[5]:
                st.write("A")
            with cols[6]:
                st.write("1")
            with cols[7]:
                st.write("")
            with cols[8]:
                st.write("")
            
            current_item_num += 1
    
    st.divider()
    
    # Summary row
    cols = st.columns([2, 3, 1.5, 1.5, 1])
    with cols[0]:
        st.markdown(f"**{item_count} Unique Items**")
    with cols[1]:
        st.markdown(f"**Quantity on Hand: {item_count}**")
    with cols[2]:
        st.markdown(f"**${total_price:.2f}**")
    with cols[3]:
        st.markdown("**$0.00**")
    
    st.divider()
    
    # Action buttons
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        if st.button("← Back to Detection"):
            st.session_state.form_values["customer_name"] = customer_name
            st.session_state.form_values["customer_address"] = customer_address
            st.session_state.form_values["account_number"] = account_number
            st.session_state.form_values["phone_number"] = phone_number
            st.session_state.show_form = False
            st.session_state.had_active_input = False
            st.rerun()
    with col3:
        pdf_buffer = generate_pdf(
            customer_name, 
            customer_address, 
            account_number, 
            phone_number,
            starting_item_num,
            is_new_consigner
        )
        st.download_button(
            label="📥 Download PDF",
            data=pdf_buffer,
            file_name=f"intake_form_{account_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
            mime="application/pdf"
        )

# Detection page
else:
    # Consigner type selection
    st.subheader("Consigner Information")
    
    # Restore consigner type from form_values if coming back from form
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
        st.session_state.starting_item_number = 0
        st.session_state.consigner_search_result = None
        st.session_state.searched_account_number = ""
    else:
        st.session_state.is_new_consigner = False
        
        # Account number search
        col1, col2 = st.columns([3, 1])
        with col1:
            account_search = st.text_input(
                "Account Number:",
                value=get_form_value('searched_account_number', ''),
                placeholder="Enter account number (e.g., 6732)",
                key="account_search_input"
            )
        with col2:
            st.write("")  # Spacing
            st.write("")  # Spacing
            search_clicked = st.button("🔍 Search", use_container_width=True)
        
        # Perform search
        if search_clicked and account_search:
            with st.spinner(f"Searching for account {account_search}..."):
                result, error = search_consigner_by_account(account_search)
                
                if error:
                    st.error(error)
                    st.session_state.consigner_search_result = None
                else:
                    st.session_state.consigner_search_result = result
                    st.session_state.searched_account_number = account_search
                    st.session_state.starting_item_number = result['next_item_number']
        
        # Display search results
        if st.session_state.consigner_search_result:
            result = st.session_state.consigner_search_result
            
            st.success(f"✓ Found account {result['account_number']}")
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Items on File", result['total_items'])
            with col2:
                st.metric("Highest Item #", result['highest_item_number'])
            with col3:
                st.metric("Next Item # Starts At", result['next_item_number'])
            
            # Show recent items (collapsible)
            with st.expander("View Recent Items"):
                recent_items = result['items'][-10:]  # Last 10 items
                for item in reversed(recent_items):
                    st.write(f"**#{item['item_number']}**: {item['title'][:50]} - ${item['price']} (Qty: {item['qty']})")
            
            st.session_state.starting_item_number = result['next_item_number']
        
        elif account_search and not search_clicked:
            # Restore previous search if coming back from form
            saved_account = get_form_value('searched_account_number', '')
            if saved_account and saved_account == account_search:
                st.session_state.starting_item_number = get_form_value('starting_item_number', 0)
                st.info(f"Using previously found data. Next item starts at #{st.session_state.starting_item_number}")
    
    st.divider()
    
    # Image input section
    st.subheader("Item Detection")
    
    input_mode = st.radio(
        "Choose image source:", 
        ["Upload Image", "Take Photo"], 
        horizontal=True,
        key="input_mode"
    )

    has_active_input = False

    if input_mode == "Take Photo":
        camera_image = st.camera_input("Take a picture of the items")
        if camera_image:
            has_active_input = True
            process_new_image(camera_image.getvalue())
    else:
        uploaded_file = st.file_uploader("Upload an image", type=["jpeg", "png"])
        if uploaded_file:
            has_active_input = True
            process_new_image(uploaded_file.getvalue())

    if st.session_state.had_active_input and not has_active_input and st.session_state.image_data is not None:
        clear_all_data()
        st.rerun()

    image = None
    if st.session_state.image_data is not None:
        image = Image.open(io.BytesIO(st.session_state.image_data))

    if image:
        st.image(image, caption="Input Image", width=150)
        
        if not st.session_state.detection_complete:
            img_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            model = YOLO("yolov8n.pt")

            with st.spinner("Detecting items..."):
                results = model(img_cv)[0]
            
            boxes = results.boxes.xyxy if results.boxes else []
            
            st.session_state.boxes_data = [list(map(int, box)) for box in boxes]
            st.session_state.num_items = len(boxes)
            st.session_state.detection_complete = True
            st.rerun()
        
        boxes = st.session_state.boxes_data
        st.subheader(f"Detected {len(boxes)} items")
        
        if len(boxes) == 0:
            st.warning("No items detected. Try uploading a different image.")
        else:
            img_cv = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

            for i, box in enumerate(boxes):
                x1, y1, x2, y2 = box
                crop = img_cv[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                crop_pil = Image.fromarray(crop_rgb)
                
                col1, col2 = st.columns([1, 2])
                
                with col1:
                    st.image(crop_pil, caption=f"Item {i+1}", width=180)
                    st.radio(
                        "Status",
                        ["Accept", "Reject"],
                        key=f"status_{i}",
                        index=0 if get_form_value(f"status_{i}", "Accept") == "Accept" else 1,
                        horizontal=True
                    )
                
                with col2:
                    st.text_input("Item name", key=f"name_{i}", value=get_form_value(f"name_{i}", ""))
                    st.text_area("Notes", key=f"notes_{i}", value=get_form_value(f"notes_{i}", ""))
                    st.number_input("Price ($)", min_value=0.0, step=0.01, key=f"price_{i}", value=get_form_value(f"price_{i}", 0.0))
                
                st.divider()
            
            if st.button("Create Form"):
                save_form_values()
                st.session_state.show_form = True
                st.rerun()