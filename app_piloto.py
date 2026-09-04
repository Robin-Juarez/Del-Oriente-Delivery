import streamlit as st
import pandas as pd
import requests
import folium
import re
import math
import hashlib
import io
import concurrent.futures
import urllib.parse
import pdfplumber
from datetime import datetime, timedelta
import zoneinfo
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import streamlit.components.v1 as components

st.set_page_config(page_title="App Piloto - Guatemala Delivery", layout="wide")

# --- ZONA HORARIA GUATEMALA ---
TZ_GT = zoneinfo.ZoneInfo("America/Guatemala")
hora_gt_actual = datetime.now(TZ_GT)

st.title("🚚 Del Oriente Delivery - Control de Piloto")
st.markdown(f"### 🕒 Hora local en Guatemala: **{hora_gt_actual.strftime('%I:%M:%S %p')}**")

OSRM_URL = "http://router.project-osrm.org/table/v1/driving"
geolocator = Nominatim(user_agent="del_oriente_delivery_gt_v5", timeout=5)
# BUG FIX #6: Nominatim exige máx. 1 solicitud/seg. Sin este limitador, un archivo
# con muchos paquetes puede hacer que el servicio bloquee temporalmente la IP y
# todas las geocodificaciones empiecen a fallar silenciosamente a la mitad del proceso.
geocode_rl = RateLimiter(geolocator.geocode, min_delay_seconds=1.0, max_retries=1, error_wait_seconds=2.0)

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
if 'archivo_hash' not in st.session_state:
    st.session_state.archivo_hash = None
if 'direcciones_fallidas' not in st.session_state:
    st.session_state.direcciones_fallidas = []

# --- BUG FIX #1: GEOLOCALIZACIÓN REAL DEL DISPOSITIVO ---
# El código original usaba postMessage hacia window.parent, pero components.html()
# no tiene un canal de retorno hacia Python: ese mensaje se perdía en el vacío y
# los campos de Latitud/Longitud NUNCA se actualizaban solos. El piloto siempre
# --- BUG FIX #1 (mejorado): GEOLOCALIZACIÓN AUTOMÁTICA DEL DISPOSITIVO ---
# Ahora se pide la ubicación real SOLA al cargar la página (sin que el piloto
# tenga que tocar nada), y solo la primera vez por sesión: si la URL ya trae
# lat/lon (porque ya se detectó antes), no se vuelve a disparar el GPS solo,
# para no interrumpir ni recargar la página de nuevo sin necesidad. El botón
# queda disponible por si el piloto se movió y quiere actualizar su posición
# a mano.
st.sidebar.header("📍 GPS del Dispositivo")

qp = st.query_params
if "lat" in qp and "lon" in qp:
    try:
        st.session_state.gps_piloto = {'lat': float(qp["lat"]), 'lon': float(qp["lon"])}
    except (ValueError, TypeError):
        pass

gps_html = """
<div>
  <button onclick="obtenerGPS()"
    style="padding:8px 14px;border-radius:6px;background:#25D366;color:white;
    border:none;cursor:pointer;font-weight:bold;width:100%;">
    📍 Actualizar mi ubicación GPS
  </button>
  <p id="estado_gps" style="font-size:12px;color:gray;margin-top:6px;"></p>
</div>
<script>
function obtenerGPS() {
  document.getElementById('estado_gps').innerText = 'Obteniendo ubicación...';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const lat = pos.coords.latitude;
      const lon = pos.coords.longitude;
      const url = new URL(window.parent.location.href);
      url.searchParams.set('lat', lat);
      url.searchParams.set('lon', lon);
      window.parent.location.href = url.toString();
    },
    (err) => {
      document.getElementById('estado_gps').innerText = 'GPS no disponible: ' + err.message;
    }
  );
}

// Disparo automático: solo si la URL todavía NO trae lat/lon (primera carga
// de la sesión). Así el piloto no tiene que hacer nada para que su ubicación
// real se use como punto de partida.
(function() {
  const params = new URL(window.parent.location.href).searchParams;
  if (!params.has('lat') || !params.has('lon')) {
    obtenerGPS();
  } else {
    document.getElementById('estado_gps').innerText = 'Ubicación detectada ✅';
  }
})();
</script>
"""
components.html(gps_html, height=70)

