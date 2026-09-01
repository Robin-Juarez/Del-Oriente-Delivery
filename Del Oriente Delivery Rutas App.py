import pyodbc
import requests
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# ==============================================================================
# CONFIGURACIONES GLOBALES (MICROSOFT SQL SERVER)
# ==============================================================================

# Cadena de conexión para SQL Server (Ajusta Servidor, BD y credenciales)
# Si usas autenticación de Windows: 'Trusted_Connection=yes;'
# Si usas usuario de SQL: 'UID=tu_usuario;PWD=tu_password;'
# Configuración corregida utilizando raw string (r'...') para LocalDB
SQL_SERVER_CONFIG = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    r"SERVER=(localdb)\MSSQLLocalDB;"
    "DATABASE=DEL_ORIENTE_DELIVERY;"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

# Servidor OSRM local (Docker)
OSRM_URL = "http://localhost:5000/table/v1/driving"

# Punto de inicio/salida (Bodega Central)
BODEGA_ORIGEN = {
    "pedido_id": None,
    "codigo_rastreo": "INICIO_BODEGA",
    "direccion": "Bodega Central - Punto de Salida",
    "latitud": 14.6000,
    "longitud": -90.5100
}

# ID del piloto asignado (UUID válido existente en tu tabla 'pilotos' de SQL Server)
PILOTO_ID_ASIGNADO = "F655B448-5944-4C35-9FAE-5D1C5CCBB271"


