import streamlit as st
import pandas as pd
import requests
import folium
import re
import math
from datetime import datetime, timedelta
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

st.set_page_config(page_title="App Piloto - Métricas y Tiempos", layout="wide")

st.title("🚚 Del Oriente Delivery - Navegación, Métricas y Tiempos")

OSRM_URL = "http://router.project-osrm.org/table/v1/driving"
geolocator = Nominatim(user_agent="del_oriente_delivery_fast_v2", timeout=3)

# --- ESTADOS DE SESIÓN ---
if 'puntos_cargados' not in st.session_state:
    st.session_state.puntos_cargados = []
if 'secuencia_optima' not in st.session_state:
    st.session_state.secuencia_optima = None
if 'puntos_ruta' not in st.session_state:
    st.session_state.puntos_ruta = None
if 'distancias_pasos' not in st.session_state:
    st.session_state.distancias_pasos = []
if 'distancia_total_m' not in st.session_state:
    st.session_state.distancia_total_m = 0
if 'gps_piloto' not in st.session_state:
    st.session_state.gps_piloto = {'lat': 14.5950, 'lon': -90.5120}

# --- GEOCODIFICACIÓN RÁPIDA CON CACHÉ ---
@st.cache_data(show_spinner=False)
def geocodificar_rapido(direccion_raw):
    dir_clean = str(direccion_raw).strip().lower()
    
    zona_match = re.search(r'zona\s*(\d+)', dir_clean)
    zona_str = f"Zona {zona_match.group(1)}" if zona_match else "Zona 12"
    
    via_match = re.search(r'(\d+\s*(?:avenida|calle|av|cll)|avenida\s*petapa|calzada\s*[a-z]+)', dir_clean)
    via_str = via_match.group(1) if via_match else ""
    
    query = f"{via_str}, {zona_str}, Ciudad de Guatemala, Guatemala" if via_str else f"{dir_clean}, Guatemala"
    
    try:
        location = geolocator.geocode(query)
        if location:
            return location.latitude, location.longitude
        
        loc_gen = geolocator.geocode(f"{zona_str}, Ciudad de Guatemala, Guatemala")
        if loc_gen:
            return loc_gen.latitude, loc_gen.longitude
    except Exception:
        pass
        
    return 14.5950, -90.5120

def calcular_matriz_distancias_haversine(puntos):
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371000  # Metros
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    n = len(puntos)
    matriz = [[0]*n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            matriz[i][j] = int(haversine(puntos[i]['lat'], puntos[i]['lon'], puntos[j]['lat'], puntos[j]['lon']))
    return matriz

# --- DETECCIÓN DE COLUMNAS ---
def detectar_columnas_inteligente(df):
    mapa = {'warehouse': None, 'nombre': None, 'direccion': None, 'telefono': None}
    muestras = df.head(15)
    palabras_dir = ['zona', 'calle', 'avenida', 'av', 'cll', 'calzada', 'blvd', 'reformita', 'petapa', 'roosevelt']
    
    puntuacion = {col: {'tel': 0, 'dir': 0, 'wh': 0, 'nom': 0} for col in df.columns}

    for col in df.columns:
        valores = muestras[col].dropna().astype(str).tolist()
        for v in valores:
            v_lower = v.lower().strip()
            if re.search(r'^\+?\d{8,12}$', v_lower.replace('-', '').replace(' ', '')) or (len(v_lower) == 8 and v_lower.isdigit()):
                puntuacion[col]['tel'] += 2
            if any(p in v_lower for p in palabras_dir) or re.search(r'\d+-\d+', v_lower):
                puntuacion[col]['dir'] += 3
            if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s]+$', v) and len(v.split()) >= 2 and not any(p in v_lower for p in palabras_dir):
                puntuacion[col]['nom'] += 2
            if re.search(r'^[a-zA-Z0-9_-]{5,20}$', v) and not any(p in v_lower for p in palabras_dir):
                puntuacion[col]['wh'] += 1

    mapa['telefono'] = max(puntuacion, key=lambda c: puntuacion[c]['tel']) if any(puntuacion[c]['tel'] > 0 for c in puntuacion) else df.columns[3]
    mapa['direccion'] = max(puntuacion, key=lambda c: puntuacion[c]['dir']) if any(puntuacion[c]['dir'] > 0 for c in puntuacion) else df.columns[2]
    
    cols_restantes = [c for c in df.columns if c not in [mapa['telefono'], mapa['direccion']]]
    mapa['nombre'] = cols_restantes[1] if len(cols_restantes) > 1 else cols_restantes[0]
    mapa['warehouse'] = cols_restantes[0]

    return mapa