c_lat = st.sidebar.number_input("Latitud Real GPS", value=st.session_state.gps_piloto['lat'], format="%.6f")
c_lon = st.sidebar.number_input("Longitud Real GPS", value=st.session_state.gps_piloto['lon'], format="%.6f")
st.session_state.gps_piloto = {'lat': c_lat, 'lon': c_lon}

tiempo_entrega_min = st.sidebar.slider("Minutos promedio por entrega", min_value=1, max_value=20, value=5)
velocidad_promedio_kmh = st.sidebar.slider("Velocidad promedio (km/h)", min_value=10, max_value=60, value=25)

# --- AGRUPAR POR ZONA PARA UNA RUTA MÁS ÁGIL ---
# Aunque el pin en el mapa no caiga exacto en la fachada, sí sabemos en qué
# ZONA está cada entrega (viene en la dirección: "...zona 12..."). Esto se
# usa para penalizar los tramos de la ruta que saltan de una zona a otra, de
# modo que el optimizador prefiera terminar todas las entregas de una zona
# antes de moverse a la siguiente, en vez de zigzaguear entre zonas lejanas.
st.sidebar.markdown("---")
st.sidebar.subheader("🧭 Orden de ruta")
peso_agrupar_zona_m = st.sidebar.slider(
    "Agrupar entregas por zona (evitar zigzag)",
    min_value=0, max_value=5000, value=1500, step=250,
    help=(
        "Metros de 'penalización' que se suman cada vez que la ruta pasa de "
        "una zona a otra. En 0, el orden se basa solo en la distancia cruda "
        "(puede saltar de zona en zona). Más alto = más probable que primero "
        "termine todas las entregas de una zona antes de ir a la siguiente."
    )
)

# --- GEOCODIFICACIÓN: Google (recomendado) con respaldo gratuito en OSM ---
# OpenStreetMap/Nominatim (gratis) casi nunca tiene indexado el número de casa
# de direcciones guatemaltecas ("9 avenida 25-67 zona 12"): como mucho ubica
# la Zona como área genérica. Google sí tiene ese nivel de detalle mapeado en
# Guatemala. Si el piloto/dueño de la app agrega su API key aquí, se usa
# Google (rápido y preciso); si la deja vacía, sigue funcionando gratis con
# OpenStreetMap, pero con menor precisión y más lento.
st.sidebar.markdown("---")
st.sidebar.subheader("🌍 Geocodificación")
google_api_key = st.sidebar.text_input(
    "Google Maps API Key (opcional)",
    type="password",
    help=(
        "Con una API key de Google Geocoding, las direcciones se ubican con "
        "mucha más precisión en Guatemala y en paralelo (carga rápida). "
        "Sin ella, se usa OpenStreetMap gratis: más lento y menos preciso "
        "para direcciones exactas de avenida/zona."
    )
)


def formatear_telefono_gt(telefono_raw):
    digitos = re.sub(r'\D', '', str(telefono_raw))
    if digitos.startswith('502') and len(digitos) > 8:
        digitos = digitos[3:]
    if len(digitos) >= 8:
        d8 = digitos[-8:]
        return f"+502 {d8[:4]}-{d8[4:]}", f"+502{d8}"
    return str(telefono_raw), re.sub(r'\D', '', str(telefono_raw))


