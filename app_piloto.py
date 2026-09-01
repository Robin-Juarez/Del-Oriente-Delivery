import streamlit as st
import pandas as pd
import requests
import folium
import re
import math
from datetime import datetime, timedelta
import zoneinfo
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import streamlit.components.v1 as components

st.set_page_config(page_title="App Piloto - Guatemala Delivery", layout="wide")

# --- ZONA HORARIA GUATEMALA ---
TZ_GT = zoneinfo.ZoneInfo("America/Guatemala")
hora_gt_actual = datetime.now(TZ_GT)

st.title("🚚 Del Oriente Delivery - Control de Piloto")

# Reloj en tiempo real con la hora local de Guatemala
st.markdown(f"### 🕒 Hora local en Guatemala: **{hora_gt_actual.strftime('%I:%M:%S %p')}**")

OSRM_URL = "http://router.project-osrm.org/table/v1/driving"
geolocator = Nominatim(user_agent="del_oriente_delivery_gt_v5", timeout=3)

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
if 'estados_paquetes' not in st.session_state:
    st.session_state.estados_paquetes = {}
if 'gps_piloto' not in st.session_state:
    st.session_state.gps_piloto = {'lat': 14.5950, 'lon': -90.5120}

# --- GEOLOCALIZACIÓN DEL DISPOSITIVO MÓVIL (HTML5/JS) ---
st.sidebar.header("📍 GPS del Dispositivo")

gps_code = """
<script>
navigator.geolocation.getCurrentPosition(
    (position) => {
        const lat = position.coords.latitude;
        const lon = position.coords.longitude;
        window.parent.postMessage({
            type: 'streamlit:setComponentValue',
            value: {lat: lat, lon: lon}
        }, '*');
    },
    (err) => { console.log("GPS no disponible o permiso denegado"); }
);
</script>
"""
coords_js = components.html(gps_code, height=0)

c_lat = st.sidebar.number_input("Latitud Real GPS", value=st.session_state.gps_piloto['lat'], format="%.6f")
c_lon = st.sidebar.number_input("Longitud Real GPS", value=st.session_state.gps_piloto['lon'], format="%.6f")
st.session_state.gps_piloto = {'lat': c_lat, 'lon': c_lon}

tiempo_entrega_min = st.sidebar.slider("Minutos promedio por entrega", min_value=1, max_value=20, value=5)
velocidad_promedio_kmh = st.sidebar.slider("Velocidad promedio (km/h)", min_value=10, max_value=60, value=25)

# --- FORMATEADOR DE TELÉFONOS DE GUATEMALA (+502 Y 8 DÍGITOS) ---
def formatear_telefono_gt(telefono_raw):
    digitos = re.sub(r'\D', '', str(telefono_raw))
    
    if digitos.startswith('502') and len(digitos) > 8:
        digitos = digitos[3:]
        
    if len(digitos) >= 8:
        d8 = digitos[-8:]
        return f"+502 {d8[:4]}-{d8[4:]}", f"+502{d8}"
    
    return str(telefono_raw), re.sub(r'\D', '', str(telefono_raw))

# --- GEOCODIFICACIÓN CON CACHÉ ---
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
        R = 6371000
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
        
        tel_fmt, tel_clean = formatear_telefono_gt(row[mapa['telefono']])
        
        paquetes.append({
            'id': idx + 1,
            'warehouse': val_wh,
            'nombre': str(row[mapa['nombre']]).strip(),
            'direccion': val_dir,
            'telefono_fmt': tel_fmt,
            'telefono_clean': tel_clean
        })
    st.session_state.puntos_cargados = paquetes

if st.session_state.puntos_cargados:
    st.write(f"### Se detectaron {len(st.session_state.puntos_cargados)} paquetes")
    st.dataframe(pd.DataFrame(st.session_state.puntos_cargados))

    if st.button("🚀 Calcular Ruta Exacta y Hora Estimada", type="primary"):
        with st.spinner("Procesando GPS de piloto y ruta exacta..."):
            
            puntos_completos = [{
                'id': 0,
                'warehouse': 'INICIO-PILOTO',
                'nombre': 'Ubicación Actual Piloto',
                'direccion': 'Punto de partida (GPS Piloto)',
                'telefono_fmt': 'N/A',
                'telefono_clean': '',
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
                    'telefono_fmt': pkt['telefono_fmt'],
                    'telefono_clean': pkt['telefono_clean'],
                    'lat': lat,
                    'lon': lon
                })
                if pkt['id'] not in st.session_state.estados_paquetes:
                    st.session_state.estados_paquetes[pkt['id']] = "Pendiente ⏳"
            
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