# ---------------- UI STREAMLIT ----------------
st.sidebar.header("📍 Configuración del Piloto")
c_lat = st.sidebar.number_input("Latitud GPS Piloto", value=st.session_state.gps_piloto['lat'], format="%.6f")
c_lon = st.sidebar.number_input("Longitud GPS Piloto", value=st.session_state.gps_piloto['lon'], format="%.6f")
st.session_state.gps_piloto = {'lat': c_lat, 'lon': c_lon}

tiempo_entrega_min = st.sidebar.slider("Minutos promedio por entrega", min_value=1, max_value=20, value=5)
velocidad_promedio_kmh = st.sidebar.slider("Velocidad promedio (km/h)", min_value=10, max_value=60, value=25)

st.subheader("1. Cargar Archivo de Entregas")
archivo = st.file_uploader("Selecciona archivo Excel (.xlsx, .xls)", type=["xlsx", "xls"])

if archivo:
    df_raw = pd.read_excel(archivo, header=None)
    primer_celda = str(df_raw.iloc[0, 0]).strip().lower()
    tiene_encabezado = not (primer_celda.isdigit() or len(primer_celda) > 8)
    
    if tiene_encabezado:
        df = pd.read_excel(archivo)
    else:
        df = df_raw.copy()
        df.columns = [f"Col_{i+1}" for i in range(len(df.columns))]
        
    mapa = detectar_columnas_inteligente(df)
    
    paquetes = []
    for idx, row in df.iterrows():
        val_wh = str(row[mapa['warehouse']]).strip() if pd.notna(row[mapa['warehouse']]) else ''
        val_dir = str(row[mapa['direccion']]).strip() if pd.notna(row[mapa['direccion']]) else ''
        if not val_wh and not val_dir: continue
        
        paquetes.append({
            'id': idx + 1,
            'warehouse': val_wh,
            'nombre': str(row[mapa['nombre']]).strip(),
            'direccion': val_dir,
            'telefono': str(row[mapa['telefono']]).strip()
        })
    st.session_state.puntos_cargados = paquetes

if st.session_state.puntos_cargados:
    st.write(f"### Se detectaron {len(st.session_state.puntos_cargados)} paquetes")
    st.dataframe(pd.DataFrame(st.session_state.puntos_cargados))

    if st.button("🚀 Calcular Ruta, Métricas y Hora Estimada", type="primary"):
        with st.spinner("Optimizando trayectos y tiempos..."):
            
            puntos_completos = [{
                'id': 0,
                'warehouse': 'INICIO-PILOTO',
                'nombre': 'Ubicación Actual Piloto',
                'direccion': 'Punto de partida (GPS Piloto)',
                'lat': st.session_state.gps_piloto['lat'],
                'lon': st.session_state.gps_piloto['lon']
            }]
            
            for pkt in st.session_state.puntos_cargados:
                lat, lon = geocodificar_rapido(pkt['direccion'])
                puntos_completos.append({
                    'id': pkt['id'],
                    'warehouse': pkt['warehouse'],
                    'nombre': pkt['nombre'],
                    'direccion': pkt['direccion'],
                    'telefono': pkt['telefono'],
                    'lat': lat,
                    'lon': lon
                })
            
            matriz_distancias = None
            try:
                coords = ";".join([f"{p['lon']},{p['lat']}" for p in puntos_completos])
                url = f"{OSRM_URL}/{coords}?annotations=distance"
                resp = requests.get(url, timeout=3).json()
                if 'distances' in resp:
                    matriz_distancias = resp['distances']
            except Exception:
                pass

            if not matriz_distancias:
                matriz_distancias = calcular_matriz_distancias_haversine(puntos_completos)

            manager = pywrapcp.RoutingIndexManager(len(puntos_completos), 1, 0)
            routing = pywrapcp.RoutingModel(manager)
            
            def distance_callback(from_index, to_index):
                return int(matriz_distancias[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])

            transit_callback_index = routing.RegisterTransitCallback(distance_callback)
            routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
            
            search_parameters = pywrapcp.DefaultRoutingSearchParameters()
            search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
            
            solution = routing.SolveWithParameters(search_parameters)
            
            if solution:
                index = routing.Start(0)
                secuencia = []
                distancias_pasos = []
                distancia_total = 0
                
                while not routing.IsEnd(index):
                    node_actual = manager.IndexToNode(index)
                    secuencia.append(node_actual)
                    next_index = solution.Value(routing.NextVar(index))
                    
                    if not routing.IsEnd(next_index):
                        node_siguiente = manager.IndexToNode(next_index)
                        dist_tramo = matriz_distancias[node_actual][node_siguiente]
                        distancias_pasos.append(dist_tramo)
                        distancia_total += dist_tramo
                    
                    index = next_index
                
                st.session_state.secuencia_optima = secuencia
                st.session_state.puntos_ruta = puntos_completos
                st.session_state.distancias_pasos = distancias_pasos
                st.session_state.distancia_total_m = distancia_total
                st.rerun()