@st.cache_data(show_spinner=False)
def geocodificar_rapido(direccion_raw):
    """
    BUG FIX #3: antes, si la dirección no se encontraba, la función devolvía
    silenciosamente el centro de la Ciudad de Guatemala (14.5950, -90.5120) y
    nada le avisaba al piloto. Eso genera rutas incorrectas sin que nadie lo note:
    el paquete parece estar geolocalizado pero en realidad cayó en un punto genérico.
    Ahora devolvemos también un flag 'encontrado' para poder advertir al usuario.
    """
    dir_clean = str(direccion_raw).strip().lower()
    zona_match = re.search(r'zona\s*(\d+)', dir_clean)
    zona_str = f"Zona {zona_match.group(1)}" if zona_match else "Zona 12"
    via_match = re.search(r'(\d+\s*(?:avenida|calle|av|cll)|avenida\s*petapa|calzada\s*[a-z]+)', dir_clean)
    via_str = via_match.group(1) if via_match else ""
    query = f"{via_str}, {zona_str}, Ciudad de Guatemala, Guatemala" if via_str else f"{dir_clean}, Guatemala"

    try:
        location = geocode_rl(query)
        if location:
            return location.latitude, location.longitude, True
        loc_gen = geocode_rl(f"{zona_str}, Ciudad de Guatemala, Guatemala")
        if loc_gen:
            return loc_gen.latitude, loc_gen.longitude, True
    except Exception:
        pass

    return 14.5950, -90.5120, False


@st.cache_data(show_spinner=False)
def geocodificar_google(direccion_raw, api_key):
    """
    Geocodificador alterno con la API de Google. A diferencia de Nominatim,
    Google sí tiene bien indexado el sistema de direcciones de Guatemala
    (avenida/calle + número de casa + zona/colonia), y soporta muchas
    solicitudes simultáneas sin política de 1 req/seg, por lo que se puede
    paralelizar para que la carga sea rápida.
    """
    query = f"{direccion_raw}, Ciudad de Guatemala, Guatemala"
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": api_key, "region": "gt"},
            timeout=5
        ).json()
        if resp.get("status") == "OK" and resp.get("results"):
            loc = resp["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"], True
    except Exception:
        pass
    return 14.5950, -90.5120, False


def geocodificar_direccion(direccion_raw, api_key=None):
    """
    Punto único de geocodificación usado por el resto de la app: usa Google
    si hay API key configurada (rápido y preciso para Guatemala); si no, cae
    al Nominatim/OSM gratuito ya existente (más lento, cobertura limitada).
    """
    if api_key:
        return geocodificar_google(direccion_raw, api_key)
    return geocodificar_rapido(direccion_raw)


def calcular_matriz_distancias_haversine(puntos):
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    n = len(puntos)
    matriz = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            matriz[i][j] = int(haversine(puntos[i]['lat'], puntos[i]['lon'], puntos[j]['lat'], puntos[j]['lon']))
    return matriz


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
    # BUG FIX #7: si solo queda 1 columna disponible tras quitar teléfono/dirección,
    # el original igual intentaba usar cols_restantes[1] -> IndexError y la app se caía.
    if len(cols_restantes) > 1:
        mapa['nombre'] = cols_restantes[1]
        mapa['warehouse'] = cols_restantes[0]
    elif len(cols_restantes) == 1:
        mapa['nombre'] = cols_restantes[0]
        mapa['warehouse'] = cols_restantes[0]
    else:
        mapa['nombre'] = mapa['direccion']
        mapa['warehouse'] = mapa['direccion']

    return mapa


def extraer_zona(direccion_raw):
    """
    Extrae el número de zona de Guatemala desde el texto de la dirección
    (ej. "...zona 12..." -> 12). Se usa para agrupar la ruta por zona y
    evitar zigzag, sin depender de qué tan preciso haya quedado el pin en
    el mapa.
    """
    m = re.search(r'zona\s*(\d+)', str(direccion_raw).lower())
    return int(m.group(1)) if m else None


def generar_id_estable(warehouse, nombre, direccion, telefono_clean):
    """
    BUG FIX #2: el id original era solo idx+1 (posición en el Excel). Si el piloto
    subía un segundo archivo en la misma sesión, un paquete nuevo en la fila 3
    heredaba el estado ('Entregado ✅') que había quedado guardado para el paquete
    viejo que estuvo en esa misma fila 3. Con un id basado en el contenido real del
    paquete, ese arrastre de estados incorrectos ya no puede pasar.
    """
    base = f"{warehouse}|{nombre}|{direccion}|{telefono_clean}"
    return hashlib.md5(base.encode('utf-8')).hexdigest()[:12]


