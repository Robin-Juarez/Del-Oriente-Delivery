import streamlit as st
import pandas as pd
import requests
import folium
import re
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import urllib.parse

st.set_page_config(page_title="App Piloto - Ruta Exacta", layout="wide")

st.title("🚚 Del Oriente Delivery - Navegación y Rutas Exactas")

# --- SERVIDORES Y SERVICIOS ---
OSRM_URL = "http://router.project-osrm.org/table/v1/driving" # Servidor público OSRM por si local falla

# Geocodificador Nominatim optimizado para Guatemala
geolocator = Nominatim(user_agent="del_oriente_delivery_v2")
geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.2)

# --- ESTADOS DE SESIÓN ---
if 'puntos_cargados' not in st.session_state:
    st.session_state.puntos_cargados = []
if 'secuencia_optima' not in st.session_state:
    st.session_state.secuencia_optima = None
if 'puntos_ruta' not in st.session_state:
    st.session_state.puntos_ruta = None
if 'gps_piloto' not in st.session_state:
    st.session_state.gps_piloto = {'lat': 14.5950, 'lon': -90.5120} # Ubicación inicial por defecto

# --- MEJORA DE GEOCODIFICACIÓN DE DIRECCIONES EN GUATEMALA ---
def limpiar_y_geocodificar(direccion_raw):
    """
    Limpia y estructura la dirección para garantizar que Nominatim u OpenStreetMap
    ubiquen el municipio y zona correcta sin saltar a Zona 10.
    """
    dir_clean = str(direccion_raw).strip()
    
    # Extraer Zona si existe (ej. Zona 12)
    zona_match = re.search(r'zona\s*(\d+)', dir_clean, re.IGNORECASE)
    zona_str = f"Zona {zona_match.group(1)}" if zona_match else ""
    
    # Armar búsquedas estructuradas progresivas
    opciones_busqueda = [
        f"{dir_clean}, Guatemala City, Guatemala",
        f"{dir_clean}, Guatemala",
        f"{zona_str}, Guatemala City, Guatemala" if zona_str else f"{dir_clean}"
    ]
    
    for query in opciones_busqueda:
        try:
            location = geocode(query)
            if location:
                return location.latitude, location.longitude
        except Exception:
            continue
            
    # Coordenadas por defecto (Obelisco / Centro) si falla totalmente
    return 14.5950, -90.5120 

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
st.sidebar.header("📍 GPS del Piloto")
c_lat = st.sidebar.number_input("Latitud GPS Piloto", value=st.session_state.gps_piloto['lat'], format="%.6f")
c_lon = st.sidebar.number_input("Longitud GPS Piloto", value=st.session_state.gps_piloto['lon'], format="%.6f")
st.session_state.gps_piloto = {'lat': c_lat, 'lon': c_lon}

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

    if st.button("🚀 Optimizar Ruta Desde Ubicación Actual del Piloto", type="primary"):
        with st.spinner("Buscando coordenadas exactas de cada dirección y calculando ruta..."):
            
            # Punto 0 = UBICACIÓN ACTUAL DEL PILOTO (GPS)
            puntos_completos = [{
                'id': 0,
                'warehouse': 'INICIO-PILOTO',
                'nombre': 'Ubicación Actual Piloto',
                'direccion': 'Punto de partida (GPS Piloto)',
                'lat': st.session_state.gps_piloto['lat'],
                'lon': st.session_state.gps_piloto['lon']
            }]
            
            # Geocodificar cada paquete
            for pkt in st.session_state.puntos_cargados:
                lat, lon = limpiar_y_geocodificar(pkt['direccion'])
                puntos_completos.append({
                    'id': pkt['id'],
                    'warehouse': pkt['warehouse'],
                    'nombre': pkt['nombre'],
                    'direccion': pkt['direccion'],
                    'telefono': pkt['telefono'],
                    'lat': lat,
                    'lon': lon
                })
            
            # Matriz de distancias vía OSRM
            coords = ";".join([f"{p['lon']},{p['lat']}" for p in puntos_completos])
            url = f"{OSRM_URL}/{coords}?annotations=distance"
            
            try:
                resp = requests.get(url, timeout=10).json()
                if 'distances' in resp:
                    matriz_distancias = resp['distances']
                    
                    # Solvedor OR-Tools para TSP / VRPTW
                    manager = pywrapcp.RoutingIndexManager(len(puntos_completos), 1, 0)
                    routing = pywrapcp.RoutingModel(manager)
                    
                    def distance_callback(from_index, to_index):
                        from_node = manager.IndexToNode(from_index)
                        to_node = manager.IndexToNode(to_index)
                        return int(matriz_distancias[from_node][to_node])

                    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
                    
                    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
                    
                    solution = routing.SolveWithParameters(search_parameters)
                    
                    if solution:
                        index = routing.Start(0)
                        secuencia = []
                        while not routing.IsEnd(index):
                            secuencia.append(manager.IndexToNode(index))
                            index = solution.Value(routing.NextVar(index))
                        
                        st.session_state.secuencia_optima = secuencia
                        st.session_state.puntos_ruta = puntos_completos
                else:
                    st.error("No se pudo obtener la matriz de carreteras OSRM.")
            except Exception as e:
                st.error(f"Error procesando optimización: {e}")

# --- MOSTRAR RESULTADOS Y NAVEGACIÓN ---
if st.session_state.secuencia_optima and st.session_state.puntos_ruta:
    st.markdown("---")
    st.success("¡Ruta ordenada exactamente según distancia en calle!")
    
    col_lista, col_mapa = st.columns([1, 1])
    
    with col_lista:
        st.subheader("📋 Orden de Entregas")
        for paso, idx in enumerate(st.session_state.secuencia_optima):
            pt = st.session_state.puntos_ruta[idx]
            if paso == 0:
                st.markdown(f"🚩 **Inicio (GPS Piloto):** Lat {pt['lat']}, Lon {pt['lon']}")
            else:
                gmaps_url = f"https://www.google.com/maps/search/?api=1&query={pt['lat']},{pt['lon']}"
                st.markdown(f"**Parada {paso:02d}:** [{pt['warehouse']}] **{pt['nombre']}**")
                st.markdown(f"📍 {pt['direccion']} | 📞 {pt['telefono']}")
                st.markdown(f"[🗺️ Navegar en Google Maps]({gmaps_url})")
                st.markdown("---")
                
    with col_mapa:
        st.subheader("🗺️ Mapa de la Ruta")
        puntos = st.session_state.puntos_ruta
        m = folium.Map(location=[puntos[0]['lat'], puntos[0]['lon']], zoom_start=13)
        
        # Dibujar puntos y líneas de recorrido
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
        st_folium(m, width=600, height=500, key="mapa_rutas_correctas")