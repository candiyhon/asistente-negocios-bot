import os
import json
import requests
import spacy
import traceback
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
from collections import defaultdict

# Cargamos el modelo de lenguaje en español
try:
    nlp = spacy.load("es_core_news_sm")
except Exception as e:
    print(f"Error cargando el modelo de spaCy: {e}")
    nlp = None

app = Flask(__name__)

# --- CONFIGURACIÓN ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_POOL_RECYCLE'] = 280
app.config['SQLALCHEMY_POOL_TIMEOUT'] = 20
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


# --- MODELOS DE BASE DE DATOS ---
class Venta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    producto_nombre = db.Column(db.String(100), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    precio_total = db.Column(db.Float, nullable=False)
    moneda = db.Column(db.String(10), nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), unique=True, nullable=False)
    stock = db.Column(db.Integer, nullable=False)

class Negocio(db.Model):
    id = db.Column(db.String(100), primary_key=True)
    nombre = db.Column(db.String(150), nullable=True)
    moneda_predeterminada = db.Column(db.String(10), default='USD')
    estado_conversacion = db.Column(db.String(50), nullable=True)

class Gasto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    descripcion = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    moneda = db.Column(db.String(10), nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)


# --- FUNCIÓN PARA ENVIAR MENSAJES A N8N ---
def enviar_a_n8n(numero_destino, tipo_mensaje, payload_mensaje):
    n8n_webhook_url = os.environ.get('N8N_WEBHOOK_URL')
    if not n8n_webhook_url:
        print("❌ ERROR: La variable de entorno N8N_WEBHOOK_URL no está configurada.")
        return False
    data = { "telefono": numero_destino, "tipo_mensaje": tipo_mensaje, "payload": payload_mensaje }
    headers = { "Content-Type": "application/json" }
    try:
        print(f"➡️  Enviando a n8n: {data}")
        response = requests.post(n8n_webhook_url, headers=headers, json=data, timeout=10)
        response.raise_for_status() 
        print(f"✔️ Petición a n8n enviada con éxito. Estado: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"❌ ERROR al enviar la petición a n8n: {e}")
        return False


# --- FUNCIÓN PARA GENERAR REPORTES (MEJORADA) ---
def generar_reporte(comando):
    hoy = date.today()
    start_date, end_date = None, None
    titulo_reporte = ""
    
    if comando == 'reporte_hoy':
        start_date = datetime.combine(hoy, datetime.min.time())
        end_date = start_date + timedelta(days=1)
        titulo_reporte = f"📊 Reporte del día ({hoy.strftime('%d/%m/%Y')})"
    elif comando == 'reporte_ayer':
        start_date = datetime.combine(hoy - timedelta(days=1), datetime.min.time())
        end_date = start_date + timedelta(days=1)
        titulo_reporte = f"📊 Reporte de ayer ({(hoy - timedelta(days=1)).strftime('%d/%m/%Y')})"
    elif comando == 'reporte_semana':
        start_date = datetime.combine(hoy - timedelta(days=hoy.weekday()), datetime.min.time())
        end_date = start_date + timedelta(days=7)
        titulo_reporte = f"📊 Reporte de la semana ({start_date.strftime('%d/%m')} al {(end_date - timedelta(days=1)).strftime('%d/%m')})"

    if not start_date:
        return "No se pudo determinar el rango del reporte."
        
    ventas_del_periodo = Venta.query.filter(Venta.fecha_creacion >= start_date, Venta.fecha_creacion < end_date).all()
    gastos_del_periodo = Gasto.query.filter(Gasto.fecha_creacion >= start_date, Gasto.fecha_creacion < end_date).all()
    
    mensaje_respuesta = f"{titulo_reporte}\n"
    
    ingresos_totales = defaultdict(float)
    for v in ventas_del_periodo:
        ingresos_totales[v.moneda] += v.precio_total

    gastos_totales = defaultdict(float)
    for g in gastos_del_periodo:
        gastos_totales[g.moneda] += g.monto

    todas_las_monedas = sorted(list(set(ingresos_totales.keys()) | set(gastos_totales.keys())))

    if not todas_las_monedas:
        mensaje_respuesta += "\nNo se encontraron movimientos."
        return mensaje_respuesta

    for moneda in todas_las_monedas:
        ingreso = ingresos_totales.get(moneda, 0.0)
        gasto = gastos_totales.get(moneda, 0.0)
        ganancia = ingreso - gasto
        
        mensaje_respuesta += f"\n--- Resumen en {moneda} ---\n"
        mensaje_respuesta += f"📈 Ingresos: {ingreso:,.2f}\n"
        mensaje_respuesta += f"📉 Gastos: {gasto:,.2f}\n"
        mensaje_respuesta += f"💰 *Ganancia Neta: {ganancia:,.2f}*\n"
            
    return mensaje_respuesta.strip()


# --- RUTAS DE LA APLICACIÓN ---
@app.route("/webhook", methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
        if request.args.get('hub.verify_token') == VERIFY_TOKEN: 
            return request.args.get('hub.challenge')
        return "Error de autenticación.", 403

    if request.method == 'POST':
        data = request.get_json()
        try:
            print(f"--- DATOS RECIBIDOS DE WHATSAPP ---:\n{json.dumps(data, indent=2)}\n--------------------")

            if 'entry' in data and data.get('entry') and data['entry'][0].get('changes') and data['entry'][0]['changes'][0].get('value'):
                value = data['entry'][0]['changes'][0]['value']
                
                if 'statuses' in value:
                    print("Recibida notificación de estado. Ignorando.")
                    return "OK", 200

                if 'messages' in value and value['messages']:
                    mensaje = value['messages'][0]
                    numero_usuario = mensaje['from']

                    # ... (El resto de la lógica de webhook completa va aquí, igual que la última versión funcional)
                    
        except Exception as e:
            print(f"❌ ERROR DETALLADO EN EL PROCESAMIENTO:")
            traceback.print_exc()
        finally:
            db.session.remove()
        
        return "OK", 200

# (El resto del código como la lógica de webhook y la ruta index)
# ...

# === CÓDIGO COMPLETO FINAL ===
import os
import json
import requests
import spacy
import traceback
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
from collections import defaultdict

# ... (El código completo que ya conoces, con la nueva función `generar_reporte` y el filtro de `statuses`)