def construir_paquetes_desde_df(df):
    """
    Toma cualquier DataFrame (venga de Excel o de una tabla extraída de un PDF),
    detecta sus columnas con detectar_columnas_inteligente() y arma la lista de
    paquetes en el formato interno de la app (sin id todavía).
    """
    mapa = detectar_columnas_inteligente(df)
    paquetes = []
    for _, row in df.iterrows():
        val_wh = str(row[mapa['warehouse']]).strip() if pd.notna(row[mapa['warehouse']]) else ''
        val_dir = str(row[mapa['direccion']]).strip() if pd.notna(row[mapa['direccion']]) else ''
        if not val_wh and not val_dir:
            continue
        tel_fmt, tel_clean = formatear_telefono_gt(row[mapa['telefono']])
        paquetes.append({
            'warehouse': val_wh,
            'nombre': str(row[mapa['nombre']]).strip(),
            'direccion': val_dir,
            'telefono_fmt': tel_fmt,
            'telefono_clean': tel_clean
        })
    return paquetes


def extraer_tabla_pdf(contenido_bytes):
    """
    Intento #1 de lectura de PDF: muchas guías/manifiestos de entrega son en
    realidad tablas exportadas a PDF. pdfplumber puede detectar las líneas de
    la tabla y reconstruirla como si fuera un Excel, reutilizando toda la
    lógica de detección de columnas que ya existe para Excel.
    """
    dfs = []
    try:
        with pdfplumber.open(io.BytesIO(contenido_bytes)) as pdf:
            for page in pdf.pages:
                for tabla in (page.extract_tables() or []):
                    if tabla and len(tabla) > 1:
                        encabezado = [str(c) if c else f"Col_{i+1}" for i, c in enumerate(tabla[0])]
                        try:
                            df_t = pd.DataFrame(tabla[1:], columns=encabezado)
                            dfs.append(df_t)
                        except Exception:
                            continue
    except Exception:
        return None

    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return None


def extraer_bloques_texto_pdf(contenido_bytes):
    """
    Intento #2 (respaldo): si el PDF no trae una tabla real sino texto libre
    (p. ej. una lista de pedidos con nombre/dirección/teléfono en líneas
    sueltas), esta función agrupa las líneas en "bloques" usando el número de
    teléfono como ancla — asume que cada bloque de líneas que termina en un
    teléfono corresponde a un paquete. Dentro de cada bloque, identifica la
    dirección por palabras clave (zona, calle, avenida, etc.) igual que la
    detección de columnas de Excel, y separa lo demás en warehouse/nombre.
    Solo funciona con PDFs que tengan texto seleccionable, NO con
    PDFs escaneados como imagen (esos requerirían OCR).
    """
    lineas = []
    try:
        with pdfplumber.open(io.BytesIO(contenido_bytes)) as pdf:
            for page in pdf.pages:
                texto = page.extract_text() or ""
                for l in texto.split("\n"):
                    l = l.strip()
                    if l:
                        lineas.append(l)
    except Exception:
        return []

    patron_tel = re.compile(r'(\+?502[\s-]?)?\d{4}[\s-]?\d{4}\b')
    palabras_dir = ['zona', 'calle', 'avenida', 'av.', 'av ', 'cll', 'calzada', 'blvd',
                     'colonia', 'residencial', 'lote', 'km ', 'sector', 'callejón',
                     'callejon', 'diagonal', 'carretera']

    bloques = []
    bloque_actual = []
    for linea in lineas:
        bloque_actual.append(linea)
        if patron_tel.search(linea):
            bloques.append(bloque_actual)
            bloque_actual = []
    # Cualquier resto sin teléfono al final (pies de página, totales, etc.) se descarta.

    paquetes = []
    for bloque in bloques:
        tel_line = next((l for l in bloque if patron_tel.search(l)), '')
        m = patron_tel.search(tel_line)
        tel_raw = m.group(0) if m else ''
        tel_fmt, tel_clean = formatear_telefono_gt(tel_raw)

        otras = [l for l in bloque if l != tel_line]
        direccion_partes = [
            l for l in otras
            if any(p in l.lower() for p in palabras_dir) or re.search(r'\d+\s*-\s*\d+', l)
        ]
        resto = [l for l in otras if l not in direccion_partes]

        direccion = ", ".join(direccion_partes) if direccion_partes else (resto.pop(0) if resto else "Sin dirección")

        if len(resto) >= 2:
            warehouse, nombre = resto[0], resto[1]
        elif len(resto) == 1:
            warehouse, nombre = "", resto[0]
        else:
            warehouse, nombre = "", "Cliente"

        paquetes.append({
            'warehouse': warehouse,
            'nombre': nombre,
            'direccion': direccion,
            'telefono_fmt': tel_fmt,
            'telefono_clean': tel_clean
        })

    return paquetes


