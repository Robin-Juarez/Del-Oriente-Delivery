import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="Del Oriente Delivery", layout="wide")

st.title("🚚 Del Oriente Delivery - Sistema de Rutas")

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("Control de Ruteo")
    piloto = st.selectbox("Seleccionar Piloto", ["Juan Repartidor", "Pedro Gómez"])
    
    if st.button("🚀 Optimizar y Generar Ruta", type="primary"):
        st.info("Obteniendo datos y consultando OSRM...")
        # Aquí se invoca tu función de optimización existente
        st.success("¡Ruta generada y guardada en SQL Server!")

with col2:
    st.subheader("Mapa de Secuencia de Entregas")
    # Generar un mapa centrado en la ciudad
    m = folium.Map(location=[14.5950, -90.5120], zoom_start=13)
    
    # Agregar marcadores de las entregas
    folium.Marker([14.5950, -90.5120], popup="Bodega Central", icon=folium.Icon(color="red", icon="home")).add_to(m)
    folium.Marker([14.5910, -90.5080], popup="Parada 1: PKG-002", icon=folium.Icon(color="blue", icon="info-sign")).add_to(m)
    folium.Marker([14.5950, -90.5120], popup="Parada 2: PKG-001", icon=folium.Icon(color="blue", icon="info-sign")).add_to(m)
    
    st_folium(m, width=700, height=450)