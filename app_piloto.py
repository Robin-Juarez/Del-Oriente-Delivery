import streamlit as st
import pandas as pd
import requests
import folium
from streamlit_folium import st_folium
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import urllib.parse

# Configuración móvil / responsive
st.set_page_config(page_title="App Piloto - Del Oriente", layout="centered", initial_sidebar_state="collapsed")

st.title("📱 App Piloto - Hoja de Ruta")

OSRM_URL = "http://localhost:5000/table/v1/driving"

# Estados de Sesión
if 'estados_entregas' not in st.session_state:
    st.session_state.estados_entregas = {}
if 'puntos_ruta' not in st.session_state:
    st.session_state.puntos_ruta = None
if 'secuencia_optima' not in st.session_state:
    st.session_state.secuencia_optima = None

# --- CARGA SIMPLIFICADA DE PRUEBA / MÓVIL ---
st.markdown("### 📋 Cargar Hoja de Trabajo")
archivo_piloto = st.file_uploader("Subir hoja de ruta (.xlsx)", type=["xlsx", "xls"])

if archivo_piloto is not None:
    if st.button("🚀 Iniciar Ruta del Día", type="primary", use_container_width=True):
        df = pd.read_excel(archivo_piloto, header=None)
        
        # Puntos base simulados para demo rápida
        puntos = [{
            'id': 0, 'warehouse': 'BODEGA', 'nombre': 'Bodega Central',
            'direccion': 'Punto de Salida', 'telefono': '', 'lat': 14.5950, 'lon': -90.5120
        }]
        
        for idx, row in df.iterrows():
            if pd.isna(row[0]): continue
            puntos.append({
                'id': idx + 1,
                'warehouse': str(row[0]).strip(),
                'nombre': str(row[1]).strip() if len(row) > 1 else 'Cliente',
                'direccion': str(row[2]).strip() if len(row) > 2 else 'Guatemala',
                'telefono': str(row[3]).strip() if len(row) > 3 else '',
                'lat': 14.6000 + (idx * 0.005), # Coordenadas de prueba
                'lon': -90.5100 - (idx * 0.003)
            })
        
        # Guardar en sesión
        st.session_state.puntos_ruta = puntos
        st.session_state.secuencia_optima = list(range(len(puntos)))
        st.session_state.estados_entregas = {p['id']: 'Pendiente' for p in puntos if p['id'] != 0}
        st.rerun()

# --- VISTA DEL PILOTO ---
if st.session_state.secuencia_optima and st.session_state.puntos_ruta:
    puntos = st.session_state.puntos_ruta
    secuencia = st.session_state.secuencia_optima
    
    # Métrica de progreso
    total_pedidos = len(puntos) - 1
    completados = sum(1 for v in st.session_state.estados_entregas.values() if v == 'Entregado')
    st.progress(completados / total_pedidos if total_pedidos > 0 else 0)
    st.caption(f"Progreso: {completados} de {total_pedidos} paquetes entregados.")
    
    st.markdown("---")
    
    # Listado de Paradas optimizadas
    for paso, idx in enumerate(secuencia):
        pt = puntos[idx]
        if idx == 0:
            continue # Omitir Bodega en la lista de tarjetas
            
        estado_actual = st.session_state.estados_entregas.get(pt['id'], 'Pendiente')
        
        # Color del encabezado según estado
        color_badge = "🟢" if estado_actual == 'Entregado' else ("🔴" if estado_actual == 'Ausente' else "🟡")
        
        with st.expander(f"{color_badge} Parada {paso}: [{pt['warehouse']}] - {pt['nombre']}", expanded=(estado_actual == 'Pendiente')):
            st.write(f"**📍 Dirección:** {pt['direccion']}")
            st.write(f"**📞 Teléfono:** {pt['telefono']}")
            st.write(f"**Estado:** `{estado_actual}`")
            
            # Links a Navegadores (Google Maps / Waze)
            dir_encoded = urllib.parse.quote(f"{pt['direccion']}, Guatemala")
            gmaps_url = f"https://www.google.com/maps/search/?api=1&query={pt['lat']},{pt['lon']}"
            waze_url = f"https://waze.com/ul?ll={pt['lat']},{pt['lon']}&navigate=yes"
            
            col_b1, col_b2, col_b3 = st.columns(3)
            with col_b1:
                st.markdown(f"[🗺️ Google Maps]({gmaps_url})")
            with col_b2:
                st.markdown(f"[🧭 Waze]({waze_url})")
            with col_b3:
                if pt['telefono']:
                    st.markdown(f"[📞 Llamar](tel:{pt['telefono']})")
            
            st.markdown("---")
            # Actualización de Estado
            col_e1, col_e2, col_e3 = st.columns(3)
            if col_e1.button("✅ Entregado", key=f"ent_{pt['id']}", use_container_width=True):
                st.session_state.estados_entregas[pt['id']] = 'Entregado'
                st.rerun()
            if col_e2.button("🚫 Ausente", key=f"aus_{pt['id']}", use_container_width=True):
                st.session_state.estados_entregas[pt['id']] = 'Ausente'
                st.rerun()
            if col_e3.button("🔄 Pendiente", key=f"pen_{pt['id']}", use_container_width=True):
                st.session_state.estados_entregas[pt['id']] = 'Pendiente'
                st.rerun()