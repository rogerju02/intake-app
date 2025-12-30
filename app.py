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
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

try:
    SHOPIFY_STORE_URL = st.secrets.get("SHOPIFY_STORE_URL", os.getenv("SHOPIFY_STORE_URL"))
    SHOPIFY_ACCESS_TOKEN = st.secrets.get("SHOPIFY_ACCESS_TOKEN", os.getenv("SHOPIFY_ACCESS_TOKEN"))
except:
    SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
    SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

# Cache the model to prevent reloading everytime
@st.cache_resource
def load_model():
    return YOLO("yolov8m.pt")

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
        'searched_account_number': "",
        'item_images': {},
        'adding_photo_for_item': None 
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
    st.session_state.item_images = {}
    st.session_state.adding_photo_for_item = None

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
        st.session_state.item_images = {}
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
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=1.5*inch,
        bottomMargin=1.5*inch,
        leftMargin=1.5*inch,
        rightMargin=1.5*inch
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
    info_table.hAlign = "CENTER"   
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
    items_table.hAlign = "CENTER"   
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
    summary_table.hAlign = "CENTER"
    
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

def generate_photo_sheet(account_number, starting_item_num):
    """Generate a PDF with item photos"""
    from reportlab.platypus import Image as RLImage
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=1.5*inch,
        bottomMargin=1.5*inch,
        leftMargin=1.5*inch,
        rightMargin=1.5*inch
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
    
    # Header
    story.append(Paragraph("Consigned By Design - Item Photos", title_style))
    story.append(Paragraph(f"Account #: {account_number or 'N/A'} | Date: {datetime.now().strftime('%m/%d/%Y')}", subtitle_style))
    
    # Collect accepted items with photos
    current_item_num = starting_item_num
    items_with_photos = []
    
    for i in range(st.session_state.num_items):
        if get_form_value(f"status_{i}", "Accept") == "Accept":
            name = get_form_value(f"name_{i}", "") or f"Item {i+1}"
            price = get_form_value(f'price_{i}', 0.0)
            has_photo = i in st.session_state.get('item_images', {})
            
            items_with_photos.append({
                'index': i,
                'item_num': current_item_num,
                'name': name,
                'price': price,
                'has_photo': has_photo
            })
            current_item_num += 1
    
    # Create 2-column grid of photos
    row_data = []
    current_row = []
    
    for item in items_with_photos:
        # Create cell content
        cell_content = []
        
        # Item header
        cell_content.append(Paragraph(f"<b>#{item['item_num']}</b> - {item['name'][:30]}", styles['Normal']))
        cell_content.append(Paragraph(f"${item['price']:.2f}", styles['Normal']))
        
        # Add image if available
        if item['has_photo']:
            img_bytes = st.session_state.item_images[item['index']]
            img = Image.open(io.BytesIO(img_bytes))
            
            # Resize image to fit
            max_size = (200, 200)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # Save to bytes for reportlab
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
        
        # Two items per row
        if len(current_row) == 2:
            row_data.append(current_row)
            current_row = []
    
    # Add remaining item if odd number
    if current_row:
        current_row.append([])  # Empty cell
        row_data.append(current_row)
    
    # Create table
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
# Form Creation Page
if st.session_state.show_form:
    st.markdown("""
    <div style="margin-bottom: 20px;">
        <h2 style="margin-bottom: 5px;">Consigned By Design</h2>
        <p style="margin: 0; color: #666;">7035 East 96th Street<br>
        Suite A<br>
        Indianapolis, Indiana 46250</p>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("### Item List")
    
    is_new_consigner = get_form_value('is_new_consigner', True)
    starting_item_num = get_form_value('starting_item_number', 0)
    
    if is_new_consigner:
        customer_name = st.text_input("Customer Name", value=get_form_value("customer_name", ""))
        customer_address = st.text_area("Customer Address", value=get_form_value("customer_address", ""), height=100)
        
        col1, col2 = st.columns(2)
        with col1:
            account_number = st.text_input("Account #", value=get_form_value("account_number", ""))
        with col2:
            phone_number = st.text_input("Phone", value=get_form_value("phone_number", ""))
        
        st.divider()
    else:
        customer_name = get_form_value("customer_name", "")
        customer_address = get_form_value("customer_address", "")
        account_number = get_form_value("searched_account_number", "")
        phone_number = get_form_value("phone_number", "")
    
    st.markdown(f"**Today's Date:** {datetime.now().strftime('%m/%d/%Y')} | **Account #:** {account_number or 'N/A'} | **Phone:** {phone_number or 'N/A'}")
    
    st.divider()
    
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
            
            st.markdown(f"""
            <div style="background: #f8f8f8; padding: 10px; border-radius: 5px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <strong>#{current_item_num}</strong>
                    <strong>${price:.2f}</strong>
                </div>
                <div style="margin-top: 5px;">{name}</div>
                {f'<div style="color: #666; font-size: 0.9em;">{notes}</div>' if notes else ''}
            </div>
            """, unsafe_allow_html=True)
            
            current_item_num += 1
    
    st.markdown(f"""
    <div style="background: #e0e0e0; padding: 10px; border-radius: 5px; margin-top: 10px;">
        <div style="display: flex; justify-content: space-between;">
            <span><strong>{item_count} Unique Items</strong></span>
            <span><strong>${total_price:.2f}</strong></span>
        </div>
        <div style="color: #666;">Quantity on Hand: {item_count}</div>
    </div>
    """, unsafe_allow_html=True)
    
    st.divider()
    
    # Back button
    if st.button("← Back to Detection", use_container_width=True):
        st.session_state.form_values["customer_name"] = customer_name
        st.session_state.form_values["customer_address"] = customer_address
        st.session_state.form_values["account_number"] = account_number
        st.session_state.form_values["phone_number"] = phone_number
        st.session_state.show_form = False
        st.session_state.had_active_input = False
        st.rerun()
    
    st.markdown("### Receipt")
    
    # Generate Receipt PDF
    pdf_buffer = generate_pdf(
        customer_name, 
        customer_address, 
        account_number, 
        phone_number,
        starting_item_num,
        is_new_consigner
    )
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="Download",
            data=pdf_buffer,
            file_name=f"intake_form_{account_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    with col2:
        pdf_buffer.seek(0)
        components.html(get_pdf_print_button(pdf_buffer, "Print", "receipt"), height=50)
    
    st.markdown("### Photo Sheet")
    
    # Generate Photo Sheet PDF
    photo_buffer = generate_photo_sheet(account_number, starting_item_num)
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="Download",
            data=photo_buffer,
            file_name=f"photos_{account_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    with col2:
        photo_buffer.seek(0)
        components.html(get_pdf_print_button(photo_buffer, "Print", "photos"), height=50)
# Detection page
else:
    # Check if we're in "add photo" mode for a specific item
    if st.session_state.adding_photo_for_item is not None:
        item_idx = st.session_state.adding_photo_for_item
        st.subheader(f"Add Photo for Item {item_idx + 1}")
        
        photo_input = st.file_uploader(
            "Take a photo or choose from library",
            type=["jpeg", "png", "jpg"],
            key=f"add_photo_input_{item_idx}"
        )
        
        if photo_input:
            # Show preview
            preview_image = Image.open(io.BytesIO(photo_input.getvalue()))
            st.image(preview_image, caption="Preview", width=300)
            
            # Auto-save when photo is selected
            st.session_state.item_images[item_idx] = photo_input.getvalue()
            st.success("✓ Photo captured!")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("✓ Done", type="primary", use_container_width=True):
                st.session_state.adding_photo_for_item = None
                st.rerun()
        
        with col2:
            if st.button("✕ Cancel", use_container_width=True):
                # Remove the photo if they cancel AND this was a newly added item
                if item_idx >= len(st.session_state.boxes_data):
                    if item_idx in st.session_state.item_images:
                        del st.session_state.item_images[item_idx]
                    st.session_state.num_items -= 1
                st.session_state.adding_photo_for_item = None
                st.rerun()
    
    else:
        # Normal detection page
        st.subheader("Consigner Information")
        
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
            
            col1, col2 = st.columns([3, 1])
            with col1:
                account_search = st.text_input(
                    "Account Number:",
                    value=get_form_value('searched_account_number', ''),
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
                        st.error(error)
                        st.session_state.consigner_search_result = None
                    else:
                        st.session_state.consigner_search_result = result
                        st.session_state.searched_account_number = account_search
                        st.session_state.starting_item_number = result['next_item_number']
            
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
                
                with st.expander("View Recent Items"):
                    recent_items = result['items'][-10:]
                    for item in reversed(recent_items):
                        st.write(f"**#{item['item_number']}**: {item['title'][:50]} - ${item['price']} (Qty: {item['qty']})")
                
                st.session_state.starting_item_number = result['next_item_number']
            
            elif account_search and not search_clicked:
                saved_account = get_form_value('searched_account_number', '')
                if saved_account and saved_account == account_search:
                    st.session_state.starting_item_number = get_form_value('starting_item_number', 0)
                    st.info(f"Using previously found data. Next item starts at #{st.session_state.starting_item_number}")
        
        st.divider()
        
        # Item Detection Section
        st.subheader("Item Detection")
        
        # Only show uploader if we don't have detection results yet
        if not st.session_state.detection_complete:
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
                
                # Use cached model
                model = load_model()

                with st.spinner("Detecting items..."):
                    results = model(img_array, conf=0.2)[0]
                
                boxes = results.boxes.xyxy if results.boxes else []
                
                st.session_state.boxes_data = [list(map(int, box)) for box in boxes]
                st.session_state.num_items = len(boxes)
                st.session_state.item_images = {}
                
                # Store cropped images from detection
                for i, box in enumerate(boxes):
                    x1, y1, x2, y2 = map(int, box)
                    crop_array = img_array[y1:y2, x1:x2]
                    if crop_array.size > 0:
                        crop_pil = Image.fromarray(crop_array)
                        img_byte_arr = io.BytesIO()
                        crop_pil.save(img_byte_arr, format='PNG')
                        st.session_state.item_images[i] = img_byte_arr.getvalue()
                
                st.session_state.detection_complete = True
                st.rerun()
        
        else:
            # Detection already complete - show results
            
            # Show the original image with option to start over
            if st.session_state.image_data:
                col1, col2 = st.columns([3, 1])
                with col1:
                    image = Image.open(io.BytesIO(st.session_state.image_data))
                    st.image(image, caption="Input Image", width=300)
                with col2:
                    if st.button("Reset", use_container_width=True):
                        clear_all_data()
                        st.rerun()
            
            # Show detected items
            st.subheader(f"Detected {len(st.session_state.boxes_data)} items")
            
            if len(st.session_state.boxes_data) == 0 and st.session_state.num_items == 0:
                st.warning("No items auto-detected. Add items manually below.")
            
            # Display all items
            for i in range(st.session_state.num_items):
                with st.container():
                    col1, col2 = st.columns([1, 2])
                    
                    with col1:
                        if i in st.session_state.get('item_images', {}):
                            item_image = Image.open(io.BytesIO(st.session_state.item_images[i]))
                            st.image(item_image, caption=f"Item {i+1}", width=180)
                            
                            if st.button("Change image", key=f"change_photo_{i}", use_container_width=True):
                                save_form_values() 
                                st.session_state.adding_photo_for_item = i
                                st.rerun()
                        else:
                            st.markdown(f"**Item {i+1}**")
                            if st.button("Add Photo", key=f"add_photo_{i}", type="primary", use_container_width=True):
                                save_form_values()
                                st.session_state.adding_photo_for_item = i
                                st.rerun()
                        
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
            
            # Add/Remove items section
            st.markdown("### Manage Items")
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if st.button("Add Item", use_container_width=True):
                    save_form_values()
                    new_index = st.session_state.num_items
                    st.session_state.num_items += 1
                    st.session_state.adding_photo_for_item = new_index
                    st.rerun()
            
            with col2:
                if st.button("Add (No Photo)", use_container_width=True):
                    save_form_values()
                    st.session_state.num_items += 1
                    st.rerun()
            
            with col3:
                if st.session_state.num_items > 0:
                    if st.button("Remove Last", use_container_width=True):
                        save_form_values()
                        last_index = st.session_state.num_items - 1
                        if last_index in st.session_state.get('item_images', {}):
                            del st.session_state.item_images[last_index]
                        for key in [f"status_{last_index}", f"name_{last_index}", f"notes_{last_index}", f"price_{last_index}"]:
                            if key in st.session_state:
                                del st.session_state[key]
                            if key in st.session_state.form_values:
                                del st.session_state.form_values[key]
                        st.session_state.num_items -= 1
                        st.rerun()
            
            st.markdown("---")
            
            if st.session_state.num_items > 0:
                if st.button("Create Form", use_container_width=True):
                    save_form_values()
                    st.session_state.show_form = True
                    st.rerun()