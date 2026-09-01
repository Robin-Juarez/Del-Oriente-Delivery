import streamlit as st
import pandas as pd
import requests
import folium
import pdfplumber
import re
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# Configuración de la página
st.set_page_config(page_title="Del Oriente Delivery", layout="wide")
st.title("🚚 Del Oriente Delivery - Panel de Rutas e Importación")

OSRM_URL = "http://localhost:5000/table/v1/driving"

# Inicialización de variables de sesión
if 'puntos_cargados' not in st.session_state:
    st.session_state.puntos_cargados = []
if 'secuencia_optima' not in st.session_state:
    st.session_state.secuencia_optima = None
if 'puntos_ruta' not in st.session_state:
    st.session_state.puntos_ruta = None

# Geocodificador para Guatemala
geolocator = Nominatim(user_agent="del_oriente_delivery_app")
geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1)

def geocodificar_direccion(direccion):
    """Convierte una dirección a coordenadas Lat/Lon"""
    query = f"{direccion}, Guatemala"
    try:
        location = geocode(query)
        if location:
            return location.latitude, location.longitude
        return 14.634915, -90.506882 # Coordenadas por defecto (Guatemala Centro)
    except Exception:
        return 14.634915, -90.506882

# --- MOTOR DE DETECCIÓN INTELIGENTE UNIFICADO (4 CAMPOS) ---
def detectar_columnas_inteligente(df):
    mapa = {'warehouse': None, 'nombre': None, 'direccion': None, 'telefono': None}
    
    # 1. Detección por encabezados
    cols_str = [str(c).strip().lower() for c in df.columns]
    for idx, col_name in enumerate(cols_str):
        if any(k in col_name for k in ['warehouse', 'wh', 'codigo', 'código', 'tracking', 'guia', 'guía', 'pedido', 'id']):
            mapa['warehouse'] = df.columns[idx]
        elif any(k in col_name for k in ['nombre', 'cliente', 'destinatario', 'recibe']):
            mapa['nombre'] = df.columns[idx]
        elif any(k in col_name for k in ['direccion', 'dirección', 'dir', 'destino', 'ubicacion', 'ubicación']):
            mapa['direccion'] = df.columns[idx]
        elif any(k in col_name for k in ['telefono', 'teléfono', 'tel', 'cel', 'celular', 'contacto']):
            mapa['telefono'] = df.columns[idx]

    # 2. Detección por inspección de contenido
    muestras = df.head(15)
    palabras_direccion = ['zona', 'calle', 'avenida', 'av', 'av.', 'cll', 'calzada', 'blvd', 'bulevar', 'diagonal', 'ruta', 'km', 'reformita', 'petapa', 'roosevelt', 'casa', 'lote', 'sector', 'barrio', 'colonia', 'col']

    puntuacion = {col: {'tel': 0, 'dir': 0, 'wh': 0, 'nom': 0} for col in df.columns}

    for col in df.columns:
        valores = muestras[col].dropna().astype(str).tolist()
        for v in valores:
            v_lower = v.lower().strip()
            
            # Teléfono (8 a 12 dígitos)
            if re.search(r'^\+?\d{8,12}$', v_lower.replace('-', '').replace(' ', '')) or (len(v_lower) == 8 and v_lower.isdigit()):
                puntuacion[col]['tel'] += 2
            # Dirección
            if any(p in v_lower for p in palabras_direccion) or re.search(r'\d+-\d+', v_lower):
                puntuacion[col]['dir'] += 3
            # Nombres de persona
            if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s]+$', v) and len(v.split()) >= 2 and not any(p in v_lower for p in palabras_direccion):
                puntuacion[col]['nom'] += 2
            # Warehouse / Código único
            if re.search(r'^[a-zA-Z0-9_-]{5,20}$', v) and not any(p in v_lower for p in palabras_direccion):
                puntuacion[col]['wh'] += 1

    if not mapa['telefono']:
        mapa['telefono'] = max(puntuacion, key=lambda c: puntuacion[c]['tel']) if any(puntuacion[c]['tel'] > 0 for c in puntuacion) else None
    if not mapa['direccion']:
        mapa['direccion'] = max(puntuacion, key=lambda c: puntuacion[c]['dir']) if any(puntuacion[c]['dir'] > 0 for c in puntuacion) else None
    if not mapa['nombre']:
        cols_restantes = [c for c in df.columns if c not in [mapa['telefono'], mapa['direccion']]]
        if cols_restantes:
            mapa['nombre'] = max(cols_restantes, key=lambda c: puntuacion[c]['nom'])
    if not mapa['warehouse']:
        cols_restantes = [c for c in df.columns if c not in [mapa['telefono'], mapa['direccion'], mapa['nombre']]]
        if cols_restantes:
            mapa['warehouse'] = cols_restantes[0]
        else:
            mapa['warehouse'] = df.columns[0]

    return mapa

