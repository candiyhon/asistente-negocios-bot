import os
import json
import requests
import spacy
import traceback
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta

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


# --- FUNCIÓN PARA GENERAR REPORTES ---
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
    
    mensaje_respuesta = f"{titulo_reporte}\n"
    if not ventas_del_periodo:
        mensaje_respuesta += "\nNo se encontraron ventas."
    else:
        ingresos_totales = {}
        for v in ventas_del_periodo:
            ingresos_totales[v.moneda] = ingresos_totales.get(v.moneda, 0) + v.precio_total
        mensaje_respuesta += f"\n- *Total de Ventas:* {len(ventas_del_periodo)}\n- *Ingresos Totales:*\n"
        for moneda, total in ingresos_totales.items():
            mensaje_respuesta += f"  - {total:,.2f} {moneda}\n"
            
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
                if 'messages' in value and value['messages']:
                    mensaje = value['messages'][0]
                    numero_usuario = mensaje['from']

                    if mensaje.get('type') == 'interactive' and mensaje.get('interactive', {}).get('type') == 'list_reply':
                        id_seleccionado = mensaje['interactive']['list_reply']['id']
                        reporte_generado = generar_reporte(id_seleccionado)
                        enviar_a_n8n(numero_usuario, 'texto', {'mensaje': reporte_generado})
                        db.session.remove()
                        return "OK", 200

                    if mensaje.get('type') == 'text':
                        texto_mensaje = mensaje['text']['body']
                        negocio = Negocio.query.get(numero_usuario)
                        
                        if not negocio:
                            nuevo_negocio = Negocio(id=numero_usuario, estado_conversacion='esperando_nombre_negocio')
                            db.session.add(nuevo_negocio); db.session.commit()
                            mensaje_bienvenida = "¡Hola! 👋 Soy tu Asistente de Negocios. Para empezar, vamos a configurar tu perfil. ¿Cuál es el nombre de tu negocio?"
                            enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_bienvenida})
                            db.session.remove()
                            return "OK", 200

                        if negocio.estado_conversacion:
                            estado = negocio.estado_conversacion
                            # (Lógica de estados de conversación completa aquí)
                            db.session.remove()
                            return "OK", 200
                        
                        comando = texto_mensaje.lower()
                        doc = nlp(comando)
                        
                        numeros_en_frase = [token.text for token in doc if token.like_num]
                        intencion_vender = (any(token.lemma_ in ["vender", "vendí"] for token in doc) or (len(numeros_en_frase) >= 2 and "por" in comando))
                        intencion_gasto = any(token.lemma_ in ["gastar", "gasté", "gasto", "pagué", "pagar"] for token in doc)
                        intencion_configurar = any(token.lemma_ in ["configurar", "moneda"] for token in doc)
                        intencion_agregar = "agregar producto" in comando
                        intencion_actualizar = "actualizar stock" in comando
                        intencion_inventario = "inventario" in comando
                        intencion_reporte = "reporte" in comando
                        intencion_borrar = "borrar ultima venta" in comando
                        intencion_reiniciar = "reiniciar inventario" in comando or "restaurar datos" in comando
                        
                        if intencion_vender:
                             if len(numeros_en_frase) >= 2:
                                cantidad = int(numeros_en_frase[0]); precio = float(numeros_en_frase[1])
                                try:
                                    start_index = comando.find(numeros_en_frase[0]) + len(numeros_en_frase[0])
                                    end_index = comando.rfind("por")
                                    if end_index == -1 or end_index < start_index: raise ValueError("Patrón no encontrado")
                                    nombre_producto = comando[start_index:end_index].strip()
                                except Exception:
                                    palabras_a_ignorar = ["vender", "vendí", "por"] + numeros_en_frase
                                    candidatos = [token.lower_ for token in doc if token.text not in palabras_a_ignorar]
                                    nombre_producto = " ".join(candidatos).strip()
                                if nombre_producto:
                                    moneda_actual = negocio.moneda_predeterminada if negocio else "USD"
                                    producto_en_db = Producto.query.filter(db.func.lower(Producto.nombre) == db.func.lower(nombre_producto)).first()
                                    if producto_en_db:
                                        producto_en_db.stock -= cantidad
                                        nueva_venta = Venta(producto_nombre=producto_en_db.nombre, cantidad=cantidad, precio_total=precio, moneda=moneda_actual)
                                        db.session.add(nueva_venta); db.session.commit()
                                        mensaje_respuesta = f"✅ Venta registrada: {cantidad} x {producto_en_db.nombre}.\nStock restante: {producto_en_db.stock} unidades."
                                        enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                                    else:
                                        mensaje_respuesta = f"❌ El producto '{nombre_producto}' no existe en tu inventario."
                                        enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                                else:
                                    mensaje_respuesta = "❌ No pude identificar el nombre del producto."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                            else:
                                mensaje_respuesta = "❌ Faltan datos en el comando de venta."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})

                        # (Y el resto de tus `elif` completos aquí...)

        except Exception as e:
            print(f"❌ ERROR DETALLADO EN EL PROCESAMIENTO:")
            traceback.print_exc()
        finally:
            db.session.remove()
        
        return "OK", 200

@app.route("/")
def index():
    return "¡El servidor para el bot de WhatsApp está funcionando!"