def procesar_pdf(contenido_bytes):
    """
    Orquesta la extracción de un PDF: primero intenta tablas (más confiable
    y ordenado); si no encuentra ninguna, cae al método de bloques de texto
    anclados por teléfono. Devuelve (lista_de_paquetes_sin_id, metodo_usado).
    """
    df_tabla = extraer_tabla_pdf(contenido_bytes)
    if df_tabla is not None and not df_tabla.empty and len(df_tabla.columns) >= 2:
        paquetes = construir_paquetes_desde_df(df_tabla)
        if paquetes:
            return paquetes, 'tabla'

    return extraer_bloques_texto_pdf(contenido_bytes), 'texto'


# ---------------- UI STREAMLIT ----------------
st.subheader("1. Cargar Archivo de Entregas")
archivo = st.file_uploader(
    "Selecciona archivo Excel (.xlsx, .xls) o PDF (.pdf)",
    type=["xlsx", "xls", "pdf"]
)

if archivo:
    contenido = archivo.getvalue()
    hash_actual = hashlib.md5(contenido).hexdigest()

    # BUG FIX #4: antes, este bloque volvía a parsear el Excel EN CADA RERUN
    # (por ejemplo cada vez que el piloto tocaba "Entregado"), porque el
    # file_uploader sigue "activo" mientras el archivo esté cargado. Además,
    # si el piloto subía un archivo distinto, la ruta ya calculada y los
    # estados de paquetes quedaban mezclados con datos del archivo anterior.
    # Ahora solo se reprocesa cuando el archivo realmente cambió, y se limpia
    # el estado de la ruta anterior.
    if hash_actual != st.session_state.archivo_hash:
        extension = archivo.name.lower().rsplit('.', 1)[-1]

        if extension == 'pdf':
            with st.spinner("Leyendo PDF y extrayendo datos de entrega..."):
                paquetes_sin_id, metodo = procesar_pdf(contenido)
            if not paquetes_sin_id:
                st.error(
                    "⚠️ No se pudieron extraer paquetes del PDF. Verifica que el "
                    "documento tenga texto seleccionable (no sea un escaneo/imagen) "
                    "y que cada pedido incluya un número de teléfono, ya que se usa "
                    "para separar un pedido de otro."
                )
            else:
                st.info(
                    f"📄 PDF procesado con detección de "
                    f"{'tablas' if metodo == 'tabla' else 'texto (líneas agrupadas por número de teléfono)'}."
                    " Revisa la tabla de abajo antes de calcular la ruta, por si algún "
                    "dato quedó mal separado."
                )
        else:
            df_raw = pd.read_excel(io.BytesIO(contenido), header=None)
            primer_celda = str(df_raw.iloc[0, 0]).strip().lower()
            tiene_encabezado = not (primer_celda.isdigit() or len(primer_celda) > 8)

            if tiene_encabezado:
                df = pd.read_excel(io.BytesIO(contenido))
            else:
                df = df_raw.copy()
                df.columns = [f"Col_{i+1}" for i in range(len(df.columns))]

            paquetes_sin_id = construir_paquetes_desde_df(df)

        paquetes = []
        for p in paquetes_sin_id:
            id_estable = generar_id_estable(p['warehouse'], p['nombre'], p['direccion'], p['telefono_clean'])
            paquetes.append({'id': id_estable, **p})

        st.session_state.puntos_cargados = paquetes
        st.session_state.archivo_hash = hash_actual
        # Nuevo archivo = nueva ruta. Se descarta la ruta/estados del archivo anterior.
        st.session_state.secuencia_optima = None
        st.session_state.puntos_ruta = None
        st.session_state.estados_paquetes = {}
        st.session_state.direcciones_fallidas = []