# --- FUNCIÓN DE PROCESAMIENTO UNIFICADO ---
def procesar_archivo_unificado(file):
    df_raw = None

    if file.name.endswith(('.xlsx', '.xls')):
        df_temp = pd.read_excel(file, header=None)
        primer_celda = str(df_temp.iloc[0, 0]).strip().lower()
        tiene_encabezado = not (primer_celda.isdigit() or len(primer_celda) > 8)
        
        if tiene_encabezado:
            df_raw = pd.read_excel(file)
        else:
            df_raw = df_temp.copy()
            df_raw.columns = [f"Columna_{i+1}" for i in range(len(df_raw.columns))]

    elif file.name.endswith('.pdf'):
        filas_pdf = []
        with pdfplumber.open(file) as pdf:
            for page in pdf.pages:
                tablas = page.extract_tables()
                for tabla in tablas:
                    for fila in tabla:
                        if any(fila):
                            filas_pdf.append([str(celda).strip() if celda else '' for celda in fila])
        
        if filas_pdf:
            df_temp = pd.DataFrame(filas_pdf)
            primer_celda = str(df_temp.iloc[0, 0]).strip().lower()
            tiene_encabezado = any(k in primer_celda for k in ['warehouse', 'codigo', 'nombre', 'direccion'])
            
            if tiene_encabezado:
                df_raw = pd.DataFrame(filas_pdf[1:], columns=filas_pdf[0])
            else:
                df_raw = df_temp
                df_raw.columns = [f"Columna_{i+1}" for i in range(len(df_raw.columns))]

    if df_raw is None or df_raw.empty:
        return []

    mapa = detectar_columnas_inteligente(df_raw)
    
    paquetes = []
    for idx, row in df_raw.iterrows():
        val_wh = str(row[mapa['warehouse']]).strip() if pd.notna(row[mapa['warehouse']]) else ''
        val_dir = str(row[mapa['direccion']]).strip() if mapa['direccion'] and pd.notna(row[mapa['direccion']]) else ''
        
        if not val_wh and not val_dir:
            continue
            
        paquetes.append({
            'id': len(paquetes) + 1,
            'warehouse': val_wh if val_wh else f"PKG-{len(paquetes)+1:03d}",
            'nombre': str(row[mapa['nombre']]).strip() if mapa['nombre'] and pd.notna(row[mapa['nombre']]) else 'Cliente',
            'direccion': val_dir,
            'telefono': str(row[mapa['telefono']]).strip() if mapa['telefono'] and pd.notna(row[mapa['telefono']]) else ''
        })
        
    return paquetes

# ----------------- INTERFAZ DE USUARIO (STREAMLIT) -----------------
st.subheader("1. Cargar Archivo de Entregas (Excel o PDF)")
uploaded_file = st.file_uploader("Selecciona un archivo .xlsx, .xls o .pdf", type=["xlsx", "xls", "pdf"])

if uploaded_file is not None:
    if st.button("📥 Procesar Archivo Cargado"):
        with st.spinner("Leyendo y detectando columnas automáticamente..."):
            paquetes = procesar_archivo_unificado(uploaded_file)
            st.session_state.puntos_cargados = paquetes
            st.success(f"¡Listo! Se reconocieron e importaron {len(paquetes)} paquetes.")

# Vista previa de datos cargados
if st.session_state.puntos_cargados:
    st.write("### Vista previa de paquetes procesados:")
    st.dataframe(pd.DataFrame(st.session_state.puntos_cargados))
    
    # Botón de Optimización
    if st.button("🚀 Geocodificar y Optimizar Ruta", type="primary"):
        with st.spinner("Geocodificando direcciones y calculando secuencia óptima con OSRM..."):
            
            bodega = {
                'id': 0,
                'warehouse': 'BODEGA-CENTRAL',
                'nombre': 'Bodega Central',
                'direccion': 'Bodega Central - Punto de Salida',
                'lat': 14.5950,
                'lon': -90.5120
            }
            
            puntos = [bodega]
            
            for pkt in st.session_state.puntos_cargados:
                lat, lon = geocodificar_direccion(pkt['direccion'])
                puntos.append({
                    'id': pkt['id'],
                    'warehouse': pkt['warehouse'],
                    'nombre': pkt['nombre'],
                    'direccion': f"{pkt['direccion']} (Tel: {pkt['telefono']})",
                    'lat': lat,
                    'lon': lon
                })
            
            coords = ";".join([f"{p['lon']},{p['lat']}" for p in puntos])
            url = f"{OSRM_URL}/{coords}?annotations=distance,duration"
            
            resp = requests.get(url).json()
            
            if 'distances' in resp:
                matriz_distancias = resp['distances']
                
                manager = pywrapcp.RoutingIndexManager(len(puntos), 1, 0)
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
                    while not routing.IsEnd(index):
                        secuencia.append(manager.IndexToNode(index))
                        index = solution.Value(routing.NextVar(index))
                    
                    st.session_state.secuencia_optima = secuencia
                    st.session_state.puntos_ruta = puntos
            else:
                st.error("Error consultando el servidor OSRM.")

# Resultados: Hoja de Ruta y Mapa
if st.session_state.secuencia_optima and st.session_state.puntos_ruta:
    st.markdown("---")
    st.success("¡Ruta optimizada generada exitosamente!")
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.subheader("Hoja de Ruta de Entregas")
        for paso, idx in enumerate(st.session_state.secuencia_optima):
            pt = st.session_state.puntos_ruta[idx]
            if paso == 0:
                st.markdown(f"** Salida:** {pt['direccion']}")
            else:
                st.markdown(f"**Parada {paso:02d}:** **[{pt['warehouse']}]** - {pt['nombre']} | {pt['direccion']}")
    
    with col2:
        st.subheader("Mapa Interactivo")
        puntos = st.session_state.puntos_ruta
        m = folium.Map(location=[puntos[0]['lat'], puntos[0]['lon']], zoom_start=12)
        
        for paso, idx in enumerate(st.session_state.secuencia_optima):
            pt = puntos[idx]
            color = "red" if paso == 0 else "blue"
            folium.Marker(
                [pt['lat'], pt['lon']], 
                popup=f"Parada {paso}: [{pt['warehouse']}] {pt['nombre']}<br>{pt['direccion']}", 
                icon=folium.Icon(color=color)
            ).add_to(m)
            
        st_folium(m, width=700, height=450, key="mapa_final_sin_dep")