# --- MOSTRAR RESULTADOS Y NAVEGACIÓN ---
if st.session_state.secuencia_optima and st.session_state.puntos_ruta:
    st.markdown("---")
    
    # --- CÁLCULOS DE MÉTRICAS GENERALES ---
    dist_total_km = st.session_state.distancia_total_m / 1000.0
    num_paquetes = len(st.session_state.secuencia_optima) - 1
    
    # Tiempo de manejo (horas = km / km/h)
    horas_manejo = dist_total_km / velocidad_promedio_kmh
    minutos_manejo = horas_manejo * 60
    
    # Tiempo total de entregas (minutos por paquete)
    minutos_entregas = num_paquetes * tiempo_entrega_min
    minutos_totales = minutos_manejo + minutos_entregas
    
    hora_actual = datetime.now()
    hora_estimada_fin = hora_actual + timedelta(minutes=minutos_totales)
    
    # Tarjetas de Resumen
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Recorrido Total", f"{dist_total_km:.2f} km", f"{st.session_state.distancia_total_m:,.0f} m")
    m2.metric("Total Paquetes", f"{num_paquetes}")
    m3.metric("Tiempo Estimado", f"{int(minutos_totales // 60)}h {int(minutos_totales % 60)}m")
    m4.metric("Hora Estimada de Fin", hora_estimada_fin.strftime("%I:%M %p"))

    st.markdown("---")
    
    col_lista, col_mapa = st.columns([1, 1])
    
    with col_lista:
        st.subheader("📋 Orden de Entregas y Distancias por Paquete")
        for paso, idx in enumerate(st.session_state.secuencia_optima):
            pt = st.session_state.puntos_ruta[idx]
            
            if paso == 0:
                st.markdown(f"🚩 **Inicio (GPS Piloto):** Lat {pt['lat']}, Lon {pt['lon']}")
                if len(st.session_state.distancias_pasos) > 0:
                    d_m = st.session_state.distancias_pasos[0]
                    st.caption(f"➡️ Distancia a la primera entrega: **{d_m/1000:.2f} km** ({d_m:,.0f} metros)")
                st.markdown("---")
            else:
                gmaps_url = f"https://www.google.com/maps/search/?api=1&query={pt['lat']},{pt['lon']}"
                st.markdown(f"**Parada {paso:02d}:** [{pt['warehouse']}] **{pt['nombre']}**")
                st.markdown(f"📍 {pt['direccion']} | 📞 {pt['telefono']}")
                
                # Distancia hacia el siguiente paquete (si aplica)
                if paso < len(st.session_state.distancias_pasos):
                    d_next_m = st.session_state.distancias_pasos[paso]
                    st.info(f"📏 Distancia hacia el siguiente paquete: **{d_next_m/1000:.2f} km** ({d_next_m:,.0f} m)")
                
                st.markdown(f"[🗺️ Navegar en Google Maps]({gmaps_url})")
                st.markdown("---")
                
    with col_mapa:
        st.subheader("🗺️ Mapa de la Ruta")
        puntos = st.session_state.puntos_ruta
        m = folium.Map(location=[puntos[0]['lat'], puntos[0]['lon']], zoom_start=13)
        
        coords_ruta = []
        for paso, idx in enumerate(st.session_state.secuencia_optima):
            pt = puntos[idx]
            coords_ruta.append([pt['lat'], pt['lon']])
            color = "green" if paso == 0 else "blue"
            folium.Marker(
                [pt['lat'], pt['lon']], 
                popup=f"Parada {paso}: {pt['nombre']}<br>{pt['direccion']}",
                tooltip=f"Parada {paso}: [{pt['warehouse']}]",
                icon=folium.Icon(color=color, icon="info-sign")
            ).add_to(m)
            
        folium.PolyLine(coords_ruta, color="red", weight=3, opacity=0.8).add_to(m)
        st_folium(m, width=600, height=500, key="mapa_rutas_metricas")