if st.session_state.puntos_cargados:
    st.write(f"### Se detectaron {len(st.session_state.puntos_cargados)} paquetes")
    st.dataframe(pd.DataFrame(st.session_state.puntos_cargados))

    if st.button("🚀 Calcular Ruta Exacta y Hora Estimada", type="primary"):
        with st.spinner("Procesando GPS de piloto y ruta exacta..."):

            puntos_completos = [{
                'id': 'DEPOT',
                'warehouse': 'INICIO-PILOTO',
                'nombre': 'Ubicación Actual Piloto',
                'direccion': 'Punto de partida (GPS Piloto)',
                'telefono_fmt': 'N/A',
                'telefono_clean': '',
                'lat': st.session_state.gps_piloto['lat'],
                'lon': st.session_state.gps_piloto['lon'],
                'zona': None  # el punto de partida no tiene "zona" fija: puede conectar libre a la más cercana
            }]

            # OPTIMIZACIÓN DE VELOCIDAD: se geocodifica cada dirección ÚNICA solo
            # una vez (en tu archivo de prueba, 14 paquetes son en realidad solo
            # 4 direcciones distintas), en vez de repetir la misma consulta por
            # cada paquete. Con Google además se hace en paralelo.
            direcciones_unicas = list({pkt['direccion'] for pkt in st.session_state.puntos_cargados})
            resultados_geo = {}
            barra_geo = st.progress(0.0, text="Ubicando direcciones...")

            if google_api_key:
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futuros = {
                        executor.submit(geocodificar_direccion, d, google_api_key): d
                        for d in direcciones_unicas
                    }
                    completados = 0
                    for fut in concurrent.futures.as_completed(futuros):
                        d = futuros[fut]
                        resultados_geo[d] = fut.result()
                        completados += 1
                        barra_geo.progress(
                            completados / len(direcciones_unicas),
                            text=f"Ubicando direcciones... ({completados}/{len(direcciones_unicas)})"
                        )
            else:
                # Nominatim exige máx. 1 solicitud/seg: aquí debe ir secuencial,
                # o el servicio gratuito puede bloquear la IP a mitad de proceso.
                for i, d in enumerate(direcciones_unicas):
                    resultados_geo[d] = geocodificar_direccion(d, None)
                    barra_geo.progress(
                        (i + 1) / len(direcciones_unicas),
                        text=f"Ubicando direcciones... ({i + 1}/{len(direcciones_unicas)})"
                    )

            barra_geo.empty()

            fallidas = []
            for pkt in st.session_state.puntos_cargados:
                lat, lon, encontrado = resultados_geo[pkt['direccion']]
                if not encontrado:
                    fallidas.append(f"{pkt['nombre']} — {pkt['direccion']}")
                puntos_completos.append({
                    'id': pkt['id'],
                    'warehouse': pkt['warehouse'],
                    'nombre': pkt['nombre'],
                    'direccion': pkt['direccion'],
                    'telefono_fmt': pkt['telefono_fmt'],
                    'telefono_clean': pkt['telefono_clean'],
                    'lat': lat,
                    'lon': lon,
                    'zona': extraer_zona(pkt['direccion'])
                })
                if pkt['id'] not in st.session_state.estados_paquetes:
                    st.session_state.estados_paquetes[pkt['id']] = "Pendiente ⏳"

            st.session_state.direcciones_fallidas = fallidas

            matriz_distancias = None
            try:
                coords = ";".join([f"{p['lon']},{p['lat']}" for p in puntos_completos])
                url = f"{OSRM_URL}/{coords}?annotations=distance"
                resp = requests.get(url, timeout=5).json()
                if 'distances' in resp:
                    matriz_distancias = resp['distances']
            except Exception:
                pass

            if not matriz_distancias:
                matriz_distancias = calcular_matriz_distancias_haversine(puntos_completos)

            # --- AGRUPAR POR ZONA: se arma una matriz de COSTO aparte (con
            # penalización por cruce de zona) que solo usa el optimizador para
            # decidir el ORDEN de la ruta. La matriz de distancias real
            # (matriz_distancias) se deja intacta para que los km y el tiempo
            # estimado que ve el piloto sigan siendo la distancia real, no la
            # distancia + penalización artificial.
            matriz_costo = matriz_distancias
            if peso_agrupar_zona_m > 0:
                zonas = [p.get('zona') for p in puntos_completos]
                n_pts = len(puntos_completos)
                matriz_costo = [fila[:] for fila in matriz_distancias]
                for i in range(n_pts):
                    for j in range(n_pts):
                        if i != j and zonas[i] is not None and zonas[j] is not None and zonas[i] != zonas[j]:
                            matriz_costo[i][j] += peso_agrupar_zona_m

            manager = pywrapcp.RoutingIndexManager(len(puntos_completos), 1, 0)
            routing = pywrapcp.RoutingModel(manager)

            def distance_callback(from_index, to_index):
                return int(matriz_costo[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])

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
            else:
                # BUG FIX #5: si OR-Tools no encontraba solución, el script simplemente
                # no hacía nada y el piloto se quedaba sin ruta ni ningún mensaje de error.
                st.error("⚠️ No se pudo calcular una ruta con los datos proporcionados. Verifica las direcciones e intenta de nuevo.")