# --- MOSTRAR RESULTADOS Y CONTROL DE PILOTO ---
if st.session_state.secuencia_optima and st.session_state.puntos_ruta:
    st.markdown("---")
    
    dist_total_km = st.session_state.distancia_total_m / 1000.0
    num_paquetes = len(st.session_state.secuencia_optima) - 1
    
    horas_manejo = dist_total_km / velocidad_promedio_kmh
    minutos_manejo = horas_manejo * 60
    minutos_entregas = num_paquetes * tiempo_entrega_min
    minutos_totales = minutos_manejo + minutos_entregas
    
    hora_estimada_fin = datetime.now(TZ_GT) + timedelta(minutes=minutos_totales)
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Recorrido Total", f"{dist_total_km:.2f} km", f"{st.session_state.distancia_total_m:,.0f} m")
    m2.metric("Total Paquetes", f"{num_paquetes}")
    m3.metric("Tiempo Estimado Total", f"{int(minutos_totales // 60)}h {int(minutos_totales % 60)}m")
    m4.metric("Hora Est. de Fin (GT)", hora_estimada_fin.strftime("%I:%M %p"))

    st.markdown("---")
    
    tab_activa, tab_historial = st.tabs(["🚚 Ruta Activa", "📋 Historial y Reportes"])
    
    with tab_activa:
        col_lista, col_mapa = st.columns([1, 1])
        
        with col_lista:
            st.subheader("📋 Entregas Pendientes")
            
            paquetes_pendientes = 0
            
            for paso, idx in enumerate(st.session_state.secuencia_optima):
                pt = st.session_state.puntos_ruta[idx]
                
                if paso == 0:
                    st.markdown(f"🚩 **Inicio (GPS Piloto):** Lat {pt['lat']}, Lon {pt['lon']}")
                    if len(st.session_state.distancias_pasos) > 0:
                        d_m = st.session_state.distancias_pasos[0]
                        st.caption(f"➡️ Distancia a primera entrega: **{d_m/1000:.2f} km** ({d_m:,.0f} m)")
                    st.markdown("---")
                else:
                    pkt_id = pt['id']
                    estado_actual = st.session_state.estados_paquetes.get(pkt_id, "Pendiente ⏳")
                    
                    if estado_actual == "Pendiente ⏳":
                        paquetes_pendientes += 1
                        gmaps_url = f"https://www.google.com/maps/search/?api=1&query={pt['lat']},{pt['lon']}"
                        
                        st.markdown(f"**Parada {paso:02d}:** [{pt['warehouse']}] **{pt['nombre']}**")
                        st.markdown(f"📍 {pt['direccion']}")
                        st.markdown(f"📞 Teléfono: **{pt['telefono_fmt']}**")
                        
                        st.markdown(f'<a href="tel:{pt["telefono_clean"]}" style="text-decoration:none;"><button style="background-color:#25D366;color:white;border:none;padding:6px 14px;border-radius:5px;cursor:pointer;font-weight:bold;">📞 Llamar al Cliente ({pt["telefono_fmt"]})</button></a>', unsafe_allow_html=True)
                        
                        b1, b2, b3 = st.columns(3)
                        if b1.button("✅ Entregado", key=f"ent_{pkt_id}"):
                            st.session_state.estados_paquetes[pkt_id] = "Entregado ✅"
                            st.rerun()
                        if b2.button("👤 Ausente", key=f"aus_{pkt_id}"):
                            st.session_state.estados_paquetes[pkt_id] = "Ausente 👤"
                            st.rerun()
                        if b3.button("❌ No Entregado", key=f"noe_{pkt_id}"):
                            st.session_state.estados_paquetes[pkt_id] = "No Entregado ❌"
                            st.rerun()
                        
                        if paso < len(st.session_state.distancias_pasos):
                            d_next_m = st.session_state.distancias_pasos[paso]
                            st.info(f"📏 Distancia al siguiente paquete: **{d_next_m/1000:.2f} km** ({d_next_m:,.0f} m)")
                        
                        st.markdown(f"[🗺️ Navegar en Google Maps]({gmaps_url})")
                        st.markdown("---")
            
            if paquetes_pendientes == 0 and num_paquetes > 0:
                st.balloons()
                st.success("🎉 ¡Felicidades! Has completado la gestión de todas las entregas de la ruta.")

        with col_mapa:
            st.subheader("🗺️ Mapa de la Ruta")
            puntos = st.session_state.puntos_ruta
            m = folium.Map(location=[puntos[0]['lat'], puntos[0]['lon']], zoom_start=13)
            
            coords_ruta = []
            for paso, idx in enumerate(st.session_state.secuencia_optima):
                pt = puntos[idx]
                coords_ruta.append([pt['lat'], pt['lon']])
                
                # Obtención segura de datos para la ventana emergente (popup)
                nombre_pt = pt.get('nombre', 'Punto de Inicio')
                tel_pt = pt.get('telefono_fmt', 'N/A')
                pkt_id_pt = pt.get('id', 0)
                
                if paso == 0:
                    color = "green"
                    est_str = "Inicio Piloto"
                else:
                    est_str = st.session_state.estados_paquetes.get(pkt_id_pt, "Pendiente ⏳")
                    if "Entregado ✅" in est_str:
                        color = "blue"
                    elif "Ausente 👤" in est_str:
                        color = "orange"
                    elif "No Entregado ❌" in est_str:
                        color = "red"
                    else:
                        color = "purple"

                folium.Marker(
                    [pt['lat'], pt['lon']], 
                    popup=f"Parada {paso}: {nombre_pt}<br>Tel: {tel_pt}<br>Estado: {est_str}",
                    tooltip=f"Parada {paso}: [{pt.get('warehouse', 'INICIO')}]",
                    icon=folium.Icon(color=color, icon="info-sign")
                ).add_to(m)
                
            folium.PolyLine(coords_ruta, color="red", weight=3, opacity=0.8).add_to(m)
            st_folium(m, width=600, height=500, key="mapa_rutas_estados_gt")

    # --- PESTAÑA HISTORIAL Y REPORTE ---
    with tab_historial:
        st.subheader("📋 Registro e Historial de Paquetes Procesados")
        
        historial_datos = []
        for idx in st.session_state.secuencia_optima:
            pt = st.session_state.puntos_ruta[idx]
            if pt.get('id', 0) != 0:
                est = st.session_state.estados_paquetes.get(pt['id'], "Pendiente ⏳")
                historial_datos.append({
                    'ID Paquete': pt.get('warehouse', ''),
                    'Cliente': pt.get('nombre', ''),
                    'Dirección': pt.get('direccion', ''),
                    'Teléfono': pt.get('telefono_fmt', 'N/A'),
                    'Estado Final': est
                })
        
        df_historial = pd.DataFrame(historial_datos)
        st.dataframe(df_historial, use_container_width=True)
        
        st.markdown("#### 🔄 Cambiar estado o reactivar pedido")
        lista_opciones = [p['warehouse'] for p in st.session_state.puntos_cargados if 'warehouse' in p]
        
        if lista_opciones:
            col_sel, col_est = st.columns([2, 1])
            with col_sel:
                pkt_reactivar = st.selectbox("Selecciona un paquete para modificar su estado:", options=lista_opciones)
            with col_est:
                nuevo_est = st.selectbox("Nuevo Estado:", ["Pendiente ⏳", "Entregado ✅", "Ausente 👤", "No Entregado ❌"])
                if st.button("Actualizar Estado"):
                    for p in st.session_state.puntos_cargados:
                        if p['warehouse'] == pkt_reactivar:
                            st.session_state.estados_paquetes[p['id']] = nuevo_est
                            st.success(f"Estado de {pkt_reactivar} actualizado a {nuevo_est}")
                            st.rerun()