# ==============================================================================
# 1. EXTRACCIÓN DE DATOS DESDE SQL SERVER
# ==============================================================================
def obtener_pedidos_pendientes():
    """Consulta los paquetes pendientes en SQL Server usando sintaxis T-SQL."""
    query = """
    SELECT 
        CAST(p.id AS VARCHAR(36)) AS pedido_id,
        p.codigo_rastreo,
        p.prioridad,
        CONCAT(d.tipo_via, ' ', d.numero_via, ' #', d.numero_casa, ', Zona ', d.zona, 
               ISNULL(', ' + d.colonia_barrio, '')) AS direccion,
        d.ubicacion_geografica.Lat AS latitud,   -- Sintaxis T-SQL para Latitud
        d.ubicacion_geografica.Long AS longitud  -- Sintaxis T-SQL para Longitud
    FROM pedidos p
    INNER JOIN direcciones d ON p.direccion_destino_id = d.id
    WHERE p.estado = 'pendiente' 
      AND d.ubicacion_geografica IS NOT NULL
    ORDER BY 
        d.zona ASC,
        d.tipo_via ASC,
        d.numero_via ASC;
    """
    
    conn = pyodbc.connect(SQL_SERVER_CONFIG)
    cursor = conn.cursor()
    cursor.execute(query)
    
    # Convertir resultados a lista de diccionarios
    columnas = [column[0] for column in cursor.description]
    registros = [dict(zip(columnas, row)) for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    return registros


# ==============================================================================
# 2. CONSULTA DE MATRIZ DE VÍAS REALES A OSRM
# ==============================================================================
def obtener_matriz_osrm(puntos):
    """Solicita a OSRM la matriz NxN de duraciones (segundos) y distancias (metros)."""
    coordenadas_str = ";".join([f"{p['longitud']},{p['latitud']}" for p in puntos])
    url = f"{OSRM_URL}/{coordenadas_str}?annotations=duration,distance"
    
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") == "Ok":
            matriz_duraciones = [[int(cell) for cell in fila] for fila in data["durations"]]
            matriz_distancias = [[int(cell) for cell in fila] for fila in data["distances"]]
            return matriz_duraciones, matriz_distancias
        else:
            raise Exception(f"Respuesta inesperada de OSRM: {data.get('code')}")
            
    except requests.exceptions.RequestException as e:
        print(f"\n❌ Error conectando al servicio OSRM Docker: {e}")
        print("Asegúrate de haber levantado el contenedor en Docker Desktop.")
        exit(1)


# ==============================================================================
# 3. MOTOR DE OPTIMIZACIÓN GOOGLE OR-TOOLS (TSP)
# ==============================================================================
def resolver_ruta_optima(matriz_costos):
    """Calcula el orden óptimo de entrega con OR-Tools (Lógica idéntica)."""
    num_puntos = len(matriz_costos)
    manager = pywrapcp.RoutingIndexManager(num_puntos, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def callback_tiempo(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return matriz_costos[from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(callback_tiempo)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )

    solution = routing.SolveWithParameters(search_parameters)
    
    if not solution:
        return None, 0

    secuencia_nodos = []
    index = routing.Start(0)
    tiempo_total_segundos = 0
    
    while not routing.IsEnd(index):
        secuencia_nodos.append(manager.IndexToNode(index))
        prev_index = index
        index = solution.Value(routing.NextVar(index))
        tiempo_total_segundos += routing.GetArcCostForVehicle(prev_index, index, 0)
        
    return secuencia_nodos, tiempo_total_segundos


# ==============================================================================
# 4. PERSISTENCIA EN SQL SERVER
# ==============================================================================
def guardar_ruta_bd(piloto_id, puntos_ordenados, distancia_total_km, tiempo_total_min):
    """Guarda la cabecera y el detalle ordenado en las tablas de SQL Server."""
    conn = pyodbc.connect(SQL_SERVER_CONFIG)
    cursor = conn.cursor()
    
    try:
        # 1. Insertar Encabezado de la Ruta (Usando OUTPUT INSERTED.id para recuperar el UUID generado por NEWID())
        cursor.execute("""
            INSERT INTO rutas (piloto_id, estado, distancia_total_km, tiempo_estimado_minutos)
            OUTPUT INSERTED.id
            VALUES (?, 'optimizada', ?, ?);
        """, (piloto_id, distancia_total_km, tiempo_total_min))
        
        ruta_id = cursor.fetchone()[0]
        
        # 2. Insertar Detalle de Paradas (omitimos el punto 0 que es la bodega)
        orden_visita = 1
        for punto in puntos_ordenados[1:]:
            cursor.execute("""
                INSERT INTO detalles_ruta (ruta_id, pedido_id, orden_visita)
                VALUES (?, ?, ?);
            """, (ruta_id, punto['pedido_id'], orden_visita))
            
            # Actualizar estado del pedido en SQL Server
            cursor.execute("""
                UPDATE pedidos SET estado = 'asignado' WHERE id = ?;
            """, (punto['pedido_id'],))
            
            orden_visita += 1
            
        conn.commit()
        print(f"\n✅ Ruta guardada correctamente en SQL Server. ID Ruta: {ruta_id}")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error al guardar en SQL Server: {e}")
    finally:
        cursor.close()
        conn.close()


# ==============================================================================
# EJECUCIÓN DEL PROGRAMA
# ==============================================================================
if __name__ == "__main__":
    print("--------------------------------------------------")
    print(" SISTEMA DE OPTIMIZACIÓN LOGÍSTICA (SQL SERVER) ")
    print("--------------------------------------------------")
    
    print("\n1. Obteniendo paquetes pendientes desde SQL Server...")
    pedidos = obtener_pedidos_pendientes()
    
    if not pedidos:
        print("No hay paquetes pendientes para procesar.")
        exit()
        
    puntos_totales = [BODEGA_ORIGEN] + pedidos
    print(f"-> Se procesarán {len(pedidos)} entregas (+1 Bodega de salida).")

    print("\n2. Consultando matriz de red vial a OSRM Docker...")
    matriz_tiempos, matriz_distancias = obtener_matriz_osrm(puntos_totales)

    print("\n3. Calculando la secuencia óptima con OR-Tools...")
    secuencia_indices, tiempo_segundos = resolver_ruta_optima(matriz_tiempos)

    if secuencia_indices:
        tiempo_minutos = round(tiempo_segundos / 60)
        distancia_metros_totales = 0
        puntos_ordenados = []
        
        print("\n==================================================")
        print(f" RESUMEN DE HOJA DE RUTA (Tiempo Est: {tiempo_minutos} min)")
        print("==================================================")
        
        for paso, idx in enumerate(secuencia_indices):
            punto = puntos_totales[idx]
            puntos_ordenados.append(punto)
            
            if paso == 0:
                print(f" [ SALIDA ] 🏁 {punto['direccion']}")
            else:
                idx_anterior = secuencia_indices[paso - 1]
                dist_tramo = matriz_distancias[idx_anterior][idx]
                tiempo_tramo = round(matriz_tiempos[idx_anterior][idx] / 60, 1)
                distancia_metros_totales += dist_tramo
                
                print(f" [Parada {paso:02d}] 📦 {punto['codigo_rastreo']} | {punto['direccion']}")
                print(f"               └─ Tramo: {dist_tramo}m (~{tiempo_tramo} min)")
                
        distancia_km_totales = round(distancia_metros_totales / 1000, 2)
        print("--------------------------------------------------")
        print(f" Distancia Total a Recorrer: {distancia_km_totales} km")

        # Guardar en la Base de Datos
        guardar_ruta_bd(
            PILOTO_ID_ASIGNADO, 
            puntos_ordenados, 
            distancia_km_totales, 
            tiempo_minutos
        )
    else:
        print("\n❌ No fue posible calcular la ruta óptima.")
        
        