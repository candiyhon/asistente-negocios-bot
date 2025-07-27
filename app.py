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
        requests.post(n8n_webhook_url, headers=headers, json=data, timeout=10).raise_for_status()
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
            if 'entry' in data and data.get('entry') and data['entry'][0].get('changes') and data['entry'][0]['changes'][0].get('value'):
                value = data['entry'][0]['changes'][0]['value']
                
                if 'statuses' in value:
                    return "OK", 200

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
                        intencion_ayuda = comando in ["ayuda", "comandos", "?"]
                        intencion_renombrar = "renombrar producto" in comando and "a" in comando

                        if intencion_ayuda:
                            mensaje_ayuda = (
                                "¡Hola! 👋 Soy tu Asistente de Negocios. Esto es todo lo que puedo hacer por ti:\n\n"
                                "📦 *INVENTARIO*\n"
                                "• Para ver tu stock: `inventario`\n"
                                "• Para añadir productos: `agregar producto [nombre] [cantidad]`\n"
                                "• Para actualizar stock: `actualizar stock [nombre] [cantidad]`\n"
                                "• Para renombrar: `renombrar producto [viejo] a [nuevo]`\n\n"
                                "💰 *VENTAS*\n"
                                "• Para registrar una venta: `vendí 2 pizzas por 10`\n"
                                "• O también: `2 pizzas por 10`\n\n"
                                "📉 *GASTOS*\n"
                                "• Para registrar un gasto: `gasté 50 en transporte`\n\n"
                                "📊 *REPORTES*\n"
                                "• Para ver el menú de reportes: `reporte`\n\n"
                                "⚙️ *SISTEMA*\n"
                                "• Para borrar la última venta: `borrar ultima venta`\n"
                                "• Para reiniciar tu inventario: `reiniciar inventario`"
                            )
                            enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_ayuda})

                        elif intencion_renombrar:
                            try:
                                partes = comando.split(" a ")
                                nombre_nuevo = partes[1].strip()
                                parte_vieja = partes[0].replace("renombrar producto", "").strip()
                                nombre_viejo = parte_vieja
                                producto_a_renombrar = Producto.query.filter(db.func.lower(Producto.nombre) == db.func.lower(nombre_viejo)).first()
                                if producto_a_renombrar:
                                    producto_a_renombrar.nombre = nombre_nuevo
                                    db.session.commit()
                                    mensaje_respuesta = f"✅ Producto renombrado de '{nombre_viejo}' a '{nombre_nuevo}'."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                                else:
                                    mensaje_respuesta = f"❌ No encontré el producto '{nombre_viejo}' para renombrar."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                            except Exception:
                                mensaje_respuesta = "❌ Formato incorrecto. Usa `renombrar producto [nombre viejo] a [nombre nuevo]`."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})

                        elif intencion_configurar:
                            moneda_elegida = "USD"
                            if "bolivares" in comando or "ves" in comando: moneda_elegida = "VES"
                            negocio.moneda_predeterminada = moneda_elegida; db.session.commit()
                            mensaje_respuesta = f"⚙️ Moneda configurada a {moneda_elegida}."
                            enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                        
                        elif intencion_vender:
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
                                        if producto_en_db.stock >= cantidad:
                                            producto_en_db.stock -= cantidad
                                            nueva_venta = Venta(producto_nombre=producto_en_db.nombre, cantidad=cantidad, precio_total=precio, moneda=moneda_actual)
                                            db.session.add(nueva_venta); db.session.commit()
                                            mensaje_respuesta = f"✅ Venta registrada: {cantidad} x {producto_en_db.nombre}.\nStock restante: {producto_en_db.stock} unidades."
                                        else:
                                            mensaje_respuesta = f"⚠️ No hay suficiente stock para '{producto_en_db.nombre}'. Quedan {producto_en_db.stock} unidades."
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
                        
                        elif intencion_gasto:
                            if not numeros_en_frase:
                                mensaje_respuesta = "❌ No encontré un monto. Intenta con 'gasté 100 en transporte'."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                            else:
                                monto_gasto = float(numeros_en_frase[0])
                                descripcion_parts = []
                                preposiciones = ['en', 'de', 'para']
                                encontrado_prep = False
                                for token in doc:
                                    if token.lower_ in preposiciones and not encontrado_prep:
                                        encontrado_prep = True; continue
                                    if encontrado_prep and not token.like_num:
                                        descripcion_parts.append(token.text)
                                descripcion = " ".join(descripcion_parts) if descripcion_parts else "Gasto sin descripción"
                                moneda_actual = negocio.moneda_predeterminada if negocio else "USD"
                                nuevo_gasto = Gasto(descripcion=descripcion, monto=monto_gasto, moneda=moneda_actual)
                                db.session.add(nuevo_gasto); db.session.commit()
                                mensaje_respuesta = f"✅ Gasto registrado: {monto_gasto:,.2f} {moneda_actual} en '{descripcion}'."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})

                        elif intencion_agregar:
                            partes = comando.split()
                            try:
                                stock_inicial = int(partes[-1]); nombre_producto = " ".join(partes[2:-1]).lower()
                                if not nombre_producto: raise ValueError("Nombre vacío")
                                existe = Producto.query.filter(db.func.lower(Producto.nombre) == db.func.lower(nombre_producto)).first()
                                if not existe:
                                    nuevo_producto = Producto(nombre=nombre_producto, stock=stock_inicial)
                                    db.session.add(nuevo_producto); db.session.commit()
                                    mensaje_respuesta = f"📦 Producto '{nombre_producto}' agregado con {stock_inicial} unidades."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                                else:
                                    mensaje_respuesta = f"📦 El producto '{nombre_producto}' ya existe."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                            except (IndexError, ValueError):
                                mensaje_respuesta = "❌ Formato incorrecto. Usa 'agregar producto [nombre] [cantidad]'."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                        
                        elif intencion_actualizar:
                            partes = comando.split()
                            try:
                                cantidad_a_sumar = int(partes[-1]); nombre_producto = " ".join(partes[2:-1]).lower()
                                if not nombre_producto: raise ValueError("Nombre vacío")
                                producto_en_db = Producto.query.filter(db.func.lower(Producto.nombre) == db.func.lower(nombre_producto)).first()
                                if producto_en_db:
                                    producto_en_db.stock += cantidad_a_sumar
                                    db.session.commit()
                                    mensaje_respuesta = f"📦 Stock de '{producto_en_db.nombre}' actualizado.\nNuevo stock: {producto_en_db.stock} unidades."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                                else:
                                    mensaje_respuesta = f"❌ El producto '{nombre_producto}' no existe."
                                    enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                            except (IndexError, ValueError):
                                mensaje_respuesta = "❌ Formato incorrecto. Usa 'actualizar stock [nombre] [cantidad]'."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                        
                        elif intencion_reiniciar:
                            negocio.estado_conversacion = 'esperando_confirmacion_reinicio'
                            db.session.commit()
                            mensaje_confirmacion = '¿Estás seguro de que quieres borrar TODOS los productos y ventas? Esta acción no se puede deshacer.\n\nEscribe *SÍ* para confirmar.'
                            enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_confirmacion})

                        elif intencion_inventario:
                            todos_los_productos = Producto.query.order_by(Producto.nombre).all()
                            if not todos_los_productos:
                                mensaje_respuesta = "📦 Tu inventario está vacío."
                            else:
                                mensaje_respuesta = "📦 *Inventario Actual:*\n"
                                for p in todos_los_productos:
                                    mensaje_respuesta += f"- {p.nombre}: {p.stock} unidades\n"
                            enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta.strip()})

                        elif intencion_reporte:
                            if comando == 'reporte':
                                payload_instruccion = {"instruccion": "mostrar_menu_reporte"}
                                enviar_a_n8n(numero_usuario, 'instruccion', payload_instruccion)
                            else:
                                comando_reporte = comando.replace(" ", "_")
                                reporte_generado = generar_reporte(comando_reporte)
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': reporte_generado})

                        elif intencion_borrar:
                            ultima_venta = Venta.query.order_by(Venta.fecha_creacion.desc()).first()
                            if not ultima_venta:
                                mensaje_respuesta = "No hay ventas para borrar."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_respuesta})
                            else:
                                negocio.estado_conversacion = 'esperando_confirmacion_borrado'
                                db.session.commit()
                                mensaje_confirmacion = "¿Estás seguro de que deseas borrar la última venta? Responde *sí* para confirmar."
                                enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_confirmacion})

                        else:
                            mensaje_ayuda = ("Disculpa, no entendí ese comando. 🤔\n\n"
                                             "Puedes probar con:\n`inventario`, `vender`, `reporte`, `gasté 50 en...` o `reiniciar inventario`.")
                            enviar_a_n8n(numero_usuario, 'texto', {'mensaje': mensaje_ayuda})

        except Exception as e:
            print(f"❌ ERROR DETALLADO EN EL PROCESAMIENTO:")
            traceback.print_exc()
        finally:
            db.session.remove()
        
        return "OK", 200

# --- Ruta de prueba ---
@app.route("/")
def index():
    return "¡El servidor para el bot de WhatsApp está funcionando!"