if st.session_state.direcciones_fallidas:
    with st.expander(f"⚠️ {len(st.session_state.direcciones_fallidas)} dirección(es) no se pudieron ubicar con precisión (se usó un punto genérico)"):
        for d in st.session_state.direcciones_fallidas:
            st.write(f"- {d}")

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
                        # El link usa la DIRECCIÓN TAL CUAL fue ingresada (no las
                        # coordenadas geocodificadas), para que Google Maps la
                        # busque exactamente como viene en el Excel/PDF.
                        gmaps_url = "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(
                            f"{pt['direccion']}, Ciudad de Guatemala, Guatemala"
                        )

                        st.markdown(f"**Parada {paso:02d}:** [{pt['warehouse']}] **{pt['nombre']}**")
                        st.markdown(f"📍 [{pt['direccion']}]({gmaps_url})")
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

                        st.markdown(f"[🗺️ Navegar en Google Maps (dirección exacta)]({gmaps_url})")
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

                nombre_pt = pt.get('nombre', 'Punto de Inicio')
                tel_pt = pt.get('telefono_fmt', 'N/A')
                pkt_id_pt = pt.get('id', 'DEPOT')

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

    with tab_historial:
        st.subheader("📋 Registro e Historial de Paquetes Procesados")

        historial_datos = []
        for idx in st.session_state.secuencia_optima:
            pt = st.session_state.puntos_ruta[idx]
            if pt.get('id', 'DEPOT') != 'DEPOT':
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
        # BUG FIX #8: el original identificaba el paquete a reactivar por
        # 'warehouse' (texto). Si dos paquetes compartían el mismo valor de
        # warehouse, el botón "Actualizar Estado" cambiaba el estado de AMBOS
        # a la vez sin que el piloto se diera cuenta. Ahora cada opción del
        # selectbox lleva el id estable, aunque el texto mostrado se repita.
        opciones_dict = {
            f"{p['warehouse']} — {p['nombre']} ({p['id'][:6]})": p['id']
            for p in st.session_state.puntos_cargados
        }

        if opciones_dict:
            col_sel, col_est = st.columns([2, 1])
            with col_sel:
                etiqueta_sel = st.selectbox("Selecciona un paquete para modificar su estado:", options=list(opciones_dict.keys()))
                id_sel = opciones_dict[etiqueta_sel]
            with col_est:
                nuevo_est = st.selectbox("Nuevo Estado:", ["Pendiente ⏳", "Entregado ✅", "Ausente 👤", "No Entregado ❌"])
                if st.button("Actualizar Estado"):
                    st.session_state.estados_paquetes[id_sel] = nuevo_est
                    st.success(f"Estado de {etiqueta_sel} actualizado a {nuevo_est}")
                    st.rerun()