import streamlit as st
import streamlit.components.v1 as components

st.markdown("### Print your document")
print_button = st.button("print")

if print_button:
    components.html(
        """
        <script>
            window.print();
        </script>
        """,
        height=0,
    )